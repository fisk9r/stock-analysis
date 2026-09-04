# -*- coding: utf-8 -*-
"""推荐推送新模式的数据准备（2026-09-04 用户拍板：重新构建推荐推送）。

三段式推送所需数据，全部由纯标准库 + pipeline.zones / bandtrade 计算，无外部依赖，CPU-only：

  ① 持仓今日操作（data["holdings_ops"]）
     对 config/holdings.json 的实盘持仓，用 zones.analyze_one 给出今日明确结论：
     卖出 / 卖出换股 / 加仓低吸 / 格局持有 / 继续持有，并附盈亏、买卖区、止损、更换建议。

  ② 买点候选（data["buy_candidates"] = {ladder, trend, band}）
     从 连板计划 / 趋势票 / 波段票 三池收拢「当下就是买点」的票——
     连板：次日竞价可追；趋势：entry_state ∈ 可买/微超；波段：回到阶段底附近。
     每只附【板块提示】+【综合打分】（基础分 + 板块强度加权），只给买区/卖区(目标)，绝不给"卖出"动作。

  ③ 板块强度图（data["board_strength"] = {板块: 强度分}）
     融合 sector_trend(主线/强势) + 主力净流入(money.boards_in) + 退潮(money.boards_out)，
     供②综合打分加权，也供推送「板块强弱」次级段展示。

设计原则：缺字段不崩、不引入未来函数；与现有 zones/entry_plan 共用一套买点口径。
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_GENERIC = ("昨日", "连板", "涨停", "融资融券", "深股通", "沪股通", "标准普尔", "富时",
            "MSCI", "转融券", "机构重仓", "基金重仓", "预盈预增", "创业板综", "深成500",
            "中证", "上证", "破净股", "参股", "股权激励", "高送转", "AB股", "AH股",
            "昨曾涨停", "证金持股", "社保重仓", "QFII重仓", "长江三角", "西部大开发",
            "央国企改革", "国企改革", "中字头", "转债标的", "MSCI中国", "富时罗素",
            "标普道琼斯", "中证500", "沪深300", "上证50", "深证成指", "创业板指",
            "科创50", "北证50", "ST股", "深股通标的", "沪股通标的", "创业板综",
            "昨日涨停", "养老金持股", "险资持股", "GDR", "同花顺", "数据中心",
            "股权转让", "HS300_", "上证180_", "上证380_", "上证50_", "中证100_",
            "沪深300_", "中证500_", "中证800_", "沪股通", "深股通", "融资融券标的")


def _clip(v, lo, hi):
    try:
        v = float(v)
    except Exception:
        return lo
    return max(lo, min(hi, v))


def primary_board(code, code2boards):
    """取代表板块（非泛用概念 > 行业），剔除融资融券/HS300_ 等噪声。失败回退 '—'。"""
    try:
        from build import primary_board as _pb
        return _pb(code, code2boards) or "—"
    except Exception:
        boards = (code2boards or {}).get(str(code)) or []
        for _, name, kind in boards:
            if kind == "concept" and not any(g in name for g in _GENERIC):
                return name
        for _, name, kind in boards:
            if kind == "industry" and not any(g in name for g in _GENERIC):
                return name
        return boards[0][1] if boards else "—"


# ═══════════════════════════════════════════════════════════════════════════
# ③ 板块强度图
# ═══════════════════════════════════════════════════════════════════════════
def board_strength_map(rec, money, code2boards=None):
    """返回 {板块名: 强度分(-40~+40)}。多源融合：

      - rec["sector_trend"]：tier=主线 → +30，强势 → +15，活跃 → +8
      - money.boards_in   ：主力净流入（亿），按 net/3 折分，封顶 +12
      - money.boards_out  ：主力净流出，按 -net/3 折分，封底 -12
    返回的是「板块 → 强度」，个股打分时用其代表板块查表（带模糊包含匹配）。
    """
    smap = {}

    def _add(name, v):
        if not name:
            return
        # 累加多源信号（主线/强势 + 主力净流入/流出），最后统一裁剪到 [-40,40]；
        # 用累加而非 max，否则净流出(负值)会被初始 0 吞掉、净流入也加不到主线分上。
        smap[name] = _clip(smap.get(name, 0) + v, -40, 40)

    # 1) 板块趋势主线/强势
    for s in (rec.get("sector_trend") or []):
        nm = s.get("sector")
        tier = s.get("tier")
        v = 30 if tier == "主线" else (15 if tier == "强势" else 8)
        _add(nm, v)
        # 该板块龙头个股的细分概念也加分（让同板块概念票吃到强度）
        for l in (s.get("leads") or []):
            _add(l.get("name"), v * 0.5)

    # 2) 主力净流入 / 流出
    for b in (money.get("boards_in") or []):
        nm = b.get("name")
        net = b.get("net") or 0
        _add(nm, max(-12, min(12, net / 3.0)))
    for b in (money.get("boards_out") or []):
        nm = b.get("name")
        net = b.get("net") or 0
        if net < 0:
            _add(nm, max(-12, min(0, net / 3.0)))

    return smap


def board_bonus(board, smap):
    """查板块强度分，带模糊包含匹配（板块名互为子串时取较大值）。无匹配返回 0。"""
    if not board or board == "—":
        return 0
    best = smap.get(board)
    if best is not None:
        return _clip(best, -25, 25)
    # 模糊：板块名出现在某强度键里或反之
    for k, v in smap.items():
        if not k or k == "—":
            continue
        if k in board or board in k:
            best = v if best is None else max(best, v)
    return _clip(best or 0, -25, 25)


# ═══════════════════════════════════════════════════════════════════════════
# ① 持仓今日操作
# ═══════════════════════════════════════════════════════════════════════════
def _map_holding_decision(r):
    """把 zones.analyze_one 的 action/rotate 映射成一句话操作结论。"""
    act = r.get("action")
    rot = r.get("rotate")
    if rot in ("止损", "割肉") or act == "破位卖出":
        return ("卖出", "🛑")
    if rot == "更换":
        return ("卖出换股", "🔄")
    if act in ("加仓提示", "回踩买入区"):
        return ("加仓低吸", "➕")
    if act in ("逼近卖出", "突破持有"):
        return ("格局持有·注意止盈", "🎯")
    if act == "跌破警示":
        return ("谨慎持有·观察", "⚠️")
    return ("继续持有·格局", "📌")


def compute_holdings_ops(u, date, con, code2boards, replace_pool=None):
    """对 config/holdings.json 的实盘持仓计算今日操作结论。

    返回 list[{code,name,decision,emoji,cost,pnl,close,action,rotate,
                buy_zone,sell_zone,stop,horizon,reasons,replace}]。
    """
    import zones
    try:
        import holdings as H
        positions = H.load_positions() or []
    except Exception:
        positions = []

    out = []
    for p in positions:
        code = p.get("code")
        cost = p.get("cost")
        if not code:
            continue
        bs = [b for b in (u.bars.get(code) or []) if b["d"] <= date]
        name = p.get("name") or (u.stocks.get(code, {}) or {}).get("name") or code
        if len(bs) < 40:
            out.append({"code": code, "name": name, "decision": "持仓(数据不足)",
                        "emoji": "❓", "cost": cost, "close": None, "pnl": None,
                        "action": "", "rotate": None, "buy_zone": None,
                        "sell_zone": None, "stop": None, "horizon": None,
                        "reasons": ["上市/数据不足，无法判定"], "replace": []})
            continue
        try:
            r = zones.analyze_one(
                code, name, bs, cost=cost,
                horizon=p.get("horizon") or None, elapsed=None,
                replace_pool=replace_pool, exclude=None,
                industries={code: primary_board(code, code2boards)})
        except Exception as e:
            out.append({"code": code, "name": name, "decision": "持仓(计算异常)",
                        "emoji": "❓", "cost": cost, "close": None, "pnl": None,
                        "action": "", "rotate": None, "buy_zone": None,
                        "sell_zone": None, "stop": None, "horizon": None,
                        "reasons": ["%r" % e], "replace": []})
            continue
        decision, emoji = _map_holding_decision(r)
        out.append({
            "code": code, "name": name, "decision": decision, "emoji": emoji,
            "cost": cost, "pnl": r.get("pnl_pct"), "close": r.get("close"),
            "action": r.get("action"), "rotate": r.get("rotate"),
            "buy_zone": r.get("buy_zone"), "sell_zone": r.get("sell_zone"),
            "stop": r.get("stop"), "horizon": r.get("horizon"),
            "reasons": r.get("reasons") or [], "replace": r.get("replace") or [],
        })
    # 卖出类置顶，确保最该看的先出现
    _ord = {"卖出": 0, "卖出换股": 1, "加仓低吸": 2, "格局持有·注意止盈": 3,
            "谨慎持有·观察": 4, "继续持有·格局": 5}
    out.sort(key=lambda x: (_ord.get(x["decision"], 9), -(x.get("pnl") or 0)))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# ② 买点候选（连板 / 趋势 / 波段）
# ═══════════════════════════════════════════════════════════════════════════
def _mk_cand(code, name, kind, board, bz, sz, sp, base, bonus, entry_state,
             extra=None, buy_now=None, buy_pull=None):
    """buy_now = 近端可执行买区(now_zone, 现价附近, 直接可挂单)；buy_pull = 回踩买区(pull_zone)。
    buy_zone/sell_zone 保留为结构性参考区（band_levels）。推送主显示用 buy_now。"""
    score = _clip(base + bonus, 0, 100)
    c = {
        "code": code, "name": name, "kind": kind, "board": board or "—",
        "buy_zone": bz, "sell_zone": sz, "stop": sp,
        "buy_now": buy_now, "buy_pull": buy_pull,
        "base_score": round(base, 1), "board_bonus": round(bonus, 1),
        "score": round(score, 1), "entry_state": entry_state,
    }
    if extra:
        c.update(extra)
    return c


def compute_buy_candidates(rec, u, date, code2boards, bmap, ladder_warn=None):
    """收拢三池「当下就是买点」的票，附板块提示 + 综合打分。

    连板：ladder_plans（次日竞价可追，买区=[close*0.995,close*1.03]）；
    趋势：rec["trend"]，只留 entry_state ∈ {可买, 微超}；
    波段：rec["band_trade"]，只留回到阶段底附近（close ≤ 买区上沿×1.05）。
    综合分 = 基础分 + 板块强度加权（board_bonus）。
    """
    ladder = []
    trend = []
    band = []

    # ---- 连板票 ----
    for p in (rec.get("ladder_plans") or []):
        code = p.get("code")
        if not code:
            continue
        name = p.get("name") or ""
        board = primary_board(code, code2boards)
        bz = p.get("buy_zone")
        sz = p.get("sell_zone")
        sp = p.get("stop")
        # 连板计划不直接带 close，但 buy_zone=[close*0.995, close*1.03] → close≈bz[0]/0.995
        _close = round(float(bz[0]) / 0.995, 2) if (bz and bz[0]) else None
        base = (p.get("worth_score") or 0) or ((p.get("rr") or 0) * 10) or 55
        if not base or base <= 0:
            base = 55
        bonus = board_bonus(board, bmap)
        ladder.append(_mk_cand(
            code, name, "连板", board, bz, sz, sp, base, bonus,
            "次日竞价介入(达标买)",
            extra={"streak": p.get("entry_streak"), "expected_top": p.get("expected_top"),
                   "hold_days": p.get("hold_days"), "rr": p.get("rr"),
                   "reach10": p.get("reach10"), "evidence": p.get("evidence"),
                   "sample_n": p.get("sample_n"), "close": _close}))

    # ---- 趋势票 ----
    import zones as _z
    for t in (rec.get("trend") or []):
        code = t.get("code")
        if not code:
            continue
        st = t.get("entry_state") or (t.get("entry") or {}).get("entry_state")
        # 只推「当下就是买点」的票：过热勿追/已破位/等回踩 一律不进买点候选
        if st in ("过热勿追", "已破位", "等回踩"):
            continue
        name = t.get("name") or ""
        board = primary_board(code, code2boards) or t.get("industry") or "—"
        bz = t.get("buy_zone")
        sz = t.get("sell_zone")
        base = (t.get("worth_score") or 0) or (t.get("kronos_score") or 0) or 50
        if not base or base <= 0:
            base = 50
        bonus = board_bonus(board, bmap)
        meta = t.get("trend_meta") or {}
        # 近端可执行买区（now_zone 现价附近 / pull_zone 回踩），替代远离现价的 band_levels 买区
        _bs = [b for b in (u.bars.get(code) or []) if b["d"] <= date]
        _ep = (t.get("entry") or {})
        if not _ep.get("now_zone") and _bs:
            try:
                _ep = _z.entry_plan(_bs, deep_zone=bz, stop=t.get("stop")) or {}
            except Exception:
                _ep = {}
        _buy_now = _ep.get("now_zone")
        _buy_pull = _ep.get("pull_zone")
        _st = _ep.get("entry_state") or st or "买点"
        trend.append(_mk_cand(
            code, name, "趋势", board, bz, sz, t.get("stop"), base, bonus,
            _st, buy_now=_buy_now, buy_pull=_buy_pull,
            extra={"close": t.get("close"), "streak": t.get("streak"),
                   "is_new": t.get("is_new"),
                   "continued": t.get("continued"), "times": t.get("times"),
                   "trend_state": meta.get("trend_state"), "band": meta.get("band"),
                   "avg_daily": meta.get("avg_daily"), "up_days": meta.get("up_days"),
                   "verdict": t.get("verdict")}))

    # ---- 波段票 ----
    import zones as _z
    for b in (rec.get("band_trade") or []):
        code = b.get("code")
        if not code:
            continue
        bz = b.get("buy_zone")
        sz = b.get("sell_zone")
        close = b.get("close")
        if not bz:
            continue
        # 只留「回到阶段底附近」的票，远离买区的跳过
        if close and bz[1] and close > float(bz[1]) * 1.05:
            continue
        name = b.get("name") or ""
        board = b.get("board") or "—"
        touches = b.get("touches") or 0
        bounce = b.get("bounce") or 0
        # 综合分拉开差距：阶段底触底次数(反复确认) + 刚启动反弹幅度；封顶 85，
        # 余量留给板块强度加权（board_bonus ±25），避免全 100 失去区分度。
        base = min(85, 40 + min(touches, 10) * 2.0 + min(max(bounce, 0), 18) * 0.6)
        bonus = board_bonus(board, bmap)
        # 近端可执行买区
        _bs = [x for x in (u.bars.get(code) or []) if x["d"] <= date]
        _ep = {}
        if _bs:
            try:
                _ep = _z.entry_plan(_bs, deep_zone=bz, stop=b.get("stop")) or {}
            except Exception:
                _ep = {}
        _buy_now = _ep.get("now_zone") or bz
        _buy_pull = _ep.get("pull_zone")
        band.append(_mk_cand(
            code, name, "波段", board, bz, sz, b.get("stop"), base, bonus,
            "阶段底买点", buy_now=_buy_now, buy_pull=_buy_pull,
            extra={"close": b.get("close"), "bottom": b.get("bottom"),
                   "touches": touches, "bounce": bounce}))

    ladder.sort(key=lambda x: -x["score"])
    trend.sort(key=lambda x: -x["score"])
    band.sort(key=lambda x: -x["score"])
    return {
        "ladder": ladder[:6],
        "trend": trend[:8],
        "band": band[:8],
        "ladder_warn": ladder_warn,
    }
