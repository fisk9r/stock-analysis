# -*- coding: utf-8 -*-
"""题材主线识别：对当日涨停股的概念/行业标签做聚类，判定当日主线/支线，
并结合历史 theme_daily 计算主线已持续天数与退潮预警。

输入：build 传入的 limit_ups 列表（每只含 concepts / industry 字段）。
输出结构用于前端「题材主线」卡 + 推送 + 情绪分维度。
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store


# 题材热度归一权重：概念标签视为强题材信号，行业作为弱补充（避免“医药”这种大类稀释）
CONCEPT_WEIGHT = 1.0
INDUSTRY_WEIGHT = 0.4

# 元标签/非题材噪声：融资融券、昨日涨停、机构重仓等是状态描述而非题材，须排除
STOPLIST = {
    "融资融券", "转融券标的", "昨日涨停", "昨日触板", "昨日跌停", "机构重仓",
    "QFII重仓", "深股通", "沪股通", "标普道琼斯A股", "MSCI概念", "富时罗素",
    "证金持股", "养老金持股", "国家队", "中央汇金", "沪伦通", "注册制", "ST股",
    "可转债", "股权激励", "员工持股", "业绩预增", "高送转", "摘帽", "融资标的",
    "融券标的", "深成500", "上证180", "上证50", "沪深300", "中证500", "中证1000",
    "大盘", "中盘", "小盘", "破净股", "低价股", "昨日连板", "昨日首板", "新股与次新股",
}


def scan(date, limit_ups):
    if not limit_ups:
        return None
    cnt = Counter()
    n = 0
    for x in limit_ups:
        n += 1
        for c in (x.get("concepts") or []):
            if c and c not in ("--", "无") and c not in STOPLIST:
                cnt[c] += CONCEPT_WEIGHT
        ind = x.get("industry")
        if ind and ind not in ("--", "无") and ind not in STOPLIST:
            cnt[ind] += INDUSTRY_WEIGHT
    if not cnt:
        return None
    ranked = [(t, round(v, 1)) for t, v in cnt.most_common(10) if v >= 1.0]
    if not ranked:
        return None
    main_theme, main_n = ranked[0]
    return {
        "date": date,
        "n_stocks": n,
        "main_theme": main_theme,
        "main_n": main_n,
        "sub_themes": [{"theme": t, "n": v} for t, v in ranked[1:4]],
        "all": [{"theme": t, "n": v} for t, v in ranked],
    }


def persist(con, date, r):
    if not r:
        return
    counts = {t["theme"]: t["n"] for t in r.get("all", [])}
    store.upsert_themes(con, date, counts)


def theme_signal(con, min_days=3):
    """读 theme_daily 历史，判定主线连续天数 + 退潮预警。"""
    rows = con.execute(
        "SELECT date,theme,n FROM theme_daily ORDER BY date").fetchall()
    if not rows:
        return None
    # 每日的“主线”（当日 n 最大者）
    by_date = {}
    for d, t, n in rows:
        by_date.setdefault(d, []).append((t, n))
    dates = sorted(by_date)
    daily_main = []
    for d in dates:
        top = max(by_date[d], key=lambda x: x[1])
        daily_main.append((d, top[0], top[1]))
    cur_main = daily_main[-1][1]
    # 连续作为主线的天数（从末尾往前数）
    streak = 0
    for i in range(len(daily_main) - 1, -1, -1):
        if daily_main[i][1] == cur_main:
            streak += 1
        else:
            break
    # 退潮预警：近 3 日主线强度均值 < 此前 3 日
    def avg_n(window):
        if not window:
            return 0
        return sum(x[2] for x in window) / len(window)
    recent = daily_main[-3:]
    prev = daily_main[-6:-3]
    recent_avg = avg_n(recent)
    prev_avg = avg_n(prev)
    pullback = (prev_avg > 0 and recent_avg < prev_avg * 0.7)
    # 主线切换（末尾主线与再之前不同）
    switched = (len(daily_main) >= 4 and daily_main[-2][1] != cur_main
                and daily_main[-3][1] != cur_main)
    return {
        "main_theme": cur_main,
        "streak": streak,
        "recent_avg": round(recent_avg, 1),
        "prev_avg": round(prev_avg, 1),
        "pullback": pullback,
        "switched": switched,
        "verdict": ("退潮预警" if pullback else
                    ("主线切换" if switched else
                     ("主线强化" if streak >= min_days and recent_avg >= prev_avg else "主线延续"))),
    }


def summary_lines(r, sig=None):
    if not r:
        return []
    sig = sig or r.get("signal")
    out = ["题材主线：%s（%d 只涨停贡献，主线%s）"
           % (r["main_theme"], int(r["main_n"]),
              ("持续%d日" % sig["streak"]) if sig and sig.get("streak", 0) >= 2 else "初现")]
    for s in r.get("sub_themes", [])[:3]:
        out.append("- 支线：%s（%d）" % (s["theme"], int(s["n"])))
    if sig and sig.get("verdict") not in ("主线延续", None):
        out.append("- 主线状态：%s" % sig["verdict"])
    return out
