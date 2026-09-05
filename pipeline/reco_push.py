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
# ④ 个性化策略（2026-09-04 长期升级1：按持仓周期切换权重）
# ═══════════════════════════════════════════════════════════════════════════
# holdings.json 每条持仓/关注可标 "period": "short"|"mid"|"long"（缺省 mid）。
# 权重矩阵（乘在候选综合分 base 上，再叠加板块强度加权）：
#   short 短线：重连板/题材动量（竞价纪律 67.4% 实证），轻慢趋势与波段
#   mid   中线：三通道均衡，趋势略优先
#   long  长线：重波段阶段底与趋势质量（Kronos 结构分），连板题材大幅降权
PERIOD_PROFILES = {
    "short": {"ladder": 1.25, "trend": 0.95, "band": 0.70, "label": "短线"},
    "mid":   {"ladder": 1.00, "trend": 1.10, "band": 1.00, "label": "中线"},
    "long":  {"ladder": 0.60, "trend": 1.15, "band": 1.30, "label": "长线"},
}


def _period_of(p):
    """持仓/关注记录 → 周期档位（容错，缺省 mid）。"""
    pr = (p.get("period") or p.get("horizon") or "mid") if isinstance(p, dict) else "mid"
    pr = str(pr).strip().lower()
    if pr in ("short", "短线", "超短"):
        return "short"
    if pr in ("long", "长线"):
        return "long"
    return "mid"


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
                buy_zone,sell_zone,stop,horizon,reasons,replace,period,t1_locked}]。

    T+1 硬约束（2026-09-04 短期升级2）：持仓带 date(买入日)==分析日 → 当日锁定，
    任何「卖出」类结论一律压成 🔒T+1锁定（与 executor sell_decision 同一红线）。
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
        period = _period_of(p)
        buy_date = (p.get("date") or "").strip()
        t1_locked = bool(buy_date and buy_date == date)
        bs = [b for b in (u.bars.get(code) or []) if b["d"] <= date]
        name = p.get("name") or (u.stocks.get(code, {}) or {}).get("name") or code
        if len(bs) < 40:
            out.append({"code": code, "name": name, "decision": "持仓(数据不足)",
                        "emoji": "❓", "cost": cost, "close": None, "pnl": None,
                        "action": "", "rotate": None, "buy_zone": None,
                        "sell_zone": None, "stop": None, "horizon": None,
                        "period": period, "t1_locked": t1_locked,
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
                        "period": period, "t1_locked": t1_locked,
                        "reasons": ["%r" % e], "replace": []})
            continue
        decision, emoji = _map_holding_decision(r)
        reasons = list(r.get("reasons") or [])
        # ---- 个性化：周期口径微调（不推翻 zones 结论，只调提示） ----
        if period == "short" and r.get("time_alert") and decision not in ("卖出", "卖出换股"):
            decision, emoji = "周期到期·兑现", "⏰"
            reasons.append("短线周期到期：超短线持仓到期应兑现，不恋战")
        # ---- T+1 锁定（红线，压过一切卖出类结论） ----
        if t1_locked:
            reasons = ["🔒 今日(%s)买入，T+1 规则今日不可卖出" % buy_date] + reasons
            if decision in ("卖出", "卖出换股", "周期到期·兑现"):
                decision, emoji = "T+1锁定·明日执行", "🔒"
        out.append({
            "code": code, "name": name, "decision": decision, "emoji": emoji,
            "cost": cost, "pnl": r.get("pnl_pct"), "close": r.get("close"),
            "action": r.get("action"), "rotate": r.get("rotate"),
            "buy_zone": r.get("buy_zone"), "sell_zone": r.get("sell_zone"),
            "stop": r.get("stop"), "horizon": r.get("horizon"),
            "period": period, "t1_locked": t1_locked,
            "time_status": r.get("time_status"),
            "reasons": reasons, "replace": r.get("replace") or [],
        })
    # 卖出类置顶，确保最该看的先出现
    _ord = {"卖出": 0, "卖出换股": 1, "周期到期·兑现": 2, "T+1锁定·明日执行": 3,
            "加仓低吸": 4, "格局持有·注意止盈": 5,
            "谨慎持有·观察": 6, "继续持有·格局": 7}
    out.sort(key=lambda x: (_ord.get(x["decision"], 9), -(x.get("pnl") or 0)))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# ② 买点候选（连板 / 趋势 / 波段）
