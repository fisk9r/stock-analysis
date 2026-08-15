# -*- coding: utf-8 -*-
"""妖股潜力挖掘（实时涨停池驱动）。

规律来源：以利通电子(603629)为样本——2026 年大妖股，年内最高 +815%。
其反复拉板的底层可量化因子（均来自东方财富涨停池单接口，无需额外请求）：
  · 题材风口：站在「板块涨停潮」里（同行业/概念多只涨停）——算力租赁掀涨停潮即典型
  · 连板位置：首板/二板是介入黄金区（妖股多在 1~3 板被市场确认），晋级(连板高度打开)更佳
  · 封单强度：封单额/流通市值 高 = 资金锁仓坚决（利通单日封单 3 亿+，2 分钟秒板）
  · 流通盘：适中（20~200 亿）最易炒作
  · 换手：充分换手（10~18%）筹码交换健康、弱转强(T字/回封)成妖概率最高；过高=出货
  · 封板质量：早盘秒板 > 上午板 > 尾盘偷板；一字板=锁仓极强，换手回封=弱转强(最强)，多次炸板=弱
  · 题材启动：【新增】板块涨停数相对昨日由冷转热 = 新主线诞生，潜力最高

与 engine.demon_scan（K线形态相似度「妖股基因」）互补：
  本模块抓『实时资金 + 题材』维度，可盘中(每15分钟异动)与盘后(每日榜)双用；
  demon_scan 抓『历史形态』维度，仅盘后(依赖本地K线库)。两者叠加最稳。

【提取口径】盘后推送/看板默认取「最近一个有完整涨停数据的交易日」(上一交易日)，
  保证推送永不空推；盘中异动标签另走实时涨停池（见 build._live_anomaly_summary）。

涨停池字段（实测 20260814）：
  c=代码 n=名称 m=市场(0沪/1深) p=价(0.001元) zdp=涨跌幅% amount=成交额(元)
  ltsz=流通市值(元) tshare=总市值 hs=换手% lbc=连板数 fbt=首封时间(HHMMSS)
  lbt=末封时间 fund=封单额(元) zbc=炸板次数 hybk=行业 zttj={days,ct}
"""
import datetime
import time

try:
    from . import em_api
except ImportError:  # 允许 `python pipeline/build.py` 直引
    import em_api


def _bj_now_str():
    return (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            .replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"))


def _bj_today():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))) \
        .replace(tzinfo=None)


# 题材过滤：东方财富「概念板块」里混入了大量「属性/指数」类板块
# （融资融券、深股通、创业板综、专精特新、机构重仓、富时罗素…），它们不是题材，
# 其「涨停数」只是全市场涨停股的属性统计，会淹没真实热点。以下关键词命中即剔除。
_GENERIC_BOARD_KW = (
    "融资融券", "股通", "创业板", "科创板", "专精特新", "机构重仓", "富时", "标普",
    "中证", "上证", "深证", "北证", "沪深", "证金", "汇金", "MSCI", "基金", "指数",
    "全部", "昨日", "首发", "转股", "新股", "退市", "破净", "央企", "国企", "沪企",
    "深企", "地方", "龙头", "ST", "AH股", "B股", "沪市", "深市", "A股", "大盘",
    "中字头", "举牌", "增持", "减持", "回购", "分红", "高股息", "低市盈率",
    "小盘股", "大盘股", "中盘股", "QFII", "社保", "基金重仓", "保险", "信托",
    "养老金", "证金", "高净资产", "低市净率", "高送转", "含可转债", "沪港通",
)


def _is_generic_board(name):
    if not name:
        return True
    for kw in _GENERIC_BOARD_KW:
        if kw in name:
            return True
    return False


def sector_strength(zt):
    """返回 {行业: 今日涨停数} 板块强度。"""
    cnt = {}
    for r in zt:
        b = r.get("hybk") or "—"
        if b == "—":
            continue
        cnt[b] = cnt.get(b, 0) + 1
    return cnt


def _prev_trading_day(base=None):
    """返回 base(YYYYMMDD 或 None=今天) 的【上一个交易日】(跳过周末) 日期串。"""
    d = (datetime.datetime.strptime(base, "%Y%m%d") if base
         else _bj_today())
    d -= datetime.timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat 6=Sun
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")


