# -*- coding: utf-8 -*-
"""52周新高新低广度统计

回答实战问题：
1) 今天全市场有多少只创 52 周新高 / 新低？占比多少？广度是在扩张还是收缩？
2) 新高新低比（NH-NL Ratio）历史上处于什么水平？极值往往对应情绪顶/底。
3) 当下创新高/新低的股票都是谁（清单参考）？

口径：
- 新高：当日 close >= 近 250 日（约一年）最高收盘（含当日）
- 新低：当日 close <= 近 250 日最低收盘
- 广度序列逐日可算，输出近 60 日走势供前端画线。

纯标准库。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WIN = 250        # 52 周 ≈ 250 个交易日
SERIES_N = 60    # 输出广度序列长度


def scan(u, date, topn=12):
    n_total = 0
    highs, lows = [], []          # 当日新高/新低清单
    series = []                   # 逐日 {date, nh, nl, ratio}
    ds = u.dates

    # 预取每只股票的收盘序列，滚动窗口统计逐日 NH/NL
    from collections import defaultdict
    nh_by_date = defaultdict(int)
    nl_by_date = defaultdict(int)

    for code, bs in u.bars.items():
        st = u.stocks.get(code, {})
        name = st.get("name") or ""
        if "ST" in name.upper():
            continue
        closes = [b["c"] for b in bs if b["c"]]
        if len(closes) < 60:      # 次新股不足 60 根不参与（250 窗口退化太严重）
            continue
        n = len(closes)
        start = max(WIN, 1)
        for i in range(start, n):
            w = closes[i - WIN:i]
            c = closes[i]
            d = bs[i]["d"]
            if c >= max(w):
                nh_by_date[d] += 1
                if i == n - 1:
                    highs.append({"code": code, "name": name, "close": round(c, 2)})
            elif c <= min(w):
                nl_by_date[d] += 1
                if i == n - 1:
                    lows.append({"code": code, "name": name, "close": round(c, 2)})

    total_by_date = {}
    for code, bs in u.bars.items():
        nm = (u.stocks.get(code, {}) or {}).get("name") or ""
        if "ST" in nm.upper():
            continue
        for b in bs:
            total_by_date[b["d"]] = total_by_date.get(b["d"], 0) + 1

    for d in ds[-SERIES_N:]:
        nh, nl = nh_by_date.get(d, 0), nl_by_date.get(d, 0)
        tot = total_by_date.get(d, 0) or 1
        series.append({"date": d,
                       "nh": nh, "nl": nl,
                       "nh_pct": round(nh / tot * 100, 2),
                       "nl_pct": round(nl / tot * 100, 2),
                       "ratio": round((nh - nl) / max(1, nh + nl), 3)})

    last = series[-1] if series else None
    # 分位：用全部可得历史日期算占比分位（比展示序列更长，口径更稳）
    hist_dates = [d for d in ds if total_by_date.get(d)]
    nh_all, nl_all = [], []
    for d in hist_dates:
        tot = total_by_date.get(d) or 1
        nh_all.append(nh_by_date.get(d, 0) / tot * 100)
        nl_all.append(nl_by_date.get(d, 0) / tot * 100)

    def _rank(v, hist):
        if not hist or v is None:
            return None
        return round(sum(1 for x in hist if x <= v) / len(hist) * 100, 1)

    span_note = "近一年" if len(hist_dates) >= 200 else "近 %d 日" % len(hist_dates)
    highs.sort(key=lambda x: -x["close"])
    lows.sort(key=lambda x: x["close"])
    return {
        "date": date,
        "today": {
            "nh": last["nh"], "nl": last["nl"],
            "nh_pct": last["nh_pct"], "nl_pct": last["nl_pct"],
            "ratio": last["ratio"],
            "nh_rank": _rank(last["nh_pct"], nh_all[:-1]),
            "nl_rank": _rank(last["nl_pct"], nl_all[:-1]),
        },
        "span_note": span_note,
        "series": series,
        "new_highs": highs[:topn],
        "new_lows": lows[:topn],
        "n_highs": len(highs),
        "n_lows": len(lows),
    }


def summary_lines(nb):
    """推送用紧凑摘要"""
    if not nb:
        return []
    t = nb.get("today") or {}
    out = ["52周广度：新高 %d 只（%.2f%%）vs 新低 %d 只（%.2f%%），NH-NL 比 %+.2f"
           % (t.get("nh", 0), t.get("nh_pct", 0), t.get("nl", 0),
              t.get("nl_pct", 0), t.get("ratio", 0))]
    if t.get("nh_rank") is not None and t.get("nl_rank") is not None:
        out.append("广度分位：新高占比处%s %.0f%% 分位，新低占比 %.0f%% 分位"
                   % (nb.get("span_note") or "历史", t["nh_rank"], t["nl_rank"]))
    hs = nb.get("new_highs") or []
    ls = nb.get("new_lows") or []
    if hs or ls:
        out.append("%s%s" % (
            ("新高前排：" + "、".join(x["name"] for x in hs[:4])) if hs else "",
            ("｜新低前排：" + "、".join(x["name"] for x in ls[:4])) if ls else ""))
    return out
