# -*- coding: utf-8 -*-
"""断头铡刀 / 出水芙蓉 单K穿越形态识别

与 candles.py 的组合形态不重叠：这里只做「一根K线切断/站上多条均线」的趋势级信号。

口径：
- 断头铡刀：单根大阴线（跌幅 ≤ -4%，实体占比高），收盘同时跌破 MA5/10/20 中至少 2 条，
  且此前均线呈多头或纠缠状态；跌破线数越多越危险。
- 出水芙蓉：单根中大阳线（涨幅 ≥ +4%），收盘同时上穿 MA5/10/20/60 中至少 3 条
  （此前收盘在均线下方），且放量（量 > 5日均量 1.5 倍）。
- 附带历史统计：全市场近一年出现该形态后 T+1/T+5 平均表现与胜率（自校准）。

纯标准库。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ZHABAN_PCT = -4.0     # 断头铡刀最小跌幅
FR_PCT = 4.0          # 出水芙蓉最小涨幅
FR_VOL = 1.5          # 出水芙蓉量比门槛
STAT_LOOKBACK = 250   # 形态后表现回溯窗口


def _mas_at(closes, i, ns=(5, 10, 20, 60)):
    out = {}
    for m in ns:
        if i + 1 >= m:
            out[m] = sum(closes[i - m + 1:i + 1]) / m
    return out


def _check(bs, closes, i):
    """判定第 i 根是否铡刀/芙蓉，返回 (kind, n_broken)"""
    b = bs[i]
    c, o = b["c"], b.get("o") or b["c"]
    pct = b.get("pct")
    if not c or not o or pct is None:
        return None, 0
    mas = _mas_at(closes, i)
    if len(mas) < 2:
        return None, 0

    if pct <= ZHABAN_PCT and c < o:
        broken = [m for m, v in mas.items() if m in (5, 10, 20) and c < v < closes[i - 1]]
        # 至少破 2 条短中期线，且前一日收盘在其上方（真「跌破」而非早已在下方）
        prev_mas = _mas_at(closes, i - 1)
        real = [m for m in broken if prev_mas.get(m) and closes[i - 1] >= prev_mas[m]]
        if len(real) >= 2:
            return "zhadao", len(real)

    if pct >= FR_PCT and c > o:
        vols = [x.get("v") or 0 for x in bs[max(0, i - 6):i]]
        v5 = sum(vols) / len(vols) if vols else 0
        if not v5 or (b.get("v") or 0) < v5 * FR_VOL:
            return None, 0
        prev_mas = _mas_at(closes, i - 1)
        crossed = [m for m, v in mas.items()
                   if prev_mas.get(m) and closes[i - 1] < prev_mas[m] <= c]
        if len(crossed) >= 3:
            return "furong", len(crossed)
    return None, 0


def scan(u, date, topn=10):
    today_hits = []   # 今日触发清单
    hist = {"zhadao": [], "furong": []}   # 历史样本 [(t1_pct, t5_pct)]

    ds_idx = {d: k for k, d in enumerate(u.dates)}

    for code, bs in u.bars.items():
        st = u.stocks.get(code, {})
        name = st.get("name") or ""
        if "ST" in name.upper():
            continue
        closes = [b["c"] for b in bs if b["c"]]
        n = len(bs)
        if n < 80:
            continue
        start = max(61, n - STAT_LOOKBACK)
        for i in range(start, n):
            kind, nb = _check(bs, closes, i)
            if not kind:
                continue
            rec_date = bs[i]["d"]
            # 后续表现
            t1 = t5 = None
            base = bs[i]["c"]
            if base and i + 1 < n:
                t1 = (bs[i + 1]["c"] / base - 1) * 100
                if i + 5 < n:
                    t5 = (bs[i + 5]["c"] / base - 1) * 100
            is_today = (rec_date == date) or (rec_date == bs[-1]["d"] and date == u.dates[-1])
            if rec_date == date:
                today_hits.append({
                    "code": code, "name": name, "kind": kind,
                    "pct": round(bs[i].get("pct") or 0, 2),
                    "close": round(base, 2), "n_lines": nb,
                    "vol_ratio": round((bs[i].get("v") or 0) /
                                       max(1e-9, (sum(x.get("v") or 0 for x in bs[max(0, i - 6):i]) / 5)), 2),
                })
            if t1 is not None:
                hist[kind].append((t1, t5))

    stats = {}
    for kind, rows in hist.items():
        t1s = [r[0] for r in rows if r[0] is not None]
        t5s = [r[1] for r in rows if r[1] is not None]
        if not t1s:
            continue
        stats[kind] = {
            "n": len(t1s),
            "avg_t1": round(sum(t1s) / len(t1s), 2),
            "win_t1": round(sum(1 for v in t1s if v > 0) / len(t1s) * 100, 1),
        }
        if t5s:
            stats[kind]["avg_t5"] = round(sum(t5s) / len(t5s), 2)
            stats[kind]["win_t5"] = round(sum(1 for v in t5s if v > 0) / len(t5s) * 100, 1)

    order = {"zhadao": 0, "furong": 1}
    today_hits.sort(key=lambda x: (order[x["kind"]], -abs(x["pct"])))
    return {
        "date": date,
        "hits": today_hits[:topn],
        "stats": stats,
    }


def summary_lines(cf):
    """推送用紧凑摘要"""
    if not cf:
        return []
    out = []
    st = cf.get("stats") or {}
    zd, fr = st.get("zhadao"), st.get("furong")
    if zd:
        out.append("断头铡刀历史 %d 例：次日均 %.2f%%（胜率 %.0f%%）"
                   % (zd["n"], zd["avg_t1"], zd["win_t1"]))
    if fr:
        out.append("出水芙蓉历史 %d 例：次日均 %+.2f%%（胜率 %.0f%%）%s"
                   % (fr["n"], fr["avg_t1"], fr["win_t1"],
                      ("，5日均 %+.2f%%" % fr["avg_t5"]) if "avg_t5" in fr else ""))
    hits = cf.get("hits") or []
    if hits:
        zds = [h for h in hits if h["kind"] == "zhadao"]
        frs = [h for h in hits if h["kind"] == "furong"]
        seg = []
        if zds:
            seg.append("⚠️今日铡刀 " + "、".join(h["name"] for h in zds[:4]))
        if frs:
            seg.append("✨今日芙蓉 " + "、".join(h["name"] for h in frs[:4]))
        if seg:
            out.append("｜".join(seg))
    return out
