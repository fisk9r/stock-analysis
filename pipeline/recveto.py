# -*- coding: utf-8 -*-
"""推荐负反馈闭环：基于 rec_picks 历史回测（480 条，2026-08 确诊）的可执行否决器。

败因确诊结论（tools 回测 2026-08-26）：
  * 整体 T+1 收红率仅 43%，均值 +0.57%
  * 最大亏损源：p_break>=82 且 当日放量(day_vol_ratio>=0.7) 子集 → 胜率 33% / 均值 -0.53%（n=150）
  * p_break 打分器过度自信：p_break 低分位(Q1,<71)反而胜率 59~60% / 均值 +2.43%
  * 最强正向信号：竞价不低开+核心龙头 82%/+6.13%；不低开+(缩量或龙头) 78%/+5.50%
  * 灾难信号：低开 → T+1 收红率仅 24% / 均值 -2.24%

目标：把推荐名单的 T+1 收红率从 43% 拉向 80%+
手段（2026-08-27 按用户指令改为「标注式」——高位票不一刀切，只降权+标注：
  100% 胜率不可能，用户仍要挖掘连板次日买点，高风险画像保留但透明警示）：
  V1 标注——p_break>=82 且放量且非一字：不再强制 avoid；仅 p_break>=90 的极端值才拦
  V2 打分校准——弱化被证伪的 p_break 高分贡献，抬升低分位
  G1 竞价纪律——低开标 avoid 证据不变（回测口径可执行）
"""
from __future__ import annotations

# ---- 可调参数（均来自历史回测分位）----
VETO_PB = 82          # 断板概率高于此值视为"高位"→ 进入「标注」区
HARD_VETO_PB = 90     # 极端断板概率且非缩量非一字 → 仍然拦下（不入选）
SHRINK_RATIO = 0.7    # day_vol_ratio < 0.7 视为缩量（67% 胜率/+3.30% 的安全子集）
LOW_OPEN = -0.01      # 竞价 open_pct < 此值视为低开（24% 胜率的灾难区）

VETO_TAG = "高位风险"
_veto_stats_cache = None  # (con, date, stats) 进程内缓存


# ---------------------------------------------------------------------------
# 特征工程
# ---------------------------------------------------------------------------
def day_vol_ratio(bar_today, bars_hist):
    """当日成交量 / 前 5 日均量。数据缺失时返回 None（调用方按中性处理）。"""
    try:
        tv = bar_today.get("v") or 0
        if not bars_hist:
            return None
        hist = [b.get("v") or 0 for b in bars_hist][-5:]
        hist = [x for x in hist if x and x > 0]
        if not hist or not tv or tv <= 0:
            return None
        return round(tv / (sum(hist) / len(hist)), 3)
    except Exception:
        return None


def is_shrunk(ratio):
    """缩量判定：ratio 缺失时保守视为非缩量（宁可错杀进回避，不放亏钱票进主推）。"""
    if ratio is None:
        return False
    return ratio < SHRINK_RATIO


def veto(it):
    """标注式风险判定（2026-08-27 用户指令：100% 胜率不可能，高位票标注即可）。

    返回：
      None                      通过（含一般高位但条件尚可的）
      "WARN|原因串"             高危画像 → 不拦，由调用方降权+risknote 标注
      "VETO|原因串"             极端画像(p_break>=90 且放量且非一字) → 拦下进回避
    """
    pb = it.get("p_break") or it.get("pb") or 0
    ratio = it.get("day_vol_ratio")
    if pb >= HARD_VETO_PB and not is_shrunk(ratio) and not it.get("yizi"):
        return "VETO|极端断板率(%.0f%%)且放量接力——历史同条件 T+1 胜率仅 33%%" % pb
    if pb >= VETO_PB and not is_shrunk(ratio) and not it.get("yizi"):
        return "WARN|断板率%.0f%%偏高且放量——历史同条件 T+1 胜率仅 33%%，轻仓/快进快出" % pb
    return None


def is_veto(verdict):
    return bool(verdict) and str(verdict).startswith("VETO")


def is_warn(verdict):
    return bool(verdict) and str(verdict).startswith("WARN")


