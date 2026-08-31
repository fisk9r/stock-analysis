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


# 全市场日成交额的绝对合理区间（元）。
# 上限：A 股历史峰值约 3.5 万亿（2024-10-08），留一倍余量取 8 万亿；
# 下限：5500+ 只个股的地量也不该低于 8000 亿。
# 为什么不用「邻日中位数比值」：2026-08 实测 07-27~08-21 连续 20 个交易日
# 总额被放大 ~17 倍——脏日占多数时中位数本身就是脏的，会把正常日误报成
# 「异常低」、脏日漏报。绝对阈值不受污染面影响。
AMOUNT_HIGH = 8e12
AMOUNT_LOW = 0.8e12


def amount_jump_scan(con, limit_dates=260):
    """逐日全市场成交额合计越界检测——抓「总额跳变/量纲错乱」型脏数据。

    scan() 按个股 vol 邻近比值中位判定，amount 单独错乱（vol 正常）会漏网：
    2026-08 实测 07-27~08-21 与 08-27 全市场总额被放大 ~17 倍（≈17% 个股
    amount×100，疑似备用源以「分」计价）。真实市场总额不可能越出绝对区间。"""
    rows = con.execute(
        "SELECT date, SUM(amount) FROM bars GROUP BY date HAVING COUNT(*)>500 ORDER BY date"
    ).fetchall()[-limit_dates:]
    out = []
    for d, a in rows:
        a = a or 0
        if a >= AMOUNT_HIGH or (a > 0 and a <= AMOUNT_LOW):
            out.append({"date": d, "type": "amount_jump",
                        "amount_ratio": round(a / 2e12, 2),
                        "amount_total": round(a, 2)})
    return out


def repair_pair_units(con, apply=True, verbose=True):
    """逐格修复「vol=股 + amount=分」×100 配对错乱（2026-08-31 定性）。

    病灶特征：同一 (股票, 交易日) 的 vol 与 amount 同时 ×100——备用源以
    「股/分」计价而主流约定是「手/元」。两字段同倍放大 → amount/vol 恒定、
    隐含股价恒正确、按股邻日比值也恒 1（错乱是连续多日的粘性模式），
    任何相对检测全部失灵；只有日总额会爆表（2025-08~2026-08 期间 5~27x）。

    绝对锚 = 流通股本 F（stocks.float_mv/close，日间稳定）：
      健康格：vol(手) = F×turn%×1e-2 → q = vol/F ≈ turn×1e-4 ≤ 0.01（turn≤100%）
      错乱格：vol(股) = F×turn%      → q = vol/F ≈ turn/100，turn≥1% 即 q>0.01
    判据 q>0.01 等价于「按手解读意味着换手>100%」——不可能，误报率≈0。
    修复：vol、amount 同 ÷100（幂等，修复后 q 缩小 100 倍不会再命中）。"""
    # 每股流通股本 F = float_mv / 最新收盘价
    F = {}
    for code, fmv, close in con.execute(
            "SELECT s.code, s.float_mv, b.close FROM stocks s "
            "JOIN bars b ON b.code = s.code "
            "WHERE s.float_mv>0 AND b.close>0 "
            "AND b.date = (SELECT MAX(date) FROM bars WHERE code=s.code)"):
        F[code] = fmv / close
    # 兜底：stocks 表没有的，用有 turn 的日反推 F 的中位数
    missing = [c for c, in con.execute(
        "SELECT DISTINCT code FROM bars WHERE vol>0") if c not in F]
    for code in missing:
        vals = [v * 100.0 / (t / 100.0) for v, t in con.execute(
            "SELECT vol, turn FROM bars WHERE code=? AND vol>0 AND turn>0", (code,))]
        if vals:
            F[code] = _median(vals)

    ups = []
    for code, date, v, a in con.execute(
            "SELECT code, date, vol, amount FROM bars WHERE vol>0 AND amount>0"):
        f = F.get(code)
        if not f or f <= 0:
            continue
        q = v / f
        if 0.01 < q < 100.0:
            ups.append((v / 100.0, a / 100.0, code, date))
    if not ups:
        if verbose:
            print("[data_guard] 配对单位体检通过（0 格错乱）")
        return []
    if verbose:
        codes = len(set(u[2] for u in ups))
        dates = len(set(u[3] for u in ups))
        print("[data_guard] 发现 %d 格 vol+amount ×100 配对错乱（%d 只股票 / %d 个交易日）"
              % (len(ups), codes, dates))
    if apply:
        con.executemany("UPDATE bars SET vol=?, amount=? WHERE code=? AND date=?", ups)
        con.commit()
        still = amount_jump_scan(con)
        if verbose:
            print("[data_guard] 修复 %d 格；复扫后总额越界日：%s"
                  % (len(ups), ",".join(b["date"] for b in still) if still else "无"))
    return ups


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
    # 总额跳变（amount 单独错乱、vol 正常时 scan 漏网）
    aj = amount_jump_scan(con)
    if aj:
        rep["issues"].append("成交额跳变交易日：" +
                             ",".join("%s(%.1fx)" % (b["date"], b["amount_ratio"]) for b in aj[:10]))
    rep["amount_anomalies"] = aj
    # 价格异常
    nbad = con.execute(
        "SELECT COUNT(*) FROM bars WHERE date=? AND (close IS NULL OR close<=0 OR high<low)",
        (dates[-1],)).fetchone()[0]
    if nbad:
        rep["issues"].append("最新交易日 %d 行价格异常" % nbad)
    rep["ok"] = not rep["issues"]
    return rep


def integrity_report(con):
    """数据完整性自检报告（构建期调用，仅告警不阻断）。

    归一化 health_report 的输出，供 build.py / 站点展示使用。
    返回 {ok, warnings:[...], trade_days, last_date, last_day_rows, scale_anomalies}。"""
    r = health_report(con)
    return {
        "ok": r.get("ok", False),
        "warnings": list(r.get("issues") or []),
        "trade_days": r.get("trade_days"),
        "last_date": r.get("last_date"),
        "last_day_rows": r.get("last_day_rows"),
        "scale_anomalies": r.get("scale_anomalies") or [],
        "amount_anomalies": r.get("amount_anomalies") or [],
    }


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
