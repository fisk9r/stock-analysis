# -*- coding: utf-8 -*-
"""游资席位画像：识别当日龙虎榜上的知名席位，并给出历史 T+1 跟随胜率。

数据源：RPT_OPERATEDEPT_TRADE_DETAILS（2026-08-26 实证可用）——营业部级买卖明细，
含 OPERATEDEPT_NAME / SECURITY_CODE / ACT_BUY / ACT_SELL / NET_AMT / CHANGE_RATE。

胜率口径：signal_backtest.t1_stats —— 该席位上榜日买入后，个股 T+1 收盘相对
T 日收盘的涨跌（用本地 market.db 的 bars 计算，无需额外接口）。
样本 <8 次不出示胜率（避免小样本误导）。

注意：「坊间归因」为市场流传的席位-游资对应关系，仅供参考，不构成事实认定。
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emdc

REPORT = "RPT_OPERATEDEPT_TRADE_DETAILS"

# 知名席位映射：(营业部名关键词, 画像标签)。命中任一关键词即打标。
FAMOUS = [
    ("拉萨", "东财拉萨·散户集中营"),
    ("东环路第二", "东财拉萨·散户集中营"),
    ("中国银河证券绍兴", "绍兴·赵老哥(坊间)"),
    ("华鑫证券上海分公司", "华鑫上海·炒股养家(坊间)"),
    ("国盛证券宁波桑田路", "宁波桑田路·方新侠(坊间)"),
    ("财通证券杭州上塘路", "杭州上塘路·顶级接力"),
    ("中信证券上海溧阳路", "上海溧阳路游资"),
    ("上海江苏路", "江苏路·章盟主(坊间)"),
    ("华泰证券深圳益田路荣超", "深圳益田路·深圳帮"),
    ("宁波解放北", "宁波解放北路·敢死队"),
    ("中泰证券深圳欢乐海岸", "深圳欢乐海岸"),
    ("银河证券大连黄河路", "大连黄河路"),
    ("南京太平南路", "南京太平南路"),
]


def _fnum(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def scan(date, max_pages=6):
    """抓当日席位明细，筛出知名席位命中行。"""
    hits = []
    seen = set()
    for page in range(1, max_pages + 1):
        rows = emdc.get(REPORT, columns="ALL", flt="(TRADE_DATE='%s')" % date,
                        page_size=500, sort="NET_AMT", page=page)
        if not rows:
            break
        for r in rows:
            dept = r.get("OPERATEDEPT_NAME") or ""
            label = None
            for kw, lb in FAMOUS:
                if kw in dept:
                    label = lb
                    break
            if not label:
                continue
            code = str(r.get("SECURITY_CODE") or "")
            key = (dept, code)
            if not code or key in seen:
                continue
            seen.add(key)
            net = _fnum(r.get("NET_AMT"))
            buy = _fnum(r.get("ACT_BUY"))
            sell = _fnum(r.get("ACT_SELL"))
            # 只关心真金白银的净买/大幅净卖
            if abs(net) < 3e7:      # 净额 <3000万 忽略
                continue
            hits.append({
                "dept_code": r.get("OPERATEDEPT_CODE") or dept,
                "label": label,
                "seat_short": dept.split("股份有限公司")[-1][:14],
                "code": code,
                "name": r.get("SECURITY_NAME_ABBR") or "",
                "net_yi": round(net / 1e8, 2),
                "act_buy_yi": round(buy / 1e8, 2),
                "act_sell_yi": round(sell / 1e8, 2),
                "chg": round(_fnum(r.get("CHANGE_RATE")), 2),
            })
        if len(rows) < 500:
            break
    if not hits:
        return None
    # 同一标签合并展示，按 |net| 排序取 TOP
    hits.sort(key=lambda x: -abs(x["net_yi"]))
    return {"date": date, "n_hits": len(hits), "hits": hits[:14]}


def win_rates(con, min_samples=8):
    """按画像标签统计历史 T+1 胜率（基于已积累的 seat_daily × 本地日K）。"""
    import store
    import signal_backtest
    rows = store.seats_history(con, days=120)
    if not rows:
        return {}
    by_label = defaultdict(list)
    for date, _dc, label, code, _nm, net_yi in rows:
        if label and (net_yi or 0) > 0:     # 只统计净买入跟随
            by_label[label].append((date, code))
    out = {}
    for label, events in by_label.items():
        st = signal_backtest.t1_stats(con, events)
        if st and st["n"] >= min_samples:
            out[label] = st
    return out


def summary_lines(r, stats=None):
    if not r:
        return []
    stats = stats or r.get("stats") or {}
    out = []
    top = sorted(r.get("hits") or [], key=lambda x: -x["net_yi"])[:4]
    for h in top:
        wr = (stats.get(h["label"]) or {})
        tail = " · 跟随胜率%s%%(%d次)" % (wr["win_rate"], wr["n"]) if wr else ""
        out.append("- %s【%s】净买 %s亿 → %s%s"
                   % (h["name"], h["label"], h["net_yi"],
                      ("%+.1f%%" % h["chg"]), tail))
    if not out:
        out = ["龙虎榜：今日无知名席位显著动作"]
    # 2026-08-30 回避席位提示（样本回填后胜率可用）：低胜率知名席位上榜 = 负期望跟随
    bad = [(lb, st) for lb, st in sorted(stats.items(), key=lambda kv: kv[1].get("win_rate", 0))
           if st.get("win_rate", 100) < 40 and st.get("n", 0) >= 20]
    if bad:
        out.append("⚠ 回避席位：%s"
                   % "、".join("%s（胜率%.0f%%/%d次，跟随负期望）" % (lb, st["win_rate"], st["n"])
                               for lb, st in bad[:3]))
    return out
