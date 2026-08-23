# -*- coding: utf-8 -*-
"""筹码分布 / 获利盘比例估计。

方法：取近 LOOKBACK 日日K，将每日成交量均匀分布在当日 [low, high] 价格区间，
并按「换手率衰减」给旧筹码降权（越久远的筹码被换掉的概率越高），
累加得到价格区间成交量分布直方图；收盘价之下的量占比即「获利盘比例」。
局限：假设日内成交均匀 + 指数衰减近似换手过程，属一阶近似，仅作参考。
"""
LOOKBACK = 120
NBINS = 60          # 价格直方格数
HALF_LIFE = 60      # 换手半衰期（交易日）：60日前筹码权重减半
TOP = 10


def _weights(n):
    """旧->新 权重序列，半衰期 HALF_LIFE。"""
    return [0.5 ** ((n - 1 - i) / HALF_LIFE) for i in range(n)]


def dist_ratio(bars):
    """bars 为 engine bar dict 列表（升序）。返回获利盘比例 [0,1] 或 None。"""
    bars = [b for b in bars if b.get("l") and b.get("h") and b["l"] > 0 and b["h"] >= b["l"]]
    if len(bars) < LOOKBACK // 2:
        return None
    close = float(bars[-1]["c"])
    lo = min(float(b["l"]) for b in bars)
    hi = max(float(b["h"]) for b in bars)
    if hi <= lo or hi <= 0:
        return None
    w = _weights(len(bars))
    hist = [0.0] * NBINS
    for b, wt in zip(bars, w):
        bl, bh, vol = float(b["l"]), float(b["h"]), float(b.get("v") or 0)
        if bh <= bl or vol <= 0:
            continue
        i0 = min(NBINS - 1, max(0, int((bl - lo) / (hi - lo) * NBINS)))
        i1 = min(NBINS - 1, max(0, int((bh - lo) / (hi - lo) * NBINS)))
        per = vol * wt / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            hist[i] += per
    cut = min(NBINS - 1, max(0, int((close - lo) / (hi - lo) * NBINS)))
    total = sum(hist)
    if total <= 0:
        return None
    return sum(hist[:cut + 1]) / total


def scan(u, date, limit=TOP):
    """u 为 engine.Universe。返回 {date,n,avg,top_low,top_high}。"""
    rows = []
    di = u.di.get(date)
    if di is None:
        return {'date': date, 'n': 0, 'avg': 0.0, 'top_low': [], 'top_high': []}
    for code, s in u.stocks.items():
        bs = [b for b in u.bars.get(code, []) if b["d"] <= date][-LOOKBACK:]
        if not bs or bs[-1]["d"] != date:
            continue
        r = dist_ratio(bs)
        if r is None:
            continue
        pct = bs[-1].get("pct") or 0.0
        rows.append({'code': code, 'name': s['name'], 'ratio': round(r, 4),
                     'close': round(float(bs[-1]['c']), 2), 'pct': round(pct, 2)})
    rows.sort(key=lambda x: x['ratio'])
    avg = round(sum(x['ratio'] for x in rows) / len(rows), 4) if rows else 0.0
    return {
        'date': date,
        'n': len(rows),
        'avg': avg,
        'top_low': rows[:limit],       # 获利盘最低：套牢盘沉重/超跌区
        'top_high': rows[-limit:],     # 获利盘最高：注意兑现风险
    }


if __name__ == '__main__':
    import store as _store
    import engine
    import json
    con = _store.connect()
    u = engine.Universe(con, days=LOOKBACK + 30)
    res = scan(u, u.dates[-1])
    print(json.dumps(res, ensure_ascii=False, indent=2))
