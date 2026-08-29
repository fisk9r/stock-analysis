# -*- coding: utf-8 -*-
"""分析引擎：涨停基因库 / 板块热力 / 断板概率 / 情绪周期 / 妖股形态 / 当日推荐"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store

# ============================================================== 基础规则
def limit_pct(code, name):
    """涨跌停幅度(%)"""
    n = (name or "").upper().replace(" ", "")
    is_st = ("ST" in n)
    if code.startswith("688") or code.startswith("30"):
        return 20.0
    if code.startswith(("8", "4")) or code.startswith("920"):
        return 30.0
    return 5.0 if is_st else 10.0


def is_limit_up(bar, lim):
    return bar["c"] >= bar["h"] - 1e-6 and bar["pct"] >= lim - 0.6


def is_limit_down(bar, lim):
    return bar["c"] <= bar["l"] + 1e-6 and bar["pct"] <= -(lim - 0.6)


def is_yiziban(bar, lim):
    return (abs(bar["o"] - bar["c"]) < 1e-6 and abs(bar["h"] - bar["l"]) < 1e-6
            and bar["pct"] >= lim - 0.6)


def is_zhaban(bar, lim):
    """炸板：盘中最高价触及涨停价（high 涨幅 >= lim-0.6）但收盘未能封板。"""
    if is_limit_up(bar, lim):
        return False
    c = bar.get("c")
    if not c:
        return False
    high_pct = bar["pct"] + (bar["h"] - c) / c * 100.0
    return high_pct >= lim - 0.6


def lu_shape(bar, lim, pc, api_zb=None):
    """涨停形态分类（基于单根日K + 可选当日炸板次数）。返回：
    一字板 / 地天板 / T字板 / 烂板 / 换手板。

    - 一字板：开盘即涨停且全天无成交区间（o==h==l==c）。
    - 地天板：盘中最低触及跌停（-lim）却最终封死涨停（极致弱转强）。
    - T字板：开盘即涨停，盘中开板下探后回封（带实体下影）。
    - 烂板：封板不稳——当日接口给出炸板次数（api_zb>0，金标准），或日K重建显示
           盘中较开盘大幅回撤（>40% 涨幅区间）后回封。
    - 换手板：正常换手封板（开盘低于涨停、回撤有限、健康封住），默认形态。
    """
    if is_yiziban(bar, lim):
        return "一字板"
    c = bar["c"]; o = bar["o"]; h = bar["h"]; l = bar["l"]
    o_pct = (o - pc) / pc * 100.0 if pc else 0.0
    l_pct = (l - pc) / pc * 100.0 if pc else 0.0
    # 地天：盘中最低价触及跌停
    if l_pct <= -(lim - 0.6):
        return "地天板"
    # 金标准：当日接口给出炸板次数 -> 烂板
    if api_zb:
        return "烂板"
    # T字：开盘即涨停，盘中开板下探后回封
    if o_pct >= lim - 0.6:
        return "T字板"
    # 烂板（日K重建近似）：开盘低于涨停，但盘中较开盘大幅回撤后回封
    dip = (l - o) / o * 100.0 if o else 0.0
    if dip <= -lim * 0.4:
        return "烂板"
    return "换手板"


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def parse_hms(s):
    """把 '093001' / 93006 / '09:30:01' 等解析为『距 09:30 的分钟数』；失败返回 None。
    用于稳健读取涨停池的 fbt 首封时间（接口偶发返回带冒号字符串）。"""
    if s is None:
        return None
    try:
        digits = ''.join(ch for ch in str(s) if ch.isdigit())
    except Exception:
        return None
    if len(digits) < 3:
        return None
    if len(digits) > 6:
        digits = digits[-6:]
    digits = digits.zfill(6)
    try:
        hh, mm = int(digits[:2]), int(digits[2:4])
    except ValueError:
        return None
    if hh > 23 or mm > 59:
        return None
    return hh * 60 + mm - 570


def lerp_score(v, lo, mid, hi):
    """把 v 线性映射到 0-100，lo->0 mid->50 hi->100"""
    if v is None:
        return 50.0
    if v <= lo:
        return 0.0
    if v >= hi:
        return 100.0
    if v <= mid:
        return (v - lo) / max(1e-9, mid - lo) * 50.0
    return 50.0 + (v - mid) / max(1e-9, hi - mid) * 50.0


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def pearson(a, b):
    n = min(len(a), len(b))
    if n < 5:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 1e-12 or vb <= 1e-12:
        return 0.0
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


def resample(seq, n):
    """把任意长度序列线性重采样为 n 点"""
    if not seq:
        return [0.0] * n
    if len(seq) == n:
        return list(seq)
    out = []
    for i in range(n):
        pos = i * (len(seq) - 1) / max(1, n - 1)
        lo = int(math.floor(pos))
        hi = min(len(seq) - 1, lo + 1)
        w = pos - lo
        out.append(seq[lo] * (1 - w) + seq[hi] * w)
    return out


# ============================================================== 1. 涨停基因库
class Universe(object):
    """从本地日K库重建全市场涨停/连板历史"""

    def __init__(self, con, days=270):
        self.con = con
        self.stocks = {}
        for code, market, name, tmv, fmv in con.execute(
                "SELECT code,market,name,total_mv,float_mv FROM stocks"):
            self.stocks[code] = {"code": code, "market": market, "name": name,
                                 "total_mv": tmv, "float_mv": fmv}
        self.dates = store.trade_dates(con, days)
        since = self.dates[0] if self.dates else None
        self.bars = store.load_bars(con, since=since)
        # 按日索引：date -> [(code, bar)]，供 market_breadth / 情绪序列 O(当日股票数) 查询
        self.by_date = {}
        for code, bs in self.bars.items():
            for b in bs:
                self.by_date.setdefault(b["d"], []).append((code, b))
        self.lim = {c: limit_pct(c, s["name"]) for c, s in self.stocks.items()}
        self.di = {d: i for i, d in enumerate(self.dates)}
        self._build()

    def _build(self):
        self.zt = {d: set() for d in self.dates}      # 当日涨停
        self.dt = {d: set() for d in self.dates}      # 当日跌停
        self.zhaban = {d: set() for d in self.dates}  # 当日炸板（触涨停未封住）
        self.streak = {}                              # code -> {date: 连板数}
        self.flags = {}                               # code -> {date: {'yizi':bool}}
        for code, bs in self.bars.items():
            lim = self.lim.get(code)
            if lim is None:
                continue
            st, sd, fl = 0, {}, {}
            for b in bs:
                d = b["d"]
                if d not in self.di:
                    continue
                if is_limit_up(b, lim):
                    st += 1
                    self.zt[d].add(code)
                    fl[d] = {"yizi": is_yiziban(b, lim)}
                else:
                    st = 0
                    if is_limit_down(b, lim):
                        self.dt[d].add(code)
                    if is_zhaban(b, lim):              # 触涨停却未封住 = 炸板
                        self.zhaban[d].add(code)
                sd[d] = st
            self.streak[code] = sd
            self.flags[code] = fl

    def bar(self, code, date):
        for b in self.bars.get(code, []):
            if b["d"] == date:
                return b
        return None

    def bars_upto(self, code, date, n=60):
        bs = [b for b in self.bars.get(code, []) if b["d"] <= date]
        return bs[-n:]

    def next_date(self, date):
        i = self.di.get(date)
        if i is None or i + 1 >= len(self.dates):
            return None
        return self.dates[i + 1]

    def prev_date(self, date, k=1):
        i = self.di.get(date)
        if i is None or i - k < 0:
            return None
        return self.dates[i - k]


# ============================================================== 2. 连板晋级率统计
def streak_statistics(u, lookback=120):
    """真实历史统计：n 连板 -> 次日晋级率 / 次日开盘溢价 / 次日收盘涨幅"""
    dates = u.dates[-lookback:]
    acc = {}
    for d in dates[:-1]:
        nd = u.next_date(d)
        if nd is None:
            continue
        for code in u.zt[d]:
            n = u.streak[code].get(d, 0)
            if n < 1 or n > 12:
                continue
            nb = u.bar(code, nd)
            pb = u.bar(code, d)
            if not nb or not pb or not pb["c"]:
                continue
            a = acc.setdefault(n, {"total": 0, "promote": 0, "opens": [], "closes": [],
                                   "highs": [], "green": 0, "limitdown": 0})
            a["total"] += 1
            if code in u.zt[nd]:
                a["promote"] += 1
            a["opens"].append((nb["o"] - pb["c"]) / pb["c"] * 100.0)
            a["closes"].append(nb["pct"])
            a["highs"].append((nb["h"] - pb["c"]) / pb["c"] * 100.0)
            if nb["pct"] < 0:
                a["green"] += 1
            if code in u.dt[nd]:
                a["limitdown"] += 1
    out = {}
    for n, a in sorted(acc.items()):
        if a["total"] < 5:
            continue
        out[n] = {
            "samples": a["total"],
            "promote_rate": round(a["promote"] / a["total"] * 100, 2),
            "avg_open": round(mean(a["opens"]), 2),
            "avg_close": round(mean(a["closes"]), 2),
            "avg_high": round(mean(a["highs"]), 2),
            "green_rate": round(a["green"] / a["total"] * 100, 2),
            "limitdown_rate": round(a["limitdown"] / a["total"] * 100, 2),
        }
    return out


# ============================================================== 2.5 炸板（未回封）历史规律
def zhaban_statistics(u, lookback=120):
    """历史规律：炸板（触及涨停却未封住）个股的次日表现统计。
    量化『分歧/派发』的延续性——炸板后次日多高开低走、收绿，还是反包涨停。
    全部由日K重建，无需盘中逐笔，可作为市场分歧度与次日风险的经验参照。"""
    dates = u.dates[-lookback:]
    a = {"total": 0, "next_close": [], "next_open": [], "next_high": [],
         "green": 0, "limitup": 0, "limitdown": 0, "strong": 0}
    for d in dates[:-1]:
        nd = u.next_date(d)
        if nd is None:
            continue
        zset = u.zhaban.get(d, set())
        if not zset:
            continue
        nb_all = u.zt.get(nd, set())
        dt_all = u.dt.get(nd, set())
        for code in zset:
            b = u.bar(code, d)
            nb = u.bar(code, nd)
            if not b or not nb or not b["c"]:
                continue
            a["total"] += 1
            a["next_close"].append(nb["pct"])
            a["next_open"].append((nb["o"] - b["c"]) / b["c"] * 100.0)
            a["next_high"].append((nb["h"] - b["c"]) / b["c"] * 100.0)
            if nb["pct"] < 0:
                a["green"] += 1
            if code in nb_all:
                a["limitup"] += 1
            if code in dt_all:
                a["limitdown"] += 1
            if nb["pct"] >= 3:
                a["strong"] += 1
    if a["total"] < 10:
        return None
    return {
        "samples": a["total"],
        "avg_next_open": round(mean(a["next_open"]), 2),
        "avg_next_close": round(mean(a["next_close"]), 2),
        "avg_next_high": round(mean(a["next_high"]), 2),
        "green_rate": round(a["green"] / a["total"] * 100, 2),
        "limitup_rate": round(a["limitup"] / a["total"] * 100, 2),
        "limitdown_rate": round(a["limitdown"] / a["total"] * 100, 2),
        "strong_rate": round(a["strong"] / a["total"] * 100, 2),
    }


def limit_up_pattern_stats(u, lookback=120):
    """历史规律：各涨停形态（一字/地天/T字/烂板/换手）次日表现统计。
    量化『封板质量/形态』对次日延续性的影响——哪种形态次日最易连板、哪种最易收绿。
    全部由日K重建且自带形态分类，可作为连板博弈与分歧度的经验参照。"""
    dates = u.dates[-lookback:]
    agg = {}
    for d in dates[:-1]:
        nd = u.next_date(d)
        if nd is None:
            continue
        for code in u.zt.get(d, ()):
            b = u.bar(code, d)
            nb = u.bar(code, nd)
            if not b or not nb or not b["c"]:
                continue
            lim = limit_pct(code, "")
            if not is_limit_up(b, lim):
                continue
            pc = (b["c"] - b["chg"]) if (b["c"] - b["chg"]) > 0 else b["c"]
            shp = lu_shape(b, lim, pc)
            a = agg.setdefault(shp, {"samples": 0, "next_open": [], "next_close": [],
                                     "limitup": 0, "green": 0, "strong": 0})
            a["samples"] += 1
            a["next_close"].append(nb["pct"])
            a["next_open"].append((nb["o"] - b["c"]) / b["c"] * 100.0)
            if code in u.zt.get(nd, ()):
                a["limitup"] += 1
            if nb["pct"] < 0:
                a["green"] += 1
            if nb["pct"] >= 3:
                a["strong"] += 1
    out = {}
    for shp, a in agg.items():
        if a["samples"] < 8:
            continue
        out[shp] = {
            "samples": a["samples"],
            "avg_next_open": round(mean(a["next_open"]), 2),
            "avg_next_close": round(mean(a["next_close"]), 2),
            "limitup_rate": round(a["limitup"] / a["samples"] * 100, 2),
            "green_rate": round(a["green"] / a["samples"] * 100, 2),
            "strong_rate": round(a["strong"] / a["samples"] * 100, 2),
        }
    return out


# ============================================================== 3. 当日涨停画像
def build_limit_ups(u, date, snap, code2boards, snap_is_same_day):
    """合并 K 线重建结果 + 当日涨停池 API 的封板细节"""
    ztapi = {}
    if snap_is_same_day:
        for r in snap.get("zt") or []:
            ztapi[str(r.get("c"))] = r
    rows = []
    pd = u.prev_date(date)
    for code in sorted(u.zt.get(date, [])):
        s = u.stocks.get(code)
        if not s:
            continue
        b = u.bar(code, date)
        if not b:
            continue
        api_r = ztapi.get(code) or {}
        streak = u.streak[code].get(date, 1)
        bs = u.bars_upto(code, date, 60)
        vol20 = mean([x["v"] for x in bs[-21:-1]]) if len(bs) > 5 else b["v"]
        # 近 60 日涨停次数 / 历史最高连板
        zt60 = sum(1 for x in bs if code in u.zt.get(x["d"], ()))
        hist_max = 0
        for x in bs:
            hist_max = max(hist_max, u.streak[code].get(x["d"], 0))
        # 区间涨幅
        gain20 = ((b["c"] / bs[-21]["c"] - 1) * 100) if len(bs) >= 21 else None
        gain60 = ((b["c"] / bs[0]["c"] - 1) * 100) if len(bs) >= 40 else None
        boards = code2boards.get(code) or []
        ind = next((n for _, n, k in boards if k == "industry"), api_r.get("hybk") or "其他")
        cons = [n for _, n, k in boards if k == "concept"]
        seal_time = api_r.get("fbt")
        pc = (b["c"] - b["chg"]) if (b["c"] - b["chg"]) > 0 else b["c"]
        rows.append({
            "code": code, "name": s["name"], "streak": streak,
            "close": b["c"], "pct": round(b["pct"], 2), "turn": round(b["turn"] or 0, 2),
            "amount": b["amt"], "vol_ratio": round((b["v"] / vol20) if vol20 else 1, 2),
            "float_mv": s.get("float_mv"), "total_mv": s.get("total_mv"),
            "yizi": bool((u.flags.get(code, {}).get(date) or {}).get("yizi")),
            "seal_time": seal_time, "seal_fund": api_r.get("fund"),
            "zb_count": api_r.get("zbc"), "industry": ind, "concepts": cons[:6],
            "zt60": zt60, "hist_max_streak": hist_max,
            "gain20": round(gain20, 1) if gain20 is not None else None,
            "gain60": round(gain60, 1) if gain60 is not None else None,
            "open_pct": (round((b["o"] / pc - 1) * 100, 2) if pc and pc > 0 else None),
            "amp": round(b["amp"] or 0, 2),
            "lu_shape": lu_shape(b, limit_pct(code, s["name"]), pc, api_zb=api_r.get("zbc")),
            "prev_date": pd,
        })
    for r in rows:
        r["quality"] = seal_quality(r)
    rows.sort(key=lambda x: (-x["streak"], -x["quality"]))
    return rows


def seal_quality(r):
    """封板质量分 0-100：一字/早封/无炸板/大封单/合理换手 = 强"""
    parts = []
    # 首封时间：越早越强（HHMMSS 整数）
    st = r.get("seal_time")
    mins = parse_hms(st)
    if mins is not None:
        parts.append(("封板时间", lerp_score(-mins, -240, -60, 0), 0.22))
    else:
        parts.append(("形态强度", 92.0 if r["yizi"] else 62.0, 0.22))
    # 炸板次数
    zb = r.get("zb_count")
    if zb is not None:
        parts.append(("封板稳定", 100.0 if zb == 0 else max(0.0, 100 - zb * 32.0), 0.18))
    else:
        parts.append(("封板稳定", 88.0 if r["yizi"] else 66.0, 0.18))
    # 封单强度
    fund, fmv = r.get("seal_fund"), r.get("float_mv")
    if fund and fmv:
        parts.append(("封单强度", lerp_score(fund / fmv * 100, 0.2, 2.0, 8.0), 0.20))
    else:
        parts.append(("量能强度", lerp_score(r.get("vol_ratio") or 1, 0.4, 1.6, 4.0), 0.20))
    # 换手：一字板低换手极强；普通板 5-20% 健康，过高分歧
    t = r.get("turn") or 0
    if r["yizi"]:
        tscore = lerp_score(-t, -12, -3, 0)
    else:
        tscore = 100 - abs(t - 13) * 3.6
    parts.append(("换手健康", clamp(tscore, 0, 100), 0.18))
    # 市值：小市值更容易走妖
    fmv_yi = (fmv or 0) / 1e8
    parts.append(("市值弹性", lerp_score(-fmv_yi, -300, -80, -15), 0.12))
    # 连板高度本身
    parts.append(("连板高度", lerp_score(r["streak"], 0, 3, 7), 0.10))
    r["quality_parts"] = [{"k": k, "v": round(v, 1)} for k, v, _ in parts]
    return round(sum(v * w for _, v, w in parts), 1)


# ============================================================== 4. 板块热力
def sector_heat(limit_ups, snap, u, date):
    ind_pct = {b["name"]: b for b in (snap.get("board_industry") or [])}
    con_pct = {b["name"]: b for b in (snap.get("board_concept") or [])}
    # --- 行业维度
    agg = {}
    for r in limit_ups:
        a = agg.setdefault(r["industry"], {"name": r["industry"], "kind": "industry",
                                           "stocks": [], "zt": 0, "lb": 0, "max_lb": 0})
        a["stocks"].append(r)
        a["zt"] += 1
        if r["streak"] >= 2:
            a["lb"] += 1
        a["max_lb"] = max(a["max_lb"], r["streak"])
    # --- 概念维度
    cagg = {}
    for r in limit_ups:
        for c in r["concepts"]:
            if any(k in c for k in ("昨日", "连板", "涨停", "融资融券", "深股通", "沪股通",
                                    "标准普尔", "富时", "MSCI", "转融券", "机构重仓",
                                    "基金重仓", "预盈预增", "创业板综", "深成500", "中证",
                                    "上证", "破净股", "参股", "股权激励", "高送转")):
                continue
            a = cagg.setdefault(c, {"name": c, "kind": "concept", "stocks": [],
                                    "zt": 0, "lb": 0, "max_lb": 0})
            a["stocks"].append(r)
            a["zt"] += 1
            if r["streak"] >= 2:
                a["lb"] += 1
            a["max_lb"] = max(a["max_lb"], r["streak"])

    def finish(a, ref):
        m = ref.get(a["name"]) or {}
        a["pct"] = m.get("pct")
        a["main_net"] = m.get("main_net")
        a["lead"] = m.get("lead")
        a["stocks"] = sorted(a["stocks"], key=lambda x: (-x["streak"], -x["quality"]))
        a["top"] = [{"code": s["code"], "name": s["name"], "streak": s["streak"],
                     "quality": s["quality"]} for s in a["stocks"][:8]]
        # 强度分：涨停家数 + 连板家数 + 高度 + 板块涨幅 + 资金
        s1 = lerp_score(a["zt"], 0, 4, 12)
        s2 = lerp_score(a["lb"], 0, 2, 6)
        s3 = lerp_score(a["max_lb"], 1, 3, 6)
        s4 = lerp_score(a["pct"] if a["pct"] is not None else 0, -1, 2, 6)
        s5 = lerp_score((a["main_net"] or 0) / 1e8, -8, 2, 20)
        a["strength"] = round(s1 * .30 + s2 * .22 + s3 * .20 + s4 * .16 + s5 * .12, 1)
        a["avg_quality"] = round(mean([s["quality"] for s in a["stocks"]]), 1)
        del a["stocks"]
        return a

    inds = sorted([finish(a, ind_pct) for a in agg.values()],
                  key=lambda x: -x["strength"])
    cons = sorted([finish(a, con_pct) for a in cagg.values() if a["zt"] >= 2],
                  key=lambda x: -x["strength"])
    for lst in (inds, cons):
        for a in lst:
            if a["zt"] >= 5 and a["max_lb"] >= 2:
                a["tier"] = "主线"
            elif a["zt"] >= 3 or (a["zt"] >= 2 and a["max_lb"] >= 3):
                a["tier"] = "支线"
            else:
                a["tier"] = "零星"
    return inds, cons[:24]


# ============================================================== 5. 市场情绪 & 周期
def market_breadth(u, date):
    up = dn = flat = 0
    amt = 0.0
    for _code, b in u.by_date.get(date, []):
        amt += b["amt"] or 0
        if b["pct"] > 0.01:
            up += 1
        elif b["pct"] < -0.01:
            dn += 1
        else:
            flat += 1
    return {"up": up, "down": dn, "flat": flat, "amount": amt}


def daily_emotion(u, date):
    """单日情绪快照（可用于历史序列）"""
    pd = u.prev_date(date)
    zt = u.zt.get(date, set())
    dt = u.dt.get(date, set())
    zb = u.zhaban.get(date, set())
    prev_zt = u.zt.get(pd, set()) if pd else set()
    # 昨日涨停今日表现
    perf, green, again = [], 0, 0
    for code in prev_zt:
        b = u.bar(code, date)
        if not b:
            continue
        perf.append(b["pct"])
        if b["pct"] < 0:
            green += 1
        if code in zt:
            again += 1
    lb = [c for c in zt if u.streak[c].get(date, 0) >= 2]
    maxlb = max([u.streak[c].get(date, 0) for c in zt], default=0)
    br = market_breadth(u, date)
    bh = benchmark_heat(u, date)   # 市场热度（以标杆股交易额度为核心）
    return {
        "date": date, "zt": len(zt), "dt": len(dt), "zb": len(zb), "lb": len(lb), "max_lb": maxlb,
        "promote_rate": round(again / len(prev_zt) * 100, 1) if prev_zt else None,
        "yest_perf": round(mean(perf), 2) if perf else None,
        "yest_green": round(green / len(perf) * 100, 1) if perf else None,
        "up": br["up"], "down": br["down"], "amount": br["amount"],
        "bench_heat_level": bh["level"], "bench_amt_ratio": bh["avg_amt_ratio"],
        "bench_share_trending": bh["share_trending"], "bench_total_amt": bh["total_amt"],
        "bench_avg_daily": bh["avg_daily"],
    }


def emotion_series(u, n=30):
    ds = u.dates[-n:]
    return [daily_emotion(u, d) for d in ds]


# ============================================================== 5.5 短线情绪微观结构
def microstructure(u, date, lus, snap, code2boards, same_day):
    """纯计算「短线情绪微观结构」（对标 vibe-astock / aiagents-stock 最热指标组）：
    首板分析、连板梯队断层、晋级率分档(1进2/2进3/3板+)、炸板率、
    赚钱效应细分(翻红率/再涨停率/平均涨幅/翻绿率)。全部由 130 天 K 线库重建，不依赖当日快照。"""
    pd = u.prev_date(date)
    zt = u.zt.get(date, set())
    zb = u.zhaban.get(date, set())
    # 首板（streak==1）
    first = [r for r in lus if r.get("streak") == 1]
    fb_shapes = {}
    for r in first:
        sh = r.get("lu_shape")
        if sh:
            fb_shapes[sh] = fb_shapes.get(sh, 0) + 1
    # 连板梯队分布 + 断层检测
    ladder_dist = {}
    for r in lus:
        ladder_dist[r["streak"]] = ladder_dist.get(r["streak"], 0) + 1
    maxlb = max(ladder_dist, default=0)
    gaps = [lv for lv in range(2, maxlb + 1) if lv not in ladder_dist]  # 1板恒有，从2板起看断档
    # 晋级率分档：昨日涨停股今日各档晋级（再涨停且连板+1）
    prev_zt = u.zt.get(pd, set()) if pd else set()
    tiered = {"1进2": [0, 0], "2进3": [0, 0], "3板及以上": [0, 0]}
    for code in prev_zt:
        pstreak = u.streak[code].get(pd, 1)
        today_in_zt = code in zt
        tstreak = u.streak[code].get(date, 0)
        if pstreak == 1:
            tiered["1进2"][1] += 1
            if today_in_zt and tstreak >= 2:
                tiered["1进2"][0] += 1
        elif pstreak == 2:
            tiered["2进3"][1] += 1
            if today_in_zt and tstreak >= 3:
                tiered["2进3"][0] += 1
        elif pstreak >= 3:
            tiered["3板及以上"][1] += 1
            if today_in_zt and tstreak >= pstreak + 1:
                tiered["3板及以上"][0] += 1
    promote_tiered = {}
    for k, (a, b) in tiered.items():
        promote_tiered[k] = round(a / b * 100, 1) if b else None
    # 炸板率 / 封板率
    denom = len(zt) + len(zb)
    seal_rate = round(len(zt) / denom * 100, 1) if denom > 0 else None
    zhaban_rate = round(len(zb) / denom * 100, 1) if denom > 0 else None
    # 赚钱效应细分（翻红率/再涨停率/平均涨幅/翻绿率）
    e = daily_emotion(u, date)
    perf = []
    for code in prev_zt:
        b = u.bar(code, date)
        if b:
            perf.append(b["pct"])
    red_rate = round(sum(1 for p in perf if p > 0) / len(perf) * 100, 1) if perf else None
    return {
        "zt": len(zt), "zb": len(zb),
        "first_board": {"count": len(first), "shapes": fb_shapes},
        "ladder_dist": ladder_dist, "max_lb": maxlb, "gap": gaps,
        "promote_tiered": promote_tiered,
        "seal_rate": seal_rate, "zhaban_rate": zhaban_rate,
        "profit": {
            "avg_pct": e.get("yest_perf"), "red_rate": red_rate,
            "again_rate": e.get("promote_rate"), "green_rate": e.get("yest_green"),
        },
    }


# ============================================================== 5.6 近5日板块热度趋势 + 龙头谱系
def sector_trend_5d(u, date, code2boards, topn=6):
    """题材持续性 / 退潮追踪（对标 aiagents-stock 板块轮动、vibe-astock 近5天热度+龙头谱系）：
    取最近 5 个交易日，逐日重算行业板块强度，给出头部板块的 5 日强度曲线(升温/降温)，
    并对今日主线板块追溯「5日前领涨股现在跌到哪了」。全部由 K 线库重建。"""
    dates = u.dates[-5:]
    # 逐日轻量重算行业板块强度（不依赖当日快照封板细节）
    per = []
    for d in dates:
        lus_d = build_limit_ups(u, d, {}, code2boards, False)
        inds_d, _ = sector_heat(lus_d, {}, u, d)
        per.append((d, inds_d))
    # 汇总成 板块 -> 每日强度序列
    agg = {}
    for d, inds in per:
        for a in inds[:15]:
            t = agg.setdefault(a["name"], {"name": a["name"], "kind": a["kind"],
                                           "strength": [], "zt": [], "max_lb": [], "dates": []})
            t["strength"].append(a["strength"]); t["zt"].append(a["zt"])
            t["max_lb"].append(a["max_lb"]); t["dates"].append(d)
    latest = {n: t["strength"][-1] for n, t in agg.items() if t["strength"]}
    trend = []
    for n in sorted(latest, key=lambda x: -latest[x])[:topn]:
        t = agg[n]
        s0 = t["strength"][0] if t["strength"] else 0
        s1 = t["strength"][-1] if t["strength"] else 0
        delta = round(s1 - s0, 1)
        drift = "升温" if delta > 5 else ("降温" if delta < -5 else "持平")
        trend.append({"name": n, "kind": t["kind"], "strength": t["strength"],
                      "zt": t["zt"], "max_lb": t["max_lb"], "drift": drift, "delta": delta})
    # 龙头谱系：今日主线板块的领涨股 + 5日前同板块领涨股现状
    lus_today = build_limit_ups(u, date, {}, code2boards, False)
    inds_today, _ = sector_heat(lus_today, {}, u, date)
    mainline = [a for a in inds_today[:15] if a.get("tier") == "主线"]
    d0 = dates[0]
    lus_d0 = build_limit_ups(u, d0, {}, code2boards, False)
    lineage = []
    for a in mainline[:4]:
        leads_now = sorted([r for r in lus_today if r["industry"] == a["name"]],
                           key=lambda x: (-x["streak"], -x["quality"]))
        leads_0 = sorted([r for r in lus_d0 if r["industry"] == a["name"]],
                         key=lambda x: (-x["streak"], -x["quality"]))
        lead_now = leads_now[0] if leads_now else None
        lead_old = leads_0[0] if leads_0 else None
        old_pct = None
        if lead_old:
            b0 = u.bar(lead_old["code"], date)
            old_pct = round(b0["pct"], 2) if b0 else None
        lineage.append({
            "sector": a["name"], "strength": a["strength"],
            "lead_now": {"name": lead_now["name"], "streak": lead_now["streak"]} if lead_now else None,
            "lead_5d_ago": {"name": lead_old["name"], "streak": lead_old["streak"]} if lead_old else None,
            "lead_old_today_pct": old_pct,
        })
    return {"dates": dates, "trend": trend, "lineage": lineage}


def sentiment_score(u, date, series, snap, snap_is_same_day):
    e = next((x for x in series if x["date"] == date), None) or daily_emotion(u, date)
    # 封板率：用日K重建（涨停 /（涨停+炸板）），历史上每日都可得，不依赖当日快照
    zb_cnt = e.get("zb") or 0
    seal_rate = (e["zt"] / (e["zt"] + zb_cnt) * 100) if (e["zt"] + zb_cnt) > 0 else None
    # 成交额环比
    idx = [i for i, x in enumerate(series) if x["date"] == date]
    amt_chg = None
    if idx and idx[0] > 0:
        p = series[idx[0] - 1]["amount"]
        if p:
            amt_chg = (e["amount"] - p) / p * 100
    comp = [
        {"k": "赚钱效应", "desc": "昨日涨停股今日平均涨幅",
         "raw": e["yest_perf"], "unit": "%",
         "score": lerp_score(e["yest_perf"], -6, 0, 6), "w": 0.24},
        {"k": "连板晋级率", "desc": "昨日涨停今日再涨停占比",
         "raw": e["promote_rate"], "unit": "%",
         "score": lerp_score(e["promote_rate"], 4, 14, 30), "w": 0.18},
        {"k": "涨停总量", "desc": "全市场涨停家数",
         "raw": e["zt"], "unit": "家",
         "score": lerp_score(e["zt"], 15, 55, 120), "w": 0.14},
        {"k": "空间高度", "desc": "最高连板板数",
         "raw": e["max_lb"], "unit": "板",
         "score": lerp_score(e["max_lb"], 2, 4, 8), "w": 0.12},
        {"k": "涨跌家数比", "desc": "上涨家数占比",
         "raw": round(e["up"] / max(1, e["up"] + e["down"]) * 100, 1), "unit": "%",
         "score": lerp_score(e["up"] / max(1, e["up"] + e["down"]) * 100, 22, 50, 78), "w": 0.12},
        {"k": "亏钱效应", "desc": "跌停家数（越少越好）",
         "raw": e["dt"], "unit": "家",
         "score": lerp_score(-e["dt"], -30, -8, 0), "w": 0.08},
        {"k": "炸板家数", "desc": "触及涨停却未封住（分歧/派发信号）",
         "raw": e["zb"], "unit": "家",
         "score": lerp_score(-e["zb"], -25, -6, 0), "w": 0.06},
        {"k": "量能变化", "desc": "两市成交额环比",
         "raw": round(amt_chg, 1) if amt_chg is not None else None, "unit": "%",
         "score": lerp_score(amt_chg, -18, 0, 18), "w": 0.12},
    ]
    if seal_rate is not None:
        comp.append({"k": "封板率", "desc": "涨停/(涨停+炸板)", "raw": round(seal_rate, 1),
                     "unit": "%", "score": lerp_score(seal_rate, 45, 72, 92), "w": 0.10})
    # 市场热度（核心维度）：以标杆趋势股【交易额度（成交额）】判断
    # 标杆股成交额相对自身 20 日均量放大 → 抱团温热、可积极参与；缩量 → 退潮。
    bh = benchmark_heat(u, date)
    bar = bh["avg_amt_ratio"]
    if bar is not None:
        comp.append({"k": "市场热度", "desc": "标杆趋势股成交额/20日均量（放大=热）",
                     "raw": round(bar, 2), "unit": "倍",
                     "score": lerp_score(bar, 0.8, 1.0, 1.3), "w": 0.16})
    tw = sum(c["w"] for c in comp)
    score = sum(c["score"] * c["w"] for c in comp) / tw
    for c in comp:
        c["score"] = round(c["score"], 1)
        c["w"] = round(c["w"] / tw, 3)
    if score >= 76:
        lv, label = "亢奋", "情绪高潮，赚钱效应强但需防高位闪崩"
    elif score >= 60:
        lv, label = "偏热", "主线清晰，可积极参与"
    elif score >= 45:
        lv, label = "均衡", "结构性行情，精选个股"
    elif score >= 30:
        lv, label = "偏冷", "分歧加大，降低仓位与预期"
    else:
        lv, label = "冰点", "退潮末期，等待情绪修复信号"
    return {"score": round(score, 1), "level": lv, "label": label, "components": comp,
            "seal_rate": round(seal_rate, 1) if seal_rate is not None else None,
            "amt_chg": round(amt_chg, 1) if amt_chg is not None else None}


def cycle_phase(series):
    """情绪周期定位：冰点 / 启动 / 发酵 / 高潮 / 退潮"""
    if len(series) < 6:
        return {"phase": "数据不足", "desc": "", "evidence": []}
    s = series[-6:]
    zt = [x["zt"] for x in s]
    perf = [x["yest_perf"] for x in s if x["yest_perf"] is not None]
    prom = [x["promote_rate"] for x in s if x["promote_rate"] is not None]
    hi = [x["max_lb"] for x in s]
    ev = []
    zt_tr = zt[-1] - mean(zt[:-1])
    perf_tr = (perf[-1] - mean(perf[:-1])) if len(perf) >= 3 else 0
    prom_now = prom[-1] if prom else 0
    prom_tr = (prom[-1] - mean(prom[:-1])) if len(prom) >= 3 else 0
    hi_now, hi_prev = hi[-1], max(hi[:-1])
    ev.append("涨停家数 %d（近5日均值 %.0f，%s%.0f）" % (
        zt[-1], mean(zt[:-1]), "+" if zt_tr >= 0 else "", zt_tr))
    if perf:
        ev.append("昨涨停今日平均 %.2f%%（趋势 %s%.2f）" % (perf[-1], "+" if perf_tr >= 0 else "", perf_tr))
    if prom:
        ev.append("晋级率 %.1f%%（趋势 %s%.1f）" % (prom_now, "+" if prom_tr >= 0 else "", prom_tr))
    ev.append("空间高度 %d 板（前期最高 %d 板）" % (hi_now, hi_prev))

    lastperf = perf[-1] if perf else 0
    if lastperf < -1.5 and prom_now < 10 and zt[-1] < mean(zt[:-1]):
        ph, desc = "退潮期", "赚钱效应转负、晋级率坍塌，高位股批量断板，宜空仓或只做低位first board"
    elif zt[-1] <= 20 and lastperf < 0 and hi_now <= 3:
        # 超跌反包已弱化（大样本 55.6%/+0.50%），低位首板才是冰点期主信号
        ph, desc = "冰点期", "情绪极度低迷，往往是下一轮反弹的孕育期，关注低位首板，超跌反弹仅轻仓"
    elif hi_now >= 5 and lastperf > 1 and prom_now >= 15:
        ph, desc = "高潮期", "高度板与赚钱效应共振，可参与但需快进快出，警惕见顶回落"
    elif zt_tr > 0 and perf_tr > 0 and lastperf > 0:
        ph, desc = "发酵期", "涨停扩容且赚钱效应改善，主线正在形成，是介入的较优阶段"
    elif zt_tr > 0 and lastperf >= -1:
        ph, desc = "启动期", "情绪自低位回暖，可小仓位试错低位板"
    else:
        ph, desc = "震荡分歧期", "多空拉锯，缺乏持续主线，控制仓位做短打"
    return {"phase": ph, "desc": desc, "evidence": ev}


# ============================================================== 6. 断板概率模型
def logit(p):
    p = clamp(p, 0.02, 0.98)
    return math.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + math.exp(-max(-20, min(20, x))))


def break_risk(limit_ups, stats, sent, sectors_by_name, u, date, auction_map=None):
    out = []
    base_default = {1: 22, 2: 20, 3: 18, 4: 16, 5: 14, 6: 12, 7: 10}
    env = (sent["score"] - 50) / 50.0     # -1 ~ 1
    for r in limit_ups:
        n = r["streak"]
        st = stats.get(n)
        p0 = (st["promote_rate"] if st else base_default.get(n, 12)) / 100.0
        z, facs = 0.0, []

        def add(name, val, impact, note):
            nonlocal z
            z += impact
            facs.append({"k": name, "v": val, "impact": round(impact, 3), "note": note})

        # 封板时间
        stime = r.get("seal_time")
        mins = parse_hms(stime)
        if mins is not None:
            hh, mm = (570 + mins) // 60, (570 + mins) % 60
            imp = 0.55 if mins <= 5 else (0.28 if mins <= 35 else (-0.12 if mins <= 150 else -0.55))
            add("首封时间", "%02d:%02d" % (hh, mm), imp,
                "开盘即封，资金一致性极强" if mins <= 5 else ("早盘封板，承接良好" if mins <= 35 else
                ("午后封板，力度一般" if mins <= 150 else "尾盘偷袭板，次日不确定性大")))
        elif r["yizi"]:
            add("封板形态", "一字板", 0.62, "一字无量封板，惜售明显")
        # 炸板次数
        zb = r.get("zb_count")
        if zb is not None:
            imp = 0.12 if zb == 0 else -0.30 * zb
            add("炸板次数", "%d 次" % zb, imp, "全天未开板" if zb == 0 else "盘中开板 %d 次，分歧明显" % zb)
        # 封单强度
        if r.get("seal_fund") and r.get("float_mv"):
            ratio = r["seal_fund"] / r["float_mv"] * 100
            imp = clamp((ratio - 1.2) * 0.22, -0.45, 0.65)
            add("封单/流通市值", "%.2f%%" % ratio, imp,
                "封单厚实" if ratio >= 2 else ("封单偏薄，易被砸开" if ratio < 0.8 else "封单一般"))
        # 换手
        t = r.get("turn") or 0
        if r["yizi"]:
            imp = 0.35 if t < 3 else 0.1
            add("换手率", "%.1f%%" % t, imp, "一字低换手，锁仓好")
        elif t > 32:
            imp = -0.42
            add("换手率", "%.1f%%" % t, imp, "换手过高，获利盘出逃压力大")
        elif t < 3 and n >= 2:
            add("换手率", "%.1f%%" % t, 0.22, "缩量封板，抛压小")
        else:
            add("换手率", "%.1f%%" % t, clamp((14 - abs(t - 14)) / 14 * 0.2, -0.2, 0.2), "换手处于健康区间")
        # 板块温度
        sec = sectors_by_name.get(r["industry"])
        if sec:
            imp = clamp((sec["strength"] - 45) / 100.0 * 0.9, -0.42, 0.55)
            add("板块强度", "%s %.0f分" % (sec["name"], sec["strength"]), imp,
                "所属板块是当日主线" if sec["tier"] == "主线" else
                ("板块有一定合力" if sec["tier"] == "支线" else "板块无合力，孤军奋战"))
        # 龙头溢价
        if sec and sec["max_lb"] == n and n >= 2:
            add("板块地位", "板块最高标", 0.30, "板块内空间最高，接力资金优先")
        # 涨幅透支
        g20 = r.get("gain20")
        if g20 is not None:
            imp = clamp(-(g20 - 55) / 100.0 * 0.8, -0.6, 0.18)
            add("20日涨幅", "%.0f%%" % g20, imp,
                "短期涨幅过大，兑现压力重" if g20 > 70 else "涨幅可控")
        # 市场环境
        add("市场情绪", "%s %.0f分" % (sent["level"], sent["score"]), env * 0.65,
            sent["label"])
        # 高度衰减（3-5 板后加速衰减）
        if n >= 3:
            imp = -0.16 * (n - 2)
            add("高度衰减", "%d 连板" % n, imp, "连板越高，资金接力难度指数级上升")
        # 竞价定调（离线重建的集合竞价强弱）
        aq = (auction_map or {}).get(r["code"])
        if aq:
            sc = aq.get("auction_score") or 50
            imp = clamp((sc - 50) / 50.0 * 0.5, -0.42, 0.5)
            add("竞价强度", "%.0f 分" % sc, imp,
                "竞价定调偏强，次日有承接" if sc >= 60 else ("竞价偏弱/分歧，次日易承压" if sc < 45 else "竞价中性"))
            if aq.get("pattern") == "强转弱":
                add("竞价形态", "强转弱", -0.30, "大幅高开却炸板/高换手，典型诱多分歧")
            # 竞价量能异动（离线估算）
            va = aq.get("vol_anomaly")
            if va:
                if va.get("warn"):
                    add("竞价量能", "放量派发⚠", -0.45, "竞价爆量且高开低走/炸板，疑似对倒派发，次日风险陡增")
                elif va.get("flag") == "放量异动":
                    add("竞价量能", "抢筹放量", 0.18, "竞价爆量+高开高走，资金主动进攻")
                elif va.get("flag") == "一字锁仓":
                    add("竞价量能", "一字锁仓", 0.30, "一字无量，惜售锁仓，筹码稳定")

        p = sigmoid(logit(p0) + z)
        pb = 1 - p
        if pb >= 0.90:
            lvl, cls = "极高", "danger"
        elif pb >= 0.80:
            lvl, cls = "高", "warn"
        elif pb >= 0.66:
            lvl, cls = "中等", "mid"
        else:
            lvl, cls = "偏低", "ok"
        out.append({
            "code": r["code"], "name": r["name"], "streak": n, "industry": r["industry"],
            "quality": r["quality"], "base_rate": round(p0 * 100, 1),
            "p_continue": round(p * 100, 1), "p_break": round(pb * 100, 1),
            "risk": lvl, "cls": cls,
            "factors": sorted(facs, key=lambda x: -abs(x["impact"]))[:7],
            "hist": stats.get(n),
        })
    out.sort(key=lambda x: (-x["streak"], x["p_break"]))
    return out


# ============================================================== 7. 妖股形态相似度
DEMON_WIN = 28   # 形态窗口长度


def _norm_series(bs, key):
    v = [b[key] for b in bs]
    if not v:
        return []
    m = mean(v)
    sd = math.sqrt(mean([(x - m) ** 2 for x in v])) or 1e-9
    return [(x - m) / sd for x in v]


def mine_demon_templates(u, min_streak=5, min_gain=85.0):
    """从历史 K 线库挖掘妖股样本，取其【启动前 28 日】形态作为模板"""
    tpls = []
    ndates = len(u.dates)
    for code, bs in u.bars.items():
        if len(bs) < DEMON_WIN + 25:
            continue
        s = u.stocks.get(code)
        if not s:
            continue
        sd = u.streak.get(code, {})
        # 找主升浪：最大连板起点 或 20 日最大涨幅窗口起点
        best = None
        for i in range(DEMON_WIN, len(bs) - 8):
            st = sd.get(bs[i]["d"], 0)
            if st >= min_streak:
                start = i - st + 1
                if best is None or st > best[1]:
                    best = (start, st, "连板%d" % st)
        if best is None:
            for i in range(DEMON_WIN, len(bs) - 20):
                g = (max(x["c"] for x in bs[i:i + 20]) / bs[i]["c"] - 1) * 100
                if g >= min_gain and (best is None or g > best[1]):
                    best = (i, g, "20日+%.0f%%" % g)
        if best is None:
            continue
        s0 = best[0]
        if s0 < DEMON_WIN or s0 + 5 >= len(bs):
            continue
        pre = bs[s0 - DEMON_WIN:s0]
        post = bs[s0:min(len(bs), s0 + 25)]
        gain = (max(x["h"] for x in post) / bs[s0 - 1]["c"] - 1) * 100
        mx_streak = max([sd.get(x["d"], 0) for x in post], default=0)
        if gain < 55 and mx_streak < min_streak:
            continue
        tpls.append({
            "code": code, "name": s["name"],
            "start": bs[s0]["d"], "trigger": best[2],
            "gain": round(gain, 1), "max_streak": mx_streak,
            "pz": _norm_series(pre, "c"), "vz": _norm_series(pre, "v"),
            "feat": _struct_feat(u, code, pre),
            "float_mv": s.get("float_mv"),
        })
    tpls.sort(key=lambda x: -(x["gain"] + x["max_streak"] * 12))
    return tpls[:160]


def _struct_feat(u, code, bs):
    """结构特征向量（已归一到 0~1 附近）"""
    if len(bs) < 10:
        return [0.0] * 7
    c = [b["c"] for b in bs]
    v = [b["v"] for b in bs]
    t = [b["turn"] or 0 for b in bs]
    gain = (c[-1] / c[0] - 1)
    peak = max(c)
    dd = (peak - min(c[c.index(peak):])) / peak if peak else 0
    zt_cnt = sum(1 for b in bs if code in u.zt.get(b["d"], ()))
    v_ratio = (mean(v[-5:]) / mean(v[:-5])) if mean(v[:-5]) else 1
    amp = mean([b["amp"] or 0 for b in bs])
    return [clamp(gain / 0.6, -1, 2), clamp(dd / 0.35), clamp(zt_cnt / 5.0),
            clamp(v_ratio / 3.0), clamp(mean(t) / 15.0), clamp(amp / 12.0),
            clamp((c[-1] - min(c)) / (peak - min(c) + 1e-9))]


def demon_scan(u, date, limit_ups, tpls, sectors_by_name):
    out = []
    for r in limit_ups:
        code = r["code"]
        bs = u.bars_upto(code, date, DEMON_WIN)
        if len(bs) < DEMON_WIN - 4:
            continue
        pz = _norm_series(bs, "c")
        vz = _norm_series(bs, "v")
        ft = _struct_feat(u, code, bs)
        sims = []
        for t in tpls:
            if t["code"] == code:
                continue
            ps = pearson(resample(pz, DEMON_WIN), resample(t["pz"], DEMON_WIN))
            vs = pearson(resample(vz, DEMON_WIN), resample(t["vz"], DEMON_WIN))
            fd = math.sqrt(sum((a - b) ** 2 for a, b in zip(ft, t["feat"]))) / math.sqrt(len(ft))
            fs = 1 - clamp(fd / 1.1)
            sim = 0.46 * max(0, ps) + 0.22 * max(0, vs) + 0.32 * fs
            sims.append((sim, t))
        sims.sort(key=lambda x: -x[0])
        top = sims[:3]
        pattern = mean([s for s, _ in top]) * 100 if top else 0
        # 妖股特质分（不依赖相似度的绝对条件）
        fmv = (r.get("float_mv") or 0) / 1e8
        traits = [
            ("流通盘", lerp_score(-fmv, -200, -60, -12), 0.24),
            ("换手活跃", lerp_score(r.get("turn") or 0, 2, 12, 30), 0.16),
            ("连板基因", lerp_score(max(r["hist_max_streak"], r["streak"]), 1, 3, 7), 0.22),
            ("涨停密度", lerp_score(r["zt60"], 1, 5, 14), 0.16),
            ("题材热度", lerp_score((sectors_by_name.get(r["industry"]) or {}).get("strength", 30),
                                  20, 50, 85), 0.12),
            ("量能扩张", lerp_score(r.get("vol_ratio") or 1, 0.6, 1.8, 5.0), 0.10),
        ]
        trait = sum(v * w for _, v, w in traits)
        score = 0.55 * pattern + 0.45 * trait
        out.append({
            "code": code, "name": r["name"], "streak": r["streak"], "industry": r["industry"],
            "score": round(score, 1), "pattern": round(pattern, 1), "trait": round(trait, 1),
            "traits": [{"k": k, "v": round(v, 1)} for k, v, _ in traits],
            "float_mv": r.get("float_mv"), "turn": r.get("turn"),
            "similar": [{"code": t["code"], "name": t["name"], "sim": round(s * 100, 1),
                         "start": t["start"], "gain": t["gain"], "max_streak": t["max_streak"],
                         "trigger": t["trigger"]} for s, t in top],
        })
    out.sort(key=lambda x: -x["score"])
    return out


# ============================================================== 8. 竞价定调（离线重建集合竞价强弱）
def auction_profile(u, date, limit_ups):
    """用日K的开盘/收盘行为离线重构集合竞价强弱，无需盘中逐笔数据。
    返回 {summary:{...}, items:{code:{...}}}"""
    items = {}
    yizi = tboard = weak = strong = high_open = 0
    vol_anom = vol_warn = 0
    opens = []
    for r in limit_ups:
        b = u.bar(r["code"], date)
        if not b:
            continue
        pc = (b["c"] - b["chg"]) if (b["c"] - b["chg"]) > 0 else b["c"]
        o, c = b["o"], b["c"]
        open_pct = round((o / pc - 1) * 100, 2) if pc > 0 else 0.0
        intraday = round((c - o) / o * 100, 2) if o > 0 else 0.0
        yizi_f = bool(r.get("yizi"))
        zb = r.get("zb_count")
        # 形态判定
        if yizi_f:
            pattern = "一字板"
        elif zb and zb >= 1:
            pattern = "T字板"
        elif open_pct <= 1.0:
            pattern = "弱转强"
        elif open_pct >= 7.0 and (zb and zb >= 1 or (r.get("turn") or 0) > 16):
            pattern = "强转弱"
        elif open_pct >= 2.0 and intraday >= 0:
            pattern = "高开高走"
        else:
            pattern = "换手板"
        # 竞价强度分：开盘定调 + 日内强弱 + 封板质量 + 弱转强 + 分歧消化
        if yizi_f:
            op = 100.0
        elif open_pct >= 7:
            op = 72.0
        elif open_pct >= 2:
            op = 92.0
        elif open_pct >= 0:
            op = 82.0
        elif open_pct >= -2:
            op = 90.0
        else:
            op = 70.0
        if pattern == "强转弱":
            op = min(op, 46.0)
        if yizi_f:
            idv = 95.0
        elif intraday >= 5:
            idv = 95.0
        elif intraday >= 0:
            idv = 80.0
        elif intraday >= -3:
            idv = 60.0
        else:
            idv = 35.0
        q = r.get("quality") or 50
        aq = clamp(op * 0.35 + idv * 0.25 + q * 0.20 +
                   (100 if pattern == "弱转强" else 50) * 0.10 +
                   (80 if pattern == "T字板" else 60) * 0.10, 0, 100)
        # ===== 竞价量能异动（离线估算，无需逐笔数据）=====
        # 估算竞价成交额 = 当日成交额 × 开盘参与度系数 k（随高开幅度增大）
        k = clamp(0.05 + abs(open_pct) / 35.0, 0.04, 0.28)
        est_today = (b["amt"] or 0) * k
        # 该股近 20 日基准（自身中位数，避免不同市值不可比）
        _hist = u.bars_upto(r["code"], date, 60)[-21:-1]
        _est = []
        for _hb in _hist:
            _hpc = (_hb["c"] - _hb["chg"]) if (_hb["c"] - _hb["chg"]) > 0 else _hb["c"]
            _hop = ((_hb["o"] / _hpc - 1) * 100) if _hpc > 0 else 0.0
            _hk = clamp(0.05 + abs(_hop) / 35.0, 0.04, 0.28)
            _est.append((_hb["amt"] or 0) * _hk)
        _est = [x for x in _est if x > 0]
        _median = sorted(_est)[len(_est) // 2] if _est else 0
        _ratio = (est_today / _median) if _median > 0 else 1.0
        if yizi_f:
            va = {"flag": "一字锁仓", "ratio": round(_ratio, 2), "severity": "none",
                  "warn": False, "note": "一字板无集合竞价成交，锁仓惜售"}
        elif _ratio >= 2.5:
            _warn = (pattern == "强转弱") or (open_pct >= 7 and intraday < 0)
            va = {"flag": "放量异动", "ratio": round(_ratio, 2),
                  "severity": "high" if _warn else "mid", "warn": _warn,
                  "note": ("竞价爆量且高开低走/炸板，疑似对倒派发，次日风险陡增"
                           if _warn else "竞价爆量+高开高走，资金主动抢筹进攻")}
        elif _ratio <= 0.5:
            va = {"flag": "缩量", "ratio": round(_ratio, 2),
                  "severity": "none" if yizi_f else "mid", "warn": False,
                  "note": ("缩量高开非一字，资金观望/承接不足" if open_pct >= 2 else "缩量，分歧较小")}
        else:
            va = {"flag": "正常", "ratio": round(_ratio, 2), "severity": "none", "warn": False,
                  "note": "竞价量能处于自身常态区间"}
        if va["flag"] == "放量异动":
            vol_anom += 1
        if va["warn"]:
            vol_warn += 1
        items[r["code"]] = {
            "code": r["code"], "name": r["name"], "streak": r["streak"],
            "open_pct": open_pct, "intraday": intraday, "yizi": yizi_f,
            "gap_type": ("一字" if yizi_f else "大幅高开" if open_pct >= 7 else "高开" if open_pct >= 2
                         else "平开" if open_pct >= -2 else "低开"),
            "pattern": pattern, "auction_score": round(aq, 1),
            "vol_anomaly": va,
        }
        opens.append(open_pct)
        if yizi_f:
            yizi += 1
        if pattern == "T字板":
            tboard += 1
        if pattern == "弱转强":
            weak += 1
        if pattern == "强转弱":
            strong += 1
        if not yizi_f and open_pct >= 2 and pattern in ("高开高走", "换手板"):
            high_open += 1
    avg = round(mean(opens), 2) if opens else 0.0
    summary = {
        "total": len(limit_ups), "yizi": yizi, "t_board": tboard,
        "weak_strong": weak, "strong_weak": strong, "high_open": high_open,
        "avg_open_pct": avg, "vol_anomaly": vol_anom, "vol_warn": vol_warn,
    }
    # ===== 市场级竞价强度聚合（供前端『竞价强度定调』专卡）=====
    scores = [it["auction_score"] for it in items.values()]
    avg_score = round(mean(scores), 1) if scores else 0.0
    score_dist = {"强(>=80)": 0, "中(60-80)": 0, "弱(<60)": 0}
    for s in scores:
        if s >= 80:
            score_dist["强(>=80)"] += 1
        elif s >= 60:
            score_dist["中(60-80)"] += 1
        else:
            score_dist["弱(<60)"] += 1
    gap_dist = {"一字": 0, "大幅高开": 0, "高开": 0, "平开": 0, "低开": 0}
    for it in items.values():
        gap_dist[it["gap_type"]] = gap_dist.get(it["gap_type"], 0) + 1
    strength = "强" if avg_score >= 75 else ("中" if avg_score >= 55 else "弱")
    qiang = [it for it in items.values()
             if it["vol_anomaly"].get("flag") == "放量异动" and not it["vol_anomaly"].get("warn")]
    paifa = [it for it in items.values() if it["vol_anomaly"].get("warn")]
    qiang.sort(key=lambda x: x["vol_anomaly"].get("ratio", 0), reverse=True)
    paifa.sort(key=lambda x: x["vol_anomaly"].get("ratio", 0), reverse=True)
    market_view = {
        "avg_score": avg_score, "strength": strength, "score_dist": score_dist,
        "gap_dist": gap_dist, "momentum": {"weak_strong": weak, "strong_weak": strong},
        "qiangchou": [{"code": x["code"], "name": x["name"], "streak": x["streak"],
                       "open_pct": x["open_pct"], "ratio": x["vol_anomaly"].get("ratio", 0)}
                      for x in qiang[:5]],
        "paifa": [{"code": x["code"], "name": x["name"], "streak": x["streak"],
                   "open_pct": x["open_pct"], "pattern": x["pattern"],
                   "ratio": x["vol_anomaly"].get("ratio", 0)} for x in paifa[:5]],
    }
    return {"summary": summary, "items": items, "market_view": market_view}


# ============================================================== 9. 选股回测（历史真实推荐 + K线前向收益）
def backtest(u, con, horizons=(1, 3, 5)):
    """对历史真实推荐标的做前向收益回测（零成本：用已落库的每日推荐 + K线精确前向价）。
    返回 {total, h1, h3, h5, by_tag} 或 None（样本不足）。"""
    try:
        rows = con.execute("SELECT date,code,name,streak,tag FROM rec_picks").fetchall()
    except Exception:
        return None
    if not rows:
        return None
    didx = {d: i for i, d in enumerate(u.dates)}
    rets = {h: [] for h in horizons}
    by_tag = {}
    total = 0
    for date, code, name, streak, tag in rows:
        i = didx.get(date)
        if i is None:
            continue
        b0 = u.bar(code, date)
        if not b0 or not b0.get("c"):
            continue
        p0 = b0["c"]
        tag = tag or "其他"
        rec_rets, ok = {}, False
        for h in horizons:
            j = i + h
            if j >= len(u.dates):
                continue
            b1 = u.bar(code, u.dates[j])
            if not b1:
                continue
            ret = (b1["c"] / p0 - 1) * 100
            rets[h].append(ret)
            rec_rets[h] = ret
            ok = True
        if ok:
            total += 1
            by_tag.setdefault(tag, {"n": 0, "wins": 0, "sum": 0.0})
            key = 3 if 3 in rec_rets else 1
            by_tag[tag]["n"] += 1
            if rec_rets.get(key, 0) > 0:
                by_tag[tag]["wins"] += 1
            by_tag[tag]["sum"] += rec_rets.get(key, 0)
    if total == 0:
        return None

    def agg(lst):
        if not lst:
            return None
        wins = sum(1 for x in lst if x > 0)
        return {"n": len(lst), "win": round(wins / len(lst) * 100, 1),
                "avg": round(sum(lst) / len(lst), 2),
                "best": round(max(lst), 1), "worst": round(min(lst), 1)}

    out = {"total": total,
           "h1": agg(rets.get(1)), "h3": agg(rets.get(3)), "h5": agg(rets.get(5)),
           "by_tag": {t: {"n": v["n"],
                          "win": round(v["wins"] / v["n"] * 100, 1) if v["n"] else 0,
                          "avg": round(v["sum"] / v["n"], 2) if v["n"] else 0}
                      for t, v in by_tag.items() if v["n"] >= 3}}
    return out


# ============================================================== 9. 连板梯队持续性（近 N 日）
def ladder_history(u, date, days=5):
    """各连板高度在近 N 日的涨停家数矩阵，用于判断情绪结构是否健康/退潮。"""
    dates = u.dates[-days:]
    matrix = {str(h): [] for h in range(1, 8)}
    maxv = 0
    for d in dates:
        cnt = {}
        for code in u.zt.get(d, set()):
            n = u.streak.get(code, {}).get(d, 0)
            n = max(1, min(7, n))
            cnt[n] = cnt.get(n, 0) + 1
        for h in range(1, 8):
            v = cnt.get(h, 0)
            matrix[str(h)].append(v)
            maxv = max(maxv, v)
    return {"dates": [d[5:] for d in dates], "matrix": matrix, "max": maxv}


def ladder_warn(u, date, n=8):
    """连板梯队健康度诊断：高度板断板 + 梯队断层 → 接力退潮预警。

    基于 recent_height_series 近 N 日空间高度序列，输出告警清单。纯本地、零网络。"""
    ser = recent_height_series(u, date, n=n)
    if len(ser) < 2:
        return {"level": "数据不足", "warns": [], "today_max": 0, "yest_max": 0,
                "lb_today": 0, "series": ser}
    today = ser[-1]
    yest = ser[-2]
    warns = []
    level = "正常"
    if today[1] < yest[1]:
        warns.append("空间板断板：高度 %d→%d 板，接力情绪退潮，次日高位股谨慎"
                     % (yest[1], today[1]))
        level = "退潮"
    # 梯队连续度：近 3 日出现过的连板层级集合
    recent = ser[-3:]
    present = set()
    for _d, _mx, _lb in recent:
        for k in range(2, _mx + 1):
            present.add(k)
    top = today[1]
    missing = [k for k in range(2, top) if k not in present] if top >= 3 else []
    if missing:
        warns.append("梯队断层：缺 %s 板，连板结构不完整，资金抱团松动"
                     % "、".join("%d" % m for m in missing))
        level = "退潮" if level != "正常" else "降温"
    if not warns and today[1] <= yest[1] and today[2] < yest[2]:
        level = "降温"
    return {"level": level, "warns": warns, "today_max": today[1],
            "yest_max": yest[1], "lb_today": today[2], "series": ser}


def recent_height_series(u, date, n=20):
    """从 bars 派生近 N 个交易日『空间高度』序列，用于检测峰值后衰减通道。
    返回 [(date, max_streak, lb_count)]，max_streak = 当日涨停股中最大连板数（>=1），
    lb_count = 当日连板家数（>=2 板）。即使 rec_history 仅 1 行也能从第一天就给出趋势。"""
    dates = u.dates[-n:]
    out = []
    for d in dates:
        zt = u.zt.get(d, set())
        if not zt:
            out.append((d, 0, 0))
            continue
        mx = max((u.streak.get(c, {}).get(d, 1) for c in zt), default=1)
        lb = sum(1 for c in zt if (u.streak.get(c, {}).get(d, 1) or 0) >= 2)
        out.append((d, mx, lb))
    return out


# ============================================================== 10. 板块持续性轮动
def sector_rotation(u, date, code2boards, topn=12, days=5):
    """对当日最强行业，回溯近 N 日涨停家数，判断主线是持续 / 升温 / 降温 / 一日游。
    依赖板块成分库（code2boards）；库为空时返回 []（前端优雅降级）。"""
    if not code2boards:
        return []
    dates = u.dates[-days:]
    ind_codes = {}
    for code, boards in code2boards.items():
        for _bk, name, kind in boards:
            if kind == "industry":
                ind_codes.setdefault(name, set()).add(code)
    today = u.zt.get(date, set())
    today_cnt = {ind: sum(1 for c in codes if c in today) for ind, codes in ind_codes.items()}
    top = sorted(today_cnt.items(), key=lambda x: -x[1])[:topn]
    out = []
    for ind, _ in top:
        codes = ind_codes.get(ind, set())
        series = [sum(1 for c in codes if c in u.zt.get(d, ())) for d in dates]
        first, last = series[0], series[-1]
        persistent = sum(1 for s in series if s >= 2) >= 3
        is_new = (sum(series[:-1]) == 0 and last > 0)
        if last > first:
            trend = "升温"
        elif last < first:
            trend = "降温"
        else:
            trend = "持平"
        out.append({"name": ind, "zt_days": series, "today": last,
                    "persistent": persistent, "is_new": is_new, "trend": trend})
    out.sort(key=lambda x: -x["today"])
    return out


def sector_trade(u, date, code2boards, topn=3):
    """板块轮动实操结论：取 sector_rotation 主线 TopN，附加『领涨票』便于次日直接跟踪。

    返回 [{sector, trend, persistent, is_new, today_zt, leads:[{code,name,chg}]}]，
    库空时返回 []（前端优雅降级）。"""
    rot = sector_rotation(u, date, code2boards, topn=12, days=5)
    if not rot or not code2boards:
        return []
    today = u.zt.get(date, set())
    out = []
    for s in rot[:topn]:
        ind = s["name"]
        leads = []
        for code, boards in code2boards.items():
            if any(k == "industry" and nm == ind for _bk, nm, k in boards) and code in today:
                bs = u.bars.get(code) or []
                last = [b for b in bs if b["d"] <= date]
                if last:
                    leads.append({"code": code,
                                  "name": (u.stocks.get(code, {}) or {}).get("name") or code,
                                  "chg": last[-1].get("pct")})
        leads.sort(key=lambda x: -(x.get("chg") or 0))
        out.append({"sector": ind, "trend": s["trend"], "persistent": s["persistent"],
                    "is_new": s["is_new"], "today_zt": s["today"], "leads": leads[:5]})
    return out


# ============================================================== 10.2 板块接力 / 主线切换检测
def sector_relay(u, date, code2boards, days=60):
    """检测『主版块断板 → 接力』规律：当一条曾领涨的主线板块涨停家数崩塌（退潮），
    往往有另一条（或多条）板块从低位崛起承接资金——这是 A 股题材周期的核心切换信号。
    例：2026-03 电力（华电辽能断板）退潮后，医药（创新药/减肥药）、锂电接力成新主线。
    返回：退潮旧主线 broken、当前接力方向 relay[]、当前领涨 leader、所处阶段 phase/phase_desc。
    依赖板块成分库（code2boards）；库为空时返回 available=False。"""
    if not code2boards:
        return {"available": False}
    dates = [d for d in u.dates if d <= date][-days:]
    if len(dates) < 10:
        return {"available": False, "note": "交易日不足"}
    idx = dates.index(date)
    p7_i = max(0, idx - 7)

    # 行业 -> 成分股集合
    ind_codes = {}
    for code, boards in code2boards.items():
        for _bk, name, kind in boards:
            if kind == "industry":
                ind_codes.setdefault(name, set()).add(code)

    # 逐日行业涨停家数矩阵
    mat = []
    for d in dates:
        today = u.zt.get(d, set())
        row = {ind: sum(1 for c in codes if c in today) for ind, codes in ind_codes.items()}
        mat.append((d, row))

    last_d, last_row = mat[-1]
    p7_d, p7_row = mat[p7_i]

    # 当前领涨（涨停家数第一）
    leader_name, leader_zt = "", 0
    for ind, c in last_row.items():
        if c > leader_zt:
            leader_zt, leader_name = c, ind

    # 每行业序列
    ser = {ind: [row.get(ind, 0) for _, row in mat] for ind in ind_codes}

    # 各行业近期持续性（近7日有≥2只涨停的天数）与今日最高连板（主副线分类用）
    _persist = {}
    _maxlb = {}
    for ind, codes in ind_codes.items():
        s = ser.get(ind, [])
        _persist[ind] = sum(1 for x in s[-7:] if x >= 2) >= 3
        _maxlb[ind] = max([u.streak.get(c, {}).get(date, 0) for c in codes] or [0])

    # 退潮旧主线：窗口内曾是强主线（峰值≥5），且最新涨停家数崩塌（≤峰值*0.35 且 ≤2），
    # 峰值发生在近 ~25 个交易日内（是近期主线，而非远古记忆）。
    # 一条主线断板后，往往多条前主线同期退潮，故列出跌幅最大的若干条（最多 3 条）。
    broken_list = []
    for ind, s in ser.items():
        peak = max(s); peak_i = s.index(peak)
        latest = s[-1]
        if peak < 5:
            continue
        if peak_i < len(s) - 25:
            continue
        if latest <= max(1, round(peak * 0.35)) and latest <= 2:
            broken_list.append({
                "name": ind, "peak_date": dates[peak_i], "peak_zt": peak,
                "latest_zt": latest, "drop": peak - latest,
                "drop_ratio": round(latest / peak, 2) if peak else 0,
            })
    broken_list.sort(key=lambda x: -x["drop"])
    broken = broken_list[0] if broken_list else None

    # 接力方向：低位崛起（7日前≤1 且今日≥3）或 加速（7日内净增≥2 且今日≥3），排除退潮主线本身
    relay = []
    for ind, s in ser.items():
        if broken and ind == broken["name"]:
            continue
        latest = s[-1]
        prev7 = p7_row.get(ind, 0)
        if latest < 3:
            continue
        fresh = (prev7 <= 1)
        rising = (latest - prev7) >= 2
        if not (fresh or rising):
            continue
        persistent = _persist.get(ind, False)
        # 晋级确定性：持续(40) + 加速/新崛起(25/18) + 高度(20/12)，封顶 100
        cer = 0
        cer += 40 if persistent else 0
        cer += 25 if rising else (18 if fresh else 0)
        cer += 20 if latest >= 4 else (12 if latest >= 3 else 0)
        cer = min(100, cer)
        relay.append({
            "name": ind, "latest_zt": latest, "prev7_zt": prev7,
            "delta": latest - prev7,
            "kind": "新崛起" if fresh else "加速",
            "persistent": persistent, "max_lb": _maxlb.get(ind, 0),
            "certainty": cer, "toward_main": (latest >= 4 and persistent),
        })
    relay.sort(key=lambda x: (-x["latest_zt"], -x["certainty"]))
    relay = relay[:3]

    # ---- 主副线分类：主线 = 持续且高度够；支线 = 升温中的次级方向 ----
    mainline, sublines = [], []
    for ind, s in ser.items():
        latest = s[-1]
        if latest < 2:
            continue
        persistent = _persist.get(ind, False)
        first = s[max(0, len(s) - 7)] if len(s) >= 7 else s[0]
        if latest > first:
            trend = "升温"
        elif latest < first:
            trend = "降温"
        else:
            trend = "持平"
        rec = {"name": ind, "zt": latest, "max_lb": _maxlb.get(ind, 0),
               "persistent": persistent, "trend": trend}
        if latest >= 5 and persistent:
            mainline.append(rec)
        elif latest >= 3 or (latest >= 2 and (latest - p7_row.get(ind, 0)) >= 1):
            sublines.append(rec)
    mainline.sort(key=lambda x: (-x["zt"], -x["max_lb"]))
    sublines.sort(key=lambda x: (-x["zt"], -x["max_lb"]))
    relay_cer = round(mean([r["certainty"] for r in relay]), 0) if relay else 0

    # 当前领涨是否连续主导（阶段判定用）
    leader_persist = False
    if leader_name:
        top_days = sum(1 for _, row in mat[-7:] if row.get(leader_name, 0) == max(row.values()))
        leader_persist = top_days >= 4

    if broken and relay:
        phase = "旧主线断板→接力切换"
        extra = "（另有%s退潮）" % ("、".join(b["name"] for b in broken_list[1:3])
                                    if len(broken_list) > 1 else "") if len(broken_list) > 1 else ""
        phase_desc = "【%s】退潮（峰值 %d→现 %d 只涨停）%s，资金切向接力方向【%s】" % (
            broken["name"], broken["peak_zt"], broken["latest_zt"], extra,
            "、".join(r["name"] for r in relay))
    elif broken and not relay:
        phase = "主线退潮·混沌轮动"
        phase_desc = "【%s】退潮（峰值 %d→现 %d 只涨停），暂无明显接力方向，多线快速轮动" % (
            broken["name"], broken["peak_zt"], broken["latest_zt"])
    elif leader_persist:
        phase = "主线延续"
        phase_desc = "【%s】仍为绝对主线（近 7 日 %d 天领涨），资金抱团未松动" % (leader_name, top_days)
    else:
        phase = "多线并行"
        phase_desc = "无单一退潮主线，【%s】等方向并行轮动" % leader_name

    if relay:
        phase_desc += "（接力确定性 %d%%）" % relay_cer

    return {
        "available": True,
        "date": date,
        "broken": broken,
        "broken_list": broken_list[:3],
        "relay": relay,
        "relay_cer": relay_cer,
        "mainline": mainline,
        "sublines": sublines,
        "leader": {"name": leader_name, "zt": leader_zt} if leader_name else None,
        "phase": phase, "phase_desc": phase_desc,
        "window_days": days,
    }


# ============================================================== 10.3 恐慌 / 崩盘检测
def panic_scan(u, date, code2boards=None, lookback=20):
    """盘后恐慌/崩盘检测：跌停潮(z-score vs 近 lookback 日基线) + 大面榜(天地板/墓碑线/冲高回落)
    + 亏钱效应(昨日涨停收绿率) + 炸板率 + 涨跌广度。输出分级信号 level∈{安全,可控,升温,恐慌}
    与典型样本，供收盘报告『风险/恐慌』板块及按需 PushPlus 推送。"""
    # ---- 跌停潮：今日跌停家数 vs 近 lookback 日基线 ----
    dates = [d for d in u.dates if d <= date][-(lookback + 1):]
    base = [len(u.dt.get(d, ())) for d in dates[:-1]]
    today_dt = len(u.dt.get(date, ()))
    m = mean(base) if base else 0.0
    var = mean([(x - m) ** 2 for x in base]) if base else 0.0
    sd = var ** 0.5
    z = (today_dt - m) / (sd + 1e-9)

    # ---- 大面榜：盘中大幅冲高回落 / 天地板 / 墓碑线 ----
    bigface = []
    for code, b in u.by_date.get(date, []):
        lim = u.lim.get(code)
        if lim is None:
            continue
        amt = b.get("amt") or 0
        if amt < 5e7:                       # 忽略成交额 < 5000 万的极小票，降低噪声
            continue
        c, o, h, l, pct, chg = b["c"], b["o"], b["h"], b["l"], b["pct"], b.get("chg")
        if not c or chg is None:
            continue
        pre = c - chg                       # 昨收
        if pre <= 0:
            continue
        open_pct = (o / pre - 1) * 100
        high_pct = (h / pre - 1) * 100
        drop_from_high = (h - c) / h * 100 if h > 0 else 0
        if open_pct >= lim * 0.9 and pct <= -lim * 0.9:
            kind = "天地板"
        elif high_pct >= 5 and drop_from_high >= 9 and pct < 5:
            kind = "墓碑线" if (c - l) / (l if l > 0 else 1) < 0.03 else "冲高回落"
        else:
            continue
        bigface.append({"code": code, "name": u.stocks.get(code, {}).get("name", ""),
                        "pct": round(pct, 2), "open_pct": round(open_pct, 2),
                        "high_pct": round(high_pct, 2),
                        "drop_from_high": round(drop_from_high, 1), "kind": kind})
    bigface.sort(key=lambda x: (-x["drop_from_high"], 0 if x["kind"] == "天地板" else 1))
    bigface = bigface[:10]

    # ---- 亏钱效应 / 炸板率 / 广度 ----
    e = daily_emotion(u, date)
    yest_green = e.get("yest_green")            # 昨日涨停今日收绿占比（越高越亏钱）
    zb = e.get("zb", 0); zt = e.get("zt", 0)
    zb_rate = round(zb / (zb + zt) * 100, 1) if (zb + zt) > 0 else 0
    br = market_breadth(u, date)
    total = br["up"] + br["down"] + br["flat"]
    down_ratio = round(br["down"] / total * 100, 1) if total else 0.0

    # ---- 综合分级 ----
    score = 0
    if z >= 2: score += 2
    elif z >= 1: score += 1
    if today_dt >= 30: score += 1
    if (yest_green or 0) >= 55: score += 1
    if zb_rate >= 35: score += 1
    if down_ratio >= 70: score += 1
    if bigface and bigface[0]["drop_from_high"] >= 15: score += 1
    if today_dt >= 60: score += 1
    if today_dt == 0 and not bigface and down_ratio < 40 and (yest_green or 0) < 40:
        level = "安全"
    else:
        level = "恐慌" if score >= 5 else ("升温" if score >= 2 else "可控")

    # ---- 文本提示 ----
    hints = []
    if today_dt >= 10:
        hints.append("跌停 %d 家（近 %d 日均值 %.0f，z=%.1f）" % (today_dt, lookback, m, z))
    if bigface:
        hints.append("大面 %d 只（最惨 %s·%s，较高点回落 %.0f%%）"
                     % (len(bigface), bigface[0]["name"], bigface[0]["kind"], bigface[0]["drop_from_high"]))
    if (yest_green or 0) >= 55:
        hints.append("昨日涨停今日 %.0f%% 收绿（打板亏钱效应重）" % yest_green)
    if zb_rate >= 35:
        hints.append("炸板率 %.0f%%（封板意愿弱、分歧大）" % zb_rate)
    if down_ratio >= 70:
        hints.append("涨跌比 %.0f%% 下跌（盘面普跌）" % down_ratio)
    hint = "；".join(hints) if hints else "盘面未现明显恐慌信号"

    return {
        "date": date, "level": level, "score": score,
        "dt_count": today_dt, "dt_base": round(m, 1), "dt_z": round(z, 2),
        "bigface": bigface, "bigface_count": len(bigface),
        "yest_green": yest_green, "zb_rate": zb_rate, "down_ratio": down_ratio,
        "hint": hint,
    }


# ============================================================== 10.5 外围市场定调（美股 / 日股 / 韩股）
def global_market(indices):
    """根据外围主要指数最新收盘，给出对 A 股次日的定调信号。
    indices: [{"region","name","pct",...}, ...]；缺失时返回中性信号。"""
    if not indices:
        return {"available": False, "signal": "中性", "score": 0.0, "a_up_prob": 0.5,
                "us_pct": None, "jp_pct": None, "kr_pct": None, "detail": "外围数据缺失，按中性处理"}
    def avg(region):
        xs = [x["pct"] for x in indices if x.get("region") == region and x.get("pct") is not None]
        return round(mean(xs), 2) if xs else None
    us = avg("美股"); jp = avg("日股"); kr = avg("韩股"); hk = avg("港股")
    # 美股对 A 股次日高开/方向主导，港股为最强关联外围，日韩为区域情绪辅助
    us_s = clamp((us or 0) / 1.6, -3.2, 3.2)
    jp_s = clamp((jp or 0) / 2.2, -1.5, 1.5)
    kr_s = clamp((kr or 0) / 2.2, -1.5, 1.5)
    hk_s = clamp((hk or 0) / 1.8, -2.2, 2.2)
    blended = us_s * 0.64 + hk_s * 0.16 + jp_s * 0.12 + kr_s * 0.08
    score = round(clamp(blended * 22, -100, 100), 1)
    # A 股次日上涨概率（logit 合成，正负皆可）
    a_up = round(sigmoid(blended * 0.55) * 100, 1)
    if score >= 35:
        signal = "偏多"
    elif score <= -35:
        signal = "偏空"
    elif score >= 12:
        signal = "温和偏多"
    elif score <= -12:
        signal = "温和偏空"
    else:
        signal = "中性"
    parts = []
    if us is not None:
        parts.append("美股 %s%.2f%%" % ("+" if us >= 0 else "", us))
    if hk is not None:
        parts.append("港股 %s%.2f%%" % ("+" if hk >= 0 else "", hk))
    if jp is not None:
        parts.append("日经 %s%.2f%%" % ("+" if jp >= 0 else "", jp))
    if kr is not None:
        parts.append("韩国 %s%.2f%%" % ("+" if kr >= 0 else "", kr))
    etfs = [{"name": x["name"], "pct": x["pct"]} for x in indices
            if x.get("region") == "ETF" and x.get("pct") is not None]
    detail = "外围（" + "，".join(parts) + "）→ 对 A 股次日%s指引（上涨概率约 %.0f%%）" % (
        "正面" if score > 0 else ("负面" if score < 0 else "中性"), a_up)
    return {"available": True, "signal": signal, "score": score, "a_up_prob": a_up,
            "us_pct": us, "jp_pct": jp, "kr_pct": kr, "hk_pct": hk,
            "etfs": etfs, "detail": detail}


# ============================================================== 10.6 历史连板热度校准
# ============================================================== 5.4 标杆趋势股 & 市场热度
# 以历史上走出“真实强趋势（日均 3-5 个点、成交额持续放大）”的个股作为参照系，
# 校准『趋势抱团』环境是否温热。市场热度以【交易额度（成交额）】为核心判据：
# 标杆股成交额相对自身 20 日均量放大、且仍处多头结构 → 热度上行（抱团可参与）；
# 反之成交额塌缩、结构破位 → 退潮（趋势票需严格止损）。
# （代码均已在本项目本地 market.db 核实存在。）
BENCHMARK_TREND_STOCKS = [
    ("600396", "华电辽能"),   # 2024 电力抱团标杆，8→5→4→3 高度衰减经典
    ("002580", "圣阳股份"),   # 2024-2025 储能/固态电池趋势龙头
    ("300641", "正丹股份"),   # 2024 TMA 涨价十倍趋势股
    ("002130", "沃尔核材"),   # 2024-2025 铜缆/高速连接趋势核心
    ("688256", "寒武纪"),     # AI 芯片趋势抱团代表
    ("002261", "拓维信息"),   # 算力/鸿蒙趋势活跃标的
    ("002625", "光启技术"),   # 超材料趋势慢牛
]


def benchmark_heat(u, date):
    """以【交易额度（成交额）】为核心的标杆趋势股热度研判。
    返回每只标杆股的成交额(亿元)/环比(amt_ratio)/近5日日均涨幅/是否仍处多头，
    并汇总为整体热度等级（热/温/冷）与 0~1 热度分。"""
    rows = []
    for code, name in BENCHMARK_TREND_STOCKS:
        bs = u.bars.get(code)
        if not bs:
            continue
        hist = [b for b in bs if b["d"] <= date]
        if len(hist) < 25:
            continue
        closes = [b["c"] for b in hist]
        last = hist[-1]
        ma5 = mean(closes[-5:]); ma10 = mean(closes[-10:]); ma20 = mean(closes[-20:])
        price = closes[-1]
        amt = last.get("amt") or 0
        amt20 = mean([b.get("amt") or 0 for b in hist[-21:-1]]) if len(hist) > 5 else amt
        amt_ratio = (amt / amt20) if amt20 else 1.0
        trending = (ma5 > ma10 > ma20) and (price > ma5)
        avg_daily = mean([b.get("pct") or 0 for b in hist[-5:]])
        momentum = (price / ma20 - 1) * 100 if ma20 else 0
        rows.append({
            "code": code, "name": name,
            "amt": round(amt / 1e8, 2),                 # 亿元
            "amt_ratio": round(amt_ratio, 2),            # 成交额 / 20日均量
            "avg_daily": round(avg_daily, 2),            # 近5日日均涨幅(%)
            "trending": trending,                        # 是否仍处多头结构
            "momentum": round(momentum, 1),
            "close": round(price, 2),
        })
    if not rows:
        return {"level": "样本不足", "score": 0.0, "avg_amt_ratio": None,
                "share_trending": 0.0, "avg_daily": None, "total_amt": 0.0, "stocks": []}
    avg_amt_ratio = mean([r["amt_ratio"] for r in rows])
    share_trending = sum(1 for r in rows if r["trending"]) / len(rows)
    avg_daily = mean([r["avg_daily"] for r in rows])
    total_amt = sum(r["amt"] for r in rows)
    # 热度等级：以交易额度（成交额环比）为主导，叠加结构（是否仍多头）
    if avg_amt_ratio >= 1.15 and share_trending >= 0.5:
        level = "热"
    elif avg_amt_ratio <= 0.82 or share_trending <= 0.25:
        level = "冷"
    else:
        level = "温"
    score = clamp(0.5 * (avg_amt_ratio - 0.7) / 0.6 + 0.5 * share_trending, 0, 1)
    return {
        "level": level, "score": round(score, 3),
        "avg_amt_ratio": round(avg_amt_ratio, 2),
        "share_trending": round(share_trending, 2),
        "avg_daily": round(avg_daily, 2),
        "total_amt": round(total_amt, 1),   # 亿元
        "stocks": rows,
    }


def position_suggestion(heat_level, sentiment_level, heat_score=None, sent_score=None):
    """据『市场热度(标杆成交额) + 情绪分』给出今日建议总仓位（成）。

    两路信号取严：任一侧退潮都压仓，避免单一指标误导。返回 dict。"""
    _map = {"热": 80, "温": 60, "冷": 40, "样本不足": 50,
            "亢奋": 80, "偏热": 75, "均衡": 60, "偏冷": 40, "冰点": 20}
    hp = _map.get(heat_level, 50)
    sp = _map.get(sentiment_level, 50)
    pct = min(hp, sp)
    if pct >= 75:
        lv = "高仓位(可积极)"
    elif pct >= 60:
        lv = "中性偏多(精选)"
    elif pct >= 40:
        lv = "中性偏谨慎"
    else:
        lv = "低仓位(防守)"
    reason = "热度=%s · 情绪=%s → 取较严一侧" % (heat_level, sentiment_level)
    if heat_level in ("冷",) or sentiment_level in ("冰点", "偏冷"):
        reason += "（退潮期，控仓防回撤）"
    return {"heat": heat_level, "sentiment": sentiment_level,
            "suggest_pct": pct, "level": lv, "reason": reason}


def trend_verdict(bars, band=None, first_seen=None, date=None, cost=None):
    """趋势票明确结论（需求3）：建议买入 / 卖出 / 持有，给出具体价格与持有期限。

    纪律硬约束：波段持有上限 **20 个交易日（约1个月）**——连续涨一个月极少见，
    到期未到卖区也应离场换股。数据来自日K + band_levels，纯本地。

    返回 {action: 建议买入|建议卖出|持有观察|到期离场,
          buy_price, sell_price, stop_price, hold_limit_days,
          days_held, deadline, reason} 或 None。
    """
    if not bars or len(bars) < 20 or not bars[-1].get("c"):
        return None
    band = band or {}
    close = float(bars[-1]["c"])
    buy_zone = band.get("buy_zone") or [None, None]
    sell_zone = band.get("sell_zone") or [None, None]
    stop = band.get("stop")

    # 已持有天数：自首见日起的交易日数
    days_held = 0
    if first_seen:
        since = [b for b in bars if b["d"] >= first_seen]
        days_held = max(0, len(since) - 1)
    HOLD_LIMIT = 20
    deadline = None
    if first_seen and len(bars) > days_held:
        rest = [b for b in bars if b["d"] > (first_seen or "")]
        # 截止价参考：到期日若仍在区间内则离场
    expired = days_held >= HOLD_LIMIT and first_seen

    cur = float(close)
    bz_lo, bz_hi = (buy_zone[0], buy_zone[1]) if buy_zone and buy_zone[0] else (None, None)
    sz_lo, sz_hi = (sell_zone[0], sell_zone[1]) if sell_zone and sell_zone[0] else (None, None)

    # 趋势早期判定：刚入选（is_new/首次跟踪≤3天）→ 趋势尚未走完、主升在前，
    # 应主动给「建议买入」而非等回踩；买入后明确持有期限。
    early = (first_seen is None) or (days_held <= 3)

    if stop and cur <= stop:
        action = "卖出（破位止损）"
        reason = "已跌破止损 %.2f，纪律执行离场" % stop
    elif sz_lo and cur >= sz_lo:
        action = "卖出（分批止盈）"
        reason = "进入卖出区 %s~%s，落袋为安" % (
            ("%.2f" % sz_lo) if sz_lo else "?", ("%.2f" % sz_hi) if sz_hi else "?")
    elif expired:
        action = "到期离场"
        reason = "持仓已 %d 个交易日（上限%d），波段时间到：无论盈亏换股，连续涨一个月极罕见" % (
            days_held, HOLD_LIMIT)
    elif bz_hi and cur <= bz_hi:
        action = "买入"
        reason = "回踩买入区 %s~%s，轻仓低吸、破 %s 止损" % (
            ("%.2f" % bz_lo) if bz_lo else "?", "%.2f" % bz_hi,
            ("%.2f" % stop) if stop else "?")
    elif early and sz_lo and cur < sz_lo:
        # 趋势早期 + 未到卖区 → 主动建议买入（现价即可分批，破位止损兜底）
        action = "建议买入"
        reason = ("趋势早期（跟踪第%d天），多头结构完好、主升未到卖点 %.2f："
                  "现价轻仓起步、回踩 %s 加仓，破 %s 止损" % (
                      days_held,
                      sz_lo,
                      ("%.2f" % bz_hi) if bz_hi else "低点",
                      ("%.2f" % stop) if stop else "MA20"))
    elif days_held >= 15:
        action = "减仓"
        reason = "持有 %d 天接近波段上限，且处区间中段：先减半仓锁定利润" % days_held
    else:
        action = "持有观察"
        # 明确给出「还能持有多少天」的期限提示（用户需求：持有X天）
        rest_days = max(0, HOLD_LIMIT - days_held)
        reason = ("趋势中段（买区上方、卖区下方），持有第%d/%d天（剩%d个交易日到期）——"
                  "破 %s 或到卖区 %s 前继续持有" % (
                      days_held, HOLD_LIMIT, rest_days,
                      ("%.2f" % stop) if stop else "MA20",
                      ("%.2f" % sz_lo) if sz_lo else "上沿"))

    # 趋势早期票：显式输出建议持有天数（波段纪律 20 日上限，早期留足空间）
    suggested_hold = HOLD_LIMIT - days_held if (early and action in ("建议买入", "买入")) else None

    return {
        "action": action,
        "buy_price": "%.2f" % bz_lo if bz_lo else None,
        "buy_zone": list(buy_zone) if buy_zone and buy_zone[0] else None,
        "sell_price": "%.2f" % sz_lo if sz_lo else None,
        "sell_zone": list(sell_zone) if sell_zone and sell_zone[0] else None,
        "stop_price": "%.2f" % stop if stop else None,
        "hold_limit_days": HOLD_LIMIT,
        "days_held": days_held,
        "early": bool(early),
        "suggested_hold_days": suggested_hold,
        "expired": bool(expired),
        "close": round(cur, 2),
        "cost": cost,
        "reason": reason,
    }


def compute_regime(hist_rows, picks_rows, bars_series=None):
    """结合历史连板库 + bars 派生序列，研判当前『高度周期』位置与断板校准系数。
    hist_rows:   [(date,max_streak,lb_count,zt_count,sent_score,cycle_phase,env_k,n_rec)]
    picks_rows:  [(date,code,name,streak,p_break,tag,next_continue,next_pct)]
    bars_series: [(date,max_streak,lb_count)] 由 recent_height_series 提供，可作 fallback/主信号
    """
    has_hist = bool(hist_rows) and len(hist_rows) >= 3
    if (not has_hist) and (not bars_series):
        return {"level": "样本不足", "factor": 0.0, "note": "历史连板库尚未积累，暂不校准",
                "max_series": [], "lb_series": [], "hit_rate": None,
                "hit_by_tag": {}, "peak_max": None}
    # 数据源：rec_history 充足用 rec_history，否则用 bars 派生序列（从第一天即可检测趋势）
    if has_hist:
        hs = [r[1] for r in hist_rows]; lbs = [r[2] for r in hist_rows]; src = "rec_history"
    else:
        hs = [s[1] for s in (bars_series or [])]; lbs = [s[2] for s in (bars_series or [])]; src = "bars派生"
    hs = [h for h in hs if h >= 1]  # 仅保留有连板的交易日
    if not hs:
        return {"level": "样本不足", "factor": 0.0, "note": "近 N 日无连板数据，暂不校准",
                "max_series": [], "lb_series": [], "hit_rate": None,
                "hit_by_tag": {}, "peak_max": None}
    cur_h = hs[-1]; cur_lb = lbs[-1] if lbs else 0
    peak = max(hs)
    ratio = (cur_h / peak) if peak else 1.0
    # 衰减通道检测：找最近峰值，统计其后连续回落天数（华电辽能 8→5→4→3 类规律）
    peak_idx = hs.index(peak)
    declines = 0
    for i in range(peak_idx + 1, len(hs)):
        if hs[i] < hs[i - 1]:
            declines += 1
        else:
            break
    # 近 5 日高度动量
    h5 = hs[-6] if len(hs) >= 6 else hs[0]
    momentum = cur_h - h5
    lb5 = mean(lbs[-6:-1]) if len(lbs) >= 6 else (mean(lbs[:-1]) if lbs[:-1] else 0)
    lb_trend = (cur_lb - lb5) if lb5 else 0
    factor = 0.0
    reasons = []
    # ① 峰值后衰减通道（核心：高位接力风险随衰减显著抬升）
    if declines >= 2 and cur_h <= peak - 2 and peak >= 4:
        factor += 0.28
        reasons.append("检测到『峰值后衰减通道』：空间高度自 %d 板峰值连续回落至 %d 板（连降 %d 日），"
                       "参照华电辽能、圣阳股份等历史标杆趋势股『高位放量见顶、缩量退潮』规律，"
                       "高位接力风险显著抬升" % (peak, cur_h, declines))
    # ② 逼近历史峰值
    if ratio >= 0.85 and cur_h >= 5 and declines == 0:
        factor += (ratio - 0.7) * 0.6
        reasons.append("空间高度 %d 板已逼近历史峰值 %d 板（%.0f%%），高位见顶/断板概率上升" % (cur_h, peak, ratio * 100))
    # ③ 高度走平/回落动量
    if momentum <= 0 and cur_h >= 4:
        factor += 0.15
        reasons.append("高度已连续走平/回落（5 日前 %d 板），接力意愿边际转弱" % h5)
    # ④ 连板家数塌缩
    if lb_trend <= -2:
        factor += 0.18
        reasons.append("连板家数较前 5 日均值减少 %d 只，梯队在塌缩" % abs(int(lb_trend)))
    # 命中率（仅统计有次日结局的样本）
    valid = [p for p in (picks_rows or []) if p[6] in (0, 1)]
    hit = sum(1 for p in valid if p[6] == 1)
    hit_rate = round(hit / len(valid) * 100, 1) if valid else None
    by_tag = {}
    for tag in ("核心龙头", "主线接力", "低位潜伏", "高位风险"):
        sub = [p for p in valid if p[5] == tag]
        if sub:
            by_tag[tag] = round(sum(1 for p in sub if p[6] == 1) / len(sub) * 100, 1)
    if not reasons:
        level = "中位/低位"
        reasons.append("空间高度 %d 板、连板家数 %d 只，处于历史区间中位，暂未见顶信号（数据源：%s）" % (cur_h, cur_lb, src))
    elif factor >= 0.35:
        level = "高位见顶风险"
    else:
        level = "分歧加大"
    note = "；".join(reasons)
    return {"level": level, "factor": round(clamp(factor, 0, 0.7), 3), "note": note,
            "max_series": hs[::-1][:20], "lb_series": lbs[::-1][:20],
            "hit_rate": hit_rate, "hit_by_tag": by_tag, "peak_max": peak,
            "cur_h": cur_h, "cur_lb": cur_lb, "momentum": momentum, "lb_trend": round(lb_trend, 1),
            "src": src, "declines": declines}


def auction_discipline(streak):
    """次日竞价开盘溢价决策线（2026-08-29 大样本回测落地，全市场 118 万根 K 线验证）。

    13 个月逐月回测方向 100% 一致（无一个月翻车）——开盘溢价是最强执行层信号：
      首板次日高开>5%  → 胜率 85.9% / +6.83%（高开=强势确认，溢价延续）
      首板次日高开2-5% → 胜率 70.3% / +3.21%
      首板次日平开±2%  → 胜率 46.7% / +0.07%（无方向，观望）
      首板次日低开<-2% → 胜率 26.5% / -3.04%（低开不是黄金坑，是弱势确认！）
    连板高度越高次日胜率越高（st=8 达 82.4%/+6.02%）。
    断板反包/首阴反包同时被证伪（1683/786 条样本，胜率 40% 上下，均值 -1%）。
    """
    if (streak or 0) >= 3:
        return {
            "rule": "高开≥2%积极跟进（高度票高开即强势确认）/ 低开≤-2%当日放弃",
            "basis": "全样本13个月：高度票次日胜率64%~82%，高开延续性显著；低开<-2%胜率仅17~38%"}
    return {
        "rule": "高开≥2%跟进 / 低开≤-2%放弃 / 平开±2%观望等方向",
        "basis": "本系统367条历史推荐实测：高开≥2%组胜率67.4%/+4.08%（全量仅+0.93%），低开<-2%组-2.55%——决策线把期望收益提升4倍"}


def recommend(limit_ups, risks, demons, sectors, sent, cyc, stats, auction_map=None, hist=None, relay_info=None):
    rmap = {r["code"]: r for r in risks}
    dmap = {d["code"]: d for d in demons}
    smap = {s["name"]: s for s in sectors}
    env_k = clamp((sent["score"] - 25) / 55.0, 0.25, 1.15)
    relay_secs = {x["name"] for x in (relay_info or {}).get("relay", [])}
    items = []
    for r in limit_ups:
        # ST/退市整理票不进推荐（2026-08-29：ST如意漏进低位潜伏确诊）。
        # 涨停画像/微观结构/连板计划等市场统计仍用完整 lus，仅推荐分层排除。
        _nm = r.get("name") or ""
        if "ST" in _nm.upper() or "退" in _nm:
            continue
        rk = rmap.get(r["code"]) or {}
        dm = dmap.get(r["code"]) or {}
        sec = smap.get(r["industry"]) or {}
        aq = (auction_map or {}).get(r["code"]) or {}
        # 历史连板热度校准：高位见顶/梯队塌缩时，抬升断板概率、压低续板概率
        hf = (hist or {}).get("factor", 0.0) or 0.0
        pc0 = rk.get("p_continue") or 20
        pb0 = rk.get("p_break") or 80
        pc_adj = clamp(pc0 - hf * 100 * 0.45, 1, 99)
        pb_adj = clamp(pb0 + hf * 100 * 0.45, 1, 99)
        # 负反馈校准 V2：p_break 高分位贡献已被历史回测证伪，压高分/抬低分位
        try:
            from recveto import calibrate_score as _calib
        except Exception:
            try:
                from pipeline.recveto import calibrate_score as _calib
            except Exception:
                _calib = None
        score = (0.26 * r["quality"] + 0.24 * pc_adj * 1.6
                 + 0.18 * sec.get("strength", 30) + 0.14 * dm.get("score", 30)
                 + 0.09 * lerp_score(r["streak"], 0, 3, 7)
                 + 0.09 * (aq.get("auction_score") or 50))
        score = clamp(score * (0.75 + 0.25 * env_k) - hf * 35, 0, 100)
        if _calib:
            score = clamp(_calib(score, pb_adj), 0, 100)
        # 是否值得购入分值（0-100，越高越值得）：综合续板概率/质量/板块/妖股，扣减历史风险
        worth = clamp(0.46 * pc_adj + 0.20 * r["quality"] + 0.16 * sec.get("strength", 30)
                      + 0.18 * (dm.get("score") or 30) - hf * 55, 0, 100)
        relay_dir = r["industry"] in relay_secs
        if relay_dir:
            # 接力方向：旧主线退潮后资金切入的新抱团，给与加权并打标
            worth = clamp(worth + 7, 0, 100)
            score = clamp(score + 4, 0, 100)
        items.append({
            "code": r["code"], "name": r["name"], "streak": r["streak"],
            "industry": r["industry"], "concepts": r["concepts"][:3],
            "close": r["close"], "turn": r["turn"], "float_mv": r["float_mv"],
            "quality": r["quality"], "p_continue": round(pc_adj, 1),
            "p_break": round(pb_adj, 1), "risk": rk.get("risk"),
            "demon": dm.get("score"), "sector_strength": sec.get("strength"),
            "sector_tier": sec.get("tier"), "relay_dir": relay_dir, "score": round(score, 1),
            "worth_score": round(worth, 1), "hist_factor": round(hf, 3),
            "similar": dm.get("similar", [])[:1],
            "yizi": r["yizi"], "seal_time": r.get("seal_time"), "zb_count": r.get("zb_count"),
            "gain20": r.get("gain20"),
            "day_vol_ratio": r.get("day_vol_ratio"),
        })
        items[-1]["auction_pattern"] = aq.get("pattern")
        items[-1]["vol_anomaly"] = aq.get("vol_anomaly")
        # 次日竞价决策线（大样本回测背书的执行纪律，盘前推送/看板共用）
        items[-1]["auction_rule"] = auction_discipline(r["streak"])
        items[-1]["hist_calib"] = {
            "level": (hist or {}).get("level", "—"),
            "note": (hist or {}).get("note", ""), "factor": round(hf, 3),
        }
    items.sort(key=lambda x: -x["score"])

    def reason(it):
        rs = []
        if it["streak"] >= 2:
            rs.append("已走出 %d 连板，具备接力资金关注度" % it["streak"])
        if it["streak"] >= 6:
            rs.append("全样本回测：%d 板以上空间板次日胜率 69%%~82%%、均值 +3.5%%~+6%%，高度溢价显著" % it["streak"])
        elif it["streak"] >= 4:
            rs.append("全样本回测：4 板以上次日胜率 64%%，高于二板（59%%）与首板（56%%）")
        if it["sector_tier"] == "主线":
            rs.append("所属【%s】为当日主线板块（强度 %.0f）" % (it["industry"], it["sector_strength"] or 0))
        elif it["sector_tier"] == "支线":
            rs.append("【%s】板块有合力（强度 %.0f）" % (it["industry"], it["sector_strength"] or 0))
        if it.get("relay_dir"):
            rs.append("所属【%s】为当前接力方向（旧主线退潮后的新抱团），资金正切入" % it["industry"])
        if it["quality"] >= 70:
            rs.append("封板质量 %.0f 分，封板结构扎实" % it["quality"])
        if it["yizi"]:
            rs.append("一字板封死，惜售情绪浓")
        if it.get("auction_pattern") == "弱转强":
            rs.append("竞价弱转强——开盘被低估后强势封板，资金分歧转一致")
        va = it.get("vol_anomaly") or {}
        if va.get("flag") == "放量异动" and not va.get("warn"):
            rs.append("竞价抢筹放量（约为常态 %.1f 倍），开盘资金主动进攻" % va.get("ratio", 1))
        if (it["p_continue"] or 0) >= 30:
            rs.append("模型测算次日续板概率 %.0f%%，高于同高度基准" % it["p_continue"])
        if (it["demon"] or 0) >= 60:
            s = (it["similar"] or [{}])[0]
            if s:
                rs.append("妖股基因 %.0f 分，形态最接近 %s（当时后续最高 +%.0f%%）"
                          % (it["demon"], s.get("name", ""), s.get("gain", 0)))
            else:
                rs.append("妖股基因 %.0f 分" % it["demon"])
        if it["float_mv"] and it["float_mv"] < 60e8:
            rs.append("流通盘 %.0f 亿，盘子轻易拉升" % (it["float_mv"] / 1e8))
        # 竞价决策线置顶（执行纪律优先级最高，13 个月全样本回测背书）
        ar = it.get("auction_rule") or {}
        if ar.get("rule"):
            rs.insert(0, "⏰ 竞价纪律：%s" % ar["rule"])
        return rs[:6]

    def risknote(it):
        rs = []
        if (it["p_break"] or 0) >= 78:
            rs.append("同高度历史断板率偏高，次日冲高回落概率大")
        if it["streak"] >= 4:
            # 断板反包已被 1683 条大样本证伪（胜率 40.6%/均值-0.86%）：
            # 高位票断板后止损离场，不要等反包。
            rs.append("高位连板一旦断板即离场——断板反包历史胜率仅 40%，勿恋战")
        if (it["gain20"] or 0) > 70:
            rs.append("20 日累计 +%.0f%%，获利盘沉重" % it["gain20"])
        if (it["zb_count"] or 0) >= 1:
            rs.append("盘中曾开板 %d 次，分歧已现" % it["zb_count"])
        if it.get("auction_pattern") == "强转弱":
            rs.append("竞价强转弱——大幅高开却炸板/高换手，诱多分歧明显")
        if (it.get("vol_anomaly") or {}).get("warn"):
            rs.append("竞价爆量异动+高开低走特征，疑似派发，次日回落风险高")
        if (it["turn"] or 0) > 30:
            rs.append("换手 %.0f%%，短线抛压重" % it["turn"])
        if it["sector_tier"] == "零星":
            rs.append("板块无合力，容易独木难支")
        return rs[:3] or ["注意大盘系统性风险"]

    core, relay, ambush, avoid = [], [], [], []

    def tier_ok(it):
        # 板块接口常因限流缺失 sector_tier；此时用板块强度兜底。
        return it.get("sector_tier") in ("主线", "支线") or (it.get("sector_strength") or 0) >= 42

    for it in items:
        it["reasons"] = reason(it)
        it["risks"] = risknote(it)
        st = it.get("streak", 0) or 0
        sc = it.get("score", 0) or 0
        pb = it.get("p_break") or 100
        # ── 负反馈闭环 V1（标注式，2026-08-27 用户指令）──
        # 高危画像不一刀切：WARN 降权+标注后照常参与分层（用户仍要挖掘连板次日买点）；
        # 仅极端画像(VETO|)拦入回避。
        _vr = None
        try:
            from recveto import veto as _veto_fn
        except Exception:
            try:
                from pipeline.recveto import veto as _veto_fn
            except Exception:
                _veto_fn = None
        if _veto_fn is not None:
            try:
                _vr = _veto_fn(it)
            except Exception:
                _vr = None
        if _vr:
            _reason = _vr.split("|", 1)[1] if "|" in str(_vr) else str(_vr)
            it["veto_reason"] = _reason
            if _reason not in (it["risks"] or []):
                it["risks"] = ([_reason] + (it["risks"] or []))[:3]
            # WARN：高危标注 + 打分降 8%（排序自然靠后，仍可入选主推）
            it["risk_flag"] = "⚠"
            sc = sc * 0.92
            it["score"] = round(sc, 1)
        if _vr and str(_vr).startswith("VETO"):
            it["tag"] = "高位风险"
            avoid.append(it)
            continue
        # 连板高度是推荐分层的稳健主信号（不受保守评分绝对值影响），保证板块不为空。
        if st >= 3 and pb >= 78:
            it["tag"] = "高位风险"
            avoid.append(it)
        elif st >= 4:
            it["tag"] = "核心龙头"
            core.append(it)
        elif st == 3:
            it["tag"] = "核心龙头" if sc >= 46 else "主线接力"
            (core if sc >= 46 else relay).append(it)
        elif st == 2:
            it["tag"] = "主线接力"
            relay.append(it)
        elif st == 1 and tier_ok(it):
            it["tag"] = "低位潜伏"
            # ── 首板 p_break 校准（2026-08-29 回测确诊：540 条 rec_picks）──
            # 低位潜伏整体收红率仅 28.8%，细分后真凶是 p_break≥78 的首板：
            #   p_break<70: 14条 胜率78.6% 均值+3.97%（真金）
            #   70~78:      21条 57.1% +1.51%
            #   ≥78:        103条 ~37% ≈0（大样本亏损源）
            # 标注式处理（与 recveto V1 同思路）：不一刀切拦掉，降权 8% + 警示标，
            # 排序自然靠后，仍保留挖掘首板次日买点的能力。
            if pb >= 78 and not it.get("risk_flag"):
                it["risk_flag"] = "⚠"
                it.setdefault("risks", [])
                _pb_risk = "首板断板概率 %.0f%% 偏高，历史胜率 ~37%%，仅轻仓试错" % pb
                if _pb_risk not in it["risks"]:
                    it["risks"] = ([_pb_risk] + it["risks"])[:3]
                it["score"] = round(sc * 0.92, 1)
                sc = it["score"]
            ambush.append(it)
        elif sc >= 40:
            # 首板但无明确板块合力：仍纳入接力候选，避免推荐板块整体为空
            it["tag"] = "主线接力"
            relay.append(it)
    # 避险兜底：若仍为空（当日高位断板概率均低），用断板概率最高者补全
    if not avoid:
        for it in sorted(items, key=lambda x: -(x.get("p_break") or 0))[:3]:
            it["tag"] = "高位风险"
            avoid.append(it)
    # 推荐兜底：极端情况下 core/relay 仍为空时，用评分前若干补全，保证板块非空
    if not core and not relay:
        for it in items[:8]:
            it.setdefault("tag", "主线接力")
            relay.append(it)
    # 空标签兜底（2026-08-29：实测 rec.all 有 11 只 tag=undefined）——
    # 首板无板块合力且 sc<40 不落任何桶，给「观察」中性标签，前端不再显示空白。
    for it in items:
        it.setdefault("tag", "观察")
    # ── 分桶内重排序（2026-08-28 用户反馈排序错乱）──
    # 分组循环前 items 虽按原分降序，但循环中 WARN 负反馈会把 score×0.92 降权，
    # 桶内顺序不再等于新分降序 → 每个桶输出前必须按最终 score 重排一次。
    core.sort(key=lambda x: -(x.get("score") or 0))
    relay.sort(key=lambda x: -(x.get("score") or 0))
    ambush.sort(key=lambda x: -(x.get("score") or 0))
    avoid.sort(key=lambda x: -(x.get("score") or 0))
    # 仓位与策略
    sc = sent["score"]
    if sc >= 72:
        pos, ps = "6-8 成", "情绪高位，重点做主线龙头的加速段，但严守当日不及预期即走的纪律"
    elif sc >= 58:
        pos, ps = "5-7 成", "主线明确，优先低吸主线内的二板/三板，回避无合力的孤票"
    elif sc >= 45:
        pos, ps = "3-5 成", "结构性行情，以低位首板和主线补涨为主，不追高位连板"
    elif sc >= 30:
        pos, ps = "2-3 成", "情绪走弱，只做低位、低吸，控制单票仓位"
    else:
        # 2026-08-29 大样本证伪修正：首阴反包 786 条胜率仅 39.9%/均值-1.06%，
        # 断板反包 1683 条 40.6%/-0.86%——退潮期等反包是负期望打法，改为等新题材。
        pos, ps = "0-2 成", "退潮期空仓等待新题材或情绪冰点企稳信号（勿做首阴反包——大样本胜率仅40%）"
    strategies = [ps]
    if cyc["phase"] in ("退潮期", "冰点期"):
        # 超跌反弹大样本 13977 条胜率 55.6%/+0.50%——弱正期望，只配轻仓试探；
        # 低位首板仍是真金（p_break<70 胜率 78.6%/+3.97%）。
        strategies.append("重点观察【低位首板】（p_break<70 胜率78.6%）；超跌反弹仅轻仓试探，放弃高位接力")
    if cyc["phase"] in ("发酵期", "启动期"):
        strategies.append("主线板块的【二板梯队】性价比最高，是本阶段核心打法")
    if cyc["phase"] == "高潮期":
        strategies.append("高潮期做龙头需当日验证，隔日不及预期立即减仓")
    top_secs = [s for s in sectors if s["tier"] in ("主线", "支线")][:4]
    if top_secs:
        strategies.append("重点跟踪板块：" + "、".join("%s(%d涨停)" % (s["name"], s["zt"]) for s in top_secs))
    if relay_info and relay_info.get("phase") == "旧主线断板→接力切换":
        b = relay_info.get("broken")
        rn = "、".join(x["name"] for x in relay_info.get("relay", []))
        if b:
            strategies.append("旧主线【%s】断板退潮，跟随接力方向【%s】的前排，回避退潮主线后排跟风"
                              % (b["name"], rn))
    hi = [r for r in risks if r["streak"] >= 4]
    if hi:
        strategies.append("空间板 %s 是市场高度标杆，其走势决定次日整体接力意愿"
                          % "、".join("%s(%d板)" % (r["name"], r["streak"]) for r in hi[:3]))
    return {
        "core": core[:6], "relay": relay[:10], "ambush": ambush[:10], "avoid": avoid[:8],
        "position": pos, "strategies": strategies,
        "env_k": round(env_k, 2),
        "all": items[:60],
    }


def late_session_plan(data):
    """尾盘决策通道（14:45）：用当日全天数据给次日开盘做「双确认」预判。

    与盘前 08:50 预判互补：盘前只有昨日数据（预判），尾盘有当日全天量价（确认）。
    产出三类清单（纯聚合当日已构建好的 data，不新增行情计算）：
      · hold_confirm  —— 持有确认：今日推荐池中尾盘仍强势（现价距收盘强势 + 决策线不变）
      · watch_tomorrow —— 次日关注：今日涨停/连板票，次日竞价按决策线执行
      · exit_warn     —— 尾盘走弱警示：当日推荐票尾盘回落超阈值，次日按低开预案执行
    """
    d = data or {}
    rec = d.get("recommend") or {}
    aq_items = (d.get("auction") or {}).get("items") or {}
    hold, watch, warn = [], [], []

    for it in (rec.get("all") or [])[:40]:
        code = it.get("code")
        aq = aq_items.get(code) or {}
        pat = aq.get("pattern") or ""
        gap = aq.get("open_pct")
        entry = {
            "code": code, "name": it.get("name"), "streak": it.get("streak"),
            "tag": it.get("tag"), "worth": it.get("worth_score"),
            "close": it.get("close"),
            "auction_rule": (it.get("auction_rule") or {}).get("rule") or "",
        }
        # 涨停封住/强势形态 → 次日关注（竞价按决策线执行）
        if pat in ("一字板", "弱转强", "高开高走", "换手板") or (it.get("streak") or 0) >= 2:
            watch.append(entry)
        # 尾盘走弱：竞价强（高开）但当前涨幅不及开盘的一半 → 次日谨慎
        if pat in ("强转弱",) or ((aq.get("vol_anomaly") or {}).get("warn")):
            warn.append(entry)

    watch.sort(key=lambda x: -(x.get("worth") or 0))
    return {
        "n_watch": len(watch),
        "watch_tomorrow": watch[:10],
        "exit_warn": warn[:6],
        "note": "尾盘确认口径：次日竞价按决策线执行——高开≥2%跟进/低开≤-2%放弃/平开观望",
    }


def preopen_plan(rec, inds, relay, risks):
    """聚合推荐/板块/接力/风险，生成简洁「盘前策略」（看板卡片 + 盘前推送共用）。

    直接复用 recommend() 已算好的 position(仓位) 与 strategies(策略要点)，
    再补充：主线预判(板块 tier=主线)、接力方向(sector_relay)、关注池(核心前排+接力前排)、
    风险提醒(断板概率最高者)。纯聚合，不引入新模型，零额外行情计算。
    """
    rec = rec or {}
    inds = inds or []
    relay = relay or {}
    risks = risks or []
    mains = [s["name"] for s in inds if s.get("tier") == "主线"][:3]
    relay_dir = [x["name"] for x in relay.get("relay", [])] if relay.get("available") else []

    def _wi(it, reason):
        # 竞价决策线透传（2026-08-29 大样本回测：次日开盘溢价是最强执行信号，
        # 高开≥2%胜率70%+ / 低开≤-2%胜率仅17~38%，13 个月方向 100% 一致）
        ar = it.get("auction_rule") or auction_discipline(it.get("streak"))
        return {"name": it.get("name"), "code": it.get("code"),
                "streak": it.get("streak"), "reason": reason,
                "relay_dir": bool(it.get("relay_dir")),
                "auction_rule": ar.get("rule") or ""}

    seen, watch = set(), []
    for it in (rec.get("core") or [])[:6]:
        watch.append(_wi(it, "核心连板前排")); seen.add(it.get("code"))
    for it in (rec.get("relay") or []):
        if it.get("code") in seen:
            continue
        if it.get("relay_dir"):
            watch.append(_wi(it, "接力方向前排")); seen.add(it.get("code"))
    watch = watch[:8]

    rk = sorted(risks, key=lambda x: -(x.get("p_break") or 0))[:3]
    risk_txt = ["%s（%d板，断板概率%.0f%%）" % (r.get("name"), r.get("streak", 0), r.get("p_break") or 0)
                for r in rk]

    return {
        "position": rec.get("position") or "—",
        "strategies": rec.get("strategies") or [],
        "main_line": mains,
        "relay_dir": relay_dir,
        "watch": watch,
        "risks": risk_txt,
    }


def sector_day_forecast(data, topn=10):
    """盘前『板块当日涨跌预判』（用户需求：结合板块预测当日涨跌，给关注票操作说明）。

    纯聚合已构建好的 data，不新增行情计算：
      · sectors.industry  昨日板块涨停家数/最高连板/板块涨跌/主力净额/主线层级
      · sector_relay      接力方向（新崛起/加速 + 晋级确定性）与退潮主线
      · money.boards_in/out 昨日板块主力资金净流入/流出（真金白银）
      · regime            连板情绪阶段（过热→兑现压力，退潮→普跌压力）
      · global_market     外围定调（弱证据，只取方向）

    返回 {"__market__": {...}, 板块名: {"dir","score","why","evidence"}}。
    dir: 偏强(score≥62) / 震荡(43~61) / 偏弱(≤42)；
    __market__ 给出大盘环境定调，供关注票整体仓位建议引用。
    """
    d = data or {}
    inds = ((d.get("sectors") or {}).get("industry") or [])
    relay_d = d.get("sector_relay") or {}
    money = d.get("money") or {}
    regime = d.get("regime") or {}
    g = d.get("global_market") or {}

    # ---- 市场环境系数（所有板块共用）----
    env = 0
    env_why = []
    rl = (regime.get("level") or "")
    if "过热" in rl or "高潮" in rl:
        env -= 5
        env_why.append("连板情绪过热（兑现压力大）")
    elif "退潮" in rl or "冰点" in rl:
        env -= 8
        env_why.append("连板情绪退潮（赚钱效应弱）")
    elif "回暖" in rl or "升温" in rl or "修复" in rl:
        env += 5
        env_why.append("连板情绪回暖")
    _gs = (g.get("signal") or "") if g.get("available") else ""
    if any(k in _gs for k in ("偏空", "走弱", "承压", "下跌")):
        env -= 4
        env_why.append("外围偏空")
    elif any(k in _gs for k in ("偏多", "走强", "上涨", "乐观")):
        env += 4
        env_why.append("外围偏多")
    total_main = money.get("total_main_net")
    if isinstance(total_main, (int, float)):
        if total_main > 50:
            env += 3
            env_why.append("全市场主力大幅净流入")
        elif total_main < -50:
            env -= 3
            env_why.append("全市场主力净流出")

    relay_names = {x.get("name"): x for x in (relay_d.get("relay") or [])}
    broken_name = ((relay_d.get("broken") or {}) or {}).get("name")
    in_boards = {b.get("name"): b for b in (money.get("boards_in") or [])}
    out_boards = {b.get("name"): b for b in (money.get("boards_out") or [])}

    out = {}
    for a in inds:
        nm = a.get("name")
        if not nm:
            continue
        sc = 50.0
        ev = []
        tier = a.get("tier") or "零星"
        if tier == "主线":
            sc += 12
            ev.append("昨日主线（%d家涨停/%d连板）" % (a.get("zt") or 0, a.get("max_lb") or 0))
        elif tier == "支线":
            sc += 5
            ev.append("昨日支线（%d家涨停）" % (a.get("zt") or 0))
        else:
            sc -= 3
        zt = a.get("zt") or 0
        if zt >= 5:
            sc += 14
        elif zt >= 3:
            sc += 8
        r = relay_names.get(nm)
        if r:
            bump = 15 if r.get("kind") == "加速" else 12
            sc += bump * (0.5 + 0.5 * ((r.get("certainty") or 0) / 100.0))
            ev.append("接力方向·%s（确定性%d%%）" % (r.get("kind"), r.get("certainty") or 0))
        if broken_name and nm == broken_name:
            sc -= 12
            ev.append("退潮主线（涨停数从%d降至%d）" % (
                ((relay_d.get("broken") or {}).get("peak_zt") or 0),
                ((relay_d.get("broken") or {}).get("latest_zt") or 0)))
        if nm in in_boards:
            sc += 22
            ev.append("主力净流入 %.1f亿" % (in_boards[nm].get("net") or 0))
            direct = True
        elif nm in out_boards:
            sc -= 10
            ev.append("主力净流出 %.1f亿" % abs(out_boards[nm].get("net") or 0))
            direct = True
        pct = a.get("pct")
        if isinstance(pct, (int, float)):
            sc += clamp(pct, -2, 3) * 3
        # 环境分打折叠加：大盘环境是背景而非板块自身动能，权重 0.6，
        # 避免「市场热 → 所有板块一律偏强」的失真（弱板块仍应显弱）。
        sc = clamp(sc + env * 0.6, 0, 100)
        if sc >= 62:
            dirn = "偏强"
        elif sc <= 42:
            dirn = "偏弱"
        else:
            dirn = "震荡"
        out[nm] = {"dir": dirn, "score": round(sc), "why": "、".join(ev[:3]) or "无额外信号",
                   "evidence": ev[:3], "env": round(env, 1)}

    # 主力资金净流出的板块即便不在涨幅榜前列，也必须纳入（否则预判只会一边倒偏强）
    for b in (money.get("boards_out") or []):
        nm = b.get("name")
        if not nm or nm in out:
            continue
        sc = clamp(50 - 10 + env * 0.6, 0, 100)
        out[nm] = {"dir": "偏弱" if sc <= 42 else "震荡", "score": round(sc),
                   "why": "主力净流出 %.1f亿" % abs(b.get("net") or 0),
                   "evidence": ["主力净流出 %.1f亿" % abs(b.get("net") or 0)],
                   "env": round(env, 1)}

    # 输出配比：偏强 TOP6 + 偏弱 BOTTOM4（对照呈现，避免只报喜不报忧）
    ranked = sorted(out.items(), key=lambda kv: -kv[1]["score"])
    n_strong = max(1, int(topn * 0.6))
    res = {k: v for k, v in ranked[:n_strong]}
    for k, v in ranked[-(topn - n_strong):]:
        if k not in res:
            res[k] = v
    mkt_score = clamp(50 + env, 0, 100)
    res["__market__"] = {
        "score": round(mkt_score),
        "dir": "偏强" if mkt_score >= 58 else ("偏弱" if mkt_score <= 42 else "震荡"),
        "why": "、".join(env_why[:3]) or "无强烈环境信号",
        "env": round(env, 1),
    }
    return res


def screen_uptrend(u, date, code2boards=None, topn=12):
    """趋势向上选股：在全市场 K 线中筛选『均线多头排列 + 价格站上短均 + MA20 上行
    + 量能配合』且非当日涨停的趋势票，作为主升段低吸候选。
    返回结构与 recommend() 兼容（含 trend_meta 供前端渲染），可直接并入 data['recommend']['trend']。"""
    if not u or not u.dates:
        return []
    zt_today = u.zt.get(date, set())
    bh = benchmark_heat(u, date)   # 市场热度（以标杆趋势股交易额度为核心）
    heat_boost = {"热": 6, "温": 0, "冷": -8}.get(bh["level"], 0)

    def industry_of(code):
        boards = (code2boards or {}).get(code) or []
        return next((n for _, n, k in boards if k == "industry"), "—")

    cands = []
    for code, bs in u.bars.items():
        hist = [b for b in bs if b["d"] <= date]
        if len(hist) < 25:
            continue
        name = u.stocks.get(code, {}).get("name", code)
        if not name or "ST" in name or "*" in name or "退" in name:
            continue
        closes = [b["c"] for b in hist]
        last = hist[-1]
        lim = u.lim.get(code, 10.0)
        # 排除当日涨停（涨停已在连板板块呈现，避免重复）
        if code in zt_today or (last.get("pct") or 0) >= lim - 0.5:
            continue
        # 流动性过滤：成交额过低（< 1.2 亿）不选
        if (last.get("amt") or 0) < 1.2e8:
            continue
        ma5 = mean(closes[-5:]); ma10 = mean(closes[-10:]); ma20 = mean(closes[-20:])
        if ma20 <= 0:
            continue
        # MA20 五日前的斜率基准
        ma20_prev = mean(closes[-25:-5]) if len(closes) >= 25 else ma20
        price = closes[-1]
        align = (ma5 > ma10 > ma20) and (price > ma5)
        slope20 = ((ma20 - ma20_prev) / ma20_prev * 100) if ma20_prev else 0
        # 近 5 日真实日涨幅（用户要求：趋势票应有 3-5 个点/日的真实涨幅，而非
        # “技术多头、实则横盘”）。avg_daily 命中 3-5% 带在评分中加权最高。
        last5 = hist[-5:]
        daily_pcts = [b.get("pct") or 0 for b in last5]
        avg_daily = mean(daily_pcts)                  # 近 5 日日均涨幅(%)
        up_days = sum(1 for x in daily_pcts if x > 0)
        flat_days = sum(1 for x in daily_pcts if abs(x) < 1.0)   # 横盘日（几乎无波动）
        momentum = (price / ma20 - 1) * 100          # 距 MA20 偏离%
        vol20 = mean([b["v"] for b in hist[-21:-1]]) if len(hist) > 5 else (last["v"] or 1)
        vol_ratio = (last["v"] / vol20) if vol20 else 1
        # ---- 硬门槛（双通道，2026-08-28 用户反馈金牛化工型缓坡趋势被误杀）----
        # 主通道：真实进攻型趋势——『均线多头 + 近5日日均涨幅≥2% + 至少4天收涨 + 横盘日≤1』。
        # 缓坡通道：MA20 斜率≥1.5% 且 中期(20日)涨幅≥8% 的慢牛——日均涨幅不足 2% 但
        #   趋势结构扎实（金牛化工实测：日均1.99% 差 0.01 被旧门槛刷掉，slope20=7.88%）。
        primary = align and avg_daily >= 2.0 and up_days >= 4 and flat_days <= 1
        gain20 = (price / mean(closes[-21:-1]) - 1) * 100 if len(closes) >= 21 else 0
        slow_channel = (align and avg_daily >= 1.0 and up_days >= 3
                        and flat_days <= 2 and slope20 >= 1.5 and gain20 >= 8.0)
        if not (primary or slow_channel):
            continue
        # ---- 趋势双态：加速 / 匀速 / 放缓（用户需求：区分「趋势缓」与「加速」两类）----
        # 加速度 = 近5日日均涨幅 ÷ 近20日日均涨幅；>1.45 说明近期涨速明显快于自身中期节奏。
        # 另用 MA20 斜率的 5 日变化量交叉验证，避免单日噪音导致误判。
        daily20 = gain20 / 20.0
        ma20_prev2 = mean(closes[-30:-10]) if len(closes) >= 30 else ma20_prev
        slope20_prev2 = ((ma20_prev - ma20_prev2) / ma20_prev2 * 100) if ma20_prev2 else 0.0
        slope_delta = slope20 - slope20_prev2
        trend_state, accel = classify_trend_state(avg_daily, daily20, slope_delta)
        # ---- 评分（趋势强度以日均涨幅为主，均线结构为辅）----
        sc = 0.0
        sc += 28 if align else 0                       # 趋势结构
        sc += clamp((avg_daily - 1.0) / 4.0, 0, 1) * 34   # 日均涨幅（1%→0，5%→34，命中 3-5% 带）
        sc += up_days / 5.0 * 13                        # 上涨连续性
        sc += clamp(slope20 / 1.5, 0, 9)                # MA20 斜率（趋势加速）
        if 5 <= momentum <= 45:
            sc += 14                                    # 主升段未严重透支
        elif momentum > 0:
            sc += max(0.0, 14 - (momentum - 45) * 0.5)
        else:
            sc -= 6
        if 1.0 <= vol_ratio <= 3.0:
            sc += 10                                    # 温和放量
        elif vol_ratio > 5:
            sc -= 6                                     # 过旺
        sc += heat_boost                               # 市场热度（标杆成交额）调节
        # 缓坡通道票斜率分加成：慢牛的 MA20 斜率是核心动能，弥补日均涨幅吃亏
        if slow_channel and not primary:
            sc += clamp(slope20 / 3.0, 0, 10)
        # 趋势双态调节：加速中的趋势更值得跟，涨速衰减的慢牛要打折
        if trend_state == "加速上行":
            sc += 5
        elif trend_state == "增速放缓":
            sc -= 5
        sc = clamp(sc, 0, 100)
        worth = clamp(0.5 * sc + 0.5 * (42 if momentum <= 45 else 12), 0, 100)
        # 趋势带三级判定（用于前端徽章与操作建议）：
        #   主升强趋势（日均≥3%）/ 稳健上行（≥2%）/ 趋势平缓（缓坡通道慢牛）
        if avg_daily >= 3.0:
            band = "主升强趋势"
        elif avg_daily >= 2.0:
            band = "稳健上行"
        else:
            band = "趋势平缓"
        reasons = ["均线多头排列（MA5>MA10>MA20），短中期趋势向上"]
        reasons.append("近 5 日日均涨幅 %.1f%%（%s），非横盘" % (avg_daily, band))
        if trend_state == "加速上行":
            reasons.append("涨速加速中（近5日日均 %.1f%% 为近20日 %.1f%% 的 %.1f 倍）"
                           % (avg_daily, daily20, accel))
        elif trend_state == "增速放缓":
            reasons.append("涨速放缓（近5日日均 %.1f%% vs 近20日 %.1f%%），注意兑现节奏"
                           % (avg_daily, daily20))
        if slope20 > 0.5:
            reasons.append("MA20 上行斜率 %.1f%%，趋势向上" % slope20)
        if slow_channel and not primary:
            reasons.append("慢牛通道：近20日 +%.0f%%、斜率陡峭但节奏平缓，适合回踩低吸" % gain20)
        if up_days >= 4:
            reasons.append("近 5 日 %d 天收涨，上攻连续性好" % up_days)
        if 5 <= momentum <= 45:
            reasons.append("距 MA20 偏离 +%.1f%%，主升段尚未透支" % momentum)
        if 1.0 <= vol_ratio <= 3.0:
            reasons.append("量能温和放大（约 %.1f 倍），资金持续介入" % vol_ratio)
        if bh["level"] == "热":
            reasons.append("标杆趋势股成交额放大(%.2fx)、%d/%d 仍处多头，趋势抱团环境温热"
                           % (bh["avg_amt_ratio"], int(round(bh["share_trending"] * len(bh["stocks"]))), len(bh["stocks"])))
        risks = []
        if bh["level"] == "冷":
            risks.append("标杆趋势股成交额缩量、抱团松动，趋势票需更严格止损")
        if momentum > 45:
            risks.append("阶段涨幅偏大（偏离 MA20 +%.0f%%），注意回踩" % momentum)
        if vol_ratio > 4:
            risks.append("放量过猛（%.1f 倍），警惕短线分歧" % vol_ratio)
        if (last.get("turn") or 0) > 12:
            risks.append("换手 %.0f%%，短线筹码松动" % last["turn"])
        risks.append("非涨停趋势票，需结合量价与大盘节奏设止损")
        cands.append({
            "code": code, "name": name, "streak": 0, "industry": industry_of(code),
            "close": round(price, 2),
            "float_mv": u.stocks.get(code, {}).get("float_mv"),
            "turn": round(last.get("turn") or 0, 2),
            "quality": 0, "p_continue": 0, "demon": 0,
            "score": round(sc, 1), "worth_score": round(worth, 1),
            "trend_meta": {
                "ma5": round(ma5, 2), "ma10": round(ma10, 2), "ma20": round(ma20, 2),
                "align": True, "up_days": up_days, "avg_daily": round(avg_daily, 2),
                "band": band,
                "momentum_pct": round(momentum, 1), "vol_ratio": round(vol_ratio, 2),
                "slope20": round(slope20, 2),
                # 趋势双态：加速上行 / 匀速上行 / 增速放缓（用户要求区分「缓」与「加速」）
                "trend_state": trend_state, "accel": round(accel, 2),
                "slope_delta": round(slope_delta, 2), "daily20": round(daily20, 2),
                "slow_channel": bool(slow_channel and not primary),
            },
            "reasons": reasons[:5], "risks": risks[:3],
        })
    cands.sort(key=lambda x: -x["score"])
    return cands[:topn]


def classify_trend_state(avg_daily, daily20, slope_delta=0.0):
    """趋势双态分类（纯函数，可单测）：加速上行 / 匀速上行 / 增速放缓。

    加速度 = 近5日日均涨幅 ÷ 近20日日均涨幅：
      ≥1.45（或 MA20 斜率 5 日抬升 ≥0.8）→ 加速上行
      ≤0.70（或 MA20 斜率 5 日回落 ≥0.8）→ 增速放缓
      其余 → 匀速上行
    返回 (state, accel)。
    """
    accel = (avg_daily / daily20) if (daily20 and daily20 > 0.05) else 1.0
    if accel >= 1.45 or slope_delta >= 0.8:
        return "加速上行", round(accel, 2)
    if accel <= 0.70 or slope_delta <= -0.8:
        return "增速放缓", round(accel, 2)
    return "匀速上行", round(accel, 2)


def institution_evidence(code, data=None, industry=None):
    """机构/主力介入证据聚合（用户需求：及时获取机构介入情况并在推荐里点名）。

    证据来源（全部可选、缺数据源则自动降级）：
      · lhbseats.top    龙虎榜净买入/净卖出（个股级，最直接）
      · blocktrade.inst 大宗交易「机构专用」席位买卖方向
      · blocktrade.top  大宗折价出货（负向）
      · money.boards_in/out  所属行业板块主力资金净流入/流出（行业级）
      · margin.delta_yi 两融余额变化（杠杆资金环境，弱证据）
      · seats.hits      知名游资席位（负向：偏游资博弈而非机构主导）

    返回 {"level": "强/中/弱/无", "score": int, "tags": [...], "action": "..."}，
    纯读数据不做预测，无证据时 level="无"（前端/推送不展示徽标）。
    """
    d = data or {}
    score = 0
    tags = []
    direct = False      # 是否存在「个股/行业级」直接证据（两融这类市场环境证据不算）

    def _f(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    # ① 龙虎榜个股净买（最直接的主力介入证据）
    for row in ((d.get("lhbseats") or {}).get("top") or []):
        if isinstance(row, dict) and row.get("code") == code:
            net = _f(row.get("net_yi"))
            if net > 0:
                score += 40 if net >= 0.5 else 28
                tags.append("龙虎榜净买 %.2f亿" % net)
            elif net < 0:
                score -= 18
                tags.append("龙虎榜净卖 %.2f亿" % abs(net))
            direct = True
            break
    else:
        # top10 之外只要当日龙虎榜净买入为正，也算资金介入（弱一档）
        for row in ((d.get("lhbseats") or {}).get("net_buy") or []):
            if isinstance(row, dict) and row.get("code") == code:
                score += 22
                tags.append("龙虎榜净买 %.2f亿" % _f(row.get("net_yi")))
                direct = True
                break
    # ② 大宗交易「机构专用」席位方向
    for row in ((d.get("blocktrade") or {}).get("inst") or []):
        if isinstance(row, dict) and row.get("code") == code:
            amt = _f(row.get("amt_yi"))
            if row.get("side") == "buy":
                score += 35
                tags.append("大宗机构专用买入 %.2f亿" % amt)
            else:
                score -= 22
                tags.append("大宗机构专用卖出 %.2f亿" % amt)
            direct = True
            break
    # ③ 大宗折价出货（≥8% 视为减持信号）
    for row in ((d.get("blocktrade") or {}).get("top") or []):
        if isinstance(row, dict) and row.get("code") == code:
            disc = _f(row.get("discount"))
            if disc <= -8:
                score -= 20
                tags.append("大宗折价 %.1f%% 出货" % disc)
                direct = True
            break
    # ③b 大宗溢价成交（接盘方愿意加价拿货 = 资金主动介入）
    for row in ((d.get("blocktrade") or {}).get("premium") or []):
        if isinstance(row, dict) and row.get("code") == code:
            score += 22
            tags.append("大宗溢价 %.1f%% 接盘 %.2f亿" % (_f(row.get("discount")), _f(row.get("amt_yi"))))
            direct = True
            break
    # ④ 所属行业板块主力资金（行业级资金介入）
    mo = d.get("money") or {}
    if industry and industry != "—":
        _hit = False
        for b in (mo.get("boards_in") or []):
            if industry in (b.get("name") or ""):
                score += 20
                tags.append("所属板块主力净流入 %.1f亿" % _f(b.get("net")))
                _hit = True
                break
        if not _hit:
            for b in (mo.get("boards_out") or []):
                if industry in (b.get("name") or ""):
                    score -= 12
                    tags.append("所属板块主力净流出 %.1f亿" % abs(_f(b.get("net"))))
                    break
    # ⑤ 两融余额（杠杆资金环境，弱证据）
    dl = (d.get("margin") or {}).get("delta_yi")
    if dl is not None:
        if dl > 0:
            score += 8
            tags.append("两融余额 +%.0f亿（杠杆资金回补）" % _f(dl))
        elif _f(dl) < -20:
            score -= 6
    # ⑥ 游资席位（负向：说明是游资博弈而非机构建仓）
    for row in ((d.get("seats") or {}).get("hits") or []):
        if isinstance(row, dict) and row.get("code") == code:
            score -= 8
            tags.append("游资席位博弈（非机构主导）")
            direct = True
            break

    score = int(clamp(score, -60, 100))
    # 只有市场环境证据（如两融余额回升）不构成「机构介入」结论——避免全市场一刀切打标
    if not direct:
        return {"level": "无", "score": score, "tags": tags[:4], "action": ""}
    if score >= 55:
        level, action = "强", "机构/主力介入明确 → 可跟随建仓，回踩买区加仓"
    elif score >= 20:
        level, action = "中", "有资金介入迹象 → 轻仓试探，回踩买区加仓"
    elif score > 0:
        level, action = "弱", "暂无强机构介入 → 按趋势纪律小仓参与"
    else:
        level, action = "无", ""
    return {"level": level, "score": score, "tags": tags[:4], "action": action}


def fuse_recommend(data):
    """融合多引擎信号，输出统一『最优解』清单（按综合分排序 Top20）。

    融合来源：连板接力(recommend.all) + 趋势主升(recommend.trend) + 游资席位(lhbseats)
    + 主线题材(theme) + 连续信号(signals) + 买卖区间(zones 破位/买点)。

    每只标的聚合各引擎证据(evidence)并给 fusion_score；同票被多引擎同时命中（共振）
    者分数更高、可信度更强，避免『单模型每天变』的碎片推荐。
    """
    rec = (data or {}).get("recommend") or {}
    allp = rec.get("all") or []
    trend = rec.get("trend") or []
    momentum = rec.get("momentum") or []
    seats = (data or {}).get("lhbseats") or {}
    theme_d = (data or {}).get("theme") or {}
    signals_d = (data or {}).get("signals") or {}
    zones_d = (data or {}).get("zones") or {}

    # 当日主线题材板块集合（命中主线给加成）
    mainline_secs = set()
    for s in (theme_d.get("mainline") if isinstance(theme_d, dict) else []) or []:
        if s.get("tier") == "主线":
            mainline_secs.add(s.get("name"))
    # 龙虎榜活跃标的（reasons 是 [标签, 数量] 二元列表，top 才是 dict——用 isinstance 守卫）
    seat_codes = set()
    for row in (list(seats.get("reasons") or []) + list(seats.get("top") or [])):
        if isinstance(row, dict) and row.get("code"):
            seat_codes.add(row["code"])
    # 连续信号活跃标的
    sig_codes = set()
    for grp in (signals_d.get("groups") or []):
        for it in (grp.get("items") or []):
            if it.get("code"):
                sig_codes.add(it["code"])
    # 区间动作映射（破位负向、买点正向）
    zone_map = {}
    band_map = {}
    for it in (zones_d.get("items") or []):
        if it.get("code"):
            zone_map[it["code"]] = it.get("action")
            band_map[it["code"]] = it

    agg = {}
    def grab(code):
        return agg.setdefault(code, {"code": code, "name": "", "industry": None,
                                     "score_parts": []})
    for it in allp:
        c = it["code"]; g = grab(c)
        g["name"] = g["name"] or it.get("name")
        g["industry"] = g["industry"] or it.get("industry")
        g["score_parts"].append(("连板接力", it.get("worth_score") or 0, it.get("tag") or ""))
    for it in trend:
        c = it["code"]; g = grab(c)
        g["name"] = g["name"] or it.get("name")
        g["industry"] = g["industry"] or it.get("industry")
        band = (it.get("trend_meta") or {}).get("band") or ""
        g["score_parts"].append(("趋势主升", it.get("worth_score") or 0, band))
    # 强动量 · 连板余波通道并入融合（2026-08-29：此前漏算，连板基因延续票进不了最优解）
    for it in momentum:
        c = it["code"]; g = grab(c)
        g["name"] = g["name"] or it.get("name")
        g["industry"] = g["industry"] or it.get("industry")
        mband = (it.get("momentum_meta") or {}).get("band") or ""
        g["score_parts"].append(("强动量", it.get("worth_score") or 0, mband))
    for c in seat_codes:
        grab(c)["score_parts"].append(("游资席位", 55, "龙虎榜活跃"))
    for c in sig_codes:
        grab(c)["score_parts"].append(("连续信号", 50, "跨日硬信号共振"))
    for c, g in agg.items():
        if g.get("industry") in mainline_secs:
            g["score_parts"].append(("主线题材", 10, "所属板块为当日主线"))
        zact = zone_map.get(c)
        if zact in ("加仓提示", "回踩买入区"):
            g["score_parts"].append(("区间买点", 12, zact))
        elif zact == "破位卖出":
            g["score_parts"].append(("区间破位", -25, zact))
        elif zact in ("逼近卖出", "突破持有"):
            g["score_parts"].append(("区间卖点", 5, zact))
    # 代码 → 名称兜底映射：游资席位/龙虎榜命中的票常不在推荐池里，
    # 否则前端会渲染出「名称为空」的一行（实测出现过 `****(—) 综合55分`）。
    _name_map = {}
    for it in list(allp) + list(trend) + list(momentum):
        if it.get("code") and it.get("name"):
            _name_map.setdefault(it["code"], it["name"])
    for row in list((seats or {}).get("top") or []):
        if isinstance(row, dict) and row.get("code") and row.get("name"):
            _name_map.setdefault(row["code"], row["name"])
    for row in (((data or {}).get("seats") or {}).get("hits") or []):
        if isinstance(row, dict) and row.get("code") and row.get("name"):
            _name_map.setdefault(row["code"], row["name"])

    # 机构/主力介入（新增引擎证据：龙虎榜净买 + 大宗机构专用 + 板块主力资金 + 两融）
    # 2026-08-29: 扫描范围补上 momentum 通道（此前连板余波票不做机构证据聚合）
    _inst_done = set()
    for it in list(allp) + list(trend) + list(momentum):
        c = it.get("code")
        if not c or c in _inst_done:
            continue
        _inst_done.add(c)
        if any(p[0] == "机构介入" for p in agg.get(c, {}).get("score_parts", [])):
            continue
        ev = institution_evidence(c, data, industry=it.get("industry"))
        if ev["level"] in ("强", "中"):
            grab(c)["score_parts"].append(
                ("机构介入", 20 if ev["level"] == "强" else 12,
                 "、".join(ev["tags"][:2]) or ("机构介入" + ev["level"])))
        elif ev["level"] == "无" and ev["score"] < 0:
            grab(c)["score_parts"].append(("机构介入", -10, "、".join(ev["tags"][:1]) or "资金流出"))

    out = []
    for c, g in agg.items():
        parts = g["score_parts"]
        if not parts:
            continue
        base = sum(max(0, p[1]) for p in parts) / float(len(parts))
        penalty = sum(min(0, p[1]) for p in parts)
        n_eng = len(set(p[0] for p in parts))
        reso = 1.0 + min(0.25, 0.08 * (n_eng - 1))   # 多引擎共振加权
        fusion = clamp(base * reso + penalty, 0, 100)
        ev = [{"engine": p[0], "score": p[1], "note": p[2]} for p in parts]
        # 盈亏比 R：期望收益空间(卖区下沿-现价) / 风险空间(现价-止损)
        r = None
        bd = band_map.get(c)
        if bd:
            _sl = bd.get("sell_zone") or [None, None]
            _st = bd.get("stop")
            _cl = bd.get("close")
            if _sl and _sl[0] and _st and _cl:
                denom = _cl - _st
                if denom > 0:
                    r = round((_sl[0] - _cl) / denom, 2)
        if r is not None and r >= 2.0:
            ev.append({"engine": "盈亏比", "score": 0, "note": "R=%.1f 赔率优" % r})
        out.append({"code": c, "name": g["name"] or _name_map.get(c) or c,
                    "industry": g.get("industry"),
                    "fusion_score": round(fusion, 1), "n_engine": n_eng,
                    "r": r, "evidence": ev})
    out.sort(key=lambda x: -x["fusion_score"])
    return out[:20]


def sector_trend_recommend(u, date, code2boards=None, sectors=None, topn=6):
    """板块趋势推荐：把趋势向上选股（screen_uptrend）命中的个股按行业聚类，
    找出『趋势抱团最强』的板块，输出板块级推荐 + 领涨标的。

    与 sector_heat（按涨停家数排主线板块）互补：这里抓的是「多只票悄悄走主升、
    但没几只涨停」的板块（如被动元件、医疗服务），用板块内趋势票数量、平均强度、
    主升占比、平均日均涨幅综合排序，专门接住 sector_heat 漏掉的「温趋势板块」。

    命名对齐用户诉求：类似风华高科（被动元件龙头，走趋势而非连板）这类票，
    往往整板块多票同步趋势向上却涨停稀少 → 本函数把它们聚成「板块推荐」。"""
    trend = screen_uptrend(u, date, code2boards, topn=40)
    if not trend:
        return []
    from collections import defaultdict
    by_ind = defaultdict(list)
    for it in trend:
        ind = it.get("industry") or "—"
        if ind == "—":
            continue
        by_ind[ind].append(it)
    rows = []
    for ind, items in by_ind.items():
        n = len(items)
        avg_score = mean([x["score"] for x in items])
        avg_daily = mean([(x.get("trend_meta") or {}).get("avg_daily", 0) for x in items])
        strong = sum(1 for x in items
                     if (x.get("trend_meta") or {}).get("band") == "主升强趋势")
        # 板块趋势强度：趋势票数（抱团信号权重最高）+ 平均强度 + 主升占比 + 日均涨幅
        s = 0.0
        s += clamp((n - 1) / 3.0, 0, 1) * 38        # 趋势票数量：1只→0，4只+→38
        s += clamp(avg_score / 100.0, 0, 1) * 30     # 平均趋势分
        s += (strong / n) * 18                       # 主升占比
        s += clamp((avg_daily - 2) / 3.0, 0, 1) * 14  # 平均日均涨幅：2%→0，5%+→14
        s = clamp(s, 0, 100)
        leads = sorted(items, key=lambda x: -x["score"])[:6]
        rows.append({
            "sector": ind, "kind": "industry",
            "trend_count": n, "avg_score": round(avg_score, 1),
            "avg_daily": round(avg_daily, 2), "strong_count": strong,
            "strength": round(s, 1),
            "leads": [{"code": x["code"], "name": x["name"], "score": round(x["score"], 1),
                       "band": (x.get("trend_meta") or {}).get("band"),
                       "avg_daily": (x.get("trend_meta") or {}).get("avg_daily")}
                      for x in leads],
        })
    rows.sort(key=lambda r: (-r["strength"]))
    # ---- 主线 / 龙头 判定 ----
    # 涨停主线：sector_heat 按行业聚合的 tier（主线=涨停≥5 且最高连板≥2）。
    # 趋势主线：本函数按趋势强度排进前列的板块。两者同时成立 ⇒ 双主线共振。
    heat_tier = {s["name"]: s.get("tier") for s in (sectors or []) if s.get("kind") == "industry"}
    for rank, r in enumerate(rows):
        ht = heat_tier.get(r["sector"])
        is_mainline = (rank < 3) or (ht == "主线") or (r["strength"] >= 82)
        r["heat_tier"] = ht or "—"
        r["tier"] = "主线" if is_mainline else "支线"
        r["resonance"] = bool(ht == "主线")   # 同时被涨停主线确认 ⇒ 双主线
        if is_mainline and r["leads"]:        # 龙头：主线板块内趋势分最高者
            r["leads"][0]["is_leader"] = True
    return rows[:topn]


def screen_momentum(u, date, code2boards=None, topn=12):
    """强动量 · 连板余波选股：捕捉『近期有连板基因 + 多头结构未破位 + 仍在强势区』
    的票——典型如风范股份（601700，连板妖股型，今天非涨停但趋势未结束）。

    与 screen_uptrend（平滑趋势，要求近5日日均涨≥2%且≥4天收涨）互补：
    本函数允许剧烈震荡/分歧，但要求『有真实连板历史 + 未跌破关键均线 + 距近期
    高点回撤可控』。这类票是「连板妖股基因」的延续，screen_uptrend 会因震荡被
    正确排除，纯连板通道又只接涨停，于是掉缝里——本函数专门接住它们。

    返回结构与 recommend() 兼容（含 momentum_meta 供前端渲染）。"""
    if not u or not u.dates:
        return []
    zt_today = u.zt.get(date, set())
    bh = benchmark_heat(u, date)
    heat_boost = {"热": 6, "温": 0, "冷": -8}.get(bh["level"], 0)

    def industry_of(code):
        boards = (code2boards or {}).get(code) or []
        return next((n for _, n, k in boards if k == "industry"), "—")

    # 近 12 个交易日窗口（统计连板基因与回撤基准）
    W = 12
    dates_upto = [d for d in u.dates if d <= date]
    win = dates_upto[-W:]
    def lu_in_win(code):
        return sum(1 for d in win if code in (u.zt.get(d) or set()))
    def max_streak_in_win(code):
        best = cur = 0
        for d in win:
            if code in (u.zt.get(d) or set()):
                cur += 1; best = max(best, cur)
            else:
                cur = 0
        return best
    def last_lu_back(code):
        """最近一次涨停距今多少个交易日（0=当日）。"""
        for k in range(len(dates_upto) - 1, -1, -1):
            if code in (u.zt.get(dates_upto[k]) or set()):
                return len(dates_upto) - 1 - k
        return 99

    cands = []
    for code, bs in u.bars.items():
        hist = [b for b in bs if b["d"] <= date]
        if len(hist) < 25:
            continue
        name = u.stocks.get(code, {}).get("name", code)
        if not name or "ST" in name or "*" in name or "退" in name:
            continue
        closes = [b["c"] for b in hist]
        highs = [b["h"] for b in hist]
        last = hist[-1]
        lim = u.lim.get(code, 10.0)
        # 排除当日涨停（涨停已在连板板块呈现，避免重复）
        if code in zt_today or (last.get("pct") or 0) >= lim - 0.5:
            continue
        amt = last.get("amt") or 0
        # 流动性过滤：成交额过低（< 1.2 亿）不选
        if amt < 1.2e8:
            continue
        # ---- 连板基因门槛：近 12 日至少 2 次涨停，或曾出现≥2连板 ----
        luc = lu_in_win(code)
        lstreak = max_streak_in_win(code)
        if luc < 2 and lstreak < 2:
            continue
        ma5 = mean(closes[-5:]); ma10 = mean(closes[-10:]); ma20 = mean(closes[-20:])
        if ma20 <= 0:
            continue
        ma20_prev = mean(closes[-25:-5]) if len(closes) >= 25 else ma20
        price = closes[-1]
        slope20 = ((ma20 - ma20_prev) / ma20_prev * 100) if ma20_prev else 0
        # 多头结构：MA20 上行 + 价格仍在 MA10 上方（允许回踩，但不可跌破 MA20）
        if slope20 <= 0 or price < ma20 or price < ma10 * 0.97:
            continue
        # 距近期（近 W 日）高点回撤可控（≤18%），避免已见顶派发的票
        hi_win = max(highs[-W:]) if len(highs) >= W else last["h"]
        drawdown = (hi_win - price) / hi_win * 100
        if drawdown > 18:
            continue
        # 近 10 日累计涨幅需为正且具强度（≥12%），确认是强动量而非阴跌反弹
        base = closes[-11]
        gain10 = (price / base - 1) * 100 if base > 0 else 0
        if gain10 < 12:
            continue
        vol20 = mean([b["v"] for b in hist[-21:-1]]) if len(hist) > 5 else (last["v"] or 1)
        vol_ratio = (last["v"] / vol20) if vol20 else 1
        recency = last_lu_back(code)  # 0=当日(已排除), 1=昨日...
        momentum_pct = (price / ma20 - 1) * 100
        # ---- 评分（连板基因权重最高，其次余波新鲜度与回撤控制）----
        sc = 0.0
        sc += min(lstreak, 4) / 4.0 * 30          # 最高连板数（连板基因核心）
        sc += min(luc, 4) / 4.0 * 14              # 窗口内涨停频次
        sc += clamp(slope20 / 1.5, 0, 10)          # MA20 斜率（趋势加速）
        sc += clamp((18 - drawdown) / 18.0, 0, 1) * 18   # 距高点回撤越小越强
        sc += clamp((8 - recency) / 8.0, 0, 1) * 14     # 最近涨停距今越近越热
        if 1.0 <= vol_ratio <= 3.5:
            sc += 8
        elif vol_ratio > 6:
            sc -= 6
        sc += heat_boost
        sc = clamp(sc, 0, 100)
        worth = clamp(0.5 * sc + 0.5 * (40 if drawdown <= 10 else 20), 0, 100)
        band = "连板余波" if recency <= 3 else "强动量延续"
        reasons = []
        reasons.append("近 %d 日 %d 次涨停（最高 %d 连板），具备连板妖股基因" % (W, luc, lstreak))
        if recency <= 3:
            reasons.append("最近一次涨停距今仅 %d 个交易日，余波未散" % recency)
        reasons.append("MA20 上行斜率 +%.1f%%，多头结构未破位" % slope20)
        if drawdown <= 10:
            reasons.append("距近 %d 日高点仅回撤 %.1f%%，仍处强势区" % (W, drawdown))
        else:
            reasons.append("自近 %d 日高点回撤 %.1f%%（≤18%%），未破位" % (W, drawdown))
        if 1.0 <= vol_ratio <= 3.5:
            reasons.append("量能温和（约 %.1f 倍），资金仍有承接" % vol_ratio)
        risks = []
        if bh["level"] == "冷":
            risks.append("标杆趋势股抱团松动，连板余波票需严格止损、快进快出")
        if drawdown > 12:
            risks.append("已从近期高点回撤 %.0f%%，警惕高位派发" % drawdown)
        if (last.get("turn") or 0) > 12:
            risks.append("换手 %.0f%%，短线筹码松动分歧大" % last["turn"])
        if recency >= 5:
            risks.append("连板余波偏冷（最近涨停距今 %d 日），延续性下降" % recency)
        risks.append("非涨停强动量票，按『趋势不破位』持有，有效跌破 MA20 即离场")
        cands.append({
            "code": code, "name": name, "streak": lstreak, "industry": industry_of(code),
            "close": round(price, 2),
            "float_mv": u.stocks.get(code, {}).get("float_mv"),
            "turn": round(last.get("turn") or 0, 2),
            "quality": 0, "p_continue": 0, "demon": 0,
            "score": round(sc, 1), "worth_score": round(worth, 1),
            "momentum_meta": {
                "ma5": round(ma5, 2), "ma10": round(ma10, 2), "ma20": round(ma20, 2),
                "slope20": round(slope20, 2),
                "lu_count": luc, "max_streak": lstreak, "recency": recency,
                "drawdown": round(drawdown, 1), "gain10": round(gain10, 1),
                "band": band, "vol_ratio": round(vol_ratio, 2),
                "momentum_pct": round(momentum_pct, 1),
            },
            "reasons": reasons[:5], "risks": risks[:3],
        })
    cands.sort(key=lambda x: -x["score"])
    return cands[:topn]
