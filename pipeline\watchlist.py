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
    """合并 notify.json watch + holdings.json watch==true + config/watch.json（网页管理写回）"""
    codes, names = [], {}
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
    except Exception:
        pass
    return codes, names


def scan(u, date):
    codes, extra_names = load_watch_codes()
    if not codes:
        return None
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
        closes = [b["c"] for b in bs]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60 if n >= 60 else None
        vols = [b.get("v") or 0 for b in bs]
        v5 = sum(vols[-6:-1]) / 5
        vr = (cur.get("v") or 0) / v5 if v5 else 0
        hi20 = max(b["h"] for b in bs[-21:-1]) if n >= 22 else None

        sig = []
        if c in u.zt.get(date, ()):
            sig.append("涨停")
        elif c in u.dt.get(date, ()):
            sig.append("跌停")
        elif c in u.zhaban.get(date, ()):
            sig.append("炸板")
        bull = cur["c"] > ma20 and (ma60 is None or ma20 > ma60)
        bear = cur["c"] < ma20 and (ma60 is None or ma20 < ma60)
        if hi20 and cur["c"] > hi20 and vr >= 1.5:
            sig.append("放量突破20日高")
        prev_ma20 = sum(closes[-21:-1]) / 20 if n >= 21 else ma20
        if closes[-2] >= prev_ma20 > cur["c"] and vr >= 1.2:
            sig.append("趋势破位")
        if bull and not bear:
            sig.append("多头排列")
        elif bear and not bull:
            sig.append("空头排列")

        items.append({
            "code": c, "name": name,
            "close": round(cur["c"], 2),
            "pct": round(cur.get("pct") or 0, 2),
            "vol_ratio": round(vr, 2),
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
    """收盘推送用紧凑摘要"""
    if not wl:
        return []
    out = []
    its = wl.get("items") or []
    urg = [x for x in its if x.get("urgent")]
    if urg:
        out.append("⚠️ " + "；".join(
            "%s %s(%+.1f%%·%s)" % (x["name"], "/".join(x["signals"][:2]), x.get("pct", 0), "急讯")
            for x in urg[:4]))
    normal = [x for x in its if not x.get("urgent")][:6]
    if normal:
        out.append("关注股速览：" + "、".join(
            "%s%+.1f%%" % (x["name"], x.get("pct", 0)) for x in normal))
    if not out:
        out.append("关注池 %d 只今日无异动" % wl.get("n", 0))
    return out
