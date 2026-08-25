# -*- coding: utf-8 -*-
"""市场风格判定引擎：小微盘题材轮动 / 连板接力 / 大盘权重抱团 / 核心资产趋势主升

回答实战问题：今天这市场是谁在主导？该用什么打法？
- 小微盘题材轮动市：涨停池被小市值霸榜，高换手高轮动，接力打板打法
- 中小盘连板接力市：连板梯队完整、晋级率健康，空间板打开高度
- 大盘权重护盘/指数市：权重超额收益为正、涨停以中大市值为主
- 核心资产抱团趋势市：成交额向少数强趋势票集中（CR10 高），机构风格主升浪
- 均衡混合市 / 风格切换预警

判据（全由本地日K库自校准）：
1) 涨停股市值分布：小微盘(<50亿)/小盘(50-100)/中盘(100-300)/大盘(>300亿) 占比与中位数
2) CR10 成交额集中度：当日成交额 Top10 占全市场比重（抱团度）
3) 权重超额：流通市值 Top100 当日均涨 − 全市场均涨（α）
4) 强趋势占比：均线多头(c>MA20>MA60 且 20日涨幅>8%)股的成交额占比（趋势资金浓度）

纯标准库。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CAP_BUCKETS = [(0, 50, "微盘"), (50, 100, "小盘"),
               (100, 300, "中盘"), (300, 1e18, "大盘")]
CR10_CROWD = 0.22      # CR10 ≥22% 视为抱团
TREND_AMT_CROWD = 0.30 # 强趋势股成交额占比 ≥30% 视为趋势资金主导
WEIGHT_ALPHA = 0.5     # 权重超额 ≥0.5pct 视为权重强


def _bucket(fmv):
    for lo, hi, nm in CAP_BUCKETS:
        if lo <= fmv < hi:
            return nm
    return "大盘"


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def metrics_for_date(u, d):
    """单日四维指标"""
    rows = u.by_date.get(d) or []
    if not rows:
        return None

    # 1) 涨停股市值分布（float_mv 单位为元，换算成亿）
    caps = []
    zt = u.zt.get(d) or set()
    for c in zt:
        fmv = (u.stocks.get(c, {}) or {}).get("float_mv")
        if fmv:
            fmv_yi = fmv / 1e8
            caps.append((fmv_yi, _bucket(fmv_yi)))
    dist = {}
    for _, b in caps:
        dist[b] = dist.get(b, 0) + 1
    med_cap = _median([c for c, _ in caps])

    # 2) CR10 + 全市场均涨
    amts, pcts, total_amt = [], [], 0.0
    for c, b in rows:
        a = b.get("amt") or 0
        total_amt += a
        amts.append((a, c))
        pcts.append(b.get("pct") or 0.0)
    amts.sort(reverse=True)
    top = [x[0] for x in amts[:10]]
    cr10 = sum(top) / total_amt if total_amt else None

    mkt_avg = sum(pcts) / len(pcts) if pcts else 0.0

    # 3) 权重超额（流通市值 Top100，单位元）
    bigs = sorted(((s.get("float_mv") or 0), c)
                  for c, s in u.stocks.items() if s.get("float_mv"))[-100:]
    bigset = {c for _, c in bigs}
    bp = [b.get("pct") or 0.0 for c, b in rows if c in bigset]
    weight_alpha = (sum(bp) / len(bp) - mkt_avg) if bp else None

    # 4) 强趋势占比（c>MA20>MA60 且 20日涨幅>8% 的股票成交额占比）
    trend_amt = 0.0
    trend_n = 0
    for c, b in rows:
        bs = [x["c"] for x in u.bars.get(c, []) if x["d"] <= d]
        n = len(bs)
        if n < 65:
            continue
        ma20 = sum(bs[-20:]) / 20
        ma60 = sum(bs[-60:]) / 60
        c20 = bs[-1] / bs[-21] - 1 if bs[-21] else 0
        if bs[-1] > ma20 > ma60 and c20 > 0.08:
            trend_amt += b.get("amt") or 0
            trend_n += 1
    trend_share = trend_amt / total_amt if total_amt else None

    return {
        "date": d,
        "zt_n": len(zt),
        "zt_dist": dist,
        "zt_med_cap": round(med_cap, 1) if med_cap else None,
        "cr10": round(cr10, 3) if cr10 is not None else None,
        "weight_alpha": round(weight_alpha, 2) if weight_alpha is not None else None,
        "trend_share": round(trend_share, 3) if trend_share is not None else None,
        "trend_n": trend_n,
    }


def verdict_of(m):
    """规则判定 -> {style, label, note}"""
    dist = m.get("zt_dist") or {}
    zt_n = m.get("zt_n", 0)
    micro_small = dist.get("微盘", 0) + dist.get("小盘", 0)
    ms_ratio = micro_small / zt_n if zt_n else 0
    cr10 = m.get("cr10")
    alpha = m.get("weight_alpha")
    tsh = m.get("trend_share")

    notes = ["涨停 %d 只，中位流通市值 %.0f 亿（微/小盘占 %.0f%%）"
             % (zt_n, m.get("zt_med_cap") or 0, ms_ratio * 100),
             "CR10 成交集中度 %.0f%%" % ((cr10 or 0) * 100)]
    if alpha is not None:
        notes.append("权重超额 %+.2f pct" % alpha)
    if tsh is not None:
        notes.append("强趋势股额占比 %.0f%%（%d 只）" % (tsh * 100, m.get("trend_n", 0)))

    if cr10 is not None and cr10 >= CR10_CROWD and tsh is not None and tsh >= TREND_AMT_CROWD \
            and alpha is not None and alpha < WEIGHT_ALPHA:
        style, label = "crowd_trend", "核心资产抱团 · 趋势主升市"
        note = "成交额向少数强趋势票高度集中，权重不占优——机构资金抱团主升浪，宜持股不动、忌频繁换股。"
    elif alpha is not None and alpha >= WEIGHT_ALPHA and ms_ratio >= 0.55:
        style, label = "dual_track", "双轨市 · 权重护盘 + 小微盘题材轮动"
        note = ("指数由权重托底，涨停池却以微/小市值为主——稳指数与做弹性并行，"
                "权重负责底仓、小盘题材快进快出，警惕风格随时收敛。")
    elif alpha is not None and alpha >= WEIGHT_ALPHA and (cr10 or 0) >= CR10_CROWD * 0.8:
        style, label = "big_weight", "大盘权重主导 · 指数护盘市"
        note = "权重超额收益显著、量能向头部集中——指数行情/护盘特征，中小票注意抽血效应。"
    elif ms_ratio >= 0.6 and (m.get("zt_med_cap") or 999) < 80:
        style, label = "micro_theme", "小微盘题材轮动市"
        note = "涨停池被微/小市值霸榜，题材快速轮动、一日游频发——快进快出打板/低吸打法，忌追高恋战。"
    elif zt_n and 0.35 <= ms_ratio < 0.6:
        style, label = "mid_relay", "中小盘连板接力市"
        note = "涨停分布均衡偏中小盘，看连板梯队完整性与晋级率定强弱——接力打法看高做低。"
    else:
        style, label = "balanced", "均衡混合市"
        note = "市值分布与集中度均无极端倾向——多空观望，等待风格选择方向再下注。"

    # 风格切换提示由 build 层对比昨日判定后追加
    return {"style": style, "label": label, "note": note, "evidence": notes}


def scan(u, date, series_n=20):
    ds = u.dates
    cur_m = metrics_for_date(u, date)
    if not cur_m:
        return None
    v = verdict_of(cur_m)

    prev_d = u.prev_date(date)
    switch = None
    if prev_d:
        pm = metrics_for_date(u, prev_d)
        if pm:
            pv = verdict_of(pm)["style"]
            if pv != v["style"]:
                switch = {"from_style": pv, "to_style": v["style"]}
                v["note"] = ("⚠️风格切换：%s → 今日 %s。" % (style_cn(pv), v["label"])) + v["note"]

    series = []
    for d in ds[-series_n:]:
        mm = metrics_for_date(u, d)
        if mm:
            series.append({"date": d, "cr10": mm["cr10"],
                           "weight_alpha": mm["weight_alpha"],
                           "trend_share": mm["trend_share"],
                           "zt_med_cap": mm["zt_med_cap"]})

    return {
        "date": date,
        "today": cur_m,
        "verdict": v,
        "switch": switch,
        "series": series,
    }


_STYLE_CN = {
    "crowd_trend": "核心资产抱团趋势",
    "big_weight": "大盘权重主导",
    "dual_track": "双轨市（权重护盘+小微盘题材）",
    "micro_theme": "小微盘题材轮动",
    "mid_relay": "中小盘连板接力",
    "balanced": "均衡混合",
}


def style_cn(k):
    return _STYLE_CN.get(k, k)


def summary_lines(sty):
    """推送用紧凑摘要"""
    if not sty:
        return []
    v = sty.get("verdict") or {}
    out = ["市场风格判定：**%s**" % v.get("label", "—")]
    for e in (v.get("evidence") or [])[:2]:
        out.append(e)
    if sty.get("switch"):
        out.append("⚠️检测到风格切换：%s → %s" % (
            style_cn(sty["switch"]["from_style"]), style_cn(sty["switch"]["to_style"])))
    return out
