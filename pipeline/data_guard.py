# -*- coding: utf-8 -*-
"""数据完整性守卫：检测并修复日K库中的量纲错乱

背景：备用数据源偶发返回 成交量=股（而非手）、成交额=分（而非元），
导致该交易日全市场 vol/amount 被放大 100 倍。这类脏数据会：
  · 把当日伪装成『天量爆发』（标杆股成交额环比 45x），热度百分位冲到 100；
  · 污染后续所有均量类指标（量比、量能衰减、持仓监测的缩量判断）。

守卫策略（保守）：
  1. 逐日计算『每只股票当日 vol / 该股前后邻近交易日 vol 中位数』的全市场中位比值；
  2. 仅当该比值稳定落在 [30, 300] → 判定 100 倍量纲错乱；落在 [300, 3000] → 1000 倍；
  3. 修复时同步缩放 vol 与 amount，并在 meta 表留痕，可重复执行（幂等）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[len(xs) // 2]


def scan(con, limit_dates=260):
    """返回 [{date, ratio, factor, rows, turn_zero_share}]，仅列出疑似量纲错乱的交易日。"""
    dates = [r[0] for r in con.execute(
        "SELECT date, COUNT(*) n FROM bars GROUP BY date HAVING n>500 ORDER BY date").fetchall()][-limit_dates:]
    if len(dates) < 5:
        return []
    dset = {d: i for i, d in enumerate(dates)}
    # 一次性载入 (code,date)->vol，避免逐股查询
    vols = {}
    turn0 = {}
    total = {}
    for code, date, vol, turn in con.execute(
            "SELECT code,date,vol,turn FROM bars WHERE date>=?", (dates[0],)):
        if date not in dset:
            continue
        vols.setdefault(code, {})[date] = vol
        total[date] = total.get(date, 0) + 1
        if not turn:
            turn0[date] = turn0.get(date, 0) + 1

    out = []
    for i, d in enumerate(dates):
        # 邻近交易日（前2后2，跳过自身）
        nb = [dates[j] for j in range(max(0, i - 2), min(len(dates), i + 3)) if j != i]
        if not nb:
            continue
        ratios = []
        for code, m in vols.items():
            v = m.get(d)
            if not v:
                continue
            base = _median([m.get(x) for x in nb])
            if base and base > 0:
                ratios.append(v / base)
        if len(ratios) < 200:
            continue
        r = _median(ratios)
        if r is None:
            continue
        factor = None
        if 30 <= r <= 300:
            factor = 100.0
        elif 300 < r <= 3000:
            factor = 1000.0
        if factor:
            out.append({
                "date": d, "ratio": round(r, 2), "factor": factor,
                "rows": total.get(d, 0),
                "turn_zero_share": round(turn0.get(d, 0) / max(1, total.get(d, 0)), 3),
            })
    return out


def repair(con, apply=True, verbose=True):
    """检测并（可选）修复。返回修复记录列表。"""
    bad = scan(con)
    done = []
    for b in bad:
        if verbose:
            print("[data_guard] 发现量纲错乱 %s：全市场量比中位 %.1f，判定放大 %.0f 倍，%d 行%s"
                  % (b["date"], b["ratio"], b["factor"], b["rows"],
                     "（turn 全为0，疑似备用源）" if b["turn_zero_share"] > 0.9 else ""))
        if apply:
            con.execute("UPDATE bars SET vol=vol/?, amount=amount/? WHERE date=?",
                        (b["factor"], b["factor"], b["date"]))
            hist = store.meta_get(con, "data_guard_repairs", []) or []
            hist.append({"date": b["date"], "factor": b["factor"], "ratio": b["ratio"]})
            store.meta_set(con, "data_guard_repairs", hist[-50:])
            con.commit()
            if verbose:
                print("[data_guard] 已修复 %s（vol/amount 同步 ÷%.0f）" % (b["date"], b["factor"]))
        done.append(b)
    if verbose and not bad:
        print("[data_guard] 量纲体检通过，未发现异常交易日")
    return done


def health_report(con):
    """轻量体检报告，供构建日志/站点展示。"""
    dates = [r[0] for r in con.execute(
        "SELECT date, COUNT(*) n FROM bars GROUP BY date HAVING n>500 ORDER BY date").fetchall()]
    rep = {"trade_days": len(dates), "last_date": dates[-1] if dates else None,
           "issues": []}
    if not dates:
        rep["issues"].append("日K库为空")
        return rep
    # 覆盖度
    n_last = con.execute("SELECT COUNT(*) FROM bars WHERE date=?", (dates[-1],)).fetchone()[0]
    rep["last_day_rows"] = n_last
    if n_last < 4000:
        rep["issues"].append("最新交易日仅 %d 只股票，覆盖不足" % n_last)
    # 量纲
    bad = scan(con)
    if bad:
        rep["issues"].append("量纲错乱交易日：" + ",".join(b["date"] for b in bad))
    rep["scale_anomalies"] = bad
    # 价格异常
    nbad = con.execute(
        "SELECT COUNT(*) FROM bars WHERE date=? AND (close IS NULL OR close<=0 OR high<low)",
        (dates[-1],)).fetchone()[0]
    if nbad:
        rep["issues"].append("最新交易日 %d 行价格异常" % nbad)
    rep["ok"] = not rep["issues"]
    return rep


if __name__ == "__main__":
    con = store.connect()
    apply = "--apply" in sys.argv
    print("=== 数据完整性体检 ===")
    r = health_report(con)
    print("交易日数 %s，最新 %s（%s 行）" % (r["trade_days"], r["last_date"], r.get("last_day_rows")))
    if r["issues"]:
        for i in r["issues"]:
            print("  ! " + i)
    else:
        print("  全部通过")
    if apply:
        print("\n=== 执行修复 ===")
        repair(con, apply=True)
    else:
        print("\n（只读模式；加 --apply 执行修复）")
        repair(con, apply=False)
