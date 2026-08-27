# -*- coding: utf-8 -*-
"""雷区日历：未来两周限售股解禁（金额 TOP）。

数据源：东方财富数据中心 RPT_LIFT_STAGE（2026-08-25 实证可用；filter 需 URL 编码）。
字段实证：FREE_DATE=解禁日, LIFT_MARKET_CAP=万元, FREE_RATIO=解禁占比(0.138=13.8%)。
财报预约披露表无公开可靠接口（多候选名均空），财报段预留、自动跳过。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emdc


UNLOCK_REPORT = "RPT_LIFT_STAGE"


def scan(date, horizon=14):
    """date: 今天(YYYY-MM-DD)。返回未来 horizon 天内的解禁 TOP。"""
    from datetime import datetime, timedelta
    end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=horizon)).strftime("%Y-%m-%d")

    unlock_rows = emdc.get(
        UNLOCK_REPORT, columns="ALL",
        flt="(FREE_DATE>='%s')(FREE_DATE<='%s')" % (date, end),
        page_size=300)
    if not unlock_rows:
        return None

    def fnum(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    unlock = []
    for r in unlock_rows:
        day = str(r.get("FREE_DATE") or "")[:10]
        if not day or not r.get("SECURITY_CODE"):
            continue
        unlock.append({
            "code": r.get("SECURITY_CODE"),
            "name": r.get("SECURITY_NAME_ABBR") or "",
            "day": day,
            "mv_yi": round(fnum(r.get("LIFT_MARKET_CAP")) / 1e4, 1),   # 万元→亿
            "ratio": round(fnum(r.get("FREE_RATIO")) * 100, 2),
        })
    if not unlock:
        return None
    unlock.sort(key=lambda x: x["mv_yi"], reverse=True)

    return {"date": date, "horizon": horizon, "n_all": len(unlock),
            "unlock_top": unlock[:15], "fin_due": []}


def grade(rp):
    """给解禁清单打『可操作评级』：回避 / 中性偏空 / 影响有限（按金额+占比）。"""
    if not rp:
        return rp
    for x in rp.get("unlock_top", []):
        mv = x.get("mv_yi") or 0
        ratio = x.get("ratio") or 0
        if mv >= 50 or ratio >= 10:
            x["grade"], x["gflag"] = "回避", -1
        elif mv >= 20 or ratio >= 5:
            x["grade"], x["gflag"] = "中性偏空", 0
        else:
            x["grade"], x["gflag"] = "影响有限", 1
    return rp


def watch_flags(rp):
    """解禁→关注股雷达的 code->评级 映射，供 watchlist.scan 标注。"""
    if not rp:
        return {}
    return {x["code"]: x.get("grade") for x in rp.get("unlock_top", [])
            if x.get("code")}


def summary_lines(r):
    if not r:
        return []
    un = r.get("unlock_top") or []
    out = ["解禁雷区（未来 %d 日共 %d 笔，金额 TOP）：未来两周合计压力需留意"
           % (r.get("horizon", 14), r.get("n_all", len(un)))]
    for x in un[:5]:
        g = x.get("grade")
        tag = ("［%s］" % g) if g else ""
        out.append("- %s（%s）%s 解禁 %.1f 亿（占流通 %.2f%%）%s"
                   % (x.get("name"), x.get("code"), x.get("day"), x.get("mv_yi"), x.get("ratio"), tag))
    return out
