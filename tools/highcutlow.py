# -*- coding: utf-8 -*-
"""板块高切低挖掘（2026-09-03，用户需求：结合近期板块轮动，找同板块高切低机会）。

逻辑（全部本地 market.db，无网络依赖）：
  1. 行业板块近 5/10 日等权涨幅排名 → 近期强势板块（成员数>=20 才统计，防小样本）。
  2. 板块内部高切低证据：近10日涨幅 Top30%（高位股）与 Bottom30%（低位股）的
     「今日」表现对比——高位转弱（高位组今日均值 < 低位组今日均值）即发生高切低。
  3. 候选挖掘：在高切低板块里选「低位启动」股：
       · 位置：收盘价距 60 日最低收盘 <= 15%，近10日自身涨幅 <= 8%（还没涨）；
       · 启动：今日放量（量 >= 5日均量 1.3 倍）且今日收涨 0~7%（排除一字板/爆板），
               且收盘站上 MA5；
       · 卫生：非 ST、近60日无连续阴跌（60日涨幅 > -25% 仍在地 organism 上方），
               换手率 1%~20%。
  4. 输出：高切低板块对 + 每板块最多 2 只候选，按「板块强度×低位度×放量度」打分。

用法：python tools/highcutlow.py [top_n]
数据截至 market.db 最后交易日（盘中跑=昨日收盘口径，收盘后 build 即当日）。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pipeline import store  # noqa: E402


def _rows(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def analyze(top_n=12, min_members=20):
    con = sqlite3.connect(store.DB_PATH)
    con.row_factory = sqlite3.Row
    dates = [r[0] for r in _rows(con, "SELECT DISTINCT date FROM bars ORDER BY date")]
    d0, d1 = dates[-5], dates[-1]          # 近5日窗口
    d10 = dates[-11] if len(dates) >= 11 else dates[0]
    d60 = dates[-61] if len(dates) >= 61 else dates[0]
    d5 = dates[-6] if len(dates) >= 6 else dates[0]     # 前5日均量窗口起点
    d20 = dates[-21] if len(dates) >= 21 else dates[0]  # 20日高点窗口

    # ---- 1. 板块近5/10日等权涨幅 ----
    # 只统计行业板块，成员数 >= min_members
    sec_stat = {}
    for bk, name, n in _rows(con, """
            SELECT b.bk, b.name, COUNT(*) FROM boards b
            JOIN board_member m ON m.bk = b.bk
            WHERE b.kind='industry'
            GROUP BY b.bk HAVING COUNT(*) >= ?""", (min_members,)):
        sec_stat[bk] = {"name": name, "n": n}

    for bk in list(sec_stat):
        r = _rows(con, """
            SELECT AVG(CASE WHEN date>=? THEN pct END) AS p5,
                   AVG(CASE WHEN date>=? THEN pct END) AS p10,
                   AVG(CASE WHEN date=? THEN pct END) AS p_now
            FROM bars WHERE code IN (SELECT code FROM board_member WHERE bk=?)
              AND date>=?""", (d0, d10, d1, bk, d10))[0]
        sec_stat[bk].update(p5=r["p5"] or 0, p10=r["p10"] or 0, p_now=r["p_now"] or 0)

    strong = sorted(sec_stat.items(), key=lambda kv: -kv[1]["p5"])[:15]

    # ---- 2. 板块内部高切低证据（高位组 vs 低位组近3日表现）----
    # 2026-09-03 改进：单日口径在普跌日噪音过大（实测 09-02 普跌日 14/15 板块负 cut），
    # 改用近 3 日累计：高位组 3 日弱于低位组 >= 1pp 才认定高切低。
    d3 = dates[-4] if len(dates) >= 4 else dates[0]
    out = []
    for bk, s in strong:
        members = [r[0] for r in _rows(con, "SELECT code FROM board_member WHERE bk=?", (bk,))]
        ph = dict((r["code"], r) for r in _rows(con, """
            SELECT code,
                   SUM(CASE WHEN date>=? THEN pct ELSE 0 END) AS p10,
                   SUM(CASE WHEN date>=? THEN pct ELSE 0 END) AS p_now
            FROM bars WHERE date>=? AND code IN (%s)
            GROUP BY code""" % ",".join("?" * len(members)), (d10, d3, d10, *members)))
        vals = sorted((v["p10"] for v in ph.values() if v["p10"] is not None))
        if len(vals) < 10:
            continue
        import statistics
        q30 = vals[int(len(vals) * 0.3)]
        q70 = vals[int(len(vals) * 0.7)]
        hi_now = [v["p_now"] for v in ph.values() if v["p10"] is not None and v["p10"] >= q70]
        lo_now = [v["p_now"] for v in ph.values() if v["p10"] is not None and v["p10"] <= q30]
        if not hi_now or not lo_now:
            continue
        hi_avg, lo_avg = statistics.mean(hi_now), statistics.mean(lo_now)
        # 高切低证据：高位组近3日弱于低位组（差 >= 1 个百分点）
        if lo_avg - hi_avg < 1.0:
            continue
        out.append({"bk": bk, "sector": s["name"], "p5": s["p5"], "p10": s["p10"],
                    "hi_avg": hi_avg, "lo_avg": lo_avg,
                    "cut": lo_avg - hi_avg, "members": ph, "q30": q30, "q70": q70})

    out.sort(key=lambda x: -x["cut"])

    # ---- 3. 低位启动候选 ----
    # 2026-09-03 改进：cut 板块为空（无高切低格局）时，退化为从最强板块 Top6 直接挖
    # 低位启动票——保证任何市况下都有候选输出；cut 板块优先且打分加权。
    targets = list(out[:6])
    if not targets:
        for bk, s in strong[:6]:
            members = [r[0] for r in _rows(con, "SELECT code FROM board_member WHERE bk=?", (bk,))]
            ph = dict((r["code"], r) for r in _rows(con, """
                SELECT code,
                       SUM(CASE WHEN date>=? THEN pct ELSE 0 END) AS p10,
                       SUM(CASE WHEN date>=? THEN pct ELSE 0 END) AS p_now
                FROM bars WHERE date>=? AND code IN (%s)
                GROUP BY code""" % ",".join("?" * len(members)), (d10, d1, d10, *members)))
            vals = sorted((v["p10"] for v in ph.values() if v["p10"] is not None))
            if len(vals) < 10:
                continue
            targets.append({"sector": s["name"], "p5": s["p5"], "cut": 0.0,
                            "members": ph, "q30": vals[int(len(vals) * 0.3)]})
    cands = []
    nears = []
    for x in targets:
        lo_codes = [c for c, v in x["members"].items()
                    if v["p10"] is not None and v["p10"] <= x["q30"]]
        if not lo_codes:
            continue
        for r in _rows(con, """
                SELECT s.code, MAX(CASE WHEN s.name IS NOT NULL THEN s.name END) AS name,
                       MAX(CASE WHEN b.date=? THEN b.close END) AS close,
                       MAX(CASE WHEN b.date=? THEN b.pct END) AS pct,
                       MAX(CASE WHEN b.date=? THEN b.turn END) AS turn,
                       MAX(CASE WHEN b.date=? THEN b.vol END) AS vol,
                       MAX(CASE WHEN b.date=? THEN b.vol END) /
                         NULLIF(AVG(CASE WHEN b.date>=? AND b.date<? THEN b.vol END), 0) AS vratio,
                       SUM(CASE WHEN b.date>=? THEN b.pct ELSE 0 END) AS p10,
                       MIN(CASE WHEN b.date>=? THEN b.close END) AS low60,
                       AVG(CASE WHEN b.date=? THEN b.close END) AS ma5,
                       AVG(CASE WHEN b.date>=? AND b.date<? THEN b.close END) AS ma5_prev
                FROM bars b JOIN stocks s ON s.code=b.code
                WHERE b.code IN (%s) AND b.date>=?
                GROUP BY b.code"""
                % ",".join("?" * len(lo_codes)),
                (d1, d1, d1, d1, d1, d5, d1, d10, d60, d1, d10, d1, d60, *lo_codes)):
            c, close, pct = r["code"], r["close"], r["pct"]
            name = r["name"] or c
            if not close or pct is None or r["low60"] in (0, None) or not r["vratio"]:
                continue
            if "ST" in (name or "").upper() or "退" in (name or ""):
                continue
            if "ST" in (name or "").upper() or "退" in (name or ""):
                continue
            pos = (close / r["low60"] - 1) * 100          # 距60日低点
            # 软条件打分：最多破 1 条进「接近达标」兜底，保证输出永远可操作
            fails = []
            if pos > 20:
                fails.append("距低点%.0f%%偏高" % pos)
            if (r["p10"] or 0) > 10:
                fails.append("近10日已涨%.0f%%" % (r["p10"] or 0))
            if r["vratio"] < 1.15:
                fails.append("量比%.2f未放量" % r["vratio"])
            if not (-1 < pct < 7):
                fails.append("今日%+.1f%%" % pct)
            if not (r["ma5"] and close >= r["ma5"] * 0.99):
                fails.append("MA5下方")
            if not (0.8 <= (r["turn"] or 0) <= 20):
                fails.append("换手%.1f%%" % (r["turn"] or 0))
            base = {
                "code": c, "name": name, "sector": x["sector"],
                "close": round(close, 2), "pct": round(pct, 2),
                "pos60": round(pos, 1), "vratio": round(r["vratio"], 2),
                "p10": round(r["p10"] or 0, 1), "turn": round(r["turn"] or 0, 1),
            }
            if not fails:
                base["score"] = round(x["cut"] * 2 + (20 - pos) * 0.3 + min(r["vratio"], 3) * 1.5, 1)
                cands.append(base)
            elif len(fails) == 1:
                base["score"] = round(x["cut"] * 2 + (20 - pos) * 0.15 + min(r["vratio"], 3) * 0.5, 1)
                base["near_miss"] = fails[0]
                nears.append(base)
    cands.sort(key=lambda x: -x["score"])
    nears.sort(key=lambda x: -x["score"])

    # ---- 3b. 强者恒强模式：无高切低时，从强势板块挖「趋势股回踩/平台」候选 ----
    # 用户要的是能立即上车的票；低位埋伏在强者恒强格局下负期望，改为跟随强势板块趋势股：
    # 近10日涨>=5%、守 MA5（容差2%）、距20日高点<=8%（回踩而非深跌）、当日未崩（>-4%）。
    trend_cands = []
    if not out:
        for bk, s in strong[:8]:
            members = [r[0] for r in _rows(con, "SELECT code FROM board_member WHERE bk=?", (bk,))]
            for r in _rows(con, """
                    SELECT s.code, MAX(CASE WHEN s.name IS NOT NULL THEN s.name END) AS name,
                           MAX(CASE WHEN b.date=? THEN b.close END) AS close,
                           MAX(CASE WHEN b.date=? THEN b.pct END) AS pct,
                           MAX(CASE WHEN b.date=? THEN b.turn END) AS turn,
                           MAX(CASE WHEN b.date=? THEN b.vol END) /
                             NULLIF(AVG(CASE WHEN b.date>=? AND b.date<? THEN b.vol END), 0) AS vratio,
                           SUM(CASE WHEN b.date>=? THEN b.pct ELSE 0 END) AS p10,
                           AVG(CASE WHEN b.date=? THEN b.close END) AS ma5,
                           MAX(CASE WHEN b.date>=? AND b.date<=? THEN b.close END) AS hi20
                    FROM bars b JOIN stocks s ON s.code=b.code
                    WHERE b.code IN (%s) AND b.date>=?
                    GROUP BY b.code"""
                    % ",".join("?" * len(members)),
                    # 占位符顺序：10个日期占位符 → IN 成员 → WHERE date（日期必须在 members 后再补一个）
                    (d1, d1, d1, d1, d5, d1, d10, d1, d20, d1, *members, d20)):
                c, close, pct = r["code"], r["close"], r["pct"]
                name = r["name"] or c
                if not close or pct is None or not r["hi20"]:
                    continue
                if "ST" in (name or "").upper() or "退" in (name or ""):
                    continue
                if (r["p10"] or 0) < 5:                      # 真趋势
                    continue
                if not (r["ma5"] and close >= r["ma5"] * 0.98):   # 守住 MA5
                    continue
                pullback = (r["hi20"] / close - 1) * 100     # 距20日高点回撤
                if pullback > 8 or pullback < -1:            # 回踩而非突破过远
                    continue
                if pct < -4:                                 # 当日未崩
                    continue
                if not (0.8 <= (r["turn"] or 0) <= 25):
                    continue
                score = s["p5"] * 2 + (r["p10"] or 0) * 0.5 - pullback * 0.4 + min(r["vratio"] or 1, 2)
                trend_cands.append({
                    "code": c, "name": name, "sector": s["name"],
                    "close": round(close, 2), "pct": round(pct, 2),
                    "p10": round(r["p10"] or 0, 1), "turn": round(r["turn"] or 0, 1),
                    "pullback": round(pullback, 1),
                    "vratio": round(r["vratio"] or 1, 2),
                    "score": round(score, 1),
                })
        trend_cands.sort(key=lambda x: -x["score"])

    # 每板块最多 2 只
    seen_sec = {}
    final = []
    for c in cands:
        if seen_sec.get(c["sector"], 0) >= 2:
            continue
        seen_sec[c["sector"]] = seen_sec.get(c["sector"], 0) + 1
        final.append(c)
        if len(final) >= top_n:
            break
    # 兜底：正式候选不足 3 只时，用「接近达标」（仅破 1 条软条件）补位
    if len(final) < 3:
        for c in nears:
            if c in final or seen_sec.get(c["sector"], 0) >= 2:
                continue
            seen_sec[c["sector"]] = seen_sec.get(c["sector"], 0) + 1
            final.append(c)
            if len(final) >= top_n:
                break
    # 强者恒强模式：趋势股候选补入（弱于正式候选，仅在低位候选空缺时顶上）
    if trend_cands:
        seen_t = {}
        for c in trend_cands:
            if c in final or seen_t.get(c["sector"], 0) >= 2 or len(final) >= 6:
                continue
            seen_t[c["sector"]] = seen_t.get(c["sector"], 0) + 1
            final.append(c)
    return {"date": d1, "cuts": out[:6], "cands": final,
            "regime": "cut" if out else "strong"}


def lines(res, n=12):
    out = ["（数据截至 %s 收盘）" % res["date"], ""]
    if res.get("regime") == "cut":
        out.append("## 一、正在发生高切低的强势板块（近3日口径）")
        for x in res["cuts"]:
            out.append("- **%s**（近5日%+.1f%%）：高位股近3日 %+.1f%% → 低位股近3日 %+.1f%%"
                       % (x["sector"], x["p5"], x["hi_avg"], x["lo_avg"]))
    else:
        out.append("## 一、市场格局判断：当前无同板块高切低，呈「强者恒强」")
        out.append("- 近3日强势板块内高位股普遍继续跑赢低位股（低位补涨条件不成立）")
        out.append("- 策略含义：不宜埋伏低位等补涨；跟随强势板块中的趋势股（与竞价纪律 st 体系一致）")
    out.append("")
    if any(c.get("pullback") is not None for c in res["cands"][:n]):
        out.append("## 二、强势板块·趋势股候选（强者恒强跟随，回踩 MA5 可立即买）")
    else:
        out.append("## 二、低位启动候选（可立即关注）")
    for c in res["cands"][:n]:
        if c.get("pullback") is not None:
            out.append("- **%s**(%s) %s板块 ｜ 现价%.2f（%+.1f%%）｜ 近10日%+.1f%% ｜ 距20日高点回撤%.1f%%（踩MA5附近）｜ 换手%.1f%%"
                       % (c["name"], c["code"], c["sector"],
                          c["close"], c["pct"], c["p10"], c["pullback"], c["turn"]))
        else:
            tag = " ⚠%s" % c["near_miss"] if c.get("near_miss") else ""
            out.append("- **%s**(%s) %s板块 ｜ 现价%.2f（%+.1f%%）｜ 距60日低点仅%+.1f%% ｜ 量比%.1f ｜ 近10日%+.1f%%%s"
                       % (c["name"], c["code"], c["sector"],
                          c["close"], c["pct"], c["pos60"], c["vratio"], c["p10"], tag))
    if not res["cands"]:
        out.append("-（今日无符合「低位+放量+站上MA5」的候选）")
    return out


if __name__ == "__main__":
    res = analyze(top_n=int(sys.argv[1]) if len(sys.argv) > 1 else 12)
    print("\n".join(lines(res)))