# ═══════════════════════════════════════════════════════════════════════════
def _one_line_reason(c):
    """一句话理由（#487）：说清「为什么是它」，与决策行配合看秒懂。
    #493：板块名后带今日强/弱标签（board_tag，由板块强度加权正负决定）。"""
    kind = c.get("kind")
    board = (c.get("board") or "—") + (c.get("board_tag") or "")
    if kind == "连板":
        return "%s板 · 预期%s · %s" % (c.get("streak") or "?",
                                       c.get("expected_top") or "—", board)
    if kind == "波段":
        # #488：按用户口径展示「底 X → 高 Y（空间%）」，可直接照着做
        lo = c.get("range_low") or c.get("bottom") or "?"
        hi = c.get("range_high")
        up = c.get("upside")
        seg = "底%s" % lo
        if hi:
            seg += "→高%s" % hi
        if up:
            seg += "(%+.0f%%)" % up
        return "区间%s 反复%s次 · %s" % (seg, c.get("touches") or 0, board)
    ts = c.get("trend_state") or c.get("band") or ""
    return "%s · %s" % ((ts + "趋势") if ts else "趋势向上", board)


def _decide(c, buy_now, bz, sz):
    """把一只候选压缩成**一句话可执行的决策**（2026-09-05 #487）。

    用户反馈：「每个都要看半天，到底买还是不买我也不清楚」——旧格式一行塞
    现价/买区/回踩区/卖区/综合分/基础分/板块分/形态状态共 8 个字段，却没有
    结论。这里直接算出三件事：
      action  ✅ 现在买 / ⏳ 等回踩X元 / 👀 观望
      buy_price  具体挂单价（不是区间）
      target/upside  目标价与空间%（值不值得买的核心判据）
    """
    close = c.get("close")
    buy_ref = buy_now or bz or None
    buy_px = None
    if buy_ref and len(buy_ref) >= 2 and buy_ref[1]:
        buy_px = round(float(buy_ref[1]), 2)
    tgt = None
    if sz and len(sz) >= 2 and sz[1]:
        tgt = round(float(sz[1]), 2)
    # 现价已在买区内（含 0.5% 容差）→ 现在就能买；否则给回踩挂单价
    if buy_px and close and float(close) <= buy_px * 1.005:
        action, emoji = "现在买", "✅"
        px = round(float(close), 2)
    elif buy_px:
        action, emoji = "等回踩%.2f" % buy_px, "⏳"
        px = buy_px
    else:
        action, emoji = "观望", "👀"
        px = round(float(close), 2) if close else None
    upside = round((tgt / px - 1) * 100, 1) if (px and tgt and px > 0) else None
    return action, emoji, px, tgt, upside, _one_line_reason(c)


def _mk_cand(code, name, kind, board, bz, sz, sp, base, bonus, entry_state,
             extra=None, buy_now=None, buy_pull=None):
    """buy_now = 近端可执行买区(now_zone, 现价附近, 直接可挂单)；buy_pull = 回踩买区(pull_zone)。
    buy_zone/sell_zone 保留为结构性参考区（band_levels）。推送主显示用 buy_now。

    2026-09-05 #487：额外算出 action/act_emoji/buy_price/target/upside/reason，
    让推送与网页都能「一眼看到买不买」。"""
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
    # 板块今日强/弱标签（#493 用户要求「板块后面加今日强弱」）：
    # board_bonus 即板块强度加权（正=今日强势板块加分，负=走弱减分）。
    c["board_tag"] = "🔥强" if bonus > 0 else ("❄弱" if bonus < 0 else "")
    # 决策字段依赖 extra（close/bottom/streak 等），须在 update 之后计算
    try:
        (c["action"], c["act_emoji"], c["buy_price"],
         c["target"], c["upside"], c["reason"]) = _decide(c, buy_now, bz, sz)
    except Exception:
        c.setdefault("action", "观望")
        c.setdefault("act_emoji", "👀")
        c.setdefault("buy_price", None)
        c.setdefault("target", None)
        c.setdefault("upside", None)
        c.setdefault("reason", "")
    return c


