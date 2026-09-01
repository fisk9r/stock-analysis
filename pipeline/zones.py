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

周期标注（短线/中线/长线）+ 多周期目标价 + 时间到期预警（核心增强）：
- 每只股票标注周期：持仓可显式指定 horizon（holdings.json）；否则按波动率/趋势自动建议。
- 三档技术目标价（时间窗）：
    短线目标 = 卖出区间上沿（即时压力）         ~5  交易日
    中线目标 = 当前价 + (60日振幅)×0.618 或前高   ~15 交易日
    长线目标 = 250日高（无则60日高×1.2）或×1.3  ~60 交易日
- 时间状态 time_status（仅对「有建仓锚点且为持仓」的票计算 elapsed 后生效）：
    已达目标：现价≥目标×0.99 → ✅ 可分批止盈
    破位优先：现价≤止损 → 🛑 按周期纪律审视/减仓
    到期未达：elapsed ≥ 时间窗 → ⏰ 建议限期了结/降仓释放资金
    观察中  ：其余 → ⏳ 目标X，剩N天

纯标准库 + 复用 pipeline.chanlun。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chanlun  # noqa: E402

ACTION_URGENT = ("破位卖出", "加仓提示")
# 周期标注：超短线为显式标注（用户 holdings.json 指定），自动建议只出 短线/中线/长线
HORIZONS = ("短线", "超短线", "中线", "长线")
SHORT_HORIZONS = ("短线", "超短线")
# 各周期默认时间窗（交易日）
HORIZON_DAYS = {"短线": 5, "超短线": 3, "中线": 15, "长线": 60}


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


def suggest_horizon(close, ma20, ma60, amp20, slope60, cur_pct):
    """按波动率/趋势自动建议周期（用于无显式标注的票）。"""
    if amp20 is None:
        amp20 = 0.0
    if ma20 and close >= ma20 and (amp20 > 0.18 or (slope60 or 0) > 0.12):
        return "短线"
    if amp20 < 0.09 and abs(slope60 or 0) < 0.05:
        return "长线"
    return "中线"


def compute_targets(close, ma20, slope60, hi60, lo20, hi250, sell_hi):
    """返回三档技术目标价 {周期: {price, days, pct}}。"""
    short = round(sell_hi, 2)
    amp = (hi60 - lo20) if (hi60 and lo20) else close * 0.15
    mid = close + amp * 0.618
    if (slope60 or 0) < 0:  # 弱势只看前高收复
        mid = max(mid, hi60)
    mid = max(mid, hi60 * 1.02) if hi60 else mid
    base = hi250 if hi250 else (hi60 * 1.2 if hi60 else close * 1.2)
    long = max(base, close * 1.3)
    if long < mid:
        long = mid  # 保证长线目标不低于中线目标
    return {
        "短线": {"price": short, "days": HORIZON_DAYS["短线"],
                 "pct": round((short / close - 1) * 100, 1) if close else 0},
        "超短线": {"price": short, "days": HORIZON_DAYS["超短线"],
                   "pct": round((short / close - 1) * 100, 1) if close else 0},
        "中线": {"price": round(mid, 2), "days": HORIZON_DAYS["中线"],
                 "pct": round((mid / close - 1) * 100, 1) if close else 0},
        "长线": {"price": round(long, 2), "days": HORIZON_DAYS["长线"],
                 "pct": round((long / close - 1) * 100, 1) if close else 0},
    }


