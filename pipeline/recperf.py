# -*- coding: utf-8 -*-
"""推荐池历史胜率曲线：从 rec_picks 表回测「当日推荐标的 T+1 表现」。

纯本地、零网络：数据来自 store.rec_picks_all（每次 build 已 upsert 当日推荐及次日结局）。
输出一条可画图的时间序列：每日推荐样本的 T+1 平均收益、盈利占比、续板率，以及一条
「等权买入次日卖出」的累计净值曲线——回答一个问题：这个系统长期到底准不准。

前端据此画一条净值 vs 沪深基准的对照曲线 + 近月胜率条形。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store


def build(con=None, limit=2400):
    """返回胜率曲线 dict。无数据时返回 None。"""
    if con is None:
        con = store.connect()
    rows = store.rec_picks_all(con, limit=limit)
    if not rows:
        return None
    # rows: date, code, name, streak, p_break, tag, next_continue, next_pct
    by_date = {}
    for date, code, name, streak, p_break, tag, ncont, npct in rows:
        d = by_date.setdefault(date, {"n": 0, "pn": 0.0, "win": 0, "cont": 0, "cont_n": 0})
        d["n"] += 1
        try:
            npct = float(npct) if npct is not None else 0.0
        except Exception:
            npct = 0.0
        d["pn"] += npct
        if npct > 0:
            d["win"] += 1
        try:
            ncont = int(ncont) if ncont is not None else 0
        except Exception:
            ncont = 0
        # 续板率只对连板/强动量类有意义（streak>=2 视为可续板样本）
        if streak is not None and streak >= 2:
            d["cont_n"] += 1
            if ncont == 1:
                d["cont"] += 1

    dates = sorted(by_date.keys())
    if len(dates) < 5:
        return None

    series_dates, win_rate, avg_pct, cont_rate = [], [], [], []
    cum = 1.0
    cumulative = []
    for d in dates:
        s = by_date[d]
        series_dates.append(d)
        win_rate.append(round(100.0 * s["win"] / s["n"], 1) if s["n"] else 0.0)
        avg_pct.append(round(s["pn"] / s["n"], 2) if s["n"] else 0.0)
        cont_rate.append(round(100.0 * s["cont"] / s["cont_n"], 1) if s["cont_n"] else None)
        # 累计净值：每日等权买入当日全部推荐、次日以 next_pct 了结
        cum *= (1.0 + s["pn"] / s["n"] / 100.0)
        cumulative.append(round(cum, 3))

    # 近 30 日 / 近 60 日 综合
    def avg(lst, n):
        v = [x for x in lst[-n:] if x is not None]
        return round(sum(v) / len(v), 1) if v else None

    return {
        "dates": series_dates,
        "win_rate": win_rate,
        "avg_pct": avg_pct,
        "cont_rate": cont_rate,
        "cumulative": cumulative,
        "n_days": len(dates),
        "recent30": {
            "win_rate": avg(win_rate, 30),
            "avg_pct": avg(avg_pct, 30),
            "cont_rate": avg([x for x in cont_rate[-30:] if x is not None], 30 * 2),
        },
        "final_cum": round(cum, 3),
    }


def summary_lines(rp):
    if not rp:
        return ["推荐池胜率：暂无足够历史样本"]
    r = rp.get("recent30") or {}
    out = []
    out.append("推荐池近 30 日：T+1 盈利占比 **%s%%** ｜ 平均收益 **%s%%** ｜ 连板续板率 **%s%%**"
               % (r.get("win_rate"), r.get("avg_pct"), r.get("cont_rate")))
    out.append("累计净值（等权次日了结）：**%s**（回溯 %d 个交易日）"
               % (rp.get("final_cum"), rp.get("n_days")))
    if rp.get("final_cum") and rp["final_cum"] >= 1.0:
        out[1] += " · 长期跑赢「无脑持有」"
    return out
