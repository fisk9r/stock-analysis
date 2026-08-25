# -*- coding: utf-8 -*-
"""六个新引擎模块的本地冒烟测试（用 cache/market.db，含残缺日保护验证）"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline"))
import store
import engine
import dryvol
import newhigh
import maglue
import trendsword
import stylereg


def pick_full_date(u):
    """找一个成交额正常（非残缺）的近期交易日"""
    for d in reversed(u.dates[-10:]):
        rows = u.by_date.get(d) or []
        amt = sum((b.get("amt") or 0) for _, b in rows)
        if len(rows) > 4000 and amt > 1e13:
            return d
    return u.dates[-2]


con = store.connect()
u = engine.Universe(con)
date = u.dates[-1]      # 最后一天可能是残缺日 → 验证完整性保护
full = pick_full_date(u)
print("date =", date, " stocks =", len(u.bars), " full =", full)

t0 = time.time()
dv = dryvol.analyze(u, date)
print("[dryvol@末日用] %.1fs" % (time.time() - t0), "->",
      (dv or {}).get("state"), "| partial=",
      str((dv or {}).get('today', {}).get('partial')))
dv2 = dryvol.analyze(u, full)
print("[dryvol@完整日] ->", (dv2 or {}).get("state"))
for ln in dryvol.summary_lines(dv2):
    print("   ", ln)

t0 = time.time()
nb = newhigh.scan(u, full)
print("[newhigh] %.1fs" % (time.time() - t0), " nh=", nb["today"]["nh"],
      " nl=", nb["today"]["nl"])
for ln in newhigh.summary_lines(nb)[:2]:
    print("   ", ln)

t0 = time.time()
gg = maglue.scan(u, full)
print("[maglue] %.1fs" % (time.time() - t0), " glue_n=", gg["glue_n"],
      " launching=", gg["launching_n"])
for ln in maglue.summary_lines(gg):
    print("   ", ln)

t0 = time.time()
cf = trendsword.scan(u, full)
print("[trendsword] %.1fs" % (time.time() - t0), " hits=", len(cf["hits"]),
      " stats=", {k: v.get("n") for k, v in cf["stats"].items()})
for ln in trendsword.summary_lines(cf):
    print("   ", ln)

t0 = time.time()
sty = stylereg.scan(u, full)
print("[stylereg] %.1fs" % (time.time() - t0), " ->", sty["verdict"]["label"])
for ln in stylereg.summary_lines(sty):
    print("   ", ln)

# tailraid：本地焦点池 + 网络单点验证
import tailraid
fc = tailraid.focus_codes(u, full)
print("[tailraid] focus pool =", len(fc))
try:
    days = tailraid._fetch_days(fc[0])
    ymd = full.replace("-", "")
    blk = days.get(ymd)
    a = tailraid._analyze_day(blk) if blk else None
    print("[tailraid] fetch OK", fc[0], "->", a)
except Exception as e:
    print("[tailraid] fetch FAIL (离线环境可接受):", repr(e))

print("SMOKE_OK")
