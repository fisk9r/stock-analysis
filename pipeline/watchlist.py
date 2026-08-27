# -*- coding: utf-8 -*-
"""关注股雷达：自选/持仓观察股的每日状态与异动信号

数据源：
- config/notify.json 的 "watch" 字段（["600519", {"code":"000001","name":"平安"}] 均可）
- config/holdings.json 中 watch==true 的持仓

信号（纯日K可判）：
- 涨停 / 跌停 / 炸板（触板未封）
- 放量突破 20 日高（量比≥1.5 且收创新高）
- 跌破 MA20 且放量（趋势破位警示）
- 多头排列（C>MA20>MA60）/ 空头排列
- 近 5 日首次翻多 / 首次翻空

urgent = 涨停/跌停/炸板/破位 —— 值得立刻知道；其余进收盘摘要。

纯标准库。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store

URGENT = ("涨停", "跌停", "炸板", "趋势破位")


def load_watch_codes():
    """合并 notify.json watch + holdings.json watch==true + config/watch.json（网页管理写回）。
    返回 (codes, names, added) —— added: {code: "2026-08-01"} 用户显式标注的关注锚点日（可选）。"""
    codes, names, added = [], {}, {}
    root = store.ROOT
    # 1) 网页管理的关注池（WATCH_JSON 密钥 → CI 还原为 config/watch.json）
    wp = os.path.join(root, "config", "watch.json")
    try:
        wj = json.load(open(wp, encoding="utf-8")) or {}
        for x in (wj.get("watch") or []):
            if isinstance(x, str) and x.strip():
                c = x.strip().zfill(6)
                if c not in names:
                    codes.append(c); names[c] = ""
            elif isinstance(x, dict) and x.get("code"):
                c = str(x["code"]).zfill(6)
                if c not in names:
                    codes.append(c); names[c] = x.get("name") or ""
                if x.get("added"):
                    added[c] = str(x["added"]).strip()
    except Exception:
        pass
    np = os.path.join(root, "config", "notify.json")
    try:
        w = (json.load(open(np, encoding="utf-8")) or {}).get("watch") or []
        for x in w:
            if isinstance(x, str) and x.strip():
                c = x.strip().zfill(6)
                if c not in names:
                    codes.append(c)
                    names[c] = ""
            elif isinstance(x, dict) and x.get("code"):
                c = str(x["code"]).zfill(6)
                if c not in names:
                    codes.append(c)
                    names[c] = x.get("name") or ""
                if x.get("added"):
                    added[c] = str(x["added"]).strip()
    except Exception:
        pass
    hp = os.path.join(root, "config", "holdings.json")
    try:
        h = json.load(open(hp, encoding="utf-8")) or {}
        for p in h.get("positions") or []:
            if p.get("watch") and p.get("code"):
                c = str(p["code"]).zfill(6)
                if c not in names:
                    codes.append(c)
                    names[c] = p.get("name") or ""
                elif not names[c]:
                    names[c] = p.get("name") or ""
                # 持仓观察股的建仓日可作为关注锚点；未标注 added 时回退到该日期
                if p.get("date"):
                    added.setdefault(c, str(p["date"]).strip())
    except Exception:
        pass
    return codes, names, added


def scan(u, date, con=None, risk_flags=None):
    """关注股雷达：除当日信号外，额外计算「关注以来累计」（自关注锚点日至今）。

    锚点日优先用用户显式标注的 added；否则用持久化的首次进入关注池日期
    （store.watch_first_seen，跨构建保留，最贴近「关注时候」）；
    再不行回退到最近 60 根K线起点。

    返回每个标的：今日价/涨幅 + since_added{锚点日, 持有天数, 累计涨跌幅,
    区间最高/最低, 最大回撤, 期间涨停/跌停/炸板次数} + 当日信号。
    """
    codes, extra_names, added = load_watch_codes()
    if not codes:
        return None
    if con is None:
        con = store.connect()
    # 持久化首见日（跨构建保留，作为「关注时候」锚点）
    first_seen_db = store.watch_first_seen(con, date, codes)
    items = []
    for c in codes:
        bs = [b for b in u.bars.get(c, []) if b["d"] <= date]
        st = u.stocks.get(c, {})
        name = extra_names.get(c) or st.get("name") or ""
        n = len(bs)
        if n < 25 or not bs[-1]["c"]:
            items.append({"code": c, "name": name, "no_data": True,
                          "note": "K线不足" if n < 25 else ""})
            continue
        cur = bs[-1]
        close = float(cur["c"])
        closes = [float(b["c"]) for b in bs]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60 if n >= 60 else None
        vols = [float(b.get("v") or 0) for b in bs]
        v5 = sum(vols[-6:-1]) / 5
        vr = (cur.get("v") or 0) / v5 if v5 else 0
        hi20 = max(b["h"] for b in bs[-21:-1]) if n >= 22 else None

        # ---- 关注以来累计 ----
        anchor = added.get(c) or first_seen_db.get(c) or None
        since = []
        if anchor:
            since = [b for b in bs if b["d"] >= anchor]
        if len(since) < 2:
            since = bs[-60:]
            anchor = since[0]["d"]
        n_since = len(since)
        first_c = float(since[0]["c"])
        # 区间最高/最低/最大回撤
        run_max = since[0]["c"]
        max_dd = 0.0
        for b in since:
            cc = float(b["c"])
            run_max = max(run_max, cc)
            if run_max > 0:
                dd = (cc / run_max - 1) * 100
                if dd < max_dd:
                    max_dd = dd
        hi_s = max(float(b["h"]) for b in since)
        lo_s = min(float(b["l"]) for b in since)
        cum_pct = (close / first_c - 1) * 100 if first_c else 0.0
        # 期间涨停/跌停/炸板次数
        n_zt = sum(1 for b in since if c in u.zt.get(b["d"], ()))
        n_dt = sum(1 for b in since if c in u.dt.get(b["d"], ()))
        n_zb = sum(1 for b in since if c in u.zhaban.get(b["d"], ()))
        since_added = {
            "anchor": anchor, "days": n_since,
            "pct": round(cum_pct, 2),
            "hi": round(hi_s, 2), "lo": round(lo_s, 2),
            "max_dd": round(max_dd, 2),
            "n_zt": n_zt, "n_dt": n_dt, "n_zb": n_zb,
        }

        sig = []
        if c in u.zt.get(date, ()):
            sig.append("涨停")
        elif c in u.dt.get(date, ()):
            sig.append("跌停")
        elif c in u.zhaban.get(date, ()):
            sig.append("炸板")
        bull = close > ma20 and (ma60 is None or ma20 > ma60)
        bear = close < ma20 and (ma60 is None or ma20 < ma60)
        if hi20 and close > hi20 and vr >= 1.5:
            sig.append("放量突破20日高")
        prev_ma20 = sum(closes[-21:-1]) / 20 if n >= 21 else ma20
        if closes[-2] >= prev_ma20 > close and vr >= 1.2:
            sig.append("趋势破位")
        if bull and not bear:
            sig.append("多头排列")
        elif bear and not bull:
            sig.append("空头排列")

        items.append({
            "code": c, "name": name,
            "close": round(close, 2),
            "pct": round(cur.get("pct") or 0, 2),
            "vol_ratio": round(vr, 2),
            "first_seen": anchor,
            "since_added": since_added,
            "risk_flag": (risk_flags or {}).get(c),
            "signals": sig,
            "urgent": any(s in URGENT for s in sig),
        })

    items.sort(key=lambda x: (not x.get("urgent", False), -(x.get("pct") or 0)))
    return {
        "date": date,
        "n": len(items),
        "items": items[:20],
        "alert_n": sum(1 for x in items if x.get("urgent")),
    }


def summary_lines(wl):
    """收盘推送用紧凑摘要；明确区分『今日』与『关注以来』。"""
    if not wl:
        return []
    out = []
    its = wl.get("items") or []
    urg = [x for x in its if x.get("urgent")]
    if urg:
        out.append("⚠️ " + "；".join(
            "%s %s(今%+.1f%%·%s)" % (x["name"], "/".join(x["signals"][:2]),
                                     x.get("pct", 0), "急讯")
            for x in urg[:4]))
    cum = [x for x in its if not x.get("urgent") and x.get("since_added")]
    if cum:
        out.append("关注以来：" + "；".join(
            "%s 持有%s天 %+.%1f%%（高%s/低%s%s）"
            % (x["name"], x["since_added"]["days"], x["since_added"]["pct"],
               x["since_added"]["hi"], x["since_added"]["lo"],
               ("·%d板" % x["since_added"]["n_zt"] if x["since_added"]["n_zt"] else ""))
            for x in cum[:6]))
    if not out:
        out.append("关注池 %d 只今日无异动" % wl.get("n", 0))
    return out