def _resolve_trading_date(date=None, lookback=6):
    """解析『提取用交易日』：
    - 指定 date → 直接用；
    - 未指定 → 从今天往前定位最近【交易日】(跳过周末)，再确认该日涨停池非空
      (节假日则继续往前兜底)，即『最近一个有完整涨停数据的交易日』(上一交易日)。
      保证推送/看板永不空推，且日期标签正确。
    注意：① 先跳过周末再判非空，避免向东财请求周末日期——其接口对周末日期会返回
          最近交易日数据却用请求日期打标，会污染『上一交易日』对照；
          ② 判空用原始 _pool（不带涨幅榜兜底），避免把『当前日推导数据』误判成历史日有数据。"""
    if date:
        return date
    d = _bj_today()
    for _ in range(lookback):
        while d.weekday() >= 5:  # 跳过周末，先定位交易日
            d -= datetime.timedelta(days=1)
        cand = d.strftime("%Y%m%d")
        try:
            if em_api._pool("getTopicZTPool", "fbt%3Aasc", cand):
                return cand
        except Exception:
            pass
        d -= datetime.timedelta(days=1)
    return None


def yaogu_score(row, sector_count, yest_lbc=None, yest_sector=None):
    """对单只涨停股打『妖股潜力分』(0~100)，返回 (score, reasons, meta)。

    reasons: list of (维度, 说明, 该维度得分) —— 用于向用户解释『为什么是妖股潜力』。
    meta: 量化中间量，供格式化与盘中标签复用。

    维度权重(合计100)：板块联动20 / 连板位置18 / 封单强度18 / 流通盘10 /
                       换手8 / 封板质量16 / 题材启动10（晋级额外+2）。
    """
    reasons = []
    score = 0.0

    # 1) 板块联动（题材风口）—— 20
    sc = int(sector_count or 1)
    if sc >= 8:
        s1, r1 = 20, "板块涨停潮（同板块≥8只，强主线）"
    elif sc >= 6:
        s1, r1 = 17, "板块强势（同板块%d只涨停）" % sc
    elif sc >= 4:
        s1, r1 = 13, "板块联动（同板块%d只涨停）" % sc
    elif sc >= 2:
        s1, r1 = 9, "板块偏弱（同板块%d只）" % sc
    else:
        s1, r1 = 5, "独苗（同板块仅1只涨停）"
    score += s1
    reasons.append(("板块联动", r1, s1))

    # 2) 连板位置 —— 18
    lbc = int(row.get("lbc") or 1)
    if lbc <= 1:
        s2, r2 = 12, "首板（早期，需其他因子确认）"
    elif lbc == 2:
        s2, r2 = 16, "二板（最佳确认区·一进二）"
    elif lbc == 3:
        s2, r2 = 14, "三板（妖性初显·二进三）"
    elif lbc == 4:
        s2, r2 = 10, "四板"
    elif lbc == 5:
        s2, r2 = 7, "五板"
    else:
        s2, r2 = 4, "%d板（高位明牌，风险大）" % lbc
    if yest_lbc is not None and lbc > yest_lbc:  # 晋级加成
        s2 += 2
        r2 += " · 晋级（连板高度继续打开）"
    score += s2
    reasons.append(("连板位置", r2, s2))

    # 3) 封单强度 —— 18（封单额/流通市值，资金锁仓坚决度）
    ltsz = (row.get("ltsz") or 0) / 1e8      # 亿
    fund = (row.get("fund") or 0) / 1e8      # 亿
    ratio = (fund / ltsz * 100) if ltsz else 0.0
    if ratio >= 2:
        s3, r3 = 18, "封单%.2f亿（流通盘%.2f%%，锁仓极坚决）" % (fund, ratio)
    elif ratio >= 1:
        s3, r3 = 14, "封单%.2f亿（流通盘%.2f%%）" % (fund, ratio)
    elif ratio >= 0.5:
        s3, r3 = 9, "封单%.2f亿（流通盘%.2f%%，一般）" % (fund, ratio)
    elif ratio >= 0.2:
        s3, r3 = 5, "封单%.2f亿（流通盘%.2f%%，偏弱）" % (fund, ratio)
    else:
        s3, r3 = 2, "封单%.2f亿（流通盘%.2f%%，偏弱）" % (fund, ratio)
    score += s3
    reasons.append(("封单强度", r3, s3))

    # 4) 流通盘 —— 10（适中最易炒作）
    if 20 <= ltsz <= 120:
        s4, r4 = 10, "流通市值%.0f亿（适中，易炒作）" % ltsz
    elif 120 < ltsz <= 250:
        s4, r4 = 7, "流通市值%.0f亿" % ltsz
    elif ltsz < 20:
        s4, r4 = 6, "流通市值%.0f亿（小盘，易庄/流动性差）" % ltsz
    else:
        s4, r4 = 3, "流通市值%.0f亿（大票，难连续板）" % ltsz
    score += s4
    reasons.append(("流通盘", r4, s4))

    # 5) 换手 —— 8（分歧换手才出妖：10~18%最佳，过高=出货）
    hs = float(row.get("hs") or 0)
    if 10 <= hs <= 18:
        s5, r5 = 8, "换手%.1f%%（分歧充分，成妖最佳区间）" % hs
    elif 8 <= hs < 10 or 18 < hs <= 22:
        s5, r5 = 5, "换手%.1f%%" % hs
    elif hs < 8:
        s5, r5 = 3, "换手%.1f%%（一字/惜售，筹码未充分交换）" % hs
    else:
        s5, r5 = 1, "换手%.1f%%（过高，警惕出货）" % hs
    score += s5
    reasons.append(("换手", r5, s5))

    # 6) 封板质量 —— 16（早盘秒板 > 上午 > 尾盘；弱转强(T字/回封)最强，多次炸板最弱）
    fbt = str(row.get("fbt") or "")
    fbt = ("000000" + fbt)[-6:] if fbt.isdigit() else ""
    hh = int(fbt[:2]) if fbt else 15
    mm = int(fbt[2:4]) if fbt else 0
    if hh < 9 or (hh == 9 and mm <= 35):
        ts, tm = 11, "早盘先锋板(%s:%s，资金抢筹)" % (fbt[:2], fbt[2:4])
    elif hh <= 11:
        ts, tm = 8, "上午封板(%s:%s)" % (fbt[:2], fbt[2:4])
    else:
        ts, tm = 4, "尾盘封板(%s:%s)" % (fbt[:2], fbt[2:4])
    zbc = int(row.get("zbc") or 0)
    if zbc == 0 and hs < 3:
        fs, fm = 5, "一字板（全天不开板，主力锁仓极强·加速型妖股）"
    elif 0 < zbc <= 1 and 8 <= hs <= 22:
        fs, fm = 5, "换手回封/T字（炸%d次快速回封，弱转强·成妖概率最高）" % zbc
    elif zbc == 0:
        fs, fm = 3, "一次封死（未炸板，筹码稳定）"
    elif zbc == 1:
        fs, fm = 2, "炸板1次"
    else:
        pen = min(4, (zbc - 2) * 2)
        fs, fm = -4 - pen, "炸板%d次（多次开板，封板质量弱）" % zbc
    s6 = ts + fs
    r6 = tm + " · " + fm
    score += s6
    reasons.append(("封板质量", r6, s6))

    # 7) 题材启动（【新增】相对昨日由冷转热 = 新主线诞生）—— 10
    ycnt = yest_sector.get(row.get("hybk") or "—") if yest_sector else None
    if ycnt is None:
        s7, r7 = 3, "（无昨日对照，题材持续性未知）"
    elif sc >= 4 and ycnt <= 2:
        s7, r7 = 10, "题材启动（板块从%d只→%d只涨停，新主线诞生）" % (ycnt, sc)
    elif sc >= ycnt + 3:
        s7, r7 = 7, "题材升温（板块%d→%d只）" % (ycnt, sc)
    elif sc >= ycnt:
        s7, r7 = 4, "题材延续（板块%d只，持平）" % sc
    else:
        s7, r7 = 1, "题材降温（板块%d→%d只）" % (ycnt, sc)
    score += s7
    reasons.append(("题材启动", r7, s7))

    meta = {
        "lbc": lbc, "sector_count": sc, "fund_yi": round(fund, 2),
        "ltsz_yi": round(ltsz, 1), "ratio": round(ratio, 2), "hs": hs,
        "fbt": ("%s:%s" % (fbt[:2], fbt[2:4])) if fbt else "—",
        "zbc": zbc,
    }
    return min(100, round(score, 1)), reasons, meta


