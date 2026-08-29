# -*- coding: utf-8 -*-
"""T+1 卖出策略引擎（strategy）——全部规则来自无未来函数的可执行口径回测。

回测结论（tools/hypo_backtest.py，118 万根 K 线，13 个月逐月验证）：
  1. 买入日日内是负期望（均值 -0.81%）→ 必须靠「续板隔夜溢价」弥补
  2. 昨日买入后今日断板 → 今日开盘卖出（回测里断板后拖到明天开盘平均 -1.18%）
  3. 昨日续板（仍涨停）→ 高度溢价：st 越高次日胜率越高（st=5 达 67.5%/+1.99%）
     → 续板但今日冲高乏力（现价低于开盘价）→ 开盘/现价卖出锁定
  4. 持仓超过 3 个交易日仍未卖 → 无条件清仓（避免趋势票占用资金）

数据源：腾讯 fqkline 日 K（CORS 友好、无需鉴权），与站点主源一致。
无未来函数：所有判断只用到「今日已发生的行情」（开盘/现价/昨收/昨日是否涨停）。
"""
import urllib.request
import time

_CTX = __import__("ssl")._create_unverified_context()


def _tencent_kline(code: str, n: int = 10, timeout: int = 10) -> list:
    """腾讯日 K：qfqday 行=[日期,开,收,高,低,量]。返回 [{d,o,c,h,l}]，旧→新。"""
    prefix = "sh" if code[0] in ("6", "9") else ("bj" if code[0] in ("4", "8") else "sz")
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=%s%s,day,,,%d,qfq" % (prefix, code, n))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        js = __import__("json").loads(r.read().decode("utf-8"))
    node = (js.get("data") or {}).get(prefix + code) or {}
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    for k in rows:
        try:
            out.append({"d": k[0], "o": float(k[1]), "c": float(k[2]),
                        "h": float(k[3]), "l": float(k[4])})
        except (ValueError, IndexError, TypeError):
            continue
    return out


def is_limit_up(bar: dict, prev_close: float, code: str) -> bool:
    """判断一根K线是否涨停（按板块涨停幅度，10%/20%/30%）。"""
    if not bar or not prev_close:
        return False
    pct = (bar["c"] / prev_close - 1) * 100
    if code.startswith(("30", "68")):
        lim = 19.9  # 创业板/科创板 20%
    elif code.startswith(("4", "8", "92")):
        lim = 29.9  # 北交所 30%
    else:
        lim = 9.9   # 主板 10%（ST 实际5%，但我们不推荐 ST）
    return pct >= lim


def sell_decision(pos: dict, quote: dict, klines: list, today: str = None) -> dict:
    """对一条持仓给出今日卖出裁决。

    pos: {code, name, buy_date, avg_price, volume, streak, open_gap}
    quote: 该票今日实时行情 {open, price, prev_close}（无行情=停牌，返回 HOLD）
    klines: 日K（旧→新），最后一根应为今日（或最近交易日）
    返回 {verdict: SELL/HOLD, price, reason}
    """
    code = pos["code"]
    q = quote or {}
    cur = q.get("price") or 0
    opn = q.get("open") or 0
    prev_close = q.get("prev_close") or 0
    if not cur or not opn:
        return {"verdict": "HOLD", "price": 0,
                "reason": "无有效行情（停牌/未开盘），顺延"}
    today = today or time.strftime("%Y-%m-%d")

    # 找昨日K线（今日之前最后一根）
    yest = None
    for i in range(len(klines) - 1, -1, -1):
        if klines[i]["d"] < today:
            yest = klines[i]
            # 昨日的前一日收盘
            if i > 0:
                prev2 = klines[i - 1]["c"]
                yest_limit = is_limit_up(yest, prev2, code)
                break
    else:
        yest = None
        yest_limit = False

    buy_date = pos.get("buy_date") or ""
    days_held = 0
    if yest:
        # 持仓交易日数 = K线里 buy_date 之后（不含）到 yest 的根数
        dates = [k["d"] for k in klines]
        if buy_date in dates:
            i = dates.index(buy_date)
            # yest 是今日之前最后一根 → days = yest索引 - buy_date索引
            # yest 索引 = len(klines)-1（若今日不在K线）或日期小于today的最后一根
            yest_idx = dates.index(yest["d"])
            days_held = max(0, yest_idx - i)
        else:
            # 买入日不在K线（未来日期/停牌），按「今日已是新交易日」保守计 1 天起
            days_held = 1

    # 规则0：持仓 >=3 个交易日 → 无条件清仓
    if days_held >= 3:
        return {"verdict": "SELL", "price": cur,
                "reason": "持仓%d个交易日超限，无条件清仓" % days_held}

    # 规则1：昨日断板（买入后未续板）→ 今日开盘卖
    #   回测：断板后 T+2 开盘卖平均 -1.18%，越拖越差
    if yest and not yest_limit:
        return {"verdict": "SELL", "price": max(cur, opn) if cur else opn,
                "reason": "昨日断板（收%.2f%%未封板），按纪律开盘卖出（回测拖到T+2平均-1.18%%）"
                          % ((yest["c"] / (prev2 if yest else 1) - 1) * 100)}

    # 规则2：昨日续板 → 吃高度溢价，但现价弱于开盘则锁定
    if yest_limit:
        # 今日冲高乏力：现价 < 开盘价（高开低走）→ 卖
        if cur < opn * 0.995:
            return {"verdict": "SELL", "price": cur,
                    "reason": "续板票今日高开低走（开%.2f 现%.2f），锁定利润" % (opn, cur)}
        # 现价已大涨 ≥5% → 落袋
        day_gain = (cur / prev_close - 1) * 100 if prev_close else 0
        if day_gain >= 5:
            return {"verdict": "SELL", "price": cur,
                    "reason": "续板票日内涨%.2f%%≥5%%，落袋为安" % day_gain}
        return {"verdict": "HOLD", "price": cur,
                "reason": "昨日续板，继续持有吃高度溢价（st=%s）" % pos.get("streak")}

    # 规则3：兜底——数据不足以判断昨日是否涨停，按止损线
    pnl = (cur / pos["avg_price"] - 1) * 100
    if pnl <= -3:
        return {"verdict": "SELL", "price": cur,
                "reason": "持仓浮亏%.2f%%≤-3%%止损" % pnl}
    return {"verdict": "HOLD", "price": cur,
            "reason": "昨日行情不足判断，暂持有（浮盈%.2f%%）" % pnl}