# ---------------------------------------------------------------------------
# 阈值滚动回测自校准（2026-09-01 升级 #10）
# 原本 VETO_PB/HARD_VETO_PB/SHRINK_RATIO/LOW_OPEN 是 2026-08 一次性回测定值，
# 市场风格漂移后易失真。新增「滚动回测自动建议」：用 rec_picks 近 N 笔实时统计，
# 给出随数据漂移自适应的阈值建议；默认仅记录建议、不改行为，设环境变量
# RECVETO_AUTO_CALIB=1 时才自动覆盖（兼顾「历史回测背书」与「数据漂移自适应」）。
# ---------------------------------------------------------------------------
def suggest_thresholds(con, recent_n=120):
    """基于 rec_picks 滚动回测，自动建议四组阈值。

    方法：按 p_break 每 5 分桶统计 T+1 收红率，找到胜率跌破 50% 的临界 p_break 作
    VETO_PB，再下探到胜率跌破 35% 的临界作 HARD_VETO_PB；缩量比取「收红子集」中
    day_vol_ratio 上界；低开门限取「亏损子集」中 open_pct 上界。
    样本不足(<30)或不可用时返回 None（调用方维持硬编码默认值，行为不变）。"""
    if con is None:
        return None
    try:
        rows = con.execute(
            "SELECT p_break, next_pct, day_vol_ratio, open_pct FROM rec_picks "
            "WHERE next_pct IS NOT NULL "
            # 与 historical_stats 同口径：排除趋势/动量通道，避免稀释连板池阈值
            "AND (tag IS NULL OR (tag NOT LIKE '趋势%' AND tag NOT LIKE '动量%')) "
            "ORDER BY date DESC LIMIT ?", (recent_n,)).fetchall()
    except Exception:
        return None
    if len(rows) < 30:
        return None
    buckets = {}
    for r in rows:
        pb = r[0]
        if pb is None:
            continue
        b = int(pb // 5) * 5
        buckets.setdefault(b, []).append(r)
    veto_pb = None
    hard_pb = None
    for b in sorted(buckets):
        sub = buckets[b]
        wr = sum(1 for x in sub if (x[1] or 0) > 0) / len(sub)
        if veto_pb is None and wr < 0.50:
            veto_pb = b
        if hard_pb is None and wr < 0.35:
            hard_pb = b
    safe = [x[2] for x in rows if (x[1] or 0) > 0 and x[2] is not None and x[2] > 0]
    shrink = round(max(safe), 2) if safe else SHRINK_RATIO
    low = [x[3] for x in rows if (x[1] or 0) <= 0 and x[3] is not None]
    low_open = round(max(low), 3) if low else LOW_OPEN
    return {
        "veto_pb": int(veto_pb) if veto_pb is not None else VETO_PB,
        "hard_veto_pb": int(hard_pb) if hard_pb is not None else HARD_VETO_PB,
        "shrink_ratio": shrink, "low_open": low_open, "n": len(rows),
    }


def auto_calibrate(con):
    """用滚动回测建议值覆盖模块级阈值（仅当 RECVETO_AUTO_CALIB=1 由 build 调用）。
    默认不调用 → 维持硬编码回测默认值，行为不变。返回应用后的建议或 None。"""
    s = suggest_thresholds(con)
    if not s:
        return None
    global VETO_PB, HARD_VETO_PB, SHRINK_RATIO, LOW_OPEN
    VETO_PB, HARD_VETO_PB = s["veto_pb"], s["hard_veto_pb"]
    SHRINK_RATIO, LOW_OPEN = s["shrink_ratio"], s["low_open"]
    return s


def current_thresholds():
    return {"veto_pb": VETO_PB, "hard_veto_pb": HARD_VETO_PB,
            "shrink_ratio": SHRINK_RATIO, "low_open": LOW_OPEN}


def calibrate_score(score, pb):
    """打分校准 V2：p_break 高分位贡献已被证伪，压高分、抬低分位。

    回测：pb<71 分位实际胜率最高(59%)却拿低权重；pb>=82 反而最差。
    平滑折减：pb>=82 时 score*0.88；pb<71 时 score*1.08。
    """
    s = score
    if pb is not None and pb >= VETO_PB:
        s = score * 0.88
    elif pb is not None and pb < 71:
        s = min(score * 1.08, 100)
    return round(s, 1)


# ---------------------------------------------------------------------------
# 竞价纪律（9:25 决策口径，回测可直接执行）
# ---------------------------------------------------------------------------
def auction_gate(open_pct, aq=None):
    """竞价后动作裁决。返回 dict:

      action: "buy"   不低开 + (缩量 或 核心龙头) —— 历史胜率 78%
              "watch" 不低开（普通续强）               —— 历史胜率 53%
              "avoid" 低开                              —— 历史胜率仅 24%
    核心龙头用 tag in ("核心龙头",) 判定；缩量看 aq.vol_anomaly.note 含「缩量」或显式传 shrunk。
    """
    it_tag = ((aq or {}).get("tag")) or ""
    shrunk = False
    note = ((aq or {}).get("vol_anomaly") or {}).get("note") or ""
    if "缩量" in str(note):
        shrunk = True
    low = (open_pct is None) or (open_pct < LOW_OPEN)
    if low:
        return {"action": "avoid",
                "evidence": "历史同条件(竞价低开) T+1 收红率仅 24%% / 均值 -2.24%%"}
    leader = ("核心龙头" in str(it_tag))
    if leader or shrunk:
        return {"action": "buy",
                "evidence": ("核心龙头" if leader else "") +
                            ("缩量承接" if shrunk else "").strip() +
                            " · 不低开——历史胜率 78% / 均值 +5.5%"}
    return {"action": "watch", "evidence": "不低开但无龙头/缩量加成 · 历史胜率约 53%"}


# ---------------------------------------------------------------------------
# 历史胜率透明化（自 rec_picks 表实时统计）
# ---------------------------------------------------------------------------
_VALID_FILTER = "next_continue IN (0,1) AND next_pct IS NOT NULL"


def historical_stats(con, recent_n=20):
    """T+1 收红率统计（口径 = 用户目标口径：次日涨跌幅 > 0 视为赢）。

    数据源 rec_picks：next_pct 是次日真实涨跌幅；收红率以 next_pct>0 判定，
    而非 next_continue（那是「次日再涨停」口径，历史上榜收红率会被低估成 ~17%）。
    """
    out = {"total": None, "recent": None, "recent_n": recent_n,
           "n_total": 0, "n_recent": 0}
    if con is None:
        return out
    try:
        rows = con.execute(
            "SELECT date, next_pct FROM rec_picks WHERE next_pct IS NOT NULL "
            # 2026-08-29 起 rec_picks 混入趋势/动量通道（tag 前缀「趋势·」/「动量·」），
            # 本指标口径是「连板推荐池」胜率实证，需排除其他通道避免稀释
            "AND (tag IS NULL OR (tag NOT LIKE '趋势%' AND tag NOT LIKE '动量%')) "
            "ORDER BY date, code").fetchall()
    except Exception:
        return out
    if not rows:
        return out
    total = len(rows)
    wins = sum(1 for r in rows if (r[1] or 0) > 0)
    recent_rows = rows[-recent_n:]
    rwins = sum(1 for r in recent_rows if (r[1] or 0) > 0)
    out.update({
        "total": round(wins / total * 100, 1) if total else None,
        "recent": round(rwins / len(recent_rows) * 100, 1) if recent_rows else None,
        "n_total": total, "n_recent": len(recent_rows),
        "avg_next_pct": round(sum(float(r[1] or 0) for r in rows) / total, 2),
    })
    return out


def quality_hint(con):
    """给推送/前端的一句话证据：「筛选改造前历史上榜 X 笔 T+1 收红率 Y%；近 Z 笔 W%」"""
    st = historical_stats(con)
    if not st["n_total"]:
        return ""
    base = "上榜 %d 笔 · T+1 收红率 %.0f%%" % (st["n_total"], st["total"] or 0)
    if st["n_recent"]:
        base += " · 近 %d 笔 %.0f%%" % (st["n_recent"], st["recent"] or 0)
    return base


def apply_veto(items):
    """对 recommend() 的 items 全列表执行标注式判定，返回 (kept, vetoed)。
    vetoed 仅含极端(VETO|)者；WARN 者保留在 kept 且带 veto_reason 供降权/标注。"""
    kept, vetoed = [], []
    for it in items:
        reason = veto(it)
        if not reason:
            kept.append(it)
        elif is_veto(reason):
            it.setdefault("veto_reason", reason.split("|", 1)[1])
            vetoed.append(it)
        else:
            it.setdefault("veto_reason", reason.split("|", 1)[1])
            it["risk_flag"] = "⚠"
            kept.append(it)
    return kept, vetoed
