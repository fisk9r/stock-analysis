# -*- coding: utf-8 -*-
"""跳空缺口检测 + 回补规律统计

回答两个实战问题：
1) 历史上跳空缺口（≥1%）多快被回补？按深度/方向分桶的回补率是多少？
   —— 向上缺口未回补是支撑，向下缺口未回补是压力；回补率给「缺口必补」迷信定量。
2) 当前市场上还挂着哪些未回补缺口？
   —— 近期形成的未回补缺口清单：向上=支撑参考，向下=压力/补跌风险参考。

全部由本地日K库实测统计（自校准，不写死经验值）。纯标准库。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GAP_TH = 0.01          # 缺口幅度阈值（跳空方向 ≥1%）
DEEP_BUCKETS = [(0.01, 0.02, "1-2%"), (0.02, 0.03, "2-3%"),
                (0.03, 0.05, "3-5%"), (0.05, 99.0, ">5%")]
STATS_MATURE = 10      # 统计样本需在形成后还有 ≥10 根K线（防右截断偏倚）
OPEN_WINDOW = 20       # 「当前未回补」只看近 20 个交易日内形成的缺口


def _bucket(pct):
    for lo, hi, name in DEEP_BUCKETS:
        if lo <= pct < hi:
            return name
    return None


def scan(u, date, lookback=260, topn=12):
    """主入口：
    - 历史规律：全市场近 lookback 日所有缺口的 T+1/3/5/10 回补率，按深度与方向分桶；
    - 当下清单：近 OPEN_WINDOW 日形成、至今仍未回补的缺口股。
    """
    all_gaps = []          # (form_idx_from_end, dir, pct, fill_lag|None) —— fill_lag 以形成日为 0
    open_gaps = []

    for code, bs in u.bars.items():
        st = u.stocks.get(code, {})
        name = st.get("name") or ""
        if "ST" in name.upper():
            continue
        n = len(bs)
        if n < 30:
            continue
        start = max(1, n - lookback)
        for i in range(start, n):
            prev, cur = bs[i - 1], bs[i]
            ph, pl, po = prev["h"], prev["l"], prev["c"]
            if not ph or not pl or not po:
                continue
            gdir, gpct, glow, ghigh = None, 0.0, 0.0, 0.0
            if cur["l"] > ph:
                gpct = cur["l"] / ph - 1
                if gpct >= GAP_TH:
                    gdir, glow, ghigh = "up", ph, cur["l"]
            elif cur["h"] < pl:
                gpct = cur["h"] / pl - 1
                if gpct <= -GAP_TH:
                    gdir, glow, ghigh = "down", cur["h"], pl
            if not gdir:
                continue
            a_pct = abs(gpct)

            # 回补判定：向上缺口被后续 low ≤ 缺口下沿回补；向下被 high ≥ 上沿回补
            fill_lag = None
            for j in range(i + 1, n):
                b = bs[j]
                if gdir == "up" and b["l"] <= glow:
                    fill_lag = j - i
                    break
                if gdir == "down" and b["h"] >= ghigh:
                    fill_lag = j - i
                    break
            age = (n - 1) - i
            all_gaps.append((age, gdir, a_pct, fill_lag))

            if fill_lag is None and age < OPEN_WINDOW:
                open_gaps.append({
                    "code": code, "name": name, "dir": gdir,
                    "gap_date": bs[i]["d"], "gap_pct": round(a_pct * 100, 1),
                    "days_alive": age, "gap_low": round(glow, 2), "gap_high": round(ghigh, 2),
                })

    def _rates(rows):
        if not rows:
            return {}
        out = {}
        for k in (1, 3, 5, 10):
            obs = [g for g in rows if g[0] >= k]           # 形成后至少还有 k 根K线的样本
            filled = sum(1 for g in obs if g[3] is not None and g[3] <= k)
            out["t%d" % k] = {"n": len(obs),
                              "rate": round(filled / len(obs), 3)} if obs else {"n": 0, "rate": None}
        return out

    mature = [g for g in all_gaps if g[0] >= STATS_MATURE]
    by_depth = {}
    for lo, hi, nm in DEEP_BUCKETS:
        rows = [g for g in mature if lo <= g[2] < hi]
        r = _rates(rows)
        if r.get("t5", {}).get("n"):
            by_depth[nm] = {"n": r["t5"]["n"], "fill_t5": r["t5"]["rate"]}
    ups = _rates([g for g in mature if g[1] == "up"])
    downs = _rates([g for g in mature if g[1] == "down"])

    stats = {
        "n_total": len(all_gaps),
        "overall": _rates(mature),
        "by_depth": by_depth,
        "up_t5": (ups.get("t5") or {}).get("rate"),
        "down_t5": (downs.get("t5") or {}).get("rate"),
        "up_n": (ups.get("t5") or {}).get("n", 0),
        "down_n": (downs.get("t5") or {}).get("n", 0),
    }

    open_gaps.sort(key=lambda x: -x["gap_pct"])
    return {
        "date": date,
        "stats": stats,
        "open_gaps": open_gaps[:topn],
        "open_n": len(open_gaps),
    }


def summary_lines(g):
    """推送用紧凑摘要"""
    if not g:
        return []
    s = g.get("stats") or {}
    ov = (s.get("overall") or {}).get("t5") or {}
    out = []
    if ov.get("n"):
        out.append("缺口回补规律：历史 %d 个缺口（≥1%%），5 日内回补 %.0f%%"
                   % (ov["n"], (ov["rate"] or 0) * 100))
    if s.get("by_depth"):
        parts = ["%s:%.0f%%(%d)" % (k, v["fill_t5"] * 100, v["n"])
                 for k, v in sorted(s["by_depth"].items())]
        if parts:
            out.append("按深度 5 日回补：" + "｜".join(parts))
    if s.get("up_t5") is not None and s.get("down_t5") is not None:
        out.append("方向差异：向上缺口 5 日回补 %.0f%% vs 向下 %.0f%%"
                   % (s["up_t5"] * 100, s["down_t5"] * 100))
    og = g.get("open_gaps") or []
    if og:
        ups = [x for x in og if x["dir"] == "up"]
        downs = [x for x in og if x["dir"] == "down"]
        line = "当前未回补 %d 个：%s%s%s" % (
            g.get("open_n", len(og)),
            ("支撑 " + "、".join("%s(%.1f%%)" % (x["name"], x["gap_pct"]) for x in ups[:3])) if ups else "",
            "｜" if (ups and downs) else "",
            ("压力 " + "、".join("%s(%.1f%%)" % (x["name"], x["gap_pct"]) for x in downs[:3])) if downs else "")
        out.append(line.rstrip("："))
    return out
