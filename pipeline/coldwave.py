# -*- coding: utf-8 -*-
"""冷启修复节奏预判 + 冷后领涨风格轮动规律

回答两个实战问题：
1) 市场偏冷/爆冷之后，第几天开始修复？是隔天(T+1)还是再隔几天？
2) 每次转冷后领涨的是什么风格（价位/市值/超跌程度/行业），方向会不会重复？

全部结论由本地日K库实测统计得出（自校准，不写死经验值）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine

COLD_TH = 30.0      # 热度百分位 <=30 视为偏冷
DEEP_TH = 10.0      # <=10 视为爆冷
WARM_TH = 50.0      # >=50 视为修复（回到中性以上）


def _zs(vals):
    good = [v for v in vals if v is not None]
    if len(good) < 2:
        return [0.0] * len(vals)
    m = sum(good) / len(good)
    sd = (sum((v - m) ** 2 for v in good) / len(good)) ** 0.5 or 1.0
    return [((v - m) / sd if v is not None else 0.0) for v in vals]


def _pct_rank(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    n = len(vals)
    for rank, i in enumerate(order):
        out[i] = rank / max(1, n - 1) * 100.0
    return out


def level_of(hp):
    if hp <= DEEP_TH:
        return "爆冷"
    if hp <= COLD_TH:
        return "偏冷"
    if hp < 70:
        return "温"
    return "热"


# ============================================================== 1. 热度序列
def heat_series(u, n=140, emo=None):
    """逐日热度合成 + 百分位。emo 可传入已算好的 daily_emotion 列表以复用。"""
    ds = u.dates[-n:]
    rows = emo if emo else [engine.daily_emotion(u, d) for d in ds]
    if len(rows) != len(ds):
        ds = [r["date"] for r in rows]

    zt = [r["zt"] for r in rows]
    pr = [r["promote_rate"] for r in rows]
    yg = [r["yest_green"] for r in rows]
    mx = [r["max_lb"] for r in rows]
    upr = [(r["up"] / max(1, r["up"] + r["down"]) * 100.0) for r in rows]
    bar = [r.get("bench_amt_ratio") for r in rows]

    zzt, zpr, zupr, zbar, zmx, zyg = _zs(zt), _zs(pr), _zs(upr), _zs(bar), _zs(mx), _zs(yg)
    heat = [1.0 * zzt[i] + 0.9 * zpr[i] + 1.0 * zupr[i] + 0.8 * zbar[i]
            + 0.5 * zmx[i] - 0.8 * zyg[i] for i in range(len(rows))]
    hp = _pct_rank(heat)
    out = []
    for i, r in enumerate(rows):
        out.append({
            "date": r["date"], "zt": r["zt"], "lb": r["lb"], "max_lb": r["max_lb"],
            "promote_rate": r["promote_rate"], "up_ratio": round(upr[i], 1),
            "bench_amt_ratio": r.get("bench_amt_ratio"),
            "yest_green": r["yest_green"],
            "heat": round(heat[i], 3), "hp": round(hp[i], 1), "level": level_of(hp[i]),
        })
    return out


# ============================================================== 2. 冷段 + 修复节奏
def cold_spells(series):
    """连续偏冷聚成一段，取最冷日为『冷谷』"""
    spells = []
    i = 0
    n = len(series)
    while i < n:
        if series[i]["hp"] <= COLD_TH:
            j = i
            while j + 1 < n and series[j + 1]["hp"] <= COLD_TH:
                j += 1
            seg = list(range(i, j + 1))
            t = min(seg, key=lambda k: series[k]["hp"])
            spells.append({"start": i, "end": j, "trough": t, "len": len(seg),
                           "trough_date": series[t]["date"], "min_hp": series[t]["hp"]})
            i = j + 1
        else:
            i += 1
    return spells


def _first_repair(series, t):
    for k in range(t + 1, len(series)):
        if series[k]["hp"] >= WARM_TH:
            return k
    return None


def repair_stats(series, spells):
    """修复滞后的经验分布：整体 / 按冷度深浅 / 按冷段长度"""
    lags, by_depth, by_len = [], {}, {}
    for sp in spells:
        t = sp["trough"]
        r = _first_repair(series, t)
        if r is None:
            continue
        lag = r - t
        lags.append(lag)
        key = "爆冷" if sp["min_hp"] < DEEP_TH else ("深冷" if sp["min_hp"] < 20 else "偏冷")
        by_depth.setdefault(key, []).append(lag)
        by_len.setdefault(sp["len"], []).append(lag)

    def summarize(xs):
        if not xs:
            return None
        s = sorted(xs)
        n = len(s)
        return {
            "n": n,
            "median": s[n // 2],
            "mean": round(sum(s) / n, 2),
            "p_t1": round(sum(1 for x in s if x == 1) / n, 3),
            "p_t2": round(sum(1 for x in s if x == 2) / n, 3),
            "p_t3": round(sum(1 for x in s if x == 3) / n, 3),
            "p_t4p": round(sum(1 for x in s if x >= 4) / n, 3),
            "cum_t2": round(sum(1 for x in s if x <= 2) / n, 3),
            "cum_t3": round(sum(1 for x in s if x <= 3) / n, 3),
        }

    return {
        "overall": summarize(lags),
        "by_depth": {k: summarize(v) for k, v in by_depth.items()},
        "by_len": {str(k): summarize(v) for k, v in by_len.items()},
    }


# ============================================================== 3. 冷后领涨股扫描
def _launch_targets(series, spells, max_lag=3):
    """冷谷之后 T+1..T+max_lag 的日期索引 -> 归属冷谷"""
    tgt = {}
    for sp in spells:
        t = sp["trough"]
        for lag in range(1, max_lag + 1):
            k = t + lag
            if k < len(series):
                tgt.setdefault(k, (t, lag))
    return tgt


def launch_scan(u, series, spells, code2boards, max_lag=3):
    """扫描每个冷谷后 T+1..T+3 启动的强势股，输出风格画像 + 轮动序列。

    启动定义：起始日后 6 日内 >=2 个涨停且区间涨幅 >=15%，且启动前 20 日涨幅 <10%
    （即『前期没走过、从冷位起爆』）。只取主板/中小板 10% 制度，避开 20%/30% 制度差异。
    """
    di = {r["date"]: i for i, r in enumerate(series)}
    tgt = _launch_targets(series, spells, max_lag)
    samples = []

    for code, bs in u.bars.items():
        lim = u.lim.get(code)
        if lim is None or lim > 11:
            continue
        idx = {b["d"]: i for i, b in enumerate(bs)}
        for d, (tr, lag) in tgt.items():
            date = series[d]["date"]
            bi = idx.get(date)
            if bi is None or bi < 21 or bi + 5 >= len(bs):
                continue
            win = bs[bi:bi + 6]
            nzt = sum(1 for b in win if engine.is_limit_up(b, lim))
            if nzt < 2:
                continue
            base = bs[bi - 1]["c"]
            if not base:
                continue
            g5 = win[-1]["c"] / base - 1
            pre20 = base / (bs[bi - 21]["c"] or base) - 1
            if g5 < 0.15 or pre20 >= 0.10:
                continue
            hi60 = max(b["h"] for b in bs[max(0, bi - 60):bi]) or base
            st = u.stocks.get(code, {})
            inds = [b[1] for b in (code2boards or {}).get(code, []) if b[2] == "industry"]
            samples.append({
                "code": code, "name": st.get("name"), "trough": tr, "lag": lag,
                "date": date, "g5": round(g5 * 100, 1), "nzt": nzt,
                "price": round(base, 2),
                "fmv": round((st.get("float_mv") or 0) / 1e8, 1),
                "pre20": round(pre20 * 100, 1),
                "dd60": round((base / hi60 - 1) * 100, 1),
                "ind": inds[0] if inds else "",
            })

    def med(key, rows):
        xs = sorted(r[key] for r in rows)
        return xs[len(xs) // 2] if xs else None

    style = None
    if samples:
        n = len(samples)
        cnt = {}
        for s in samples:
            if s["ind"]:
                cnt[s["ind"]] = cnt.get(s["ind"], 0) + 1
        style = {
            "n": n,
            "price_median": med("price", samples),
            "fmv_median": med("fmv", samples),
            "pre20_median": med("pre20", samples),
            "dd60_median": med("dd60", samples),
            "share_low_price": round(sum(1 for s in samples if s["price"] < 10) / n, 3),
            "share_small": round(sum(1 for s in samples if s["fmv"] < 60) / n, 3),
            "share_oversold": round(sum(1 for s in samples if s["dd60"] <= -20) / n, 3),
            "lag_hist": {str(l): sum(1 for s in samples if s["lag"] == l)
                         for l in range(1, max_lag + 1)},
            "top_inds": [{"name": k, "n": v} for k, v in
                         sorted(cnt.items(), key=lambda x: -x[1])[:12]],
        }

    # 轮动序列：每个冷谷的头号领涨股
    leaders = []
    for sp in spells:
        pool = [s for s in samples if s["trough"] == sp["trough"]]
        if not pool:
            continue
        best = max(pool, key=lambda x: x["g5"])
        r = _first_repair(series, sp["trough"])
        leaders.append({
            "trough_date": sp["trough_date"], "min_hp": sp["min_hp"], "spell_len": sp["len"],
            "repair_lag": (r - sp["trough"]) if r is not None else None,
            "code": best["code"], "name": best["name"], "ind": best["ind"],
            "start": best["date"], "lag": best["lag"], "g5": best["g5"],
            "nzt": best["nzt"], "price": best["price"], "fmv": best["fmv"],
            "pre20": best["pre20"], "dd60": best["dd60"],
        })

    inds = [l["ind"] for l in leaders if l["ind"]]
    repeat = sum(1 for a, b in zip(inds, inds[1:]) if a == b)
    rotation = {
        "leaders": leaders,
        "sequence": inds,
        "repeat_pairs": repeat,
        "pairs": max(0, len(inds) - 1),
        "repeat_rate": round(repeat / max(1, len(inds) - 1), 3),
        "switch_rate": round(1 - repeat / max(1, len(inds) - 1), 3),
        "last_inds": inds[-3:][::-1],
    }
    return {"style": style, "rotation": rotation, "samples_n": len(samples)}


# ============================================================== 4. 当下符合冷后风格的候选
def style_candidates(u, date, style, rotation, code2boards, topn=12):
    """按实测风格画像，筛当下最像『下一个冷后领涨股』的标的。"""
    if not style:
        return []
    hot = set(x["name"] for x in style.get("top_inds", [])[:10])
    just_ran = set(rotation.get("last_inds", [])[:2])   # 刚走过的方向（88%不重复）
    out = []
    for code, bs in u.bars.items():
        lim = u.lim.get(code)
        if lim is None or lim > 11:
            continue
        hist = [b for b in bs if b["d"] <= date]
        if len(hist) < 62:
            continue
        cur = hist[-1]
        price = cur["c"]
        if not price or price <= 0:
            continue
        st = u.stocks.get(code, {})
        name = st.get("name") or ""
        if "ST" in name.upper():
            continue
        fmv = (st.get("float_mv") or 0) / 1e8
        if fmv < 15 or fmv > 200:
            continue
        # 已启动的排除：当日连板 >=2，或近3日涨幅过大
        streak = u.streak.get(code, {}).get(date, 0)
        if streak >= 2:
            continue
        g3 = price / (hist[-4]["c"] or price) - 1
        if g3 > 0.18:
            continue
        pre20 = price / (hist[-21]["c"] or price) - 1
        if pre20 >= 0.12:
            continue
        hi60 = max(b["h"] for b in hist[-61:-1]) or price
        dd60 = price / hi60 - 1
        if dd60 > -0.10:
            continue
        v5 = engine.mean([b["v"] or 0 for b in hist[-6:-1]]) or 1
        vr = (cur["v"] or 0) / v5
        inds = [b[1] for b in (code2boards or {}).get(code, []) if b[2] == "industry"]
        ind = inds[0] if inds else ""

        sc, why = 0.0, []
        if price < 5:
            sc += 2.5; why.append("低价%.2f元" % price)
        elif price < 10:
            sc += 2.0; why.append("低价%.2f元" % price)
        elif price < 20:
            sc += 0.8
        if 30 <= fmv <= 60:
            sc += 2.5; why.append("流通%.0f亿(主流盘)" % fmv)
        elif 15 <= fmv < 30 or 60 < fmv <= 120:
            sc += 1.6; why.append("流通%.0f亿" % fmv)
        if dd60 <= -0.30:
            sc += 2.2; why.append("距60日高%.0f%%(深度超跌)" % (dd60 * 100))
        elif dd60 <= -0.20:
            sc += 1.6; why.append("距60日高%.0f%%(超跌)" % (dd60 * 100))
        elif dd60 <= -0.10:
            sc += 0.8
        if pre20 <= -0.05:
            sc += 1.5; why.append("前20日%+.0f%%(充分调整)" % (pre20 * 100))
        elif pre20 <= 0.05:
            sc += 1.0; why.append("前20日横盘")
        if vr >= 1.6:
            sc += 1.6; why.append("量比%.1f(资金异动)" % vr)
        elif vr >= 1.2:
            sc += 0.9; why.append("量比%.1f" % vr)
        if streak == 1:
            sc += 1.2; why.append("昨日涨停(启动信号)")
        if ind and ind in hot:
            sc += 1.5; why.append("%s(高频出妖方向)" % ind)
        if ind and ind in just_ran:
            sc -= 1.8; why.append("%s刚走过(轮动回避)" % ind)

        if sc < 4.5:
            continue
        out.append({
            "code": code, "name": name, "score": round(sc, 2),
            "price": round(price, 2), "fmv": round(fmv, 1),
            "pre20": round(pre20 * 100, 1), "dd60": round(dd60 * 100, 1),
            "vol_ratio": round(vr, 2), "streak": streak, "ind": ind,
            "why": "，".join(why[:4]),
        })
    out.sort(key=lambda x: -x["score"])
    return out[:topn]


# ============================================================== 5. 汇总
def analyze(u, date, code2boards=None, n=140, emo=None):
    """主入口：输出当下冷度、修复预判、风格轮动规律、候选股。"""
    series = heat_series(u, n=n, emo=emo)
    if not series:
        return None
    spells = cold_spells(series)
    rs = repair_stats(series, spells)
    ls = launch_scan(u, series, spells, code2boards or {})

    # 当下状态
    cur = series[-1]
    hp = cur["hp"]
    in_cold = hp <= COLD_TH
    cold_days = 0
    for r in reversed(series):
        if r["hp"] <= COLD_TH:
            cold_days += 1
        else:
            break
    # 距最近冷谷多少天
    last_trough = None
    for sp in spells:
        last_trough = sp
    since_trough = None
    if last_trough:
        since_trough = (len(series) - 1) - last_trough["trough"]

    depth_key = "爆冷" if hp < DEEP_TH else ("深冷" if hp < 20 else "偏冷")
    bucket = (rs["by_depth"] or {}).get(depth_key) or rs["overall"]

    forecast = None
    if in_cold and bucket:
        forecast = {
            "state": "冷中待修复",
            "depth": depth_key,
            "expect": "T+%d" % bucket["median"],
            "p_t1": bucket["p_t1"], "p_t2": bucket["p_t2"],
            "p_t3": bucket["p_t3"], "p_t4p": bucket["p_t4p"],
            "cum_t2": bucket["cum_t2"], "cum_t3": bucket["cum_t3"],
            "n": bucket["n"],
            "note": ("同等冷度历史 %d 次样本：隔天(T+1)修复概率 %.0f%%，两日内累计 %.0f%%，"
                     "三日内累计 %.0f%%。" % (bucket["n"], bucket["p_t1"] * 100,
                                          bucket["cum_t2"] * 100, bucket["cum_t3"] * 100)),
        }
    elif since_trough is not None and since_trough <= 3 and bucket:
        forecast = {
            "state": "冷后启动窗口",
            "depth": depth_key,
            "expect": "T+%d 窗口内" % since_trough,
            "p_t1": bucket["p_t1"], "p_t2": bucket["p_t2"],
            "p_t3": bucket["p_t3"], "p_t4p": bucket["p_t4p"],
            "cum_t2": bucket["cum_t2"], "cum_t3": bucket["cum_t3"],
            "n": bucket["n"],
            "note": ("距最近冷谷 %s 已 T+%d，正处历史新方向最密集启动窗口（T+1 占比最高）。"
                     % (last_trough["trough_date"], since_trough)),
        }

    cands = style_candidates(u, date, ls["style"], ls["rotation"], code2boards or {})

    return {
        "date": date,
        "today": cur,
        "in_cold": in_cold,
        "cold_days": cold_days,
        "since_trough": since_trough,
        "last_trough": (last_trough["trough_date"] if last_trough else None),
        "last_trough_hp": (last_trough["min_hp"] if last_trough else None),
        "series": series[-60:],
        "spells_n": len(spells),
        "repair": rs,
        "forecast": forecast,
        "style": ls["style"],
        "rotation": ls["rotation"],
        "candidates": cands,
    }


def summary_lines(cw):
    """给推送用的紧凑文字摘要"""
    if not cw:
        return []
    t = cw["today"]
    out = ["市场热度位 %.1f（%s）｜涨停%d 连板%d 最高%d板 红盘%.0f%%"
           % (t["hp"], t["level"], t["zt"], t["lb"], t["max_lb"], t["up_ratio"])]
    f = cw.get("forecast")
    if f:
        out.append("修复预判：%s → 预计 %s（T+1 %.0f%% / 两日内 %.0f%% / 三日内 %.0f%%）"
                   % (f["state"], f["expect"], f["p_t1"] * 100, f["cum_t2"] * 100, f["cum_t3"] * 100))
    st = cw.get("style")
    if st:
        out.append("冷后领涨风格：启动价中位%.1f元、流通%.0f亿、前20日%+.0f%%、距60日高%+.0f%%；低价占%.0f%% 超跌占%.0f%%"
                   % (st["price_median"], st["fmv_median"], st["pre20_median"],
                      st["dd60_median"], st["share_low_price"] * 100, st["share_oversold"] * 100))
    ro = cw.get("rotation") or {}
    if ro.get("pairs"):
        out.append("方向轮动：相邻两次冷后换方向概率 %.0f%%；最近领涨方向 %s"
                   % (ro["switch_rate"] * 100, "→".join(ro.get("last_inds") or []) or "—"))
    return out
