# -*- coding: utf-8 -*-
"""risklevel —— 风险预警三级分级（红/黄/蓝，纯标准库）。

目的（2026-09-04 用户确认的长期升级2）：
  把散落在各引擎里的风险信号统一收敛成「一眼可执行」的三级灯：
    🔴 红 · 立即行动：破位/追板回落/触发止损/市场恐慌——今天就该动作
    🟡 黄 · 提高警惕：接近止损、时间到期、板块转弱、破 MA10——设好预案
    🔵 蓝 · 正常跟踪：暂无结构性风险
  分级不是新算法，而是把 zones（止损/追板回落/到期）、holdings_ops（操作结论）、
  board_strength（板块退潮）、panic（大盘恐慌）按优先级编排成统一灯号。

输入：build 产出的 data（部分字段可能缺失，全部容错）。
输出 data["risk_levels"]：
  {
    "overall": {"level": "red|yellow|blue", "reasons": [...], "date": ...},
    "holdings": [{"code","name","level","reasons","decision"}],
    "counts": {"red": n, "yellow": n, "blue": n},
  }
规则优先级：红 > 黄 > 蓝（单标的命中多条时保留全部原因、灯号取最高）。
"""
from __future__ import annotations

# 单标的判定 ---------------------------------------------------------------
def classify_holding(op, board_strength=None):
    """op = holdings_ops 中的一条持仓操作记录 → (level, reasons)。"""
    reasons_r, reasons_y = [], []
    decision = op.get("decision") or ""
    stop = op.get("stop")
    close = op.get("close")
    pnl = op.get("pnl")

    # ---- 红级信号 ----
    if decision in ("卖出", "卖出换股"):
        reasons_r.append("引擎结论=%s" % decision)
    reasons = op.get("reasons") or []
    for r in reasons:
        rs = str(r)
        if "追板回落" in rs:
            reasons_r.append("追板回落·追高资金被套")
        if "破位" in rs or "止损" in rs or "割肉" in rs:
            reasons_r.append("技术破位/触发止损线")
    if stop and close and close <= stop:
        reasons_r.append("现价%.2f 已跌破止损%.2f" % (close, stop))

    # ---- 黄级信号 ----
    if decision in ("谨慎持有·观察",):
        reasons_y.append("引擎结论=谨慎持有·观察")
    if stop and close and stop < close <= stop * 1.04:
        reasons_y.append("贴近止损线（止损%.2f，现价%.2f）" % (stop, close))
    for r in reasons:
        rs = str(r)
        if ("⏰" in rs) or ("到期" in rs) or ("时间" in rs):
            reasons_y.append("持有时间到期/临近到期")
        if "MA10" in rs or "跌破" in rs:
            reasons_y.append(rs.strip("⚠ "))
    if pnl is not None and pnl <= -5:
        reasons_y.append("浮亏 %.1f%%，接近纪律线" % pnl)
    # 板块退潮：所属板块强度深度为负
    if board_strength is not None:
        bs = op.get("board_bonus")
        if isinstance(bs, (int, float)) and bs <= -15:
            reasons_y.append("所属板块强度 %+.0f（退潮）" % bs)

    if reasons_r:
        return "red", reasons_r
    if reasons_y:
        return "yellow", reasons_y
    return "blue", []


def compute(data, date=None):
    """主入口：data → risk_levels dict。全部容错，绝不抛出。"""
    try:
        return _compute(data, date)
    except Exception as e:
        return {"overall": {"level": "blue", "reasons": ["分级计算异常:%r" % e],
                            "date": date},
                "holdings": [], "counts": {"red": 0, "yellow": 0, "blue": 0}}


def _compute(data, date=None):
    bmap = data.get("board_strength") or {}
    ops = data.get("holdings_ops") or []
    holdings = []
    counts = {"red": 0, "yellow": 0, "blue": 0}
    for op in ops:
        if not isinstance(op, dict) or not op.get("code"):
            continue
        level, reasons = classify_holding(op, bmap)
        counts[level] = counts.get(level, 0) + 1
        holdings.append({"code": op.get("code"), "name": op.get("name"),
                         "level": level, "reasons": reasons,
                         "decision": op.get("decision")})

    # ---- 大盘级信号 ----
    ov_r, ov_y = [], []
    panic = data.get("panic") or {}
    if (panic.get("triggered") if isinstance(panic, dict) else None):
        ov_r.append("大盘恐慌信号触发：%s" % (panic.get("summary") or panic.get("reason") or ""))
    micro = data.get("micro") or {}
    try:
        sent = micro.get("sentiment")
        if isinstance(sent, dict):
            score = sent.get("score")
            if isinstance(score, (int, float)) and score <= 30:
                ov_y.append("短线情绪冰点（%s）" % (sent.get("label") or "冰点"))
    except Exception:
        pass
    regime = data.get("regime") or {}
    if isinstance(regime, dict) and regime.get("state") in ("退潮", "冰点", "恐慌"):
        ov_y.append("市场阶段=%s" % regime.get("state"))

    if ov_r or counts.get("red"):
        overall = "red"
        if counts.get("red") and not ov_r:
            ov_r.append("持仓中 %d 只亮红灯（见下）" % counts["red"])
    elif ov_y or counts.get("yellow"):
        overall = "yellow"
        if counts.get("yellow") and not ov_y:
            ov_y.append("持仓中 %d 只亮黄灯" % counts["yellow"])
    else:
        overall = "blue"

    return {
        "overall": {"level": overall,
                    "reasons": ov_r + ov_y,
                    "date": date},
        "holdings": holdings,
        "counts": counts,
    }


LEVEL_META = {
    "red": {"emoji": "🔴", "label": "红·立即行动", "color": "#e02020"},
    "yellow": {"emoji": "🟡", "label": "黄·提高警惕", "color": "#e6a700"},
    "blue": {"emoji": "🔵", "label": "蓝·正常跟踪", "color": "#2f6fed"},
}
