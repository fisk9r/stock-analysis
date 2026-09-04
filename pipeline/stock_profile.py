# -*- coding: utf-8 -*-
"""stock_profile —— 个股深度档案（短期升级3：点击股票名弹出完整档案，纯标准库）。

为持仓 + 关注 + 推荐买点候选生成统一档案 data["stock_profiles"][code]：
  基础：名称/板块/现价/涨跌/市值区间
  技术：MA5/10/20/60、距60日高点回撤、距60日低点空间、20日波幅、量能分位
  结构：Kronos 特征与结构分、小时级成本锚（multitime，可选）
  操作：zones.analyze_one 操作结论（持仓带成本才有意义，关注票给买区）
  波段：bandtrade 阶段底信息（若有）
前端 app.js：点股票名 → openStockProfile(code) 模态弹窗；另附搜索框。

生成数量控制：只对感兴趣的 codes 生成（通常 ≤ 40 只），CPU 可控。
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _sma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / float(n)


def _last_close(bars):
    return bars[-1]["c"] if bars else None


def build_profile(code, name, bars, cost=None, zone_result=None, m60=None,
                  board=None, kronos=None):
    """纯函数：给一只票的 bars（截至 date）生成档案 dict。bars < 30 → 精简档案。"""
    closes = [b["c"] for b in bars if b.get("c")]
    close = _last_close(bars) or (zone_result or {}).get("close")
    prof = {
        "code": code, "name": name or code, "board": board or "—",
        "close": close, "cost": cost,
        "n_bars": len(bars),
    }
    if cost and close:
        prof["pnl"] = round((close / cost - 1) * 100, 2)

    if len(closes) < 30:
        prof["note"] = "K线不足30根，档案精简"
        return prof

    hi60 = max(b["h"] for b in bars[-60:] if b.get("h"))
    lo60 = min(b["l"] for b in bars[-60:] if b.get("l"))
    vols = [b.get("v") or 0 for b in bars[-20:]]
    vol_rank = None
    if vols and vols[-1] and len(vols) >= 10:
        below = sum(1 for v in vols if v <= vols[-1])
        vol_rank = round(below / len(vols) * 100)

    ma5, ma10, ma20, ma60 = (_sma(closes, n) for n in (5, 10, 20, 60))
    ret5 = closes[-1] / closes[-6] - 1 if len(closes) >= 6 else None
    ret20 = closes[-1] / closes[-21] - 1 if len(closes) >= 21 else None

    prof.update({
        "ma5": round(ma5, 2) if ma5 else None,
        "ma10": round(ma10, 2) if ma10 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "hi60": hi60, "lo60": lo60,
        "dd_from_hi60": round((close / hi60 - 1) * 100, 2) if (close and hi60) else None,
        "room_to_lo60": round((lo60 / close - 1) * 100, 2) if (close and lo60) else None,
        "ret5": round(ret5 * 100, 2) if ret5 is not None else None,
        "ret20": round(ret20 * 100, 2) if ret20 is not None else None,
        "vol_rank20": vol_rank,
    })
    # 均线形态
    if ma5 and ma10 and ma20:
        prof["ma_shape"] = ("多头排列" if ma5 > ma10 > ma20
                            else "空头排列" if ma5 < ma10 < ma20 else "纠缠")

    if kronos is None:
        try:
            from kronos_lite import kronos_features, kronos_score
            f = kronos_features(bars[-30:])
            kronos = {"score": kronos_score(f),
                      "entropy": f.get("pattern_entropy"),
                      "pv": f.get("pv_health")}
        except Exception:
            kronos = None
    if kronos:
        prof["kronos"] = kronos

    if zone_result:
        keep = ("action", "rotate", "buy_zone", "sell_zone", "stop", "horizon",
                "targets", "time_status", "reasons", "entry_state",
                "now_zone", "pull_zone")
        prof["zone"] = {k: zone_result.get(k) for k in keep if zone_result.get(k) is not None}

    if m60:
        prof["m60"] = m60

    return prof


def collect(u, date, codes, costs=None, boards=None, zone_results=None,
            m60_map=None, kronos_map=None, max_n=40):
    """批量生成 {code: profile}。codes: 去重后的关注代码列表。"""
    out = {}
    seen = []
    for c in codes:
        c = str(c)
        if c not in seen:
            seen.append(c)
    for code in seen[:max_n]:
        bars = [b for b in (u.bars.get(code) or []) if b["d"] <= date]
        name = (u.stocks.get(code, {}) or {}).get("name") or code
        out[code] = build_profile(
            code, name, bars,
            cost=(costs or {}).get(code),
            zone_result=(zone_results or {}).get(code),
            m60=(m60_map or {}).get(code),
            board=(boards or {}).get(code),
            kronos=(kronos_map or {}).get(code))
    return out