def compute_buy_candidates(rec, u, date, code2boards, bmap, ladder_warn=None,
                           period="mid"):
    """收拢三池「当下就是买点」的票，附板块提示 + 综合打分。

    连板：ladder_plans（次日竞价可追，买区=[close*0.995,close*1.03]）；
    趋势：rec["trend"]，只留 entry_state ∈ {可买, 微超}；
    波段：rec["band_trade"]，只留回到阶段底附近（close ≤ 买区上沿×1.05）。
    综合分 = (基础分 × 周期权重) + 板块强度加权（board_bonus）。
    period ∈ short/mid/long：个性化策略权重（PERIOD_PROFILES），影响各通道排序。
    """
    w = PERIOD_PROFILES.get(period, PERIOD_PROFILES["mid"])
    # 市场准入（2026-09-05 #486）：科创板(688/689)、北交所(43/83/87/88/920)
    # 用户未开通，推了也买不了 → 三池统一先过滤。
    import mktfilter as _mkt
    ladder = []
    trend = []
    band = []

    # ---- 连板票 ----
    for p in (rec.get("ladder_plans") or []):
        code = p.get("code")
        if not code or not _mkt.tradable(code):
            continue
        name = p.get("name") or ""
        board = primary_board(code, code2boards)
        bz = p.get("buy_zone")
        sz = p.get("sell_zone")
        sp = p.get("stop")
        # 连板计划不直接带 close，但 buy_zone=[close*0.995, close*1.03] → close≈bz[0]/0.995
        _close = round(float(bz[0]) / 0.995, 2) if (bz and bz[0]) else None
        # 2026-09-05 #493 评分 bug 修复：旧公式 base = rr*10（rr≈0.1 → 1 分），
        # 量纲完全错误——连板票在 0-100 分制里全线趴底，环境加权后永远进不了
        # Top5（线上实测连板最高 1 分 vs 波段 90 分）。改为有意义组合：
        # 55 基础 + rr 贡献(≤25) + 10板到达率贡献(≤15)，ladder_plans 无
        # worth_score 字段（grep 证实），此函数即连板池唯一定分处。
        _rr = float(p.get("rr") or 0)
        _r10 = float(p.get("reach10") or 0)
        base = 55 + min(25, max(0, _rr) * 12) + min(15, max(0, _r10) * 15)
        bonus = board_bonus(board, bmap)
        ladder.append(_mk_cand(
            code, name, "连板", board, bz, sz, sp, base * w["ladder"], bonus,
            "次日竞价介入(达标买)",
            extra={"streak": p.get("entry_streak"), "expected_top": p.get("expected_top"),
                   "hold_days": p.get("hold_days"), "rr": p.get("rr"),
                   "reach10": p.get("reach10"), "evidence": p.get("evidence"),
                   "sample_n": p.get("sample_n"), "close": _close,
                   "period": w["label"]}))

    # ---- 趋势票 ----
    import zones as _z
    for t in (rec.get("trend") or []):
        code = t.get("code")
        if not code or not _mkt.tradable(code):
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
            code, name, "趋势", board, bz, sz, t.get("stop"), base * w["trend"], bonus,
            _st, buy_now=_buy_now, buy_pull=_buy_pull,
            extra={"close": t.get("close"), "streak": t.get("streak"),
                   "is_new": t.get("is_new"),
                   "continued": t.get("continued"), "times": t.get("times"),
                   "trend_state": meta.get("trend_state"), "band": meta.get("band"),
                   "avg_daily": meta.get("avg_daily"), "up_days": meta.get("up_days"),
                   "verdict": t.get("verdict"), "period": w["label"]}))

    # ---- 波段票 ----
    import zones as _z
    for b in (rec.get("band_trade") or []):
        code = b.get("code")
        if not code or not _mkt.tradable(code):
            continue
        bz = b.get("buy_zone")
        sz = b.get("sell_zone")
        close = b.get("close")
        if not bz:
            continue
        # 只留「回到阶段底附近」的票，远离买区的跳过（bandtrade 已限 ≤箱底×1.12；
        # bz[1]=箱底×1.05，×1.07 ≈ 箱底×1.12，与引擎口径对齐）
        if close and bz[1] and close > float(bz[1]) * 1.07:
            continue
        name = b.get("name") or ""
        board = b.get("board") or "—"
        touches = b.get("touches") or 0
        tops = b.get("tops") or 0
        # 综合分拉开差距：箱底回踩次数(支撑可靠) + 箱顶触及次数(上沿有效)；封顶 85，
        # 余量留给板块强度加权（board_bonus ±25），避免全 100 失去区分度。
        base = min(85, 40 + min(touches, 10) * 2.0 + min(tops, 10) * 1.5)
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
                   "touches": touches, "tops": tops,
                   "pos_in_box": b.get("pos_in_box"),
                   # #488：阶段底→区间高点与空间，供决策行与理由展示
                   "range_low": b.get("range_low") or b.get("bottom"),
                   "range_high": b.get("range_high"),
                   "upside": b.get("upside")}))

    ladder.sort(key=lambda x: -x["score"])
    trend.sort(key=lambda x: -x["score"])
    band.sort(key=lambda x: -x["score"])
    # #487：每池收紧（6/8/8 → 5/6/6）——决策卡改成两行后单条更长，
    # 保持推送总量不膨胀，宁可少推几只也要每只看得清。
    return {
        "ladder": ladder[:5],
        "trend": trend[:6],
        "band": band[:6],
        "ladder_warn": ladder_warn,
        "period": w["label"],
    }


