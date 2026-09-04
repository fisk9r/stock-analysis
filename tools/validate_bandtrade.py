"""验证 bandtrade.detect_stage_bottom：用真实 market.db 跑全市场，看候选质量与数量。"""
import os, sys, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import engine, store, bandtrade

ROOT = os.path.join(os.path.dirname(__file__), "..")
con = sqlite3.connect(os.path.join(ROOT, "cache", "market.db"))
u = engine.Universe(con, days=270)
date = u.dates[-1]
print("分析日:", date, "| 股票数:", len(u.stocks))

c2b = store.code_boards(con)
cands = bandtrade.detect_stage_bottom(u, date, c2b, topn=20)
print("波段/阶段底候选数(top20):", len(cands))
print()
for c in cands:
    print("  %s %s【%s】 现价%.2f | 阶段底%.2f 触底%d次 反弹+%.1f%% | 买区%.2f~%.2f 卖区%.2f~%.2f 止损%.2f"
          % (c["code"], c["name"], c["board"], c["close"], c["bottom"], c["touches"],
             c["bounce"], c["buy_zone"][0], c["buy_zone"][1], c["sell_zone"][0], c["sell_zone"][1], c["stop"]))

# 验证三只范例票在历史上是否会被识别（回看各窗口）
print()
print("=== 范例票历史阶段底证据 ===")
for code in ["001258", "600272", "003031"]:
    bars = u.bars_upto(code, date, 270)
    if len(bars) < 90:
        print(code, "数据不足"); continue
    closes = [b["c"] for b in bars]
    # 简单重算 touches
    troughs = []
    for i in range(4, len(closes)-4):
        if closes[i] < closes[i-1] and closes[i] <= closes[i+1] and closes[i] < sum(closes[i-20:i])/20*0.97:
            troughs.append(closes[i])
    if troughs:
        lo = min(troughs); bh = lo*1.10
        touches = sum(1 for p in troughs if lo <= p <= bh)
        print("  %s %s: 近270日最低低点%.2f, 底部带触底%d次, 当前价%.2f(距底%.0f%%)"
              % (code, u.stocks[code]["name"], lo, touches, closes[-1], (closes[-1]/lo-1)*100))
    else:
        print("  %s %s: 近270日无显著低点" % (code, u.stocks[code]["name"]))
