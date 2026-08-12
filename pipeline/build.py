# -*- coding: utf-8 -*-
"""构建层：跑分析引擎 -> 生成前端 dist/data.js"""
import glob
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
import store
import em_api
import ai_judge
import notifier

ROOT = store.ROOT
DIST = os.path.join(ROOT, "dist")
ARCHIVE = os.path.join(ROOT, "archive")


def log(*a):
    print("[build]", *a, flush=True)


def pick_date(u, override=None):
    """选定分析日：默认最后一个已收盘交易日"""
    if override:
        return override
    if not u.dates:
        raise RuntimeError("行情库为空，请先运行 fetch.py")
    last = u.dates[-1]
    today = time.strftime("%Y-%m-%d")
    now = time.strftime("%H%M")
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


def run(date_override=None, dedup_close=False):
    t0 = time.time()
    con = store.connect()
    log("载入行情库 ...")
    u = engine.Universe(con, days=130)
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

    log("竞价定调分析 ...")
    auction = engine.auction_profile(u, date, lus)
    ladder_hist = engine.ladder_history(u, date, 5)
    rotation = engine.sector_rotation(u, date, code2boards, 12, 5)
    asum = auction.get("summary", {})
    log("  竞价：平均高开 %.2f%% · 一字板 %d · 弱转强 %d · 强转弱 %d"
        % (asum.get("avg_open_pct", 0), asum.get("yizi", 0), asum.get("weak_strong", 0), asum.get("strong_weak", 0)))

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
    rec = engine.recommend(lus, risks, demons, inds, sent, cyc, stats, auction["items"], regime)

    # 趋势向上选股（独立于连板体系，覆盖主升段趋势票）
    try:
        rec["trend"] = engine.screen_uptrend(u, date, code2boards, topn=12)
        log("  趋势向上筛选 %d 只" % len(rec.get("trend") or []))
    except Exception as e:
        log("  趋势向上筛选失败（不影响主流程）：%r" % e)
        rec["trend"] = []

    # 强动量 · 连板余波选股（接住『连板妖股基因、今天非涨停』掉缝里的票，
    # 如风范股份；与 screen_uptrend 的平滑趋势互补，两档并列呈现）
    try:
        rec["momentum"] = engine.screen_momentum(u, date, code2boards, topn=12)
        log("  强动量/连板余波筛选 %d 只" % len(rec.get("momentum") or []))
    except Exception as e:
        log("  强动量筛选失败（不影响主流程）：%r" % e)
        rec["momentum"] = []

    # 阶梯
    ladder = {}
    for r in lus:
        ladder.setdefault(str(r["streak"]), []).append(
            {"code": r["code"], "name": r["name"], "industry": r["industry"],
             "quality": r["quality"], "turn": r["turn"], "yizi": r["yizi"],
             "float_mv": r["float_mv"],
             "p_continue": next((x["p_continue"] for x in risks if x["code"] == r["code"]), None)})

    e_today = next((x for x in series if x["date"] == date), None) or {}
    idx = snap.get("index") or []
    data = {
        "meta": {
            "date": date, "prev_date": prev,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
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
        "demons": demons[:40],
        "demon_templates": [{"code": t["code"], "name": t["name"], "start": t["start"],
                             "gain": t["gain"], "max_streak": t["max_streak"],
                             "trigger": t["trigger"]} for t in tpls[:40]],
        "recommend": rec,
        "global_market": gm,
        "regime": regime,
        "news": load_news(),
    }

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
            store.upsert_rec_pick(con, date, it["code"], it["name"], it["streak"],
                                  it.get("p_break"), it.get("tag"))
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
                log("  采用 Hy3 引擎叙事（%s，%s）" % (ai.get("generated_by", "Hy3"), ai.get("date")))
            else:
                log("  ai_narrative.json 日期(%s)与本次(%s)不符，使用引擎模板叙事" %
                    (ai.get("date"), data["meta"].get("date")))
        except Exception as e:
            log("  ai_narrative.json 读取失败，回退模板叙事：%s" % e)
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
            summary = notifier.format_stock_summary(data, deploy_url, mode="close")
            if dedup_close:
                prev = notifier.last_close_text()
                if prev is not None and prev.strip() == summary["text"].strip():
                    log("  收盘补发内容与当日『收盘后』推送完全相同，跳过（节省 ServerChan 额度，留给异动/强晋级）")
                else:
                    notifier.push(summary, mode="close")
            else:
                notifier.push(summary, mode="close")
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
            if len(last) >= 4:  # 收盘后 / 竞价前 / 竞价确认 / 盘中异动
                break
        data["last_push"] = last
    except Exception:
        pass

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
        summary = notifier.format_stock_summary(data, deploy_url, mode="close")
    return notifier.push(summary, mode=mode)


def push_auction():
    return _push_with_store("auction")


def push_anomaly():
    """盘中异动捕捉：优先用东方财富实时行情生成『异动提醒』（随时捕捉），
    失败则回退到最近一次已分析数据。始终经 PushPlus 推送（双 token 同时送达），
    不占 ServerChan 的 5 条关键节点额度。"""
    # 1) 实时异动（最优）：盘中随时捕捉涨停/急拉/板块异动
    try:
        s = _live_anomaly_summary(_deploy_url())
        if s and s.get("text"):
            return notifier.push(s, mode="anomaly")
    except Exception as e:
        print("[anomaly] 实时异动抓取失败，回退已分析数据：%r" % e)
    # 2) 回退：最近一次已分析数据中的竞价量能异动 / 高位断板风险
    return _push_with_store("anomaly")


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
                                   "f12,f14,f2,f3,f62,f184", max_pages=2)
    except Exception as e:
        L.append("> 涨幅榜实时拉取暂不可用：%s" % str(e)[:40])
    movers = [m for m in (mv or []) if (m.get("f3") or 0) < 9.8 and (m.get("f3") or 0) > 0]
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
    if url:
        L.append("---")
        L.append("完整数据看板：%s" % url)
    return {"title": "A股盘中异动提醒 %s" % now, "text": "\n".join(L)}


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
    now = _dt.datetime.now()
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
            cutoff = (_dt.datetime.now() - _dt.timedelta(hours=18)).strftime("%Y-%m-%d %H:%M:%S")
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
        elif a in ("--push-close", "--close-push"):
            action = "close_again"
        elif a == "--weekend-push":
            action = "weekend"
    if action == "preauction":
        push_preauction()
    elif action == "auction":
        push_auction()
    elif action == "anomaly":
        push_anomaly()
    elif action == "close_again":
        push_close_again()
    elif action == "weekend":
        push_weekend()
    else:
        run(d)
