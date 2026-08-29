# -*- coding: utf-8 -*-
"""stock-analysis 本地执行器主入口（runner）——完整 T+1 模拟买卖循环。

每日 09:26（竞价结束后）自动执行一轮：
  Phase 1 平仓：对全部未平仓持仓跑策略引擎（strategy.sell_decision）
    - 昨日断板 → 今日开盘卖（回测：拖到 T+2 平均 -1.18%）
    - 昨日续板但今日高开低走/日内涨≥5% → 锁定利润
    - 持仓 ≥3 交易日 → 无条件清仓
  Phase 2 开仓：新信号 → 决策线裁决（gap≥2% 才买）→ 最优变体分级过滤
    - A级 gap>5%+st≥3+市值60-150亿（胜率 62.2%/+2.71%）
    - B级 st≥3+60-150亿（61.8%）、C级 gap>5%+60-150亿 半仓（55.5%）
    - 其余放弃（全样本仅 48.7%/+0.37%，不值得占仓位）
  → 过风控闸门 → broker 下单 → 记录 → 可选推送

用法：
  python tools/executor/runner.py --now        # 立即执行一轮（测试/手动）
  python tools/executor/runner.py --loop       # 常驻模式，每天 09:26 自动执行
  python tools/executor/runner.py --summary    # 查看模拟盘持仓概览
  python tools/executor/runner.py --report     # 月度盈亏报告（全流水+统计）
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exec_core import fetch_user_data, extract_signals, realtime_quote, auction_gate, SITE
from risk_gate import RiskGate
import broker_sim
import strategy

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_cfg():
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg))


def _notify(cfg, title, text):
    key = ((cfg.get("notify") or {}).get("serverchan_key") or "").strip()
    if not key:
        _log("（未配置 ServerChan，跳过推送）")
        return
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({"title": title, "desp": text}).encode()
        urllib.request.urlopen(
            "https://sctapi.ftqq.com/%s.send" % key, data=data, timeout=15)
        _log("已推送：%s" % title)
    except Exception as e:
        _log("推送失败（不影响执行）：%r" % e)


def pick_broker(cfg):
    mode = cfg.get("broker") or "sim"
    if mode == "qmt":
        import broker_qmt
        if not broker_qmt.is_available():
            _log("⚠ broker=qmt 但 xtquant/账户未配置，回落到模拟盘 sim")
            mode = "sim"
        else:
            return broker_qmt.QmtBroker(), "qmt"
    return broker_sim.SimBroker(), mode


# ---------------- Phase 1：平仓 ----------------

def run_sells(broker, mode, cfg):
    """对全部持仓跑卖出策略。返回 (lines, n_sold)。"""
    lines, n_sold = [], 0
    try:
        poss = broker.positions(open_only=True)
    except Exception as e:
        _log("持仓读取失败：%r" % e)
        return lines, 0
    if not poss:
        _log("Phase1 平仓：无持仓")
        return ["- 无持仓"], 0
    _log("Phase1 平仓：%d 笔持仓待裁决" % len(poss))

    codes = [p["code"] for p in poss]
    try:
        quote = realtime_quote(codes)
    except Exception as e:
        _log("行情失败：%r" % e)
        return ["- 行情失败，平仓顺延"], 0

    for p in poss:
        q = quote.get(p["code"])
        klines = []
        try:
            klines = strategy._tencent_kline(p["code"], n=12)
        except Exception as e:
            _log("K线失败 %s：%r" % (p["code"], e))
        try:
            dec = strategy.sell_decision(p, q, klines)
        except Exception as e:
            _log("策略异常 %s：%r" % (p["code"], e))
            dec = {"verdict": "HOLD", "price": 0, "reason": "策略异常，顺延"}
        if dec["verdict"] == "SELL" and dec.get("price"):
            r = broker.sell_limit(p["code"], dec["price"], sig={
                "name": p.get("name"), "reason": dec["reason"], "source": "strategy"})
            if r.get("ok"):
                n_sold += 1
                lines.append("- **SELL %s**(%s) @%.2f 盈亏 %+.2f%%｜%s"
                             % (p.get("name"), p["code"], r["price"],
                                r["pnl_pct"], dec["reason"]))
                _log("卖出 %s：%s" % (p["code"], dec["reason"]))
            else:
                lines.append("- %s(%s) 卖出失败：%s" % (p.get("name"), p["code"], r.get("reason")))
        else:
            lines.append("- HOLD %s(%s)：%.2f%%｜%s"
                         % (p.get("name"), p["code"],
                            ((q.get("price") / p["avg_price"] - 1) * 100) if q else 0,
                            dec["reason"]))
            _log("持有 %s：%s" % (p["code"], dec["reason"]))
    return lines, n_sold


# ---------------- Phase 2：开仓 ----------------

def run_buys(broker, mode, cfg, sigs):
    lines, n_buy = [], 0
    if not sigs:
        return ["- 今日无新信号"], 0
    codes = [s["code"] for s in sigs]
    try:
        quote = realtime_quote(codes)
    except Exception as e:
        _log("✗ 行情失败：%r" % e)
        return ["- 行情失败，开仓顺延"], 0
    if not quote:
        return ["- 行情为空（可能非交易时段）"], 0

    gate = RiskGate((cfg.get("risk") or {}))
    bal = broker.balance() if hasattr(broker, "balance") else {}
    total = bal.get("total")
    _log("broker=%s | 总资产 %.0f | 熔断=%s"
         % (mode, total or 0, "YES" if gate.tripped else "no"))

    for s in sigs:
        verdict = auction_gate(s, quote)
        if verdict["verdict"] != "BUY":
            gate.record(verdict, verdict["verdict"], 0, verdict["reason"])
            lines.append("- %s(%s) %s：%.2f%%｜%s"
                         % (verdict["name"], verdict["code"], verdict["verdict"],
                            verdict["open_gap"] or 0, verdict["reason"]))
            continue
        # 决策线通过 → 最优变体分级
        q = quote.get(verdict["code"]) or {}
        mc = q.get("float_mv") or None
        sf = strategy.strategy_filter(verdict, q, mc)
        if sf["grade"] == "X":
            gate.record(verdict, "SKIP", 0, sf["reason"])
            lines.append("- %s(%s) 放弃：%.2f%%｜%s"
                         % (verdict["name"], verdict["code"],
                            verdict["open_gap"] or 0, sf["reason"]))
            continue
        amount = int((gate.cfg["max_trade_amount"]) * sf["weight"])
        if total:
            amount = int(min(amount, total * gate.cfg["max_position_pct"]))
        chk = gate.check(verdict, total)
        if not chk["ok"]:
            gate.record(verdict, "REJECT", 0, chk["reason"])
            lines.append("- %s(%s) 过闸拒绝：%s" % (verdict["name"], verdict["code"], chk["reason"]))
            continue
        price = verdict.get("close") or 0
        r = broker.buy_limit(verdict["code"], price, amount, sig=verdict)
        ok = "✓" if r.get("ok") else "✗ %s" % r.get("reason")
        gate.record(verdict, "BUY", amount, ok)
        lines.append("- **BUY %s**(%s) %s 高开 %.2f%% %d 元｜%s"
                     % (verdict["name"], verdict["code"], sf["grade"],
                        verdict["open_gap"], amount, sf["reason"]))
        if r.get("ok"):
            n_buy += 1
            _log("买入 %s %s：%s" % (verdict["code"], sf["grade"], sf["reason"]))
        else:
            _log("买入失败 %s：%s" % (verdict["code"], r.get("reason")))
    return lines, n_buy


# ---------------- 主流程 ----------------

def run_once(cfg):
    acc = cfg.get("account") or {}
    if not acc.get("user_id") or not acc.get("passwd"):
        _log("未配置 account，退出")
        return
    broker, mode = pick_broker(cfg)

    # Phase 1：平仓（不依赖线上数据，行情+K线就够）
    _log("=" * 30 + " Phase1 平仓 " + "=" * 30)
    sell_lines, n_sold = run_sells(broker, mode, cfg)

    # Phase 2：开仓（需要线上信号）
    _log("=" * 30 + " Phase2 开仓 " + "=" * 30)
    buy_lines, n_buy = [], 0
    _log("拉取线上数据 %s ..." % SITE)
    try:
        data = fetch_user_data(acc["user_id"], acc["passwd"])
        sigs = extract_signals(data)
        _log("信号 %d 条（core+relay+fused 去重）" % len(sigs))
        buy_lines, n_buy = run_buys(broker, mode, cfg, sigs)
    except Exception as e:
        _log("✗ 数据拉取/解密失败：%r" % e)
        buy_lines = ["- 数据拉取失败：%r" % e]

    # 汇总
    _log("=" * 60)
    all_lines = ["## 平仓（%d 笔）" % n_sold] + sell_lines + ["", "## 开仓（%d 笔）" % n_buy] + buy_lines
    for ln in all_lines:
        _log(ln)
    if mode == "sim":
        try:
            _log(broker_sim.SimBroker().summary())
        except Exception as e:
            _log("战绩汇总失败：%r" % e)
    _notify(cfg, "执行器回报（卖%d 买%d）" % (n_sold, n_buy), "\n".join(all_lines))


def run_summary():
    import broker_sim
    b = broker_sim.SimBroker()
    print(b.summary())
    print()
    print("== 持仓中 ==")
    for r in b.con.execute("SELECT buy_date,code,name,buy_price,volume,streak "
                           "FROM sim_positions WHERE sell_date IS NULL ORDER BY buy_date"):
        print("  %s %s %s 成本%.2f %d股 st=%s" % (r[0], r[1], r[2], r[3], r[4], r[5]))
    print()
    print("== 最近平仓 ==")
    for r in b.con.execute("SELECT buy_date,code,name,buy_price,sell_date,sell_price,pnl_pct,"
                           "sell_reason FROM sim_positions WHERE sell_date IS NOT NULL "
                           "ORDER BY sell_date DESC LIMIT 20"):
        print("  %s %s %s %.2f→%.2f (%+.2f%%) %s" % (r[0], r[1], r[2], r[3], r[5], r[6], r[7]))


def run_report(month: str = None):
    """月度盈亏报告：累计收益/已实现/浮盈/胜率/最大单笔盈亏/全流水。"""
    import broker_sim
    b = broker_sim.SimBroker()
    con = b.con
    month = month or time.strftime("%Y-%m")

    rows = con.execute(
        "SELECT buy_date,code,name,buy_price,volume,sell_date,sell_price,pnl_pct,sell_reason "
        "FROM sim_positions ORDER BY buy_date").fetchall()
    in_month = [r for r in rows if (r[0] or "").startswith(month)]
    closed = [r for r in in_month if r[5]]
    holding = [r for r in in_month if not r[5]]

    bal = b.balance()
    init = broker_sim._initial_cash()
    wins = [r for r in closed if (r[7] or 0) > 0]
    win_rate = len(wins) * 100.0 / len(closed) if closed else 0
    pnls = [r[7] for r in closed if r[7] is not None]
    best = max(pnls) if pnls else 0
    worst = min(pnls) if pnls else 0
    realized_pct = sum(pnls)

    print("=" * 62)
    print(" 模拟盘月度报告 %s" % month)
    print("=" * 62)
    print(" 初始资金      : %s 元" % format(int(init), ","))
    print(" 当前总资产    : %s 元（%+.2f%%）" % (format(int(bal["total"]), ","),
                                              (bal["total"] / init - 1) * 100))
    print(" 可用现金      : %s 元" % format(int(bal["cash"]), ","))
    print(" 持仓市值      : %s 元" % format(int(bal["market_value"]), ","))
    print("-" * 62)
    print(" 本月开仓      : %d 笔（已平仓 %d / 持仓中 %d）" % (len(in_month), len(closed), len(holding)))
    print(" 胜率          : %.1f%%（%d/%d）" % (win_rate, len(wins), len(closed)))
    print(" 平均盈亏      : %+.2f%%" % (sum(pnls) / len(pnls) if pnls else 0))
    print(" 最佳/最差单笔 : %+.2f%% / %+.2f%%" % (best, worst))
    print(" 本月累计盈亏  : %+.2f%%（逐笔算术和，费前）" % realized_pct)
    print("-" * 62)
    print(" 全部流水：")
    for r in rows:
        tag = "✓平仓" if r[5] else "·持仓"
        print("  [%s] %s %s(%s) 成本%.2f %d股 %s %s 盈亏%s %s" % (
            tag, r[0], r[2], r[1], r[3], r[4],
            ("→卖出%s" % r[5]) if r[5] else "       ",
            ("@%.2f" % r[6]) if r[6] else "     ",
            ("%+.2f%%" % r[7]) if r[7] is not None else "  --  ",
            r[8] or ""))
    # 逐笔交易明细
    print("-" * 62)
    print(" 委托流水（sim_trades）：")
    for r in con.execute("SELECT date,code,name,action,price,volume,amount,verdict_reason "
                         "FROM sim_trades ORDER BY ts"):
        print("  %s %s %s %s %.2f × %d = %.0f 元  %s" % (
            r[0], r[3], r[2], r[1], r[4], r[5], r[6], (r[7] or "")[:40]))


def main():
    args = sys.argv[1:]
    cfg = load_cfg()
    if "--summary" in args:
        run_summary()
    elif "--report" in args:
        month = None
        for i, a in enumerate(args):
            if a == "--month" and i + 1 < len(args):
                month = args[i + 1]
        run_report(month)
    elif "--now" in args:
        run_once(cfg)
    elif "--loop" in args:
        target = ((cfg.get("schedule") or {}).get("auction_time") or "09:26")
        _log("常驻模式：每天 %s 自动执行（平仓+开仓）（Ctrl+C 退出）" % target)
        while True:
            now = time.strftime("%H:%M")
            if now == target:
                try:
                    run_once(cfg)
                except Exception as e:
                    _log("执行异常：%r" % e)
                time.sleep(60)  # 跳过同一分钟
            time.sleep(5)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