def _board_tag(n):
    return "首板" if n <= 1 else ("%d板" % n)


def live_report(date=None, topn=12, with_yesterday=True):
    """妖股潜力榜。返回结构化 dict；无可用交易日数据返回 None。
    未指定 date 时自动取『最近一个有完整涨停数据的交易日』(上一交易日)。"""
    base = _resolve_trading_date(date)
    if not base:
        return None
    zt = em_api.zt_pool(base) or []
    if not zt:
        return None
    s_cnt = sector_strength(zt)

    # 晋级对照 + 题材启动对照：取上一交易日的连板数与板块强度
    yest_lbc, yest_sector = {}, {}
    if with_yesterday:
        pd = _prev_trading_day(base)
        try:
            yz = em_api.zt_pool(pd) or []
            if yz:
                yest_lbc = {str(r.get("c")): int(r.get("lbc") or 1) for r in yz}
                yest_sector = sector_strength(yz)
        except Exception:
            yest_lbc, yest_sector = {}, {}

    ranked = []
    for r in zt:
        sc, reasons, meta = yaogu_score(
            r, s_cnt.get(r.get("hybk") or "—", 1),
            yest_lbc.get(str(r.get("c"))), yest_sector)
        ranked.append({
            "code": str(r.get("c")), "name": r.get("n", ""),
            "sector": r.get("hybk") or "—", "score": sc,
            "boards": meta["lbc"], "reasons": reasons, "meta": meta,
            "pct": r.get("zdp"),
        })
    ranked.sort(key=lambda x: -x["score"])

    # 连板梯隊
    ladder = {}
    for r in zt:
        ladder.setdefault(int(r.get("lbc") or 1), []).append(
            {"code": str(r.get("c")), "name": r.get("n"), "sector": r.get("hybk")})
    fresh = [{"code": str(r.get("c")), "name": r.get("n"), "sector": r.get("hybk")}
             for r in zt if int(r.get("lbc") or 1) == 1]

    # 今日最强题材（概念板块涨停数 TOP，过滤属性/指数类噪音板）
    concept_top = []
    try:
        cons = em_api.board_list("concept") or []
        concept_top = sorted([c for c in cons
                              if (c.get("up") or 0) > 0 and not _is_generic_board(c.get("name"))],
                             key=lambda x: -(x.get("up") or 0))[:8]
    except Exception:
        concept_top = []

    return {
        "date": base,
        "count": len(zt),
        "ranked": ranked[:topn],
        "ladder": {str(k): v for k, v in sorted(ladder.items())},
        "fresh_boards": fresh,
        "sector_top": sorted([{"sector": k, "count": v} for k, v in s_cnt.items() if v >= 2],
                             key=lambda x: -x["count"])[:10],
        "concept_top": concept_top,
        "generated_at": _bj_now_str(),
    }


