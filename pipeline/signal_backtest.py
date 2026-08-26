# -*- coding: utf-8 -*-
"""统一信号回测框架。

把「某信号在历史上出现后、标的后续 N 日表现」抽象成通用函数，
任意引擎（游资席位/风格切换/推荐池/题材）都能一键算：
  - 胜率（N 日上涨占比）
  - 平均收益 / 盈亏比
  - 样本数（样本 < min_n 视为不可信，不输出）
完全基于本地 market.db 的 bars，无需额外接口。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store


def _close_on(con, code, date):
    r = con.execute("SELECT close FROM bars WHERE code=? AND date=?", (code, date)).fetchone()
    return r[0] if r else None


def _next_date(con, code, date):
    r = con.execute(
        "SELECT date FROM bars WHERE code=? AND date>? ORDER BY date LIMIT 1",
        (code, date)).fetchone()
    return r[0] if r else None


def _bars_between(con, code, d0, d1):
    rows = con.execute(
        "SELECT date,close FROM bars WHERE code=? AND date>=? AND date<=? ORDER BY date",
        (code, d0, d1)).fetchall()
    return rows


def _bars_forward(con, code, date, fwd):
    """自 date（含）起取 fwd+1 根K线，只取所需条数，避免全表扫到末日。"""
    return con.execute(
        "SELECT date,close FROM bars WHERE code=? AND date>=? ORDER BY date LIMIT ?",
        (code, date, fwd + 1)).fetchall()


def backtest_events(con, events, fwd=1, min_n=8):
    """events: [(date, code), ...]；fwd: 持有 N 个交易日。
    返回 {n, win_rate, avg_ret, win_n, loss_n} 或 None（样本不足）。
    """
    wins = 0
    n = 0
    rets = []
    for date, code in events:
        c0 = _close_on(con, code, date)
        if c0 is None or c0 <= 0:
            continue
        # 取 date 之后第 fwd 个交易日的收盘价（rows 含 date 本身）
        rows = _bars_forward(con, code, date, fwd)
        if len(rows) <= fwd:
            continue
        c1 = rows[fwd][1]
        if c1 is None:
            continue
        pct = (c1 - c0) / c0 * 100.0
        n += 1
        rets.append(pct)
        if pct > 0:
            wins += 1
    if n < min_n:
        return None
    avg = sum(rets) / n
    # 盈亏比：平均盈利 / 平均亏损（绝对值）
    up = [x for x in rets if x > 0]
    dn = [x for x in rets if x < 0]
    pl = (sum(up) / len(up)) / (abs(sum(dn)) / len(dn)) if up and dn else None
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "win_n": wins,
        "loss_n": n - wins,
        "avg_ret": round(avg, 2),
        "pl_ratio": round(pl, 2) if pl else None,
    }


def t1_stats(con, events, min_samples=8):
    """T+1（次日）跟随胜率，样本不足返回 None。seats.py 直接调用。"""
    return backtest_events(con, events, fwd=1, min_n=min_samples)
