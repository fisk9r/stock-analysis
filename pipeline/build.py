# -*- coding: utf-8 -*-
"""构建层：跑分析引擎 -> 生成前端 dist/data.js"""
import glob
import hashlib
import json
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
import store
import em_api
import ai_judge
import notifier
import yaogu
import bull
import strategies
import candles
import chips
import coldwave
import gapscan
import dryvol
import newhigh
import maglue
import trendsword
import stylereg
import tailraid
import watchlist
import recperf
import patsim
import lhbseats
import riskcal
import blocktrade
import margin
import etfflow
import holdings
import data_guard
import seats
import theme
import signals
import chanlun
import signal_backtest
import zones

ROOT = store.ROOT
DIST = os.path.join(ROOT, "dist")
ARCHIVE = os.path.join(ROOT, "archive")


_BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _bj_now():
    """北京时间 naive datetime（与 notifier 一致；CI runner 为 UTC，必须用北京时间）。"""
    return datetime.datetime.now(_BJ_TZ).replace(tzinfo=None)


def log(*a):
    print("[build]", *a, flush=True)


# ---- 模块导出自检（2026-08-28 事故防呆）----
# 曾发生：本地 store.py 新增了 trend_track_* / watch_first_seen 四个函数与两张表，
# 但文件从未推送 → CI 每次构建都在 except 里静默降级（只留一行「不影响主流程」），
# 线上 trend 长期缺 is_new/verdict。这里在启动时显式核对关键导出，
# 缺失即打 ⚠⚠ 严重标记（可直接 grep CI 日志发现漏推）。
_REQUIRED_EXPORTS = {
    "store": ("trend_track_states", "trend_track_upsert", "trend_track_drop",
              "watch_first_seen"),
    "engine": ("screen_uptrend", "trend_verdict", "institution_evidence",
               "sector_day_forecast", "classify_trend_state"),
    "zones": ("band_levels", "scan"),
    "watchreco": ("distill", "lines"),
}


def selfcheck_exports():
    miss = []
    for mod, names in _REQUIRED_EXPORTS.items():
        m = sys.modules.get(mod)
        if m is None:
            continue
        for n in names:
            if not hasattr(m, n):
                miss.append("%s.%s" % (mod, n))
    if miss:
        log("  ⚠⚠ 严重：关键函数缺失 %s（本地与远端不同步？先跑 tools/diff_remote.py）" % miss)
    return miss


def pick_date(u, override=None):
    """选定分析日：默认最后一个已收盘交易日"""
    if override:
        return override
    if not u.dates:
        raise RuntimeError("行情库为空，请先运行 fetch.py")
    last = u.dates[-1]
    today = _bj_now().strftime("%Y-%m-%d")
    now = _bj_now().strftime("%H%M")
    if last == today and now < "1505":
        return u.dates[-2] if len(u.dates) >= 2 else last
    return last


def load_snapshot(date):
    """载入盘后快照（涨停池封单/首封时间等增量字段）"""
    p = os.path.join(ARCHIVE, "snapshot_%s.json" % date.replace("-", ""))
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f), True
    files = sorted(glob.glob(os.path.join(ARCHIVE, "snapshot_*.json")))
    if files:
        with open(files[-1], encoding="utf-8") as f:
            return json.load(f), False
    return {}, False


