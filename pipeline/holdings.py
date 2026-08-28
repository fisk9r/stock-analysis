# -*- coding: utf-8 -*-
"""持股监测：对已持仓/关注标的做逐日体检 + 未来预判 + 持续跟踪

核心能力
  1. 盈亏与结构体检：均线排列、量能、距高点回撤、连板高度；
  2. 未来预判：把『趋势×位置×量能』分成 27 类状态桶，用本地日K库实测
     每类状态的次日/三日后收益分布与上涨概率（自校准，非拍脑袋）；
  3. 目标位 / 止损位：技术位就近取值，给出明确动作建议与评级；
  4. 持续关注：每日快照落库 holdings_track，与昨日对比，评级恶化即预警。

持仓来源（任一）：
  · config/holdings.json      —— 仓库内文件（CI 与本地都读得到）
  · 环境变量 HOLDINGS_JSON    —— CI Secret 注入，适合不想让持仓进仓库的场景
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.path.join(ROOT, "config", "holdings.json")

TRACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS holdings_track(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  price REAL, pnl_pct REAL, grade TEXT, action TEXT,
  risks TEXT, p_up1 REAL, created_at TEXT,
  PRIMARY KEY(date, code)
) WITHOUT ROWID;
"""


# ============================================================== 持仓读取
def _norm_pos(it):
    """把一条持仓记录归一化为标准 dict；非法则返回 None。"""
    if not isinstance(it, dict):
        return None
    code = str(it.get("code") or "").strip()
    if not code.isdigit() or len(code) != 6:
        return None
    try:
        cost = float(it.get("cost") or 0) or None
    except Exception:
        cost = None
    try:
        shares = float(it.get("shares") or 0) or None
    except Exception:
        shares = None
    return {"code": code, "name": (it.get("name") or "").strip(),
            "cost": cost, "shares": shares,
            "date": (it.get("date") or "").strip(),
            "note": (it.get("note") or "").strip(),
            "horizon": (it.get("horizon") or "").strip(),
            "watch": bool(it.get("watch")) or cost is None}


