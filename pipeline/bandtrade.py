"""波段 / 阶段底 选股引擎（2026-09-04 新增）。

用户需求：立新能源 / 开开实业 / 中瓷电子 这类「非主升浪、但反复形成阶段底、反反复复」的票，
也可以作为选股依据——回到阶段底、历史多次在相近价位获得支撑并反弹时，是低吸做波段的好买点。

核心思路（纯 Python、无未来函数、CPU-only 友好）：
  1. 取该股近 ~130 个交易日日K，识别「局部低点（trough）= 低于前一日且 ≤ 后一日、且跌破 20 日均线 ≥3%」。
  2. 以全部低点中的最低点定义「阶段底」bottom，底部带 = [bottom, bottom*1.10]。
  3. 落在该底部带的低点次数 = touches（「反反复复」的量化证据）；要求 touches ≥ 2（至少回踩两次才叫可靠阶段底）。
  4. 当前价必须「回到阶段底附近」：last ≤ bottom*1.12（贴近底，不是飞在天上），且
     从最近一个低点反弹幅度在 (0, 16%]（刚启动、未走完，才是低吸点，不是追高）。
  5. 排除下跌趋势自由落体：最近两个低点不能「新低大幅低于前低」（last_low ≥ prev_low*0.93），
     即做底而非破位。
  6. 排除死票 / 庄股：流通市值 ≥ 15 亿、近 20 日平均换手 ≥ 0.5%、非 ST/退市。
  7. 非主升浪：bounce 上限 16% 已天然排除「刚从底爆拉的主升浪」。

输出：按 worth 降序的候选列表，每只附 买区(近底)/卖区(区间上沿)/止损 + 板块标签，
可直接进入 build.rec["band_trade"] 与 notifier 的「🔁 波段/阶段底」段。
"""

import os

_GENERIC = ("昨日", "连板", "涨停", "融资融券", "深股通", "沪股通", "标准普尔", "富时",
            "MSCI", "转融券", "机构重仓", "基金重仓", "预盈预增", "创业板综", "深成500",
            "中证", "上证", "破净股", "参股", "股权激励", "高送转", "AB股", "AH股",
            "昨曾涨停", "证金持股", "社保重仓", "QFII重仓", "长江三角", "西部大开发",
            "央国企改革", "国企改革", "中字头", "转债标的", "MSCI中国", "富时罗素",
            "标普道琼斯", "中证500", "沪深300", "上证50", "深证成指", "创业板指",
            "科创50", "北证50", "ST股", "深股通标的", "沪股通标的", "星闪概念",
            "养老金持股", "险资持股", "GDR", "股权转让", "同花顺", "数据中心",
            "HS300", "上证180", "上证380", "深证100", "央视50", "上证红利", "深证红利",
            "标普", "MSCI中国", "中证红利", "上证380", "沪深300", "上证180", "深证100R")


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def _board_label(code, code2boards):
    """返回该票代表性板块短名（优先非泛用概念，回退行业）。"""
    boards = (code2boards or {}).get(str(code)) or []
    if not boards:
        return "—"
    for _, name, kind in boards:
        if name.endswith("_"):   # 如 HS300_ 这类指数成分残名，跳过
            continue
        if kind == "concept" and not any(g in name for g in _GENERIC):
            return name
    for _, name, kind in boards:
        if kind == "industry":
            return name
    return boards[0][1]


def _load_code_boards():
    try:
        import sqlite3
        here = os.path.dirname(os.path.abspath(__file__))
        db = os.path.join(here, "..", "cache", "market.db")
        if not os.path.exists(db):
            return {}
        import store
        con = sqlite3.connect(db)
        c2b = store.code_boards(con)
        con.close()
        return c2b
    except Exception:
        return {}


def detect_stage_bottom(u, date, code2boards=None, topn=8, window=130):
    """扫描全市场，返回「当前回到阶段底、历史反复获得支撑、刚启动反弹」的波段候选。

    u: engine.Universe；date: 分析日；code2boards: {(code):[(bk,name,kind)]}；
    返回 list[dict]，按 worth 降序，最多 topn 只。"""
    if code2boards is None:
        code2boards = _load_code_boards()

    # 流通市值下限（避免死票/极小盘庄股）
    MIN_FMV = 15e8
    # 换手下限（近 20 日均值，百分比）
    MIN_TURN = 0.5

    cand = []
    codes = list(u.stocks.keys()) if hasattr(u, "stocks") else []
    for code in codes:
        s = u.stocks.get(code)
        if not s:
            continue
        name = s.get("name", "")
        if ("ST" in name) or ("退" in name) or ("N " in name[:2]):
            continue
        fmv = s.get("float_mv") or 0
        if fmv < MIN_FMV:
            continue
        bars = u.bars_upto(code, date, window)
        if len(bars) < 90:
            continue
        # 字段兼容：bars 元素可能是 dict（engine）或 tuple；这里按 dict 处理
        def _c(b):
            return b["c"]
        def _l(b):
            return b.get("l", b["c"])
        closes = [_c(b) for b in bars]
        # 换手过滤（取近 20 日平均；bars 含 turn/换手字段）
        turns = [((b.get("turn") or b.get("换手")) or 0) for b in bars[-20:]]
        if _mean(turns) < MIN_TURN:
            continue

        n = len(closes)
        # 识别局部低点
        troughs = []
        for i in range(4, n - 4):
            if closes[i] < closes[i - 1] and closes[i] <= closes[i + 1]:
                ma20 = _mean(closes[i - 20:i]) if i >= 20 else None
                if ma20 and closes[i] < ma20 * 0.97:
                    troughs.append((i, closes[i]))
        if len(troughs) < 2:
            continue

        lowest_i, lowest = min(troughs, key=lambda t: t[1])
        band_hi = lowest * 1.10
        touches = sum(1 for (_, p) in troughs if lowest <= p <= band_hi)
        if touches < 2:
            continue

        last = closes[-1]
        # 当前需回到阶段底附近（贴近底，不是飞在天上）
        if last > lowest * 1.12:
            continue

        # 下跌趋势排除：最近两个低点不能大幅新低
        sorted_t = sorted(troughs, key=lambda t: t[0])
        if len(sorted_t) >= 2:
            prev_p = sorted_t[-2][1]
            last_p = sorted_t[-1][1]
            if last_p < prev_p * 0.93:
                continue

        # 反弹确认：从最近低点起涨幅在 (0, 16%]
        last_trough_p = sorted_t[-1][1]
        bounce = (last / last_trough_p - 1) * 100
        if not (0 < bounce <= 16):
            continue

        # 卖点参考：区间上沿（除底部带外的最高收盘）
        above = [p for p in closes if p > band_hi]
        sell_ref = max(above) if above else last
        sell_ref = max(sell_ref, last)

        # 评分：触碰次数（反反复复的可靠度）为主，刚启动（bounce 小）加分
        worth = touches * 10 + (8 if bounce <= 8 else 4) + min(6, int(bounce))

        cand.append({
            "code": code,
            "name": name,
            "bottom": round(lowest, 2),
            "band_hi": round(band_hi, 2),
            "touches": int(touches),
            "bounce": round(bounce, 1),
            "close": round(last, 2),
            "buy_zone": [round(lowest * 0.97, 2), round(lowest * 1.05, 2)],
            "sell_zone": [round(sell_ref * 0.96, 2), round(sell_ref, 2)],
            "stop": round(lowest * 0.92, 2),
            "worth": round(worth, 1),
            "board": _board_label(code, code2boards),
        })

    cand.sort(key=lambda x: -x["worth"])
    return cand[:topn]