def _time_status(horizon, targets, close, stop, elapsed):
    """生成时间状态字符串与是否需提醒。elapsed 为 None 时返回 (None, False)。"""
    if elapsed is None:
        return (None, False)
    t = targets.get(horizon) or {}
    th = t.get("price")
    dh = t.get("days")
    if not th or not dh:
        return (None, False)
    if close >= th * 0.99:
        return ("✅ 已达%s目标 %.2f，可分批止盈" % (horizon, th), True)
    if stop is not None and close <= stop * 1.005:
        return ("🛑 已破位（%.2f≤止损%.2f），按%s纪律应审视/减仓"
                % (close, stop, horizon), True)
    if elapsed >= dh:
        return ("⏰ %s预期到期（持有%d天）：目标%.2f未达，建议限期了结/降仓释放资金"
                % (horizon, elapsed, th), True)
    return ("⏳ %s观察中：目标%.2f（剩%d天），现价%.2f"
            % (horizon, th, dh - elapsed, close), False)


def _volatility(bars, close, n=8):
    """近 n 日 的振幅(range) 与 净漂移(drift)，返回 (range_pct, drift_pct)。"""
    w = bars[-n:]
    if len(w) < 3 or not close:
        return None, None
    hi = max(float(b.get("h") or b["c"]) for b in w)
    lo = min(float(b.get("l") or b["c"]) for b in w)
    rng = (hi - lo) / close
    drift = abs(float(w[-1]["c"]) - float(w[0]["c"])) / close
    return rng, drift


def _trend_state(close, ma20, ma60, slope60):
    """粗判趋势方向：'down' / 'up' / 'side'，供中长线割肉判定。"""
    if ma20 and ma60 and ma20 < ma60:
        return "down"
    if (slope60 or 0) < -0.04 and ma20 and close < ma20:
        return "down"
    if (slope60 or 0) > 0.04 and ma20 and close > ma20:
        return "up"
    return "side"


def detect_zhuiban(code, bars):
    """检测「追板回落」：近 3 个交易日曾触及涨停（炸板）但大幅回落。返回 dict 或 None。

    主板 limit=10%、双创(300/301/302/688/689) limit=20%、北交所(8/4/920) limit=30%（自适应）。
    命中条件：当日最高价 >= 涨停价×0.995（触板），且 收盘 < 涨停价×0.985 或 自高点回落 ≥5%。
    这是「追板资金被套」的强离场信号，尤其对短线/超短线。
    """
    if not bars or len(bars) < 2:
        return None
    code = str(code or "")
    if code.startswith(("300", "301", "302", "688", "689")):
        LIM = 0.20
    elif code.startswith(("8", "4", "920")):
        LIM = 0.30
    else:
        LIM = 0.10
    # 取最近 3 根（含当前），从近到远找第一次触板回落；取最近一次命中
    for i in range(len(bars) - 1, max(-1, len(bars) - 4), -1):
        b = bars[i]
        if i < 1:
            break
        pc = float(bars[i - 1]["c"])
        if pc <= 0:
            continue
        hi = float(b["h"]); cl = float(b["c"])
        if hi <= 0 or cl <= 0:
            continue
        limit_up = round(pc * (1 + LIM), 2)
        if hi < limit_up * 0.995:   # 未触板
            continue
        from_high = (hi - cl) / hi
        fallback = (limit_up - cl) / limit_up
        if cl < limit_up * 0.985 or from_high >= 0.05:
            return {"date": b.get("d"), "limit_up": limit_up,
                    "close": round(cl, 2),
                    "fallback_pct": round(fallback * 100, 1),
                    "from_high_pct": round(from_high * 100, 1)}
    return None