def load_positions():
    """-> [{code,name,cost,shares,date,note,watch}]，容错：缺字段自动补全。"""
    raw = None
    env = os.environ.get("HOLDINGS_JSON", "").strip()
    if env:
        try:
            raw = json.loads(env)
        except Exception as e:
            print("[holdings] 环境变量 HOLDINGS_JSON 解析失败：%r" % e)
    if raw is None and os.path.exists(CONF):
        try:
            with open(CONF, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print("[holdings] 读取 %s 失败：%r" % (CONF, e))
    if not raw:
        return []
    items = raw.get("positions") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [p for p in (_norm_pos(it) for it in items) if p]


# ============================================================== 状态分桶 + 实测前瞻
def _trend_tag(ma5, ma10, ma20, price):
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20 and price >= ma5:
            return "多头"
        if ma5 < ma10 < ma20:
            return "空头"
    return "走平"


def _pos_tag(dd60):
    if dd60 > -5:
        return "高位"
    if dd60 > -20:
        return "中位"
    return "低位"


def _vol_tag(vr):
    if vr >= 1.5:
        return "放量"
    if vr < 0.7:
        return "缩量"
    return "常量"


def _bucket(trend, pos, vol):
    return trend + "|" + pos + "|" + vol


def forward_stats(u, con, use_cache=True):
    """实测各状态桶的前瞻收益：{bucket: {n, p_up1, r1, p_up3, r3}}"""
    key = "fwd_stats@" + (u.dates[-1] if u.dates else "na")
    if use_cache:
        c = store.meta_get(con, key)
        if c:
            return c
    agg = {}
    for code, bs in u.bars.items():
        n = len(bs)
        if n < 66:
            continue
        closes = [b["c"] or 0 for b in bs]
        highs = [b["h"] or 0 for b in bs]
        vols = [b["v"] or 0 for b in bs]
        s5 = s10 = s20 = 0.0
        sv5 = 0.0
        mx = None      # (value, index) 60日滚动最高
        for i in range(n):
            s5 += closes[i]
            s10 += closes[i]
            s20 += closes[i]
            sv5 += vols[i]
            if i >= 5:
                s5 -= closes[i - 5]
                sv5 -= vols[i - 5]
            if i >= 10:
                s10 -= closes[i - 10]
            if i >= 20:
                s20 -= closes[i - 20]
            # 滚动 60 日最高（摊还 O(1)）
            if mx is None or highs[i] >= mx[0]:
                mx = (highs[i], i)
            elif mx[1] < i - 59:
                lo = max(0, i - 59)
                j = max(range(lo, i + 1), key=lambda k: highs[k])
                mx = (highs[j], j)
            if i < 60 or i + 3 >= n:
                continue
            price = closes[i]
            if price <= 0:
                continue
            ma5 = s5 / 5.0
            ma10 = s10 / 10.0
            ma20 = s20 / 20.0
            v5 = (sv5 - vols[i]) / 4.0 if i >= 4 else 0
            vr = (vols[i] / v5) if v5 > 0 else 1.0
            dd60 = (price / mx[0] - 1) * 100 if mx[0] else 0
            b = _bucket(_trend_tag(ma5, ma10, ma20, price), _pos_tag(dd60), _vol_tag(vr))
            r1 = closes[i + 1] / price - 1
            r3 = closes[i + 3] / price - 1
            a = agg.setdefault(b, [0, 0, 0.0, 0, 0.0])
            a[0] += 1
            if r1 > 0:
                a[1] += 1
            a[2] += r1
            if r3 > 0:
                a[3] += 1
            a[4] += r3
    out = {}
    for b, (n, u1, sr1, u3, sr3) in agg.items():
        if n < 100:
            continue
        out[b] = {"n": n, "p_up1": round(u1 / n, 3), "r1": round(sr1 / n * 100, 2),
                  "p_up3": round(u3 / n, 3), "r3": round(sr3 / n * 100, 2)}
    store.meta_set(con, key, out)
    con.commit()
    return out


# ============================================================== 单只体检
def diagnose(u, date, pos, fwd, code2boards=None):
    code = pos["code"]
    bs = [b for b in u.bars.get(code, []) if b["d"] <= date]
    st = u.stocks.get(code, {})
    name = pos.get("name") or st.get("name") or code
    if len(bs) < 25:
        return {"code": code, "name": name, "ok": False,
                "msg": "本地日K不足（可能新股/停牌），暂无法体检"}

    cur = bs[-1]
    price = cur["c"]
    closes = [b["c"] for b in bs]
    ma5 = engine.mean(closes[-5:])
    ma10 = engine.mean(closes[-10:])
    ma20 = engine.mean(closes[-20:])
    ma60 = engine.mean(closes[-60:]) if len(closes) >= 60 else None
    hi60 = max(b["h"] for b in bs[-60:]) if len(bs) >= 2 else price
    lo20 = min(b["l"] for b in bs[-20:])
    dd60 = (price / hi60 - 1) * 100 if hi60 else 0
    v5 = engine.mean([b["v"] or 0 for b in bs[-6:-1]]) or 0
    vr = ((cur["v"] or 0) / v5) if v5 > 0 else 1.0
    streak = u.streak.get(code, {}).get(date, 0)
    lim = u.lim.get(code, 10.0)
    trend = _trend_tag(ma5, ma10, ma20, price)
    ptag = _pos_tag(dd60)
    vtag = _vol_tag(vr)
    bkt = _bucket(trend, ptag, vtag)
    f = (fwd or {}).get(bkt) or {}

    cost = pos.get("cost")
    shares = pos.get("shares")
    pnl_pct = ((price / cost - 1) * 100) if cost else None
    pnl_amt = ((price - cost) * shares) if (cost and shares) else None
    mv = (price * shares) if shares else None

    # ---- 风险信号
    risks = []
    if ma5 and price < ma5:
        risks.append("跌破5日线")
    if ma10 and price < ma10:
        risks.append("跌破10日线")
    if ma20 and price < ma20:
        risks.append("跌破20日线（趋势转弱）")
    if trend == "空头":
        risks.append("均线空头排列")
    if vr >= 2.5 and (cur["pct"] or 0) < 2:
        risks.append("天量滞涨（量比%.1f）" % vr)
    if engine.is_zhaban(cur, lim):
        risks.append("今日炸板（触板未封）")
    if dd60 <= -20 and trend != "多头":
        risks.append("距60日高%.0f%%（深度回撤）" % dd60)
    if vtag == "缩量" and trend == "多头" and (cur["pct"] or 0) < 0:
        risks.append("缩量阴跌（资金撤离）")
    if cost and pnl_pct is not None and pnl_pct <= -8:
        risks.append("浮亏%.1f%%（跌破风控线）" % pnl_pct)
    if len(bs) >= 3:
        d3 = sum(1 for b in bs[-3:] if (b["pct"] or 0) < 0)
        if d3 == 3:
            risks.append("三连阴")

    # ---- 亮点
    plus = []
    if trend == "多头":
        plus.append("均线多头排列")
    if streak >= 1:
        plus.append("%d连板" % streak)
    if ma5 and price >= ma5 and vtag == "缩量" and (cur["pct"] or 0) >= -2:
        plus.append("缩量回踩不破5日线（健康）")
    if vr >= 1.5 and (cur["pct"] or 0) >= 3:
        plus.append("放量上攻（量比%.1f）" % vr)
    if dd60 > -5 and trend == "多头":
        plus.append("创新高附近强势")

    # ---- 目标位 / 止损位
    target = None
    if trend == "多头":
        # 前高上方量度目标：以 20 日振幅测算
        amp = (hi60 - lo20) or (price * 0.15)
        target = round(max(hi60, price) + amp * 0.382, 2)
    else:
        target = round(hi60, 2) if hi60 else None
    stops = [x for x in [ma10, ma20] if x and x < price]
    stop = round(max(stops), 2) if stops else (round(price * 0.92, 2))
    if cost:
        stop = max(stop, round(cost * 0.92, 2)) if price > cost else stop
    stop_pct = round((stop / price - 1) * 100, 1) if price else None
    tgt_pct = round((target / price - 1) * 100, 1) if (target and price) else None

    # ---- 评级 + 动作
    nrisk = len(risks)
    hard = any(("跌破20日线" in r) or ("空头排列" in r) or ("风控线" in r) for r in risks)
    if hard and trend != "多头":
        grade, action = "D", "止损离场"
        why = "趋势已破坏" + ("且浮亏超风控线" if (pnl_pct is not None and pnl_pct <= -8) else "")
    elif pnl_pct is not None and pnl_pct >= 30 and nrisk >= 2:
        grade, action = "C", "止盈减仓"
        why = "浮盈%.0f%%叠加%d项风险信号，落袋为安" % (pnl_pct, nrisk)
    elif nrisk >= 3:
        grade, action = "C", "减仓观察"
        why = "%d项风险信号共振" % nrisk
    elif nrisk >= 1 and trend != "多头":
        grade, action = "C", "减仓观察"
        why = "结构走弱且有风险信号"
    elif trend == "多头" and nrisk == 0:
        grade, action = "A", "继续持有"
        why = "多头结构完好，无风险信号"
    else:
        grade, action = "B", "持有观察"
        why = "结构尚可，留意%s" % ("、".join(risks[:2]) if risks else "量能变化")

    d = {
        "code": code, "name": name, "ok": True, "watch": pos.get("watch"),
        "cost": cost, "shares": shares, "entry": pos.get("date"), "note": pos.get("note"),
        "price": round(price, 2), "pct": round(cur["pct"] or 0, 2),
        "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        "pnl_amt": round(pnl_amt, 0) if pnl_amt is not None else None,
        "mv": round(mv, 0) if mv is not None else None,
        "ma5": round(ma5, 2) if ma5 else None,
        "ma10": round(ma10, 2) if ma10 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "hi60": round(hi60, 2), "dd60": round(dd60, 1),
        "vol_ratio": round(vr, 2), "streak": streak,
        "trend": trend, "pos_tag": ptag, "vol_tag": vtag, "bucket": bkt,
        "p_up1": f.get("p_up1"), "r1": f.get("r1"),
        "p_up3": f.get("p_up3"), "r3": f.get("r3"), "fwd_n": f.get("n"),
        "target": target, "target_pct": tgt_pct, "stop": stop, "stop_pct": stop_pct,
        "risks": risks, "plus": plus,
        "grade": grade, "action": action, "why": why,
    }

    # ---- 波段操作建议（回踩买/反弹卖/止损）：持仓给卖出建议，关注给买卖价 ----
    try:
        import zones as _zmod
        _bd = _zmod.band_levels(bs, cost=cost)
        if _bd:
            d["buy_zone"] = _bd["buy_zone"]
            d["sell_zone"] = _bd["sell_zone"]
            d["stop_band"] = _bd["stop"]
            d["band_action"] = _bd["band_action"]
            d["sell_advice"] = _bd["advice"]
    except Exception:
        pass

    return d


# ============================================================== 持续跟踪
def _track(con, date, rows):
    con.executescript(TRACK_SCHEMA)
    import time
    prev = {}
    r = con.execute("SELECT MAX(date) FROM holdings_track WHERE date<?", (date,)).fetchone()
    pd = r[0] if r and r[0] else None
    if pd:
        for c, g, a, p in con.execute(
                "SELECT code,grade,action,pnl_pct FROM holdings_track WHERE date=?", (pd,)):
            prev[c] = {"grade": g, "action": a, "pnl_pct": p}
    order = {"A": 3, "B": 2, "C": 1, "D": 0}
    alerts = []
    for d in rows:
        if not d.get("ok"):
            continue
        p = prev.get(d["code"])
        if p:
            d["prev_grade"] = p["grade"]
            og, ng = order.get(p["grade"], 2), order.get(d["grade"], 2)
            if ng < og:
                d["changed"] = "降级"
                alerts.append("%s %s 评级 %s→%s（%s）"
                              % (d["code"], d["name"], p["grade"], d["grade"], d["action"]))
            elif ng > og:
                d["changed"] = "升级"
            if p.get("pnl_pct") is not None and d.get("pnl_pct") is not None:
                d["pnl_chg"] = round(d["pnl_pct"] - p["pnl_pct"], 2)
        con.execute(
            "INSERT OR REPLACE INTO holdings_track"
            "(date,code,name,price,pnl_pct,grade,action,risks,p_up1,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (date, d["code"], d["name"], d["price"], d.get("pnl_pct"), d["grade"],
             d["action"], "；".join(d.get("risks") or []), d.get("p_up1"),
             time.strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()
    return alerts, pd


def history(con, code=None, limit=60):
    con.executescript(TRACK_SCHEMA)
    q = "SELECT date,code,name,price,pnl_pct,grade,action FROM holdings_track"
    args = []
    if code:
        q += " WHERE code=?"
        args.append(code)
    q += " ORDER BY date DESC LIMIT ?"
    args.append(limit)
    return [{"date": r[0], "code": r[1], "name": r[2], "price": r[3],
             "pnl_pct": r[4], "grade": r[5], "action": r[6]}
            for r in con.execute(q, args)]


# ============================================================== 主入口
def monitor(u, date, con, positions=None, code2boards=None, persist=True):
    """持仓体检主入口。

    positions: 外部传入的持仓列表（用户级个性化推送用）；为 None 时回退到
                load_positions()（共享 config/holdings.json 或 HOLDINGS_JSON）。
    persist:   True 时把当日快照写入 holdings_track（共享默认持仓用，用于跨日预警）；
               False 时仅计算展示（用户级个性化推送用，避免不同用户的持仓互相覆盖 track 表）。
    """
    if positions is None:
        pos = load_positions()
    else:
        pos = [p for p in (_norm_pos(it) for it in positions) if p]
    if not pos:
        return {"date": date, "enabled": False, "n": 0, "items": [],
                "msg": "未配置持仓（config/holdings.json 为空或不存在）"}
    fwd = forward_stats(u, con)
    rows = [diagnose(u, date, p, fwd, code2boards) for p in pos]
    if persist:
        alerts, prev_date = _track(con, date, rows)
    else:
        alerts, prev_date = [], None
    good = [r for r in rows if r.get("ok")]
    held = [r for r in good if not r.get("watch")]
    tot_mv = sum(r["mv"] for r in held if r.get("mv")) or None
    tot_pnl = sum(r["pnl_amt"] for r in held if r.get("pnl_amt") is not None) or None
    wtd = None
    if tot_mv and held:
        num = sum((r["pnl_pct"] or 0) * (r["mv"] or 0) for r in held if r.get("mv"))
        wtd = round(num / tot_mv, 2)
    order = {"D": 0, "C": 1, "B": 2, "A": 3}
    rows.sort(key=lambda x: (order.get(x.get("grade"), 9), -(x.get("pnl_pct") or 0)))
    return {
        "date": date, "enabled": True, "n": len(rows),
        "n_held": len(held), "n_watch": len(good) - len(held),
        "total_mv": tot_mv, "total_pnl": tot_pnl, "pnl_pct_weighted": wtd,
        "alerts": alerts, "prev_date": prev_date,
        "need_action": [r for r in rows if r.get("grade") in ("C", "D")],
        "items": rows,
        "fwd_buckets": len(fwd or {}),
    }


def summary_lines(rep, limit=8):
    """推送用紧凑摘要"""
    if not rep or not rep.get("enabled"):
        return []
    out = []
    head = "持仓 %d 只" % rep["n_held"]
    if rep.get("n_watch"):
        head += "（+关注 %d）" % rep["n_watch"]
    if rep.get("pnl_pct_weighted") is not None:
        head += "，加权浮动 %+.2f%%" % rep["pnl_pct_weighted"]
    if rep.get("total_pnl") is not None:
        head += "，浮动盈亏 %+.0f 元" % rep["total_pnl"]
    out.append(head)
    for d in rep["items"][:limit]:
        if not d.get("ok"):
            out.append("· %s %s：%s" % (d["code"], d["name"], d.get("msg", "无数据")))
            continue
        seg = "· %s %s %.2f(%+.2f%%)" % (d["code"], d["name"], d["price"], d["pct"])
        if d.get("pnl_pct") is not None:
            seg += " 浮盈%+.1f%%" % d["pnl_pct"]
        seg += " ｜%s级·%s" % (d["grade"], d["action"])
        if d.get("p_up1") is not None:
            seg += " ｜次日上涨%.0f%%(均%+.2f%%)" % (d["p_up1"] * 100, d["r1"])
        out.append(seg)
        detail = []
        if d.get("risks"):
            detail.append("风险：" + "、".join(d["risks"][:3]))
        elif d.get("plus"):
            detail.append("亮点：" + "、".join(d["plus"][:2]))
        if d.get("stop"):
            detail.append("止损%.2f(%+.1f%%)" % (d["stop"], d["stop_pct"] or 0))
        if d.get("target"):
            detail.append("目标%.2f(%+.1f%%)" % (d["target"], d["target_pct"] or 0))
        if d.get("buy_zone") and d.get("sell_zone"):
            detail.append("波段 买%s~%s/卖%s~%s" % (d["buy_zone"][0], d["buy_zone"][1],
                                                   d["sell_zone"][0], d["sell_zone"][1]))
        if d.get("sell_advice"):
            detail.append("操作：%s" % d["sell_advice"])
        if detail:
            out.append("   " + "；".join(detail))
    if rep.get("alerts"):
        out.append("⚠ 评级变化：" + "；".join(rep["alerts"][:4]))
    return out
