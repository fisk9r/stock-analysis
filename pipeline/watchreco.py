# -*- coding: utf-8 -*-
"""自选/持仓操作结论引擎（watchreco）：每只自选/持仓票给一条「跟着做」的明确动作。

用户诉求（2026-08-28）：
  · 自选股也要出现在推荐体系里（此前只进关注雷达，推荐区永远看不到）
  · 推荐股票给买入提示；持有股票给卖出/加仓/持有提示

实现：从 zones.items（已含买卖区/成本盈亏/rotate/追板回落/时间预警）提炼归一化动作：
  卖出（止损）> 卖出（止盈）> 离场换强 > 减仓 > 加仓 > 建议买入 > 回踩买入 > 持有 > 观望
纯提炼不改判定逻辑——判定单一真源仍是 zones.py。
"""
import os


def _emoji(act):
    """买卖动作 → 红绿 emoji（A股惯例：红=买/涨，绿=卖/跌）。推送 Markdown 无法着色文字，用圆形 emoji 区分。"""
    a = act or ""
    if a.startswith("卖出") or "卖出" in a or a in ("离场换强", "减仓", "止损", "割肉"):
        return "🟢"
    if a in ("建议买入", "回踩买入", "加仓") or "买入" in a:
        return "🔴"
    return "⚪"
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 动作紧急度（越小越靠前）——持仓票视角：卖出最优先（止盈止损关系真金白银）
_URGENCY = {
    "卖出（止损）": 0, "卖出（追板被套）": 0, "卖出（止盈）": 1,
    "离场换强": 2, "减仓": 3, "加仓": 4,
    "建议买入": 5, "回踩买入": 6, "持有（强势）": 7, "持有": 8, "观望": 9,
}

# 2026-09-02（用户拍板：我没买的票不要给我推送卖点）——非持仓票视角：买点最优先。
# 自选票的 zones「卖出」判定本是持仓者语境（进入卖出区=持仓者止盈），
# 对未买入者实际含义是「已过买点/走坏」，转译后按「现在能不能买」排序。
_URGENCY_UNHELD = {
    "建议买入": 0, "回踩买入": 1, "加仓": 2, "持有（强势）": 3, "持有": 4,
    "观望": 5, "不追（已过买点）": 6, "回避（趋势走坏）": 7,
}


def _verdict(it):
    """zones.item → 归一化动作。"""
    if it.get("action") == "破位卖出" or it.get("rotate") in ("止损", "割肉"):
        return "卖出（止损）"
    if it.get("zhuiban") and it.get("horizon") in ("短线", "超短线"):
        return "卖出（追板被套）"
    if it.get("rotate") == "更换":
        return "离场换强"
    a = it.get("action") or ""
    if "止盈" in a or "卖出" in a:
        return "卖出（止盈）"
    if a == "加仓提示":
        return "加仓"
    if a == "回踩买入区":
        return "回踩买入"
    if a == "突破持有":
        return "持有（强势）"
    if a == "正常持有":
        return "持有"
    return "观望"


def distill(zones_data, holding_codes=None, watch_names=None, topn=14, watch_codes=None):
    """zones.scan 输出 → 自选/持仓操作结论列表（按紧急度→买入价值排序）。

    holding_codes：持仓代码集合（用于区分「持仓」与「自选」语境）。
    watch_codes：用户关注池代码集合（notify/holdings/watch.json 合并）。
    返回 {"n": int, "sell_n": int, "buy_n": int, "items": [...]}。
    """
    items = []
    holding_codes = set(holding_codes or [])
    watch_codes = set(watch_codes or [])
    for it in (zones_data or {}).get("items") or []:
        try:
            code = it.get("code")
            v = _verdict(it)
            is_holding = code in holding_codes or it.get("cost")
            # 2026-09-02（用户拍板）：没买的票不给「卖出」动作——
            # 「卖出（止盈）」= 价格进入卖出区 → 未持仓者视角「不追（已过买点）」；
            # 「卖出（止损/追板被套）」= 走坏 → 未持仓者视角「回避（趋势走坏）」。
            # 持仓票（有成本/在持仓表）保留原卖出动作，止盈止损只对真金白银有意义。
            if not is_holding and v.startswith("卖出"):
                v = "不追（已过买点）" if "止盈" in v else "回避（趋势走坏）"
            buy = it.get("buy_zone") or [None, None]
            sell = it.get("sell_zone") or [None, None]
            rs = it.get("reasons") or []
            entry = {
                "code": code,
                "name": it.get("name") or (watch_names or {}).get(code) or code,
                "close": it.get("close"),
                "pct": it.get("pct"),
                "action": v,
                "is_holding": bool(is_holding),
                # 2026-09-01：是否用户关注池成员（自选股），用于排序前置
                "is_watch": code in watch_codes,
                "pnl_pct": it.get("pnl_pct"),
                "buy_zone": buy, "sell_zone": sell, "stop": it.get("stop"),
                "horizon": it.get("horizon"),
                "urgent": v.startswith("卖出") or it.get("urgent"),
                "reason": rs[0] if rs else "",
                "rotate_reason": it.get("rotate_reason") or "",
                "replace": it.get("replace") or [],
                "time_status": it.get("time_status"),
            }
            items.append(entry)
        except Exception:
            continue
    # 2026-09-01 排序前置（用户诉求：自选票加入后每天都要收到操作说明）：
    # 此前统一按紧急度排序，自选/持仓票常被推荐池的票挤到 n=6 之外被截断，
    # 表现为「票加进自选了但推送里看不到它的操作提示」（中化国际 600500 即此例）。
    # 组序：持仓(有成本) > 自选(关注池) > 其它推荐票；
    # 组内紧急度分视角：持仓票卖出优先（_URGENCY），非持仓票买点优先（_URGENCY_UNHELD，2026-09-02）。
    def _urg(x):
        tbl = _URGENCY if x.get("is_holding") else _URGENCY_UNHELD
        return tbl.get(x["action"], 9)

    items.sort(key=lambda x: (0 if x.get("is_holding") else (1 if x.get("is_watch") else 2),
                              _urg(x),
                              -(x.get("pnl_pct") if x.get("pnl_pct") is not None else -99)))
    items = items[:topn]
    return {
        "n": len(items),
        "sell_n": sum(1 for x in items if x["action"].startswith("卖出")),
        "buy_n": sum(1 for x in items if x["action"] in ("建议买入", "回踩买入", "加仓")),
        "items": items,
    }