def band_levels(bars, cost=None, vol_ratio=None):
    """轻量波段区间：给定日K（≥20 根），返回买入区/卖出区/止损 + 操作建议。

    用于：① 趋势票（无成本）给「回踩买 / 反弹卖」波段价；② 持仓（有成本）给
    卖出建议（止盈/止损/持有）。不调用缠论，开销小，可批量跑全市场。

    返回 dict 或 None（数据不足）。字段：
      close, ma20, ma60, buy_zone[lo,hi], sell_zone[lo,hi], stop,
      advice（中文操作建议）, band_action（波段动作枚举）。
    """
    if not bars or len(bars) < 20 or not bars[-1].get("c"):
        return None
    cur = bars[-1]
    close = float(cur["c"])
    closes = [float(b["c"]) for b in bars]
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    struct_low = min(float(b["l"]) for b in bars[max(0, len(bars) - 45):-5])
    struct_high = max(float(b["h"]) for b in bars[max(0, len(bars) - 60):-2])
    lo10 = min(float(b["l"]) for b in bars[-10:])
    lo20 = min(float(b["l"]) for b in bars[-20:]) if len(bars) >= 2 else close
    hi60 = max(float(b["h"]) for b in bars[-60:]) if len(bars) >= 2 else close
    low10 = min(float(b["l"]) for b in bars[-10:])

    sup_cands = [("结构低点", struct_low), ("MA20", ma20)]
    if ma60:
        sup_cands.append(("MA60", ma60))
    key_sup_tag, key_sup = _pick_level(sup_cands, close, "below")
    res_cands = [("阶段高点", struct_high)]
    key_res_tag, key_res = _pick_level(res_cands, close, "above")

    buy_lo = round(key_sup * 0.985, 2)
    buy_hi = round(key_sup * 1.02, 2)
    sell_lo = round(key_res * 0.98, 2)
    sell_hi = round(key_res * 1.03, 2)
    stop = round(min(buy_lo, low10), 2)

    # ---- 波段操作建议 ----
    if cost is not None and close:
        pnl = (close / float(cost) - 1) * 100
        if close <= stop:
            advice, band_action = "止损离场（已破位，按纪律执行）", "止损"
        elif close >= sell_hi:
            advice, band_action = "止盈减仓（突破卖出区上沿，落袋为安）", "止盈"
        elif close >= sell_lo:
            advice, band_action = "分批止盈（进入卖出区，锁定利润）", "止盈"
        elif close <= buy_hi and pnl < 15:
            advice, band_action = "回踩买入区，可逢低摊低/加仓", "加仓"
        else:
            advice, band_action = "区间内持有（趋势未破，等待方向）", "持有"
    else:
        if close >= sell_lo:
            advice, band_action = "波段卖点：反弹至卖出区可高抛", "卖点"
        elif close <= buy_hi:
            advice, band_action = "波段买点：回踩买入区可低吸", "买点"
        else:
            advice, band_action = "区间内持有，等回踩买/冲高卖", "持有"

    # ---- 三档网格：把买/卖区各拆成 3 档价位 + 仓位点，便于分批建仓/减仓 ----
    def _grid(lo, hi, ratios):
        if hi <= lo:
            return [{"price": round(lo, 2), "ratio": round(sum(ratios), 3)}]
        pts = [lo + (hi - lo) * (i / (len(ratios))) for i in range(len(ratios))]
        return [{"price": round(p, 2), "ratio": round(r, 3)}
                for p, r in zip(pts, ratios)]
    grid_buy = _grid(buy_lo, buy_hi, [0.40, 0.35, 0.25])    # 越低买得越多
    grid_sell = _grid(sell_lo, sell_hi, [0.30, 0.35, 0.35])  # 越高卖得越多

    return {
        "close": round(close, 2),
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "buy_zone": [buy_lo, buy_hi],
        "sell_zone": [sell_lo, sell_hi],
        "grid_buy": grid_buy,
        "grid_sell": grid_sell,
        "stop": stop,
        "sup_ref": "%s %.2f" % (key_sup_tag, key_sup),
        "res_ref": "%s %.2f" % (key_res_tag, key_res),
        "advice": advice,
        "band_action": band_action,
    }


