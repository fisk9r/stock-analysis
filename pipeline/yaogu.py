# -*- coding: utf-8 -*-
"""妖股潜力挖掘（实时涨停池驱动）。

规律来源：以利通电子(603629)为样本——2026 年大妖股，年内最高 +815%。
其反复拉板的底层可量化因子（均来自东方财富涨停池单接口，无需额外请求）：
  · 题材风口：站在「板块涨停潮」里（同行业/概念多只涨停）——算力租赁掀涨停潮即典型
  · 连板位置：首板/二板是介入黄金区（妖股多在 1~3 板被市场确认），晋级(连板高度打开)更佳
  · 封单强度：封单额/流通市值 高 = 资金锁仓坚决（利通单日封单 3 亿+，2 分钟秒板）
  · 流通盘：适中（20~200 亿）最易炒作
  · 换手：充分换手（8~25%）筹码交换健康，才走得远
  · 封板时间/质量：早盘秒板 > 上午板 > 尾盘偷板；炸板次数少 = 封板质量高

与 engine.demon_scan（K线形态相似度「妖股基因」）互补：
  本模块抓『实时资金 + 题材』维度，可盘中(每15分钟异动)与盘后(每日榜)双用；
  demon_scan 抓『历史形态』维度，仅盘后(依赖本地K线库)。两者叠加最稳。

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


def _prev_days(base, n=3):
    """返回 base(YYYYMMDD) 往前 n 天的日期串列表。"""
    try:
        d = datetime.datetime.strptime(base, "%Y%m%d")
    except Exception:
        d = datetime.datetime.now()
    return [(d - datetime.timedelta(days=k)).strftime("%Y%m%d") for k in range(1, n + 1)]


def yaogu_score(row, sector_count, yest_lbc=None):
    """对单只涨停股打『妖股潜力分』(0~100)，返回 (score, reasons, meta)。

    reasons: list of (维度, 说明, 该维度得分) —— 用于向用户解释『为什么是妖股潜力』。
    meta: 量化中间量，供格式化与盘中标签复用。
    """
    reasons = []
    score = 0.0

    # 1) 板块联动（题材风口）—— 25
    sc = int(sector_count or 1)
    if sc >= 6:
        s1, r1 = 25, "板块涨停潮（同板块≥6只，强风口）"
    elif sc >= 4:
        s1, r1 = 20, "板块强势（同板块%d只涨停）" % sc
    elif sc >= 2:
        s1, r1 = 14, "板块联动（同板块%d只涨停）" % sc
    else:
        s1, r1 = 8, "独苗（同板块仅1只涨停）"
    score += s1
    reasons.append(("板块联动", r1, s1))

    # 2) 连板位置 —— 20
    lbc = int(row.get("lbc") or 1)
    if lbc <= 1:
        s2, r2 = 15, "首板（早期，需其他因子确认）"
    elif lbc == 2:
        s2, r2 = 20, "二板（最佳确认区）"
    elif lbc == 3:
        s2, r2 = 18, "三板（妖性初显）"
    elif lbc == 4:
        s2, r2 = 13, "四板"
    elif lbc == 5:
        s2, r2 = 9, "五板"
    else:
        s2, r2 = 5, "%d板（高位明牌，风险大）" % lbc
    score += s2
    reasons.append(("连板位置", r2, s2))
    if yest_lbc is not None and lbc > yest_lbc:  # 晋级加成
        score += 3
        reasons.append(("晋级", "昨日%d板→今日%d板（连板高度继续打开）" % (yest_lbc, lbc), 3))

    # 3) 封单强度 —— 20（封单额/流通市值，资金锁仓坚决度）
    ltsz = (row.get("ltsz") or 0) / 1e8      # 亿
    fund = (row.get("fund") or 0) / 1e8      # 亿
    ratio = (fund / ltsz * 100) if ltsz else 0.0
    if ratio >= 2:
        s3, r3 = 20, "封单%.2f亿（流通盘%.2f%%，锁仓极坚决）" % (fund, ratio)
    elif ratio >= 1:
        s3, r3 = 15, "封单%.2f亿（流通盘%.2f%%）" % (fund, ratio)
    elif ratio >= 0.5:
        s3, r3 = 10, "封单%.2f亿（流通盘%.2f%%，一般）" % (fund, ratio)
    elif ratio >= 0.2:
        s3, r3 = 6, "封单%.2f亿（流通盘%.2f%%，偏弱）" % (fund, ratio)
    else:
        s3, r3 = 3, "封单%.2f亿（流通盘%.2f%%，偏弱）" % (fund, ratio)
    score += s3
    reasons.append(("封单强度", r3, s3))

    # 4) 流通盘 —— 12（适中最易炒作）
    if 20 <= ltsz <= 150:
        s4, r4 = 12, "流通市值%.0f亿（适中，易炒作）" % ltsz
    elif 150 < ltsz <= 300:
        s4, r4 = 9, "流通市值%.0f亿" % ltsz
    elif 300 < ltsz <= 600:
        s4, r4 = 6, "流通市值%.0f亿（偏大）" % ltsz
    elif ltsz < 20:
        s4, r4 = 8, "流通市值%.0f亿（小盘，易庄/流动性差）" % ltsz
    else:
        s4, r4 = 4, "流通市值%.0f亿（大票，难连续板）" % ltsz
    score += s4
    reasons.append(("流通盘", r4, s4))

    # 5) 换手 —— 10（充分换手筹码健康）
    hs = float(row.get("hs") or 0)
    if 8 <= hs <= 25:
        s5, r5 = 10, "换手%.1f%%（充分，筹码健康）" % hs
    elif 5 <= hs < 8 or 25 < hs <= 40:
        s5, r5 = 6, "换手%.1f%%" % hs
    elif hs < 5:
        s5, r5 = 4, "换手%.1f%%（一字锁死/惜售）" % hs
    else:
        s5, r5 = 2, "换手%.1f%%（过高，警惕出货）" % hs
    score += s5
    reasons.append(("换手", r5, s5))

    # 6) 封板时间/质量 —— 13（早盘秒板 > 上午 > 尾盘；炸板次数少质量高）
    fbt = str(row.get("fbt") or "")
    fbt = ("000000" + fbt)[-6:] if fbt.isdigit() else ""
    hh = int(fbt[:2]) if fbt else 15
    mm = int(fbt[2:4]) if fbt else 0
    if hh < 9 or (hh == 9 and mm <= 35):
        s6, r6 = 10, "早盘秒板(%s:%s，资金抢筹)" % (fbt[:2], fbt[2:4])
    elif hh <= 11:
        s6, r6 = 7, "上午封板(%s:%s)" % (fbt[:2], fbt[2:4])
    else:
        s6, r6 = 4, "尾盘封板(%s:%s)" % (fbt[:2], fbt[2:4])
    zbc = int(row.get("zbc") or 0)
    if zbc == 0:
        s6 += 3
        r6 += " · 一次封死(质量高)"
    elif zbc == 1:
        s6 += 1
        r6 += " · 炸板1次"
    else:
        pen = min(6, zbc * 2)
        s6 -= pen
        r6 += " · 炸板%d次(质量弱)" % zbc
    score += s6
    reasons.append(("封板质量", r6, s6))

    meta = {
        "lbc": lbc, "sector_count": sc, "fund_yi": round(fund, 2),
        "ltsz_yi": round(ltsz, 1), "ratio": round(ratio, 2), "hs": hs,
        "fbt": ("%s:%s" % (fbt[:2], fbt[2:4])) if fbt else "—",
        "zbc": zbc,
    }
    return round(score, 1), reasons, meta


def _board_tag(n):
    return "首板" if n <= 1 else ("%d板" % n)


def live_report(date=None, topn=12, with_yesterday=True):
    """实时妖股潜力榜。返回结构化 dict；涨停池为空返回 None。"""
    zt = em_api.zt_pool(date) or []
    if not zt:
        return None
    base = date or time.strftime("%Y%m%d")
    s_cnt = sector_strength(zt)

    # 晋级对照：取最近一个有数据的上一交易日连板数
    yest = {}
    if with_yesterday:
        for pd in _prev_days(base, 3):
            yz = em_api.zt_pool(pd) or []
            if yz:
                yest = {str(r.get("c")): int(r.get("lbc") or 1) for r in yz}
                break

    ranked = []
    for r in zt:
        sc, reasons, meta = yaogu_score(r, s_cnt.get(r.get("hybk") or "—", 1),
                                        yest.get(str(r.get("c"))))
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
    L.append("## 🔥 妖股潜力榜（实时涨停池 · 封单/板块/连板多维）")
    L.append("")
    L.append("> 评分 = 板块联动 + 连板位置 + 封单强度 + 流通盘 + 换手 + 封板质量（0~100）。")
    L.append("> 与「妖股基因(K线形态)」互补：本榜抓**实时资金+题材**维度，盘中/盘后双用。")
    L.append("")
    if rep.get("concept_top"):
        cs = "、".join("%s(%d)" % (c.get("name", "?"), c.get("up") or 0)
                      for c in rep["concept_top"][:6])
        L.append("**今日最强题材(概念涨停数)**：%s" % cs)
        L.append("")
    ladder_s = "  ".join("%s板×%d" % (k, len(v)) for k, v in rep["ladder"].items())
    L.append("**连板梯隊**：%s" % ladder_s)
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
