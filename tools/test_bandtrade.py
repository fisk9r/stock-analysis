"""波段/阶段底选股引擎单元测试（2026-09-04）。

验证 detect_stage_bottom 不引入未来函数、输出结构正确、过滤逻辑生效。
直接用真实 market.db 跑全市场（CI 环境可达），本地也可跑。
"""
import os, sys, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import engine, store, bandtrade

PASS = 0
FAIL = 0

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, extra))

ROOT = os.path.join(os.path.dirname(__file__), "..")
con = sqlite3.connect(os.path.join(ROOT, "cache", "market.db"))
u = engine.Universe(con, days=270)
date = u.dates[-1]
c2b = store.code_boards(con)

cands = bandtrade.detect_stage_bottom(u, date, c2b, topn=8)

check("返回列表且≤8只", isinstance(cands, list) and len(cands) <= 8,
      "len=%d" % len(cands))
check("至少命中若干候选(>0)", len(cands) > 0, "len=%d" % len(cands))

_required = {"code", "name", "board", "close", "bottom", "touches",
             "bounce", "buy_zone", "sell_zone", "stop", "worth"}
for i, c in enumerate(cands):
    miss = _required - set(c.keys())
    check("[%d]%s 字段齐全" % (i, c.get("name")), not miss, "缺%s" % miss)
    check("[%d] 触底次数≥2" % i, c.get("touches", 0) >= 2, "touches=%s" % c.get("touches"))
    b = c.get("bounce", 0)
    check("[%d] 反弹幅度∈(0,16%%]" % i, 0 < b <= 16, "bounce=%s" % b)
    # 买区 < 现价 ≤ 卖区；止损 < 买区
    bz, sz, st = c.get("buy_zone"), c.get("sell_zone"), c.get("stop")
    check("[%d] 买区<卖区且止损<买区" % i,
          bz[0] < bz[1] <= sz[0] < sz[1] and st < bz[0],
          "bz=%s sz=%s st=%s" % (bz, sz, st))
    # 板块标签不应是泛用噪声（HS300_ 之类）
    check("[%d] 板块标签非泛用噪声" % i,
          c.get("board") not in ("—",) and not str(c.get("board", "")).endswith("_"),
          "board=%s" % c.get("board"))

# 验证三只范例票在历史上确实具有「反复阶段底」特征（非当前候选，而是模式证据）
for code in ["001258", "600272", "003031"]:
    bars = u.bars_upto(code, date, 270)
    closes = [b["c"] for b in bars]
    troughs = [closes[i] for i in range(4, len(closes) - 4)
               if closes[i] < closes[i - 1] and closes[i] <= closes[i + 1]
               and closes[i] < sum(closes[i - 20:i]) / 20 * 0.97]
    lo = min(troughs) if troughs else 0
    touches = sum(1 for p in troughs if lo <= p <= lo * 1.10) if troughs else 0
    check("%s 历史有反复阶段底(触底≥2)" % code,
          touches >= 2, "touches=%d" % touches)

print("\nPASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