def _fallback_replaces(item, rec, tol=1.15):
    """replace 候选被「现价远超买区」过滤清空、或 zones 本就没给（如止盈/离场）时的兜底：
    从推荐池补真正可买的票。用户 2026-09-03 拍板：提示卖出/割肉的票必须同时给出可以买入
    的票，不能只留「观望」让人无所适从。

    候选来源：推荐池全量（core/relay/ambush/all/trend）+ 连板计划，要求『现价就在买点附近』
    （close ≤ 买区上沿×tol），按价值分排序，排除原票，最多 2 只。
    注意：连板计划（ladder_plans）多为高位续强票，现价常远超买区上沿被过滤——所以主源
    改为带买区的推荐池本身，连板计划只作为补充。"""
    if not rec:
        return []
    sold = str(item.get("code") or "")
    cand = []
    seen = set()
    _pools = []
    for k in ("core", "relay", "ambush", "all", "trend"):
        _pools.extend(rec.get(k) or [])
    _pools.extend(rec.get("ladder_plans") or [])
    for x in _pools:
        c = str(x.get("code") or "")
        if not c or c in seen or c == sold:
            continue
        bz = x.get("buy_zone") or [None, None]
        if not (bz and bz[1]):
            continue
        pr = x.get("close")
        if pr is None:
            continue
        try:
            pr = float(pr)
        except Exception:
            continue
        if pr > float(bz[1]) * tol:
            continue
        seen.add(c)
        _st = (x.get("streak") or x.get("entry_streak") or 0)
        cand.append({
            "code": c, "name": x.get("name") or c,
            "industry": x.get("industry"),
            "buy_zone": bz, "sell_zone": x.get("sell_zone"),
            "stop": x.get("stop"), "close": round(pr, 2),
            "streak": _st,
            "worth": x.get("worth_score") or 0,
            "market_type": "连板" if _st >= 1 else "趋势",
            "tag": (x.get("tag") or x.get("channel") or ""),
        })
    # 2026-09-03 优化：用户要「能买的票」，优先推「现价就在买点附近」的票，
    # 而不是距离买区还有 8~12% 才能买到的远端票。先按「距买区上沿的溢出百分比」
    # 升序（越小越近、≤0 即已在买区内），再按价值分降序。
    def _gap(x):
        bz = x.get("buy_zone") or [None, None]
        try:
            return (float(x["close"]) / float(bz[1]) - 1) * 100
        except Exception:
            return 999.0
    cand.sort(key=lambda x: (max(0.0, _gap(x)), -x["worth"]))
    return cand[:2]


