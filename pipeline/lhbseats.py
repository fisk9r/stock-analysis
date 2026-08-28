# -*- coding: utf-8 -*-
"""龙虎榜席位深挖：今日上榜个股净买额 TOP + 上榜原因 + 买卖席位金额结构。

数据源：东方财富数据中心 RPT_DAILYBILLBOARD_DETAILSNEW（2026-08-25 实证可用）。
实证要点：filter 必须 URL 编码；按 BILLBOARD_NET_AMT(元) 排序；columns=ALL。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emdc


REPORT = "RPT_DAILYBILLBOARD_DETAILSNEW"


def scan(date):
    flt = "(TRADE_DATE='%s')" % date
    rows = emdc.get(REPORT, columns="ALL", flt=flt, page_size=200,
                    sort="BILLBOARD_NET_AMT")
    if not rows:
        return None
    items = emdc.extract(rows, {
        "code": ["SECURITY_CODE"],
        "name": ["SECURITY_NAME_ABBR"],
        "chg": ["CHANGE_RATE"],
        "net": ["BILLBOARD_NET_AMT"],      # 元
        "buy": ["BILLBOARD_BUY_AMT"],
        "sell": ["BILLBOARD_SELL_AMT"],
        "reason": ["EXPLANATION", "EXPLAIN"],
    })
    if not items:
        return None

    def fnum(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    top = sorted(items, key=lambda x: fnum(x.get("net")), reverse=True)[:10]
    top = [{"code": t.get("code"), "name": t.get("name"),
            "net_yi": round(fnum(t.get("net")) / 1e8, 2),
            "buy_yi": round(fnum(t.get("buy")) / 1e8, 2),
            "chg": round(fnum(t.get("chg")), 2),
            "reason": (t.get("reason") or "")[:20]} for t in top]

    # 上榜原因聚合（题材/资金性质侧写）
    from collections import Counter
    reasons = Counter((x.get("reason") or "").split("日")[0][:12]
                      for x in items if x.get("reason"))
    # 全量净买入名单（top10 之外也保留，供「机构/主力介入」引擎做个股级证据匹配）
    net_buy = [{"code": x.get("code"), "name": x.get("name"),
                "net_yi": round(fnum(x.get("net")) / 1e8, 2)}
               for x in items if fnum(x.get("net")) > 0]
    net_buy.sort(key=lambda x: -x["net_yi"])
    return {
        "date": date,
        "n": len(items),
        "top": top,
        "reasons": reasons.most_common(3),
        "net_buy": net_buy[:40],
        "net_buy_n": len(net_buy),
    }


def summary_lines(r):
    if not r:
        return []
    out = ["龙虎榜：今日上榜 %d 只" % r.get("n", 0)]
    for t in r.get("top", [])[:5]:
        out.append("- %s（%s）净买 %s亿 · %s%% · %s"
                   % (t.get("name"), t.get("code"), t.get("net_yi"),
                      ("+" if (t.get("chg") or 0) >= 0 else "") + str(t.get("chg")),
                      t.get("reason") or "—"))
    return out
