# -*- coding: utf-8 -*-
"""触发式盯盘：把「波段区 / 持仓成本」转化为可执行的条件单命中。

纯本地、零网络：输入来自 build 已算好的 data（zones / holdings / watch），
输出一组「触发事件」，供 notifier 经 PushPlus 即时推送（盘中/盘后均可）。

触发类型：
  止损  —— 持仓或关注票跌破止损线（高优先级）
  止盈  —— 触及卖出区（分批止盈/突破上沿）
  买点  —— 回踩买入区/加仓提示（低吸机会）
  锁定  —— 关注以来涨幅过大，提示部分获利了结
"""
SEV = {"止损": 3, "止盈": 2, "买点": 1, "锁定": 1}


def _classify(action):
    if action in ("破位卖出", "止损"):
        return "止损"
    if action in ("逼近卖出", "突破持有", "卖点"):
        return "止盈"
    if action in ("回踩买入区", "加仓提示", "买点"):
        return "买点"
    return None


def build_triggers(data, date):
    """输入完整 data dict，返回 {date, n, hits:[...]}。无命中返回空结构。"""
    zones = (data or {}).get("zones") or {}
    holdings = (data or {}).get("holdings") or {}
    watch = (data or {}).get("watch") or {}

    hold_codes = {it.get("code") for it in (holdings.get("items") or [])}
    watch_codes = {it.get("code") for it in (watch.get("items") or [])}

    hits = []

    # 1) 波段区动作（覆盖关注+推荐头部）
    for it in (zones.get("items") or []):
        c = it.get("code")
        if not c:
            continue
        tp = _classify(it.get("action"))
        if not tp:
            continue
        ref = it.get("stop") or (it.get("sell_zone") or [None])[0] or it.get("close")
        detail = "%s：现价 %.2f，%s" % (
            tp, it.get("close"), it.get("advice") or it.get("action"))
        hits.append({"code": c, "name": it.get("name", ""),
                     "type": tp, "sev": SEV[tp],
                     "pool": "持仓" if c in hold_codes else ("关注" if c in watch_codes else "推荐"),
                     "detail": detail, "ref": ref})

    # 2) 持仓卖出建议（holdings.diagnose 已带 band_action）
    for it in (holdings.get("items") or []):
        c = it.get("code")
        if not c:
            continue
        ba = it.get("band_action")
        tp = _classify(ba) if ba else None
        if tp == "买点":
            tp = None  # 持仓里“买点”不算触发卖出，跳过
        if not tp and it.get("sell_advice"):
            tp = "止盈" if "止盈" in (it.get("sell_advice") or "") else None
        if not tp:
            continue
        hits.append({"code": c, "name": it.get("name", ""),
                     "type": tp, "sev": SEV[tp], "pool": "持仓",
                     "detail": it.get("sell_advice") or ba or "",
                     "ref": it.get("stop_band") or (it.get("sell_zone") or [None])[0]})

    # 3) 关注以来大幅盈利 → 提示锁定
    for it in (watch.get("items") or []):
        c = it.get("code")
        if not c:
            continue
        sa = it.get("since_added") or {}
        pct = sa.get("pct")
        if pct is not None and pct >= 30:
            hits.append({"code": c, "name": it.get("name", ""),
                         "type": "锁定", "sev": SEV["锁定"], "pool": "关注",
                         "detail": "关注以来累计 +%.1f%%，可考虑部分获利了结" % pct,
                         "ref": it.get("close")})

    # 去重（同 code+type 仅留最严重一条）
    uniq = {}
    for h in hits:
        k = (h["code"], h["type"])
        if k not in uniq or h["sev"] > uniq[k]["sev"]:
            uniq[k] = h
    hits = sorted(uniq.values(), key=lambda x: (-x["sev"], x["code"]))
    return {"date": date, "n": len(hits), "hits": hits}


def summary_lines(tr):
    if not tr or not tr.get("hits"):
        return []
    out = ["触发盯盘：%d 条条件命中" % tr["n"]]
    for h in tr["hits"][:8]:
        out.append("· 【%s·%s】%s（%s）：%s" % (h["pool"], h["type"], h["name"], h["code"], h["detail"]))
    return out