def lines(wr, n=6, compact=False, rec=None):
    """推送行：'- 持仓 **XX** 14.20 浮盈+8.2% → 加仓 ｜ 买点5.12~5.30 ／ 卖点6.10~6.30'

    2026-08-31 升级（用户要求：关注的股票操作必须提示买点和卖点）：
    每只自选/持仓票都同时给出买点（买区）与卖点（卖区，无卖区时退化为止损位），
    无论动作是买/卖/持有——持有票也要知道「在哪止盈、在哪止损」，买入票也要知道「目标在哪」。
    """
    if not wr:
        return []
    out = []
    for it in (wr.get("items") or [])[:n]:
        tag = "持仓" if it.get("is_holding") else "自选"
        seg = "- %s **%s**" % (tag, it["name"])
        if it.get("close"):
            seg += " %.2f" % it["close"]
        if it.get("pnl_pct") is not None:
            seg += " 浮盈%+.1f%%" % it["pnl_pct"]
        seg += " → %s **%s**" % (_emoji(it["action"]), it["action"])
        # 买卖点（用户 2026-08-31 要求：每只关注股都提示买点+卖点）
        bz = it.get("buy_zone") or [None, None]
        sz = it.get("sell_zone") or [None, None]
        pts = []
        if bz[0] is not None and bz[1] is not None:
            pts.append("买点%.2f~%.2f" % (bz[0], bz[1]))
        if sz[0] is not None and sz[1] is not None:
            pts.append("卖点%.2f~%.2f" % (sz[0], sz[1]))
        elif it.get("stop") is not None:
            pts.append("止损%.2f" % it["stop"])
        if pts:
            seg += " ｜ " + " ／ ".join(pts)
        # 2026-09-03（用户：给了一堆观望让我做什么）——「观望」必须给可执行的距离信息：
        # 现价距买区上沿 ≤5% 可买 / 5~20% 回踩关注 / >20% 建议放弃跟踪（别空等）。
        if (it.get("action") or "") in ("观望", "不追（已过买点）") and bz[0] and bz[1] and it.get("close"):
            try:
                _gap = (float(it["close"]) / float(bz[1]) - 1) * 100
            except Exception:
                _gap = None
            if _gap is not None:
                if _gap <= 5:
                    seg += " ｜ 🔴 距买点仅 %.1f%%，可挂单 %.2f~%.2f" % (_gap, bz[0], float(bz[1]))
                elif _gap <= 20:
                    seg += " ｜ 距买点 %.1f%%，回落至 %.2f 再关注" % (_gap, float(bz[1]))
                else:
                    seg += " ｜ 距买点 %.1f%%（短期难到，建议放弃跟踪）" % _gap
        note = it.get("rotate_reason") or it.get("reason") or ""
        if note and not compact:
            seg += "（%s）" % note[:28]
        out.append(seg)
        # 2026-09-01 用户需求：需要卖出/不追的票 → 给出买入建议（可连板票/可趋势票）
        # + 同板块优先 + 买入区间/卖出区间（zones.replace 候选已带 industry/market_type/buy_zone/sell_zone）
        # 2026-09-02：「不追（已过买点）」票同样附替代买入建议（用户诉求：推送要给能买的票）
        if ((it.get("action") or "").startswith("卖出")
                or (it.get("action") or "").startswith("不追")
                or it.get("rotate") or (it.get("action") == "离场换强")):
            # 2026-09-02 渲染层防御：只推「现价就在买点附近」的候选——
            # 现价距买区上沿超过 5% 的票（买区远低于现价，根本等不到）不推；
            # 先过滤再取前 2，避免无效候选占掉名额。
            _n = 0
            # 2026-09-03（用户：给了一堆观望让我做什么）：卖出/割肉必须给可买票。
            # 合并 zones 自带 replace（已带买卖区）+ 推荐池兜底（_fallback_replaces 从全量
            # 推荐池挑现价在买点附近的票），去重后最多给 2 只真正可买的票。
            _merged = []
            _seen = set()
            for _rp0 in (list(it.get("replace") or []) + _fallback_replaces(it, rec)):
                _c0 = str(_rp0.get("code") or "")
                if _c0 in _seen:
                    continue
                _seen.add(_c0)
                _merged.append(_rp0)
            for rp in _merged:
                _rp_close, _rp_bz = rp.get("close"), (rp.get("buy_zone") or [None, None])
                if _rp_close and _rp_bz[1] and _rp_close > float(_rp_bz[1]) * 1.15:
                    continue
                if _n >= 2:
                    break
                _n += 1
                _gap = ((_rp_close / float(_rp_bz[1]) - 1) * 100) if (_rp_close and _rp_bz[1]) else None
                mt = rp.get("market_type") or ("连板" if (rp.get("streak") or 0) >= 1 else "趋势")
                if mt == "连板":
                    mtag = "连板" + ("%d板" % rp["streak"] if rp.get("streak") else "票")
                else:
                    mtag = "趋势票"
                rseg = "  ↳ 买入建议（%s·%s）：%s(%s)" % (
                    "同板块" if (rp.get("industry") and rp.get("industry") == it.get("industry")) else "全市场",
                    mtag,
                    rp.get("name") or rp.get("code"),
                    rp.get("industry") or "—")
                bz2, sz2 = rp.get("buy_zone"), rp.get("sell_zone")
                if bz2 and bz2[0]:
                    rseg += " 买%.2f~%.2f" % (bz2[0], bz2[1])
                if sz2 and sz2[0]:
                    rseg += " 卖%.2f~%.2f" % (sz2[0], sz2[1])
                elif rp.get("stop"):
                    rseg += " 止损%.2f" % rp["stop"]
                # 距离买点多少（现价 vs 买区上沿）：可买直接标，需回踩标回落幅度
                if _gap is not None:
                    if _gap <= 0:
                        rseg += " ｜ ✅现价在买区内"
                    else:
                        rseg += " ｜ 需回落%.1f%%到买区" % _gap
                out.append(rseg)
    return out
