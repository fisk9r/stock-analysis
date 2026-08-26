# -*- coding: utf-8 -*-
"""连续信号：从落库历史（engine_snapshots / seat_daily / theme_daily）提取
跨日硬信号——两融连日净流出、ETF 连续净申购、龙虎榜情绪变化、知名席位重复扫货、
题材主线持续/退潮。这些单日快照看不到，必须靠历史序列。

所有函数对“无历史/数据不足”友好降级为 None，由 build 决定是否展示。
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store


def _tail_streak(vals):
    """vals: 按日期正序的数值列表。返回 (连续正天数, 连续负天数, 最新值)。"""
    if not vals:
        return 0, 0, None
    pos = neg = 0
    for v in reversed(vals):
        if v > 0:
            if neg == 0:
                pos += 1
            else:
                break
        elif v < 0:
            if pos == 0:
                neg += 1
            else:
                break
        else:
            break
    return pos, neg, vals[-1]


def margin_signal(con, days=25):
    hist = store.snapshot_history(con, "margin", days)
    if not hist:
        return None
    deltas = [h.get("delta_yi") for d, h in hist if isinstance(h, dict) and h.get("delta_yi") is not None]
    if len(deltas) < 3:
        return None
    pos, neg, last = _tail_streak(deltas)
    verdict = "中性"
    if pos >= 3:
        verdict = "两融连续%d日净流入（杠杆资金加仓）" % pos
    elif neg >= 3:
        verdict = "两融连续%d日净流出（杠杆资金撤退）" % neg
    return {
        "key": "margin",
        "last_delta": round(last, 1) if last is not None else None,
        "streak_in": pos,
        "streak_out": neg,
        "verdict": verdict,
    }


def etf_signal(con, days=25):
    hist = store.snapshot_history(con, "etfflow", days)
    if not hist:
        return None
    nets = [h.get("total_net_yi") for d, h in hist if isinstance(h, dict) and h.get("total_net_yi") is not None]
    if len(nets) < 3:
        return None
    pos, neg, last = _tail_streak(nets)
    verdict = "中性"
    if pos >= 3:
        verdict = "ETF 连续%d日主力净申购（增量资金入场）" % pos
    elif neg >= 3:
        verdict = "ETF 连续%d日主力净赎回（资金离场）" % neg
    return {
        "key": "etf",
        "last_net": round(last, 1) if last is not None else None,
        "streak_in": pos,
        "streak_out": neg,
        "verdict": verdict,
    }


def lhb_signal(con, days=20):
    hist = store.snapshot_history(con, "lhbseats", days)
    if not hist:
        return None
    ns = [h.get("n") for d, h in hist if isinstance(h, dict) and h.get("n") is not None]
    if len(ns) < 5:
        return None
    recent = sum(ns[-3:]) / 3.0
    prior = sum(ns[-6:-3]) / 3.0
    if recent >= prior * 1.2 and recent >= 30:
        verdict = "龙虎榜活跃度抬升（游资进攻意愿增强）"
    elif recent <= prior * 0.8:
        verdict = "龙虎榜活跃度回落（游资趋于谨慎）"
    else:
        verdict = "龙虎榜活跃度平稳"
    return {"key": "lhb", "recent_avg": round(recent, 0), "prior_avg": round(prior, 0),
            "verdict": verdict}


def seat_repeat(con, days=10, min_times=2):
    """知名席位在最近交易日里反复净买同一只股票 → 重点跟踪。"""
    rows = con.execute(
        "SELECT date,label,code,name,net_yi FROM seat_daily "
        "WHERE date>=? AND net_yi>0 ORDER BY date",
        (store._days_ago(days),)).fetchall()
    if not rows:
        return []
    by_stock = defaultdict(list)
    for date, label, code, name, net in rows:
        by_stock[code].append((date, label, name, net))
    out = []
    for code, evs in by_stock.items():
        if len(evs) >= min_times:
            dates = sorted({e[0] for e in evs})
            labels = sorted({e[1] for e in evs})
            name = evs[0][2]
            out.append({
                "code": code,
                "name": name,
                "times": len(evs),
                "labels": labels,
                "net_yi": round(sum(e[3] for e in evs) / 1e8, 2) if evs[0][3] > 100 else round(sum(e[3] for e in evs), 2),
                "dates": dates,
            })
    out.sort(key=lambda x: (-x["times"], -x["net_yi"]))
    return out[:10]


def compute_all(con):
    out = {"margin": margin_signal(con),
           "etf": etf_signal(con),
           "lhb": lhb_signal(con),
           "seat_repeat": seat_repeat(con)}
    # 仅留有效信号
    out = {k: v for k, v in out.items() if v}
    return out or None


def summary_lines(sig):
    if not sig:
        return []
    out = []
    for k in ("margin", "etf", "lhb"):
        s = sig.get(k)
        if s and s.get("verdict") not in ("中性", "龙虎榜活跃度平稳", None):
            out.append("- %s" % s["verdict"])
    sr = sig.get("seat_repeat") or []
    for r in sr[:3]:
        out.append("- %s 被知名席位反复净买 %d 次（%s）"
                   % (r["name"], r["times"], "、".join(r["labels"][:2])))
    return out
