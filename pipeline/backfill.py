# -*- coding: utf-8 -*-
"""补齐层：把行情库中缺失/残缺的个股日 K 补全。

策略：东财 push2his 与腾讯 ifzq 双源并行（各领一半代码），
互不抢同一 host 的令牌桶，整体吞吐翻倍且单源压力减半；
每 200 只提交一次，中断后重跑自动跳过已完成部分。
"""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import em_api as A
import store

MIN_BARS = 100   # 少于该根数视为残缺，需要重拉
LIMIT = 130      # 每只拉取的日 K 根数


def log(*a):
    print("[backfill]", *a, flush=True)


def pending(con, min_bars=MIN_BARS):
    """-> [(code, market)] 需要补的个股"""
    cnt = dict(con.execute("SELECT code, COUNT(*) FROM bars GROUP BY code").fetchall())
    out = []
    for code, mkt, name in con.execute("SELECT code, market, name FROM stocks").fetchall():
        # 退市/停牌类不强求
        if cnt.get(code, 0) < min_bars:
            out.append((code, int(mkt or 0)))
    return out


def _pull_em(item):
    code, mkt = item
    return A.kline("%d.%s" % (mkt, code), LIMIT)


def _pull_tx(item):
    code, mkt = item
    return A.kline_tencent(code, mkt, LIMIT)


def run(min_bars=MIN_BARS, workers_em=6, workers_tx=6):
    t0 = time.time()
    con = store.connect()
    con.close()
    # 多线程共用一条连接需显式放开线程检查，写入统一走 wlock 串行化
    import sqlite3
    con = sqlite3.connect(store.DB_PATH, timeout=60, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    wlock = threading.Lock()
    todo = pending(con, min_bars)
    total = len(todo)
    if not total:
        log("行情库已完整，无需补齐")
        return 0
    log("待补 %d 只（每只 %d 日 K）" % (total, LIMIT))

    # 交错切分：两源各领一半，避免同一 host 令牌桶争抢
    part_em = todo[0::2]
    part_tx = todo[1::2]
    log("东财领 %d 只 / 腾讯领 %d 只" % (len(part_em), len(part_tx)))

    lock = threading.Lock()
    buf = []
    stat = {"ok": 0, "fail": 0, "rows": 0, "done": 0, "last": t0}

    def flush(force=False):
        nonlocal buf
        with lock:
            if not buf or (len(buf) < 4000 and not force):
                return
            chunk, buf = buf, []
        with wlock:
            store.upsert_bars(con, chunk)
            con.commit()

    def handle(item, primary, secondary):
        code, mkt = item
        ks = []
        for fn in (primary, secondary):
            try:
                ks = fn(item)
            except Exception:
                ks = []
            if ks:
                break
        rows = store.parse_kline(code, ks) if ks else []
        with lock:
            stat["done"] += 1
            if rows:
                stat["ok"] += 1
                stat["rows"] += len(rows)
                buf.extend(rows)
            else:
                stat["fail"] += 1
            n, now = stat["done"], time.time()
            if n % 200 == 0:
                sp = n / max(0.001, now - t0)
                log("  %d/%d  成功 %d  失败 %d  %.1f 只/s  剩余约 %.0fs  em=%.1f/s tx=%.1f/s"
                    % (n, total, stat["ok"], stat["fail"], sp, (total - n) / max(sp, 0.01),
                       A.limiter("push2his.eastmoney.com").rate,
                       A.limiter("web.ifzq.gtimg.cn").rate))
        if len(buf) >= 4000:
            flush()

    def run_em():
        with ThreadPoolExecutor(max_workers=workers_em) as ex:
            list(ex.map(lambda it: handle(it, _pull_em, _pull_tx), part_em))

    def run_tx():
        with ThreadPoolExecutor(max_workers=workers_tx) as ex:
            list(ex.map(lambda it: handle(it, _pull_tx, _pull_em), part_tx))

    th = [threading.Thread(target=run_em), threading.Thread(target=run_tx)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    flush(force=True)

    n = con.execute("SELECT COUNT(DISTINCT code) FROM bars").fetchone()[0]
    d = con.execute("SELECT COUNT(DISTINCT date) FROM bars").fetchone()[0]
    log("补齐完成：新增 %d 行，成功 %d / 失败 %d，耗时 %.0fs"
        % (stat["rows"], stat["ok"], stat["fail"], time.time() - t0))
    log("行情库现覆盖 %d 只个股 / %d 个交易日" % (n, d))
    con.close()
    return stat["fail"]


if __name__ == "__main__":
    mb = MIN_BARS
    for a in sys.argv[1:]:
        if a.startswith("--min="):
            mb = int(a.split("=", 1)[1])
    run(mb)