# ═══════════════════════════════════════════════════════════════════════════
# ④ 今日该买什么（2026-09-05 #490）
# ═══════════════════════════════════════════════════════════════════════════
def env_bias(data):
    """按市场环境给三类池加权系数 + 一句话环境结论。

    用户的问题：「到底买连板还是区间？」——答案随环境变：
      接力好（晋级率高、炸板少）→ 连板加分；
      炸板多/退潮 → 连板降权，波段（低吸）加分；
      情绪暖 → 趋势延续加分。
    口径来源：sentiment.promote_rate（晋级率）/ micro.zhaban_rate（炸板率）/
    sentiment.score（情绪分），均为 data 既有字段。
    """
    sent = ((data.get("market") or {}).get("sentiment") or {})
    micro = data.get("micro") or {}
    promote = float(sent.get("promote_rate") or 0)
    zhaban = float(micro.get("zhaban_rate") or 0)
    score = float(sent.get("score") or 50)
    # 单位归一（2026-09-05 线上核验发现：线上 zhaban_rate=63.2 是百分数，
    # 按小数解释会显示"炸板率6320%"且阈值判断漂移）。>1 即百分数口径。
    if promote > 1:
        promote /= 100
    if zhaban > 1:
        zhaban /= 100
    w = {"连板": 1.0, "趋势": 1.0, "波段": 1.0}
    notes = []
    if promote >= 0.55 and zhaban <= 0.30:
        w["连板"] *= 1.25
        notes.append("涨停晋级率%.0f%%且炸板仅%.0f%%，接力环境好，连板优先"
                     % (promote * 100, zhaban * 100))
    elif zhaban >= 0.40:
        w["连板"] *= 0.70
        notes.append("炸板率%.0f%%偏高（封板不稳），降连板权重" % (zhaban * 100))
    elif promote < 0.40:
        w["连板"] *= 0.85
        notes.append("晋级率仅%.0f%%，接力偏弱" % (promote * 100))
    if score >= 60:
        w["趋势"] *= 1.15
        notes.append("情绪分%.0f偏暖，趋势延续概率高" % score)
    if score <= 40 or (promote < 0.40 and zhaban >= 0.35):
        w["波段"] *= 1.20
        notes.append("情绪偏弱/退潮，低吸波段优于追高")
    return w, "；".join(notes) or "环境中性，三类平权"


def compute_top_picks(cands, data, topn=5):
    """三池合并 → 环境加权统一评分 → 「今日该买什么」优先级 TopN。

    与 buy_candidates 的分工：buy_candidates 按池分类展示（谁都能看懂类型），
    top_picks 直接回答「现在最该买哪只、为什么是它」。决策优先排序：
    ✅现在买 永远排在 ⏳等回踩 / 👀观望 之前。
    """
    w, note = env_bias(data)
    all_ = []
    for kind, key in (("连板", "ladder"), ("趋势", "trend"), ("波段", "band")):
        for c in (cands.get(key) or []):
            # 2026-09-05 用户口径：top_picks 只收**能买的票**——✅现在买/⏳等回踩
            # （有明确挂单价），👀观望（无买点）与天上票不进这个清单。
            if (c.get("action") not in ("现在买", "等回踩")):
                continue
            x = dict(c)
            x["score"] = round(min(100, (c.get("score") or 0) * w.get(kind, 1.0)), 1)
            x["env_weight"] = round(w.get(kind, 1.0), 2)
            all_.append(x)
    ordm = {"现在买": 0, "等回踩": 1, "观望": 2}
    all_.sort(key=lambda x: (ordm.get(x.get("action"), 3), -(x.get("score") or 0)))
    return {
        "items": all_[:topn],
        "env_note": note,
        "weights": {k: round(v, 2) for k, v in w.items()},
    }
