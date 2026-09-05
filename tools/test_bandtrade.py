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
             "tops", "range_low", "range_high", "upside",
             "buy_zone", "sell_zone", "stop", "worth"}
for i, c in enumerate(cands):
    miss = _required - set(c.keys())
    check("[%d]%s 字段齐全" % (i, c.get("name")), not miss, "缺%s" % miss)
    check("[%d] 触底次数≥2" % i, c.get("touches", 0) >= 2, "touches=%s" % c.get("touches"))
    check("[%d] 箱顶触及≥2（上沿有效）" % i, c.get("tops", 0) >= 2, "tops=%s" % c.get("tops"))
    # 2026-09-05 #488 箱体法：现价须在低吸区（箱底×1.12 以内）且未破位（箱底×0.95 以上）
    _lo = c.get("range_low") or c.get("bottom") or 0
    _px = c.get("close") or 0
    check("[%d] 现价在低吸区且未破位" % i,
          _lo * 0.95 <= _px <= _lo * 1.12,
          "box_low=%s close=%s" % (_lo, _px))
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

# ════════════════════════════════════════════════════════════════════
# 2026-09-05 #486 市场准入：用户只能交易沪深主板 + 创业板
#   （科创板 688/689、北交所 43/83/87/88/920 未达开通门槛 → 一律不推）
# ════════════════════════════════════════════════════════════════════
import mktfilter as _mktf

_cases = [("600500", "沪深主板", True), ("601398", "沪深主板", True),
          ("000001", "沪深主板", True), ("002631", "沪深主板", True),
          ("300750", "创业板", True), ("301001", "创业板", True),
          ("688981", "科创板", False), ("689009", "科创板", False),
          ("830799", "北交所", False), ("430047", "北交所", False),
          ("873169", "北交所", False), ("920002", "北交所", False),
          ("900001", "其它", False), ("200011", "其它", False)]
for _code, _exp_m, _exp_t in _cases:
    check("市场准入 %s→%s" % (_code, _exp_m),
          _mktf.market_of(_code) == _exp_m and _mktf.tradable(_code) == _exp_t,
          "got=%s/tradable=%s" % (_mktf.market_of(_code), _mktf.tradable(_code)))

_bad = [c["code"] for c in cands if not _mktf.tradable(c["code"])]
check("波段候选无科创板/北交所", not _bad, str(_bad))

_rec = {"core": [{"code": "600500"}, {"code": "688981"}, {"code": "300750"}],
        "note": [{"name": "无code字段"}]}
_cut = _mktf.filter_rec(_rec)
check("filter_rec 剔除科创板", _cut == 1 and len(_rec["core"]) == 2,
      "cut=%s core=%s" % (_cut, _rec["core"]))
check("filter_rec 不动无code列表", len(_rec["note"]) == 1)

# ════════════════════════════════════════════════════════════════════
# 2026-09-05 #488 波段口径：阶段底 → 区间高点（用户要「底 X → 高 Y」可照做）
#   立新能源 12.22→13.8(+13%) / 利通电子 94→120(+28%)
# ════════════════════════════════════════════════════════════════════
for i, c in enumerate(cands):
    check("[%d] 含阶段底/区间高点/空间%%" % i,
          c.get("range_low") and c.get("range_high") and c.get("upside") is not None,
          str({k: c.get(k) for k in ("range_low", "range_high", "upside")}))
    check("[%d] 区间高点>阶段底" % i,
          (c.get("range_high") or 0) > (c.get("range_low") or 0),
          "low=%s high=%s" % (c.get("range_low"), c.get("range_high")))
    # 空间下限 8%（含等于）：空间太小扣掉手续费不值得做
    check("[%d] 空间≥8%%" % i, (c.get("upside") or 0) >= 8.0,
          "upside=%s" % c.get("upside"))
    # 区间高点须是「阶段底之后、最近 60 日」的高点，不能来自数月前的旧行情
    _bars = u.bars_upto(c["code"], date, 60)
    _hi60 = max((b["c"] for b in _bars), default=0)
    check("[%d] 区间高点不脱离近60日行情" % i,
          (c.get("range_high") or 0) <= max(_hi60, c.get("close") or 0) * 1.01,
          "high=%s hi60=%s" % (c.get("range_high"), _hi60))

print("\nPASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
