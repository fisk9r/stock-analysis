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


# ─────────────── 升级功能（2026-08-27 第二批） ───────────────

def test_cond_buckets():
    """低开/不低开条件分桶：混入两类次日开盘，验证子桶分离。"""
    u = FakeU()
    u.dates = ["2026-01-%02d" % (i + 1) for i in range(30)]

    def _mk_bars_custom(seq, gap_pct):
        """gap_pct：最后一个涨停日的【次日】开盘相对涨停日收盘的 %（低开=-3 / 高开=+2）。
        次日延续 gap 方向：低开日继续走弱（收在开盘 -4%），高开日走强（收在开盘 +2%），
        以模拟真实的竞价方向延续性。"""
        bars = []
        prev_c = 10.0
        zt_idx = [i for i, p in enumerate(seq) if p >= 9]
        last_zt = zt_idx[-1] if zt_idx else -1
        for i, p in enumerate(seq):
            c = round(prev_c * (1 + p / 100.0), 2)
            if i == last_zt + 1 and last_zt >= 0:
                o = round(prev_c * (1 + gap_pct / 100.0), 2)   # prev_c=涨停日收盘
                c = round(o * (0.96 if gap_pct < 0 else 1.02), 2)
                p_real = round((c / prev_c - 1) * 100, 2)
            else:
                o = round(prev_c * 1.02, 2)
                p_real = p
            h = max(round(c * 1.05, 2), o)
            bars.append({"d": "2026-01-%02d" % (i + 1), "o": o, "h": h,
                         "c": c, "pct": p_real, "v": 1000})
            prev_c = c
        return bars

    # 12 只低开路径 + 12 只高开路径（各 24>MIN_N? n 子桶阈值=_MIN_N//2=10 ✓）
    for k in range(12):
        seq = [0.5] * 5 + [9.9] * 3 + [3.0] * 4
        code = "600A%02d" % k
        u.bars[code] = _mk_bars_custom(seq, -3.0)
        u.stocks[code] = {"name": "低%d" % k}
    for k in range(12):
        seq = [0.5] * 5 + [9.9] * 3 + [3.0] * 4
        code = "600B%02d" % k
        u.bars[code] = _mk_bars_custom(seq, +2.0)
        u.stocks[code] = {"name": "高%d" % k}
    st = ladderplan.ladder_stats(u, lookback=30)
    b3 = st.get(3) or {}
    cond = b3.get("cond") or {}
    gu, gd = cond.get("gap_up") or {}, cond.get("gap_down") or {}
    check("主桶含 cond 键", isinstance(cond, dict), repr(b3))
    check("不低开子桶有样本", gu.get("n", 0) >= 10, repr(gu))
    check("低开子桶有样本", gd.get("n", 0) >= 10, repr(gd))
    check("子桶样本数之和≈主桶",
          abs((gu.get("n", 0) + gd.get("n", 0)) - b3.get("n", 0)) <= 2,
          repr((gu.get("n"), gd.get("n"), b3.get("n"))))
    # 低开子桶首日尾盘收益应显著差于高开子桶
    check("低开期望<不低开期望(方向性)",
          (gd.get("exp") or 0) < (gu.get("exp") or 99),
          repr((gd.get("exp"), gu.get("exp"))))


def test_env_mod():
    coef, note = ladderplan.env_mod(sent={"score": 30}, regime={"factor": 0.1},
                                    ladder_warn={"level": "退潮"})
    check("冷市×冰点×退潮 复合打折",
          abs(coef - round(0.85 * 0.85 * 0.75, 3)) < 1e-9 and "退潮" in note,
          repr((coef, note)))
    coef2, _ = ladderplan.env_mod(sent={"score": 75}, regime={"factor": 0.5},
                                  ladder_warn={"level": "正常"})
    check("热市×过热 放大但有抑制", 0.9 < coef2 <= 1.05, repr(coef2))
    coef3, note3 = ladderplan.env_mod()
    check("空环境无修正", coef3 == 1.0 and note3 == "", repr((coef3, note3)))


