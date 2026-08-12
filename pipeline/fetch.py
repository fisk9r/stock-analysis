# -*- coding: utf-8 -*-
"""采集层：刷新本地行情库 + 抓取当日盘后快照

策略：
  · 首次运行 → 全市场逐只拉 130 日 K 线建库（东财主源，失败自动切腾讯备源）
  · 日常增量 → 全市场列表接口一次拿到当日完整 OHLCV（约 59 个请求，秒级）
  · 断档补齐 → 若库内最新日期落后，则全市场补拉近 30 根 K 线
"""
import json
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import em_api as api
import store

ROOT = store.ROOT
ARCHIVE = os.path.join(ROOT, "archive")
CLOSE_TIME = "1505"   # 收盘落定时间（北京时间）

_BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _bj_now():
    """北京时间 naive datetime（CI runner 为 UTC，必须用北京时间判断收盘/日期）。"""
    return datetime.datetime.now(_BJ_TZ).replace(tzinfo=None)


def log(*a):
    print("[fetch]", *a, flush=True)


def market_closed():
    return _bj_now().strftime("%H%M") >= CLOSE_TIME


def refresh_stocks(con):
    log("拉取全市场股票清单与当日快照 ...")
    rows = api.all_stocks()
    rows = [r for r in rows if r["code"] and r["name"] and "退市" not in r["name"]]
    store.upsert_stocks(con, rows)
    store.meta_set(con, "stocks_updated", _bj_now().strftime("%Y-%m-%d %H:%M:%S"))
    con.commit()
    log("股票清单 %d 只" % len(rows))
    return rows


def snapshot_to_bars(con, rows, date=None):
    """把当日快照直接写成日K（仅收盘后调用）"""
    date = date or _bj_now().strftime("%Y-%m-%d")
    tup = []
    for r in rows:
        if r["price"] is None or r["open"] is None or r["prev_close"] in (None, 0):
            continue
        tup.append((r["code"], date, r["open"], r["price"], r["high"], r["low"],
                    r["vol"] or 0, r["amount"] or 0, r["amp"] or 0, r["pct"] or 0,
                    r["chg"] or 0, r["turnover"] or 0))
    if tup:
        store.upsert_bars(con, tup)
        con.commit()
    log("当日快照写入日K %d 行（%s）" % (len(tup), date))
    return len(tup)


def refresh_bars(con, stocks, full_days=130):
    n_code = con.execute("SELECT COUNT(DISTINCT code) FROM bars").fetchone()[0] or 0
    n_date = con.execute("SELECT COUNT(DISTINCT date) FROM bars").fetchone()[0] or 0
    last = con.execute("SELECT MAX(date) FROM bars").fetchone()[0]
    today = _bj_now().strftime("%Y-%m-%d")

    need_full = n_code < len(stocks) * 0.6 or n_date < 60
    if need_full:
        log("首次建库：全市场 %d 只 × %d 日 K 线（预计数分钟）..." % (len(stocks), full_days))
        return _pull_klines(con, stocks, full_days)

    # 断档判断：库内最新日期与今天相差超过 1 个自然周 → 补拉
    gap_days = 0
    if last:
        gap_days = (time.mktime(time.strptime(today, "%Y-%m-%d"))
                    - time.mktime(time.strptime(last, "%Y-%m-%d"))) / 86400
    if gap_days > 4:
        log("库内最新 %s，落后 %.0f 天，全市场补拉近 35 日 ..." % (last, gap_days))
        return _pull_klines(con, stocks, 35)

    if market_closed():
        snapshot_to_bars(con, stocks, today)
    else:
        log("尚未收盘（%s），跳过当日快照入库，分析将以上一交易日为准" % _bj_now().strftime("%H:%M"))
    return 0


