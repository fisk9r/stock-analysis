# -*- coding: utf-8 -*-
"""本地缓存层：SQLite 存全市场日K + 股票清单 + 板块归属"""
import json
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "cache")
DB_PATH = os.path.join(CACHE_DIR, "market.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars(
  code TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, close REAL, high REAL, low REAL,
  vol REAL, amount REAL, amp REAL, pct REAL, chg REAL, turn REAL,
  PRIMARY KEY(code, date)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_bars_date ON bars(date);
CREATE TABLE IF NOT EXISTS stocks(
  code TEXT PRIMARY KEY, market INTEGER, name TEXT,
  total_mv REAL, float_mv REAL
);
CREATE TABLE IF NOT EXISTS boards(
  bk TEXT NOT NULL, kind TEXT, name TEXT, PRIMARY KEY(bk)
);
CREATE TABLE IF NOT EXISTS board_member(
  bk TEXT NOT NULL, code TEXT NOT NULL, PRIMARY KEY(bk, code)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_bm_code ON board_member(code);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS rec_history(
  date TEXT PRIMARY KEY, max_streak INTEGER, lb_count INTEGER, zt_count INTEGER,
  sent_score REAL, cycle_phase TEXT, env_k REAL, n_rec INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS rec_picks(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, streak INTEGER,
  p_break REAL, tag TEXT, next_continue INTEGER DEFAULT -1,
  next_pct REAL DEFAULT NULL, PRIMARY KEY(date, code)
);
CREATE TABLE IF NOT EXISTS global_market(
  region TEXT, code TEXT, name TEXT, price REAL, pct REAL,
  fetched_at TEXT, PRIMARY KEY(region, code)
);
CREATE TABLE IF NOT EXISTS engine_snapshots(
  k TEXT NOT NULL, date TEXT NOT NULL, payload TEXT,
  PRIMARY KEY(k, date)
);
CREATE TABLE IF NOT EXISTS seat_daily(
  date TEXT NOT NULL, dept_code TEXT NOT NULL, label TEXT,
  code TEXT NOT NULL, name TEXT,
  net_yi REAL, act_buy_yi REAL, act_sell_yi REAL, chg REAL DEFAULT NULL,
  PRIMARY KEY(date, dept_code, code)
);
CREATE INDEX IF NOT EXISTS idx_seat_label ON seat_daily(label);
CREATE TABLE IF NOT EXISTS theme_daily(
  date TEXT NOT NULL, theme TEXT NOT NULL, n INTEGER,
  PRIMARY KEY(date, theme)
);
CREATE TABLE IF NOT EXISTS watch_firstseen(
  code TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trend_track(
  code TEXT PRIMARY KEY, name TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  times INTEGER DEFAULT 1
);
"""


def connect():
    os.makedirs(CACHE_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def meta_get(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))


def upsert_stocks(con, rows):
    con.executemany(
        "INSERT OR REPLACE INTO stocks(code,market,name,total_mv,float_mv) VALUES(?,?,?,?,?)",
        [(r["code"], r["market"], r["name"], r.get("total_mv"), r.get("float_mv")) for r in rows])


def parse_kline(code, klines):
    """东财 kline 原始行 -> bars 元组"""
    out = []
    for ln in klines:
        p = ln.split(",")
        if len(p) < 11:
            continue
        try:
            out.append((code, p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]),
                        float(p[5]), float(p[6]), float(p[7]), float(p[8]), float(p[9]),
                        float(p[10])))
        except ValueError:
            continue
    return out


def upsert_bars(con, tuples):
    # 源头清洗（2026-08-29）：停牌/接口空值日会写出全零行（实测 *ST康佳A、湖南黄金
    # 等停牌日 o/h/l/c 全 0，另有 close 保留但 o/h/l 全 0 的停牌占位行），
    # 曾致 maglue 除零、trendsword 索引错位越界、data_guard 报「价格异常」。
    # 收盘价<=0 或 最高/最低<=0 一律视为无效行拒收。
    tuples = [t for t in tuples
              if (t[3] or 0) > 0 and (t[4] or 0) > 0 and (t[5] or 0) > 0]
    con.executemany(
        "INSERT OR REPLACE INTO bars(code,date,open,close,high,low,vol,amount,amp,pct,chg,turn)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", tuples)


def purge_zero_bars(con):
    """清洗历史库中已存在的无效行（自愈：CI 每次构建启动时调用）。"""
    try:
        cur = con.execute(
            "DELETE FROM bars WHERE close<=0 OR close IS NULL "
            "OR high<=0 OR low<=0 OR high IS NULL OR low IS NULL")
        con.commit()
        return cur.rowcount
    except Exception:
        return 0


def load_bars(con, codes=None, since=None):
    """-> {code: [bar_dict,...] 按日期升序]}"""
    q = ("SELECT code,date,open,close,high,low,vol,amount,amp,pct,chg,turn FROM bars")
    cond, args = [], []
    if since:
        cond.append("date>=?")
        args.append(since)
    if codes is not None:
        codes = list(codes)
        cond.append("code IN (%s)" % ",".join("?" * len(codes)))
        args.extend(codes)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY code,date"
    res = {}
    for row in con.execute(q, args):
        res.setdefault(row[0], []).append({
            "d": row[1], "o": row[2], "c": row[3], "h": row[4], "l": row[5],
            "v": row[6], "amt": row[7], "amp": row[8], "pct": row[9],
            "chg": row[10], "turn": row[11]})
    return res


def trade_dates(con, limit=200):
    rows = con.execute(
        "SELECT date, COUNT(*) n FROM bars GROUP BY date HAVING n>500 ORDER BY date").fetchall()
    return [r[0] for r in rows][-limit:]


def upsert_boards(con, boards, members):
    con.executemany("INSERT OR REPLACE INTO boards(bk,kind,name) VALUES(?,?,?)",
                    [(b["bk"], b["kind"], b["name"]) for b in boards])
    pairs = []
    for bk, codes in members.items():
        for c in codes:
            pairs.append((bk, c))
    if pairs:
        con.executemany("INSERT OR REPLACE INTO board_member(bk,code) VALUES(?,?)", pairs)


def code_boards(con):
    """-> {code: [(bk,name,kind),...]}"""
    res = {}
    for bk, name, kind, code in con.execute(
            "SELECT b.bk,b.name,b.kind,m.code FROM board_member m JOIN boards b ON b.bk=m.bk"):
        res.setdefault(code, []).append((bk, name, kind))
    return res


# ---------------------------------------------------------------- 推荐历史 / 连板库
def rec_history_rows(con, limit=120):
    return con.execute(
        "SELECT date,max_streak,lb_count,zt_count,sent_score,cycle_phase,env_k,n_rec "
        "FROM rec_history ORDER BY date DESC LIMIT ?", (limit,)).fetchall()


def rec_picks_all(con, limit=400):
    return con.execute(
        "SELECT date,code,name,streak,p_break,tag,next_continue,next_pct "
        "FROM rec_picks ORDER BY date DESC LIMIT ?", (limit,)).fetchall()


def upsert_rec_day(con, date, max_streak, lb_count, zt_count, sent_score,
                   cycle_phase, env_k, n_rec):
    con.execute(
        "INSERT OR REPLACE INTO rec_history"
        "(date,max_streak,lb_count,zt_count,sent_score,cycle_phase,env_k,n_rec,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (date, max_streak, lb_count, zt_count, sent_score, cycle_phase, env_k,
         n_rec, time.strftime("%Y-%m-%d %H:%M:%S")))


def upsert_rec_pick(con, date, code, name, streak, p_break, tag):
    con.execute(
        "INSERT OR REPLACE INTO rec_picks"
        "(date,code,name,streak,p_break,tag) VALUES(?,?,?,?,?,?)",
        (date, code, name, streak, p_break, tag))


def backfill_rec_outcomes(con, u):
    """回填前一日推荐标的的次日真实结局（续板=1 / 断板=0）。"""
    row = con.execute("SELECT MAX(date) FROM rec_history").fetchone()
    if not row or not row[0]:
        return 0
    prev = row[0]
    nd = u.next_date(prev)
    if not nd:
        return 0
    zt_next = u.zt.get(nd, set())
    cnt = 0
    for date, code, _, _, _, _, _, _ in con.execute(
            "SELECT date,code,name,streak,p_break,tag,next_continue,next_pct "
            "FROM rec_picks WHERE date=?", (prev,)):
        b = u.bar(code, nd)
        cont = 1 if code in zt_next else 0
        npct = round(b["pct"], 2) if b else None
        con.execute("UPDATE rec_picks SET next_continue=?, next_pct=? WHERE date=? AND code=?",
                    (cont, npct, date, code))
        cnt += 1
    con.commit()
    return cnt


# ---------------------------------------------------------------- 外围市场缓存
def upsert_global(con, region, code, name, price, pct, fetched_at):
    con.execute(
        "INSERT OR REPLACE INTO global_market(region,code,name,price,pct,fetched_at)"
        " VALUES(?,?,?,?,?,?)", (region, code, name, price, pct, fetched_at))


def global_rows(con):
    return con.execute(
        "SELECT region,code,name,price,pct,fetched_at FROM global_market").fetchall()


# ---------------------------------------------------------------- 引擎历史快照
def save_snapshot(con, k, date, payload):
    """引擎当日摘要 JSON 落库（margin/etfflow/lhbseats/themes...），同日重跑覆盖"""
    con.execute(
        "INSERT OR REPLACE INTO engine_snapshots(k,date,payload) VALUES(?,?,?)",
        (k, date, json.dumps(payload, ensure_ascii=False)))


def snapshot_history(con, k, days=20):
    """取某引擎最近 N 天快照，按日期正序返回 [(date, payload_dict)]"""
    rows = con.execute(
        "SELECT date,payload FROM engine_snapshots WHERE k=? ORDER BY date DESC LIMIT ?",
        (k, days)).fetchall()
    out = []
    for d, p in reversed(rows):
        try:
            out.append((d, json.loads(p)))
        except Exception:
            pass
    return out


def upsert_seats(con, date, rows):
    for r in rows:
        con.execute(
            "INSERT OR REPLACE INTO seat_daily(date,dept_code,label,code,name,"
            "net_yi,act_buy_yi,act_sell_yi,chg) VALUES(?,?,?,?,?,?,?,?,?)",
            (date, r["dept_code"], r["label"], r["code"], r["name"],
             r.get("net_yi"), r.get("act_buy_yi"), r.get("act_sell_yi"), r.get("chg")))


def seats_history(con, days=90):
    return con.execute(
        "SELECT date,dept_code,label,code,name,net_yi FROM seat_daily "
        "WHERE date>=? ORDER BY date", (_days_ago(days),)).fetchall()


def upsert_themes(con, date, counts):
    """counts: {theme: n}"""
    con.execute("DELETE FROM theme_daily WHERE date=?", (date,))
    for theme, n in counts.items():
        if n:
            con.execute("INSERT INTO theme_daily(date,theme,n) VALUES(?,?,?)",
                        (date, theme, n))


def themes_series(con, days=30):
    """返回 {theme: [(date, n)]} 按日期正序"""
    rows = con.execute(
        "SELECT date,theme,n FROM theme_daily WHERE date>=? ORDER BY date",
        (_days_ago(days),)).fetchall()
    out = {}
    for d, t, n in rows:
        out.setdefault(t, []).append((d, n))
    return out


def _days_ago(days):
    import datetime
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


# ---------------------------------------------------------------- 关注股锚点 / 趋势票持久化
def watch_first_seen(con, date, codes):
    """记录/读取关注股「首次进入关注池」的日期（用于「关注以来累计」统计）。

    返回 {code: first_seen_date}。新出现的 code 以传入 date 作为首见日写入。
    """
    out = {}
    for c in codes:
        row = con.execute("SELECT first_seen FROM watch_firstseen WHERE code=?", (c,)).fetchone()
        if row:
            out[c] = row[0]
        else:
            con.execute(
                "INSERT OR REPLACE INTO watch_firstseen(code,first_seen,last_seen) VALUES(?,?,?)",
                (c, date, date))
            out[c] = date
    # 已在池中的更新 last_seen，离池的不动（保留历史首见日）
    con.executemany("UPDATE watch_firstseen SET last_seen=? WHERE code=?",
                    [(date, c) for c in codes])
    con.commit()
    return out


def trend_track_upsert(con, date, picks):
    """把当日命中的趋势票写入 trend_track，保留首次见日与出现次数。

    picks: [{code, name, ...}]（当日严格筛选命中的趋势票）。
    返回 {code: {first_seen, times, last_seen, is_new}}。"""
    prev = {}
    for code, name, fs, ls, times in con.execute(
            "SELECT code,name,first_seen,last_seen,times FROM trend_track"):
        prev[code] = {"first_seen": fs, "last_seen": ls, "times": times, "name": name}
    out = {}
    for p in picks:
        c = p.get("code")
        if not c:
            continue
        if c in prev:
            times = prev[c]["times"] + 1
            fs = prev[c]["first_seen"]
            is_new = False
        else:
            times = 1
            fs = date
            is_new = True
        con.execute(
            "INSERT OR REPLACE INTO trend_track(code,name,first_seen,last_seen,times)"
            " VALUES(?,?,?,?,?)",
            (c, p.get("name") or "", fs, date, times))
        out[c] = {"first_seen": fs, "last_seen": date, "times": times, "is_new": is_new}
    con.commit()
    return out


def trend_track_states(con):
    """返回全部已记录趋势票状态 {code: {first_seen, last_seen, times, name}}。"""
    out = {}
    for code, name, fs, ls, times in con.execute(
            "SELECT code,name,first_seen,last_seen,times FROM trend_track"):
        out[code] = {"first_seen": fs, "last_seen": ls, "times": times, "name": name}
    return out


def trend_track_drop(con, codes):
    """趋势彻底破位的票移出跟踪（不再标历史），避免长期挂着失效标签。"""
    if not codes:
        return
    con.executemany("DELETE FROM trend_track WHERE code=?", [(c,) for c in codes])
    con.commit()