def analyze_one(code, name, bars, cost=None, horizon=None, elapsed=None,
                replace_pool=None, exclude=None, industries=None):
    """bars: [{d,o,h,l,c,v,...}] 升序；cost: 持仓成本价（可选）；
    horizon: 显式周期（"短线"/"中线"/"长线"，可选，缺省自动建议）；
    elapsed: 建仓锚点起已持有交易日数（可选，用于时间到期预警）。

    返回区间字典或 None（数据不足）。新增字段：
      horizon, targets{短/中/长}, elapsed, time_status, time_alert。
    """
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

    # 目标位所需的高/低/振幅
    hi60 = max(float(b["h"]) for b in bars[-60:]) if len(bars) >= 2 else close
    lo20 = min(float(b["l"]) for b in bars[-20:]) if len(bars) >= 2 else close
    hi250 = max(float(b["h"]) for b in bars[-250:]) if len(bars) >= 250 else None
    amp20 = ((max(float(b["h"]) for b in bars[-20:]) -
              min(float(b["l"]) for b in bars[-20:])) / close) if close else 0
    slope60 = ((ma20 - ma60) / ma60) if (ma20 and ma60) else 0

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

    # ---- 周期标注 + 多周期目标价 + 时间到期预警 ----
    horizon_explicit = horizon in HORIZONS
    final_horizon = horizon if horizon_explicit else suggest_horizon(
        close, ma20, ma60, amp20, slope60, cur.get("pct") or 0)
    targets = compute_targets(close, ma20, slope60, hi60, lo20, hi250, sell_hi)
    # 时间到期预警仅对「持仓」（有成本 或 显式标注周期）生效，
    # 避免对纯关注股误发「了结/降仓」建议。
    if elapsed is not None and (cost is not None or horizon_explicit):
        time_status, time_alert = _time_status(final_horizon, targets, close, stop, elapsed)
    else:
        time_status, time_alert = None, False
    if time_alert and time_status:
        reasons.append(time_status)

    # ---- 关注股优化提示（离场/更换/止损/割肉 + 更换建议）----
    # 你最关心的「跟着做」：短线不浮动/波动小→离场换更强；已破位→止损；
    # 中长线趋势向下→割肉。破位优先于一切；止损/更换/割肉时附强势备选池 Top3。
    rotate = None
    rotate_reason = ""
    replace = []
    if action == "破位卖出":
        rotate = "止损"
        rotate_reason = "已跌破关键支撑且收于MA20之下，应果断止损离场、避免深套"
    elif final_horizon == "短线":
        rng8, drift8 = _volatility(bars, close, 8)
        hi8 = max(float(b["h"]) for b in bars[-8:])
        # 近8日振幅≤6%（波动小）或 净漂移≤2%（不浮动）；仍在上升蓄势（净移>3%且贴近高点）则不算死水
        is_stagnant = ((rng8 is not None and rng8 < 0.06)
                       or (drift8 is not None and drift8 < 0.02))
        still_strong = (drift8 or 0) > 0.03 and close >= hi8 * 0.97
        if is_stagnant and not still_strong:
            rotate = "更换"
            rotate_reason = ("短线标的近8日振幅%.1f%%、净移%.1f%%，原地踏步无弹性，"
                             "建议离场换更强标的" % (rng8 * 100 if rng8 else 0,
                                                   drift8 * 100 if drift8 else 0))
    else:  # 中线/长线
        if _trend_state(close, ma20, ma60, slope60) == "down":
            rotate = "割肉"
            rotate_reason = ("中长线趋势已向下（MA20<MA60 或 斜率%.0f%%），"
                             "应止损割肉控制回撤，待重新走平再加回" % ((slope60 or 0) * 100))
    # 更换建议：仅当发出离场/止损/割肉时，从强势备选池挑 Top3（排除自身与关注池）。
    # 2026-09-01 用户需求：卖出建议要结合板块给更换标的（可连板可趋势）——
    # 候选排序改为「同板块优先，再按强度分」；候选票的行业/连板高度一并带上，
    # 买卖区间由 scan() 统一用 band_levels 补齐（此处无 u.bars 访问权）。
    if rotate in ("止损", "更换", "割肉") and replace_pool:
        ex = set(exclude or [])
        ex.add(code)
        # 本票所属板块：优先 industries 映射（code2boards），候选池兜底
        _my_ind = (industries or {}).get(code) or \
            next((x.get("industry") for x in (replace_pool or [])
                  if x.get("code") == code), None)
        cands = [x for x in (replace_pool or [])
                 if x.get("code") and x["code"] not in ex
                 and (x.get("worth_score") or 0) > 0]
        # 同板块优先（二级排序：worth_score 降序）
        cands.sort(key=lambda x: (1 if (_my_ind and x.get("industry") == _my_ind) else 0,
                                  x.get("worth_score") or 0), reverse=True)
        replace = [{"code": c["code"], "name": c.get("name") or "",
                    "score": c.get("worth_score"),
                    "industry": c.get("industry"),
                    "streak": c.get("streak") or 0,
                    "p_continue": c.get("p_continue")} for c in cands[:3]]

    # ---- 追板回落检测：触板后炸板回落 → 短线/超短线当日离场 ----
    zhuiban = detect_zhuiban(code, bars)
    if zhuiban:
        reasons.append("⚠ 追板回落：%s 触及涨停(限价%.2f)后回落，收%.2f（较涨停-%s%%、自高点-%s%%），追板资金被套"
                       % (zhuiban["date"], zhuiban["limit_up"], zhuiban["close"],
                          zhuiban["fallback_pct"], zhuiban["from_high_pct"]))
        if final_horizon in SHORT_HORIZONS:
            # 短线追板资金被套=当日离场信号：盖过停滞/割肉，仅次于已破位
            rotate = "止损"
            rotate_reason = ("追板回落：%s 涨停炸板收%.2f（较涨停-%s%%），"
                             "短线追板资金被套，盘前宜果断离场"
                             % (zhuiban["date"], zhuiban["close"], zhuiban["fallback_pct"]))

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
        # ---- 新增：周期 / 多目标 / 时间状态 ----
        "horizon": final_horizon,
        "targets": targets,
        "elapsed": elapsed,
        "time_status": time_status,
        "time_alert": time_alert,
        # ---- 新增：关注股优化提示 ----
        "rotate": rotate,
        "rotate_reason": rotate_reason,
        "replace": replace,
        # ---- 新增：追板回落（触板炸板回落，短线当日离场）----
        "zhuiban": zhuiban,
    }