def limit_prices(prev_close: float, code: str) -> dict:
    """按板块计算今日涨跌停价（A股制度：主板±10% / 创业板科创板±20% / 北交所±30%）。

    返回 {limit_up, limit_down}，四舍五入到分（交易所规则）。
    prev_close 无效时返回 {limit_up: 0, limit_down: 0}。
    """
    if not prev_close or prev_close <= 0:
        return {"limit_up": 0.0, "limit_down": 0.0}
    if code.startswith(("30", "68")):
        pct = 0.20
    elif code.startswith(("4", "8", "92")):
        pct = 0.30
    else:
        pct = 0.10
    return {"limit_up": round(prev_close * (1 + pct), 2),
            "limit_down": round(prev_close * (1 - pct), 2)}


def can_buy(quote: dict, code: str) -> dict:
    """可买性检查：一字涨停板买不进（开盘=涨停价且现价仍封死 → 全天无卖单成交）。

    判定：开盘价 ≥ 涨停价×0.998（允许 0.2% 精度容差）→ 一字板/开盘即封板，放弃。
    返回 {ok: True} 或 {ok: False, reason: "..."}。
    """
    q = quote or {}
    prev_close = q.get("prev_close") or 0
    opn = q.get("open") or 0
    cur = q.get("price") or 0
    if not prev_close or not opn:
        return {"ok": False, "reason": "无有效行情（停牌/未开盘），放弃"}
    lp = limit_prices(prev_close, code)
    limit_up = lp["limit_up"]
    if limit_up and opn >= limit_up * 0.998:
        return {"ok": False,
                "reason": "开盘%.2f=涨停价%.2f（一字板/开盘即封板），买不进，放弃" % (opn, limit_up)}
    # 盘中已封板也买不进（排板队列轮不到模拟盘）
    if limit_up and cur >= limit_up * 0.998 and cur > opn:
        return {"ok": False,
                "reason": "现价%.2f已封涨停（%.2f），封单无法成交，放弃" % (cur, limit_up)}
    return {"ok": True, "limit_up": limit_up, "limit_down": lp["limit_down"]}


def can_sell(quote: dict, code: str) -> dict:
    """可卖性检查：跌停封死卖不出（全天无买单 → 只能顺延明日）。

    判定：现价 ≤ 跌停价×1.002 → 跌停，顺延。
    返回 {ok: True} 或 {ok: False, reason: "..."}。
    """
    q = quote or {}
    prev_close = q.get("prev_close") or 0
    cur = q.get("price") or 0
    if not prev_close or not cur:
        return {"ok": False, "reason": "无有效行情（停牌），顺延"}
    lp = limit_prices(prev_close, code)
    limit_down = lp["limit_down"]
    if limit_down and cur <= limit_down * 1.002:
        return {"ok": False,
                "reason": "现价%.2f封死跌停（%.2f），无买单接盘卖不出，顺延明日" % (cur, limit_down)}
    return {"ok": True, "limit_up": lp["limit_up"], "limit_down": limit_down}


def strategy_filter(sig: dict, quote: dict,流通市值亿: float = None) -> dict:
    """买入端最优变体过滤（回测 62.2%/+2.71% 那条）。

    优先级：
      A级：gap>5% + st≥3 + 流通市值 60-150 亿  → 全额
      B级：st≥3 + 60-150 亿（不限 gap）        → 全额（胜率 61.8%）
      C级：gap>5% + 60-150 亿                  → 半仓（胜率 55.5%）
      其他 → 放弃（全样本 48.7%/+0.37% 不值得占用仓位）
    流通市值未知时按 B 级 st≥3 兜底。
    """
    gap = sig.get("open_gap") or 0
    st = int(sig.get("streak") or 0)
    mc = 流通市值亿
    mc_ok = mc is None or (60 <= mc <= 150)

    if gap > 5 and st >= 3 and mc_ok:
        return {"grade": "A", "weight": 1.0,
                "reason": "A级：gap%.1f%%+st%d+市值%s（62.2%%/+2.71%%）"
                          % (gap, st, ("%.0f亿" % mc) if mc else "?")}
    if st >= 3 and mc_ok:
        return {"grade": "B", "weight": 1.0,
                "reason": "B级：st%d+市值%s（61.8%%/+2.53%%）"
                          % (st, ("%.0f亿" % mc) if mc else "?")}
    if gap > 5 and mc_ok:
        return {"grade": "C", "weight": 0.5,
                "reason": "C级：gap%.1f%%+市值%s 半仓（55.5%%/+1.53%%）"
                          % (gap, ("%.0f亿" % mc) if mc else "?")}
    return {"grade": "X", "weight": 0.0,
            "reason": "不满足最优变体（gap%.1f%%/st%d/市值%s），全样本口径仅48.7%%/+0.37%%"
                      % (gap, st, ("%.0f亿" % mc) if mc else "未知")}
