# -*- coding: utf-8 -*-
"""连板预期空间引擎（ladderplan）专项单测。

运行：python tools/test_ladderplan.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

from pipeline import ladderplan  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))


class FakeU:
    """最小 Universe 桩：dates + bars + stocks。"""

    def __init__(self):
        self.dates = []
        self.bars = {}
        self.stocks = {}


def _mk_bars(seq):
    """seq: list of pct（涨停日=10）；生成 d/o/h/c 序列。
    涨停日 open 简化 = 前收*1.02；次日开盘给前收*1.0；high = c*(1+mfe_cap)。"""
    bars = []
    prev_c = 10.0
    for i, p in enumerate(seq):
        c = round(prev_c * (1 + p / 100.0), 2)
        o = round(prev_c * 1.02, 2)
        h = round(c * 1.05, 2)
        bars.append({"d": "2026-01-%02d" % (i + 1), "o": o, "h": h,
                     "c": c, "pct": p, "v": 1000})
        prev_c = c
    return bars


def test_limit_pct():
    check("主板涨停 10%", ladderplan._limit_pct("600000", "浦发银行") == 10.0)
    check("创业板 20%", ladderplan._limit_pct("300001", "某某") == 20.0)
    check("科创板 20%", ladderplan._limit_pct("688001", "某某") == 20.0)
    check("ST 5%", ladderplan._limit_pct("600001", "*ST四通") == 5.0)


def test_ladder_stats():
    u = FakeU()
    u.dates = ["2026-01-%02d" % (i + 1) for i in range(30)]
    # 构造 20 只票：每只「3连板 → 次日仍涨」的简单路径
    for k in range(20):
        seq = [0.5] * 5 + [9.9] * 3 + [3.0] * 4   # 中段三连板
        code = "6001%02d" % k
        u.bars[code] = _mk_bars(seq)
        u.stocks[code] = {"name": "票%d" % k}
    st = ladderplan.ladder_stats(u, lookback=30)
    b3 = st.get(3) or {}
    check("三连板桶样本 n>=20", b3.get("n", 0) >= 20, repr(b3))
    check("期望收益>0", (b3.get("exp") or 0) > 0, repr(b3))
    r5 = (b3.get("reach") or {}).get(5.0)
    check("reach5 概率在(0,1]", r5 is not None and 0 < r5 <= 1.0, repr(b3))


def test_expected_top():
    st = {3: {"n": 40, "exp": 5.5, "mfe_med": 7.2,
              "reach": {5.0: 0.55, 10.0: 0.30, 15.0: 0.12, 20.0: 0.05},
              "days_med": 2}}
    et = ladderplan.expected_top(3, st)
    check("预期再走 ≥0 板", et["more_lo"] >= 0, repr(et))
    check("hold_days>=1", et["hold_days"] >= 1, repr(et))
    et_empty = ladderplan.expected_top(6, {})   # 无样本→先验
    check("无样本回退先验", et_empty["more_lo"] >= 0 and et_empty["hold_days"] >= 1,
          repr(et_empty))


def test_plan():
    st = {3: {"n": 40, "exp": 5.5, "mfe_med": 7.2,
              "reach": {5.0: 0.55, 10.0: 0.30, 15.0: 0.12, 20.0: 0.05},
              "days_med": 2}}
    u = FakeU()
    p = ladderplan.plan(u, "600123", 3, 13.00, st)
    check("计划含 expected_top 字符串", p["expected_top"].startswith("3→"), repr(p))
    bz, sz, sp = p["buy_zone"], p["sell_zone"], p["stop"]
    check("买单区间 left<right", bz[0] < bz[1], repr(p))
    check("买区围绕涨停价±3%", abs(bz[0] - 13.00 * 0.995) < 0.01 and abs(bz[1] - 13.00 * 1.03) < 0.01, repr(p))
    check("止损=-8%", abs(sp - 13.00 * 0.92) < 0.01, repr(p))
    check("目标价高于现价", sz[0] > 13.00 and sz[1] >= sz[0], repr(p))
    check("样本证据带 n", p["sample_n"] == 40, repr(p))
    check("盈亏比>0", p["rr"] > 0, repr(p))
    check("到10%%率百分位 0~100", 0 <= p["reach10"] <= 100, repr(p))


def test_scan_order_and_fields():
    u = FakeU()
    lus = [
        {"code": "600111", "name": "甲", "streak": 3, "close": 10.0,
         "industry": "电", "p_continue": 30, "p_break": 70, "yizi": False},
        {"code": "600222", "name": "乙", "streak": 2, "close": 20.0,
         "industry": "药", "p_continue": 35, "p_break": 60, "yizi": True},
    ]
    plans = ladderplan.scan(u, "2026-01-01", lus, stats={}, topn=5)
    check("扫描产出 2 条计划", len(plans) == 2, repr(plans))
    ok = all(("expected_top" in p and "buy_zone" in p and "stop" in p) for p in plans)
    check("计划字段齐全", ok)


test_limit_pct()
test_ladder_stats()
test_expected_top()
test_plan()
test_scan_order_and_fields()

print("\n结果: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