def scan(u, date, codes=None, extra_names=None, costs=None,
         horizons=None, elapsed_map=None, top_n=30,
         replace_pool=None, exclude_codes=None, code2boards=None):
    """对给定代码（默认=关注池）跑区间分析。u 需提供 .bars 与 .stocks。
    costs: {code: 成本价}，可选；有成本者输出盈亏提示，且**永不截断丢弃**。
    horizons: {code: "短线"/"中线"/"长线"} 显式周期（可选）。
    elapsed_map: {code: int} 建仓锚点起已持有交易日数（可选，用于时间到期预警）。
    replace_pool: [{code,name,worth_score,p_continue,industry,streak}] 强势备选池，
        用于离场/止损/割肉时给出「更换建议」（2026-09-01 起同板块优先，
        且每只候选票补齐 buy_zone/sell_zone/stop——用户需求：卖出建议给更换标的
        的购买区间与卖出区间，可连板可趋势）；
    code2boards: {code: [(板块名, 名, kind), ...]} 行业映射（可选）。
    exclude_codes: 不参与更换建议的代码集合（关注池自身）。"""
    if codes is None:
        import watchlist
        codes, extra_names, _added = watchlist.load_watch_codes()
    extra_names = extra_names or {}
    costs = costs or {}
    horizons = horizons or {}
    elapsed_map = elapsed_map or {}
    exclude_codes = set(exclude_codes or [])
    # 行业映射：code2boards → {code: 行业名}，供更换建议「同板块优先」
    _c2b = code2boards or {}
    industries = {}
    for c in codes:
        industries[c] = next(
            (n for _, n, k in (_c2b.get(c) or []) if k == "industry"), None)
    # 候选池连板高度兜底映射（池条目 streak 缺失时用 u.streak 补）
    _pool_streak = {}
    for x in (replace_pool or []):
        if x.get("code") and x.get("streak") is not None:
            _pool_streak[x["code"]] = x["streak"]
    items = []
    for c in codes:
        bs = [b for b in (u.bars.get(c) or []) if b["d"] <= date]
        name = extra_names.get(c) or (u.stocks.get(c, {}) or {}).get("name") or ""
        try:
            r = analyze_one(c, name, bs, cost=costs.get(c),
                            horizon=horizons.get(c), elapsed=elapsed_map.get(c),
                            replace_pool=replace_pool, exclude=exclude_codes,
                            industries=industries)
        except Exception:
            r = None
        if r:
            # 更换建议候选票补齐买卖区间 + 连板/趋势标签（band_levels 轻量、可批量）
            for rep in (r.get("replace") or []):
                rc = rep.get("code")
                if not rc:
                    continue
                cb = [b for b in (u.bars.get(rc) or []) if b["d"] <= date]
                try:
                    bd = band_levels(cb)
                except Exception:
                    bd = None
                if bd:
                    rep["buy_zone"] = bd.get("buy_zone")
                    rep["sell_zone"] = bd.get("sell_zone")
                    rep["stop"] = bd.get("stop")
                    rep["band_action"] = bd.get("band_action")
                rep["streak"] = rep.get("streak") or \
                    (((getattr(u, "streak", None) or {}).get(rc) or {}).get(date, 0)) or \
                    _pool_streak.get(rc, 0)
                rep["market_type"] = "连板" if (rep.get("streak") or 0) >= 1 else "趋势"
            items.append(r)
    order = {"破位卖出": 0, "加仓提示": 1, "回踩买入区": 2,
             "跌破警示": 3, "逼近卖出": 4, "突破持有": 5, "正常持有": 6}
    # 关注股优化提示（止损/更换/割肉）置顶，确保离场类建议最显眼
    items.sort(key=lambda x: (0 if x.get("rotate") else 1,
                              order.get(x["action"], 9), -(x.get("pct") or 0)))
    # 带持仓成本的股票（用户自选）永远保留，不因 top_n 截断丢弃
    prio = [x for x in items if x.get("cost")]
    others = [x for x in items if not x.get("cost")][:max(0, top_n - len(prio))]
    kept = prio + others
    kept.sort(key=lambda x: (0 if x.get("rotate") else 1,
                             order.get(x["action"], 9), -(x.get("pct") or 0)))
    alerts = {
        "sell": [x for x in items if x["action"] == "破位卖出"],
        "add": [x for x in items if x["action"] in ("加仓提示", "回踩买入区")],
        "take_profit": [x for x in items if x["action"] in ("逼近卖出", "突破持有")],
        "time": [x for x in items if x.get("time_alert")],
        "rotate": [x for x in items if x.get("rotate")],
    }
    return {
        "date": date,
        "n": len(items),
        "items": kept,
        "alerts": alerts,
        "alert_n": len(alerts["sell"]) + len(alerts["add"]) + len(alerts["time"])
        + len(alerts["rotate"]),
    }


