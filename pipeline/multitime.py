# -*- coding: utf-8 -*-
"""multitime —— 分钟级多周期成本锚（60分钟线，纯标准库 + 腾讯接口）。

目的（2026-09-04 用户确认的中期升级3）：
  日线 MA5/MA10 成本锚对短线太钝——盘中一次急拉就可能远离日线买区。
  用 60 分钟线的 MA5/MA10/MA20 作为「小时级成本锚」，让买区/止损在盘中也有刻度。

数据源（与全站同源策略：腾讯，CORS 无关——这里是服务端拉取）：
  https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sz002631,m60,,64
  返回 data[code]["m60"] = [[yymmddHHMM, open, close, high, low, volume, ...], ...]

设计约束：
  - 拉取失败/网络异常 → 返回 None（调用方降级到日线锚），绝不让主流程崩。
  - 无第三方依赖：urllib + json。
  - 提供纯函数 compute_anchors(bars60) 可离线单测。
"""
from __future__ import annotations

import json
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def tencent_symbol(code):
    """6位代码 → 腾讯带市场前缀符号（6/9→sh，其余→sz，与全站口径一致）。"""
    code = str(code).strip()
    if code[:1] in ("6", "9", "5"):
        return "sh" + code
    return "sz" + code


def fetch_m60(code, n=64, timeout=8):
    """拉取 60 分钟K线 → [{dt,o,c,h,l,v}]（升序）。失败返回 None。"""
    sym = tencent_symbol(code)
    url = ("https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=%s,m60,,%d"
           % (sym, n))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "ignore")
        js = json.loads(body)
        rows = ((js.get("data") or {}).get(sym) or {}).get("m60") or []
        out = []
        for row in rows:
            try:
                out.append({"dt": str(row[0]),
                            "o": float(row[1]), "c": float(row[2]),
                            "h": float(row[3]), "l": float(row[4]),
                            "v": float(row[5] or 0)})
            except (ValueError, IndexError, TypeError):
                continue
        return out or None
    except Exception:
        return None


def _sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / float(n)


def compute_anchors(bars60, price=None):
    """小时级成本锚（纯函数，可离线测）。

    返回 dict：
      ma5/ma10/ma20 : 小时级均线（None=数据不足）
      pos           : 现价相对小时MA5 的位置（above/below/on，None=未知）
      dist_pct      : 现价距小时MA5 的百分比（正=上方）
      trend         : 小时趋势（multi_up 均线多头 / down 空头 / mixed）
      last_dt       : 最新小时bar时间戳
    """
    if not bars60 or len(bars60) < 5:
        return None
    closes = [b["c"] for b in bars60]
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ref = ma5
    pos = dist = None
    if price and ref:
        dist = round((price / ref - 1) * 100, 2)
        pos = "above" if price > ref else ("below" if price < ref else "on")
    if ma5 and ma10 and ma20:
        trend = "multi_up" if (ma5 >= ma10 >= ma20) else \
                ("down" if (ma5 <= ma10 <= ma20) else "mixed")
    else:
        trend = None
    return {"ma5": round(ma5, 3) if ma5 else None,
            "ma10": round(ma10, 3) if ma10 else None,
            "ma20": round(ma20, 3) if ma20 else None,
            "pos": pos, "dist_pct": dist, "trend": trend,
            "last_dt": bars60[-1]["dt"], "n_bars": len(bars60)}


def anchors_for(code, price=None, n=64):
    """便捷入口：拉取+计算。任何失败返回 None（调用方降级日线）。"""
    bars = fetch_m60(code, n=n)
    if not bars:
        return None
    try:
        return compute_anchors(bars, price=price)
    except Exception:
        return None


def batch_anchors(codes_with_price, max_n=12):
    """批量：只对持仓/关注等小集合使用，限制条数防拖慢构建。
    codes_with_price: [(code, price_or_None), ...] → {code: anchors}"""
    out = {}
    for code, price in (codes_with_price or [])[:max_n]:
        a = anchors_for(code, price=price)
        if a:
            out[str(code)] = a
    return out