def _pull_klines(con, stocks, lmt):
    items = [(s["code"], s["market"]) for s in stocks]
    t0 = time.time()

    def prog(d, t):
        el = time.time() - t0
        rate = api.limiter("push2his.eastmoney.com").rate
        log("  K线 %d/%d  用时 %.0fs  剩余约 %.0fs  当前限速 %.1f/s"
            % (d, t, el, el / max(d, 1) * (t - d), rate))

    res, nfail = api.kline_batch(items, limit=lmt, workers=12, on_progress=prog)
    buf, total = [], 0
    for code, ks in res.items():
        buf.extend(store.parse_kline(code, ks))
        if len(buf) > 50000:
            store.upsert_bars(con, buf)
            con.commit()
            total += len(buf)
            buf = []
    if buf:
        store.upsert_bars(con, buf)
        total += len(buf)
    con.commit()
    got = sum(1 for v in res.values() if v)
    log("日K写入 %d 行，成功 %d/%d 只（主源失败 %d 只已走备源），耗时 %.0fs"
        % (total, got, len(items), nfail, time.time() - t0))
    return total


def refresh_boards(con, force=False, max_age_days=7):
    upd = store.meta_get(con, "boards_updated")
    cnt = con.execute("SELECT COUNT(*) FROM board_member").fetchone()[0] or 0
    fresh = False
    if upd and cnt > 10000:
        try:
            fresh = (time.time() - time.mktime(time.strptime(upd, "%Y-%m-%d %H:%M:%S"))) < max_age_days * 86400
        except Exception:
            fresh = False
    if fresh and not force:
        log("板块成分缓存有效（%s，%d 条映射），跳过" % (upd, cnt))
        return
    log("重建板块成分映射（行业 + 概念）...")
    boards = api.board_list("industry") + api.board_list("concept")
    log("  板块 %d 个，拉取成分股 ..." % len(boards))
    t0 = time.time()
    members = api.board_members_batch(
        [b["bk"] for b in boards], workers=8,
        on_progress=lambda d, t: log("  成分 %d/%d  %.0fs" % (d, t, time.time() - t0)))
    store.upsert_boards(con, boards, members)
    store.meta_set(con, "boards_updated", _bj_now().strftime("%Y-%m-%d %H:%M:%S"))
    con.commit()
    log("板块映射完成 %d 个板块，耗时 %.0fs" % (len(members), time.time() - t0))


def snapshot_today():
    log("抓取涨停池 / 炸板池 / 强势股池 / 指数 / 板块行情 ...")
    snap = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "market_closed": market_closed()}
    for k, fn in [("zt", api.zt_pool), ("zb", api.zb_pool), ("qs", api.qs_pool),
                  ("dt", api.dt_pool), ("index", api.index_snapshot),
                  ("fundflow", api.market_fundflow)]:
        try:
            snap[k] = fn()
        except Exception as e:
            log("  %s 抓取失败：%r" % (k, e))
            snap[k] = []
    for k, kind in [("board_industry", "industry"), ("board_concept", "concept")]:
        try:
            snap[k] = api.board_list(kind)
        except Exception as e:
            log("  %s 抓取失败：%r" % (k, e))
            snap[k] = []
    log("  涨停 %d / 炸板 %d / 强势 %d / 跌停 %d / 行业板块 %d"
        % (len(snap.get("zt") or []), len(snap.get("zb") or []), len(snap.get("qs") or []),
           len(snap.get("dt") or []), len(snap.get("board_industry") or [])))
    return snap


def save_archive(snap):
    os.makedirs(ARCHIVE, exist_ok=True)
    p = os.path.join(ARCHIVE, "snapshot_%s.json" % _bj_now().strftime("%Y%m%d"))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    log("盘后快照归档 -> %s" % os.path.relpath(p, ROOT))


def run(force_boards=False, skip_bars=False, skip_boards=False):
    con = store.connect()
    stocks = refresh_stocks(con)
    if not skip_bars:
        refresh_bars(con, stocks)
    if not skip_boards:
        try:
            refresh_boards(con, force=force_boards)
        except Exception as e:
            log("板块映射刷新失败（不影响主流程，将使用涨停池自带行业字段降级）：%r" % e)
    save_archive(snapshot_today())
    dates = store.trade_dates(con)
    log("行情库：%d 只个股 / %d 个交易日（%s ~ %s）"
        % (con.execute("SELECT COUNT(DISTINCT code) FROM bars").fetchone()[0],
           len(dates), dates[0] if dates else "-", dates[-1] if dates else "-"))
    con.close()


if __name__ == "__main__":
    run(force_boards="--boards" in sys.argv, skip_bars="--nobars" in sys.argv,
        skip_boards="--noboards" in sys.argv)
