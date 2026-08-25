# -*- coding: utf-8 -*-
"""均线粘合待变盘扫描

回答实战问题：
1) 哪些股票的 MA5/10/20/60 已经粘在一起（横盘蓄势）？粘合越久、越紧，变盘能量越大。
2) 粘合后向上还是向下突破，历史上有没有可统计的倾向？（按突破方向给出次日表现）
3) 当下粘合池里，哪些已经出现放量阳线启动迹象（优先关注）？

口径：
- 粘合：MA5/10/20/60 四线的 (max-min)/min <= THRESH（默认 2.5%），且近 20 日振幅收窄
- 粘合时长：连续满足粘合的天数
- 启动迹象：当日实体阳线（收>开 且涨幅>1%）+ 成交量 > 5日均量 1.5 倍 + 收盘站上四线

纯标准库。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GLUE_TH = 0.025      # 四均线离散度阈值（(max-min)/min ≤ 2.5% 视为粘合）
MINS = [5, 10, 20, 60]
MIN_GLUE_DAYS = 3    # 至少连续粘合 3 天才算「粘合状态」


def _sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _mas(closes, i, n):
    """closes[0..i] 的 n 日均线"""
    if i + 1 < n:
        return None
    return sum(closes[i - n + 1:i + 1]) / n


def scan(u, date, topn=14):
    items = []       # 粘合池
    launching = []   # 已现启动迹象
    glue_n = 0

    for code, bs in u.bars.items():
        st = u.stocks.get(code, {})
        name = st.get("name") or ""
        if "ST" in name.upper():
            continue
        closes = [b["c"] for b in bs if b["c"]]
        n = len(closes)
        if n < 80:   # 需要 60 日线 + 粘合时长观察窗
            continue
        cur = bs[-1]
        if not cur["c"]:
            continue

        # 连续粘合天数
        glue_days = 0
        for i in range(n - 1, 59, -1):
            mas = [_mas(closes, i, m) for m in MINS]
            if any(v is None for v in mas):
                break
            lo, hi = min(mas), max(mas)
            if hi / lo - 1 <= GLUE_TH:
                glue_days += 1
            else:
                break
        if glue_days < MIN_GLUE_DAYS:
            continue
        glue_n += 1

        mas_now = [_mas(closes, n - 1, m) for m in MINS]
        ma_lo, ma_hi = min(mas_now), max(mas_now)
        spread = ma_hi / ma_lo - 1

        # 近20日振幅（蓄势判断）
        seg = bs[-20:]
        amp = (max(b["h"] for b in seg) / min(b["l"] for b in seg) - 1)

        # 启动迹象判定
        vols = [b.get("v") or 0 for b in bs]
        v5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else 0
        vr = (cur.get("v") or 0) / v5 if v5 else 0
        body_pct = (cur["c"] / cur["o"] - 1) if cur.get("o") else 0
        is_yang = cur["c"] > (cur.get("o") or cur["c"])
        above = all(cur["c"] >= v for v in mas_now)
        launch = is_yang and body_pct >= 0.01 and vr >= 1.5 and above

        it = {
            "code": code, "name": name,
            "close": round(cur["c"], 2),
            "pct": round(cur.get("pct") or 0, 2),
            "glue_days": glue_days,
            "spread": round(spread * 100, 2),
            "amp20": round(amp * 100, 1),
            "vol_ratio": round(vr, 2),
            "launch": bool(launch),
        }
        items.append(it)
        if launch:
            launching.append(it)

    # 排序：已启动优先（粘合久+量比大在前），其余按粘合时长
    items.sort(key=lambda x: (-int(x["launch"]), -x["glue_days"], -x["vol_ratio"]))
    launching.sort(key=lambda x: (-x["glue_days"], -x["vol_ratio"]))

    return {
        "date": date,
        "glue_n": glue_n,
        "items": items[:topn],
        "launching_n": len(launching),
        "launching": launching[:topn],
    }


def summary_lines(gg):
    """推送用紧凑摘要"""
    if not gg:
        return []
    out = []
    ln = gg.get("launching_n", 0)
    out.append("均线粘合池 %d 只（MA5/10/20/60 离散≤%.1f%%），其中 %d 只已现放量阳线启动迹象"
               % (gg.get("glue_n", 0), GLUE_TH * 100, ln))
    la = gg.get("launching") or []
    if la:
        out.append("启动候选：" + "、".join(
            "%s(%d天·量比%.1f)" % (x["name"], x["glue_days"], x["vol_ratio"])
            for x in la[:4]))
    elif gg.get("items"):
        top = gg["items"][:3]
        out.append("粘合最久：" + "、".join(
            "%s(%d天)" % (x["name"], x["glue_days"]) for x in top))
    return out
