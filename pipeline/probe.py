# -*- coding: utf-8 -*-
"""接口探测脚本：一次性验证所有候选数据源"""
import json
import sys
import time
import urllib.request
import urllib.parse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def show(tag, url, extract=None):
    print("=" * 70)
    print("[%s]" % tag)
    print(url[:160])
    try:
        txt = get(url)
        try:
            obj = json.loads(txt)
        except Exception:
            print("  RAW:", txt[:300])
            return None
        if extract:
            try:
                extract(obj)
            except Exception as e:
                print("  extract err:", e)
                print("  KEYS:", json.dumps(obj, ensure_ascii=False)[:400])
        else:
            print("  ", json.dumps(obj, ensure_ascii=False)[:400])
        return obj
    except Exception as e:
        print("  FAIL:", repr(e))
        return None


TS = int(time.time() * 1000)
UT = "7eea3edcaed734bea9cbfc24409ed989"

# 1. 涨停池（历史日期 20260803）
for pool, name in [("getTopicZTPool", "涨停池"), ("getTopicYZTPool", "昨日涨停池"),
                   ("getTopicZBPool", "炸板池"), ("getTopicDTPool", "跌停池"),
                   ("getTopicQSPool", "强势股池")]:
    u = ("https://push2ex.eastmoney.com/%s?ut=%s&dpt=wz.ztzt&Pageindex=0&pagesize=5"
         "&sort=fbt%%3Aasc&date=20260803&_=%d" % (pool, UT, TS))
    show(name + "/20260803", u, lambda o: print("  tc=%s qdate=%s sample=%s" % (
        o.get("data", {}).get("tc"), o.get("data", {}).get("qdate"),
        json.dumps((o.get("data") or {}).get("pool", [])[:1], ensure_ascii=False)[:520])))

# 2. 日K线
u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=0.003032"
     "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
     "&klt=101&fqt=1&end=20500101&lmt=10&_=%d" % TS)
show("日K线/传智教育", u, lambda o: print("  name=%s klines=%s" % (
    o["data"]["name"], o["data"]["klines"][-3:])))

# 3. 指数行情
u = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=1.000001,0.399001,0.399006,1.000688,0.399005"
     "&fields=f1,f2,f3,f4,f6,f12,f13,f14,f104,f105&_=%d" % TS)
show("指数快照", u, lambda o: print("  ", json.dumps(o["data"]["diff"], ensure_ascii=False)[:500]))

# 4. 全市场涨跌家数（用 clist 拉全部 A 股，只取涨跌幅）
u = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=10&po=1&np=1&fltt=2&invt=2"
     "&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
     "&fields=f2,f3,f12,f14,f20,f21,f6,f8,f10&_=%d" % TS)
show("全市场A股列表", u, lambda o: print("  total=%s sample=%s" % (
    o["data"]["total"], json.dumps(o["data"]["diff"][:2], ensure_ascii=False)[:300])))

# 5. 行业板块
u = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=8&po=1&np=1&fltt=2&invt=2"
     "&fid=f3&fs=m:90+t:2+f:!50&fields=f2,f3,f12,f14,f62,f104,f105,f128,f136&_=%d" % TS)
show("行业板块", u, lambda o: print("  total=%s sample=%s" % (
    o["data"]["total"], json.dumps(o["data"]["diff"][:3], ensure_ascii=False)[:400])))

# 6. 概念板块
u = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=8&po=1&np=1&fltt=2&invt=2"
     "&fid=f3&fs=m:90+t:3+f:!50&fields=f2,f3,f12,f14,f62,f104,f105,f128,f136&_=%d" % TS)
show("概念板块", u, lambda o: print("  total=%s sample=%s" % (
    o["data"]["total"], json.dumps(o["data"]["diff"][:3], ensure_ascii=False)[:400])))

# 7. 交易日历（用上证指数K线反推）
u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001"
     "&fields1=f1,f2&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
     "&klt=101&fqt=1&end=20500101&lmt=8&_=%d" % TS)
show("上证指数近8日", u, lambda o: print("  ", o["data"]["klines"]))

print("=" * 70)
print("PROBE DONE")