def summary_lines(zr):
    """收盘推送用摘要：先急讯后常规。含周期/多目标/时间到期提示。"""
    if not zr:
        return []

    def tag(x):
        """名称后附周期与持仓盈亏（有成本者）。"""
        p = x.get("pnl_pct")
        seg = x["name"]
        if x.get("horizon"):
            seg += "[%s]" % x["horizon"]
        if x.get("cost") and p is not None:
            seg += "(成本%.2f %+.1f%%)" % (x["cost"], p)
        return seg

    def tgt_str(x):
        t = x.get("targets") or {}
        parts = []
        for h in HORIZONS:
            d = t.get(h)
            if d:
                parts.append("%s%.2f(%d日)" % (h[0], d["price"], d["days"]))
        return "/".join(parts)

    out = []
    al = zr.get("alerts") or {}
    sells = al.get("sell") or []
    adds = al.get("add") or []
    tps = al.get("take_profit") or []
    times = al.get("time") or []
    rotates = al.get("rotate") or []
    # 追板回落·离场（短线/超短线追板资金被套，当日走人）——最高优先级，置于段首
    zbs = [x for x in (zr.get("items") or [])
           if x.get("zhuiban") and x.get("horizon") in SHORT_HORIZONS]
    if zbs:
        out.append("🚨 追板回落·离场：" + "；".join(
            "%s[%s] %s炸板收%.2f(较涨停-%s%%)" % (tag(x), x["horizon"], x["zhuiban"]["date"],
                                               x["zhuiban"]["close"], x["zhuiban"]["fallback_pct"])
            for x in zbs[:5]))
    if rotates:
        out.append("🔄 关注股优化（离场/更换/止损/割肉）：" + "；".join(
            "%s[%s] %s：%s" % (tag(x), x.get("horizon"), x["rotate"],
                               (x.get("rotate_reason") or "")[:46])
            for x in rotates[:5]))
        for x in rotates[:3]:
            rp = x.get("replace") or []
            if rp:
                parts = []
                for s in rp:
                    mt = s.get("market_type") or ("连板" if (s.get("streak") or 0) >= 1 else "趋势")
                    mtag = "连板" + ("%d板" % s["streak"] if s.get("streak") else "票") if mt == "连板" else "趋势票"
                    seg = "%s(%s·%s)" % (s.get("name") or s.get("code"), mtag, s.get("industry") or "—")
                    bz, sz = s.get("buy_zone"), s.get("sell_zone")
                    if bz and bz[0]:
                        seg += " 买%.2f~%.2f" % (bz[0], bz[1])
                    if sz and sz[0]:
                        seg += " 卖%.2f~%.2f" % (sz[0], sz[1])
                    elif s.get("stop"):
                        seg += " 止损%.2f" % s["stop"]
                    parts.append(seg)
                out.append("   ↳ %s 买入建议：%s" % (x["name"], "；".join(parts)))
    if sells:
        out.append("🛑 破位卖出：" + "；".join(
            "%s 破 %.2f，止损 %.2f" % (tag(x), x["buy_zone"][0], x["stop"])
            for x in sells[:4]))
    if times:
        out.append("⏰ 周期到期/达标：" + "；".join(
            "%s %s" % (tag(x), (x.get("time_status") or "").replace("⏰ ", "").replace("✅ ", ""))
            for x in times[:4]))
    if adds:
        out.append("➕ 加仓提示：" + "；".join(
            "%s 买区 %s~%s" % (tag(x), x["buy_zone"][0], x["buy_zone"][1])
            for x in adds[:4]))
    if tps:
        out.append("🎯 逼近卖点：" + "；".join(
            "%s 卖区 %s~%s" % (tag(x), x["sell_zone"][0], x["sell_zone"][1])
            for x in tps[:4]))
    costed = [x for x in (zr.get("items") or []) if x.get("cost")]
    if costed:
        out.append("📌 自选持仓：" + "；".join(
            "%s 现%.2f %+.1f%% 目标[%s]" % (tag(x), x["close"], x.get("pnl_pct") or 0,
                                           tgt_str(x))
            for x in costed[:4]))
    normal = [x for x in (zr.get("items") or [])
              if x["action"] == "正常持有" and not x.get("cost")][:3]
    if normal and not out:
        out.append("🎯 区间速览：" + "、".join(
            "%s[%s] 买%s~%s/卖%s~%s" % (tag(x), x.get("horizon"), x["buy_zone"][0],
                                        x["buy_zone"][1], x["sell_zone"][0], x["sell_zone"][1])
            for x in normal))
    if not out:
        out.append("关注池暂无有效区间数据")
    return out
