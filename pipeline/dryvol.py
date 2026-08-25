# -*- coding: utf-8 -*-
"""地量/缩量变盘窗口检测

回答实战问题：
1) 全市场成交额缩到什么程度算「地量」？地量之后几天出现变盘（放量方向选择）？
2) 当下是不是地量？连续缩量几天了？按历史经验，变盘概率多大、往上还是往下？

判据（全部由本地日K库自校准）：
- 市场额比 = 当日全市场成交额 / 前20日全市场日均成交额（amt_ratio）
- 地量 = amt_ratio 处于近 lookback 日序列的低分位（默认 <=10%）
- 变盘 = 地量确认日后 5 日内某日额比 >= 1.25（显著放量），记录方向（该日指数涨跌）

纯标准库。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DRY_PCT = 10.0     # 额比分位 <=10% 视为地量
SHRINK_DAYS = 3    # 连续缩量天数阈值
VOLATILE_TH = 1.25 # 放量判定：额比 >= 1.25 视为变盘放量
WINDOW = 5         # 地量后观察变盘的窗口（交易日）


def market_amt_series(u, n=140, upto=None):
    """逐日全市场总成交额 + 额比（对前20日均值的比值）；upto 只取该日（含）之前"""
    ds = [d for d in u.dates if upto is None or d <= upto][-n:]
    out = []
    for d in ds:
        rows = u.by_date.get(d) or []
        amt = sum((b.get("amt") or 0) for _, b in rows)
        out.append({"date": d, "amt": amt})
    for i, r in enumerate(out):
        seg = [x["amt"] for x in out[max(0, i - 20):i] if x["amt"]]
        r["ratio"] = (r["amt"] / (sum(seg) / len(seg))) if (seg and r["amt"]) else None
    return out


def _pct_rank_now(series):
    vals = [r["ratio"] for r in series if r["ratio"] is not None]
    if len(vals) < 30:
        return None
    cur = series[-1]["ratio"]
    if cur is None:
        return None
    below = sum(1 for v in vals if v <= cur)
    return below / len(vals) * 100.0


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def analyze(u, date, n=140):
    """主入口。输出当下地量判定、连缩天数、历史地量后变盘规律。"""
    series = market_amt_series(u, n=n, upto=date)
    if len(series) < 60:
        return None
    if series[-1]["date"] != date:
        return None   # 该日不在交易日序列中

    # 数据完整性保护：当日参与股票数明显低于近期中位（残缺数据）时，
    # 成交额不可比，不判地量（防止部分同步的库产生假信号）
    cnt_today = len(u.by_date.get(date) or [])
    med_cnt = _median([len(u.by_date.get(r["date"]) or []) for r in series[-21:-1]])
    partial = bool(med_cnt and cnt_today < med_cnt * 0.7)
    if not partial:
        amts = [r["amt"] for r in series[-21:-1] if r["amt"]]
        if amts and series[-1]["amt"] and series[-1]["amt"] < _median(amts) * 0.15:
            partial = True

    # 历史地量日集合（用当日往前看的滚动分位，避免未来函数：
    # 每个交易日的分位只用它之前的数据计算）
    dry_days = []   # {"idx","date","ratio"}
    for i in range(20, len(series)):
        hist = [r["ratio"] for r in series[max(0, i - 120):i] if r["ratio"] is not None]
        cur = series[i]["ratio"]
        if cur is None or len(hist) < 30:
            continue
        below = sum(1 for v in hist if v <= cur)
        pr = below / len(hist) * 100.0
        if pr <= DRY_PCT:
            dry_days.append({"idx": i, "date": series[i]["date"], "ratio": cur})

    # 地量 -> 变盘统计（去重：连续地量日合并为一段，取段末日）
    spells = []
    for dd in dry_days:
        if spells and dd["idx"] - spells[-1]["end_idx"] <= 2:
            spells[-1]["end_idx"] = dd["idx"]
        else:
            spells.append({"start_idx": dd["idx"], "end_idx": dd["idx"],
                           "end_date": dd["date"]})

    # 全序列逐日市场平均涨跌幅（方向/波动统计用）
    mkt_pct = []
    for r in series:
        rows = u.by_date.get(r["date"]) or []
        pcts = [b.get("pct") or 0.0 for _, b in rows]
        mkt_pct.append(sum(pcts) / len(pcts) if pcts else 0.0)
    base_vol = sum(abs(x) for x in mkt_pct) / max(1, len(mkt_pct))

    lags, dirs, by_lag_up = [], [], {}
    dir5s, vol5s = [], []      # 段末后5日市场累计方向 / 波动放大倍数
    for sp in spells:
        t = sp["end_idx"]
        hit = None
        for k in range(t + 1, min(t + WINDOW + 1, len(series))):
            r = series[k]["ratio"]
            if r is not None and r >= VOLATILE_TH:
                hit = k - t
                break
        if hit is not None:
            vd = series[t + hit]["date"]
            rows = dict(u.by_date.get(vd) or [])
            chg = [b.get("pct") or 0.0 for b in rows.values()]
            avg = sum(chg) / len(chg) if chg else 0.0
            direction = "up" if avg > 0.15 else ("down" if avg < -0.15 else "flat")
            lags.append(hit)
            dirs.append(direction)
            by_lag_up.setdefault(hit, []).append(direction)
        # 后5日方向与波动（有足够未来数据才算）
        if t + 5 < len(series):
            seg = [mkt_pct[t + k] for k in range(1, 6)]
            dir5s.append(sum(seg))
            v5 = sum(abs(x) for x in seg) / 5
            if base_vol > 1e-9:
                vol5s.append(v5 / base_vol)

    n_sp = len(lags)
    stats = None
    if len(dir5s):
        up_n = sum(1 for x in dirs if x == "up")
        down_n = sum(1 for x in dirs if x == "down")
        dist = {}
        for k in sorted(by_lag_up):
            dist["t%d" % k] = round(len(by_lag_up[k]) / n_sp, 3) if n_sp else 0
        stats = {
            "n": len(dir5s),
            "hit_n": n_sp,
            "hit_rate": round(n_sp / max(1, len(dir5s)), 3),
            "up_rate": round(up_n / n_sp, 3) if n_sp else None,
            "down_rate": round(down_n / n_sp, 3) if n_sp else None,
            "lag_dist": dist,
            "median_lag": sorted(lags)[len(lags) // 2] if lags else None,
            "dir_up_rate": round(sum(1 for x in dir5s if x > 0) / len(dir5s), 3),
            "avg_dir5": round(sum(dir5s) / len(dir5s), 3),
            "vol_expand": round(sum(vol5s) / len(vol5s), 2) if vol5s else None,
        }

    # 当下状态
    hp = _pct_rank_now(series)
    cur_ratio = series[-1]["ratio"]
    shrink = 0
    for i in range(len(series) - 1, 20, -1):
        prev_seg = [r["amt"] for r in series[max(0, i - 21):i - 1]]
        if not prev_seg or not series[i]["amt"]:
            break
        base = sum(prev_seg) / len(prev_seg)
        if series[i]["amt"] < base and i < len(series) - 1 or \
           (i == len(series) - 1 and series[i]["amt"] < base):
            shrink += 1
        else:
            break
    in_dry = hp is not None and hp <= DRY_PCT and not partial
    if partial:
        in_dry = False
        shrink = 0

    state = None
    if in_dry:
        extra = ""
        if stats:
            if stats.get("hit_n"):
                extra = ("历史 %d 次地量段中 %.0f%% 在 %d 日内放量变盘，向上占比 %.0f%%。"
                         % (stats["n"], stats["hit_rate"] * 100, WINDOW,
                            (stats["up_rate"] or 0) * 100))
            else:
                extra = "历史 %d 次地量段均未在 %d 日内放量——地量之后常见继续磨底，勿抢跑。" % (
                    stats["n"], WINDOW)
            if stats.get("vol_expand") is not None:
                extra += "段末后5日波动为常态 %.1f 倍。" % stats["vol_expand"]
        state = {
            "state": "地量区",
            "note": ("当前额比 %.2f，处于近一年 %.0f%% 分位（地量线 %d%%）。"
                     % (cur_ratio or 0, hp, int(DRY_PCT))) + extra,
        }
    elif partial:
        state = {"state": "数据待补全",
                 "note": "当日成交额明显低于常态（疑似残缺数据），地量判定暂缓，待完整收盘数据后重算。"}
    elif shrink >= SHRINK_DAYS:
        state = {"state": "连续缩量", "note": "已连续 %d 日缩量（低于前20日均额），接近地量观察区。" % shrink}

    return {
        "date": date,
        "today": {"date": series[-1]["date"], "ratio": cur_ratio, "hp": hp,
                  "shrink_days": shrink, "in_dry": in_dry, "partial": partial},
        "series": [{"date": r["date"], "ratio": (round(r["ratio"], 3) if r["ratio"] else None)}
                   for r in series[-60:]],
        "dry_spell_n": len(spells),
        "stats": stats,
        "state": state,
    }


def summary_lines(dv):
    if not dv:
        return []
    t = dv.get("today") or {}
    s = dv.get("stats") or {}
    out = []
    if t.get("hp") is not None and not t.get("partial"):
        out.append("市场额比 %.2f（近一年 %.0f%% 分位）｜连续缩量 %d 日%s"
                   % (t.get("ratio") or 0, t["hp"], t.get("shrink_days") or 0,
                      "，已达地量" if t.get("in_dry") else ""))
    if s:
        seg = ["历史 %d 次地量段（%d 日观察窗）" % (s.get("n", 0), WINDOW)]
        if s.get("hit_n"):
            seg.append("%d 日内放量变盘率 %.0f%%，向上占 %.0f%%，最常见 T+%s"
                       % (WINDOW, s["hit_rate"] * 100, (s["up_rate"] or 0) * 100,
                          s.get("median_lag") or "-"))
        else:
            seg.append("尚未出现过 %d 日内放量（地量后常继续磨底）" % WINDOW)
        if s.get("vol_expand") is not None:
            seg.append("段末后5日波动为常态 %.1f 倍、5日累计均 %+.2f%%（上涨占比 %.0f%%）"
                       % (s["vol_expand"], s.get("avg_dir5") or 0,
                          (s.get("dir_up_rate") or 0) * 100))
        out.append("；".join(seg))
    st = dv.get("state")
    if st:
        out.append("%s：%s" % (st["state"], st["note"]))
    return out
