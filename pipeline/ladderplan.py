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

# 低开判定阈值：与 recveto.LOW_OPEN 保持同一口径（< -0.1% 开盘即低开）
LOW_OPEN_PCT = -0.1

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
    每个高度桶内再按「次日开盘 gap」拆两个条件子桶：
      gap_up   = 开盘 >= 前收*(1+LOW_OPEN_PCT/100)（不低开）
      gap_down = 开盘 <  前收*(1+LOW_OPEN_PCT/100)（低开）
    返回 {s: {n, exp, mfe_med, reach:{lvl:p}, days_med, cond:{gap_up:{...}, gap_down:{...}}}}。
    """
    def _new_bk():
        return {"n": 0, "exp_sum": 0.0, "mfes": [], "days": []}

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
            prev_close = b.get("c") or 0
            gap_open_pct = (onext / prev_close - 1) * 100.0 if prev_close else 0.0
            low_open = gap_open_pct < LOW_OPEN_PCT
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
                                    "mfes": [], "days": [],
                                    "cond": {"gap_up": _new_bk(), "gap_down": _new_bk()}})
            sub = bk["cond"]["gap_down" if low_open else "gap_up"]
            bk["n"] += 1
            bk["exp_sum"] += eod_ret          # 用首日尾盘收益近似经验收益（快而稳）
            bk["mfes"].append(mfe)
            bk["days"].append(best_day)
            sub["n"] += 1
            sub["exp_sum"] += eod_ret
            sub["mfes"].append(mfe)
            sub["days"].append(best_day)
    # 汇总成桶统计

    def _summarize(bk):
        mfes = sorted(bk["mfes"])
        n = bk["n"]
        days = sorted(bk["days"])
        med = lambda a: a[len(a) // 2] if a else 0
        reach = {}
        for lv in _MFE_LEVELS:
            reach[lv] = round(sum(1 for x in mfes if x >= lv) / n, 3)
        return {
            "n": n,
            "exp": round(bk["exp_sum"] / n, 2),
            "mfe_med": round(med(mfes), 2),
            "reach": reach,
            "days_med": med(days),
        }

    res = {}
    for s, bk in out.items():
        entry = _summarize(bk)
        entry["cond"] = {}
        for cname, cbk in bk.get("cond", {}).items():
            # 条件子桶样本不足时不出统计，调用方回退整桶口径
            entry["cond"][cname] = _summarize(cbk) if cbk["n"] >= _MIN_N // 2 else None
        res[s] = entry
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


def env_mod(sent=None, regime=None, ladder_warn=None):
    """环境动态修正系数（>0），及人话摘要。三项取乘积、各自封顶防过度放大：

      · 情绪分 sent.score：<40 冷市 ×0.85；>=70 高热 ×1.05；
      · 接力环境 regime.factor：<0.2 冰点再 ×0.85；>0.4 过热再 ×0.90（高位票过热反噬）。
      · 梯队健康度 ladder_warn.level：退潮 ×0.75 / 降温 ×0.92 / 正常及以上 ×1.0。
    返回 (coef, reason)；reason 为空串表示无修正。
    """
    coef = 1.0
    reasons = []
    sc = (sent or {}).get("score")
    try:
        sc = float(sc) if sc is not None else None
    except (TypeError, ValueError):
        sc = None
    if sc is not None and sc >= 0:
        if sc < 40:
            coef *= 0.85
            reasons.append("情绪%.0f冷×0.85" % sc)
        elif sc >= 70:
            coef *= 1.05
            reasons.append("情绪%.0f热×1.05" % sc)
    rf = (regime or {}).get("factor")
    try:
        rf = float(rf) if rf is not None else None
    except (TypeError, ValueError):
        rf = None
    if rf is not None:
        if rf < 0.2:
            coef *= 0.85
            reasons.append("接力冰点×0.85")
        elif rf > 0.4:
            coef *= 0.90
            reasons.append("高位过热×0.90")
    lvl = str((ladder_warn or {}).get("level") or "")
    if lvl == "退潮":
        coef *= 0.75
        reasons.append("梯队退潮×0.75")
    elif lvl == "降温":
        coef *= 0.92
        reasons.append("梯队降温×0.92")
    return round(coef, 3), ";".join(reasons)


def expected_top(s, stats, cond=None, coef=1.0):
    """预期板数区间：由期望涨幅折算『还能再走几个板』（10% 一板近似）。

    cond：'gap_up'(不低开)/'gap_down'(低开)/None(不分) —— 有对应子桶样本时用其期望替代整桶，
    替代量依然与先验按 _BUCKET_BLEND 融合，避免小子样本过拟合。
    coef：环境修正系数（env_mod 输出），直接乘在期望涨幅上。
    """
    b = _bucket(stats, s) or {}
    prior = _PRIOR.get(s, {"exp": 3.0, "reach10": 0.25, "days": 2})
    n = b.get("n") or 0
    exp_base = prior["exp"]
    days = prior["days"]
    reach10 = prior["reach10"]
    used_cond = False
    if n >= _MIN_N:
        exp_base = b["exp"] * _BUCKET_BLEND + prior["exp"] * (1 - _BUCKET_BLEND)
        days = b.get("days_med") or prior["days"]
        reach10 = (b.get("reach") or {}).get(10.0, prior["reach10"])
    cb = ((b.get("cond") or {}).get(cond)) if (cond and b) else None
    if cond and cb and cb.get("n", 0) >= _MIN_N // 2:
        exp_base = cb["exp"] * _BUCKET_BLEND + exp_base * (1 - _BUCKET_BLEND)
        days = cb.get("days_med") or days
        reach10 = (cb.get("reach") or {}).get(10.0, reach10)
        used_cond = True
    exp = max(exp_base * (coef or 1.0), 0.2)
    boards_more = max(exp / 9.8, 0.15)   # 期望涨幅折板数
    lo = max(int(boards_more), 0)
    hi = lo + (1 if boards_more - lo >= 0.45 else 1)
    return {"more_lo": lo, "more_hi": hi,
            "exp_ret": round(exp, 1), "hold_days": int(max(days, 1)),
            "used_cond": used_cond, "cond_n": (cb or {}).get("n") if used_cond else 0}


def plan(u, code, s, close, stats=None, open_pct=None, env_coef=1.0,
         env_note="", ladder_warn_level=""):
    """生成一只票的连板交易计划。

    close：当前涨停价（=明日参考基准）。
    open_pct：明日竞价前未知 → 常为 None（计划给出两分支 gate 提示）；
              竞价后复核时可传实际开盘 % 以选条件子桶重算。
    buy_zone：次日不低开时可追的区间 [close*0.995, close*1.03]
              若低开则 gate='avoid'（recveto.G1 同源证据：历史同条件收红率仅 ~24%）。
    t1/sell_zone：expected 折算；stop：-8%（回测止损口径）。
    env_coef/env_note：环境动态修正（sentiment/regime/ladder_warn 合成）。
    ladder_warn_level：梯队健康度标签，写进 evidence。
    """
    st = stats or {}
    open_v = None
    try:
        open_v = float(open_pct) if open_pct is not None else None
    except (TypeError, ValueError):
        open_v = None
    cond = None
    if open_v is not None:
        cond = "gap_down" if open_v < LOW_OPEN_PCT else "gap_up"
    et = expected_top(s, st, cond=cond, coef=env_coef)
    b = _bucket(st, s) or {}
    prior = _PRIOR.get(s, {"exp": 3.0, "reach10": 0.25, "days": 2})
    # reach10 取与 expected_top 同源的样本口径
    reach10 = (b.get("reach") or {}).get(10.0, prior["reach10"]) if b else prior["reach10"]
    if cond and et.get("used_cond"):
        cb = ((b.get("cond") or {}).get(cond)) or {}
        reach10 = (cb.get("reach") or {}).get(10.0, reach10)
    base = float(close)
    stop = round(base * 0.92, 2)
    t1 = round(base * (1 + min(et["exp_ret"], 10.0) / 100.0), 2)
    t2 = round(base * 1.10, 2) if reach10 >= 0.22 else round(base * 1.06, 2)
    buy_zone = [round(base * 0.995, 2), round(base * 1.03, 2)]   # 微低开~+3% 内都算合格买点
    sell_zone = [t1, t2]
    rr = round((min(t1, base * 1.05) - base) / max(base - stop, 0.01), 2) if stop < base else 0
    ev_n = b.get("n") if b else 0
    # 低开否决门（G1 同源证据链）：竞价未出前给分支提示，竞价后按实际开盘定生死
    gate = None
    if open_v is not None and open_v < LOW_OPEN_PCT:
        gate = "avoid"
    gate_hint = ("竞价低开<%s%%→放弃买入(recveto.G1:该条件历史T+1收红率仅24%%)"
                 % abs(LOW_OPEN_PCT))
    ev_bits = []
    if ev_n:
        ev_bits.append("近 %d 笔同高度样本·开盘接力中位溢价 %.1f%%" % (
            ev_n, (b.get("mfe_med") or 0)))
    else:
        ev_bits.append("样本不足·按先验")
    if et.get("used_cond"):
        ev_bits.append("%s子桶 n=%d 单列核算" % (
            "低开" if cond == "gap_down" else "不低开", et.get("cond_n") or 0))
    if env_note:
        ev_bits.append(env_note)
    if ladder_warn_level and ladder_warn_level != "正常":
        ev_bits.append("梯队:%s" % ladder_warn_level)
    return {
        "code": code, "entry_streak": s,
        "expected_top": "%d→%d~%d板" % (s, s + et["more_lo"], s + et["more_hi"]),
        "expected_more": [et["more_lo"], et["more_hi"]],
        "exp_ret": et["exp_ret"], "hold_days": et["hold_days"],
        "buy_zone": buy_zone, "sell_zone": sell_zone, "stop": stop,
        "rr": rr, "reach10": round((reach10 or 0) * 100),
        "sample_n": ev_n or 0,
        "gate": gate,               # None=待竞价确认 / "avoid"=已判低开放弃
        "gate_hint": gate_hint,
        "evidence": " · ".join(ev_bits),
    }


def scan(u, date, lus, stats=None, topn=12, open_pct_map=None,
         sent=None, regime=None, ladder_warn=None):
    """对当日涨停名单生成次日「机会买入」计划（含 高位但条件允许者——标注不拦）。

    open_pct_map：{code: 明日开盘涨幅%%}——盘后计划阶段未知，传 None 走双分支提示；
                  竞价后复核（auction 模式）可传入实际值启用条件子桶 + 硬性 avoid 门。
    sent/regime/ladder_warn：环境三元组 → env_mod 折算统一修正系数写入每张计划单。
    """
    st = stats if stats is not None else ladder_stats(u)
    coef, enote = env_mod(sent=sent, regime=regime, ladder_warn=ladder_warn)
    lw_lvl = str((ladder_warn or {}).get("level") or "")
    plans = []
    for r in lus or []:
        try:
            opv = (open_pct_map or {}).get(r["code"])
            p = plan(u, r["code"], r.get("streak", 1), r.get("close"), st,
                     open_pct=opv, env_coef=coef, env_note=enote,
                     ladder_warn_level=lw_lvl)
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
