# -*- coding: utf-8 -*-
"""ETF 资金流向：全市场 ETF 主力净流入排行与汇总，作风格判定第五维证据。

数据源：东方财富 push2 clist 基金板块 b:MK0021~MK0024（2026-08-25 实证可用，
返回真实 ETF 名单如 沪深300ETF/港股通医疗ETF 等）。f62=主力净流入(元)。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import em_api


FS = "b:MK0021,b:MK0022,b:MK0023,b:MK0024"
FIELDS = "f12,f14,f3,f62"


def scan(date, max_pages=4):
    rows, _ = em_api.clist_paged(FS, FIELDS, max_pages=max_pages)
    if not rows:
        return None

    def fnum(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    items = []
    for m in rows:
        code = str(m.get("f12") or "")
        name = m.get("f14") or ""
        if not code or not name:
            continue
        # 排除非 ETF 的场内基金噪音（LOF/货币），保留名称含 ETF 或代码 5/1 开头 6 位
        if "ETF" not in name.upper() and not (code[:1] in "51" and len(code) == 6):
            continue
        items.append({
            "code": code,
            "name": name,
            "pct": round(fnum(m.get("f3")), 2),
            "net_yi": round(fnum(m.get("f62")) / 1e8, 2),
        })
    if not items:
        return None

    items.sort(key=lambda x: x["net_yi"], reverse=True)
    total_net = sum(x["net_yi"] for x in items)
    inflow = [x for x in items if x["net_yi"] > 0]
    outflow = [x for x in items if x["net_yi"] < 0]
    return {
        "date": date,
        "n": len(items),
        "total_net_yi": round(total_net, 1),
        "inflow_n": len(inflow),
        "outflow_n": len(outflow),
        "top": items[:8],
        "bottom": list(reversed(items[-5:])),
    }


def summary_lines(r):
    if not r:
        return []
    out = ["ETF 资金流：%d 只样本，主力净流入合计 %s 亿（净流入 %d 只 / 净流出 %d 只）"
           % (r.get("n", 0), ("+" if (r.get("total_net_yi") or 0) >= 0 else "")
              + str(r.get("total_net_yi")), r.get("inflow_n", 0), r.get("outflow_n", 0))]
    for t in r.get("top", [])[:4]:
        out.append("- %s 净流入 %s亿（%s%%）" % (t["name"], t["net_yi"], t["pct"]))
    return out
