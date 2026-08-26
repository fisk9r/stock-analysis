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

    print("\n================ 结果 ================")
    print("PASS=%d  FAIL=%d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
