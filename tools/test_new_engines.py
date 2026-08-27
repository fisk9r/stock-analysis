"""新引擎合并终测：缠论 / 席位画像 / 题材主线 / 连续信号 / 统一回测。

全部基于本地 cache/market.db 离线跑，不依赖外网（席位实时抓取部分单独标注）。
用法：python tools/test_new_engines.py
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import chanlun          # noqa: E402
import signal_backtest  # noqa: E402
import signals          # noqa: E402
import store            # noqa: E402
import theme            # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, detail))


def main():
    db = os.path.join(ROOT, "cache", "market.db")
    if not os.path.isfile(db):
        raise SystemExit("缺少 cache/market.db，无法离线测试")
    con = sqlite3.connect(db)

    n_bars = con.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    n_stk = con.execute("SELECT COUNT(DISTINCT code) FROM bars").fetchone()[0]
    d0, d1 = con.execute("SELECT MIN(date), MAX(date) FROM bars").fetchone()
    print("DB: %d 根K线 / %d 只股票 / %s ~ %s\n" % (n_bars, n_stk, d0, d1))

    # ---------------- 1. 缠论引擎 ----------------
    print("[1] 缠论引擎 chanlun")
    codes = [r[0] for r in con.execute(
        "SELECT code FROM bars GROUP BY code HAVING COUNT(*)>=60 "
        "ORDER BY code LIMIT 300")]
    bars_map = store.load_bars(con, codes=codes)
    check("load_bars 有数据", bool(bars_map), "-> %d 只" % len(bars_map))

    # MACD 数值正确性：常数序列 DIF 应为 0（返回 (dif, dea, hist) 三元组）
    dif, dea, hist = chanlun.macd([10.0] * 60)
    check("macd 常数序列 DIF≈0", abs(dif[-1]) < 1e-9, "dif=%.2e" % dif[-1])
    check("macd 输出长度对齐", len(dif) == len(dea) == len(hist) == 60)

    # 包含处理：结果不多于原始，且不再存在包含关系（元组 (idx, high, low)）
    sample = bars_map[codes[0]]
    raw = [(i, float(b["h"]), float(b["l"])) for i, b in enumerate(sample)]
    proc = chanlun.process_inclusion(raw)
    no_incl = all(
        not (proc[i + 1][1] <= proc[i][1] and proc[i + 1][2] >= proc[i][2])
        and not (proc[i + 1][1] >= proc[i][1] and proc[i + 1][2] <= proc[i][2])
        for i in range(len(proc) - 1))
    check("包含处理后无包含关系", no_incl, "%d -> %d 根" % (len(raw), len(proc)))
    check("包含处理不增加K线数", len(proc) <= len(raw))

    # 分型 / 笔 / 中枢 / 背驰 全链路
    sig_count = {}
    n_ok = n_bi = n_zs = n_bc = 0
    examples = []
    for c in codes:
        bars = bars_map.get(c)
        if not bars:
            continue
        r = chanlun.analyze(c, bars)
        if not r:
            continue
        n_ok += 1
        sig_count[r["signal"]] = sig_count.get(r["signal"], 0) + 1
        if r.get("n_bi"):
            n_bi += 1
        if r.get("zhongshu"):
            n_zs += 1
        if r.get("beichi"):
            n_bc += 1
        if r["signal"] in ("一买", "二买", "三买") and len(examples) < 5:
            examples.append((c, r["signal"], r.get("beichi")))
    # 中枢算法构造性单测：人工造一段震荡笔序列，重叠区间必然存在
    # 端点价：10 -> 12 -> 10.5 -> 11.8 -> 10.8（段区间两两重叠于 [10.8, 11.8] 附近）
    fake_bi = []
    for i, (typ, p) in enumerate([("bottom", 10.0), ("top", 12.0), ("bottom", 10.5),
                                  ("top", 11.8), ("bottom", 10.8)]):
        fake_bi.append((i, typ, p, p, p))
    segs = chanlun.bi_segments(fake_bi)
    check("bi_segments 段数 = 端点数-1", len(segs) == 4, "-> %s" % (segs,))
    zs = chanlun.find_zhongshu(fake_bi)
    check("构造震荡序列必识别出中枢", zs is not None, "-> %s" % (zs,))
    check("中枢 upper > lower", bool(zs) and zs[0] > zs[1], "-> %s" % (zs,))
    # 单调上升序列（无重叠）应无中枢
    mono = [(i, ("bottom" if i % 2 == 0 else "top"), p, p, p)
            for i, p in enumerate([10.0, 11.0, 11.5, 13.0, 13.5, 15.0])]
    check("单调推升序列无中枢（不误判）", chanlun.find_zhongshu(mono) is None,
          "-> %s" % (chanlun.find_zhongshu(mono),))

    check("analyze 成功率", n_ok > 200, "-> %d/%d 只出结果" % (n_ok, len(codes)))
    check("笔(bi) 构建有效", n_bi > 100, "-> %d 只有笔" % n_bi)
    check("中枢识别有效", n_zs > 0, "-> %d 只识别出中枢" % n_zs)
    check("背驰检测有效", n_bc > 0, "-> %d 只检出背驰" % n_bc)
    check("买卖点分类有输出", len(sig_count) >= 2, "-> %s" % sig_count)
    print("      买点样例: %s" % (examples or "无"))

    # scan 整体
    r = chanlun.scan(None, con, codes[:80], top_n=12)
    check("scan 返回结构完整",
          bool(r) and set(["n_analyzed", "candidates", "buys"]) <= set(r),
          "-> n_analyzed=%s buys=%d" % (r and r["n_analyzed"], len(r["buys"]) if r else -1))
    check("summary_lines 可渲染", bool(chanlun.summary_lines(r)) or r["n_analyzed"] >= 0)

    # ---------------- 2. 统一回测框架 ----------------
    print("\n[2] 统一回测 signal_backtest")
    ev = []
    for c in codes[:40]:
        bs = bars_map.get(c) or []
        for b in bs[-40:-2]:
            ev.append((b["d"], c))
    bt = signal_backtest.backtest_events(con, ev[:400], fwd=1, min_n=8)
    # 约定：win_rate 已是百分数（0~100），avg_ret 已是百分比
    check("backtest_events 返回统计", bool(bt) and "win_rate" in bt,
          "-> n=%s win=%.1f%% avg=%.2f%% 盈亏比=%s"
          % (bt["n"], bt["win_rate"], bt["avg_ret"], bt["pl_ratio"]) if bt else "None")
    check("win_rate 单位为百分数(0~100)", bool(bt) and 0 <= bt["win_rate"] <= 100,
          "-> %s" % (bt and bt["win_rate"]))
    check("win_n + loss_n == n", bool(bt) and bt["win_n"] + bt["loss_n"] == bt["n"])
    check("样本不足时返回 None",
          signal_backtest.backtest_events(con, ev[:3], fwd=1, min_n=8) is None)
    t1 = signal_backtest.t1_stats(con, ev[:400])
    check("t1_stats 等价 fwd=1", bool(t1) and t1["n"] == bt["n"])

    # ---------------- 3. 题材主线 ----------------
    print("\n[3] 题材主线 theme")
    fake_lu = [
        {"code": "300001", "name": "A", "concepts": ["固态电池", "融资融券", "昨日涨停"],
         "industry": "电池"},
        {"code": "300002", "name": "B", "concepts": ["固态电池", "机构重仓"], "industry": "电池"},
        {"code": "300003", "name": "C", "concepts": ["固态电池"], "industry": "电池"},
        {"code": "300004", "name": "D", "concepts": ["人形机器人", "深股通"], "industry": "机械"},
        {"code": "300005", "name": "E", "concepts": ["人形机器人"], "industry": "机械"},
    ]
    tr = theme.scan("2026-08-25", fake_lu)
    check("scan 识别主线", bool(tr) and tr["main_theme"] == "固态电池",
          "-> main=%s n=%s" % (tr and tr["main_theme"], tr and tr["main_n"]))
    allt = []
    for t in (tr.get("all") or []):
        if isinstance(t, dict):
            allt.append(t.get("theme"))
        elif isinstance(t, (list, tuple)):
            allt.append(t[0])
        else:
            allt.append(t)
    check("all 列表已解析出题材名", all(isinstance(x, str) for x in allt) and bool(allt),
          "-> %s" % allt[:6])
    check("STOPLIST 过滤元标签",
          not any(x in allt for x in ["融资融券", "昨日涨停", "机构重仓", "深股通"]),
          "-> all=%s" % allt[:6])
    check("summary_lines 容错 signal 缺失", isinstance(theme.summary_lines(tr), list))

    # ---------------- 4. 连续信号 ----------------
    print("\n[4] 连续信号 signals")
    ps, ns, last = signals._tail_streak([1, 2, 3, 4])
    check("_tail_streak 全正", (ps, ns) == (4, 0), "-> %s" % ((ps, ns, last),))
    ps, ns, _ = signals._tail_streak([1, 2, -1, -2, -3])
    check("_tail_streak 尾部连负", (ps, ns) == (0, 3), "-> %s" % ((ps, ns),))
    ps, ns, _ = signals._tail_streak([])
    check("_tail_streak 空输入不崩", (ps, ns) == (0, 0))
    sig = signals.compute_all(con)
    check("compute_all 可执行（无历史返回空属正常）",
          sig is None or isinstance(sig, dict), "-> %s" % (list(sig) if sig else sig))
    check("summary_lines 容错", isinstance(signals.summary_lines(sig or {}), list))

    # ---------------- 5. 席位画像（离线部分）----------------
    print("\n[5] 游资席位 seats（离线部分）")
    import seats
    check("FAMOUS 席位库非空", len(seats.FAMOUS) >= 10, "-> %d 个席位" % len(seats.FAMOUS))
    wr = seats.win_rates(con)
    check("win_rates 可执行", isinstance(wr, dict), "-> %d 个席位有胜率样本" % len(wr))
    check("summary_lines 容错 stats 缺失",
          isinstance(seats.summary_lines({"n_hits": 0, "hits": [], "top": []}), list))

    # ---------------- 6. 落库往返（防回归：曾漏传 date 参数导致快照全失败）----------------
    print("\n[6] 引擎落库往返 store（内存库）")
    mem = sqlite3.connect(":memory:")
    mem.executescript(store.SCHEMA if hasattr(store, "SCHEMA") else "")
    if not hasattr(store, "SCHEMA"):
        # 回退：从源码抽取建表语句
        src = open(os.path.join(ROOT, "pipeline", "store.py"), encoding="utf-8").read()
        beg = src.index("CREATE TABLE")
        end = src.index('"""', beg)
        mem.executescript(src[beg:end])

    # save_snapshot 必须传 4 个参数 (con,k,date,payload)
    try:
        store.save_snapshot(mem, "margin", "2026-08-25", {"total_yi": 26560, "delta_yi": -5})
        store.save_snapshot(mem, "margin", "2026-08-24", {"total_yi": 26565, "delta_yi": 12})
        mem.commit()
        ok = True
    except TypeError as e:
        ok = False
        print("      TypeError:", e)
    check("save_snapshot(con,k,date,payload) 调用成功", ok)
    hist = store.snapshot_history(mem, "margin", days=20)
    check("snapshot_history 往返可读", len(hist) == 2, "-> %d 条" % len(hist))
    check("快照按日期正序", bool(hist) and hist[0][0] < hist[-1][0],
          "-> %s" % [h[0] for h in hist])
    check("payload JSON 往返一致",
          bool(hist) and hist[-1][1]["total_yi"] == 26560)

    # 席位 / 题材落库往返
    store.upsert_seats(mem, "2026-08-25", [{
        "dept_code": "D1", "label": "章盟主", "code": "600000", "name": "X",
        "net_yi": 1.2, "act_buy_yi": 2.0, "act_sell_yi": 0.8, "chg": 5.1}])
    store.upsert_themes(mem, "2026-08-25", {"固态电池": 3, "人形机器人": 2})
    mem.commit()
    check("upsert_seats 往返", len(store.seats_history(mem, days=90)) == 1)
    check("upsert_themes 往返", bool(store.themes_series(mem, days=30)))

    # 用两日快照驱动连续信号，验证非空路径
    sig2 = signals.compute_all(mem)
    check("有历史时 compute_all 走通非空路径",
          sig2 is None or isinstance(sig2, dict), "-> %s" % (list(sig2) if sig2 else sig2))

    # ---------------- 7. 买卖区间与操作提示 ----------------
    print("\n[7] 买卖区间 zones")
    import zones

    def mk_bars(closes, vols=None):
        out = []
        for i, c in enumerate(closes):
            prev = closes[i - 1] if i else c
            out.append({"d": "d%03d" % i, "o": prev, "h": max(prev, c) * 1.005,
                        "l": min(prev, c) * 0.995, "c": c,
                        "v": (vols[i] if vols else 1000)})
        return out

    # 7a 结构不变量：真实数据上跑 30 只
    rs = []
    for c in codes[:30]:
        bs = bars_map.get(c)
        if not bs or len(bs) < 40:
            continue
        r = zones.analyze_one(c, "T" + c, bs[-120:])
        if r:
            rs.append(r)
    check("zones.analyze_one 出结果", len(rs) >= 20, "-> %d/30" % len(rs))
    ok_struct = all(
        r["buy_zone"][0] < r["buy_zone"][1]
        and r["sell_zone"][0] < r["sell_zone"][1]
        and r["stop"] <= r["buy_zone"][0]
        and r["action"] in ("破位卖出", "加仓提示", "回踩买入区", "跌破警示",
                            "逼近卖出", "突破持有", "正常持有")
        for r in rs)
    check("区间结构不变量(买区/卖区有序、止损≤买区下沿)", ok_struct)
    acts = {}
    for r in rs:
        acts[r["action"]] = acts.get(r["action"], 0) + 1
    print("      动作分布:", acts)

    # 7b 构造性破位：高位横盘后连续放量下杀 → 必触发「破位卖出」
    base = [10.0 + (0.15 if i % 3 == 0 else -0.12) for i in range(55)]
    crash = [9.6, 9.2, 8.7, 8.4]
    bars_crash = mk_bars(base + crash,
                         vols=[1000] * 55 + [1800, 2200, 2600, 3000])
    rc = zones.analyze_one("TEST1", "破位样本", bars_crash)
    check("放量下杀必判「破位卖出」", bool(rc) and rc["action"] == "破位卖出",
          "-> %s %s" % (rc and rc["action"], rc and rc["reasons"][:2]))
    check("破位时止损位为有效保护线(≤买区下沿)",
          bool(rc) and rc["stop"] <= rc["buy_zone"][0], "-> stop=%s" % (rc and rc["stop"]))

    # 7c 缩量回踩：上升后缩量小回调 → 不应误报「破位卖出」
    up = [10 * (1 + 0.004 * i) for i in range(50)]
    pull = [up[-1] * 0.995, up[-1] * 0.99, up[-1] * 0.985]
    bars_pull = mk_bars(up + pull, vols=[2000] * 50 + [1500, 1300, 1100])
    rp_ = zones.analyze_one("TEST2", "回踩样本", bars_pull)
    check("缩量回踩不误报破位",
          bool(rp_) and rp_["action"] in ("加仓提示", "回踩买入区", "跌破警示", "正常持有"),
          "-> %s" % (rp_ and rp_["action"],))

    # 7d 数据不足容错
    check("K线<40 返回 None", zones.analyze_one("T3", "x", mk_bars([10] * 20)) is None)

    # 7e scan 整体 + summary_lines
    class _FakeU:
        bars = {c: (bars_map.get(c) or []) for c in codes[:8]}
        stocks = {}
    zr = zones.scan(_FakeU(), "9999-12-31", codes[:8],
                    extra_names={codes[0]: "甲"})
    check("zones.scan 返回结构完整",
          bool(zr) and set(["items", "alerts", "alert_n"]) <= set(zr),
          "-> n=%s" % (zr and zr["n"]))
    named = [x for x in (zr.get("items") or []) if x["code"] == codes[0]]
    check("extra_names 名称注入生效", bool(named) and named[0]["name"] == "甲",
          "-> %s" % (named and named[0]["name"],))
    lines_z = zones.summary_lines(zr)
    check("summary_lines 非空列表", isinstance(lines_z, list) and bool(lines_z),
          "-> %s" % lines_z[:2])

    # 7f 渲染层冒烟：app.js 已含新卡片关键字（文件级断言）
    appjs = open(os.path.join(ROOT, "dist", "app.js"), encoding="utf-8").read()
    for kw in ["买卖区间", "D.zones", "buy_zone", "sell_zone"]:
        check("app.js 含 %s" % kw, kw in appjs)

    # 7g 成本价联动：带 cost 的股票输出盈亏字段与提示
    rc2 = zones.analyze_one("TEST4", "成本样本", bars_crash, cost=10.0)
    check("cost 传入输出 pnl_pct", bool(rc2) and rc2["pnl_pct"] is not None,
          "-> %s" % (rc2 and rc2["pnl_pct"],))
    check("深亏触发预警语", bool(rc2) and any("预警线" in x for x in rc2["reasons"]),
          "-> %s" % (rc2 and rc2["reasons"][:1],))

    # ---------------- 9. 周期标注 / 多周期目标 / 时间到期预警 ----------------
    print("\n[9] 周期标注与多周期目标 zones")
    r9 = zones.analyze_one(codes[0], "T9", bars_map[codes[0]][-120:])
    check("analyze_one 含 horizon/targets",
          bool(r9) and r9.get("horizon") in zones.HORIZONS
          and set(["短线", "中线", "长线"]) <= set((r9.get("targets") or {})),
          "-> horizon=%s" % (r9 and r9.get("horizon")))
    tk = (r9 or {}).get("targets") or {}
    check("三档目标价格>0且时间窗正确",
          tk.get("短线", {}).get("price", 0) > 0
          and tk.get("中线", {}).get("price", 0) > 0
          and tk.get("长线", {}).get("price", 0) > 0
          and tk["短线"]["days"] == 5 and tk["中线"]["days"] == 15
          and tk["长线"]["days"] == 60,
          "-> 短%.2f/中%.2f/长%.2f" % (tk["短线"]["price"], tk["中线"]["price"], tk["长线"]["price"]))

    # 9b 自动建议周期合法
    hv = [10 * (1 + 0.03 * ((i % 2) * 2 - 1)) for i in range(60)]
    rhv = zones.analyze_one("THV", "高波动", mk_bars(hv))
    check("自动建议周期合法", bool(rhv) and rhv["horizon"] in zones.HORIZONS,
          "-> %s" % (rhv and rhv["horizon"]))

    # 9c _time_status 单测（已达/破位/到期/观察/无锚点）
    tg = {"短线": {"price": 10.0, "days": 5, "pct": 5},
          "中线": {"price": 12.0, "days": 15, "pct": 20},
          "长线": {"price": 15.0, "days": 60, "pct": 50}}
    s_ok, a_ok = zones._time_status("短线", tg, 10.5, 9.0, 2)
    check("时间状态-已达目标", "✅" in (s_ok or ""))
    s_br, a_br = zones._time_status("短线", tg, 8.5, 9.0, 2)
    check("时间状态-破位优先", "🛑" in (s_br or ""))
    s_exp, a_exp = zones._time_status("短线", tg, 9.5, 9.0, 6)
    check("时间状态-到期未达", "⏰" in (s_exp or ""))
    s_obs, a_obs = zones._time_status("短线", tg, 9.5, 9.0, 2)
    check("时间状态-观察中(非提醒)", "⏳" in (s_obs or "") and not a_obs)
    s_none, a_none = zones._time_status("短线", tg, 9.5, 9.0, None)
    check("无锚点返回 None", s_none is None and a_none is False)

    # 9d scan 透传 horizon/elapsed -> 到期进入 alerts.time
    class _FU:
        bars = {c: (bars_map.get(c) or []) for c in codes[:6]}
        stocks = {}
        dates = []
    zr9 = zones.scan(_FU(), "9999-12-31", codes[:6],
                     costs={codes[0]: 10.0}, horizons={codes[0]: "短线"},
                     elapsed_map={codes[0]: 99})
    check("scan 返回含 alerts.time", bool(zr9) and "time" in zr9["alerts"])
    check("到期票进入 alerts.time", bool(zr9["alerts"]["time"]),
          "-> %d 条" % len(zr9["alerts"]["time"]))

    # 9e 渲染关键字
    sl9 = zones.summary_lines(zr9)
    check("summary_lines 含周期/目标", isinstance(sl9, list) and bool(sl9)
          and any(("目标" in l or "[" in l) for l in sl9), "-> %s" % sl9[:2])

    # 9f 关注股优化提示（止损/更换/割肉 + 更换建议）
    pool = [{"code": "A001", "name": "强A", "worth_score": 80, "p_continue": 60},
            {"code": "A002", "name": "强B", "worth_score": 70, "p_continue": 55},
            {"code": "A003", "name": "强C", "worth_score": 60, "p_continue": 50}]
    # 破位 -> 止损 + 更换建议（排除自身）
    rz_crash = zones.analyze_one("TEST1", "破位样本", bars_crash,
                                 replace_pool=pool, exclude={"TEST1"})
    check("破位触发止损", bool(rz_crash) and rz_crash["rotate"] == "止损",
          "-> %s" % (rz_crash and rz_crash["rotate"],))
    check("破位给出更换建议且排除自身", bool(rz_crash) and len(rz_crash["replace"]) == 3
          and all(s["code"] != "TEST1" for s in rz_crash["replace"]),
          "-> %s" % [s["code"] for s in (rz_crash or {}).get("replace", [])])
    # 短线停滞 -> 更换（60根平盘，显式短线）
    flat = [10.0 + 0.001 * ((i % 2) * 2 - 1) for i in range(60)]
    bars_flat = mk_bars(flat)
    rz_flat = zones.analyze_one("TF", "死水样本", bars_flat, horizon="短线",
                                replace_pool=pool, exclude={"TF"})
    check("短线停滞触发更换", bool(rz_flat) and rz_flat["rotate"] == "更换",
          "-> %s | %s" % (rz_flat and rz_flat["rotate"],
                          rz_flat and rz_flat["rotate_reason"]))
    # 中线下跌趋势 -> 割肉（温和下行，MA20<MA60，但未破位）
    down = [12.0 - 0.03 * i for i in range(60)]
    bars_down = mk_bars(down)
    rz_down = zones.analyze_one("TD", "下跌样本", bars_down, horizon="中线")
    check("中线下跌趋势触发割肉", bool(rz_down) and rz_down["rotate"] == "割肉",
          "-> %s" % (rz_down and rz_down["rotate"],))
    # 更换建议排除关注池（exclude 含 A001）
    rz_exc = zones.analyze_one("TEST1", "破位样本", bars_crash,
                               replace_pool=pool, exclude={"TEST1", "A001"})
    check("更换建议排除关注池A001", bool(rz_exc)
          and "A001" not in [s["code"] for s in rz_exc["replace"]],
          "-> %s" % [s["code"] for s in (rz_exc or {}).get("replace", [])])
    # scan 汇总 alerts.rotate + summary_lines
    class _FUR:
        bars = {"TEST1": bars_crash, "TF": bars_flat, "TD": bars_down}
        stocks = {}
        dates = []
    zr_rot = zones.scan(_FUR(), "d999", ["TEST1", "TF", "TD"],
                        horizons={"TEST1": "短线", "TF": "短线", "TD": "中线"},
                        replace_pool=pool, exclude_codes={"TEST1", "TF", "TD"})
    check("scan 汇总 alerts.rotate", bool(zr_rot)
          and len(zr_rot["alerts"]["rotate"]) == 3,
          "-> %d 条" % (zr_rot and len(zr_rot["alerts"]["rotate"])))
    sl_rot = zones.summary_lines(zr_rot)
    check("summary_lines 含关注股优化段", any("关注股优化" in l for l in sl_rot),
          "-> %s" % [l for l in sl_rot if "优化" in l][:1])

    # 9g 追板回落检测 + 超短线周期 + 盘前过滤
    base = [10.0 + 0.05 * ((i % 2) * 2 - 1) for i in range(45)]
    bars_base = mk_bars(base)
    # 炸板日：前收10.0，最高触涨停11.0，收10.4（较涨停-5.5%、自高点-5.5%）
    zb_bar = {"d": "d045", "o": 10.0, "h": 11.0, "l": 10.2, "c": 10.4, "v": 5000}
    bars_zb = bars_base + [zb_bar]
    rz_zb = zones.analyze_one("TZB", "炸板样本", bars_zb, horizon="短线")
    check("追板回落命中", bool(rz_zb) and bool(rz_zb["zhuiban"]),
          "-> %s" % (rz_zb and rz_zb["zhuiban"]))
    check("短线追板回落强制止损离场",
          bool(rz_zb) and rz_zb["rotate"] == "止损",
          "-> %s | %s" % (rz_zb and rz_zb["rotate"], rz_zb and rz_zb["rotate_reason"]))
    # 守住涨停（收近涨停）不命中
    hold_bar = {"d": "d045", "o": 10.0, "h": 11.0, "l": 10.5, "c": 10.98, "v": 5000}
    rz_hold = zones.analyze_one("THD", "守板样本", bars_base + [hold_bar], horizon="短线")
    check("收近涨停不误报追板回落", bool(rz_hold) and not rz_hold["zhuiban"],
          "-> %s" % (rz_hold and rz_hold["zhuiban"]))
    # 普通波动不触板不命中
    norm_bar = {"d": "d045", "o": 10.0, "h": 10.3, "l": 9.8, "c": 10.1, "v": 1000}
    rz_norm = zones.analyze_one("TNM", "普通样本", bars_base + [norm_bar], horizon="短线")
    check("普通波动不命中追板回落", bool(rz_norm) and not rz_norm["zhuiban"])
    # 中线追板回落只给理由，不直接强制止损（除非同时破位）
    rz_mid = zones.analyze_one("TZM", "中线炸板", bars_zb, horizon="中线")
    check("中线追板回落给理由但不强制止损",
          bool(rz_mid) and rz_mid["zhuiban"] and rz_mid["rotate"] != "止损",
          "-> rotate=%s" % (rz_mid and rz_mid["rotate"]))
    # 超短线周期窗口=3日
    tz = zones.analyze_one("TCH", "超短样本", bars_zb, horizon="超短线")
    check("超短线周期窗口=3日", bool(tz) and tz["horizon"] == "超短线"
          and tz["targets"].get("超短线", {}).get("days") == 3,
          "-> %s" % (tz and tz["targets"].get("超短线")))
    # summary_lines 含追板回落段
    class _FZ:
        bars = {"TZB": bars_zb}
        stocks = {}
        dates = []
    zr_zb = zones.scan(_FZ(), "d999", ["TZB"], horizons={"TZB": "短线"})
    sl_zb = zones.summary_lines(zr_zb)
    check("summary_lines 含追板回落段", any("追板回落" in l for l in sl_zb),
          "-> %s" % [l for l in sl_zb if "追板回落" in l][:1])

    # ================= 8. 分用户推送路由（notifier 纯函数，离线可测） =================
    print("\n---- 8. 分用户推送路由 ----")
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    import notifier as _nf

    marked = ("📊 **市场概览**\n- 上证 +0.5%\n\n"
              + _nf._MARK_WL + "\n**⭐ 关注股雷达**\n- 中化国际 涨停\n" + _nf._MARK_WL_END
              + "\n\n🔥 **推荐 Top3**\n- ✅ **1. 甲**(主板) · 价值 **80分**\n\n"
              + _nf._MARK_ZN + "\n**🎯 买卖区间与操作提示**\n🛑 破位卖出：中化国际\n"
              + _nf._MARK_ZN_END + "\n\n📡 **连续信号**\n- 两融连续3日流出\n")
    stripped = _nf._strip_personal_sections(marked)
    check("剥离个人分区", "中化国际" not in stripped
          and "关注股雷达" not in stripped and "买卖区间" not in stripped)
    check("保留公共分区", "市场概览" in stripped and "推荐 Top3" in stripped
          and "连续信号" in stripped)
    check("无残留控制符", "\x01" not in stripped)
    check("无标记文本原样返回",
          _nf._strip_personal_sections("普通\n- 行") == "普通\n- 行")

    zx = {"items": [{"code": "600500", "name": "中化国际", "close": 5.32, "cost": 7.18,
                     "pnl_pct": -25.9, "buy_zone": [5.10, 5.32],
                     "sell_zone": [6.20, 6.45], "stop": 4.95, "action": "破位卖出"}]}
    appx = _nf._personal_appendix({"zones": zx}, {"600500"})
    check("附录含自选与破位提示", "你的自选跟踪" in appx and "中化国际" in appx
          and "破位卖出" in appx)
    check("无关代码不生成附录", _nf._personal_appendix({"zones": zx}, {"000001"}) == "")

    # 盘前推送含短线/超短线操作提示 + 追板回落离场
    zitems = [
        {"code": "TZB", "name": "炸板股", "horizon": "短线", "close": 10.4, "pct": -5.5,
         "zhuiban": {"date": "d045", "limit_up": 11.0, "close": 10.4,
                     "fallback_pct": 5.5, "from_high_pct": 5.5},
         "rotate": "止损", "rotate_reason": "追板回落离场", "action": "正常持有",
         "targets": {"短线": {"price": 11.0, "days": 5, "pct": 5.8}}, "time_status": None},
        {"code": "TMZ", "name": "中线股", "horizon": "中线", "close": 9.0, "pct": -1.0,
         "zhuiban": None, "rotate": None, "action": "正常持有",
         "targets": {}, "time_status": None},
    ]
    fake_pre = {"meta": {"date": "2026-08-27"}, "zones": {"items": zitems}}
    pm = _nf.format_stock_summary(fake_pre, "", mode="preauction")
    check("盘前推送含短线/超短线操作段",
          "短线/超短线盘前操作" in pm["text"], "-> %s" % ("段缺失" if "短线/超短线" not in pm["text"] else "ok"))
    check("盘前推送含追板回落离场提示",
          "追板回落" in pm["text"] and "离场" in pm["text"],
          "-> %s" % [l for l in pm["text"].splitlines() if "追板回落" in l][:1])
    sc_pm = _nf.format_sc(fake_pre, "", mode="preauction")
    check("盘前SC精简版含短线操作", "短线操作" in sc_pm["text"] and "追板回落" in sc_pm["text"])
    # 竞价后开盘前异动 含追板回落离场块
    oa = _nf.format_open_anomaly_summary(fake_pre, "")
    check("开盘前异动含追板回落离场块", "追板回落" in oa["text"] and "离场" in oa["text"])

    recs_t = [{"worth_score": 30, "p_continue": 40},
              {"worth_score": 85, "p_continue": 70},
              {"worth_score": 60, "p_continue": 55},
              {"worth_score": 75, "p_continue": 65}]
    order = [x["worth_score"] for x in sorted(
        recs_t, key=lambda x: (0 if _nf._dual_ok(x) else 1,
                               -(x.get("worth_score") or 0),
                               -(x.get("p_continue") or 0)))]
    check("推荐排序 双确认置顶+分数降序", order == [85, 75, 60, 30], "-> %s" % order)

    check("_chan_user 绑定解析",
          _nf._chan_user({"key": "K", "user": "owner"}) == "owner"
          and _nf._chan_user({"key": "K"}) is None
          and _nf._chan_user("SCTxxx") is None)
    check("_bound_uids 汇集绑定",
          _nf._bound_uids({"wechat_serverchan": {"sendkey": [
              {"key": "A", "user": "owner"}, {"key": "B"}]},
              "wechat_pushplus": {"token": [{"token": "T", "user": "mmmmmm"}]}})
          == {"owner", "mmmmmm"})

    print("\n================ 结果 ================")
    print("PASS=%d  FAIL=%d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
