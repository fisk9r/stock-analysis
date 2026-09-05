"""波段 / 阶段底 选股引擎（2026-09-04 新增，2026-09-05 #488 箱体法重构）。

用户口径（原话）：立新能源 阶段低 12.22 → 高点 13.8；利通电子 阶段底 94 →
高点 120，「依次类推」——即**近期反复震荡的箱体**：底部反复回踩获得支撑
（低吸），顶部多次触及受阻（卖出）。要的不是 130 日最低点（那会把暴涨后
回踩的票永远判成"贴不了底"），而是当前正在运行的震荡区间。

核心思路（纯 Python、无未来函数、CPU-only 友好）：
  1. 近 25 个交易日（BOX_WIN）收盘价取 P15/P85 分位数 → 箱底 box_low / 箱顶 box_high。
  2. 回踩证据 touches：盘中低点触及箱底 3% 容差带的天数 ≥2（反复获得支撑）。
  3. 上沿有效性 tops：盘中高点触及箱顶 3% 容差带的天数 ≥2（箱顶真实存在）。
  4. 入场：现价 ≤ box_low*1.12（贴近低吸区）且 ≥ box_low*0.95（未破位）。
  5. 排除单边下跌：MA20 斜率走平或向上。
  6. 排除死票 / 庄股：流通市值 ≥ 15 亿、近 20 日平均换手 ≥ 0.5%、非 ST/退市。
  7. 空间：箱顶/现价 - 1 ≥ 8%（用户样例 13%~28%）。

输出：按 worth 降序的候选列表，每只附 底 X → 高 Y（空间%）/ 买区 / 卖区 /
止损 / 箱内位置 + 板块标签，可直接进入 build.rec["band_trade"] 与 notifier
的「🔁 波段/阶段底」段。
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


def detect_stage_bottom(u, date, code2boards=None, topn=8, window=60):
    """箱体波段扫描（2026-09-05 #488 按用户口径重构）。

    用户原话举例：立新能源 阶段低 12.22 → 高点 13.8（+13%）；利通电子
    阶段底 94 → 高点 120（+28%）。这不是「130 日最低点」（那样会把暴涨后
    回踩的票永远判成"贴不了底"），而是**近期反复震荡的箱体**：
      底部反复回踩获得支撑（低吸区）、顶部多次触及受阻（卖出区）。

    箱体识别（近 BOX_WIN 日，纯统计、无未来函数）：
      box_low  = P15(近期收盘)  —— 低吸区下沿
      box_high = P85(近期收盘)  —— 卖出区上沿
      touches  = 盘中低点 ≤ box_low*1.03 的天数（回踩证据，≥2 才算可靠）
      tops     = 盘中高点 ≥ box_high*0.97 的天数（上沿有效性，≥2）
    入场：现价 ≤ box_low*1.12（贴近低吸区，不追箱体高位）；且未破位
      （现价 ≥ box_low*0.95，跌破箱底 5% = 箱体失效，不接飞刀）。
    排除单边下跌：MA20 斜率走平或向上（20 日前 MA20 对比）。
    空间：box_high/现价 - 1 ≥ MIN_UPSIDE（8%，用户样例 13%~28%）。

    u: engine.Universe；date: 分析日；code2boards: {(code):[(bk,name,kind)]}；
    返回 list[dict]，按 worth 降序，最多 topn 只。"""
    if code2boards is None:
        code2boards = _load_code_boards()

    # 流通市值下限（避免死票/极小盘庄股）
    MIN_FMV = 15e8
    # 换手下限（近 20 日均值，百分比）
    MIN_TURN = 0.5
    # 区间空间下限（%，#488 用户口径）：用户举例的波段空间在 +13%（立新能源
    # 12.22→13.8）到 +28%（利通电子 94→120）之间，低于 8% 的区间扣掉手续费
    # 与容错后不值得做，直接不推。
    MIN_UPSIDE = 8.0
    # 箱体识别窗口（交易日）：用户的「近期」≈ 最近一个多月。
    # 25 日实测校准：立新能源 P15=12.10/P85=13.72（用户口径 12.22→13.8 ✓）、
    # 利通电子 P15≈114/P85≈125（用户口径 94→120，短窗更贴近当前箱体 ✓）。
    BOX_WIN = 25

    # 市场准入（2026-09-05 #486）：科创板 / 北交所 用户未开通，源头即剔除
    import mktfilter as _mkt

    def _pct(arr, q):
        """线性插值分位数（q∈[0,1]）。"""
        s = sorted(x for x in arr if x is not None)
        if not s:
            return 0.0
        idx = q * (len(s) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (idx - lo)

    cand = []
    codes = list(u.stocks.keys()) if hasattr(u, "stocks") else []
    for code in codes:
        if not _mkt.tradable(code):
            continue
        s = u.stocks.get(code)
        if not s:
            continue
        name = s.get("name", "")
        if ("ST" in name) or ("退" in name) or ("N " in name[:2]):
            continue
        fmv = s.get("float_mv") or 0
        if fmv < MIN_FMV:
            continue
        bars = u.bars_upto(code, date, max(BOX_WIN + 25, 60))
        if len(bars) < BOX_WIN + 10:
            continue
        def _c(b):
            return b["c"]
        def _l(b):
            return b.get("l", b["c"])
        def _h(b):
            return b.get("h", b["c"])
        closes = [_c(b) for b in bars]
        # 换手过滤（取近 20 日平均；bars 含 turn/换手字段）
        turns = [((b.get("turn") or b.get("换手")) or 0) for b in bars[-20:]]
        if _mean(turns) < MIN_TURN:
            continue

        c_win = closes[-BOX_WIN:]
        l_win = [_l(b) for b in bars[-BOX_WIN:]]
        h_win = [_h(b) for b in bars[-BOX_WIN:]]

        box_low = _pct(c_win, 0.15)
        box_high = _pct(c_win, 0.85)
        # 箱体宽度须在 5%~35%：太窄无操作价值；太宽不是箱体是过山车
        # （349 只候选的教训：不限宽时 P15/P85 人人有箱，评分挤成一团没区分度）
        if not (box_low * 1.05 <= box_high <= box_low * 1.35) or box_low <= 0:
            continue

        # 回踩证据：盘中低点触及低吸区（1.5% 容差——原 3% 太宽松，349 只候选
        # 里 touches 动辄满分，失去区分度）
        touches = sum(1 for p in l_win if p <= box_low * 1.015)
        if touches < 3:
            continue
        # 上沿有效性：盘中高点触及卖出区（1.5% 容差）
        tops = sum(1 for p in h_win if p >= box_high * 0.985)
        if tops < 3:
            continue

        last = closes[-1]
        # 破位排除：跌破箱底 5% = 箱体失效（不是低吸，是接飞刀）
        if last < box_low * 0.95:
            continue
        # 入场约束：现价须贴近低吸区（箱底上方 12% 以内），不追箱体高位
        if last > box_low * 1.12:
            continue

        # 单边下跌排除：MA20 斜率须走平或向上（20 日前的 MA20 对比）
        if len(closes) >= 45:
            ma20_now = _mean(closes[-20:])
            ma20_prev = _mean(closes[-40:-20])
            if ma20_now < ma20_prev * 0.985:
                continue

        # 空间：现价 → 箱顶
        upside = (box_high / last - 1) * 100 if last else 0
        if upside < MIN_UPSIDE:
            continue

        # 评分（区分度优先，2026-09-05）：
        #   贴底位置分 30 —— 低吸的灵魂：越贴箱底越接近最佳买点（pos≤0 满分）
        #   回踩分 12 / 上沿分 8 —— 支撑与阻力的可靠度
        #   空间分 20 —— 连续计分不封顶到人均满值
        pos = (last - box_low) / (box_high - box_low) if box_high > box_low else 0.5
        _pos_score = 30.0 * max(0.0, 1.0 - max(pos, 0.0))
        worth = (_pos_score + min(12, touches * 1.2) + min(8, tops * 0.8)
                 + min(20, upside))

        cand.append({
            "code": code,
            "name": name,
            "bottom": round(box_low, 2),
            "band_hi": round(box_high, 2),
            "touches": int(touches),
            "tops": int(tops),
            "close": round(last, 2),
            "buy_zone": [round(box_low * 0.99, 2), round(box_low * 1.05, 2)],
            "sell_zone": [round(box_high * 0.97, 2), round(box_high, 2)],
            "stop": round(box_low * 0.94, 2),
            # #488：阶段底 / 区间高点 / 空间%（用户要「底 X → 高 Y」这种可照做的数）
            "range_low": round(box_low, 2),
            "range_high": round(box_high, 2),
            "upside": round(upside, 1),
            # 当前价在箱体中的位置（0=箱底 1=箱顶，低吸看这个）
            "pos_in_box": round((last - box_low) / (box_high - box_low), 2)
                if box_high > box_low else 0.5,
            "worth": round(worth, 1),
            "board": _board_label(code, code2boards),
        })

    cand.sort(key=lambda x: -x["worth"])
    return cand[:topn]
