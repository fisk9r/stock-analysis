# -*- coding: utf-8 -*-
"""买卖区间引擎：为关注股/推荐池计算 买入区间、卖出区间、止损位与每日操作提示。

区间构成（全部来自本地日K + 已有缠论引擎，无需外部接口）：
- 支撑参考集 = 缠论中枢下沿、MA20、MA60、近20日最低价
    关键支撑 = 现价「下方最近」的支撑；全在上方时取最低者
- 压力参考集 = 缠论中枢上沿、近60日最高价、现价上方的 MA60
    关键压力 = 现价「上方最近」的压力；全在下方时取最高者
- 买入区间 = [关键支撑×0.985, 关键支撑×1.02]
- 卖出区间 = [关键压力×0.98, 关键压力×1.03]
- 止损位   = min(买入区间下沿, 近10日最低)

每日操作提示（action，urgent 标记需立即注意）：
- 破位卖出：收盘跌破买入区间下沿且收于 MA20 之下（趋势+区间双破）→ urgent
- 跌破警示：收盘 < MA20 但仍在买入区间上沿之上
- 加仓提示：价格回落进入买入区间，且缩量企稳（量比≤1）或缠论二买/三买 → urgent
- 逼近卖出：收盘进入卖出区间（≥ sell_lo）
- 突破持有：放量站上卖出区间上沿（让利润奔跑，但记录止盈线已抬升）
- 正常持有：其余

纯标准库 + 复用 pipeline.chanlun。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chanlun  # noqa: E402

ACTION_URGENT = ("破位卖出", "加仓提示")


def _sma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / float(n)


def _pick_level(cands, close, direction, min_gap=0.02):
    """从候选位中选关键位：要求与现价至少相距 min_gap。

    direction="below" 取现价下方最近者；无合格者时回退为全部候选的最低值。
    direction="above" 同理取上方最近者；无合格者时回退为最高值。
    返回 (tag, value)；value 永不为 None（候选集已过滤）。
    """
    vals = [(t, float(v)) for t, v in cands if v]
    if not vals:
        return ("外推", close)
    if direction == "below":
        ok = [(t, v) for t, v in vals if v <= close * (1 - min_gap)]
        if ok:
            return max(ok, key=lambda x: x[1])
        t, v = min(vals, key=lambda x: x[1])
        return (t + "(已破)", v)
    ok = [(t, v) for t, v in vals if v >= close * (1 + min_gap)]
    if ok:
        return min(ok, key=lambda x: x[1])
    t, v = max(vals, key=lambda x: x[1])
    return (t + "(已越)", v)


def analyze_one(code, name, bars, cost=None):
    """bars: [{d,o,h,l,c,v,...}] 升序；cost: 持仓成本价（可选，带出盈亏提示）。
    返回区间字典或 None（数据不足）。"""
    if not bars or len(bars) < 40 or not bars[-1].get("c"):
        return None
    cur = bars[-1]
    close = float(cur["c"])
    closes = [float(b["c"]) for b in bars]
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    # 结构性低点：剔除最近 5 根，避免「最低点跟着暴跌价走」导致破位永不触发
    struct_low = min(float(b["l"]) for b in bars[max(0, len(bars) - 45):-5])
    low10 = min(float(b["l"]) for b in bars[-10:])
    # 结构性高点：剔除最近 2 根，给创新高的股票留出压力投影空间
    struct_high = max(float(b["h"]) for b in bars[max(0, len(bars) - 60):-2])
    vols = [float(b.get("v") or 0) for b in bars]
    v5 = sum(vols[-6:-1]) / 5 if sum(vols[-6:-1]) else 0
    vol_ratio = (vols[-1] / v5) if v5 else None

    # 缠论中枢（可选增强，失败不影响区间生成）
    zs_up = zs_low = None
    buy_signal = None
    try:
        r = chanlun.analyze(code, bars)
        if r:
            if r.get("zhongshu"):
                zs_up, zs_low = r["zhongshu"][0], r["zhongshu"][1]
            if r.get("signal") in ("二买", "三买"):
                buy_signal = r["signal"]
    except Exception:
        pass

    # ---- 关键支撑 / 关键压力 ----
    sup_cands = [("结构低点", struct_low), ("MA20", ma20)]
    if ma60:
        sup_cands.append(("MA60", ma60))
    if zs_low:
        sup_cands.append(("中枢下沿", zs_low))
    key_sup_tag, key_sup = _pick_level(sup_cands, close, "below")

    res_cands = [("阶段高点", struct_high)]
    if zs_up:
        res_cands.append(("中枢上沿", zs_up))
    key_res_tag, key_res = _pick_level(res_cands, close, "above")

    buy_lo = round(key_sup * 0.985, 2)
    buy_hi = round(key_sup * 1.02, 2)
    sell_lo = round(key_res * 0.98, 2)
    sell_hi = round(key_res * 1.03, 2)
    stop = round(min(buy_lo, low10), 2)

    # ---- 操作提示 ----
    reasons = []
    if close < buy_lo and ma20 and close < ma20:
        action, urgent = "破位卖出", True
        reasons.append("跌破买入区间下沿 %.2f（关键支撑=%s %.2f 已失守）"
                       % (buy_lo, key_sup_tag, key_sup))
        reasons.append("收于MA20 %.2f 之下" % ma20)
    elif ma20 and close < ma20 and buy_lo <= close <= buy_hi:
        # 回到买区内但仍在均线下方：需缩量企稳或缠论买点确认才算买点
        if (vol_ratio is not None and vol_ratio <= 1.05) or buy_signal:
            action, urgent = "加仓提示", True
            reasons.append("回落进入买入区间 [%.2f, %.2f]" % (buy_lo, buy_hi))
            if vol_ratio is not None and vol_ratio <= 1.05:
                reasons.append("缩量企稳(量比%.2f)" % vol_ratio)
            if buy_signal:
                reasons.append("缠论%s共振" % buy_signal)
        else:
            action, urgent = "跌破警示", False
            reasons.append("收于MA20 %.2f 之下，且未缩量企稳" % ma20)
    elif ma20 and close < ma20:
        action, urgent = "跌破警示", False
        reasons.append("收于MA20 %.2f 之下" % ma20)
        reasons.append("距买入区上沿尚有 %.1f%%" % ((buy_hi / close - 1) * 100))
    elif close >= sell_lo:
        falling = (cur.get("pct") or 0) < 0
        heavy = (vol_ratio or 0) >= 1.5
        if falling or heavy:
            action = "逼近卖出"
            urgent = close >= sell_hi * 0.995
            reasons.append("进入卖出区间 [%.2f, %.2f]" % (sell_lo, sell_hi))
            reasons.append("%s（压力=%s %.2f）"
                           % ("冲高回落" if falling else "放量滞涨(量比%.2f)" % vol_ratio,
                              key_res_tag, key_res))
        elif close > sell_hi:
            action, urgent = "突破持有", False
            reasons.append("温和放量站上卖出区上沿，趋势健康")
            reasons.append("止盈参考线抬升至 %.2f" % sell_lo)
        else:
            action, urgent = "正常持有", False
            reasons.append("贴近卖出区下方 [%.2f, %.2f]，关注量能变化" % (sell_lo, sell_hi))
    elif buy_lo <= close <= buy_hi:
        if (vol_ratio is not None and vol_ratio <= 1.05) or buy_signal:
            action, urgent = "加仓提示", True
            reasons.append("价格处于买入区间 [%.2f, %.2f]" % (buy_lo, buy_hi))
            if vol_ratio is not None and vol_ratio <= 1.05:
                reasons.append("缩量企稳(量比%.2f)" % vol_ratio)
            if buy_signal:
                reasons.append("缠论%s共振" % buy_signal)
        else:
            action, urgent = "回踩买入区", False
            reasons.append("回踩买入区间 [%.2f, %.2f]，等待缩量确认" % (buy_lo, buy_hi))
    else:
        action, urgent = "正常持有", False
        gap_sell = (sell_lo / close - 1) * 100 if close else 0
        gap_buy = (buy_hi / close - 1) * 100 if close else 0
        reasons.append("运行于买区上方、卖区下方")
        reasons.append("距卖点 %.1f%%、回踩买区 %.1f%%" % (gap_sell, -gap_buy))

    # ---- 持仓成本联动（可选）：盈亏与摊薄提示 ----
    cost = float(cost) if cost else None
    pnl_pct = round((close / cost - 1) * 100, 2) if cost and close else None
    if cost:
        if pnl_pct <= -8:
            reasons.insert(0, "成本 %.2f，浮亏 %.1f%%（已超 -8%% 预警线）" % (cost, pnl_pct))
        elif pnl_pct < 0:
            reasons.insert(0, "成本 %.2f，浮亏 %.1f%%；回踩买区可参考摊低" % (cost, pnl_pct))
        else:
            reasons.insert(0, "成本 %.2f，浮盈 %+.1f%%" % (cost, pnl_pct))

    return {
        "code": code, "name": name,
        "close": round(close, 2),
        "pct": round(cur.get("pct") or 0, 2),
        "cost": cost,
        "pnl_pct": pnl_pct,
        "ma20": round(ma20, 2) if ma20 else None,
        "buy_zone": [buy_lo, buy_hi],
        "sup_ref": "%s %.2f" % (key_sup_tag, key_sup),
        "sell_zone": [sell_lo, sell_hi],
        "res_ref": "%s %.2f" % (key_res_tag, key_res),
        "stop": stop,
        "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "chanlun_buy": buy_signal,
        "action": action,
        "urgent": urgent,
        "reasons": reasons,
    }


def scan(u, date, codes=None, extra_names=None, costs=None, top_n=24):
    """对给定代码（默认=关注池）跑区间分析。u 需提供 .bars 与 .stocks。
    costs: {code: 成本价}，可选；有成本者输出盈亏提示。"""
    if codes is None:
        import watchlist
        codes, extra_names = watchlist.load_watch_codes()
    extra_names = extra_names or {}
    costs = costs or {}
    items = []
    for c in codes:
        bs = [b for b in (u.bars.get(c) or []) if b["d"] <= date]
        name = extra_names.get(c) or (u.stocks.get(c, {}) or {}).get("name") or ""
        try:
            r = analyze_one(c, name, bs, cost=costs.get(c))
        except Exception:
            r = None
        if r:
            items.append(r)
    order = {"破位卖出": 0, "加仓提示": 1, "回踩买入区": 2,
             "跌破警示": 3, "逼近卖出": 4, "突破持有": 5, "正常持有": 6}
    items.sort(key=lambda x: (order.get(x["action"], 9), -(x.get("pct") or 0)))
    alerts = {
        "sell": [x for x in items if x["action"] == "破位卖出"],
        "add": [x for x in items if x["action"] in ("加仓提示", "回踩买入区")],
        "take_profit": [x for x in items if x["action"] in ("逼近卖出", "突破持有")],
    }
    return {
        "date": date,
        "n": len(items),
        "items": items[:top_n],
        "alerts": alerts,
        "alert_n": len(alerts["sell"]) + len(alerts["add"]),
    }


def summary_lines(zr):
    """收盘推送用摘要：先急讯后常规。"""
    if not zr:
        return []

    def tag(x):
        """名称后附持仓盈亏（有成本者）。"""
        p = x.get("pnl_pct")
        return "%s(成本%.2f %+.1f%%)" % (x["name"], x["cost"], p) \
            if x.get("cost") and p is not None else x["name"]

    out = []
    al = zr.get("alerts") or {}
    sells = al.get("sell") or []
    adds = al.get("add") or []
    tps = al.get("take_profit") or []
    if sells:
        out.append("🛑 破位卖出：" + "；".join(
            "%s 破 %.2f，止损 %.2f" % (tag(x), x["buy_zone"][0], x["stop"])
            for x in sells[:4]))
    if adds:
        out.append("➕ 加仓提示：" + "；".join(
            "%s 买区 %s~%s" % (tag(x), x["buy_zone"][0], x["buy_zone"][1])
            for x in adds[:4]))
    if tps:
        out.append("🎯 逼近卖点：" + "；".join(
            "%s 卖区 %s~%s" % (tag(x), x["sell_zone"][0], x["sell_zone"][1])
            for x in tps[:4]))
    normal = [x for x in (zr.get("items") or [])
              if x["action"] == "正常持有"][:3]
    if normal and not out:
        out.append("🎯 区间速览：" + "、".join(
            "%s 买%s~%s/卖%s~%s" % (tag(x), x["buy_zone"][0], x["buy_zone"][1],
                                    x["sell_zone"][0], x["sell_zone"][1])
            for x in normal))
    if not out:
        out.append("关注池暂无有效区间数据")
    return out
