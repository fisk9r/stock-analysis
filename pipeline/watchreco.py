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


def distill(zones_data, holding_codes=None, watch_names=None, topn=14):
    """zones.scan 输出 → 自选/持仓操作结论列表（按紧急度→买入价值排序）。

    holding_codes：持仓代码集合（用于区分「持仓」与「自选」语境）。
    返回 {"n": int, "sell_n": int, "buy_n": int, "items": [...]}。
    """
    items = []
    holding_codes = set(holding_codes or [])
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
    items.sort(key=lambda x: (_URGENCY.get(x["action"], 9),
                              -(x.get("pnl_pct") if x.get("pnl_pct") is not None else -99)))
    items = items[:topn]
    return {
        "n": len(items),
        "sell_n": sum(1 for x in items if x["action"].startswith("卖出")),
        "buy_n": sum(1 for x in items if x["action"] in ("建议买入", "回踩买入", "加仓")),
        "items": items,
    }


def lines(wr, n=6, compact=False):
    """推送行：'- 持仓 **XX** 14.20 浮盈+8.2% → 加仓 ｜ 买区5.12~5.30 缩量企稳'"""
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
        seg += " → **%s**" % it["action"]
        note = it.get("rotate_reason") or it.get("reason") or ""
        if it.get("action") in ("建议买入", "回踩买入", "加仓") and it.get("buy_zone", [None])[0]:
            seg += " ｜ 买区%.2f~%.2f" % (it["buy_zone"][0], it["buy_zone"][1])
        elif it["action"].startswith("卖出") and it.get("stop"):
            seg += " ｜ 止损位%.2f" % it["stop"]
        if note and not compact:
            seg += " (%s)" % note[:30]
        out.append(seg)
    return out