def load_news():
    p = os.path.join(ROOT, "news.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"items": [], "summary": "", "updated": ""}


def load_global_market(con, date):
    """抓取外围主要指数（美股/日股/韩股）；优先实时，失败回退本地缓存。"""
    fresh = []
    try:
        fresh = em_api.global_index_snapshot()
    except Exception as e:
        log("  外围市场抓取失败，回退缓存：%r" % e)
    if fresh:
        fa = time.strftime("%Y-%m-%d %H:%M:%S")
        for x in fresh:
            store.upsert_global(con, x["region"], x["code"], x["name"], x["price"], x["pct"], fa)
        con.commit()
        rows = fresh
    else:
        rows = [{"region": r[0], "code": r[1], "name": r[2], "price": r[3], "pct": r[4]}
                for r in store.global_rows(con)]
    gm = engine.global_market(rows)
    gm["indices"] = rows
    return gm


def _g(d, *path, default=None):
    """安全取值：_g(data,'market','sentiment','score')"""
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def build_narrative(data):
    """离线「类AI解读」：用统计引擎输出拼装自然语言复盘（无需模型，零依赖）。"""
    m = data.get("meta", {})
    date = m.get("date", "")
    sent = data.get("market", {}).get("sentiment", {}) or {}
    cyc = data.get("market", {}).get("cycle", {}) or {}
    stats = data.get("streak_stats", {}) or {}
    lus = data.get("limit_ups", []) or []
    inds = data.get("sectors", {}).get("industry", []) or []
    cons = data.get("sectors", {}).get("concept", []) or []
    risks = data.get("break_risk", []) or []
    demons = data.get("demons", []) or []
    rec = data.get("recommend", {}) or {}
    auction = data.get("auction", {}) or {}
    asum = auction.get("summary", {}) or {}

    score = sent.get("score")
    label = sent.get("label") or cyc.get("phase") or "—"
    phase = cyc.get("phase") or "—"
    cyc_desc = (cyc.get("desc") or "").split("；")[0].split(";")[0]
    em = data.get("market", {}).get("emotion", {}) or {}
    n_zt = len(lus)
    max_streak = max([r.get("streak", 0) for r in lus], default=0)
    promote = em.get("promote_rate")
    seal = sent.get("seal_rate")
    top_sec = inds[0] if inds else (cons[0] if cons else None)
    sec_txt = "—"
    if top_sec:
        sec_txt = "「%s」" % top_sec.get("name", "?")
        extra = []
        if top_sec.get("strength") is not None:
            extra.append("强度 %.1f" % top_sec["strength"])
        if top_sec.get("zt"):
            extra.append("涨停 %d 只" % top_sec["zt"])
        if top_sec.get("lb"):
            extra.append("连板高度 %d 板" % top_sec["lb"])
        if extra:
            sec_txt += "（" + "，".join(extra) + "）"
    top_risk = sorted(risks, key=lambda x: -(x.get("p_break") or 0))[:1]
    risk_txt = "—"
    if top_risk:
        r = top_risk[0]
        risk_txt = "%s（%d 板，断板概率 %.0f%%）" % (r.get("name", "?"), r.get("streak", 0), (r.get("p_break") or 0))
    top_demon = demons[0] if demons else None
    demon_txt = "—"
    if top_demon:
        _sims = top_demon.get("similar") or []
        _sim = (_sims[0].get("sim") if isinstance(_sims, list) and _sims else top_demon.get("sim")) or 0
        demon_txt = "%s（相似度 %.0f%%）" % (top_demon.get("name", "?"), _sim)
    position = rec.get("position") or "—"
    env_k = rec.get("env_k")
    core = (rec.get("core") or [])
    core_txt = (core[0].get("name") if core else "—")

    bullets = []
    bullets.append("情绪温度计 %.1f 分，处于「%s」；当前市场周期定位为「%s」%s。"
                   % (score if score is not None else 0, label, phase,
                      "（" + cyc_desc + "）" if cyc_desc else ""))
    bullets.append("当日涨停 %d 只，最高 %d 连板；连板晋级率 %s，封板率 %s。"
                   % (n_zt, max_streak,
                      ("%.0f%%" % promote) if promote is not None else "—",
                      ("%.0f%%" % seal) if seal is not None else "—"))
    bullets.append("最强主线板块为 %s；题材维度%s。"
                   % (sec_txt, "可用" if cons else "暂缺（重跑 fetch.py 可补全）"))
    if asum:
        auction_read = ("资金竞价阶段整体偏积极，弱转强多于强转弱，低位承接有力"
                        if (asum.get("weak_strong", 0) >= asum.get("strong_weak", 0))
                        else "竞价阶段分歧加大，强转弱多于弱转强，高位需防兑现")
        vol_suffix = ("；竞价量能异动 %d 只（风险预警 %d 只，爆量高开低走疑似派发）"
                      % (asum.get("vol_anomaly", 0), asum.get("vol_warn", 0))) if asum.get("vol_anomaly") else ""
        bullets.append("竞价定调：涨停股平均高开 %.2f%%，一字板 %d 只、弱转强 %d 只、强转弱 %d 只；%s%s。"
                       % (asum.get("avg_open_pct", 0), asum.get("yizi", 0),
                          asum.get("weak_strong", 0), asum.get("strong_weak", 0),
                          auction_read, vol_suffix))
    bullets.append("高位断板风险最高：%s，次日需重点警惕分歧。" % risk_txt)
    bullets.append("妖股形态线索：扫描到 %d 只具备历史妖股特征的标的，最值得关注的是 %s。"
                   % (len(demons), demon_txt))
    bullets.append("次日策略：建议仓位 %s，环境系数 %s；核心观察标的 %s。"
                   % (position, ("%.2f" % env_k) if env_k is not None else "—", core_txt))

    outlook = "综合情绪与周期，%s宜%s；高位标的注意兑现节奏，低位主线可择优跟随。" % (
        date,
        "控仓防守、去弱留强" if (score or 0) >= 75 else
        "积极试错、聚焦核心" if (score or 0) >= 55 else "等待冰点转折、控制回撤")

    return {
        "headline": "%s 盘后智能复盘" % date,
        "bullets": bullets,
        "outlook": outlook,
        "generated_at": m.get("generated_at", ""),
    }


def compute_money_flow():
    """主力资金流向 + 北向资金（东财实时，CI 端取数）。失败返回 None 不影响主流程。"""
    try:
        # 行业/概念板块主力净流入（f62 主力净流入额/元，f184 净流入率%，f66 超大单，f72 大单）
        rows, _ = em_api.clist_paged("m:90+t:2", "f12,f14,f62,f184,f66,f72", max_pages=6, fid="f62")
        boards = []
        for r in rows:
            net = r.get("f62")
            if net is None or not r.get("f14"):
                continue
            boards.append({"code": r.get("f12"), "name": r.get("f14"),
                           "net": round((net or 0) / 1e8, 2),       # 元 -> 亿
                           "rate": round(r.get("f184") or 0, 2),
                           "xl": round((r.get("f66") or 0) / 1e8, 2),  # 超大单净流入(亿)
                           "l": round((r.get("f72") or 0) / 1e8, 2)})  # 大单净流入(亿)
        boards = [b for b in boards if b["name"]]
        boards_sorted = sorted(boards, key=lambda x: x["net"], reverse=True)
        top_in = boards_sorted[:10]
        top_out = boards_sorted[-5:][::-1]
        net_in = sum(1 for b in boards if b["net"] > 0)
        net_out = sum(1 for b in boards if b["net"] < 0)
        total_net = round(sum(b["net"] for b in boards), 1)
        # 北向资金（沪深港通）：东财 kamt 口径多次调整，沪股通/深股通 dayNetAmtIn 常为 0 → 不可用则标记 None
        north = None
        try:
            d = em_api.push2_json('/api/qt/kamt/get?fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58&_=1')
            dd = (d or {}).get("data") or {}
            sh = (dd.get("hk2sh") or {}).get("dayNetAmtIn")
            sz = (dd.get("hk2sz") or {}).get("dayNetAmtIn")
            if sh is not None and sz is not None and (sh != 0 or sz != 0):
                north = {"sh": round(sh / 1e4, 1), "sz": round(sz / 1e4, 1),  # 万元 -> 亿
                         "total": round((sh + sz) / 1e4, 1)}
        except Exception:
            north = None
        return {"boards_in": top_in, "boards_out": top_out,
                "net_in_boards": net_in, "net_out_boards": net_out,
                "total_main_net": total_net, "north": north}
    except Exception as e:
        print("[money] 资金流向获取失败：%r" % e)
        return None


def run(date_override=None, dedup_close=False):
    t0 = time.time()
    selfcheck_exports()
    con = store.connect()
    # 数据完整性自检：修复可能的量纲异常（如某日 vol/amount 被放大 ~100×），幂等。
    try:
        nfix = data_guard.repair(con, apply=True, verbose=False)
        if nfix:
            log("  数据修复：修正 %d 处量纲异常" % nfix)
    except Exception as e:
        log("  数据修复跳过（不影响主流程）：%r" % e)
    # 全零 K 线自愈清洗（2026-08-29：停牌股 o/h/l/c 全 0 曾致 maglue 除零/
    # trendsword 越界/完整性告警；upsert_bars 源头已拒收，这里清历史存量）
    try:
        _nz = store.purge_zero_bars(con)
        if _nz:
            log("  清洗全零K线 %d 行" % _nz)
    except Exception as e:
        log("  全零K线清洗跳过（不影响主流程）：%r" % e)
    log("载入行情库 ...")
    u = engine.Universe(con, days=270)
    log("覆盖 %d 只个股 / %d 个交易日" % (len(u.bars), len(u.dates)))
    date = pick_date(u, date_override)
    prev = u.prev_date(date)
    snap, same_day = load_snapshot(date)
    log("分析交易日 = %s（上一交易日 %s），盘后快照%s" % (date, prev, "命中当日" if same_day else "非当日·降级"))

    code2boards = store.code_boards(con)
    log("板块归属映射 %d 只" % len(code2boards))

    log("统计连板晋级率 ...")
    stats = engine.streak_statistics(u, lookback=len(u.dates))

    log("构建当日涨停画像 ...")
    lus = engine.build_limit_ups(u, date, snap, code2boards, same_day)
    log("  涨停 %d 只，最高 %d 连板" % (len(lus), max([r["streak"] for r in lus], default=0)))

    # 负反馈闭环特征：当日量/5日均量，供高位否决器判「放量接力 vs 缩量惜售」
    try:
        import recveto as recveto_mod
        n_vr = 0
        for r in lus:
            bs_prev = [b for b in (u.bars.get(r["code"]) or []) if b["d"] < date]
            vr = recveto_mod.day_vol_ratio(u.bar(r["code"], date) or {}, bs_prev)
            r["day_vol_ratio"] = vr
            if vr is not None:
                n_vr += 1
        log("  否决器量能特征注入 %d/%d 只" % (n_vr, len(lus)))
    except Exception as e:
        log("  否决器特征注入失败（不影响主流程）：%r" % e)

    log("竞价定调分析 ...")
    auction = engine.auction_profile(u, date, lus)
    ladder_hist = engine.ladder_history(u, date, 5)
    rotation = engine.sector_rotation(u, date, code2boards, 12, 5)
    # 板块接力 / 主线切换：检测旧主线断板退潮 → 新方向接力（如 2026-03 电力→医药）
    try:
        relay = engine.sector_relay(u, date, code2boards, 60)
        if relay.get("available"):
            log("  板块接力：%s | 退潮%s | 接力【%s】" % (
                relay["phase"],
                relay["broken"]["name"] if relay.get("broken") else "无",
                "、".join(x["name"] for x in relay.get("relay", []))))
    except Exception as e:
        log("  板块接力检测失败（不影响主流程）：%r" % e)
        relay = {"available": False}

    # 恐慌 / 崩盘检测
    try:
        panic = engine.panic_scan(u, date, code2boards)
        log("  恐慌检测：%s（跌停 %d / 大面 %d / 昨日涨停收绿 %.0f%%）" % (
            panic["level"], panic["dt_count"], panic["bigface_count"], panic["yest_green"] or 0))
    except Exception as e:
        log("  恐慌检测失败（不影响主流程）：%r" % e)
        panic = None
    asum = auction.get("summary", {})
    log("  竞价：平均高开 %.2f%% · 一字板 %d · 弱转强 %d · 强转弱 %d"
        % (asum.get("avg_open_pct", 0), asum.get("yizi", 0), asum.get("weak_strong", 0), asum.get("strong_weak", 0)))

    log("主力/北向资金流向 ...")
    try:
        money = compute_money_flow()
        if money:
            n = money.get("north")
            log("  主力净流入板块 %d / 净流出 %d；全市场主力净流入约 %.1f 亿；北向=%s"
                % (money["net_in_boards"], money["net_out_boards"], money["total_main_net"],
                   ("沪%s/深%s/合计%s亿" % (n["sh"], n["sz"], n["total"])) if n else "数据源不可用"))
        else:
            log("  资金流向获取失败，跳过")
    except Exception as e:
        log("  资金流向异常（不影响主流程）：%r" % e)
        money = None

    log("板块热力 ...")
    inds, cons = engine.sector_heat(lus, snap, u, date)
    sec_by_name = {s["name"]: s for s in inds}

    log("市场情绪与周期 ...")
    series = engine.emotion_series(u, 30)
    sent = engine.sentiment_score(u, date, series, snap, same_day)
    cyc = engine.cycle_phase([x for x in series if x["date"] <= date])

    log("市场热度研判（标杆趋势股交易额度）...")
    bench_heat = engine.benchmark_heat(u, date)
    log("  标杆趋势股市场热度：%s（成交额/20日均量 %.2fx，%d/%d 仍多头）"
        % (bench_heat["level"], bench_heat["avg_amt_ratio"] or 0,
           int(round((bench_heat["share_trending"] or 0) * len(bench_heat["stocks"]))), len(bench_heat["stocks"])))

    log("炸板历史规律统计 ...")
    zhaban_stats = engine.zhaban_statistics(u, lookback=120)
    if zhaban_stats:
        log("  炸板样本 %d 只，次日平均收盘 %s%%（收绿率 %s%%）"
            % (zhaban_stats.get("samples", 0), zhaban_stats.get("avg_next_close"),
               zhaban_stats.get("green_rate")))
    else:
        log("  炸板样本不足，跳过规律统计")

    log("涨停形态分类与次日规律统计 ...")
    pattern_stats = engine.limit_up_pattern_stats(u, lookback=120)
    shape_order = ["一字板", "地天板", "T字板", "烂板", "换手板"]
    if pattern_stats:
        for shp in shape_order:
            if shp in pattern_stats:
                p = pattern_stats[shp]
                log("  %s：样本 %d，次日均收 %s%%（涨停率 %s%% / 收绿率 %s%%）"
                    % (shp, p["samples"], p["avg_next_close"], p["limitup_rate"], p["green_rate"]))
    else:
        log("  形态样本不足，跳过规律统计")
    pattern_today = {}
    for r in lus:
        shp = r.get("lu_shape")
        if shp:
            pattern_today[shp] = pattern_today.get(shp, 0) + 1

    log("断板概率模型 ...")
    risks = engine.break_risk(lus, stats, sent, sec_by_name, u, date, auction["items"])

    log("外围市场定调 ...")
    gm = load_global_market(con, date)

    log("历史连板热度研判 ...")
    hist_rows = store.rec_history_rows(con)
    picks_rows = store.rec_picks_all(con)
    bars_series = engine.recent_height_series(u, date, 20)
    regime = engine.compute_regime(hist_rows, picks_rows, bars_series)
    log("  历史研判：%s（因子 %.2f，数据源 %s，连降 %s 日）；推荐命中率 %s"
        % (regime.get("level"), regime.get("factor", 0), regime.get("src"),
           regime.get("declines"), regime.get("hit_rate")))

    log("挖掘历史妖股模板 ...")
    tpls = engine.mine_demon_templates(u)
    log("  模板 %d 个" % len(tpls))
    log("妖股形态相似度扫描 ...")
    demons = engine.demon_scan(u, date, lus, tpls, sec_by_name)

    log("生成当日推荐 ...")
    rec = engine.recommend(lus, risks, demons, inds, sent, cyc, stats, auction["items"], regime, relay)

    # 连板预期空间引擎（用户需求 2026-08-27）：挖掘「明天有机会买的连板票」，
    # 给买入/卖出区间、止损与预期高度（如 3板→预期5板）；高位票只标注不拦。
    # 2026-08-27 升级：环境动态修正（情绪/接力regime/梯队健康度三元组）。
    try:
        import ladderplan
        lp_stats = ladderplan.ladder_stats(u)
        try:
            _lw = engine.ladder_warn(u, date)
        except Exception:
            _lw = None
        rec["ladder_plans"] = ladderplan.scan(
            u, date, lus, lp_stats, topn=12,
            sent=sent, regime=regime, ladder_warn=_lw)
        _lp_coef, _lp_note = ladderplan.env_mod(
            sent=sent, regime=regime, ladder_warn=_lw)
        log("  连板计划 %d 只（3板桶 n=%d，环境系数 %.2f %s）" % (
            len(rec.get("ladder_plans") or []),
            (lp_stats.get(3) or {}).get("n", 0),
            _lp_coef, _lp_note or "无修正"))
    except Exception as e:
        log("  连板计划失败（不影响主流程）：%r" % e)
        rec["ladder_plans"] = []

    # 盘前策略：聚合 recommend 的仓位/策略 + 板块/接力/风险，供看板与盘前推送
    try:
        plan = engine.preopen_plan(rec, inds, relay, risks)
    except Exception as e:
        log("  盘前策略生成失败（不影响主流程）：%r" % e)
        plan = {}

    # 趋势向上选股（独立于连板体系，覆盖主升段趋势票）
    try:
        rec["trend"] = engine.screen_uptrend(u, date, code2boards, topn=12)
        log("  趋势向上筛选 %d 只" % len(rec.get("trend") or []))
    except Exception as e:
        log("  趋势向上筛选失败（不影响主流程）：%r" % e)
        rec["trend"] = []

    # 趋势票持久化 + 波段区间（核心增强）：标注历史/新推荐，避免每日随行情波动；
    # 给每只趋势票附加回踩买/反弹卖的波段价。
    try:
        cur_trend = rec.get("trend") or []
        for _p in cur_trend:
            _c = _p.get("code")
            _bs = [b for b in (u.bars.get(_c) or []) if b["d"] <= date]
            if len(_bs) >= 20:
                _bd = zones.band_levels(_bs)
                if _bd:
                    _p["buy_zone"] = _bd["buy_zone"]
                    _p["sell_zone"] = _bd["sell_zone"]
                    _p["stop"] = _bd["stop"]
                    _p["band_action"] = _bd["band_action"]
                    _p["advice"] = _bd["advice"]
        _states = store.trend_track_states(con) if con else {}
        _merged = {}
        for _p in cur_trend:
            _c = _p["code"]
            _st = _states.get(_c) or {}
            _p["is_new"] = _c not in _states
            _p["continued"] = False
            _p["first_seen"] = _st.get("first_seen", date)
            _p["times"] = _st.get("times", 0) + 1
            # 需求3：明确结论（买/卖/持有 + 具体价格 + 波段≤20交易日）
            try:
                _bs_v = [b for b in (u.bars.get(_c) or []) if b["d"] <= date]
                _vd = engine.trend_verdict(_bs_v, band=_p,
                                           first_seen=_p.get("first_seen"), date=date)
                if _vd:
                    _p["verdict"] = _vd
            except Exception:
                pass
            _merged[_c] = _p
        # 既往趋势票：若仍处上升趋势（MA20>MA60 且 价>MA20）则继续保留为「历史趋势」
        for _code, _stt in _states.items():
            if _code in _merged:
                continue
            _bs = [b for b in (u.bars.get(_code) or []) if b["d"] <= date]
            if len(_bs) < 40:
                continue
            _closes = [float(b["c"]) for b in _bs]
            _ma20 = sum(_closes[-20:]) / 20
            _ma60 = sum(_closes[-60:]) / 60 if len(_closes) >= 60 else 0
            if _ma20 and _ma60 and _ma20 > _ma60 and float(_bs[-1]["c"]) > _ma20:
                _ind = next((n for _, n, k in (code2boards.get(_code) or []) if k == "industry"), "—")
                _name = _stt.get("name") or (u.stocks.get(_code, {}) or {}).get("name") or _code
                # 延续票也补 trend_meta：_band_pick 按 trend_meta.band 配额挑选，
                # 缺失会被过滤掉导致「历史延续 0 只」（2026-08-29 实测 bug：19 只
                # 历史票 13 只符合延续条件却全部展示不出来）。
                _last5 = [b.get("pct") or 0 for b in _bs[-5:]]
                _avg5 = sum(_last5) / 5.0
                _ma5 = sum(_closes[-5:]) / 5.0
                _ma10 = sum(_closes[-10:]) / 10.0
                _align = _ma5 > _ma10 > _ma20
                if _avg5 >= 3.0:
                    _band = "主升强趋势"
                elif _avg5 >= 2.0:
                    _band = "稳健上行"
                else:
                    _band = "趋势平缓"
                _kscore = 0.0
                try:
                    from kronos_lite import annotate_bars as _kab2
                except Exception:
                    try:
                        from pipeline.kronos_lite import annotate_bars as _kab2
                    except Exception:
                        _kab2 = None
                if _kab2:
                    _kbars = [{"d": b.get("d"), "o": b.get("o"), "h": b.get("h"),
                               "l": b.get("l"), "c": b.get("c"), "v": b.get("v")}
                              for b in _bs[-30:]]
                    _kscore = _kab2(_kbars)
                _p = {"code": _code, "name": _name, "streak": 0, "industry": _ind,
                      "close": round(float(_bs[-1]["c"]), 2),
                      "float_mv": (u.stocks.get(_code, {}) or {}).get("float_mv"),
                      "turn": round(float(_bs[-1].get("turn") or 0), 2),
                      "quality": 0, "p_continue": 0, "demon": 0,
                      # 延续票无当日评分引擎打分，按 band 给基准分（band 顺序内
                      # 再以 kronos_score 微调排序），否则 score=0 永远垫底。
                      "score": {"主升强趋势": 62.0, "稳健上行": 55.0,
                                "趋势平缓": 48.0}.get(_band, 45.0) + _kscore * 0.1,
                      "worth_score": {"主升强趋势": 58.0, "稳健上行": 52.0,
                                      "趋势平缓": 45.0}.get(_band, 42.0) + _kscore * 0.1,
                      "trend_meta": {
                          "ma5": round(_ma5, 2), "ma10": round(_ma10, 2),
                          "ma20": round(_ma20, 2), "align": bool(_align),
                          "avg_daily": round(_avg5, 2), "band": _band,
                          "continued_hist": True,
                          "kronos_score": round(_kscore, 1),
                      },
                      "is_new": False, "continued": True,
                      "first_seen": _stt["first_seen"], "times": _stt["times"] + 1}
                _bd = zones.band_levels(_bs)
                if _bd:
                    _p["buy_zone"] = _bd["buy_zone"]; _p["sell_zone"] = _bd["sell_zone"]
                    _p["stop"] = _bd["stop"]; _p["band_action"] = _bd["band_action"]
                    _p["advice"] = _bd["advice"]
                try:
                    _vd = engine.trend_verdict(_bs, band=_bd or {},
                                               first_seen=_stt["first_seen"], date=date)
                    if _vd:
                        _p["verdict"] = _vd
                except Exception:
                    pass
                _merged[_code] = _p
        # 已破位的既往趋势票移出跟踪（不再挂失效标签）
        _broken = [c for c in _states if c not in _merged]
        if _broken:
            store.trend_track_drop(con, _broken)
        if con:
            store.trend_track_upsert(con, date, list(_merged.values()))
        # ── 分层配额展示（2026-08-28 用户反馈金牛化工型缓坡趋势看不到）──
        # 主升强趋势票天然占满前排，「趋势平缓」慢牛永远挤不进 topn。
        # 改为按 band 配额：强趋势 6 / 稳健 4 / 平缓 3 + 历史延续 7，各类型都有代表。
        _new_arr = [x for x in _merged.values() if x.get("is_new")]
        _hist_arr = [x for x in _merged.values() if not x.get("is_new")]

        def _band_pick(arr, bandname, k):
            sub = [x for x in arr if (x.get("trend_meta") or {}).get("band") == bandname]
            sub.sort(key=lambda x: -(x.get("score") or 0))
            return sub[:k]

        _show = (_band_pick(_new_arr, "主升强趋势", 6)
                 + _band_pick(_new_arr, "稳健上行", 4)
                 + _band_pick(_new_arr, "趋势平缓", 3))
        # 历史延续票同样按 band 配额（用户需求：加速型与缓坡慢牛都要有代表），
        # 否则强趋势票天然占满前排，缓坡慢牛永远看不到。
        _show += (_band_pick(_hist_arr, "主升强趋势", 4)
                  + _band_pick(_hist_arr, "稳健上行", 3)
                  + _band_pick(_hist_arr, "趋势平缓", 3))
        _seen = set(x["code"] for x in _show)
        _show += [x for x in sorted(_hist_arr, key=lambda x: -(x.get("score") or 0))
                  if x["code"] not in _seen][:2]
        _seen_codes = set()
        _trend_final = []
        for _x in _show:
            if _x["code"] in _seen_codes:
                continue
            _seen_codes.add(_x["code"])
            _trend_final.append(_x)
        rec["trend"] = _trend_final[:20]
        log("  趋势持久化：展示 %d 只（新 %d / 历史延续 %d）" % (
            len(rec["trend"]),
            sum(1 for x in rec["trend"] if x.get("is_new")),
            sum(1 for x in rec["trend"] if x.get("continued"))))
    except Exception as e:
        log("  趋势持久化失败（不影响主流程）：%r" % e)

    # 强动量 · 连板余波选股（接住『连板妖股基因、今天非涨停』掉缝里的票，
    # 如风范股份；与 screen_uptrend 的平滑趋势互补，两档并列呈现）
    try:
        rec["momentum"] = engine.screen_momentum(u, date, code2boards, topn=12)
        log("  强动量/连板余波筛选 %d 只" % len(rec.get("momentum") or []))
    except Exception as e:
        log("  强动量筛选失败（不影响主流程）：%r" % e)
        rec["momentum"] = []

    # 板块趋势推荐：把趋势票按行业聚类，找出「多只票悄悄走主升、却没几只涨停」的
    # 趋势抱团板块（与 sector_heat 按涨停家数排主线互补；如被动元件/医疗服务）
    try:
        rec["sector_trend"] = engine.sector_trend_recommend(u, date, code2boards, sectors=inds, topn=6)
        log("  板块趋势推荐 %d 个板块" % len(rec.get("sector_trend") or []))
        # 主线/龙头 → 个股映射（供个股级视图打标：是否属于主线、是否为主线龙头）
        mmap = {}
        for _s in (rec.get("sector_trend") or []):
            for _x in (_s.get("leads") or []):
                mmap[_x["code"]] = {"sector": _s["sector"],
                                    "is_mainline": _s.get("tier") == "主线",
                                    "is_leader": bool(_x.get("is_leader"))}
        rec["mainline_map"] = mmap
    except Exception as e:
        log("  板块趋势推荐失败（不影响主流程）：%r" % e)
        rec["sector_trend"] = []
        rec["mainline_map"] = {}

    # 阶梯
    ladder = {}
    # 短线情绪微观结构（首板/梯队断层/晋级率分档/炸板率/赚钱效应细分）
    try:
        micro = engine.microstructure(u, date, lus, snap, code2boards, same_day)
        log("  微观结构：首板 %d 只，梯队最高 %d 板，断层 %s"
            % (micro["first_board"]["count"], micro["max_lb"], micro["gap"] or "无"))
    except Exception as e:
        log("  微观结构计算失败（不影响主流程）：%r" % e)
        micro = {}
    # 近5日板块热度趋势 + 龙头谱系（题材持续性/退潮追踪）
    try:
        sth = engine.sector_trend_5d(u, date, code2boards)
        log("  板块趋势5日：头部 %d 个板块，主线谱系 %d 条"
            % (len(sth.get("trend", [])), len(sth.get("lineage", []))))
    except Exception as e:
        log("  板块趋势5日失败（不影响主流程）：%r" % e)
        sth = {"dates": [], "trend": [], "lineage": []}
    for r in lus:
        ladder.setdefault(str(r["streak"]), []).append(
            {"code": r["code"], "name": r["name"], "industry": r["industry"],
             "quality": r["quality"], "turn": r["turn"], "yizi": r["yizi"],
             "float_mv": r["float_mv"],
             "p_continue": next((x["p_continue"] for x in risks if x["code"] == r["code"]), None)})

    # ---- 趋势/动量票入池追踪（2026-08-29：此前仅连板票有 T+1 回测，趋势选股
    # 质量从无数据验证。必须在 engine.backtest 之前写入——回测读 rec_picks 全表，
    # 写在后面当日 17 条就赶不上（实测首日 by_tag 缺趋势/动量分组）。
    # tag 前缀「趋势·」/「动量·」区分通道；next_pct 由 backfill_rec_outcomes
    # 按 code+date 统一回填，天然兼容。INSERT OR REPLACE 幂等。----
    try:
        for it in (rec.get("trend") or []):
            store.upsert_rec_pick(con, date, it["code"], it["name"], 0, None,
                                  "趋势·%s" % ((it.get("trend_meta") or {}).get("band") or "入选"),
                                  quality=it.get("quality"), turn=it.get("turn"))
        for it in (rec.get("momentum") or []):
            store.upsert_rec_pick(con, date, it["code"], it["name"], 0, None,
                                  "动量·%s" % ((it.get("momentum_meta") or {}).get("band") or "入选"),
                                  quality=it.get("quality"), turn=it.get("turn"))
        con.commit()
    except Exception as e:
        log("  趋势/动量入池失败（不影响主流程）：%r" % e)

    # ---- 选股回测（基于历史真实推荐 + K线前向收益，零成本）----
    try:
        bt = engine.backtest(u, con)
        if bt:
            log("  回测：样本 %d，+1日胜率 %s%% / +3日 %s%% / +5日 %s%%"
                % (bt["total"], (bt["h1"] or {}).get("win", "-"),
                   (bt["h3"] or {}).get("win", "-"), (bt["h5"] or {}).get("win", "-")))
        else:
            log("  回测样本不足，跳过")
            bt = None
    except Exception as e:
        log("  回测失败（不影响主流程）：%r" % e)
        bt = None

    e_today = next((x for x in series if x["date"] == date), None) or {}
    idx = snap.get("index") or []

    # ---- 可视化数据（市场温度走势 + 板块涨停 TOP）----
    # series/emotion_series 已含每日 zt(涨停家数) 与 max_lb(连板高度)，直接取近 20 日。
    _viz_series = (series or [])[-20:]
    viz = {
        "temp": [{"d": e["date"][5:], "zt": e.get("zt", 0), "lb": e.get("max_lb", 0)} for e in _viz_series],
        "sector_zt": [{"name": s["name"], "zt": s.get("zt") or 0, "tier": s.get("tier")}
                      for s in sorted(inds, key=lambda x: -(x.get("zt") or 0))[:10]],
    }

    # 妖股潜力榜（实时涨停池驱动，与 demon_scan 的 K线基因互补）：写入 data 供登录看板展示
    yaogu_data = None
    try:
        yaogu_data = yaogu.live_report()
    except Exception as e:
        log("  妖股潜力榜生成失败（不影响主流程）：%r" % e)

    data = {
        "meta": {
            "date": date, "prev_date": prev,
            "generated_at": _bj_now().strftime("%Y-%m-%d %H:%M:%S"),
            "snapshot_same_day": same_day,
            "snapshot_at": snap.get("fetched_at"),
            "universe": len(u.bars), "trade_days": len(u.dates),
            "source": "东方财富公开行情接口（盘后重建）",
            "build_seconds": None,
        },
        "market": {
            "indexes": idx,
            "emotion": e_today,
            "sentiment": sent,
            "cycle": cyc,
            "series": series,
            "bench_heat": bench_heat,
            "zhaban_stats": zhaban_stats,
            "pattern_stats": pattern_stats,
            "pattern_today": pattern_today,
            "fundflow": (snap.get("fundflow") or [])[-30:],
        },
        "sectors": {"industry": inds[:30], "concept": cons},
        "limit_ups": lus,
        "ladder": ladder,
        "streak_stats": stats,
        "break_risk": risks,
        "auction": auction,
        "ladder_history": ladder_hist,
        "rotation": rotation,
        "sector_relay": relay,
        "panic": panic,
        "viz": viz,
        "preopen_plan": plan,
        "late_session": None,  # 数据 dict 组装完后由下方 late_session_plan 填充
        "demons": demons[:40],
        "demon_templates": [{"code": t["code"], "name": t["name"], "start": t["start"],
                             "gain": t["gain"], "max_streak": t["max_streak"],
                             "trigger": t["trigger"]} for t in tpls[:40]],
        "recommend": rec,
        "global_market": gm,
        "regime": regime,
        "news": load_news(),
        "micro": micro,
        "sector_trend_hist": sth,
        "money": money,
        "backtest": bt,
        "yaogu": yaogu_data,
    }

    # ---- 牛股雷达：多维度独立抓牛股信号（10 种探测器共振）----
    try:
        data["bull"] = bull.scan(u, date, con, code2boards, topn=12)
        log("  牛股雷达命中 %d 只" % len(data.get("bull") or []))
    except Exception as e:
        log("  牛股雷达失败（不影响主流程）：%r" % e)
        data["bull"] = []

    # ---- 经典策略库：开源选股策略移植（InStock 系 9 探测器，与牛股雷达互补）----
    try:
        data["strategies"] = strategies.scan(u, date, con, code2boards, topn=12)
        log("  经典策略命中 %d 只" % len(data.get("strategies") or []))
    except Exception as e:
        log("  经典策略失败（不影响主流程）：%r" % e)
        data["strategies"] = []

    # ---- 策略历史回测：近 25 个交易日逐日重放信号，统计次日/3日胜率 ----
    try:
        data["strategy_bt"] = strategies.backtest(u, days=25)
        ok = [s for s in (data.get("strategy_bt") or []) if not s.get("low")]
        log("  策略回测完成：%d 个策略有足够样本" % len(ok))
    except Exception as e:
        log("  策略回测失败（不影响主流程）：%r" % e)
        data["strategy_bt"] = []

    # ---- K线组合形态：12 种经典蜡烛图形态全市场识别 ----
    try:
        data["candles"] = candles.scan(u, date, limit_per_pattern=8)
        n_hit = len(data.get("candles", {}).get("hits") or [])
        log("  K线形态命中 %d 条（%d 类）" % (n_hit, len(data.get("candles", {}).get("stats") or [])))
    except Exception as e:
        log("  K线形态失败（不影响主流程）：%r" % e)
        data["candles"] = {"stats": [], "hits": []}

    # ---- 筹码分布：获利盘比例近似估计（换手半衰期加权） ----
    try:
        data["chips"] = chips.scan(u, date)
        log("  筹码获利盘：均值 %.1f%%（%d 只）" % (data["chips"]["avg"] * 100, data["chips"]["n"]))
    except Exception as e:
        log("  筹码分布失败（不影响主流程）：%r" % e)
        data["chips"] = {"n": 0, "avg": 0, "top_low": [], "top_high": []}

    # ---- 冷启修复节奏预判 + 冷后领涨风格轮动规律 ----
    try:
        data["cold"] = coldwave.analyze(u, date, code2boards, n=140)
        cw = data["cold"]
        if cw and cw.get("forecast"):
            log("  冷启预判：%s（%s）" % (cw["forecast"]["state"], cw["forecast"]["expect"]))
    except Exception as e:
        log("  冷启分析失败（不影响主流程）：%r" % e)
        data["cold"] = None

    # ---- 跳空缺口检测 + 回补规律 ----
    try:
        data["gaps"] = gapscan.scan(u, date)
        gp = data["gaps"]
        if gp and (gp.get("stats") or {}).get("n_total"):
            log("  缺口扫描：历史 %d 个，当前未回补 %d 个"
                % (gp["stats"]["n_total"], gp.get("open_n", 0)))
    except Exception as e:
        log("  缺口扫描失败（不影响主流程）：%r" % e)
        data["gaps"] = None

    # ---- 地量/缩量变盘窗口 ----
    try:
        data["dryvol"] = dryvol.analyze(u, date, n=260)
        dv = data["dryvol"]
        if dv and dv.get("today"):
            log("  地量扫描：额比 %.2f（%.0f%% 分位），连缩 %d 日"
                % (dv["today"].get("ratio") or 0, dv["today"].get("hp") or -1,
                   dv["today"].get("shrink_days") or 0))
    except Exception as e:
        log("  地量扫描失败（不影响主流程）：%r" % e)
        data["dryvol"] = None

    # ---- 52周新高新低广度 ----
    try:
        data["newhigh"] = newhigh.scan(u, date)
        nb = data["newhigh"]
        if nb and nb.get("today"):
            t = nb["today"]
            log("  52周广度：新高 %d vs 新低 %d（NH-NL 比 %+.2f）"
                % (t.get("nh", 0), t.get("nl", 0), t.get("ratio", 0)))
    except Exception as e:
        log("  52周广度失败（不影响主流程）：%r" % e)
        data["newhigh"] = None

    # ---- 均线粘合待变盘池 ----
    try:
        data["maglue"] = maglue.scan(u, date)
        gg = data["maglue"]
        if gg:
            log("  均线粘合：粘合池 %d 只，已现启动迹象 %d 只"
                % (gg.get("glue_n", 0), gg.get("launching_n", 0)))
    except Exception as e:
        log("  均线粘合失败（不影响主流程）：%r" % e)
        data["maglue"] = None

    # ---- 断头铡刀 / 出水芙蓉 ----
    try:
        data["trendsword"] = trendsword.scan(u, date)
        cf = data["trendsword"]
        if cf:
            log("  铡刀/芙蓉：今日命中 %d 条" % len(cf.get("hits") or []))
    except Exception as e:
        log("  铡刀/芙蓉失败（不影响主流程）：%r" % e)
        data["trendsword"] = None

    # ---- 市场风格判定（大小盘 / 抱团趋势）----
    try:
        data["stylereg"] = stylereg.scan(u, date)
        sty = data["stylereg"]
        if sty and sty.get("verdict"):
            log("  风格判定：%s%s" % (sty["verdict"].get("label"),
                                      ("（%s → %s）" % (stylereg.style_cn(sty["switch"]["from_style"]),
                                                       stylereg.style_cn(sty["switch"]["to_style"])))
                                      if sty.get("switch") else ""))
    except Exception as e:
        log("  风格判定失败（不影响主流程）：%r" % e)
        data["stylereg"] = None

    # ---- 尾盘偷袭/跳水（焦点池定向，需联网取腾讯分时；离线自动跳过）----
    try:
        data["tailraid"] = tailraid.scan(u, date)
        tr = data["tailraid"]
        if tr:
            log("  尾盘扫描：焦点池 %d 只，偷袭 %d / 跳水 %d"
                % (tr.get("scanned", 0), tr.get("raid_n", 0), tr.get("dump_n", 0)))
        else:
            log("  尾盘扫描：无网络或焦点池为空，跳过")
    except Exception as e:
        log("  尾盘扫描失败（不影响主流程）：%r" % e)
        data["tailraid"] = None

    # ---- 关注股雷达：notify.json watch + holdings.json watch==true ----
    try:
        data["watch"] = watchlist.scan(u, date, con=con)
        wl = data["watch"]
        if wl:
            log("  关注股雷达：%d 只，急讯 %d 条" % (wl.get("n", 0), wl.get("alert_n", 0)))
        else:
            log("  关注股雷达：关注池为空，跳过")
    except Exception as e:
        log("  关注股雷达失败（不影响主流程）：%r" % e)
        data["watch"] = None

    # 关注池清单（供前端「网页管理」读取/编辑）——仅 code/name，不含任何私密数据
    try:
        _wc, _wn, _wa = watchlist.load_watch_codes()
        data["watch_meta"] = [{"code": c, "name": _wn.get(c, "")} for c in _wc]
    except Exception:
        data["watch_meta"] = []

    # ---- 推荐池历史胜率曲线（纯本地，可验证）----
    try:
        data["recperf"] = recperf.build(con)
        rp = data["recperf"]
        if rp:
            log("  推荐胜率：回溯 %d 日，近30日盈利占比 %s%%" % (rp["n_days"], rp["recent30"]["win_rate"]))
    except Exception as e:
        log("  推荐胜率失败（不影响主流程）：%r" % e)
        data["recperf"] = None

    # ---- 推荐多维归因（2026-08-30：st=2 持续监控 + 特征列分桶 + 落袋挽回测算）----
    try:
        import recattr
        data["rec_attr"] = recattr.build(con)
        _ra = data["rec_attr"]
        if _ra:
            for ln in recattr.summary_lines(_ra)[:3]:
                log("  归因：%s" % ln)
    except Exception as e:
        log("  推荐归因失败（不影响主流程）：%r" % e)
        data["rec_attr"] = None

    # ---- 风格切换历史回测（纯本地）----
    try:
        data["style_switch"] = stylereg.switch_backtest(u)
        sb = data["style_switch"]
        if sb:
            log("  风格切换回测：历史 %d 次切换，后%d日上涨占比 %s%%" % (sb["n"], sb["look"], sb["up_rate"]))
    except Exception as e:
        log("  风格切换回测失败（不影响主流程）：%r" % e)
        data["style_switch"] = None

    # ---- K线相似形态检索（纯本地）----
    try:
        data["patsim"] = patsim.scan(u, date)
        ps = data["patsim"]
        if ps:
            log("  相似形态：焦点 %d 只，命中 %d 只可回溯" % (ps.get("focus_n", 0), len(ps.get("items", []))))
    except Exception as e:
        log("  相似形态失败（不影响主流程）：%r" % e)
        data["patsim"] = None

    # ---- 东财数据中心引擎（接口均已实证；失败自动跳过）----
    try:
        data["lhbseats"] = lhbseats.scan(date)
        if data["lhbseats"]:
            log("  龙虎榜席位：上榜 %d 只，净买TOP %s"
                % (data["lhbseats"]["n"],
                   (data["lhbseats"]["top"] or [{}])[0].get("name", "—")))
    except Exception as e:
        log("  龙虎榜席位失败（无网/解析异常，跳过）：%r" % e)
        data["lhbseats"] = None

    try:
        data["riskcal"] = riskcal.scan(date)
        if data["riskcal"]:
            log("  雷区日历：解禁 %d 笔 / 财报 %d 只（未来%d日）"
                % (len(data["riskcal"]["unlock_top"]), len(data["riskcal"]["fin_due"]), data["riskcal"]["horizon"]))
    except Exception as e:
        log("  雷区日历失败（无网/解析异常，跳过）：%r" % e)
        data["riskcal"] = None

    try:
        data["blocktrade"] = blocktrade.scan(date)
        if data["blocktrade"]:
            log("  大宗交易：%d 笔，折价≥5%% 的 %d 笔" % (data["blocktrade"]["n"], data["blocktrade"]["discount_n"]))
    except Exception as e:
        log("  大宗交易失败（无网/解析异常，跳过）：%r" % e)
        data["blocktrade"] = None

    try:
        data["margin"] = margin.scan(date)
        if data["margin"]:
            log("  两融余额：%.0f 亿（前日 %+.0f）" % (data["margin"]["latest_yi"], data["margin"]["delta_yi"]))
    except Exception as e:
        log("  两融失败（无网/解析异常，跳过）：%r" % e)
        data["margin"] = None

    try:
        data["etfflow"] = etfflow.scan(date)
        if data["etfflow"]:
            log("  ETF 资金流：净流入 %.1f 亿" % data["etfflow"]["total_net_yi"])
    except Exception as e:
        log("  ETF 资金流失败（无网/解析异常，跳过）：%r" % e)
        data["etfflow"] = None

    # ---- 引擎快照落库（供连续信号/席位画像积累历史）----
    try:
        # 签名为 save_snapshot(con, k, date, payload)，date 不可省
        if data.get("margin"):
            store.save_snapshot(con, "margin", date, data["margin"])
        if data.get("etfflow"):
            store.save_snapshot(con, "etfflow", date, data["etfflow"])
        if data.get("lhbseats"):
            store.save_snapshot(con, "lhbseats", date, data["lhbseats"])
        if data.get("riskcal"):
            store.save_snapshot(con, "riskcal", date, data["riskcal"])
        con.commit()
        log("  引擎快照已落库（margin/etfflow/lhbseats/riskcal）")
    except Exception as e:
        log("  引擎快照落库失败（不影响主流程）：%r" % e)

    # ---- 游资席位画像（东财席位明细，实证可用；失败跳过）----
    try:
        data["seats"] = seats.scan(date)
        if data["seats"]:
            store.upsert_seats(con, date, data["seats"]["hits"])
            stats = seats.win_rates(con) if con else {}
            data["seats"]["stats"] = stats
            # 2026-08-30 dept 粒度胜率：同标签内分化大（拉萨 10678762=46% vs 10428246=16.7%），
            # dept_stats 供 summary_lines 输出「回避营业部」精确到号。
            dept_stats = seats.win_rates_dept(con) if con else {}
            data["seats"]["dept_stats"] = dept_stats
            log("  游资席位：命中 %d 条知名席位动作（标签级 %d / dept 级 %d 过样本门槛）"
                % (data["seats"]["n_hits"], len(stats), len(dept_stats)))
    except Exception as e:
        log("  游资席位失败（无网/解析异常，跳过）：%r" % e)
        data["seats"] = None

    # ---- 机构/主力介入证据（用户需求：机构介入情况及时点名）----
    # 依赖 lhbseats/blocktrade/margin/money/seats，必须放在这些引擎之后。
    try:
        _src = {"lhbseats": data.get("lhbseats"), "blocktrade": data.get("blocktrade"),
                "margin": data.get("margin"), "money": data.get("money"),
                "seats": data.get("seats")}
        _hit_n = 0
        for _p in list(rec.get("trend") or []) + list(rec.get("momentum") or []):
            if _p.get("institution"):
                continue
            _ev = engine.institution_evidence(_p.get("code"), _src,
                                              industry=_p.get("industry"))
            if _ev["level"] != "无":
                _p["institution"] = _ev
                _hit_n += 1
                if _ev["level"] in ("强", "中") and _ev.get("action"):
                    _rs = _p.setdefault("reasons", [])
                    _tag = "【%s介入】%s（%s）" % (_ev["level"], _ev["action"],
                                                "、".join(_ev["tags"][:2]) or "—")
                    if _tag not in _rs:
                        _rs.insert(0, _tag)
        log("  机构介入证据：%d 只趋势/动量票命中" % _hit_n)
    except Exception as e:
        log("  机构介入证据失败（不影响主流程）：%r" % e)

    # ---- 板块当日涨跌预判（用户需求：盘前结合板块预测当日涨跌，给关注票操作说明）----
    try:
        data["sector_forecast"] = engine.sector_day_forecast(data)
        _sfc = data["sector_forecast"] or {}
        _mk = _sfc.get("__market__") or {}
        log("  板块当日预判：大盘%s(%d分) · 覆盖 %d 个板块"
            % (_mk.get("dir"), _mk.get("score", 50), max(0, len(_sfc) - 1)))
    except Exception as e:
        log("  板块当日预判失败（不影响主流程）：%r" % e)
        data["sector_forecast"] = None

    # ---- 题材主线识别（基于涨停股概念/行业聚类）----
    try:
        data["theme"] = theme.scan(date, data.get("limit_ups") or [])
        if data["theme"]:
            theme.persist(con, date, data["theme"])
            sig = theme.theme_signal(con)
            data["theme"]["signal"] = sig
            log("  题材主线：%s（%d 只贡献）" % (data["theme"]["main_theme"], int(data["theme"]["main_n"])))
    except Exception as e:
        log("  题材主线失败（不影响主流程）：%r" % e)
        data["theme"] = None

    # ---- 连续信号（两融/ETF/龙虎榜/席位重复扫货的历史硬信号）----
    try:
        data["signals"] = signals.compute_all(con)
        if data["signals"]:
            log("  连续信号：%d 类有效" % len(data["signals"]))
    except Exception as e:
        log("  连续信号失败（不影响主流程）：%r" % e)
        data["signals"] = None

    # ---- 综合最优解：融合连板/趋势/席位/题材/连续信号/区间，输出统一排序 Top20 ----
    try:
        rec["fused"] = engine.fuse_recommend(data)
        log("  综合最优解：融合 %d 只标的" % len(rec.get("fused") or []))
    except Exception as e:
        log("  综合最优解失败（不影响主流程）：%r" % e)
        rec["fused"] = []

    # ---- fused 快照落库（供 accuracy 归因「昨日 Top → 今日兑现」）----
    try:
        store.save_snapshot(con, "fused", date, rec.get("fused") or [])
        con.commit()
    except Exception as e:
        log("  fused 快照落库失败：%r" % e)

    # ---- 需求2：推荐准确率归因（昨日 Top5 次日未晋级 → 环节诊断）----
    try:
        import accuracy as _acc
        data["accuracy"] = _acc.build(u, date, con, topn=5)
        if data["accuracy"]:
            log("  准确率归因：Top%d 命中率 %s%%｜%s"
                % (data["accuracy"]["topn"], data["accuracy"]["hit_rate"],
                   ("失准 %d 只" % data["accuracy"]["n_miss"]) if data["accuracy"]["n_miss"] else "全兑现"))
    except Exception as e:
        log("  准确率归因失败（不影响主流程）：%r" % e)
        data["accuracy"] = None

    # ---- 缠论结构（对推荐池/涨停股跑笔-中枢-背驰-买卖点）----
    try:
        rec_codes = [it.get("code") for it in (data.get("recommend", {}).get("all") or [])]
        lu_codes = [it.get("code") for it in (data.get("limit_ups") or [])]
        cl_codes = list(dict.fromkeys(rec_codes + lu_codes))[:60]
        if cl_codes:
            data["chanlun"] = chanlun.scan(u, con, cl_codes, top_n=12)
            if data["chanlun"]:
                nb = len(data["chanlun"].get("buys") or [])
                log("  缠论：分析 %d 只，买点候选 %d 只"
                    % (data["chanlun"]["n_analyzed"], nb))
        else:
            data["chanlun"] = None
    except Exception as e:
        log("  缠论分析失败（不影响主流程）：%r" % e)
        data["chanlun"] = None

    # ---- 买卖区间与操作提示（关注池优先，其次推荐池头部；带持仓成本盈亏）----
    try:
        import watchlist as _wl
        w_codes, w_names, w_added = _wl.load_watch_codes()
        rec_codes_z = [it.get("code") for it in (data.get("recommend", {}).get("all") or [])]
        z_codes = list(dict.fromkeys((w_codes or []) + (rec_codes_z or [])))[:40]
        # 强势备选池（用于破位/停滞/割肉时的「更换建议」）：取推荐池全量，含价值分与续板概率
        z_replace = []
        try:
            for it in (data.get("recommend", {}).get("all") or []):
                z_replace.append({"code": it.get("code"), "name": it.get("name"),
                                  "worth_score": it.get("worth_score"),
                                  "p_continue": it.get("p_continue")})
        except Exception:
            pass
        # 持仓配置：成本映射 + 周期标注 + 建仓锚点（用于时间到期预警）
        z_costs, z_horizons, z_elapsed = {}, {}, {}
        try:
            poss = holdings.load_positions() or []
            # 确保 holdings_track 表存在，便于按首条记录取锚点（空表返回 None 不报错）
            try:
                con.executescript(holdings.TRACK_SCHEMA)
            except Exception:
                pass
            for p in poss:
                c = p.get("code")
                if not c:
                    continue
                if p.get("cost"):
                    z_costs[c] = p["cost"]
                if p.get("horizon") in ("短线", "中线", "长线"):
                    z_horizons[c] = p["horizon"]
                # 锚点：建仓日期优先；否则 holdings_track 首条日期
                anchor = p.get("date")
                if not anchor:
                    try:
                        r = con.execute(
                            "SELECT MIN(date) FROM holdings_track WHERE code=?", (c,)).fetchone()
                        anchor = r[0] if r else None
                    except Exception:
                        anchor = None
                if anchor:
                    try:
                        el = sum(1 for d in u.dates if anchor <= d <= date) - \
                             (1 if anchor in u.dates else 0)
                        if el > 0:
                            z_elapsed[c] = el
                    except Exception:
                        pass
        except Exception:
            pass
        data["zones"] = (zones.scan(u, date, z_codes, extra_names=w_names,
                                    costs=z_costs, horizons=z_horizons,
                                    elapsed_map=z_elapsed,
                                    replace_pool=z_replace or None,
                                    exclude_codes=set(z_codes)) if z_codes else None)
        if data["zones"]:
            log("  买卖区间：覆盖 %d 只，破位 %d / 加仓 %d / 逼近卖点 %d / 优化提示 %d"
                % (data["zones"]["n"],
                   len(data["zones"]["alerts"].get("sell") or []),
                   len(data["zones"]["alerts"].get("add") or []),
                   len(data["zones"]["alerts"].get("take_profit") or []),
                   len(data["zones"]["alerts"].get("rotate") or [])))
    except Exception as e:
        log("  买卖区间失败（不影响主流程）：%r" % e)
        data["zones"] = None

    # ---- 自选/持仓操作结论（2026-08-28 用户需求 P1/P4）：
    # 自选股也要进推荐体系、每只给「跟着做」动作（买/卖/加仓/持有）。
    # 从 zones.items 提炼归一化结论，挂 rec["watch_reco"] 供看板与推送。----
    try:
        import watchreco
        _wn = w_names if 'w_names' in locals() and isinstance(w_names, dict) else {}
        _zc = z_costs if 'z_costs' in locals() else {}
        rec["watch_reco"] = watchreco.distill(
            data.get("zones"), holding_codes=set(_zc.keys()), watch_names=_wn)
        _wr = rec["watch_reco"]
        # 补行业字段（zones 无行业信息）→ 供盘前「板块当日预判」关联到个股
        def _industry_of(_c):
            return next((n for _, n, k in (code2boards.get(_c) or []) if k == "industry"), "—")

        for _it in _wr.get("items") or []:
            _it["industry"] = _industry_of(_it.get("code"))
        log("  自选/持仓操作结论 %d 只（卖出 %d / 买入加仓 %d）"
            % (_wr["n"], _wr["sell_n"], _wr["buy_n"]))
    except Exception as e:
        log("  自选/持仓操作结论失败（不影响主流程）：%r" % e)
        rec["watch_reco"] = None


    # ---- 持股监测：预测未来 + 持续跟踪（无持仓配置则为空）----
    try:
        data["holdings"] = holdings.monitor(u, date, con, code2boards)
        hrep = data["holdings"]
        if hrep and hrep.get("items"):
            log("  持股监测 %d 只，预警 %d 条" % (len(hrep["items"]), len(hrep.get("alerts") or [])))
    except Exception as e:
        log("  持股监测失败（不影响主流程）：%r" % e)
        data["holdings"] = None

    # ============ 升级模块（一次性挂载，失败互不影响）============

    # ---- A4 情绪→总仓位建议：热度(标杆成交额) + 情绪分 取严 ----
    try:
        bh = engine.benchmark_heat(u, date)
        pa = engine.position_suggestion(bh.get("level"), (sent or {}).get("level"),
                                        bh.get("score"), (sent or {}).get("score"))
        data["position_advice"] = pa
        log("  总仓位建议：%d成（%s）· 热度%s/情绪%s"
            % (pa["suggest_pct"], pa["level"], pa["heat"], pa["sentiment"]))
    except Exception as e:
        log("  总仓位建议失败：%r" % e)
        data["position_advice"] = None

    # ---- B6 连板梯队断板预警 ----
    try:
        data["ladder_warn"] = engine.ladder_warn(u, date)
        lw = data["ladder_warn"]
        if lw and lw.get("warns"):
            log("  梯队预警[%s]：%s" % (lw["level"], "；".join(lw["warns"])))
    except Exception as e:
        log("  梯队预警失败：%r" % e)
        data["ladder_warn"] = None

    # ---- B8 板块轮动实操结论：主线 Top3 + 领涨票 ----
    try:
        if code2boards:
            data["sector_trade"] = engine.sector_trade(u, date, code2boards, topn=3)
            if data["sector_trade"]:
                log("  板块轮动：%s"
                    % "、".join(s["sector"] for s in data["sector_trade"]))
    except Exception as e:
        log("  板块轮动失败：%r" % e)
        data["sector_trade"] = None

    # ---- B5 龙虎榜席位可跟性排序：按历史胜率排可跟席位 ----
    # 注意：局部变量不可叫 seats——会遮蔽模块级 import seats，
    # 导致上游 seats.scan(date) 触发 UnboundLocalError（曾致游资席位引擎每次构建必失败）。
    try:
        _seat_data = data.get("seats")
        if _seat_data:
            stats = _seat_data.get("stats") or {}
            hits = _seat_data.get("hits") or []
            ranked = sorted(stats.items(), key=lambda kv: -kv[1].get("win_rate", 0))
            items = []
            for label, st in ranked:
                if st.get("win_rate", 0) >= 55 and st.get("n", 0) >= 8:
                    reps = [h for h in hits if h.get("label") == label][:3]
                    items.append({"label": label, "win_rate": st["win_rate"],
                                  "n": st["n"], "avg_pct": st.get("avg_pct"), "reps": reps})
            data["seat_follow"] = {"n": len(items), "items": items} if items else None
            if data["seat_follow"]:
                log("  可跟席位：%d 个（胜率≥55%%）" % len(items))
            # 2026-08-30 席位样本回填后数据够看胜率了：低胜率席位 = 回避信号。
            # 东财拉萨 88 日 89 样本胜率仅 30.3%/均值 -3.52%——散户集中营跟随是负期望，
            # 这类席位上榜的票要在网页与推送里明确标「回避」而不是只字不提。
            avoid = []
            for label, st in ranked:
                if st.get("win_rate", 0) < 40 and st.get("n", 0) >= 20:
                    reps = [h for h in hits if h.get("label") == label][:3]
                    avoid.append({"label": label, "win_rate": st["win_rate"],
                                  "n": st["n"], "avg_pct": st.get("avg_pct"), "reps": reps})
            data["seat_avoid"] = {"n": len(avoid), "items": avoid} if avoid else None
            if data["seat_avoid"]:
                log("  回避席位：%d 个（胜率<40%% 且样本≥20）" % len(avoid))
    except Exception as e:
        log("  可跟席位失败：%r" % e)
        data["seat_follow"] = None

    # ---- A1 触发式盯盘：波段区/持仓/关注累计 → 条件单命中 ----
    try:
        import alerts
        data["triggers"] = alerts.build_triggers(data, date)
        if data["triggers"] and data["triggers"]["n"]:
            log("  触发盯盘：%d 条命中" % data["triggers"]["n"])
    except Exception as e:
        log("  触发盯盘失败：%r" % e)
        data["triggers"] = {"date": date, "n": 0, "hits": []}

    # ---- C11 事件影响可操作评级：解禁→关注股雷达标注 ----
    try:
        if data.get("riskcal"):
            data["riskcal"] = riskcal.grade(data["riskcal"])
            rflags = riskcal.watch_flags(data["riskcal"])
            if rflags and data.get("watch"):
                for it in data["watch"]["items"]:
                    if it.get("code") in rflags:
                        it["risk_flag"] = rflags[it["code"]]
    except Exception as e:
        log("  事件评级标注失败：%r" % e)

    # ---- 持仓×策略/雷达联动：给持仓条目打上当日命中信号标签 ----
    try:
        hrep = data.get("holdings")
        if hrep and hrep.get("items"):
            sigmap = {}
            for src in ("strategies", "bull"):
                for it in (data.get(src) or []):
                    sigmap.setdefault(it.get("code"), []).extend(it.get("signals") or [])
            n_linked = 0
            for it in hrep["items"]:
                sg = sigmap.get(it.get("code"))
                if sg:
                    it["signals"] = sg[:3]
                    n_linked += 1
            if n_linked:
                log("  持仓联动：%d 只持仓命中策略/雷达信号" % n_linked)
    except Exception as e:
        log("  持仓联动失败（不影响主流程）：%r" % e)

    # ---- 龙虎榜·游资合力（盘后公开数据，无需密钥；失败则跳过，不影响主流程）----
    try:
        lhb = em_api.lhb_day_list(date)
        if lhb:
            data["lhb"] = lhb
            log("  龙虎榜 %d 只上榜（游资合力因子已纳入推送）" % len(lhb))
        else:
            data["lhb"] = None
            log("  龙虎榜为空（可能非交易日或接口限流），跳过")
    except Exception as e:
        data["lhb"] = None
        log("  龙虎榜抓取失败（不影响主流程）：%r" % e)

    # ---- 数据完整性自检（缺口/覆盖骤降/新股截断/复权异常，仅告警不阻断）----
    try:
        integ = data_guard.integrity_report(con)
        data["integrity"] = integ
        for w in (integ.get("warnings") or []):
            log("  ⚠ 数据完整性：%s" % w)
        if integ.get("ok"):
            log("  数据完整性体检通过")
    except Exception as e:
        data["integrity"] = {"ok": False, "warnings": ["自检异常：%r" % e]}
        log("  数据完整性自检异常（不影响主流程）：%r" % e)

    # ---- 尾盘决策通道（2026-08-29）：用当日全天数据对次日开盘做双确认预判 ----
    try:
        data["late_session"] = engine.late_session_plan(data)
        _ls = data.get("late_session") or {}
        log("  尾盘决策：次日关注 %d 只 / 走弱警示 %d 只"
            % (_ls.get("n_watch") or 0, len(_ls.get("exit_warn") or [])))
    except Exception as e:
        data["late_session"] = None
        log("  尾盘决策失败（不影响主流程）：%r" % e)

    # ---- 历史连板库落库：先回填前一日推荐的真实结局，再记录当日状态 ----
    try:
        n_bf = store.backfill_rec_outcomes(con, u)
        if n_bf:
            log("  回填前一日推荐结局 %d 条" % n_bf)
        maxst = max([r["streak"] for r in lus], default=0)
        lb_cnt = sum(1 for r in lus if r["streak"] >= 2)
        store.upsert_rec_day(con, date, maxst, lb_cnt, len(lus),
                             sent.get("score"), cyc.get("phase"), rec.get("env_k"), len(rec.get("all") or []))
        for it in (rec.get("all") or []):
            # 2026-08-29 特征扩列：连板票落库带上板块强度/质量/换手/竞价形态，
            # 供回测多维归因（复盘「为什么这批推荐强/弱」）。
            store.upsert_rec_pick(
                con, date, it["code"], it["name"], it["streak"],
                it.get("p_break"), it.get("tag"),
                sector_strength=it.get("sector_strength"),
                quality=it.get("quality"),
                turn=it.get("turn"),
                auction_pattern=it.get("auction_pattern"))
        con.commit()
        log("  历史连板库已更新（%s：高度 %d / 连板 %d 只）" % (date, maxst, lb_cnt))
    except Exception as e:
        log("  历史连板库写入失败（不影响主流程）：%r" % e)

    # ---- 先生成基线叙事，再叠加 AI 判断 ----
    data["narrative"] = build_narrative(data)

    # ---- 多模型综合判断（Hy3 宿主叙事 + 可选 DeepSeek/Kimi/Qwen 共识）----
    try:
        consensus = ai_judge.judge(data)
        if consensus:
            data["ai_consensus"] = consensus
            if consensus.get("n_models", 0) > 1:
                base = data["narrative"].get("generated_by", "")
                data["narrative"]["generated_by"] = ((base + " + 多模型共识") if base else "多模型共识")
                data["narrative"]["ai_generated"] = True
                log("  多模型共识参与：%s，方向=%s，置信度=%.0f"
                    % (consensus.get("models"), consensus.get("direction"), consensus.get("confidence")))
    except Exception as e:
        log("  多模型判断失败，回退模板叙事：%r" % e)

    # Hy3 引擎驱动叙事：若 dist/ai_narrative.json 存在且日期与本次分析日一致，则优先采用
    # （AI 撰写，覆盖模板文案）。日期不一致时回退模板叙事，避免旧文案套新数据。
    ai_path = os.path.join(DIST, "ai_narrative.json")
    if os.path.exists(ai_path):
        try:
            ai = json.load(open(ai_path, encoding="utf-8"))
            if ai.get("bullets") and ai.get("date") == data["meta"].get("date"):
                data["narrative"]["headline"] = ai.get("headline", data["narrative"]["headline"])
                data["narrative"]["bullets"] = ai["bullets"]
                data["narrative"]["outlook"] = ai.get("outlook", data["narrative"]["outlook"])
                data["narrative"]["ai_generated"] = True
                data["narrative"]["generated_by"] = ai.get("generated_by", "Hy3 引擎")
                data["narrative"]["generated_at"] = ai.get("generated_at", "")
                data["narrative"]["hy3_applied"] = True
                log("  采用 Hy3 引擎叙事（%s，%s）" % (ai.get("generated_by", "Hy3"), ai.get("date")))
            else:
                log("  ai_narrative.json 日期(%s)与本次(%s)不符，使用引擎模板叙事" %
                    (ai.get("date"), data["meta"].get("date")))
        except Exception as e:
                log("  ai_narrative.json 读取失败，回退模板叙事：%s" % e)
    # HY3 引擎叙事未采用（脚本环境无宿主撰写文件 / 日期不符）→ 启用备用模型生成叙事
    if not data["narrative"].get("hy3_applied"):
        try:
            # 2026-08-27 用户拍板：GLM-4.6 保底优先（Coding 端点免费）→ kimi 兜底。
            # preferred=None 走 models.json 的 narrative_backup = ["zhipu","kimi"]。
            backup = ai_judge.generate_narrative_backup(data, preferred=None)
            if backup:
                data["narrative"]["headline"] = backup["headline"]
                data["narrative"]["bullets"] = backup["bullets"]
                data["narrative"]["outlook"] = backup.get("outlook", data["narrative"].get("outlook", ""))
                data["narrative"]["ai_generated"] = True
                data["narrative"]["generated_by"] = backup["generated_by"]
                data["narrative"]["generated_at"] = time.strftime("%Y-%m-%d %H:%M")
                data["narrative"]["hy3_backup"] = True
                log("  备用模型叙事已生成（%s）" % backup["generated_by"])
        except Exception as e:
            log("  备用模型叙事失败，保留模板叙事：%r" % e)
    data["meta"]["build_seconds"] = round(time.time() - t0, 1)

    # ---- 信息推送（微信/Telegram/邮件），失败不影响主流程 ----
    # 设 SUPPRESS_PUSH=1 可仅重建数据、不重复推送（用于部署前重算）
    if os.environ.get("SUPPRESS_PUSH"):
        log("  推送已抑制（SUPPRESS_PUSH=1），仅重建数据")
    else:
        try:
            deploy_url = ""
            du = os.path.join(ROOT, "config", "deploy_url.txt")
            if os.path.exists(du):
                deploy_url = open(du, encoding="utf-8").read().strip()
            summary = notifier.format_stock_summary(data, deploy_url, mode="close", con=con)
            # ServerChan 单条 desp ≤ 8192 字：附一份精简「只给结果」版，确保关键推送不静默丢失
            summary["sc_text"] = notifier.format_sc(data, deploy_url, mode="close", con=con)["text"]
            if dedup_close:
                # 复盘补发(close_again)必须用独立的 mode，与 15:20 按时(mode="close")互不干扰，
                # 否则会被 notifier 的 once-per-day 去重当成“今日已推送”直接吞掉
                # （已复现：run 58 复盘跑成功却零发送，用户收不到复盘）。
                # 复盘必须保证送达：仅由 once-per-day 挡住多重定时器的重复触发，
                # 不再做“内容相同则跳过”——避免用户再次收不到复盘。
                # 去重按『分析日(adate)』判定，避免前一日补发在零点后运行吞掉当日名额。
                summary = dict(summary)
                summary["title"] = summary["title"].replace("盘后复盘", "复盘补发")
                summary["sc_text"] = notifier.format_sc(data, deploy_url, mode="close_again", con=con)["text"]
                notifier.push(summary, mode="close_again", analysis_date=date)
            else:
                notifier.push(summary, mode="close", analysis_date=date)
        except Exception as e:
            log("  通知推送失败（不影响主流程）：%r" % e)

    # ---- 推送记录嵌入看板（无通道时也能在站点看到最近推送内容）----
    try:
        logp = os.path.join(ROOT, "dist", "push_log.jsonl")
        pushes = []
        if os.path.exists(logp):
            with open(logp, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            pushes.append(json.loads(line))
                        except Exception:
                            pass
        last = {}
        for p in reversed(pushes):
            if p.get("mode") not in last:
                last[p["mode"]] = p
            if len(last) >= 6:  # 盘前/竞价后/收盘/复盘补发/盘中异动/周末 共6类，全部保留
                break
        data["last_push"] = last
    except Exception:
        pass


    # ---- 模拟盘模块（executor 复盘产物）：每日操作+盈亏+被拒留痕 → data["sim"] ----
    # 数据链路：本机 runner.py --review 写 sim_review.json 并自动回传仓库
    # state/sim_review.json（gh_api Contents 推送）→ CI checkout 天然可见 →
    # build 读入嵌入 data.js → 网站新增「模拟盘」视图。
    # 本机直跑 build.py 时回落读 tools/executor/sim_review.json（开发模式）。
    # 文件不存在（如云端首跑）时静默降级为空段。
    try:
        simp = os.path.join(ROOT, "state", "sim_review.json")
        if not os.path.exists(simp):
            simp = os.path.join(ROOT, "tools", "executor", "sim_review.json")
        if os.path.exists(simp):
            simrev = json.load(open(simp, encoding="utf-8"))
            days = simrev.get("days") or {}
            if days:
                skeys = sorted(days.keys())
                dlast = days[skeys[-1]]
                # 累计收益曲线（按日）
                curve = [{"d": k, "total": days[k].get("total"),
                          "pct": days[k].get("total_pct"),
                          "day": days[k].get("day_realized_pct")} for k in skeys]
                init_cash = 100000
                try:
                    _ecfg = json.load(open(os.path.join(ROOT, "tools", "executor",
                                                        "config.json"), encoding="utf-8"))
                    init_cash = float((_ecfg.get("sim") or {}).get("initial_cash") or 100000)
                except Exception:
                    pass
                # 月度汇总
                months = {}
                for k in skeys:
                    mk = k[:7]
                    m = months.setdefault(mk, {"month": mk, "n_trades": 0, "n_closed": 0,
                                               "wins": 0, "day_pnl_sum": 0.0, "n_days": 0})
                    m["n_trades"] += len(days[k].get("trades") or [])
                    m["n_days"] += 1
                    m["day_pnl_sum"] += days[k].get("day_realized_pct") or 0
                    for c in (days[k].get("closed") or []):
                        m["n_closed"] += 1
                        if (c.get("pnl_pct") or 0) > 0:
                            m["wins"] += 1
                for m in months.values():
                    m["win_rate"] = round(m["wins"] * 100.0 / m["n_closed"], 1) if m["n_closed"] else None
                    m["day_pnl_sum"] = round(m["day_pnl_sum"], 2)
                data["sim"] = {
                    "updated_at": simrev.get("updated_at") or "",
                    "initial_cash": init_cash,
                    "last": {"date": dlast.get("date"), "total": dlast.get("total"),
                             "cash": dlast.get("cash"), "market_value": dlast.get("market_value"),
                             "total_pct": dlast.get("total_pct"),
                             "day_realized_pct": dlast.get("day_realized_pct"),
                             "n_holding": dlast.get("n_holding"),
                             "trades": dlast.get("trades") or [],
                             "closed": dlast.get("closed") or [],
                             "rejects": dlast.get("rejects") or [],
                             "decisions": dlast.get("decisions") or [],
                             "holding_plans": dlast.get("holding_plans") or [],
                             "auction_watch": dlast.get("auction_watch") or [],
                             "summary_line": dlast.get("summary_line") or ""},
                    "curve": curve,
                    "months": [months[k] for k in sorted(months.keys())],
                }
                # 心跳新鲜度（2026-08-29 可完善项）：执行器失联/断网/没开机时，
                # 网站能直接看出「数据不新鲜」而不是误以为没交易。
                # updated_at 是本机时间字符串；距今超过 26 小时（隔一个交易日）即 stale。
                try:
                    import datetime as _dt
                    _ua = simrev.get("updated_at") or ""
                    _t = _dt.datetime.strptime(_ua, "%Y-%m-%d %H:%M:%S")
                    _age_h = (_dt.datetime.now() - _t).total_seconds() / 3600.0
                    data["sim"]["heartbeat"] = {
                        "age_hours": round(_age_h, 1),
                        "stale": _age_h > 26,
                    }
                except Exception:
                    pass
                log("  模拟盘模块：%d 个交易日（最新 %s，累计 %+.2f%%）"
                    % (len(skeys), dlast.get("date"), dlast.get("total_pct") or 0))
    except Exception as e:
        log("  模拟盘数据读取失败（不影响主流程）：%r" % e)

    # ---- 多源交叉校验（东方财富 + 新浪 + 腾讯）：抽查头条标的，标“数据存疑” ----
    try:
        try:
            from pipeline import multi_source
        except ImportError:
            import multi_source
        dq, qmap = multi_source.quality_for_data(data, sample=60)
        data["data_quality"] = dq
        if qmap:
            for it in (data.get("limit_ups") or []):
                if it.get("code") in qmap:
                    it["q"] = qmap[it["code"]]
            for it in (data.get("recommend", {}).get("all") or []):
                if it.get("code") in qmap:
                    it["q"] = qmap[it["code"]]
            for it in (data.get("demons") or []):
                if it.get("code") in qmap:
                    it["q"] = qmap[it["code"]]
        data["meta"]["source"] = "多源交叉校验（东方财富+新浪+腾讯，盘后重建）"
        log("  多源交叉校验：抽查 %d 只，存疑 %d 只"
            % (dq.get("checked", 0), dq.get("flagged_count", 0)))
    except Exception as e:
        log("  多源交叉校验跳过：%r" % e)

    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "data.js")
    with open(out, "w", encoding="utf-8") as f:
        f.write("window.__STOCK_DATA__ = ")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    kb = os.path.getsize(out) / 1024
    log("写出 %s（%.0f KB），总耗时 %.1fs" % (os.path.relpath(out, ROOT), kb, time.time() - t0))

    # 公开版本标记：不含任何敏感数据，供前端轮询检测「是否有新数据」，
    # 避免用户一直开着旧页面却不知道后台已经重新构建。
    try:
        _m = data.get("meta", {}) or {}
        _fp = hashlib.md5(
            json.dumps(_m, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:10]
        meta_public = {
            "date": _m.get("date"),
            "generated_at": _m.get("generated_at"),
            "build_seconds": _m.get("build_seconds"),
            "source": _m.get("source"),
            "snapshot_same_day": _m.get("snapshot_same_day"),
            "version": _fp,
        }
        with open(os.path.join(DIST, "meta.json"), "w", encoding="utf-8") as mf:
            json.dump(meta_public, mf, ensure_ascii=False, separators=(",", ":"))
            mf.write("\n")
    except Exception:
        pass

    con.close()
    return data


def load_existing_data():
    """读取 dist/data.js（最近一次分析结果），用于竞价前推送等。"""
    p = os.path.join(ROOT, "dist", "data.js")
    if not os.path.exists(p):
        return None
    txt = open(p, encoding="utf-8").read()
    i = txt.find("=")
    if i < 0:
        return None
    j = txt[i + 1:].strip()
    if j.endswith(";"):
        j = j[:-1]
    import re as _re
    j = _re.sub(r"\bNaN\b", "null", j)
    j = _re.sub(r"\bInfinity\b", "null", j)
    try:
        return json.loads(j)
    except Exception:
        return None


def push_preauction():
    """竞价前（盘前）推送：读取最近一次分析结果，生成『竞价前观察』简报并推送。"""
    data = load_existing_data()
    if not data:
        print("[preauction] 未找到 dist/data.js，无法推送")
        return None
    deploy_url = ""
    du = os.path.join(ROOT, "config", "deploy_url.txt")
    if os.path.exists(du):
        deploy_url = open(du, encoding="utf-8").read().strip()
    summary = notifier.format_stock_summary(data, deploy_url, mode="preauction")
    summary["sc_text"] = notifier.format_sc(data, deploy_url, mode="preauction")["text"]
    return notifier.push(summary, mode="preauction")


def _push_with_store(mode):
    """读取最近一次分析结果并推送（竞价确认 / 盘中异动），可随时触发。"""
    data = load_existing_data()
    if not data:
        print("[%s] 未找到 dist/data.js，无法推送" % mode)
        return None
    deploy_url = ""
    du = os.path.join(ROOT, "config", "deploy_url.txt")
    if os.path.exists(du):
        deploy_url = open(du, encoding="utf-8").read().strip()
    con = None
    try:
        con = store.connect()
    except Exception:
        con = None
    if mode == "auction":
        summary = notifier.format_auction_summary(data, deploy_url, con)
    elif mode == "anomaly":
        summary = notifier.format_anomaly_summary(data, deploy_url, con)
    else:
        summary = notifier.format_stock_summary(data, deploy_url, mode="close", con=con)
    return notifier.push(summary, mode=mode)


def push_auction():
    return _push_with_store("auction")


def push_anomaly():
    """盘中异动：外部定时器每 15 分钟巡查一次，但只在新标的『首次出现』时推送，
    已报过的票当天不再重复刷屏（内容去重，依据 push_log 中记录的 codes）。
    实时抓取失败时绝不回退昨日 data.js（那会被误读为"迟到信息"），直接跳过。

    ⚠ 交易时段闸：仅在北京时间 09:15–15:00（且为交易日）才真正推送；其余时段
    （凌晨 / 休市）即使被外部定时器或看门狗误点火，也直接跳过，绝不发出『盘中异动』
    （曾发生中国时间 4 点误推盘中异动的事故，根因即此处缺交易时段判断）。"""
    # 交易时段闸：非 09:15–15:00 北京时段不推送盘中异动（根治 4 点误推）
    if not notifier._in_anomaly_window():
        print("[anomaly] 当前非交易时段（北京 %s），跳过" % notifier._bj_now().strftime("%H:%M"))
        return ["skipped:off-hours"]
    # 1) 实时异动（最优）：盘中随时捕捉涨停/急拉/板块异动
    try:
        s = _live_anomaly_summary(_deploy_url())
    except Exception as e:
        # 实时抓取失败：绝不回退到昨日 data.js（会被误读为"迟到信息"），直接跳过
        print("[anomaly] 实时异动抓取失败，跳过（不回退旧快照）：%r" % e)
        return ["skipped:live-fetch-failed"]
    if not (s and s.get("text")):
        return ["skipped:empty"]
    if not s.get("has_signal"):
        print("[anomaly] 实时无显著异动（涨停池/涨幅榜均空），跳过空推送")
        return ["skipped:no-signal"]
    # 妖股基因观察池（最近一次分析结果里的 demons）：用于盘中双确认（历史形态基因 ∩ 今日实时封板）。
    # 盘中 CI 运行时 dist/data.js 已由 state.tar.gz 恢复，load_existing_data 可读到；
    # 若缺失/解析失败则观察池为空，双确认段自动跳过，不影响主流程。
    demon_map = {}
    try:
        _ed = load_existing_data()
        if _ed:
            demon_map = {str(d.get("code")): d for d in (_ed.get("demons") or [])
                         if d.get("code")}
    except Exception:
        demon_map = {}
    # 2) 内容去重：只推送"今天尚未报过"的新标的，已报过的当天不再重复刷屏
    reported = notifier.reported_anomaly_codes_today()
    new_codes = [c for c in s.get("codes", []) if c and c not in reported]
    if not new_codes:
        print("[anomaly] 本轮异动标的均为已报过的票，跳过（避免重复刷屏）")
        return ["skipped:no-new"]
    return notifier.push(_anomaly_focused(s, new_codes, _deploy_url(), demon_map),
                         mode="anomaly", codes=new_codes)


def _anomaly_focused(s, new_codes, url, demon_map=None):
    """把本轮『新增』异动标的提炼成一条聚焦消息（已报过的不重复列出）。
    demon_map：妖股基因观察池 {code: demon}（来自最近一次分析结果），用于盘中双确认高亮。"""
    now = time.strftime("%Y-%m-%d %H:%M")
    newset = set(new_codes)
    n_zt = [it for it in (s.get("zt") or []) if str(it.get("c")) in newset]
    n_mv = [m for m in (s.get("movers") or []) if str(m.get("f12")) in newset]
    L = []
    L.append("## 🆕 A股盘中新增异动 · %s（%d 只）" % (now, len(new_codes)))
    L.append("")
    L.append("> 实时行情来自东方财富公开接口；以下为本次巡查**新出现**的异动标的（已报过的不重复）。")
    L.append("")
    if n_zt:
        L.append("### 🔥 新封板（%d）" % len(n_zt))
        for it in n_zt[:15]:
            name = it.get("n", "?"); code = it.get("c", "")
            lbc = it.get("lbc") or 1
            hy = it.get("hybk") or "—"
            fbt = str(it.get("fbt") or "")
            fbt = ("000000" + fbt)[-6:] if fbt.isdigit() else ""
            fbt_s = (fbt[:2] + ":" + fbt[2:4]) if fbt else "—"
            zbc = it.get("zbc") or 0
            tag = ("%d板" % lbc) if lbc and lbc > 1 else "首板"
            warn = " ⚠炸板%d次" % zbc if zbc else ""
            L.append("- **%s**(/%s) %s · %s · 封板%s%s" % (name, code, tag, hy, fbt_s, warn))
        L.append("")
    if n_mv:
        L.append("### ⚡ 新急拉/强势（%d）" % len(n_mv))
        for m in n_mv[:12]:
            name = m.get("f14", "?"); code = m.get("f12", "")
            pct = m.get("f3") or 0
            main = m.get("f62") or 0
            hs = m.get("f184") or 0
            main_s = ("主力净流入 %.1f亿" % (main / 1e8)) if abs(main) >= 1e7 else "主力净流出 %.1f亿" % (abs(main) / 1e8)
            L.append("- **%s**(/%s) +%.2f%% ｜ 换手%.1f%% ｜ %s" % (name, code, pct, hs, main_s))
        L.append("")
    # 关注股盘中异动（网页自选池：涨停/跌停/急拉急跌）
    n_wl = [w for w in (s.get("watch") or []) if str(w.get("c")) in newset]
    if n_wl:
        L.append("### ⭐ 关注股盘中异动（%d）" % len(n_wl))
        for w in n_wl[:10]:
            pct = w.get("pct") or 0
            L.append("- **%s**(/%s) %s%% ｜ 现价 %s"
                     % (w.get("n"), w.get("c"), ("+" if pct >= 0 else "") + ("%.2f" % pct), w.get("price")))
        L.append("")
    # 题材联动：本轮新增涨停按行业聚合，≥3 只同板块即视为题材爆发
    from collections import Counter
    sec = Counter((it.get("hybk") or "—") for it in n_zt
                  if it.get("hybk") and it.get("hybk") != "—")
    hot = [b for b, c in sec.most_common() if c >= 3]
    if hot:
        L.append("")
        L.append("### 🔥 题材联动（本轮新增涨停）")
        for b in hot:
            L.append("- %s：%d 只涨停" % (b, sec[b]))
    # ⚡ 妖股潜力（实时资金维度）：对新增首板/早板打分，挑高分标的点出，盘中及时发现
    sec_all = Counter((it.get("hybk") or "—") for it in (s.get("zt") or []))
    yaogu_items = []
    for it in n_zt:
        try:
            sc, _reasons, meta = yaogu.yaogu_score(
                it, sec_all.get(it.get("hybk") or "—", 1))
        except Exception:
            continue
        if sc >= 55:
            yaogu_items.append((sc, it, meta))
    yaogu_items.sort(key=lambda x: -x[0])
    if yaogu_items:
        L.append("")
        L.append("### ⚡ 妖股潜力（本轮新增 · 实时资金维度）")
        for sc, it, meta in yaogu_items[:5]:
            tag = ("%d板" % meta["lbc"]) if meta["lbc"] > 1 else "首板"
            warn = (" ⚠炸板%d次" % meta["zbc"]) if meta["zbc"] else ""
            L.append("- **%s**(/%s) 潜力分%d · %s · 封单%.2f亿(流通盘%.2f%%) · %s封板%s"
                     % (it.get("n"), it.get("c"), sc, tag, meta["fund_yi"],
                        meta["ratio"], meta["fbt"], warn))
    # ⭐ 妖股双确认（盘中 · 历史形态基因 ∩ 今日实时封板）：观察池里具妖股基因的票今日共振封板，
    # 是盘中最早出现的强信号之一。只取本轮【新增封板】(new_codes)，天然按日去重、不刷屏。
    if demon_map:
        dc = [it for it in n_zt if str(it.get("c")) in demon_map]
        if dc:
            L.append("")
            L.append("### ⭐ 妖股双确认（盘中 · 历史形态基因 + 今日封板）")
            for it in dc[:8]:
                code = str(it.get("c")); d = demon_map[code]
                name = it.get("n") or d.get("name")
                lbc = it.get("lbc") or 1
                tag = ("%d板" % lbc) if lbc and lbc > 1 else "首板"
                sim = (d.get("similar") or [])
                sim_s = ("（神似历史妖股：%s）" % sim[0].get("name")) if sim else ""
                L.append("- **%s**(/%s) %s · %s%s"
                         % (name, code, tag, (it.get("hybk") or "—"), sim_s))
    zt_all = s.get("zt") or []
    mv_all = s.get("movers") or []
    L.append("### 📊 当前盘面")
    L.append("- 涨停池：**%d 只**（本轮新增 %d）" % (len(zt_all), len(n_zt)))
    L.append("- 涨幅异动(≥6%%)：**%d 只**（本轮新增 %d）" % (len(mv_all), len(n_mv)))
    trig, md = _crash_section()
    if trig:
        L.append(""); L.append(md)
    if url:
        L.append("---"); L.append("完整数据看板：%s" % url)
    return {"title": "A股盘中新增异动 %s（%d只）" % (now, len(new_codes)),
            "text": "\n".join(L)}


def push_open_anomaly():
    """竞价后开盘前异动（9:26，开盘前最后提醒）：聚焦竞价异动标的（一字板/弱转强/爆量派发/强转弱），
    经 ServerChan 推送（固定四条之一），PushPlus 冗余兜底。

    ⚠ 交易时段闸：仅在北京时间 09:15–15:00（且为交易日）才推送；其余时段（如凌晨误点火）
    直接跳过，杜绝非交易时段误推。开盘前 09:26 落在交易时段内，正常放行。"""
    if not notifier._in_anomaly_window():
        print("[open_anomaly] 当前非交易时段（北京 %s），跳过" % notifier._bj_now().strftime("%H:%M"))
        return ["skipped:off-hours"]
    data = load_existing_data()
    if not data:
        print("[open_anomaly] 未找到 dist/data.js，无法推送")
        return None
    deploy_url = _deploy_url()
    summary = notifier.format_open_anomaly_summary(data, deploy_url)
    return notifier.push(summary, mode="open_anomaly")


def push_panic():
    """盘中恐慌 / 崩盘预警（突发快速下杀时）。优先实时跌停池+指数下杀监控；
    未触发实时阈值时，回退到当日已分析的恐慌结论（仅升温/恐慌级才推）。
    经 PushPlus 随时推送，不占 ServerChan 固定 4 条额度；非交易时段/冷却期内跳过。"""
    if not notifier._in_anomaly_window():
        print("[panic] 当前非交易时段（北京 %s），跳过" % notifier._bj_now().strftime("%H:%M"))
        return None
    trig, md = _crash_section()
    if trig:
        now = time.strftime("%Y-%m-%d %H:%M")
        text = ("## ⚠️ A股突发快速下杀 / 崩盘预警 · %s\n\n%s\n\n"
                "> 实时行情来自东方财富公开接口；跌停池与各指数快速下杀已触发阈值。"
                % (now, md))
        return notifier.push({"title": "⚠️ 崩盘预警 %s" % now, "text": text}, mode="panic")
    data = load_existing_data()
    panic = (data or {}).get("panic") if data else None
    if panic and panic.get("level") in ("升温", "恐慌"):
        summary = notifier.format_panic_summary(data, _deploy_url())
        return notifier.push(summary, mode="panic")
    print("[panic] 未触发实时崩盘阈值，且当日恐慌等级=%s，跳过推送"
          % (panic.get("level") if panic else "无"))
    return None


def push_yaogu():
    """妖股潜力榜（盘后）：基于实时涨停池做『妖股潜力分』，PushPlus 推送。
    与 engine.demon_scan（K线形态『妖股基因』）互补——本函数抓『实时资金+题材』维度，
    可盘后出榜、盘中经异动标签及时发现。无信号/抓取失败则跳过，不回退旧快照。"""
    try:
        rep = yaogu.live_report()
    except Exception as e:
        print("[yaogu] 实时涨停池抓取失败，跳过：%r" % e)
        return ["skipped:live-fetch-failed"]
    if not rep:
        print("[yaogu] 涨停池为空（可能已收盘无数据或节假日），跳过")
        return ["skipped:empty"]
    md = yaogu.format_markdown(rep)
    if not md:
        return ["skipped:empty"]
    now = _bj_now().strftime("%Y-%m-%d %H:%M")
    title = "🔥 妖股潜力榜 %s（涨停%d只·Top%d）" % (
        rep["date"], rep["count"], len(rep["ranked"]))
    return notifier.push({"title": title, "text": md}, mode="yaogu")


def _crash_section():
    """实时崩盘 / 恐慌监控：跌停池 + 主要指数快速下杀。返回 (triggered, md)。
    各数据源独立容错；全部失败也不影响主流程。"""
    try:
        import em_api
        idx = em_api.index_snapshot() or []
        dtp = em_api.dt_pool() or []
        dt_count = len(dtp)
        idx_line = "、".join("%s %s%%" % (x.get("name"), x.get("pct"))
                             for x in idx[:4] if x.get("pct") is not None)
        crash = any((x.get("pct") or 0) <= -2.5 for x in idx)
        if crash or dt_count >= 15:
            L = []
            L.append("### ⚠️ 盘面恐慌 / 快速下杀")
            L.append("- 主要指数：%s" % (idx_line or "—"))
            L.append("- 跌停池：%d 只（实时）" % dt_count)
            L.append("- 信号：%s" % ("指数快速下杀 ⚠" if crash else "跌停潮涌动 ⚠"))
            return True, "\n".join(L)
        return False, ""
    except Exception as e:
        return False, "> 盘面恐慌监控暂不可用：%s" % str(e)[:40]


def _live_watch_movers(threshold=3.0):
    """关注股盘中实时异动：对关注池（notify/watch.json/holdings）实时报价，挑出 |涨跌幅|≥阈值
    或触及涨停/跌停的标的。走东财 push2（CI 有网）；任一失败仅跳过该只。"""
    import em_api
    try:
        codes, names, _added = watchlist.load_watch_codes()
    except Exception:
        return []
    if not codes:
        return []
    out = []
    for c in codes:
        m = 1 if c[0] in "69" else (0 if c[0] in "023" else None)
        if m is None:
            continue
        secid = "%d.%s" % (m, c)
        try:
            j = em_api.push2_json(
                "/api/qt/stock/get?secid=%s&fields=f43,f57,f58,f170&_=%d" % (secid, int(time.time() * 1000)))
            d = (j or {}).get("data") or {}
            pct = d.get("f170")
            try:
                pct = float(pct) if pct not in (None, "-", "--", "") else 0.0
            except Exception:
                pct = 0.0
            if abs(pct) >= threshold or abs(pct) >= 9.8:
                out.append({"c": c, "n": d.get("f58") or names.get(c) or c,
                            "pct": round(pct, 2), "price": d.get("f43")})
        except Exception:
            continue
    return out


def _live_anomaly_summary(url):
    """实时异动：涨停池 + 涨幅榜急拉，生成 Markdown。
    各数据源独立容错：某一路失败仅跳过该段，其余仍实时呈现；全部失败才上抛。"""
    import em_api
    now = time.strftime("%Y-%m-%d %H:%M")
    L = []
    L.append("## A股盘中异动提醒 · %s" % now)
    L.append("")
    L.append("> 实时行情来自东方财富公开接口，随时捕捉涨停/急拉/板块异动。")
    L.append("")
    # 实时涨停池（独立容错）
    zt = []
    try:
        zt = em_api.zt_pool() or []
    except Exception as e:
        L.append("> 涨停池实时拉取暂不可用：%s" % str(e)[:40])
    zt_sorted = sorted(zt, key=lambda x: -(x.get("lbc") or 0)) if zt else []
    L.append("### 🔥 实时涨停池（%d 只）" % len(zt))
    if zt_sorted:
        for it in zt_sorted[:15]:
            name = it.get("n", "?")
            code = it.get("c", "")
            lbc = it.get("lbc") or 1
            hy = it.get("hybk") or "—"
            fbt = str(it.get("fbt") or "")
            # fbt 为 HHMMSS 整数（如 92500 / 145930），补零到 6 位后取 HH:MM
            fbt = ("000000" + fbt)[-6:] if fbt.isdigit() else ""
            fbt_s = (fbt[:2] + ":" + fbt[2:4]) if fbt else "—"
            zbc = it.get("zbc") or 0
            tag = ("%d板" % lbc) if lbc and lbc > 1 else "首板"
            warn = " ⚠炸板%d次" % zbc if zbc else ""
            L.append("- **%s**(/%s) %s · %s · 封板%s%s" % (name, code, tag, hy, fbt_s, warn))
    else:
        L.append("（当前无涨停标的）")
    L.append("")
    # 涨幅异动（剔除已涨停，取急拉/强势；独立容错）
    mv = []
    try:
        mv, _ = em_api.clist_paged("m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                                   "f12,f14,f2,f3,f62,f184", max_pages=3)
    except Exception as e:
        L.append("> 涨幅榜实时拉取暂不可用：%s" % str(e)[:40])
    movers = [m for m in (mv or []) if 6 <= (m.get("f3") or 0) < 9.8]
    movers.sort(key=lambda x: -(x.get("f3") or 0))
    L.append("### ⚡ 涨幅异动（急拉 / 强势，前 12）")
    if movers:
        for m in movers[:12]:
            name = m.get("f14", "?")
            code = m.get("f12", "")
            pct = m.get("f3") or 0
            main = m.get("f62") or 0
            hs = m.get("f184") or 0
            main_s = ("主力净流入 %.1f亿" % (main / 1e8)) if abs(main) >= 1e7 else "主力净流出 %.1f亿" % (abs(main) / 1e8)
            L.append("- **%s**(/%s) +%.2f%% ｜ 换手%.1f%% ｜ %s" % (name, code, pct, hs, main_s))
    else:
        L.append("（当前无显著涨幅异动）")
    L.append("")
    # 崩盘 / 恐慌实时监控（突然快速下杀）
    trig, md = _crash_section()
    if trig:
        L.append("")
        L.append(md)
    # 关注股盘中实时异动（独立于涨停池/涨幅榜，单独成段）
    wm = []
    try:
        wm = _live_watch_movers()
    except Exception:
        wm = []
    if wm:
        L.append("")
        L.append("### ⭐ 关注股盘中异动（%d）" % len(wm))
        for w in wm:
            L.append("- **%s**(/%s) %s%% ｜ 现价 %s" % (w.get("n"), w.get("c"),
                      ("+" if w.get("pct", 0) >= 0 else "") + str(w.get("pct")),
                      w.get("price")))
    if url:
        L.append("---")
        L.append("完整数据看板：%s" % url)
    return {"title": "A股盘中异动提醒 %s" % now, "text": "\n".join(L),
            "zt": zt_sorted, "movers": movers, "watch": wm,
            "codes": [str(it.get("c")) for it in zt_sorted]
                      + [str(m.get("f12")) for m in movers]
                      + [str(w.get("c")) for w in wm],
            "has_signal": bool(zt_sorted) or bool(movers) or bool(wm)}


def _deploy_url():
    du = os.path.join(ROOT, "config", "deploy_url.txt")
    if os.path.exists(du):
        return open(du, encoding="utf-8").read().strip()
    return ""


def _parse_news_time(it):
    """尽力解析新闻时间，失败返回 None。支持常见字段与格式。"""
    import datetime as _dt
    for k in ("datetime", "date", "time", "showtime", "pub_time", "publish_time"):
        v = it.get(k)
        if not v:
            continue
        s = str(v)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
            try:
                return _dt.datetime.strptime(s[:len(fmt) + 2], fmt)
            except Exception:
                continue
    return None


def _weekend_window_items(items):
    """筛选『周末发酵』窗口要闻：最近一个周五 15:00 之后至今。无日期的视为近期保留。"""
    import datetime as _dt
    now = _bj_now()
    today = now.date()
    offset = (today.weekday() - 4) % 7  # 4=周五，回退到最近周五（含当天）
    last_fri = today - _dt.timedelta(days=offset)
    start = _dt.datetime(last_fri.year, last_fri.month, last_fri.day, 15, 0)
    out = []
    for it in (items or []):
        t = _parse_news_time(it)
        if t is None or t >= start:
            out.append(it)
    return out


def push_weekend():
    """周末发酵条件推送（周日晚 / 周一早）：仅当存在周末要闻时才发送，否则跳过。
    路由到 ServerChan（关键节点），不占 PushPlus/异动额度。18 小时内已发过则跳过，避免周日晚与周一早重复。"""
    data = load_existing_data()
    if not data:
        print("[weekend] 未找到 dist/data.js，跳过")
        return None
    news = load_news()
    items = _weekend_window_items(news.get("items") or [])
    if not items:
        print("[weekend] 无周末发酵信息，跳过发送（符合『没有就不发』）")
        return None
    # 去重：18 小时内已发过 weekend 推送则跳过
    try:
        lp = os.path.join(DIST, "push_log.jsonl")
        if os.path.exists(lp):
            import datetime as _dt
            cutoff = (_bj_now() - _dt.timedelta(hours=18)).strftime("%Y-%m-%d %H:%M:%S")
            for line in open(lp, encoding="utf-8"):
                try:
                    p = json.loads(line)
                except Exception:
                    continue
                if p.get("mode") == "weekend" and p.get("ts", "") >= cutoff:
                    print("[weekend] 18 小时内已发送过周末推送，跳过（防重复）")
                    return None
    except Exception:
        pass
    summary = notifier.format_weekend_summary(data, _deploy_url(), items)
    return notifier.push(summary, mode="weekend")


def push_close_again():
    """收盘后补发（如 20:00）：重新跑分析以拿到最完整数据（龙虎榜、封单等盘后增量），
    若推送正文与当日『收盘后』推送完全相同则跳过，节省 ServerChan 额度留给异动/强晋级。"""
    return run(dedup_close=True)


if __name__ == "__main__":
    d = None
    action = "build"
    for a in sys.argv[1:]:
        if a.startswith("--date="):
            d = a.split("=", 1)[1]
        elif a == "--preauction":
            action = "preauction"
        elif a == "--auction-push":
            action = "auction"
        elif a == "--anomaly-push":
            action = "anomaly"
        elif a == "--open-anomaly":
            action = "open_anomaly"
        elif a == "--panic-push":
            action = "panic"
        elif a in ("--push-close", "--close-push"):
            action = "close_again"
        elif a == "--weekend-push":
            action = "weekend"
        elif a == "--yaogu-push":
            action = "yaogu"
    if action == "preauction":
        push_preauction()
    elif action == "auction":
        push_auction()
    elif action == "anomaly":
        push_anomaly()
    elif action == "open_anomaly":
        push_open_anomaly()
    elif action == "panic":
        push_panic()
    elif action == "close_again":
        push_close_again()
    elif action == "weekend":
        push_weekend()
    elif action == "yaogu":
        push_yaogu()
    else:
        run(d)
