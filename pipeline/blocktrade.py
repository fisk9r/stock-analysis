# -*- coding: utf-8 -*-
"""大宗交易监测：当日大宗交易折价/溢价榜（折价率>8% 视为减持/出货信号）。

数据源：东方财富数据中心 RPT_BULK_DEAL_DETAIL（CI 有网即用）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emdc


REPORT = "RPT_BULK_DEAL_DETAIL"
COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,TRADE_PRICE,CLOSE_PRICE,"
        "DISCOUNT,TRADE_VOLUME,TRADE_AMOUNT,BUYER_NAME,SELLER_NAME")


def scan(date):
    rows = emdc.get(REPORT, columns=COLS,
                    flt="(TRADE_DATE='%s')" % date, page_size=300, sort="TRADE_AMOUNT")
    if not rows:
        return None
    items = emdc.extract(rows, {
        "code": ["SECURITY_CODE"],
        "name": ["SECURITY_NAME_ABBR"],
        "price": ["TRADE_PRICE"],
        "close": ["CLOSE_PRICE"],
        "discount": ["DISCOUNT"],
        "vol": ["TRADE_VOLUME"],
        "amt": ["TRADE_AMOUNT"],
        "buyer": ["BUYER_NAME"],
        "seller": ["SELLER_NAME"],
    })
    if not items:
        return None

    def fnum(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    # 折价率（DISCOUNT 字段含义常为 成交价/收盘价-1 的负值百分比，取绝对值排序）
    discount_deals = [x for x in items if fnum(x.get("discount")) <= -5.0]
    discount_deals.sort(key=lambda x: fnum(x.get("discount")))
    top = [{
        "code": d.get("code"), "name": d.get("name"),
        "discount": round(fnum(d.get("discount")), 2),
        "amt_yi": round(fnum(d.get("amt")) / 1e8, 2),
        "buyer": (d.get("buyer") or "")[:12],
        "seller": (d.get("seller") or "")[:12],
    } for d in discount_deals[:10]]

    return {"date": date, "n": len(items),
            "discount_n": len(discount_deals), "top": top}


def summary_lines(r):
    if not r:
        return []
    out = ["大宗交易：当日 %d 笔，其中折价≥5%% 的 %d 笔" % (r.get("n", 0), r.get("discount_n", 0))]
    for t in r.get("top", [])[:5]:
        out.append("- %s（/%s）折价 %s%% · %.1f亿" % (t["name"], t["code"], t["discount"], t["amt_yi"]))
    return out
