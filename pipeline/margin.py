# -*- coding: utf-8 -*-
"""两融余额趋势：全市场融资融券余额及每日变化，作市场杠杆情绪佐证。

数据源：东方财富数据中心 RPTA_RZRQ_LSHJ（2026-08-25 实证可用）。
字段实证：DIM_DATE=日期, RZYE=融资余额, RZRQYE=两融总额, RZRQYECZ=当日差值。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emdc


REPORT = "RPTA_RZRQ_LSHJ"


def scan(date, n=20):
    rows = emdc.get(REPORT, columns="ALL", page_size=n + 2, sort="DIM_DATE")
    if not rows:
        return None

    def fnum(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    series = []
    for r in rows:
        d = str(r.get("DIM_DATE") or "")[:10]
        if not d:
            continue
        series.append({
            "date": d,
            "total_yi": round(fnum(r.get("RZRQYE")) / 1e8, 0),
            "delta_yi": None,   # 稍后自行计算（RZRQYECZ 字段口径不可靠，实测异常）
        })
    if len(series) < 2:
        return None
    # DIM_DATE 倒序返回 → 转正序；当日差值 = 今日总额 - 昨日总额
    series.sort(key=lambda x: x["date"])
    for i in range(1, len(series)):
        series[i]["delta_yi"] = round(series[i]["total_yi"] - series[i - 1]["total_yi"], 1)
    latest = series[-1]
    return {
        "date": latest["date"],
        "latest_yi": latest["total_yi"],
        "delta_yi": latest["delta_yi"],
        "series": series,
    }


def summary_lines(r):
    if not r:
        return []
    d = r.get("delta_yi") or 0
    trend = "回升" if d >= 0 else "回落"
    return ["两融余额：最新 %.0f 亿（当日 %s%.0f 亿，杠杆情绪%s）"
            % (r.get("latest_yi", 0), "+" if d >= 0 else "", d, trend)]
