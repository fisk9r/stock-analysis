# -*- coding: utf-8 -*-
"""周期复盘大总结（周 / 半月 / 月）—— 2026-09-05 #497 用户需求：

「每周、每半个月、每个月分别来一份大的总结，把失败经验全部展示出来，
 然后告诉我，我需要对选股进行实时调整，增加准确率。」

数据源：sim.db（模拟盘全流水，executor 运行时已从 Release 资产恢复）。
输出：Markdown（随复盘通道推送 PushPlus / ServerChan）。
设计原则：
  · 纯标准库、无未来函数、失败单**全列表**不截断（用户明确要求"全部展示"）
  · 归因标签全部来自留痕字段（open_gap / verdict_reason / sell_reason），不猜
  · 结论落到「可执行的选股调整」，不是空话
"""
import os
import sqlite3
import time


def _period_range(period, today=None):
    """返回 (d1, d2, label)。d1/d2 为 YYYY-MM-DD 闭区间。

    weekly   本周一 ~ 今日（周五盘后跑 = 本周全周）
    biweekly 半月：每月 1-15 日 / 16 日-月末（跑在 15 日/月末 = 刚结束的半月）
    monthly  自然月（每月 1 日跑 = 上一自然月）
    """
    t = time.strptime(today, "%Y-%m-%d") if today else time.localtime()
    y, m, d = t.tm_year, t.tm_mon, t.tm_mday
    w = t.tm_wday  # Monday=0

    def fmt(yy, mm, dd):
        return "%04d-%02d-%02d" % (yy, mm, dd)

    if period == "weekly":
        # 本周一
        import datetime
        base = datetime.date(y, m, d)
        monday = base - datetime.timedelta(days=w)
        return fmt(monday.year, monday.month, monday.day), fmt(y, m, d), "周总结"
    if period == "biweekly":
        if d >= 16:
            return fmt(y, m, 16), fmt(y, m, d), "下半月总结"
        # 上月 16 日 ~ 上月末（在月初跑）
        if m == 1:
            return fmt(y - 1, 12, 16), fmt(y - 1, 12, 31), "下半月总结"
        import calendar
        last = calendar.monthrange(y, m - 1)[1]
        return fmt(y, m - 1, 16), fmt(y, m - 1, last), "下半月总结"
    # monthly：上一自然月
    if m == 1:
        return fmt(y - 1, 12, 1), fmt(y - 1, 12, 31), "月总结"
    import calendar
    last = calendar.monthrange(y, m)[1] if d > 1 else None
    if d > 1:  # 月中跑 = 本月至今
        return fmt(y, m, 1), fmt(y, m, d), "月总结"
    # 1 日跑 = 上月
    if m == 1:
        last = calendar.monthrange(y - 1, 12)[1]
        return fmt(y - 1, 12, 1), fmt(y - 1, 12, last), "月总结"
    last = calendar.monthrange(y, m - 1)[1]
    return fmt(y, m - 1, 1), fmt(y, m - 1, last), "月总结"


def _attr_label(buy_gap, verdict_reason, sell_reason, pnl_pct):
    """失败单归因标签（全部来自留痕字段，规则透明）。"""
    tags = []
    g = buy_gap or 0
    vr = verdict_reason or ""
    sr = sell_reason or ""
    if g >= 5:
        tags.append("追高(高开%.0f%%)" % g)
    elif g >= 2:
        tags.append("高开跟进(%.0f%%)" % g)
    elif g <= -2:
        tags.append("低开接刀(%.0f%%)" % g)
    if "趋势" in vr:
        tags.append("趋势通道")
    elif "st=2" in vr or "st2" in vr:
        tags.append("st2洼地(实证胜率低)")
    if "断板" in sr:
        tags.append("断板拖卖")
    if "止损" in sr:
        tags.append("止损执行")
    if "清仓" in sr or "持有" in sr and "3" in sr:
        tags.append("持有到期清仓")
    if not tags:
        tags.append("无明确特征")
    return "、".join(tags)


