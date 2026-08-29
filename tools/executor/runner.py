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
  python tools/executor/runner.py --review     # 当日复盘总结（收盘后跑，推送 PushPlus）

交易纪律（用户 2026-08-29 拍板）：
  1. 操作前先判可成交性：一字板/封板买不进、跌停封死卖不出，全部留痕记录
  2. 先预判后成交：按实时价成交（不是预设数值无脑成交），预判后价格已升高
     也只能以当前实时价买入，卖出同理
  3. 买卖理由与明细推送 PushPlus（模拟盘操作段），每日复盘总结盈亏
  4. 每日操作+复盘写入网站「模拟盘」模块（build.py 从 sim_review.json 读）
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
REVIEW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_review.json")


def load_cfg():
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg))


def _notify(cfg, title, text):
    """双通道推送：PushPlus（模拟盘主通道，owner+接收人2）+ ServerChan（可选）。
    任一通道失败不影响执行。"""
    results = []
    ncfg = cfg.get("notify") or {}
    # --- PushPlus ---
    pp_tokens = ncfg.get("pushplus_tokens") or []
    if isinstance(pp_tokens, str):
        pp_tokens = [pp_tokens]
    if not pp_tokens:
        # 回落：复用 pipeline/config/notify.json 里的 wechat_pushplus 配置（多接收人）
        try:
            root_ncfg = os.path.join(ROOT, "config", "notify.json")
            if os.path.exists(root_ncfg):
                with open(root_ncfg, encoding="utf-8") as f:
                    pp = (json.load(f).get("wechat_pushplus") or {}).get("token") or []
                    for x in (pp if isinstance(pp, list) else [pp]):
                        if isinstance(x, dict) and x.get("token"):
                            pp_tokens.append(x["token"])
                        elif isinstance(x, str) and x.strip():
                            pp_tokens.append(x.strip())
        except Exception:
            pass
    if pp_tokens:
        try:
            import urllib.request
            ok = 0
            for tk in pp_tokens:
                try:
                    payload = json.dumps({"token": tk, "title": title, "content": text,
                                          "template": "markdown"}).encode()
                    req = urllib.request.Request(
                        "http://www.pushplus.plus/send", data=payload,
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        js = json.loads(r.read().decode("utf-8"))
                    if js.get("code") == 200:
                        ok += 1
                except Exception as e:
                    _log("PushPlus 单 token 失败：%r" % e)
            results.append("PushPlus %d/%d" % (ok, len(pp_tokens)))
        except Exception as e:
            results.append("PushPlus失败:%r" % e)
    # --- ServerChan（可选备用通道）---
    key = (ncfg.get("serverchan_key") or "").strip()
    if key:
        try:
            import urllib.request
            import urllib.parse
            data = urllib.parse.urlencode({"title": title, "desp": text[:4000]}).encode()
            urllib.request.urlopen(
                "https://sctapi.ftqq.com/%s.send" % key, data=data, timeout=15)
            results.append("ServerChan ok")
        except Exception as e:
            results.append("ServerChan失败:%r" % e)
    if not results:
        _log("（未配置任何推送通道，跳过推送）")
    else:
        _log("已推送 %s：%s" % (" + ".join(results), title))


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
            # 可卖性检查（操作前留痕）：跌停封死卖不出 → 顺延并记录
            cs = strategy.can_sell(q or {}, p["code"])
            if not cs["ok"]:
                _log("卖出被拒 %s：%s" % (p["code"], cs["reason"]))
                if hasattr(broker, "record_reject"):
                    broker.record_reject(p["code"], "SELL", cs["reason"], p.get("name") or "")
                lines.append("- ⛔ **顺延** %s(%s)｜%s"
                             % (p.get("name"), p["code"], cs["reason"]))
                gate_note = dec["reason"]
                continue
            r = broker.sell_limit(p["code"], dec["price"], sig={
                "name": p.get("name"), "reason": dec["reason"], "source": "strategy"})
            if r.get("ok"):
                n_sold += 1
                lines.append("- **SELL %s**(%s) @%.2f 盈亏 %+.2f%%｜%s"
                             % (p.get("name"), p["code"], r["price"],
                                r["pnl_pct"], dec["reason"]))
                _log("卖出 %s：%s" % (p["code"], dec["reason"]))
            else:
                if hasattr(broker, "record_reject"):
                    broker.record_reject(p["code"], "SELL",
                                         "委托失败：%s" % r.get("reason"), p.get("name") or "")
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
        # 可买性检查（操作前留痕）：一字板/封板买不进
        cb = strategy.can_buy(q, verdict["code"])
        if not cb["ok"]:
            gate.record(verdict, "SKIP", 0, cb["reason"])
            if hasattr(broker, "record_reject"):
                broker.record_reject(verdict["code"], "BUY", cb["reason"], verdict.get("name") or "")
            lines.append("- ⛔ **买不进** %s(%s)：%.2f%%｜%s"
                         % (verdict["name"], verdict["code"],
                            verdict["open_gap"] or 0, cb["reason"]))
            _log("买入被拒 %s：%s" % (verdict["code"], cb["reason"]))
            continue
        amount = int((gate.cfg["max_trade_amount"]) * sf["weight"])
        if total:
            amount = int(min(amount, total * gate.cfg["max_position_pct"]))
        chk = gate.check(verdict, total)
        if not chk["ok"]:
            gate.record(verdict, "REJECT", 0, chk["reason"])
            if hasattr(broker, "record_reject"):
                broker.record_reject(verdict["code"], "BUY", "风控：%s" % chk["reason"],
                                     verdict.get("name") or "")
            lines.append("- %s(%s) 过闸拒绝：%s" % (verdict["name"], verdict["code"], chk["reason"]))
            continue
        # 实时价成交（用户纪律2）：预判后价格已升高也只能以当前实时价买入，
        # 绝不能用昨日收盘价/预设数值无脑成交
        price = q.get("price") or verdict.get("close") or 0
        if not price or price <= 0:
            gate.record(verdict, "REJECT", 0, "无有效实时价")
            continue
        r = broker.buy_limit(verdict["code"], price, amount, sig=dict(
            verdict, reason="%s｜实时价%.2f成交" % (sf["reason"], price)))
        ok = "✓" if r.get("ok") else "✗ %s" % r.get("reason")
        gate.record(verdict, "BUY", amount, ok)
        lines.append("- **BUY %s**(%s) %s 高开 %.2f%% 实时价%.2f %d 元｜%s"
                     % (verdict["name"], verdict["code"], sf["grade"],
                        verdict["open_gap"], price, amount, sf["reason"]))
        if r.get("ok"):
            n_buy += 1
            _log("买入 %s %s @实时价%.2f：%s" % (verdict["code"], sf["grade"], price, sf["reason"]))
        else:
            if hasattr(broker, "record_reject"):
                broker.record_reject(verdict["code"], "BUY",
                                     "委托失败：%s" % r.get("reason"), verdict.get("name") or "")
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


def run_review(cfg=None, push=True):
    """当日复盘总结（收盘后 15:30 左右跑）：
    1. 汇总当日成交/平仓盈亏/被拒记录/总资产
    2. 写 tools/executor/sim_review.json（build.py 读它生成网站「模拟盘」模块）
    3. PushPlus 推送「模拟盘操作+当日复盘」
    """
    cfg = cfg or load_cfg()
    if mode_check_no_sim(cfg):
        return
    b = broker_sim.SimBroker()
    ds = b.day_summary()
    bal = ds["balance"]
    init = broker_sim._initial_cash()
    total_pct = (bal["total"] / init - 1) * 100 if init else 0

    # ---- 组装推送文本 ----
    lines = ["**总资产 %.0f 元（初始 %.0f，累计 %+.2f%%）**" % (bal["total"], init, total_pct),
             "- 当日已实现盈亏：%+.2f%%" % ds["day_realized_pct"],
             "- 可用现金 %.0f / 持仓市值 %.0f" % (bal["cash"], bal["market_value"]), ""]
    if ds["trades"]:
        lines.append("## 当日操作（%d 笔）" % len(ds["trades"]))
        for t in ds["trades"]:
            act = "买入" if t["action"] == "BUY" else "卖出"
            lines.append("- **%s %s**(%s) %.2f × %d股 = %.0f元｜%s"
                         % (act, t["name"], t["code"], t["price"], t["volume"],
                            t["amount"], (t["reason"] or "")[:60]))
    else:
        lines.append("## 当日无操作")
    if ds["closed"]:
        lines.append("")
        lines.append("## 当日平仓")
        for c in ds["closed"]:
            lines.append("- %s(%s) %+.2f%%｜%s" % (c["name"], c["code"],
                                                   c["pnl_pct"], c["sell_reason"]))
    if ds["rejects"]:
        lines.append("")
        lines.append("## 被拒留痕（%d 条）" % len(ds["rejects"]))
        for rj in ds["rejects"]:
            lines.append("- ⛔ %s %s(%s)：%s" % (rj["action"], rj["name"], rj["code"], rj["reason"]))
    # 持仓中
    holding = b.positions(open_only=True)
    if holding:
        lines.append("")
        lines.append("## 持仓中（%d 笔）" % len(holding))
        for p in holding:
            lines.append("- %s(%s) 成本%.2f %d股 %s日买入 st=%d"
                         % (p["name"], p["code"], p["avg_price"], p["volume"],
                            p["buy_date"], p["streak"]))
    verdict = "今日盈利 ✅" if ds["day_realized_pct"] > 0 else (
        "今日亏损 ❌（后续按归因改进策略）" if ds["day_realized_pct"] < 0 else "今日持平")
    text = "\n".join(lines)
    _log(text)
    if push:
        _notify(cfg, "📊 模拟盘复盘 %s（%+.2f%%）%s" % (ds["date"], ds["day_realized_pct"], verdict),
                text)

    # ---- 写 sim_review.json（网站模块数据源；历史按日累积）----
    try:
        hist = {}
        if os.path.exists(REVIEW_PATH):
            try:
                hist = json.load(open(REVIEW_PATH, encoding="utf-8"))
            except Exception:
                hist = {}
        hist["days"] = hist.get("days") or {}
        hist["days"][ds["date"]] = {
            "date": ds["date"],
            "total": bal["total"], "cash": bal["cash"], "market_value": bal["market_value"],
            "total_pct": round(total_pct, 2),
            "day_realized_pct": ds["day_realized_pct"],
            "trades": ds["trades"], "closed": ds["closed"], "rejects": ds["rejects"],
            "n_holding": len(holding),
            "summary_line": (b.summary() if hasattr(b, "summary") else ""),
        }
        # 只保留最近 120 个交易日
        keys = sorted(hist["days"].keys())
        for k in keys[:-120]:
            del hist["days"][k]
        hist["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(REVIEW_PATH, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)
        _log("复盘已写入 %s（累计 %d 个交易日）" % (os.path.basename(REVIEW_PATH), len(hist["days"])))
        # 上云：推到仓库 state/sim_review.json，CI build 时读它生成网站「模拟盘」模块
        _push_review_to_repo()
    except Exception as e:
        _log("sim_review.json 写入失败：%r" % e)
    return ds


def _push_review_to_repo():
    """把 sim_review.json 推到仓库 state/（best-effort，失败只记日志）。"""
    try:
        tools_dir = os.path.join(ROOT, "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import gh_api
        # 只推最近 60 个交易日，控制 blob 体积
        hist = json.load(open(REVIEW_PATH, encoding="utf-8"))
        days = hist.get("days") or {}
        ks = sorted(days.keys())
        for k in ks[:-60]:
            del days[k]
        st, body = gh_api.push_files(
            "sim-review: 模拟盘每日复盘数据（executor 自动回传）", ["state/sim_review.json"])
        # push_files 内部直接 commit；无需检查返回（失败抛异常）
        _log("sim_review.json 已推送到仓库 state/")
    except SystemExit as e:
        _log("sim_review 推送失败（SystemExit）：%s" % e)
    except Exception as e:
        _log("sim_review 推送失败（不影响复盘）：%r" % e)


def mode_check_no_sim(cfg):
    """qmt 实盘模式下不写复盘文件（避免覆盖模拟盘数据）。"""
    return (cfg.get("broker") or "sim") != "sim"


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
    elif "--review" in args:
        run_review(cfg)
    elif "--now" in args:
        run_once(cfg)
    elif "--loop" in args:
        target = ((cfg.get("schedule") or {}).get("auction_time") or "09:26")
        rtarget = ((cfg.get("review") or {}).get("time") or "15:35")
        _log("常驻模式：每天 %s 执行（平仓+开仓），%s 复盘总结（Ctrl+C 退出）" % (target, rtarget))
        fired = set()
        while True:
            now = time.strftime("%H:%M")
            if now == target and "trade" not in fired:
                fired.add("trade")
                try:
                    run_once(cfg)
                except Exception as e:
                    _log("执行异常：%r" % e)
            if now == rtarget and "review" not in fired and not mode_check_no_sim(cfg):
                fired.add("review")
                try:
                    run_review(cfg)
                except Exception as e:
                    _log("复盘异常：%r" % e)
            if now < target:  # 跨天重置
                fired.clear()
            time.sleep(5)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
