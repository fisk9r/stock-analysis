# -*- coding: utf-8 -*-
"""watchlist 冒烟测试：本地库实测 scan + summary_lines"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline"))
import store
import engine
import watchlist


def pick_full_date(u):
    """选成交额正常日（股票数>4000 且总额>10万亿）"""
    import statistics
    for date in reversed(u.dates[-30:]):
        rows = u.by_date.get(date) or []
        amt = sum(b.get("amt") or 0 for _, b in rows)
        if len(rows) > 4000 and amt > 1e13:
            return date
    return u.dates[-1]


con = store.connect()
u = engine.Universe(con)
codes, names = watchlist.load_watch_codes()
print("关注池:", codes, names)
date = pick_full_date(u)
print("分析日:", date)

wl = watchlist.scan(u, date)
if not wl:
    print("!! scan 返回 None（关注池为空？）")
    sys.exit(1)
print("n=%d alert_n=%d" % (wl["n"], wl["alert_n"]))
for x in wl["items"]:
    print(" ", x.get("name") or x["code"], x.get("pct"), x.get("signals"), "URGENT" if x.get("urgent") else "")
print("--- summary_lines ---")
for line in watchlist.summary_lines(wl):
    print(line)
assert all(("code" in x and "name" in x) for x in wl["items"]), "item 字段缺失"
assert wl["alert_n"] == sum(1 for x in wl["items"] if x.get("urgent"))
print("SMOKE_OK")
