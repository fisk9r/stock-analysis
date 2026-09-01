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

# 动作紧急度（越小越靠前）
_URGENCY = {
    "卖出（止损）": 0, "卖出（追板被套）": 0, "卖出（止盈）": 1,
    "离场换强": 2, "减仓": 3, "加仓": 4,
    "建议买入": 5, "回踩买入": 6, "持有（强势）": 7, "持有": 8, "观望": 9,
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
    # 现在顺序：持仓(有成本) > 自选(关注池) > 其它推荐票，组内再按紧急度排序。
    items.sort(key=lambda x: (0 if x.get("is_holding") else (1 if x.get("is_watch") else 2),
                              _URGENCY.get(x["action"], 9),
                              -(x.get("pnl_pct") if x.get("pnl_pct") is not None else -99)))
    items = items[:topn]
    return {
        "n": len(items),
        "sell_n": sum(1 for x in items if x["action"].startswith("卖出")),
        "buy_n": sum(1 for x in items if x["action"] in ("建议买入", "回踩买入", "加仓")),
        "items": items,
    }


def lines(wr, n=6, compact=False):
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
        note = it.get("rotate_reason") or it.get("reason") or ""
        if note and not compact:
            seg += "（%s）" % note[:28]
        out.append(seg)
        # 2026-09-01 用户需求：需要卖出的票 → 给出买入建议（可连板票/可趋势票）
        # + 同板块优先 + 买入区间/卖出区间（zones.replace 候选已带 industry/market_type/buy_zone/sell_zone）
        if (it.get("action") or "").startswith("卖出") or it.get("rotate") or (it.get("action") == "离场换强"):
            for rp in (it.get("replace") or [])[:2]:
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
                out.append(rseg)
    return out
