# -*- coding: utf-8 -*-
"""factor_health —— 因子健康度看板（每日 IC 快照，纯标准库，无未来函数）。

目的（2026-09-04 用户确认的短期升级1）：
  选股引擎里用了不少因子（Kronos 结构分、5日动量、量价健康度、低波动……），
  但因子有效性会漂移——某个因子近一个月可能已经「失效」甚至反向。
  本模块每天收盘后对核心因子做一次截面 IC 体检，把「因子还灵不灵」变成可看的数据。

方法（严谨、无未来函数）：
  - 对最近 lookback 个交易日中的每一天 d：
      * 截面：取样本股在 d 日的因子值（只用 d 及以前的 K 线计算）
      * 结果：d+1 收盘 → d+1+h 收盘 的真实前向收益（h=horizon，通常 5 日）
      * 该日 IC = Spearman(因子值, 前向收益)
  - 只有「d+h 也已有真实 K 线」的因子日才计入 → IC 序列天然滞后 h 天，这是诚实的代价。
  - 样本：从全市场（有足够 K 线的票）确定性等距抽样 sample_n 只，保证 CI 上 CPU 可控。

输出（data["factor_health"]）：
  {date, horizon, lookback, n_stocks, factors: [
      {name, label, ic_mean, ic_std, icir, hit_rate, series: [{date, ic, n}]}
  ], verdict}

判定口径（icir = ic_mean/ic_std）：
  |icir| >= 0.5 有效；0.2~0.5 弱有效；< 0.2 失效（给出方向提示）。
"""
from __future__ import annotations

