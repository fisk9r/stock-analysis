# -*- coding: utf-8 -*-
"""kronos_lite / factor_ext 扩展测试（2026-08-29 二轮融入）。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

from kronos_lite import kronos_features, kronos_score, annotate_bars
from factor_ext import parse_tencent_quote_fields, turnover_flag, is_stale_quote, SOURCE_NOTES

PASS = FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s" % name)

def _mk_bars(n, base=10.0, step_pct=1.5, vol=1000, vol_step=0):
    bars = []
    p = base
    for i in range(n):
        o = p
        c = p * (1 + step_pct / 100.0)
        h = max(o, c) * 1.01
        l = min(o, c) * 0.99
        v = vol + vol_step * i
        bars.append({"d": "D%03d" % i, "o": round(o, 2), "h": round(h, 2),
                     "l": round(l, 2), "c": round(c, 2), "v": v})
        p = c
    return bars

print("== kronos_lite 扩展特征 ==")
feats = kronos_features(_mk_bars(30))
check("features 含 vol_regime", "vol_regime" in feats)
check("features 含 self_sim", "self_sim" in feats)
# 稳定上涨序列：每日同涨幅 → 5日/20日波动都小且接近 → vol_regime 接近 1
check("稳定趋势 vol_regime<1.5 (%s)" % feats.get("vol_regime"), 0 < feats.get("vol_regime", 9) < 1.5)
# 全同向 → self_sim = 1.0
check("全涨序列 self_sim==1 (%s)" % feats.get("self_sim"), feats.get("self_sim") == 1.0)
s = kronos_score(feats)
check("健康趋势分数偏高 s>50 (%s)" % s, s > 50)

# 震荡序列：交替涨跌 → self_sim 低
bars = _mk_bars(30, step_pct=0)
for i, b in enumerate(bars):
    sign = 1 if i % 2 == 0 else -1
    b["c"] = round(b["o"] * (1 + sign * 0.015), 2)
f2 = kronos_features(bars)
check("震荡序列 self_sim 低 (%s)" % f2.get("self_sim"), f2.get("self_sim", 1.0) < 0.3)
s2 = kronos_score(f2)
check("震荡分数低于健康趋势 (%.1f < %.1f)" % (s2, s), s2 < s)

check("bars 不足20返回空 dict", kronos_features(_mk_bars(10)) == {})
check("annotate_bars 快捷入口", annotate_bars(_mk_bars(30)) > 0)

print("== factor_ext 扩展字段 ==")
# 构造 53+ 位 fields 数组（索引对齐 SKILL.md 实测表）
fields = ["v_sh600000"] + [""] * 60
fields[1] = "浦发银行"
fields[3] = "10.50"; fields[4] = "10.00"; fields[5] = "10.20"
fields[6] = "123456"
fields[37] = "187040"; fields[38] = "4.55"; fields[39] = "300.45"
fields[43] = "7.22"; fields[44] = "410.88"; fields[45] = "1300.61"; fields[46] = "11.51"
fields[47] = "11.00"; fields[48] = "9.00"; fields[49] = "1.20"; fields[52] = "314.76"
q = parse_tencent_quote_fields(fields)
check("limit_up==11.00", q["limit_up"] == 11.0)
check("limit_down==9.00", q["limit_down"] == 9.0)
check("vol_ratio==1.20", q["vol_ratio"] == 1.2)
check("pe_static==314.76", q["pe_static"] == 314.76)
check("float_mv/total_mv 不混淆", q["float_mv"] == 410.88 and q["total_mv"] == 1300.61)
check("短数组不抛异常", parse_tencent_quote_fields(["v_sh600000"])["limit_up"] == 0.0)

check("is_stale_quote True", is_stale_quote(10.0, 10.0, 0))
check("is_stale_quote 有量 False", is_stale_quote(10.0, 10.0, 100) is False)
check("is_stale_quote 异常值 False", is_stale_quote(None, None, None) is False)

check("SOURCE_NOTES 含二轮条目",
      all(k in SOURCE_NOTES for k in ("ths_hot", "cls_telegraph", "cyq", "limit_up_pool")))
check("tencent note 含 43=振幅 警告", "振幅" in SOURCE_NOTES["tencent"])

print("== turnover_flag（回归）==")
check("thin", turnover_flag(2.0, 4)["flag"] == "thin")
check("divergent", turnover_flag(30.0, 3)["flag"] == "divergent")
check("healthy", turnover_flag(8.0, 2)["flag"] == "healthy")

print("== kronos_lite 二轮增强（2026-09-04 分层量化 token / 形态熵 / 局部模式胜率）==")
from kronos_lite import _ret_bucket, _tokenize_bar, _entropy_of_seq, _micro_edge, kronos_features
check("_ret_bucket 中位=平(4)", _ret_bucket(0.0) == 4)
check("_ret_bucket 单调(大涨档>小涨档)", _ret_bucket(0.05) > _ret_bucket(0.01))
check("_ret_bucket 限幅(0.1 封顶)", _ret_bucket(0.5) == _ret_bucket(0.1))
check("_tokenize_bar 返回(s1,s2)二元组", isinstance(_tokenize_bar(10,10.2,9.8,10.1,10.0), tuple))
# 单调上涨序列 → 低熵（结构化、可预测）
up_feats = kronos_features(_mk_bars(30))
check("新特征 pattern_entropy 存在", "pattern_entropy" in up_feats)
check("新特征 micro_edge 存在", "micro_edge" in up_feats)
check("单调上涨序列低熵(<0.5)", up_feats.get("pattern_entropy", 1) < 0.5)
# 震荡序列 → 高熵（随机、不可预测）
osc = _mk_bars(30, step_pct=0)
for i, b in enumerate(osc):
    b["c"] = round(b["o"] * (1 + (1 if i % 2 == 0 else -1) * 0.015), 2)
osc_feats = kronos_features(osc)
check("震荡序列高熵(>0.7)", osc_feats.get("pattern_entropy", 0) > 0.7)
# 局部模式方向胜率：重复 [1,2,3,4] 窗口后均上涨 → edge>0
seq = [1, 2, 3, 4, 1, 2, 3, 4, 1, 2, 3, 4, 1, 2, 3, 4]
cl = [10, 11, 12, 13, 14, 11, 12, 13, 14, 11, 12, 13, 14, 11, 12, 14]
e, n = _micro_edge(seq, cl, L=4, min_support=2)
check("_micro_edge 找到重复窗口且判涨(edge>0,n>=2)", e is not None and e > 0 and n >= 2)
# bars<30 返回空（二轮增强需要足够历史做形态熵与模式匹配）
check("bars<30 返回空 dict", kronos_features(_mk_bars(20)) == {})
# 单调序列得分应高于震荡序列（结构化走势更被青睐）
check("annotate 单调序列得分>震荡序列",
      annotate_bars(_mk_bars(30)) > annotate_bars(osc))

print("\nPASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