def test_plan_gate_and_cond():
    st = {3: {"n": 40, "exp": 5.5, "mfe_med": 7.2,
              "reach": {5.0: 0.55, 10.0: 0.30, 15.0: 0.12, 20.0: 0.05},
              "days_med": 2,
              "cond": {
                  "gap_up": {"n": 25, "exp": 6.5, "mfe_med": 8.0,
                             "reach": {5.0: 0.6, 10.0: 0.36, 15.0: 0.14, 20.0: 0.06},
                             "days_med": 3},
                  "gap_down": {"n": 15, "exp": -2.0, "mfe_med": 1.5,
                               "reach": {5.0: 0.1, 10.0: 0.04, 15.0: 0.0, 20.0: 0.0},
                               "days_med": 1}}}}
    u = FakeU()
    # 竞价未出 → gate=None + 双分支提示
    p_none = ladderplan.plan(u, "600123", 3, 13.00, st)
    check("竞价前 gate=None", p_none["gate"] is None, repr(p_none))
    check("gate 提示含纪律文案", "放弃买入" in p_none["gate_hint"], repr(p_none))
    # 竞价后实际低开 → avoid 门 + 用低开子桶核算（exp 应明显变差）
    p_low = ladderplan.plan(u, "600123", 3, 13.00, st, open_pct=-2.5)
    check("低开→gate=avoid", p_low["gate"] == "avoid", repr(p_low))
    check("低开用子桶证据", "低开子桶" in p_low["evidence"], repr(p_low["evidence"]))
    check("低开目标低于整桶口径", p_low["sell_zone"][0] < 13.00 * 1.03, repr(p_low))
    # 不低开 → 不拦 + 用不低开子桶
    p_hi = ladderplan.plan(u, "600123", 3, 13.00, st, open_pct=+2.0)
    check("不低开 gate 保持 None", p_hi["gate"] is None, repr(p_hi))
    check("不低开用子桶证据", "不低开子桶" in p_hi["evidence"], repr(p_hi["evidence"]))
    check("不低开目标高于低开口径", p_hi["sell_zone"][0] > p_low["sell_zone"][0],
          repr((p_hi["sell_zone"], p_low["sell_zone"])))
    # 环境修正系数写进计划
    p_env = ladderplan.plan(u, "600123", 3, 13.00, st, env_coef=0.85, env_note="冷市")
    check("env_note 进 evidence", "冷市" in p_env["evidence"], repr(p_env))
    check("环境修正压低目标", p_env["sell_zone"][0] < p_none["sell_zone"][0],
          repr((p_env["sell_zone"], p_none["sell_zone"])))


def test_scan_with_env():
    """scan 注入环境三元组 → 每张计划带修正 evidence；梯队级别透传。"""
    u = FakeU()
    lus = [{"code": "600111", "name": "甲", "streak": 3, "close": 10.0,
            "p_continue": 30, "p_break": 70, "yizi": False}]
    st = {3: {"n": 40, "exp": 5.5, "mfe_med": 7.2,
              "reach": {5.0: 0.55, 10.0: 0.30, 15.0: 0.12, 20.0: 0.05},
              "days_med": 2, "cond": {"gap_up": None, "gap_down": None}}}
    plans = ladderplan.scan(u, "2026-01-01", lus, stats=st,
                            sent={"score": 35}, regime={"factor": 0.15},
                            ladder_warn={"level": "降温"})
    check("扫描 1 条", len(plans) == 1, repr(plans))
    p = plans[0]
    check("梯队标签进 evidence", "梯队:降温" in p["evidence"], repr(p["evidence"]))
    check("情绪/接力因子进 evidence",
          ("情绪" in p["evidence"]) or ("接力" in p["evidence"]), repr(p["evidence"]))
    exp_cold = p["exp_ret"]
    plans_warm = ladderplan.scan(u, "2026-01-01", lus, stats=st,
                                 sent={"score": 60}, regime=None,
                                 ladder_warn=None)
    check("暖市期望不低于冷市", plans_warm[0]["exp_ret"] >= exp_cold,
          repr((plans_warm[0]["exp_ret"], exp_cold)))


test_limit_pct()
test_ladder_stats()
test_expected_top()
test_plan()
test_scan_order_and_fields()
test_cond_buckets()
test_env_mod()
test_plan_gate_and_cond()
test_scan_with_env()

print("\n结果: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
