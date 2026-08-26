# -*- coding: utf-8 -*-
"""探测二：全市场清单规模 + 并发K线拉取速度"""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TS = int(time.time() * 1000)
UT = "7eea3edcaed734bea9cbfc24409ed989"
OUT = open("probe2.log", "w", encoding="utf-8")


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    OUT.write(s + "\n")
    OUT.flush()


def get(url, timeout=15, retry=3):
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
                "Connection": "close"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            last = e
            time.sleep(0.3 * (i + 1))
    raise last


# A. 跌停池 / 强势池 换排序字段
for pool, srt in [("getTopicDTPool", "fund%3Aasc"), ("getTopicDTPool", "lbt%3Aasc"),
                  ("getTopicQSPool", "zdp%3Adesc"), ("getTopicQSPool", "ltsz%3Aasc")]:
    u = ("https://push2ex.eastmoney.com/%s?ut=%s&dpt=wz.ztzt&Pageindex=0&pagesize=3"
         "&sort=%s&date=20260804&_=%d" % (pool, UT, srt, TS))
    try:
        o = get(u)
        pl = (o.get("data") or {}).get("pool") or []
        log("[%s %s] tc=%s got=%d %s" % (pool, srt, (o.get("data") or {}).get("tc"),
                                         len(pl), json.dumps(pl[:1], ensure_ascii=False)[:320]))
    except Exception as e:
        log("[%s %s] FAIL %r" % (pool, srt, e))

# B. 全市场清单
t0 = time.time()
allrows, pn = [], 1
while pn <= 40:
    u = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=200&po=1&np=1&fltt=2&invt=2"
         "&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
         "&fields=f2,f3,f5,f6,f8,f12,f13,f14,f20,f21&_=%d" % (pn, TS))
    o = get(u, 25)
    d = o.get("data") or {}
    rows = d.get("diff") or []
    allrows.extend(rows)
    if not rows or len(allrows) >= d.get("total", 0):
        break
    pn += 1
log("[全市场清单] total=%d 耗时=%.1fs 页数=%d" % (len(allrows), time.time() - t0, pn))
pref = {}
for r in allrows:
    pref[str(r["f12"])[:3]] = pref.get(str(r["f12"])[:3], 0) + 1
log("  代码前缀分布:", json.dumps(dict(sorted(pref.items(), key=lambda x: -x[1])[:16]), ensure_ascii=False))

# C. 并发K线速度
sample = allrows[:240]


def fetch_k(r):
    secid = "%d.%s" % (r["f13"], r["f12"])
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
         "&fields1=f1,f2&fields2=f51,f53,f54,f55,f56,f59,f61"
         "&klt=101&fqt=1&end=20500101&lmt=125&_=%d" % (secid, TS))
    try:
        o = get(u, 15, retry=2)
        return len(((o.get("data") or {}) or {}).get("klines") or [])
    except Exception:
        return -1


for workers in (20, 40):
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(fetch_k, sample))
    dt = time.time() - t0
    ok = sum(1 for x in res if x > 0)
    log("[K线并发 w=%d] %d只 耗时=%.1fs 成功=%d 预估%d只=%.0fs"
        % (workers, len(sample), dt, ok, len(allrows), dt / len(sample) * len(allrows)))

u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=0.003032"
     "&fields1=f1,f2&fields2=f51,f53,f54,f55,f56,f59,f61&klt=101&fqt=1&end=20500101&lmt=125&_=%d" % TS)
o = get(u)
kl = o["data"]["klines"]
log("[单只K线] 条数=%d 首=%s 末=%s" % (len(kl), kl[0], kl[-1]))
log("PROBE2 DONE")
OUT.close()
