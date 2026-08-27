# -*- coding: utf-8 -*-
"""连板预期空间引擎：挖掘「明天有机会买的连板票」，给出买卖区间与预期高度。

用户需求（2026-08-27）：
  · 100% 胜率不可能，高位票标注即可
  · 需要挖掘「连板第二天有机会买入」的股票
  · 给出提示：入手时是连续三板 → 预期五板等；给买入/卖出区间、波段操作同理

实现：
  · ladder_stats(u, lookback)：从全市场日K重建历史连板路径，按「入手时高度 s」分桶，
    统计买入后（次日开盘，近似竞价接力口径）的 MFE(最高溢价)/MAE(最低回撤)、
    到达 +5%/+10%/+15%/+20% 的到达率、平均可持有天数。
  · plan(u, streak, close) → dict: expected_top(预期板数区间)/buy_zone/sell_zone/
    t1/t2/stop/hold_days/rr(盈亏比)/evidence(n 样本)
  · scan(u, date, lus, topn)：对当日候选连板票生成计划单。

注：全部离线自 cache/market.db 日K 重建，不依赖外部接口。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 各「入手高度 s」的经验期望收益基线（回测校准前的保守先验；真实数据覆盖后加权融合）
_PRIOR = {
    1: {"exp": 2.0, "reach10": 0.18, "days": 1.5},
    2: {"exp": 3.5, "reach10": 0.26, "days": 2.0},
    3: {"exp": 5.0, "reach10": 0.30, "days": 2.5},
    4: {"exp": 6.0, "reach10": 0.32, "days": 2.5},
    5: {"exp": 4.0, "reach10": 0.28, "days": 2.0},
}
_BUCKET_BLEND = 0.65   # 真实分桶权重（样本≥MIN_N 时），先验占余下
_MIN_N = 20            # 分桶最少样本
_MFE_LEVELS = (5.0, 10.0, 15.0, 20.0)


def _limit_pct(code, name):
    """涨停幅度：ST 5%；创业板/科创板(30/68) 20%；北交所(8开头4开头除83/87/88？简化 30%)；其余 10%。"""
    try:
        pre = str(code)[:2]
        if name and ("ST" in name.upper()):
            return 5.0
        if str(code)[:3] in ("300", "301", "688", "689"):
            return 20.0
        if str(code)[0] in ("8", "4") and str(code)[:2] in ("83", "87", "88", "43"):
            return 30.0
    except Exception:
        pass
    return 10.0


def _is_zt(b, code, name):
    """该日是否收涨停（用 pct 与幅度容差判定）。"""
    lim = _limit_pct(code, name)
    pct = b.get("pct") or 0
    return pct >= lim - 0.35


def ladder_stats(u, lookback=260):
    """全市场历史连板路径 → 按『涨停日高度 s』分桶，统计 T+1 开盘介入后的表现。

    口径：在第 s 板次日以开盘价 o_next 介入（模拟“连板第二天竞价上车”），
      mfe = (此后 max(high) / o_next - 1)*100（直到首次收在开盘价 -8% 止损或路径结束）
      达标持有天数 = 从次日起到最高价出现日的交易日数
    返回 {s: {n, exp, mfe_med, reach:{lvl:p}, days_med}}。s 只统计 1..7。
    """
    out = {}
    dates = u.dates
    n_dates = len(dates)
    start_i = max(0, n_dates - lookback)
    for code in u.bars:
        bs = u.bars.get(code) or []
        if len(bs) < 5:
            continue
        name = (u.stocks.get(code, {}) or {}).get("name") or ""
        # 建立 date->idx
        didx = {b["d"]: i for i, b in enumerate(bs)}
        for i in range(start_i, n_dates):
            d = dates[i]
            j = didx.get(d)
            if j is None:
                continue
            b = bs[j]
            if not _is_zt(b, code, name):
                continue
            # 连板高度：向前数连续涨停天数（含今日）
            s = 0
            k = j
            while k >= 0 and _is_zt(bs[k], code, name):
                s += 1
                k -= 1
            if s < 1 or s > 7:
                continue
            # 次日才有数据才算完整事件
            if j + 1 >= len(bs):
                continue
            onext = bs[j + 1].get("o") or 0
            if not onext or onext <= 0:
                continue
            # 模拟持有：从次日开始跟踪，最多 10 个交易日；
            # 止损规则：收盘较 o_next 跌 8% 离场（保守）；记录全程 high 极值
            best_high = bs[j + 1].get("h") or onext
            best_day = 1
            stop_hit_day = None
            for step in range(1, min(11, len(bs) - j)):
                bb = bs[j + step]
                hi = bb.get("h") or 0
                if hi > best_high:
                    best_high = hi
                    best_day = step
                cl = bb.get("c") or 0
                if cl and cl <= onext * 0.92:
                    stop_hit_day = step
                    break
            mfe = (best_high / onext - 1) * 100.0
            eod_ret = ((bs[j + 1].get("c") or onext) / onext - 1) * 100.0
            bk = out.setdefault(s, {"n": 0, "exp_sum": 0.0,
                                    "mfes": [], "days": []})
            bk["n"] += 1
            bk["exp_sum"] += eod_ret          # 用首日尾盘收益近似经验收益（快而稳）
            bk["mfes"].append(mfe)
            bk["days"].append(best_day)
    # 汇总成桶统计
    res = {}
    for s, bk in out.items():
        mfes = sorted(bk["mfes"])
        n = bk["n"]
        days = sorted(bk["days"])
        med = lambda a: a[len(a) // 2] if a else 0
        reach = {}
        for lv in _MFE_LEVELS:
            reach[lv] = round(sum(1 for x in mfes if x >= lv) / n, 3)
        res[s] = {
            "n": n,
            "exp": round(bk["exp_sum"] / n, 2),
            "mfe_med": round(med(mfes), 2),
            "reach": reach,
            "days_med": med(days),
        }
    return res


def _bucket(stats, s):
    """取第 s 板分桶；相邻空桶就近借样（±1）。"""
    if stats.get(s) and stats[s]["n"] >= _MIN_N:
        return stats[s]
    for d in (1, -1, 2, -2):
        b = stats.get(s + d)
        if b and b["n"] >= _MIN_N * 0.6:
            return b
    return stats.get(s)


def expected_top(s, stats):
    """预期板数区间：由期望涨幅折算『还能再走几个板』（10% 一板近似）。"""
    b = _bucket(stats, s) or {}
    prior = _PRIOR.get(s, {"exp": 3.0, "reach10": 0.25, "days": 2})
    n = b.get("n") or 0
    if n >= _MIN_N:
        exp = b["exp"] * _BUCKET_BLEND + prior["exp"] * (1 - _BUCKET_BLEND)
        days = b.get("days_med") or prior["days"]
    else:
        exp, days = prior["exp"], prior["days"]
    boards_more = max(exp / 9.8, 0.15)   # 期望涨幅折板数
    lo = max(int(boards_more), 0)
    hi = lo + (1 if boards_more - lo >= 0.45 else 1)
    return {"more_lo": lo, "more_hi": hi,
            "exp_ret": round(exp, 1), "hold_days": int(max(days, 1))}


def plan(u, code, s, close, stats=None):
    """生成一只票的连板交易计划。

    close：当前涨停价（=明日参考基准）。
    buy_zone：次日不低开时可追的区间 [close*1.0, close*1.07]（回测：高开过深性价比降）
              若昨日预测 low_open 概率高则左端收紧——此处保持静态区间，竞价纪律(recveto.G1)动态否决。
    t1/sell_zone：expected 折算；stop：-8%（回测止损口径）。
    """
    st = stats or {}
    et = expected_top(s, st)
    b = _bucket(st, s) or {}
    prior = _PRIOR.get(s, {"exp": 3.0, "reach10": 0.25, "days": 2})
    reach10 = b["reach"].get(10.0, prior["reach10"]) if b else prior["reach10"]
    base = float(close)
    stop = round(base * 0.92, 2)
    t1 = round(base * (1 + min(et["exp_ret"], 10.0) / 100.0), 2)
    t2 = round(base * 1.10, 2) if reach10 >= 0.22 else round(base * 1.06, 2)
    buy_zone = [round(base * 0.995, 2), round(base * 1.03, 2)]   # 微低开~+3% 内都算合格买点
    sell_zone = [t1, t2]
    rr = round((min(t1, base * 1.05) - base) / max(base - stop, 0.01), 2) if stop < base else 0
    ev_n = b.get("n") if b else 0
    return {
        "code": code, "entry_streak": s,
        "expected_top": "%d→%d~%d板" % (s, s + et["more_lo"], s + et["more_hi"]),
        "expected_more": [et["more_lo"], et["more_hi"]],
        "exp_ret": et["exp_ret"], "hold_days": et["hold_days"],
        "buy_zone": buy_zone, "sell_zone": sell_zone, "stop": stop,
        "rr": rr, "reach10": round((reach10 or 0) * 100),
        "sample_n": ev_n or 0,
        "evidence": "近 %d 笔同高度样本·开盘接力中位溢价 %.1f%%" % (
            (ev_n or 0), (b.get("mfe_med") or 0)) if ev_n else "样本不足·按先验",
    }


def scan(u, date, lus, stats=None, topn=12):
    """对当日涨停名单生成次日「机会买入」计划（含 高位但条件允许者——标注不拦）。"""
    st = stats if stats is not None else ladder_stats(u)
    plans = []
    for r in lus or []:
        try:
            p = plan(u, r["code"], r.get("streak", 1), r.get("close"), st)
            p["name"] = r.get("name")
            p["industry"] = r.get("industry")
            p["p_continue"] = r.get("p_continue")
            p["p_break"] = r.get("p_break")
            p["yizi"] = r.get("yizi")
            plans.append(p)
        except Exception:
            continue
    # 排序：期望收益降序 → 盈亏比降序
    plans.sort(key=lambda x: (-x.get("exp_ret", 0), -x.get("rr", 0)))
    return plans[:topn]
