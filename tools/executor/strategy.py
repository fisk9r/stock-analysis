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


def _tencent_kline(code: str, n: int = 10, timeout: int = 10, retries: int = 2) -> list:
    """腾讯日 K：qfqday 行=[日期,开,收,高,低,量]。返回 [{d,o,c,h,l}]，旧→新。

    2026-09-01 加固：加 3 次尝试 + 退避重试（与 realtime_quote 同口径）——
    CI 弱网下单次失败会让卖出裁决拿到空 K 线（断板/续板判定失效 → 误判 HOLD
    该卖不卖），重试显著降低该风险。"""
    prefix = "sh" if code[0] in ("6", "9") else ("bj" if code[0] in ("4", "8") else "sz")
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=%s%s,day,,,%d,qfq" % (prefix, code, n))
    js = None
    last_err = None
    for attempt in range(1 + max(0, retries)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                js = __import__("json").loads(r.read().decode("utf-8"))
            break
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    if js is None:
        raise last_err  # 全部重试失败，保持原有异常语义（调用方已有降级路径）
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

    # ============ T+1 硬约束（2026-09-01 修复，用户指出真BUG） ============
    # A 股 T+1：当日买入的股票当日不可卖出。此前 sell_decision 无此守卫，
    # 今日买入的票（buy_date==today）在盘中巡逻（炸板保护/规则3止损）或尾盘通道
    # 里会被 SELL 掉（例：楚天龙 09:26 买入，盘中跌3%触发规则3止损→同日卖出）。
    # 这是 T+1 违规。修正：buy_date==today 一律 HOLD（锁 T+1），最早明日才能卖。
    buy_date_today = (pos.get("buy_date") or "") == today
    if buy_date_today:
        return {"verdict": "HOLD", "price": cur,
                "reason": "今日买入（T+1 锁定），最早明日可卖（现价%.2f）" % cur}

    # 找昨日K线（今日之前最后一根）
    # 2026-08-31 修复：prev2/yest_limit 必须先初始化——旧代码若 yest 恰好是
    # klines[0]（新股/长停牌复牌只有一根历史K线），循环内不执行 if i>0 分支，
    # prev2/yest_limit 从未赋值 → 规则1 引用时 UnboundLocalError 崩掉整轮平仓
    yest = None
    yest_limit = False
    prev2 = None
    for i in range(len(klines) - 1, -1, -1):
        if klines[i]["d"] < today:
            yest = klines[i]
            # 昨日的前一日收盘
            if i > 0:
                prev2 = klines[i - 1]["c"]
                yest_limit = is_limit_up(yest, prev2, code)
            break

    buy_date = pos.get("buy_date") or ""
    days_held = 0
    # 持仓交易日数口径（2026-08-31 定稿）：今日相对买入日的交易日序差（含今日）。
    #   买入日=T0，此后每过一个交易日 +1；早盘 09:26 裁决发生在今日 → 今日计入。
    #   买入 T0 → 第1个交易日 T1 days=1 … 第3个交易日 days=3 → 触发无条件清仓。
    #   旧口径「到昨日为止的已过交易日数」恒差一天，规则0实际第4日才触发。
    # 2026-08-31 修复：交易日序列必须过滤「今日及以后」的K线——复盘 15:30 后
    # 腾讯 fqkline 已含今日K线，用全量列表索引会错位。
    past_dates = [k["d"] for k in klines if k["d"] < today]
    if buy_date in past_dates:
        days_held = max(0, len(past_dates) - past_dates.index(buy_date))
    elif past_dates:
        # 买入日早于K线窗口（K线只取了12根）→ 用窗口起点粗算，不小于1
        days_held = max(1, len(past_dates))
    elif klines and klines[-1]["d"] >= buy_date:
        # K线窗口里只有今日或买入日之后的（新股/复牌）
        days_held = 1
    else:
        # 买入日不在K线（停牌/数据缺失），保守计 1 天起
        days_held = 1

    # 规则0：持仓 >=3 个交易日 → 无条件清仓
    if days_held >= 3:
        return {"verdict": "SELL", "price": cur,
                "reason": "持仓%d个交易日超限，无条件清仓" % days_held}

    # 规则1：昨日断板（买入后未续板）→ 今日开盘卖
    #   回测：断板后 T+2 开盘卖平均 -1.18%，越拖越差
    # 2026-08-31 修复：卖出价用现价 cur（用户纪律2：按实时价成交）——
    # 旧代码 max(cur, opn) 在低走时会按更高的开盘价记录成交，虚增模拟收益
    if yest and not yest_limit:
        yest_pct_txt = ("%.2f%%" % ((yest["c"] / prev2 - 1) * 100)) if prev2 else "未确认"
        return {"verdict": "SELL", "price": cur,
                "reason": "昨日断板（收%s未封板），按纪律开盘卖出（回测拖到T+2平均-1.18%%）"
                          % yest_pct_txt}

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


def tailgate_decision(pos: dict, quote: dict) -> dict:
    """尾盘确认通道（14:45 版，2026-08-29 回测落地）——持仓管理而非新买入。

    实证（本地 309 个交易日全市场涨停票样本，仅统计按竞价纪律高开>=2%买入的）：
      尾盘微红(0~2% vs 开盘)  → 过夜次日 +3.01% / 红盘率 62.9%（14 个月全部为正，最强过夜信号）
      尾盘偏强(+2~5%)         → 过夜次日 +1.08% / 53.7%
      尾盘强势(+5%以上)       → 过夜次日 +0.62% / 50.9%（强拉透支，反而不如微红）
      尾盘小亏(-3~0%)         → 过夜次日 -0.11% / 44.0%
      尾盘深亏(<-3%)          → 过夜次日 -0.31% / 44.9%（11/14 个月为负 → 该止损）
    结论：14:45 时点不做新买入（尾盘追强期望仅 +0.6%），只做两件事：
      ① 深亏(<-3% vs 开盘) → 尾盘止损离场（避免负期望过夜）
      ② 微红(0~2%) → 尾盘确认持有过夜（最强过夜信号，推送确认）
    14:45 时点现价≈尾盘价，用现价/开盘价比值判定（与回测口径一致）。
    返回 {verdict: SELL/HOLD/None, price, reason}；None = 不适用（非买入日）
    """
    q = quote or {}
    cur = q.get("price") or 0
    opn = q.get("open") or 0
    if not cur or not opn:
        return {"verdict": None, "price": 0, "reason": "无行情"}
    ratio = cur / opn - 1  # 现价相对开盘（回测口径：close/open）
    if ratio <= -0.03:
        return {"verdict": "SELL", "price": cur,
                "reason": "尾盘确认通道：现价较开盘-3%%以下（%.1f%%），深亏过夜次日均值-0.31%%/红盘率45%%，尾盘止损离场"
                          % (ratio * 100)}
    if -0.001 <= ratio <= 0.02:
        return {"verdict": "HOLD", "price": cur,
                "reason": "尾盘确认通道：微红+%.1f%%（最强过夜信号，历史红盘率62.9%%/均值+3.0%%），确认持有过夜"
                          % (ratio * 100)}
    return {"verdict": None, "price": cur,
            "reason": "尾盘中性（%+.1f%% vs 开盘），按常规策略" % (ratio * 100)}


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
    # 2026-09-01 T级（用户要求：模拟盘什么票都可以买，不只连板票）：
    # st=0 趋势/动量票走趋势专用决策线（平开微红或尾盘微红横盘确认），一律半仓——
    # 趋势票套用涨停竞价纪律会追在高开溢价最贵处（实证 920087 st=0 高开 2.2%
    # 跟进次日 -6.03%），故降仓位。
    if sig.get("market_type") == "trend" and mc_ok:
        return {"grade": "T", "weight": 0.5,
                "reason": "T级：趋势票半仓（开盘%+.1f%%／平开微红或尾盘确认，非涨停竞价体系）"
                          % gap}
    return {"grade": "X", "weight": 0.0,
            "reason": "不满足最优变体（gap%.1f%%/st%d/市值%s），全样本口径仅48.7%%/+0.37%%"
                      % (gap, st, ("%.0f亿" % mc) if mc else "未知")}


def late_hold_decision(pos: dict, quote: dict) -> dict:
    """尾盘（14:45）持仓裁决（2026-08-30 回测落地）。

    实证（高开≥2% 买入的涨停票样本，尾盘状态 → 次日）：
      尾盘微红（较开盘 0~2% 横盘）→ 次日 +3.01% / 62.9%（14 个月全正）→ 确认持有
      尾盘深亏（较开盘 <-3%）      → 次日 -0.31% / 44.9%（11/14 个月为负）→ 止损
      尾盘强拉（>5%）              → 次日仅 +0.62%（透支）→ 收益兑现意愿优先
    与 sell_decision 的关系：sell_decision 是开盘裁决（断板卖/续板持），
    本函数是尾盘复核——开盘判断为 HOLD 但尾盘形态崩掉的票，当日 14:45 还能止损，
    不必死扛到明日开盘（明日开盘卖平均再多亏 0.3~1.2%）。

    返回 {verdict: SELL/HOLD/CONFIRM, price, reason}
      CONFIRM = 尾盘形态确认 strongest，明日按计划持有（只是确认，不触发操作）
    """
    q = quote or {}
    cur = q.get("price") or 0
    opn = q.get("open") or 0
    if not cur or not opn:
        return {"verdict": "HOLD", "price": 0, "reason": "尾盘复核：无有效行情"}
    fade = (cur / opn - 1) * 100
    pnl = (cur / pos["avg_price"] - 1) * 100
    if fade <= -3:
        return {"verdict": "SELL", "price": cur,
                "reason": "尾盘复核：现价较开盘%.2f%%深亏，隔夜负期望（44.9%%/44.9%%），"
                          "尾盘止损优于明日开盘卖" % fade}
    if 0 <= fade <= 2:
        return {"verdict": "CONFIRM", "price": cur,
                "reason": "尾盘复核：微红%.2f%%横盘不回补，最强过夜形态（62.9%%/+3.01%%），确认持有" % fade}
    if fade > 5:
        return {"verdict": "SELL", "price": cur,
                "reason": "尾盘复核：较开盘+%.2f%%强拉透支隔夜溢价（次日仅+0.62%%），"
                          "浮盈%.2f%%兑现" % (fade, pnl)}
    return {"verdict": "HOLD", "price": cur,
            "reason": "尾盘复核：较开盘%+.2f%%形态中性，维持开盘裁决持有" % fade}