def format_markdown(rep):
    """妖股潜力榜 → markdown 文本（供 PushPlus / 看板渲染）。"""
    if not rep:
        return None
    L = []
    L.append("## 🔥 妖股潜力榜（涨停池 · 封单/板块/连板/题材启动 多维）")
    L.append("")
    L.append("> **数据日期：%s（上一交易日/最近有完整涨停数据的交易日）**" % rep.get("date"))
    L.append("> **以下按妖股潜力分（0~100）降序推荐**，分越高=站在风口+早板强封单+适中流通盘+题材启动，妖股早期特征越明显。")
    L.append("> 评分 = 板块联动20 + 连板位置18 + 封单强度18 + 流通盘10 + 换手8 + 封板质量16 + 题材启动10（0~100）。")
    L.append("> 与「妖股基因(K线形态)」互补：本榜抓**实时资金+题材**维度，盘中/盘后双用。")
    L.append("")
    if rep.get("concept_top"):
        cs = "、".join("%s(%d)" % (c.get("name", "?"), c.get("up") or 0)
                      for c in rep["concept_top"][:6])
        L.append("**今日最强题材(概念涨停数)**：%s" % cs)
        L.append("")
    ladder_s = "  ".join("%s板×%d" % (k, len(v)) for k, v in rep["ladder"].items())
    L.append("**连板梯隊**：%s" % ladder_s)
    # 市场情绪（连板高度）
    try:
        heights = [int(k) for k in rep["ladder"].keys()]
        maxh = max(heights) if heights else 0
        high = sum(len(v) for k, v in rep["ladder"].items() if int(k) >= 3)
        if maxh >= 4:
            mood = "高（连板高度%d板，3板及以上%d只，易出妖）" % (maxh, high)
        elif maxh >= 3:
            mood = "中（连板高度%d板）" % maxh
        else:
            mood = "低（连板高度%d板，谨慎）" % maxh
        L.append("**市场情绪**：%s" % mood)
    except Exception:
        pass
    L.append("")
    L.append("### 🏆 潜力 Top %d" % len(rep["ranked"]))
    for i, it in enumerate(rep["ranked"], 1):
        m = it["meta"]
        L.append("")
        L.append("**%d. %s(/%s)** · 潜力分 **%d** · %s" % (
            i, it["name"], it["code"], it["score"], _board_tag(it["boards"])))
        L.append("- 板块：%s（同板块%d只涨停）｜ 流通市值%.0f亿 ｜ 换手%.1f%% ｜ "
                 "封单%.2f亿(流通盘%.2f%%) ｜ %s封板%s" % (
                     it["sector"], m["sector_count"], m["ltsz_yi"], m["hs"],
                     m["fund_yi"], m["ratio"], m["fbt"],
                     (" ⚠炸板%d次" % m["zbc"]) if m["zbc"] else ""))
        for _, r, _ in sorted(it["reasons"], key=lambda x: -x[2])[:3]:
            L.append("  - %s" % r)
    if rep.get("fresh_boards"):
        L.append("")
        L.append("### 🆕 新晋首板（%d只 · 明日重点观察能否晋级）" % len(rep["fresh_boards"]))
        L.append("、".join(x["name"] for x in rep["fresh_boards"][:20]))
    L.append("")
    L.append("> ⚠️ 妖股波动极大、风险极高，本榜仅作「规律量化+线索挖掘」参考，非投资建议。")
    return "\n".join(L)
