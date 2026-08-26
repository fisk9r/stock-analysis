# -*- coding: utf-8 -*-
"""尾盘偷袭 / 尾盘跳水检测（焦点池定向版）

数据源：腾讯 web.ifzq.gtimg.cn/appstock/app/day/query?code=sh|sz{code}
返回近 5 个交易日逐分钟明细（"HHMM 价格 累计量 累计额"，prec=昨收）。

口径：
- 焦点池 = 今日涨停 ∪ 今日炸板 ∪ 昨日涨停 ∪ 连板≥3 的股票（市场最关注的票才看尾盘）
- 尾盘偷袭拉升：14:30→15:00 涨幅 ≥1.5%，且全天涨幅 ≥2%，尾盘贡献了当日大部分涨幅
- 尾盘跳水：14:30→15:00 跌幅 ≤-1.5%（出货/砸盘警示）
- 附带近 5 日同股统计：该股是否惯于尾盘异动（偷袭惯犯标记）

离线保护：全部请求失败时返回 None（本地无网构建不炸主流程）。纯标准库。
"""
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RAID_TH = 1.5      # 尾盘(14:30后)涨跌幅阈值 %
DAY_MIN = 2.0      # 偷袭拉升要求的全天最小涨幅 %
FOCUS_CAP = 150    # 焦点池上限
WORKERS = 8
URL_TPL = "https://web.ifzq.gtimg.cn/appstock/app/day/query?code={sym}"


def _sym(code):
    return ("sh" if code[0] in "69" else ("bj" if code[0] in "48" else "sz")) + code


def _fetch_days(code, timeout=10):
    """返回 {date(YYYYMMDD): {'prec':float,'rows':[(hhmm,price,cum_amt),...]}}"""
    url = URL_TPL.format(sym=_sym(code))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    node = (d.get("data") or {}).get(_sym(code)) or {}
    out = {}
    for blk in node.get("data") or []:
        rows = []
        for line in blk.get("data") or []:
            p = line.split()
            if len(p) >= 4:
                try:
                    rows.append((p[0], float(p[1]), float(p[3])))
                except ValueError:
                    continue
        try:
            prec = float(blk.get("prec") or 0)
        except (TypeError, ValueError):
            continue
        out[blk.get("date") or ""] = {"prec": prec, "rows": rows}
    return out


def _analyze_day(blk):
    """单日分钟块 -> {day_pct, last30_pct, tail_amt_share} 或 None"""
    rows = blk["rows"]
    prec = blk["prec"]
    if not rows or not prec:
        return None
    close = rows[-1][1]
    p1430 = None
    amt_total = rows[-1][2]
    amt_1430 = None
    for t, pr, ca in rows:
        if t <= "1430":
            p1430 = pr
            amt_1430 = ca
    if p1430 is None or amt_1430 is None:
        return None
    day_pct = (close / prec - 1) * 100
    last30_pct = (close / p1430 - 1) * 100
    tail_share = max(0.0, amt_total - amt_1430) / amt_total if amt_total else 0.0
    return {"day_pct": round(day_pct, 2), "last30_pct": round(last30_pct, 2),
            "tail_amt_share": round(tail_share, 3)}


def focus_codes(u, date):
    """焦点池：今日涨停/炸板 + 昨日涨停 + 高连板"""
    codes = set()
    codes |= u.zt.get(date) or set()
    codes |= u.zhaban.get(date) or set()
    pd = u.prev_date(date)
    if pd:
        codes |= u.zt.get(pd) or set()
        for c, sd in u.streak.items():
            if sd.get(pd, 0) >= 3:
                codes.add(c)
    # 排序：今日涨停优先，控制规模
    ranked = sorted(codes, key=lambda c: 0 if c in (u.zt.get(date) or ()) else
                    (1 if c in (u.zhaban.get(date) or ()) else 2))
    return ranked[:FOCUS_CAP]


def scan(u, date):
    ymd = date.replace("-", "")
    codes = focus_codes(u, date)
    if not codes:
        return None

    results = {}

    def work(c):
        try:
            days = _fetch_days(c)
        except Exception:
            return c, None
        blk = days.get(ymd)
        if not blk:
            return c, {"today": None, "habit_n": 0, "days_n": len(days)}
        today = _analyze_day(blk)
        habit = 0
        for d2, b2 in days.items():
            if d2 == ymd:
                continue
            a2 = _analyze_day(b2)
            if a2 and abs(a2["last30_pct"]) >= RAID_TH:
                habit += 1
        return c, {"today": today, "habit_n": habit,
                   "days_n": sum(1 for d2 in days if d2 != ymd)}

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, c) for c in codes]
        for fu in as_completed(futs):
            c, r = fu.result()
            if r is None:
                fail += 1
            else:
                ok += 1
                results[c] = r
    if ok < max(3, len(codes) // 3):     # 大面积失败视为离线
        return None

    raids, dumps = [], []
    for c, r in results.items():
        t = r.get("today")
        if not t:
            continue
        st = u.stocks.get(c, {})
        item = {
            "code": c, "name": st.get("name") or "",
            "day_pct": t["day_pct"], "last30": t["last30_pct"],
            "tail_amt": t["tail_amt_share"], "habit": r.get("habit_n", 0),
            "kind": None,
        }
        if t["last30_pct"] >= RAID_TH and t["day_pct"] >= DAY_MIN \
                and t["last30_pct"] >= 0.6 * t["day_pct"]:
            item["kind"] = "raid"
            raids.append(item)
        elif t["last30_pct"] <= -RAID_TH:
            item["kind"] = "dump"
            dumps.append(item)

    raids.sort(key=lambda x: -x["last30"])
    dumps.sort(key=lambda x: x["last30"])
    return {
        "date": date,
        "scanned": len(results),
        "raids": raids[:10],
        "dumps": dumps[:10],
        "raid_n": len(raids),
        "dump_n": len(dumps),
    }


def summary_lines(tr):
    """推送用紧凑摘要"""
    if not tr:
        return []
    out = []
    rs = tr.get("raids") or []
    ds = tr.get("dumps") or []
    if rs:
        out.append("尾盘偷袭拉升 %d 只：%s" % (
            tr.get("raid_n", len(rs)),
            "、".join("%s(+%.1f%%尾盘%s)" % (
                x["name"] or x["code"], x["last30"],
                "·惯犯%d次" % x["habit"] if x["habit"] >= 2 else "")
                for x in rs[:4])))
    if ds:
        out.append("尾盘跳水 %d 只：%s" % (
            tr.get("dump_n", len(ds)),
            "、".join("%s(%.1f%%)" % (x["name"] or x["code"], x["last30"])
                      for x in ds[:3])))
    if not out:
        out.append("焦点池 %d 只尾盘无异动" % tr.get("scanned", 0))
    return out
