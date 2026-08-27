# -*- coding: utf-8 -*-
"""负反馈闭环（recveto）专项单测：覆盖否决器/校准/竞价纪律/历史统计。

运行：python tools/test_reco_veto.py
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

from pipeline import recveto  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))


# ---------- V1 标注式判定（2026-08-27 用户指令：高位标注即可，不一刀切）----------
it_high_vol = {"code": "600000", "p_break": 85, "day_vol_ratio": 1.4, "yizi": False}
r = recveto.veto(it_high_vol)
check("高位+放量 → WARN(不拦)", recveto.is_warn(r) and "33" in str(r), repr(r))

it_extreme = {"code": "600010", "p_break": 93, "day_vol_ratio": 1.4, "yizi": False}
r2 = recveto.veto(it_extreme)
check("极端断板率(>=90)+放量 → VETO(拦)", recveto.is_veto(r2), repr(r2))

it_high_shrink = {"code": "600001", "p_break": 95, "day_vol_ratio": 0.5, "yizi": False}
check("缩量即使极端断板率 → 放行", recveto.veto(it_high_shrink) is None)

it_high_yizi = {"code": "600002", "p_break": 90, "day_vol_ratio": 1.8, "yizi": True}
check("一字板惜售 → 放行", recveto.veto(it_high_yizi) is None)

it_low_pb = {"code": "600003", "p_break": 70, "day_vol_ratio": 1.4, "yizi": False}
check("非高位 → 不否决", recveto.veto(it_low_pb) is None)

it_borderline = {"code": "600004", "p_break": 82, "day_vol_ratio": None, "yizi": False}
check("量能缺失按非缩量保守处理 → 至少WARN", recveto.veto(it_borderline) is not None)

# 边界：ratio=0.7 恰好不缩量 → WARN；0.69 缩量放行
check("ratio=0.7 非缩量边界 → WARN",
      recveto.is_warn(recveto.veto({"p_break": 82, "day_vol_ratio": 0.7})))
check("ratio=0.69 缩量 → 放行",
      recveto.veto({"p_break": 82, "day_vol_ratio": 0.69}) is None)

# ---------- day_vol_ratio 特征 ----------
bars = [{"v": 100}, {"v": 100}, {"v": 100}, {"v": 100}, {"v": 200}]
today = {"v": 120}
vr = recveto.day_vol_ratio(today, bars)
check("量比计算 = 120/120=1.0", vr == 1.0, repr(vr))
check("空历史 → None", recveto.day_vol_ratio(today, []) is None)
check("今日量缺失 → None", recveto.day_vol_ratio({}, bars) is None)
bad = [{"v": None}, {"v": 0}, {"v": 0}]
check("全零历史防除零 → None", recveto.day_vol_ratio(today, bad) is None)

# ---------- apply_veto 批处理（标注式：WARN 留在 kept 带 risk_flag，VETO 才入 vetoed）----------
items = [dict(it_high_vol), dict(it_extreme), dict(it_high_shrink), dict(it_low_pb)]
kept, vetoed = recveto.apply_veto(items)
check("批处理 kept=3 / vetoed=1（仅极端）", len(kept) == 3 and len(vetoed) == 1,
      "%d/%d" % (len(kept), len(vetoed)))
check("被拦者带 veto_reason", bool(vetoed[0].get("veto_reason")))
warned = [x for x in kept if x.get("risk_flag")]
check("WARN 者留在 kept 且带 risk_flag", len(warned) == 1 and warned[0]["code"] == "600000",
      repr([x.get("code") for x in kept]))

# ---------- V2 打分校准 ----------
s82 = recveto.calibrate_score(80.0, 85)   # 高位压分
s60 = recveto.calibrate_score(80.0, 65)   # 低分位抬升
smid = recveto.calibrate_score(80.0, 75)  # 中间不动
check("高位(p_break>=82)打分下折 12%", abs(s82 - 70.4) < 0.01, repr(s82))
check("低分位(<71)打分上浮 8%", abs(s60 - 86.4) < 0.01, repr(s60))
check("中间分位不动", smid == 80.0, repr(smid))
check("上浮封顶100", recveto.calibrate_score(95.0, 65) <= 100)

# ---------- G1 竞价纪律 ----------
g_low = recveto.auction_gate(-1.5, {"tag": "核心龙头"})
check("低开一律 avoid（即使龙头）", g_low["action"] == "avoid", repr(g_low))
g_open_none = recveto.auction_gate(None, {})
check("无竞价数据保守 avoid", g_open_none["action"] == "avoid")
g_leader = recveto.auction_gate(2.3, {"tag": "核心龙头",
                                      "vol_anomaly": {"note": ""}})
check("不低开+龙头 → buy", g_leader["action"] == "buy", repr(g_leader))
g_shrunk = recveto.auction_gate(0.5, {"tag": "主线接力",
                                      "vol_anomaly": {"note": "缩量高开非一字，资金观望"}})
check("不低开+缩量 → buy", g_shrunk["action"] == "buy", repr(g_shrunk))
g_normal = recveto.auction_gate(1.0, {"tag": "主线接力",
                                      "vol_anomaly": {"note": ""}})
check("不低开普通续强 → watch", g_normal["action"] == "watch")

# ---------- 历史统计（内存库构造 rec_picks）----------
con = sqlite3.connect(":memory:")
con.execute("CREATE TABLE rec_picks (date TEXT, code TEXT, name TEXT, streak INT,"
            " p_break REAL, tag TEXT, next_continue INT DEFAULT -1,"
            " next_pct REAL DEFAULT NULL)")
rows = []
# 已回填 25 笔（有 next_pct）：20 红(+3%) 5 绿(-2%) → 收红率 80%
for i in range(20):
    rows.append(("2026-08-01", "60000%d" % (i % 10), "票%d" % i, 2, 75, "主线接力", 1, 3.0))
for i in range(5):
    rows.append(("2026-08-02", "60001%d" % i, "跌%d" % i, 1, 88, "高位风险", 0, -2.0))
# 未回填 10 笔（next_pct=NULL）必须被过滤
for i in range(10):
    rows.append(("2026-08-03", "60002%d" % i, "未%d" % i, 1, 70, "主线接力", -1, None))
con.executemany("INSERT INTO rec_picks VALUES (?,?,?,?,?,?,?,?)", rows)
st = recveto.historical_stats(con)
check("过滤未回填后 n_total=25", st["n_total"] == 25, repr(st))
check("T+1收红率 80%（next_pct>0 口径）", st["total"] == 80.0, repr(st))
check("近20条收红率 75%", st["recent"] == 75.0, repr(st))
check("平均次日涨幅 +2.0%（20×3% - 5×2%）/25", st.get("avg_next_pct") == 2.0, repr(st))
hint = recveto.quality_hint(con)
check("quality_hint 含总量与近期", "25" in hint and "近" in hint, hint)
empty_st = recveto.historical_stats(sqlite3.connect(":memory:"))
check("空表安全返回 None 统计", empty_st["total"] is None and empty_st["n_total"] == 0)

print("\n结果: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