import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _rank(xs):
    """平均秩（处理并列）。"""
    n = len(xs)
    idx = sorted(range(n), key=lambda i: xs[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and xs[idx[j + 1]] == xs[idx[i]]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[idx[k]] = r
        i = j + 1
    return ranks


def spearman(xs, ys):
    """Spearman 秩相关；样本 <8 或零方差返回 None。"""
    n = len(xs)
    if n < 8 or n != len(ys):
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sxx = syy = 0.0
    for a, b in zip(rx, ry):
        cov += (a - mx) * (b - my)
        sxx += (a - mx) ** 2
        syy += (b - my) ** 2
    if sxx <= 0 or syy <= 0:
        return None
    return cov / math.sqrt(sxx * syy)


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


# ----------------------------------------------------------------------------
# 因子定义：每个因子 = 函数(bars前缀) -> float 或 None（None=当日无值，跳过）
# bars 元素: {d,o,h,l,c,v} 升序
# ----------------------------------------------------------------------------
def _f_mom5(bs):
    c = bs[-1]["c"]
    if len(bs) < 6 or not c or c <= 0 or not bs[-6]["c"]:
        return None
    return c / bs[-6]["c"] - 1


def _f_vol5(bs):
    if len(bs) < 8:
        return None
    rets = [bs[i]["c"] / bs[i - 1]["c"] - 1 for i in range(len(bs) - 5, len(bs))
            if bs[i - 1]["c"] > 0]
    if len(rets) < 4:
        return None
    return _std(rets)          # 低波动因子：IC 为负才健康（解释里说明）


def _f_pv(bs):
    try:
        from kronos_lite import kronos_features
        f = kronos_features(bs[-30:])
        return (f or {}).get("pv_health")
    except Exception:
        return None


def _f_kronos(bs):
    try:
        from kronos_lite import kronos_features, kronos_score
        return kronos_score(kronos_features(bs[-30:]))
    except Exception:
        return None


FACTORS = [
    ("kronos", "Kronos结构分", _f_kronos, 1),
    ("mom5", "5日动量", _f_mom5, 1),
    ("pv", "量价健康度", _f_pv, 1),
    ("vol5", "5日波动(低波)", _f_vol5, -1),   # 期望负 IC：越低波未来越好
]


def _sample_codes(u, date, min_bars, sample_n):
    """确定性等距抽样：code 升序后每隔 k 取 1，保证每次构建样本一致。"""
    pool = []
    for code, bars in u.bars.items():
        if not bars or bars[-1]["d"] > date:
            # 只取在 date 当日（或之前最近）有K线的票
            bars = [b for b in bars if b["d"] <= date]
        if len(bars) < min_bars:
            continue
        pool.append((code, bars))
    pool.sort(key=lambda x: x[0])
    if not pool:
        return []
    if len(pool) <= sample_n:
        return pool
    step = len(pool) / float(sample_n)
    return [pool[int(i * step)] for i in range(sample_n)]


def compute(u, date, lookback=20, horizon=5, sample_n=350, min_bars=60):
    """主入口：返回因子健康度 dict（异常时不抛出，返回带 error 的最小 dict）。"""
    try:
        return _compute(u, date, lookback, horizon, sample_n, min_bars)
    except Exception as e:
        return {"date": date, "error": "%r" % e, "factors": []}


def _compute(u, date, lookback, horizon, sample_n, min_bars):
    # 交易日历：优先 sh000001（与 store/trade_calendar 口径一致），否则用样本股日期并集
    ref = u.bars.get("sh000001") or u.bars.get("sz399001") or []
    cal = [b["d"] for b in ref if b["d"] <= date]
    sample = _sample_codes(u, date, min_bars, sample_n)
    if not cal and sample:
        alld = set()
        for _, bars in sample[:50]:
            alld.update(b["d"] for b in bars)
        cal = sorted(alld)
    if len(cal) < lookback + horizon + 2:
        return {"date": date, "error": "交易日不足(%d)" % len(cal), "factors": []}

    # 评估日 = 最近 lookback 个「其后 h 天也已有真实K线」的交易日
    last = len(cal) - 1 - horizon          # d+h 必须有K线 → d 最早只能是 len-1-h
    eval_days = cal[max(0, last - lookback + 1): last + 1]
    fwd_of = {d: cal[cal.index(d) + horizon] for d in eval_days}

    # 每个因子：{eval_date: [(factor, fwd_ret), ...]}
    acc = {name: {d: [] for d in eval_days} for name, _, _, _ in FACTORS}
    for code, bars in sample:
        closes = {b["d"]: b["c"] for b in bars}
        # 预先切片：避免每日重复过滤
        for d in eval_days:
            prefix = [b for b in bars if b["d"] <= d]
            if len(prefix) < 30:
                continue
            fd = fwd_of.get(d)
            fc = closes.get(fd)
            if not fc or not prefix[-1]["c"]:
                continue
            fwd = fc / prefix[-1]["c"] - 1
            for name, _label, fn, _sign in FACTORS:
                try:
                    v = fn(prefix)
                except Exception:
                    v = None
                if v is not None:
                    acc[name][d].append((v, fwd))

    factors_out = []
    for name, label, _fn, sign in FACTORS:
        series = []
        ics = []
        for d in eval_days:
            pairs = acc[name][d]
            ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            if ic is None:
                continue
            ics.append(ic)
            series.append({"date": d, "ic": round(ic, 4), "n": len(pairs)})
        if not ics:
            continue
        ic_mean = sum(ics) / len(ics)
        ic_std = _std(ics) or 1e-9
        icir = ic_mean / ic_std
        # hit_rate：IC 方向与「健康方向」一致的比例（低波因子期望负IC）
        if sign >= 0:
            hits = sum(1 for x in ics if x > 0) / len(ics)
        else:
            hits = sum(1 for x in ics if x < 0) / len(ics)
        aicir = abs(icir)
        if aicir >= 0.5:
            status = "有效"
        elif aicir >= 0.2:
            status = "弱有效"
        else:
            status = "失效"
        factors_out.append({
            "name": name, "label": label,
            "ic_mean": round(ic_mean, 4), "ic_std": round(ic_std, 4),
            "icir": round(icir, 3), "hit_rate": round(hits, 3),
            "status": status, "series": series,
        })

    # 总体结论
    valid = sum(1 for f in factors_out if f["status"] != "失效")
    verdict = ("因子整体健康：%d/%d 个因子有效" % (valid, len(factors_out))
               if valid >= max(2, len(factors_out) - 1)
               else "因子出现漂移：仅 %d/%d 个有效，排序权重建议保守" % (valid, len(factors_out)))
    return {
        "date": date, "horizon": horizon, "lookback": len(eval_days),
        "n_stocks": len(sample),
        "note": "IC 为 Spearman 截面秩相关，结果滞后 %d 个交易日（前向收益需要已实现）" % horizon,
        "factors": factors_out, "verdict": verdict,
    }
