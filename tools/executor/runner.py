# -*- coding: utf-8 -*-
"""stock-analysis 本地执行器主入口（runner）。

职责（单一流程，每天 09:26 竞价结束后跑一次，也可 --now 手动触发）：
  1. 拉线上加密数据 -> 解密 -> 提取带决策线的信号
  2. 拉腾讯实时行情 -> 决策线裁决（高开≥2%买 / 低开≤-2%弃 / 平开观望）
  3. 过风控闸门 -> broker 下单（sim 模拟 / qmt 实盘）
  4. 记录+可选 ServerChan 推送回报

用法：
  python tools/executor/runner.py --now        # 立即执行一轮（测试/手动）
  python tools/executor/runner.py --loop       # 常驻模式，每天 09:26 自动执行
  python tools/executor/runner.py --summary    # 查看模拟盘战绩
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exec_core import fetch_user_data, extract_signals, realtime_quote, auction_gate, SITE
from risk_gate import RiskGate

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
    import broker_sim
    return broker_sim.SimBroker(), mode


def run_once(cfg):
    acc = cfg.get("account") or {}
    if not acc.get("user_id") or not acc.get("passwd"):
        _log("未配置 account，退出")
        return
    _log("拉取线上数据 %s ..." % SITE)
    try:
        data = fetch_user_data(acc["user_id"], acc["passwd"])
    except Exception as e:
        _log("✗ 数据拉取/解密失败：%r" % e)
        _notify(cfg, "执行器数据失败", "拉取/解密失败：%r" % e)
        return
    sigs = extract_signals(data)
    _log("信号 %d 条（core+relay+fused 去重）" % len(sigs))
    if not sigs:
        return

    codes = [s["code"] for s in sigs]
    _log("拉实时行情 ...")
    try:
        quote = realtime_quote(codes)
    except Exception as e:
        _log("✗ 行情失败：%r" % e)
        return
    if not quote:
        _log("✗ 行情为空（可能非交易时段）")
        return

    broker, mode = pick_broker(cfg)
    gate = RiskGate((cfg.get("risk") or {}))
    _log("broker=%s | 熔断状态=%s" % (mode, "YES" if gate.tripped else "no"))

    bal = broker.balance() if hasattr(broker, "balance") else {}
    total = bal.get("total")
    lines = []
    n_buy = 0
    for s in sigs:
        verdict = auction_gate(s, quote)
        if verdict["verdict"] == "BUY":
            chk = gate.check(verdict, total)
            if chk["ok"]:
                r = broker.buy_limit(verdict["code"], verdict.get("close") or 0,
                                     chk["amount"], sig=verdict)
                ok = "✓" if r.get("ok") else "✗ %s" % r.get("reason")
                gate.record(verdict, "BUY", chk["amount"], ok)
                lines.append("- **%s**(%s) 高开 %.2f%% 买入 %d 元 %s"
                             % (verdict["name"], verdict["code"], verdict["open_gap"],
                                chk["amount"], ok))
                if r.get("ok"):
                    n_buy += 1
            else:
                gate.record(verdict, "REJECT", 0, chk["reason"])
                lines.append("- %s(%s) 过闸拒绝：%s" % (verdict["name"], verdict["code"], chk["reason"]))
        else:
            gate.record(verdict, verdict["verdict"], 0, verdict["reason"])
            lines.append("- %s(%s) %s：%.2f%% %s"
                         % (verdict["name"], verdict["code"], verdict["verdict"],
                            verdict["open_gap"] or 0, verdict["reason"]))

    _log("=" * 50)
    for ln in lines:
        _log(ln)
    if mode == "sim":
        import broker_sim
        _log(broker_sim.SimBroker().summary())
    _notify(cfg, "执行器回报（%d 买）" % n_buy,
            "\n".join(lines) or "无信号")


def run_summary():
    import broker_sim
    print(broker_sim.SimBroker().summary())
    con = broker_sim._con()
    for r in con.execute("SELECT date,code,name,open_gap,buy_price,sell_date,sell_price,pnl_pct "
                         "FROM sim_positions ORDER BY date DESC LIMIT 20"):
        print(r)


def main():
    args = sys.argv[1:]
    cfg = load_cfg()
    if "--summary" in args:
        run_summary()
    elif "--now" in args:
        run_once(cfg)
    elif "--loop" in args:
        target = ((cfg.get("schedule") or {}).get("auction_time") or "09:26")
        _log("常驻模式：每天 %s 自动执行（Ctrl+C 退出）" % target)
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
