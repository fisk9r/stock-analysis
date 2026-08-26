# -*- coding: utf-8 -*-
"""缺口修复层：补齐行情库中整日缺失/严重残缺的交易日。

背景：盘后 fetch 依赖东财快照，若某日未运行或被限流，会留下整日空洞
（如 2026-08-06 完全缺失、2026-08-05 仅 1489/5538）。空洞会让"连板数"
跨日误算、"晋级率"被机械压低，进而污染情绪分。

策略：对指定日期，用腾讯 ifzq 源（不与东财争令牌）统一重拉近端日 K，
只写入目标日期，避免污染其它日期的东财原始字段。

用法：python repair_gap.py 2026-08-05 2026-08-06
"""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import em_api as A
import store

LIMIT = 15  # 只需近端若干根即可覆盖目标日


def log(*a):
    print("[repair]", *a, flush=True)


def run(targets, workers=10, missing_only=False, budget=None):
    t0 = time.time()
    targets = set(targets)
    con = store.connect()
    if missing_only:
        # 只重试在任一目标日仍缺行的个股（限流失败后的补漏，成本远低于全量）
        ph = ",".join("?" * len(targets))
        q = ("SELECT s.code, s.market FROM stocks s WHERE ("
             "SELECT COUNT(*) FROM bars b WHERE b.code=s.code AND b.date IN (%s)"
             ") < ?" % ph)
        stocks = con.execute(q, list(targets) + [len(targets)]).fetchall()
    else:
        stocks = con.execute("SELECT code, market FROM stocks").fetchall()
    log("目标日期 %s，待处理个股 %d 只%s"
        % (sorted(targets), len(stocks), "（仅补漏）" if missing_only else ""))

    have = {}
    for d in targets:
        n = con.execute("SELECT COUNT(*) FROM bars WHERE date=?", (d,)).fetchone()[0]
        have[d] = n
        log("  修复前 %s 已有 %d 行" % (d, n))
    con.close()

    lock = threading.Lock()
    buf, stat = [], {"ok": 0, "fail": 0, "rows": 0, "done": 0}
    total = len(stocks)

    def work(item):
        code, mkt = item[0], int(item[1] or 0)
        rows = []
        if budget and (time.time() - t0) > budget:
            with lock:
                stat["done"] += 1
            return
        try:
            raw = A.kline_tencent(code, mkt, LIMIT)
            for t in store.parse_kline(code, raw):
                if t[1] in targets:
                    rows.append(t)
            with lock:
                stat["ok"] += 1
        except Exception:
            with lock:
                stat["fail"] += 1
        with lock:
            buf.extend(rows)
            stat["rows"] += len(rows)
            stat["done"] += 1
            if stat["done"] % 500 == 0:
                log("  进度 %d/%d（成功 %d / 失败 %d / 命中 %d 行，%.0fs）"
                    % (stat["done"], total, stat["ok"], stat["fail"], stat["rows"], time.time() - t0))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, stocks))

    log("抓取完成：成功 %d / 失败 %d，命中目标日 %d 行，耗时 %.0fs"
        % (stat["ok"], stat["fail"], stat["rows"], time.time() - t0))

    con = store.connect()
    store.upsert_bars(con, buf)
    con.commit()
    for d in sorted(targets):
        n = con.execute("SELECT COUNT(*) FROM bars WHERE date=?", (d,)).fetchone()[0]
        z = con.execute("SELECT COUNT(*) FROM bars WHERE date=? AND pct>=9.7", (d,)).fetchone()[0]
        log("  修复后 %s -> %d 行（%s），其中涨幅>=9.7%% 共 %d 只" % (d, n, "原 %d" % have[d], z))
    con.close()
    log("总耗时 %.0fs" % (time.time() - t0))


if __name__ == "__main__":
    ds = [a for a in sys.argv[1:] if a.startswith("20")]
    if not ds:
        print("用法: python repair_gap.py 2026-08-05 2026-08-06 [--missing-only] [--budget=600]")
        sys.exit(1)
    mo = "--missing-only" in sys.argv
    bg = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--budget=")), None)
    run(ds, missing_only=mo, budget=bg)
