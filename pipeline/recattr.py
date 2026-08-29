# -*- coding: utf-8 -*-
"""推荐归因：基于 rec_picks 特征扩列（2026-08-29）的多维胜率归因。

回答三个问题（每日 build 自动刷新，纯本地零网络）：
  1. st=2 胜率为什么异常？——按 next_open_gap 分桶拆解（已确诊：弱高开接二板），
     并持续监控 2026-08-29 修复（st=2 高开门槛收紧到 5%）后的实际执行收益。
  2. 新特征列（sector_strength/quality/turn/auction_pattern）样本积累到多少了？
     各特征桶胜率差异是否已具备统计意义（≥10 条才出结论）。
  3. 盘中路径（next_max_runup/drawdown）：亏损推荐票是「冲高没落袋」还是「全天阴跌」？
     验证落袋纪律（+2% 冲高卖出）的理论挽回空间。

数据源：store.rec_picks 全表（含 2026-08-29 扩列的特征列）。
输出挂在 data["rec_attr"]，前端「推荐池」视图展示；推送用于复盘段。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_MIN_BUCKET = 10  # 分桶最少样本：低于此数不产生结论（避免小样本噪声）


def _bucket_gap(g):
    if g is None:
        return None
    if g > 5:
        return ">5"
    if g >= 2:
        return "2-5"
    if g >= -2:
        return "-2~2"
    return "<-2"


def _agg(rows):
    """rows: [(win 0/1, next_pct, runup, dd)] → 统计 dict。"""
    n = len(rows)
    if not n:
        return None
    wins = sum(r[0] for r in rows)
    avg = sum(r[1] or 0 for r in rows) / n
    runups = [r[2] for r in rows if r[2] is not None]
    dds = [r[3] for r in rows if r[3] is not None]
    return {
        "n": n,
        "win_rate": round(wins * 100.0 / n, 1),
        "avg_pct": round(avg, 2),
        "avg_runup": round(sum(runups) / len(runups), 2) if runups else None,
        "avg_dd": round(sum(dds) / len(dds), 2) if dds else None,
    }


def build(con, limit=2000):
    """返回归因 dict；样本不足时字段留空。"""
    rows = con.execute(
        "SELECT streak, tag, next_pct, next_open_gap, sector_strength, quality, "
        "turn, auction_pattern, next_max_runup, next_max_drawdown "
        "FROM rec_picks WHERE next_pct IS NOT NULL "
        "ORDER BY date DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return None

    # ---- 1) st=2 归因持续监控（按开盘溢价分桶）----
    st2 = {}
    for r in rows:
        if r[0] != 2:
            continue
        b = _bucket_gap(r[3])
        if b:
            st2.setdefault(b, []).append(
                (1 if (r[2] or 0) > 0 else 0, r[2], r[8], r[9]))
    st2_buckets = {}
    for b, lst in sorted(st2.items()):
        st2_buckets[b] = _agg(lst)

    # st=2 执行修复效果：只看 gap>=5 的子桶（决策线放行部分）
    st2_exec = None
    if ">5" in st2_buckets:
        st2_exec = st2_buckets[">5"]

    # ---- 2) 特征列样本积累 + 分桶胜率 ----
    feats = {}

    def _feat(name, getter, buckets):
        by = {}
        covered = 0
        for r in rows:
            v = getter(r)
            if v is None:
                continue
            covered += 1
            b = buckets(v)
            by.setdefault(b, []).append(
                (1 if (r[2] or 0) > 0 else 0, r[2], r[8], r[9]))
        out = {"covered": covered, "buckets": {}}
        for b, lst in sorted(by.items()):
            a = _agg(lst)
            if a and a["n"] >= _MIN_BUCKET:
                out["buckets"][b] = a
        if out["buckets"]:
            feats[name] = out

    _feat("quality", lambda r: r[5], lambda q: "高(≥70)" if q >= 70 else ("中(50-70)" if q >= 50 else "低(<50)"))
    _feat("turn", lambda r: r[6], lambda t: "高(≥15%)" if t >= 15 else ("中(5-15%)" if t >= 5 else "低(<5%)"))
    _feat("sector_strength", lambda r: r[4], lambda s: "强(≥60)" if s >= 60 else ("中(40-60)" if s >= 40 else "弱(<40)"))
    # 竞价形态文本特征
    by_aq = {}
    for r in rows:
        if r[7]:
            by_aq.setdefault(r[7], []).append(
                (1 if (r[2] or 0) > 0 else 0, r[2], r[8], r[9]))
    aq_out = {"covered": sum(len(v) for v in by_aq.values()), "buckets": {}}
    for b, lst in sorted(by_aq.items()):
        a = _agg(lst)
        if a and a["n"] >= _MIN_BUCKET:
            aq_out["buckets"][b] = a
    if aq_out["buckets"]:
        feats["auction_pattern"] = aq_out

    # ---- 3) 盘中路径：亏损票是冲高回落还是阴跌？落袋纪律挽回空间 ----
    losers = [(r[2], r[8], r[9]) for r in rows if (r[2] or 0) <= -1 and r[8] is not None]
    path = None
    if len(losers) >= _MIN_BUCKET:
        n = len(losers)
        had_runup2 = sum(1 for _, ru, _ in losers if (ru or 0) >= 2)
        path = {
            "n_losers": n,
            "pct_had_runup2": round(had_runup2 * 100.0 / n, 1),
            "avg_runup": round(sum(ru or 0 for _, ru, _ in losers) / n, 2),
            "avg_dd": round(sum(d or 0 for _, _, d in losers) / n, 2),
        }
        # 落袋纪律理论挽回：亏损票若 +2% 冲高即卖，收益从 next_pct 变成 min(+2, runup)
        rescue = sum(min(2.0, ru or 0) - np for np, ru, _ in losers)
        path["rescue_per_trade"] = round(rescue / n, 2)  # 每笔平均挽回（百分点）

    # ---- 汇总 ----
    n_total = len(rows)
    overall = _agg([(1 if (r[2] or 0) > 0 else 0, r[2], r[8], r[9]) for r in rows])
    return {
        "n_total": n_total,
        "overall": overall,
        "st2_buckets": st2_buckets,
        "st2_exec": st2_exec,
        "features": feats,
        "loser_path": path,
    }


def summary_lines(ra):
    """推送用简报（复盘段）。"""
    if not ra:
        return []
    out = []
    st2e = ra.get("st2_exec")
    if st2e:
        out.append("st=2修复后执行段（gap≥5%%）：胜率 %s%% / 均值 %s%%（%d 条）"
                   % (st2e["win_rate"], st2e["avg_pct"], st2e["n"]))
    feats = ra.get("features") or {}
    names = {"quality": "封板质量", "turn": "换手", "sector_strength": "板块强度",
             "auction_pattern": "竞价形态"}
    for k, label in names.items():
        f = feats.get(k)
        if not f:
            continue
        bs = f["buckets"]
        if len(bs) >= 2:
            top = max(bs.items(), key=lambda kv: kv[1]["win_rate"])
            bot = min(bs.items(), key=lambda kv: kv[1]["win_rate"])
            out.append("%s归因（%d 条）：最优「%s」%s%%，最差「%s」%s%%"
                       % (label, f["covered"], top[0], top[1]["win_rate"],
                          bot[0], bot[1]["win_rate"]))
    p = ra.get("loser_path")
    if p:
        out.append("亏损票盘中路径（%d 条）：%s%% 曾冲高≥2%%（落袋可挽回 %s%%/笔），平均冲高 %s%%/回落 %s%%"
                   % (p["n_losers"], p["pct_had_runup2"], p["rescue_per_trade"],
                      p["avg_runup"], p["avg_dd"]))
    return out