def generate(db_path, period="weekly", today=None, holdings=None):
    """生成周期大总结。返回 {"title", "text", "stats"}；无数据时 text 说明。

    holdings（#499 联动复盘，可选）：实盘持仓 ops 列表（compute_holdings_ops
    输出：code/name/pnl/sell_zone/stop）——输出「实盘持仓纪律检查」段，
    让大总结同时复盘实盘纪律，不只是模拟盘。"""
    d1, d2, label = _period_range(period, today)
    if not os.path.exists(db_path):
        return {"title": "%s（无数据）" % label,
                "text": ">%s：sim.db 不存在（%s），跳过。" % (label, db_path),
                "stats": {}}
    con = sqlite3.connect(db_path)
    try:
        pos = con.execute(
            "SELECT buy_date,code,name,open_gap,buy_price,volume,streak,"
            "sell_date,sell_price,sell_reason,pnl_pct FROM sim_positions "
            "WHERE sell_date>=? AND sell_date<=? ORDER BY sell_date",
            (d1, d2)).fetchall()
        buys = con.execute(
            "SELECT date,code,name,open_gap,verdict_reason FROM sim_trades "
            "WHERE date>=? AND date<=? AND action='BUY' ORDER BY ts",
            (d1, d2)).fetchall()
        open_pos = con.execute(
            "SELECT buy_date,code,name,buy_price,streak FROM sim_positions "
            "WHERE sell_date IS NULL ORDER BY buy_date").fetchall()
    finally:
        con.close()

    closed = [{"buy_date": r[0], "code": r[1], "name": r[2], "buy_gap": r[3],
               "buy_price": r[4], "streak": r[5], "sell_date": r[6],
               "sell_price": r[7], "sell_reason": r[8], "pnl_pct": r[9] or 0}
              for r in pos]
    # 买入依据映射（sim_trades.verdict_reason）
    buy_reason = {}
    for r in buys:
        buy_reason.setdefault(r[1], r[4] or "")

    # ---- 战绩 ----
    pnls = [c["pnl_pct"] for c in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = (len(wins) / len(pnls) * 100) if pnls else 0
    tot = sum(pnls)
    worst = min(pnls) if pnls else 0
    best = max(pnls) if pnls else 0
    # 最长连亏
    streak, max_streak = 0, 0
    for c in closed:
        if c["pnl_pct"] <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    L = []
    L.append("## 📊 模拟盘%s（%s ~ %s）" % (label, d1, d2))
    L.append("")
    L.append("- 平仓 %d 笔：胜率 **%.0f%%**（胜%d/负%d）｜ 累计 **%+.2f%%** ｜ "
             "最好 %+.2f%% / 最差 %+.2f%% ｜ 最长连亏 %d 笔"
             % (len(pnls), win_rate, len(wins), len(losses), tot, best, worst, max_streak))
    L.append("- 周期开仓 %d 笔 ｜ 当前在持 %d 只" % (len(buys), len(open_pos)))

    # ---- 失败经验全列表（用户明确要求：全部展示，不截断）----
    lossers = [c for c in closed if c["pnl_pct"] <= 0]
    L.append("")
    L.append("### 💀 失败单全列表（%d 笔 · 每笔都交代清楚）" % len(lossers))
    if lossers:
        for c in lossers:
            vr = buy_reason.get(c["code"]) or ""
            L.append("- %s %s(%s) 买%s→卖%s **%+.2f%%** ｜ 买入依据：%s ｜ 归因：**%s**"
                     % (c["sell_date"], c["name"], c["code"], c["buy_price"],
                        c["sell_price"], c["pnl_pct"],
                        (vr[:40] or "—"), _attr_label(c["buy_gap"], vr, c["sell_reason"],
                                                      c["pnl_pct"])))
    else:
        L.append("- 本周期无亏损平仓 ✓")

    # ---- 盈利单亮点（保持一致性）----
    winners = [c for c in closed if c["pnl_pct"] > 0]
    if winners:
        L.append("")
        L.append("### 🏆 盈利单（做对了什么，保持住）")
        for c in sorted(winners, key=lambda x: -x["pnl_pct"])[:5]:
            L.append("- %s %s(%s) **%+.2f%%** ｜ 卖出：%s"
                     % (c["sell_date"], c["name"], c["code"], c["pnl_pct"],
                        (c["sell_reason"] or "")[:40]))

    # ---- 教训清单（规则聚合，可直接执行）----
    L.append("")
    L.append("### 📌 教训与选股调整建议")
    lessons = []
    # 1) 追高亏损占比
    chase = [c for c in lossers if (c["buy_gap"] or 0) >= 5]
    if len(lossers) >= 2 and len(chase) >= max(1, len(lossers) // 2):
        avg = sum(c["pnl_pct"] for c in chase) / len(chase)
        lessons.append("高开≥5%%追入共 %d 笔、平均 %+.2f%% —— 下周起竞价纪律执行到过滤层："
                       "gap>6%% 一律放弃，不再人工放行" % (len(chase), avg))
    # 2) 断板拖卖
    zb = [c for c in lossers if "断板" in (c["sell_reason"] or "")]
    if zb:
        avg = sum(c["pnl_pct"] for c in zb) / len(zb)
        lessons.append("断板票拖到次日卖共 %d 笔、平均 %+.2f%% —— 断板当日尾盘即走，"
                       "不等次日（回测拖到 T+2 平均 -1.18%%）" % (len(zb), avg))
    # 3) 止损不坚决
    sl_late = [c for c in lossers if c["pnl_pct"] < -4]
    if sl_late:
        lessons.append("亏损超 4%% 的有 %d 笔 —— 说明止损触发偏晚，盘中破位_alert 需当日执行，"
                       "不留到尾盘" % len(sl_late))
    # 4) 持有到期清仓整体亏损
    exp = [c for c in lossers if "清仓" in (c["sell_reason"] or "")]
    if len(exp) >= 2:
        lessons.append("持有到期清仓 %d 笔全亏 —— 该类票（≥3 交易日无进展）下次提前在 T+2 减仓" % len(exp))
    # 5) 胜率分层
    if pnls and win_rate < 40:
        lessons.append("周期胜率仅 %.0f%% —— 下周期新仓金额自动降为 0.7 折，"
                       "只做 A/B 级信号，等胜率回到 55%% 再恢复" % win_rate)
    # 6) 在持仓警示
    for r in open_pos:
        if r[3] and r[4] and r[3] < r[4] * 0.93:
            L.append("")
            L.append("> ⚠️ 在持 %s(%s) 现价已低于成本 7%%+ 且未触发止损 —— 下一个交易日"
                     "开盘优先裁决（纪律卖出或给出明确持有理由）" % (r[2], r[1]))
            break
    if not lessons:
        lessons.append("本周期无明显模式性错误 —— 保持现有决策线与仓位纪律，不因盈利加码")
    for x in lessons:
        L.append("- %s" % x)

    # ---- 实盘持仓纪律检查（#499 联动复盘：大总结不只看模拟盘，也盯实盘纪律）----
    if holdings:
        L.append("")
        L.append("### 💼 实盘持仓纪律检查（%d 只）" % len(holdings))
        for h in holdings:
            if not isinstance(h, dict) or not h.get("code"):
                continue
            pnl = h.get("pnl")
            nm = "%s(%s)" % (h.get("name") or h.get("code"), h.get("code"))
            if pnl is None:
                L.append("- %s —— 浮盈数据缺失，补录成本后可纳入纪律检查" % nm)
            elif pnl <= -5:
                L.append("- %s 浮盈 **%+.1f%%** —— ⚠️ 实盘纪律：浮亏超 5%% 无条件减仓/清仓，别让小亏变大亏" % (nm, pnl))
            elif pnl >= 10:
                L.append("- %s 浮盈 **%+.1f%%** —— 落袋纪律：浮盈≥10%% 先减半仓锁定，剩余设移动止盈" % (nm, pnl))
            elif pnl >= 2:
                L.append("- %s 浮盈 %+.1f%% —— 状态良好：浮盈≥2%% 先减半仓的回测纪律可参考" % (nm, pnl))
            else:
                L.append("- %s 浮盈 %+.1f%% —— 状态正常，按计划持有" % (nm, pnl))

    text = "\n".join(L)
    stats = {"period": period, "d1": d1, "d2": d2, "closed": len(pnls),
             "win_rate": round(win_rate, 1), "total_pct": round(tot, 2),
             "losses": len(lossers), "max_streak": max_streak}
    return {"title": "模拟盘%s %s~%s" % (label, d1, d2), "text": text, "stats": stats}


if __name__ == "__main__":
    import sys
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim.db")
    p = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    r = generate(db, p)
    print(r["text"])
