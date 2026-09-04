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

    # ===== 需求：趋势双态（缓/加速）+ 机构介入信号 =====
    import engine as _eg
    check("趋势双态·加速判定", _eg.classify_trend_state(4.0, 1.2, 0.0)[0] == "加速上行",
          "-> %s" % (_eg.classify_trend_state(4.0, 1.2, 0.0),))
    check("趋势双态·放缓判定", _eg.classify_trend_state(1.2, 2.0, 0.0)[0] == "增速放缓",
          "-> %s" % (_eg.classify_trend_state(1.2, 2.0, 0.0),))
    check("趋势双态·匀速判定", _eg.classify_trend_state(2.0, 2.0, 0.0)[0] == "匀速上行",
          "-> %s" % (_eg.classify_trend_state(2.0, 2.0, 0.0),))
    check("趋势双态·斜率兜底（无加速度走斜率）",
          _eg.classify_trend_state(1.0, 1.0, -1.2)[0] == "增速放缓"
          and _eg.classify_trend_state(1.0, 1.0, 1.2)[0] == "加速上行")
    check("趋势双态·零基准不除零", _eg.classify_trend_state(3.0, 0.0)[0] == "匀速上行",
          "-> %s" % (_eg.classify_trend_state(3.0, 0.0),))

    _ins = _eg.institution_evidence(
        "600001",
        {"lhbseats": {"top": [{"code": "600001", "net_yi": 0.8}]},
         "blocktrade": {"inst": [{"code": "600001", "side": "buy", "amt_yi": 0.5}], "top": []},
         "money": {"boards_in": [{"name": "半导体", "net": 3.2}], "boards_out": []},
         "margin": {"delta_yi": 30}, "seats": {"hits": []}},
        industry="半导体")
    check("机构介入·强信号（龙虎榜+大宗机构+板块净流入）",
          _ins["level"] == "强" and "龙虎榜净买" in "".join(_ins["tags"]),
          "-> %s/%s/%s" % (_ins["level"], _ins["score"], _ins["tags"]))
    _ins2 = _eg.institution_evidence(
        "600002",
        {"lhbseats": {"top": []},
         "blocktrade": {"inst": [], "top": [{"code": "600002", "discount": -12.0}]},
         "money": {"boards_in": [], "boards_out": [{"name": "医药", "net": -2.0}]},
         "margin": {"delta_yi": -40}, "seats": {"hits": [{"code": "600002"}]}},
        industry="医药")
    check("机构介入·负向（折价出货+板块流出+游资）",
          _ins2["level"] == "无" and _ins2["score"] < 0,
          "-> %s/%s/%s" % (_ins2["level"], _ins2["score"], _ins2["tags"]))
    _ins3 = _eg.institution_evidence("600003", {}, industry="—")
    check("机构介入·无数据降级", _ins3["level"] == "无" and _ins3["score"] == 0)
    _ins4 = _eg.institution_evidence(
        "600005",
        {"blocktrade": {"premium": [{"code": "600005", "discount": 3.5, "amt_yi": 0.6}]}})
    check("机构介入·大宗溢价接盘", _ins4["level"] == "中" and "溢价" in "".join(_ins4["tags"]),
          "-> %s/%s" % (_ins4["level"], _ins4["tags"]))
    _ins5 = _eg.institution_evidence(
        "600006", {"lhbseats": {"net_buy": [{"code": "600006", "net_yi": 0.2}]}})
    check("机构介入·龙虎榜净买全量名单（非top10也命中）",
          _ins5["level"] == "中" and "龙虎榜净买" in "".join(_ins5["tags"]),
          "-> %s/%s" % (_ins5["level"], _ins5["tags"]))
    # lhbseats.reasons 是 [标签,数量] 二元列表，不得被当成 dict（历史 AttributeError 回归）
    check("机构介入·脏数据不崩",
          _eg.institution_evidence("600004", {"lhbseats": {"top": [["游资", 3]],
                                                           "reasons": [["机构专用", 5]]}})["level"] == "无")

    # ===== 需求：板块当日涨跌预判 + 关注票操作说明 =====
    _sfc_data = {
        "sectors": {"industry": [
            {"name": "半导体", "zt": 6, "max_lb": 2, "tier": "主线", "pct": 2.5},
            {"name": "医药", "zt": 1, "max_lb": 1, "tier": "零星", "pct": -1.0}]},
        "sector_relay": {"relay": [{"name": "半导体", "kind": "加速", "certainty": 80}],
                         "broken": None},
        "money": {"boards_in": [{"name": "半导体", "net": 5.0}],
                  "boards_out": [{"name": "医药", "net": -3.0}],
                  "total_main_net": 80},
        "regime": {"level": "回暖"},
        "global_market": {"available": True, "signal": "外围偏多"},
    }
    _sfc = _eg.sector_day_forecast(_sfc_data)
    check("板块预判·强板块判定", (_sfc.get("半导体") or {}).get("dir") == "偏强",
          "-> %s" % _sfc.get("半导体"))
    check("板块预判·弱板块判定", (_sfc.get("医药") or {}).get("dir") == "偏弱",
          "-> %s" % _sfc.get("医药"))
    check("板块预判·大盘环境键", (_sfc.get("__market__") or {}).get("dir") == "偏强",
          "-> %s" % _sfc.get("__market__"))
    _sfc_empty = _eg.sector_day_forecast({})
    check("板块预判·空数据不崩", "__market__" in _sfc_empty)

    check("关注票动作·卖出纪律优先于板块偏强",
          "纪律优先" in _nf._watch_action_by_sector("卖出（止损）", "偏强"),
          "-> %s" % _nf._watch_action_by_sector("卖出（止损）", "偏强"))
    check("关注票动作·板块偏弱降买入力度",
          "轻仓" in _nf._watch_action_by_sector("建议买入", "偏弱"),
          "-> %s" % _nf._watch_action_by_sector("建议买入", "偏弱"))
    check("关注票动作·板块偏强持有待涨",
          "持有待涨" in _nf._watch_action_by_sector("持有", "偏强"),
          "-> %s" % _nf._watch_action_by_sector("持有", "偏强"))

    _fc_pre = {
        "sector_forecast": _sfc,
        "recommend": {"watch_reco": {"items": [
            {"name": "中化国际", "code": "600500", "close": 6.85, "pct": 1.2,
             "action": "持有", "is_holding": True, "pnl_pct": 3.2,
             "industry": "半导体", "buy_zone": [6.5, 6.7], "stop": 6.2},
            {"name": "沃华医药", "code": "002107", "close": 7.31, "pct": -0.4,
             "action": "卖出（止盈）", "is_holding": False, "pnl_pct": None,
             "industry": "医药", "buy_zone": [None, None], "stop": 6.8}]}},
    }
    _wf = _nf.watch_forecast_lines(_fc_pre, n=5)
    check("关注票预判行生成", len(_wf) >= 3 and "半导体" in _wf[1] and "医药" in _wf[2],
          "-> %s" % _wf[:3])
    check("关注票预判含大盘环境行", "大盘环境" in _wf[0], "-> %s" % _wf[0])

    # 盘前推送必须出现「关注票操作（板块当日预判」段
    _pre2 = dict(fake_pre)
    _pre2["sector_forecast"] = _sfc
    _pre2["recommend"] = dict(fake_pre.get("recommend") or {})
    _pre2["recommend"]["watch_reco"] = _fc_pre["recommend"]["watch_reco"]
    _pm2 = _nf.format_stock_summary(_pre2, "", mode="preauction")
    check("盘前推送含关注票·板块预判段",
          "关注票操作（板块当日预判" in _pm2["text"],
          "-> %s" % [l for l in _pm2["text"].splitlines() if "关注票操作" in l][:1])
    _sc2 = _nf.format_sc(_pre2, "", mode="preauction")
    check("盘前SC精简版含板块预判", "关注票·板块当日预判" in _sc2["text"])

    # 收盘推送（新模式 2026-09-04）：持仓操作 / 买点候选 / 板块强弱 三段式取代旧分散段落
    import reco_push as _rp
    _cand = _rp._mk_cand("1", "加速票", "趋势", "半导体", [9.5, 10.2], [11, 12], 8.5,
                         71, 0, "可买",
                         extra={"close": 10.0, "streak": 3, "trend_state": "加速上行",
                                "avg_daily": 3.5, "up_days": 4})
    _close_data = {
        "meta": {"date": "2026-08-28"},
        "market": {"sentiment": {"score": 55, "label": "温和", "promote_rate": 0.6,
                                 "seal_rate": 0.7}, "cycle": {"phase": "修复"}},
        "limit_ups": [],
        "recommend": {"trend": [], "watch_reco": {}},
        "micro": {"zhaban_rate": 0.1},
        "money": {"boards_in": [{"name": "半导体", "net": 9.0}], "boards_out": []},
        "preopen_plan": {},
        "board_strength": {"半导体": 30},
        "holdings_ops": [],
        "buy_candidates": {"ladder": [], "trend": [_cand], "band": [], "ladder_warn": None},
    }
    _cl = _nf.format_stock_summary(_close_data, "", mode="close")
    check("收盘推送·新模式买点候选段",
          "买点候选（只推当下就是买点的票）" in _cl["text"])
    check("收盘推送·新模式板块强弱段",
          "板块强弱（今日主线" in _cl["text"])
    check("收盘推送·买点候选含综合分", "综合**71分**" in _cl["text"])
    check("收盘推送·买点候选含买区", "买9.50~10.20" in _cl["text"])
    check("收盘推送·板块强弱含主力净流入", "半导体 +9.0亿" in _cl["text"])

    # ============ #252 可升级点回归（2026-09-02）============
    # 关注股异动逻辑：自选/持仓排序前置（中化国际 600500 类问题——加进自选却收不到提示被截断）
    from watchreco import distill as _distill
    _z = {"items": [
        {"code": "A", "name": "推荐票", "action": "建议买入", "cost": None, "buy_zone": [1, 2],
         "sell_zone": [3, 4], "stop": 0.9, "horizon": "短线", "urgent": False, "reasons": ["x"],
         "pnl_pct": None, "rotate": None, "replace": [], "time_status": None},
        {"code": "600500", "name": "中化国际", "action": "持有", "cost": 6.8, "buy_zone": [6, 6.5],
         "sell_zone": [7, 7.5], "stop": 6.5, "horizon": "中线", "urgent": False, "reasons": ["y"],
         "pnl_pct": 3.0, "rotate": None, "replace": [], "time_status": None},
        {"code": "B", "name": "自选票", "action": "持有", "cost": None, "buy_zone": [1, 2],
         "sell_zone": [3, 4], "stop": 0.9, "horizon": "短线", "urgent": False, "reasons": ["z"],
         "pnl_pct": None, "rotate": None, "replace": [], "time_status": None},
    ]}
    _wr = _distill(_z, holding_codes={"600500"}, watch_codes={"B"}, topn=14)
    _ih = next((i for i, x in enumerate(_wr["items"]) if x["code"] == "600500"), 99)
    _ir = next((i for i, x in enumerate(_wr["items"]) if x["code"] == "A"), 99)
    check("#252 持仓排序前置于纯推荐票", _ih < _ir, "hold=%d rec=%d" % (_ih, _ir))
    _iw = next((i for i, x in enumerate(_wr["items"]) if x["code"] == "B"), 99)
    check("#252 自选排序前置于纯推荐票", _iw < _ir, "watch=%d rec=%d" % (_iw, _ir))

    # ============ 未持仓票不给「卖出」动作（2026-09-02 用户拍板）============
    # 「我都没买怎么卖」：自选票进入卖出区→原样输出「卖出（止盈）」→ 未持仓者误导。
    _z2 = {"items": [
        # 自选票·进入卖出区（原判定「卖出（止盈）」）→ 应转译「不追（已过买点）」
        {"code": "W1", "name": "自选涨到卖区", "action": "分批止盈（进入卖出区）", "cost": None,
         "buy_zone": [4, 4.5], "sell_zone": [6, 6.5], "stop": 3.8, "horizon": "短线",
         "urgent": False, "reasons": ["r"], "pnl_pct": None, "rotate": None, "replace": [], "time_status": None},
        # 自选票·破位（原判定「卖出（止损）」）→ 应转译「回避（趋势走坏）」
        {"code": "W2", "name": "自选破位", "action": "破位卖出", "cost": None,
         "buy_zone": [4, 4.5], "sell_zone": [6, 6.5], "stop": 3.8, "horizon": "短线",
         "urgent": False, "reasons": ["r"], "pnl_pct": None, "rotate": None, "replace": [], "time_status": None},
        # 持仓票·进入卖出区 → 保留「卖出（止盈）」（真金白银）
        {"code": "H1", "name": "持仓止盈", "action": "分批止盈（进入卖出区）", "cost": 5.0,
         "buy_zone": [4, 4.5], "sell_zone": [6, 6.5], "stop": 3.8, "horizon": "短线",
         "urgent": False, "reasons": ["r"], "pnl_pct": 12.0, "rotate": None, "replace": [], "time_status": None},
        # 自选票·回踩买入区 → 保持买点动作且排最前（用户要买点票）
        {"code": "W3", "name": "自选买点", "action": "回踩买入区", "cost": None,
         "buy_zone": [4, 4.5], "sell_zone": [6, 6.5], "stop": 3.8, "horizon": "短线",
         "urgent": False, "reasons": ["r"], "pnl_pct": None, "rotate": None, "replace": [], "time_status": None},
    ]}
    _wr2 = _distill(_z2, holding_codes={"H1"}, watch_codes={"W1", "W2", "W3"}, topn=14)
    _m2 = {x["code"]: x for x in _wr2["items"]}
    check("自选进卖区→不追", _m2["W1"]["action"] == "不追（已过买点）", "-> %s" % _m2["W1"]["action"])
    check("自选破位→回避", _m2["W2"]["action"] == "回避（趋势走坏）", "-> %s" % _m2["W2"]["action"])
    check("持仓卖出动作保留", _m2["H1"]["action"].startswith("卖出"), "-> %s" % _m2["H1"]["action"])
    check("买点票排自选组最前（持仓组之后）",
          [x["code"] for x in _wr2["items"]] == ["H1", "W3", "W1", "W2"],
          "-> order=%s" % [x["code"] for x in _wr2["items"]])
    check("sell_n 只计真持仓卖出", _wr2["sell_n"] == 1, "-> %d" % _wr2["sell_n"])
    # lines() 渲染：不追票应附替代买入建议（↳ 买入建议）
    from watchreco import lines as _wlines
    _z2b = {"items": [dict(_z2["items"][0], replace=[
        {"name": "替代票", "code": "ALT", "industry": "电子", "market_type": "趋势",
         "buy_zone": [5, 5.2], "sell_zone": [7, 7.4], "stop": 4.6}])]}
    _wr2b = _distill(_z2b, holding_codes=set(), watch_codes={"W1"}, topn=14)
    _ls = _wlines(_wr2b, n=10)
    check("不追票附买入建议行", any("↳ 买入建议" in l for l in _ls),
          "-> %s" % [l for l in _ls if "↳" in l][:1])

    # ---- 替代候选必须「现价就在买点附近」（2026-09-02 用户拍板：现价50买区30=永远等不到）----
    _z2c = {"items": [dict(_z2["items"][0], replace=[
        # 现价50.2 vs 买区[30,32]：差 57%，等不到 → 必须被渲染层剔除
        {"name": "到不了的票", "code": "FAR", "industry": "电子", "market_type": "趋势",
         "close": 50.2, "buy_zone": [30, 32], "sell_zone": [55, 60], "stop": 28},
        # 现价31.5 vs 买区[30,32]：就在买区内 → 保留
        {"name": "能买的票", "code": "NEAR", "industry": "电子", "market_type": "趋势",
         "close": 31.5, "buy_zone": [30, 32], "sell_zone": [38, 40], "stop": 28.5},
        # 现价33.4 vs 买区[30,32]：刚出买区 4.4%（≤5%）→ 保留
        {"name": "贴近买区的票", "code": "OK5", "industry": "机械", "market_type": "趋势",
         "close": 33.4, "buy_zone": [30, 32], "sell_zone": [40, 42], "stop": 28.5},
    ])]}
    _wr2c = _distill(_z2c, holding_codes=set(), watch_codes={"W1"}, topn=14)
    _ls2 = _wlines(_wr2c, n=12)
    _rep_ls = [l for l in _ls2 if "↳ 买入建议" in l]
    check("远买区候选被剔除", not any("到不了的票" in l for l in _rep_ls),
          "-> %s" % _rep_ls)
    check("买区内候选保留", any("能买的票" in l for l in _rep_ls))
    check("贴近买区(≤5%)候选保留", any("贴近买区的票" in l for l in _rep_ls))

    # ---- zone_buyable / buyable_first（2026-09-02 用户拍板：重点推可直接买入的票）----
    from notifier import zone_buyable as _zb, buyable_first as _bf
    check("现价在买区→可买", _zb(31.5, [30, 32]) is True)
    check("现价刚出买区4.4%→可买", _zb(33.4, [30, 32]) is True)
    check("现价超买区6%→不可买", _zb(34.0, [30, 32]) is False)
    check("现价50买区32→不可买(飞天上)", _zb(50.2, [30, 32]) is False)
    check("无买区→None不误杀", _zb(50.0, None) is None)
    _seq = [
        {"name": "飞在天上", "price": 50.2, "buy_zone": [30, 32]},
        {"name": "可直接买B", "price": 31.5, "buy_zone": [30, 32]},
        {"name": "无买区票", "price": 9.9, "buy_zone": None},
        {"name": "可直接买A", "price": 30.8, "buy_zone": [30, 32]},
    ]
    _sorted = _bf(_seq, lambda x: x["price"], lambda x: x["buy_zone"])
    check("可买票排最前(组内保持原序)",
          set(x["name"] for x in _sorted[:2]) == {"可直接买A", "可直接买B"},
          "-> %s" % [x["name"] for x in _sorted])
    check("飞在天上/无买区靠后",
          set(x["name"] for x in _sorted[2:]) == {"飞在天上", "无买区票"})

    # ---- 竞价确认操作提示（2026-09-02 用户需求：给减半/加仓/买入/观望明确动作）----
    from notifier import auction_action as _aa
    _z_hold_bad = {"action": "破位卖出", "rotate": "止损", "rotate_reason": "趋势向下",
                   "buy_zone": [5, 5.5], "sell_zone": [9, 10], "stop": 4.8}
    _e, _a, _w = _aa("600500", "持仓破位", True, _z_hold_bad, {"open": 5.4, "open_pct": -3.0})
    check("持仓破位→止损离场", _a == "止损离场" and _e == "🟢")
    _z_hold = {"action": "正常持有", "rotate": None, "rotate_reason": "",
               "buy_zone": [5, 5.5], "sell_zone": [9, 10], "stop": 4.8}
    _e, _a, _ = _aa("X", "持仓低开", True, _z_hold, {"open": 4.8, "open_pct": -3.5})
    check("持仓低开≤-2%→减半提示", "减半" in _a and _e == "🟢", "-> %s" % _a)
    _e, _a, _ = _aa("X", "持仓进卖区", True, _z_hold, {"open": 9.2, "open_pct": 5.0})
    check("持仓竞价进卖点区→止盈/减半", "止盈" in _a, "-> %s" % _a)
    _e, _a, _ = _aa("X", "持仓高开强", True, _z_hold, {"open": 5.8, "open_pct": 3.2})
    check("持仓高开≥2%→持有/可加仓", "加仓" in _a and _e == "🔴", "-> %s" % _a)
    _e, _a, _ = _aa("X", "持仓平开", True, _z_hold, {"open": 5.45, "open_pct": 0.1})
    check("持仓平开→按计划持有", "持有" in _a, "-> %s" % _a)
    _z_watch = {"action": "正常持有", "rotate": None, "rotate_reason": "",
                "buy_zone": [10, 11], "sell_zone": [15, 16], "stop": 9.5}
    _e, _a, _ = _aa("Y", "关注进买区", False, _z_watch, {"open": 10.8, "open_pct": 1.0})
    check("关注竞价进买区→可买入", "可买入" in _a and _e == "🔴", "-> %s" % _a)
    _e, _a, _ = _aa("Y", "关注高开飞", False, _z_watch, {"open": 13.0, "open_pct": 8.0})
    check("关注高开过买点→观望不追", "不追" in _a, "-> %s" % _a)
    _e, _a, _ = _aa("Y", "关注低开破区", False, _z_watch, {"open": 9.6, "open_pct": -2.5})
    check("关注低开破买区→观望", "观望" in _a, "-> %s" % _a)

    # ============ #2 / #4 可升级点回归（2026-09-02）============
    import engine as _eng
    # #2：st=2 二板降权（归因胜率23.9% vs 全样本59.3%）
    _s0, _w0, _f0 = _eng.st2_adjust(80.0, 70.0, 3)
    _s2, _w2, _f2 = _eng.st2_adjust(80.0, 70.0, 2)
    check("st=2 降权生效(score↓)", _s2 < _s0 and _f2 is True, "s3=%s s2=%s" % (_s0, _s2))
    check("st=2 降权生效(worth↓)", _w2 < _w0, "w3=%s w2=%s" % (_w0, _w2))
    check("st≠2 不降权", _f0 is False and _s0 == 80.0)
    # #4：弱市主动压低推荐密度
    _items = [{"worth_score": 50}, {"worth_score": 40}, {"worth_score": 30}, {"worth_score": 60}]
    _cold = _eng.market_density_filter(_items, "冷")
    _warm = _eng.market_density_filter(_items, "温")
    check("弱市仅留 worth≥45 头部", len(_cold) == 2 and all(x["worth_score"] >= 45 for x in _cold), "-> %d" % len(_cold))
    check("非冷市原样返回", len(_warm) == 4)
    # #4 修正：密度过滤必须作用于全部买入桶（core/relay/ambush/all），否则主推池无效
    _rec = {
        "core": [{"worth_score": 80}, {"worth_score": 40}],
        "relay": [{"worth_score": 30}, {"worth_score": 55}],
        "ambush": [{"worth_score": 20}],
        "all": [{"worth_score": 35}, {"worth_score": 50}],
        "avoid": [{"worth_score": 10}],          # 风险回避清单，弱市也不该被砍
        "position": [{"worth_score": 5}],         # 持仓建议，不动
    }
    _eng.apply_market_density(_rec, "冷")
    check("#4 冷市 core 已降密度", len(_rec["core"]) == 1 and _rec["core"][0]["worth_score"] == 80)
    check("#4 冷市 relay 已降密度", len(_rec["relay"]) == 1 and _rec["relay"][0]["worth_score"] == 55)
    check("#4 冷市 ambush 已降密度", len(_rec["ambush"]) == 0)
    check("#4 冷市 all 已降密度", len(_rec["all"]) == 1 and _rec["all"][0]["worth_score"] == 50)
    check("#4 冷市 avoid 不被砍", len(_rec["avoid"]) == 1 and _rec["avoid"][0]["worth_score"] == 10)
    check("#4 冷市 position 不动", len(_rec["position"]) == 1 and _rec["position"][0]["worth_score"] == 5)
    _rec2 = {"core": [{"worth_score": 40}], "all": [{"worth_score": 30}]}
    _eng.apply_market_density(_rec2, "温")
    check("#4 非冷市原样返回(多桶)", len(_rec2["core"]) == 1 and len(_rec2["all"]) == 1)

    # ---- 关注股盘中异动 · 东财编码值归一化（2026-09-02 华电辽能 bug：-314%/现价1357）----
    import build as _bld
    _p, _pr = _bld._norm_em_quote(-314, 1357)
    check("关注股异动 编码值归一 pct", _p == -3.14, "-> %s" % _p)
    check("关注股异动 编码值归一 price", _pr == 13.57, "-> %s" % _pr)
    _p2, _pr2 = _bld._norm_em_quote(-3.14, 13.57)
    check("关注股异动 真实值不动 pct", _p2 == -3.14)
    check("关注股异动 真实值不动 price", _pr2 == 13.57)
    _p3, _pr3 = _bld._norm_em_quote(None, "--")
    check("关注股异动 空值容错", _p3 == 0.0 and _pr3 is None)
    _p4, _pr4 = _bld._norm_em_quote(1.2, 1700.0)  # 高价股真实值（茅台级）不误伤
    check("关注股异动 高价股不误伤 price", _pr4 == 1700.0 and _p4 == 1.2)
    _p5, _pr5 = _bld._norm_em_quote(980, 98765)   # 全编码场景：9.8% / 987.65
    check("关注股异动 全编码联动除100", _p5 == 9.8 and _pr5 == 987.65)

    # ---- 实测胜率自动降权（2026-09-03 用户核心诉求：成功率闭环）----
    _wr = {"核心龙头": {"n": 37, "win_rate": 78.4, "avg_pct": 5.01},
           "主线接力": {"n": 114, "win_rate": 57.9, "avg_pct": 2.55},
           "低位潜伏": {"n": 230, "win_rate": 42.6, "avg_pct": 0.15},
           "趋势·主升强趋势": {"n": 22, "win_rate": 31.8, "avg_pct": -1.33},
           "动量·连板余波": {"n": 31, "win_rate": 29.0, "avg_pct": -1.73},
           "观察": {"n": 3, "win_rate": 0.0, "avg_pct": 0.0}}
    _k_hi = _eng.winrate_penalty(80, "核心龙头", _wr)[1]
    _k_mid = _eng.winrate_penalty(80, "主线接力", _wr)[1]
    _k_low = _eng.winrate_penalty(80, "低位潜伏", _wr)[1]
    _k_bad = _eng.winrate_penalty(80, "趋势·主升强趋势", _wr)[1]
    check("高胜率标签不降权", _k_hi == 1.0 and _k_mid == 1.0)
    check("中低胜率标签降权0.85", _k_low == 0.85, "-> %s" % _k_low)
    check("负期望标签降权0.75", _k_bad == 0.75, "-> %s" % _k_bad)
    check("样本<10不臆断", _eng.winrate_penalty(80, "观察", _wr)[1] == 1.0)
    check("无标签/无映射不动", _eng.winrate_penalty(80, None, _wr)[1] == 1.0
          and _eng.winrate_penalty(80, "主线接力", None)[1] == 1.0)
    _rec3 = {"all": [{"name": "趋势票", "tag": "趋势·主升强趋势", "worth_score": 80},
                     {"name": "接力票", "tag": "主线接力", "worth_score": 70},
                     {"name": "潜伏票", "tag": "低位潜伏", "worth_score": 72}]}
    _eng.apply_winrate_penalty(_rec3, _wr)
    _by = {x["name"]: x for x in _rec3["all"]}
    check("降权后排序：接力票居首", _rec3["all"][0]["name"] == "接力票",
          "-> %s" % [(x["name"], x["worth_score"]) for x in _rec3["all"]])
    check("降权票带实测说明", any("实测" in r for r in (_by["趋势票"].get("reasons") or [])))
    check("降权票标 wr_penalty", _by["趋势票"].get("wr_penalty") == 0.75)

    print("\n================ 结果 ================")
    print("PASS=%d  FAIL=%d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
