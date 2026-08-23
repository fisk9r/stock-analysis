# -*- coding: utf-8 -*-
"""经典选股策略库 strategies.py —— 开源策略集移植（InStock 系）

来源：调研 GitHub 开源选股项目（InStock 及其衍生）中的经典选股策略，
用纯标准库按本项目数据结构（engine.Universe 日K：o/c/h/l/v/pct/amt）重写。
与 bull.py 的 10 类探测器互补去重：
  · 「突破平台」与 bull.det_platform 重复 → 不重复实现；
  · 其余均为 bull 未覆盖的独立维度。

已移植（9 个探测器，输出结构与 bull 完全一致，可直接共振合并）：
  1. vol_up       放量上涨（温和放量上攻：涨2~7% 收>开 量比≥2 额≥2亿）
  2. ma_bull      均线多头（MA30 持续向上 + 价格站稳）
  3. helipad      停机坪（放量大阳后连续3日高开小阳横盘蓄势）
  4. pull_ma120   回踩长期均线（原版回踩年线MA250；本地130日窗口用MA120近似，
                  待历史数据扩容至250日后自动升级为真年线）
  5. turtle60     海龟·唐奇安突破（收盘创60日新高，无额外约束——与
                  bull.det_new_high 的区别：不设空间/量比门槛，纯通道信号）
  6. narrow_flag  高而窄的旗形（短期近乎翻倍后的窄幅旗形整理）
  7. steady_up    稳健上行·无大幅回撤（60日涨≥15%，无单日-7%/两日-10%）
  8. low_atr      低ATR慢牛（波动率极低 + 缓步爬升；原版含基本面过滤，
                  本地无财务数据 → 纯技术版）
  9. vol_ldp      放量跌停·博反观察（当日跌停且显著放量，次日反包参照）

未移植（数据不具备，如实说明）：
  · 基本面选股（PE/PB/ROE）：本地仅日K，无财务接口；
  · 筹码分布：需换手率分布明细；
  · 61种K线形态：工程量大，后续可按需增补高频形态。

推送只给「结果」（信号名+关键数值），判断逻辑只在引擎内部，不外发。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
import store


# ----------------------------------------------------------------- 基础工具
def _hist(u, code, date, n=130):
    return u.bars_upto(code, date, n)


def _sma(vals, n):
    if len(vals) < n or n <= 0:
        return None
    return sum(vals[-n:]) / n


def _vma(hist, n):
    vs = [b["v"] or 0 for b in hist[-n:]]
    if not vs:
        return 1.0
    return sum(vs) / len(vs) or 1.0


def _vol_ratio(hist, win=5):
    if len(hist) < win + 1:
        return 1.0
    mean5 = _vma(hist[:-1], win)
    cur = hist[-1]["v"] or 0
    return round(cur / mean5, 2) if mean5 else 1.0


def _atr_pct(hist, n=14):
    """ATR(n)/现价 —— 波动率占比。"""
    if len(hist) < n + 1:
        return None
    trs = []
    for i in range(-n, 0):
        h, l = hist[i]["h"], hist[i]["l"]
        pc = hist[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    cur = hist[-1]["c"]
    if not cur:
        return None
    return (sum(trs) / n) / cur


def _pass_basic(u, code, hist, min_len=62):
    """通用过滤：ST / 退市 / 无价 / 历史过短"""
    if len(hist) < min_len:
        return None
    st = u.stocks.get(code, {})
    name = st.get("name") or ""
    if "ST" in name.upper() or "退" in name:
        return None
    cur = hist[-1]
    if not cur["c"] or cur["c"] <= 0:
        return None
    return cur, st


def _industry(code, code2boards):
    for bk, name, kind in (code2boards.get(code) or []):
        if kind == "industry" and name:
            return name
    return ""


def _mk(code, st, signal, score, tag, hist=None, code2boards=None, vr=None):
    cur = hist[-1] if hist else {}
    price = cur.get("c")
    pct = cur.get("pct")
    dd = None
    if hist and len(hist) >= 61:
        hi = max(b["h"] for b in hist[-61:-1]) or price
        dd = round((price / hi - 1.0) * 100, 1)
    return {
        "code": code, "name": st.get("name", ""),
        "signal": signal, "score": score,
        "price": round(price, 2) if price else None,
        "pct": round(pct, 2) if pct is not None else None,
        "vol_ratio": vr,
        "ind": _industry(code, code2boards) if code2boards else "",
        "dd60": dd,
        "tag": tag,
    }


# ----------------------------------------------------------------- 探测器
def det_vol_up(u, date, code2boards):
    """放量上涨：温和放量上攻（InStock 原版参数）。"""
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 30)
        r = _pass_basic(u, code, hist)
        if not r:
            continue
        cur, st = r
        lim = u.lim.get(code)
        if lim is None:
            continue
        pct = cur["pct"]
        if not (2.0 <= pct <= 7.0):
            continue
        if engine.is_limit_up(cur, lim):
            continue  # 涨停另由 bull 探测器覆盖，此处只要"温和"
        if cur["c"] <= cur["o"]:
            continue  # 必须收阳
        vr = _vol_ratio(hist)
        if vr < 2.0:
            continue
        amt = cur.get("amt") or 0
        if amt < 2e8:
            continue
        sc = 2.8 + min(1.4, (vr - 2.0) * 0.8) + min(0.8, amt / 10e8)
        tag = ("%.2f元 温和放量+%0.1f%% 量比%.1f 额%.1f亿"
               % (cur["c"], pct, vr, amt / 1e8))
        out.append(_mk(code, st, "放量上涨", round(sc, 2), tag, hist, code2boards, vr))
    return out


def det_ma_bull(u, date, code2boards):
    """均线多头：MA30 持续向上 + 价格站稳其上（InStock 核心条件）。"""
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 70)
        r = _pass_basic(u, code, hist)
        if not r:
            continue
        cur, st = r
        closes = [b["c"] for b in hist]
        ma30 = _sma(closes, 30)
        ma30b = _sma(closes[:-5], 30)
        ma30b2 = _sma(closes[:-10], 30)
        if None in (ma30, ma30b, ma30b2):
            continue
        # MA30 三点连续抬升 = 趋势持续向上
        if not (ma30 > ma30b > ma30b2 and cur["c"] > ma30):
            continue
        slope = ma30 / ma30b2 - 1
        vr = _vol_ratio(hist)
        sc = 2.6 + min(1.6, slope * 120)
        # 多头排列加分
        ma5, ma10, ma20 = (_sma(closes, 5), _sma(closes, 10), _sma(closes, 20))
        align = all(v is not None for v in (ma5, ma10, ma20)) and ma5 > ma10 > ma20
        if align:
            sc += 0.8
        if vr >= 1.2 and cur["pct"] > 0:
            sc += 0.5
        tag = ("%.2f元 MA30向上(斜率%.1f%%)%s"
               % (cur["c"], slope * 100, " 多头排列" if align else ""))
        out.append(_mk(code, st, "均线多头", round(sc, 2), tag, hist, code2boards, vr))
    return out


def det_helipad(u, date, code2boards):
    """停机坪：15日内放量大阳(pct>9.5%)，随后连续3日高开收阳横盘蓄势。"""
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 25)
        r = _pass_basic(u, code, hist, min_len=20)
        if not r:
            continue
        cur, st = r
        if len(hist) < 19:
            continue
        # 最近3日（不含今日）：高开、收阳、逐日横盘（累计涨幅<12%不过热）
        seg = hist[-4:-1]
        ok = True
        prev_c = hist[-4]["c"]
        for b in seg:
            if not (b["o"] > prev_c and b["c"] > b["o"]):
                ok = False
                break
            prev_c = b["c"]
        if not ok:
            continue
        seg_gain = seg[-1]["c"] / hist[-4]["c"] - 1
        if seg_gain > 0.12 or seg_gain <= 0:
            continue
        # 前15日内存在放量大阳
        found = False
        base = hist[-19:-4]
        for i, b in enumerate(base):
            if b["pct"] is None or b["pct"] <= 9.5:
                continue
            pre5 = [x["v"] or 0 for x in base[max(0, i - 5):i]]
            m5 = sum(pre5) / len(pre5) if pre5 else 0
            if m5 and (b["v"] or 0) >= m5 * 2:
                found = True
                break
        if not found:
            continue
        vr = _vol_ratio(hist)
        if cur["c"] < seg[-1]["c"] * 0.97:
            continue  # 今日明显走弱则不算蓄势
        sc = 3.2 + min(1.2, seg_gain * 8)
        if cur["pct"] > 0:
            sc += 0.6
        tag = ("大阳后3日高开小阳蓄势 累计+%.0f%% 量比%.1f" % (seg_gain * 100, vr))
        out.append(_mk(code, st, "停机坪", round(sc, 2), tag, hist, code2boards, vr))
    return out


def det_pull_long_ma(u, date, code2boards):
    """回踩长期均线：两段式上涨后缩量回踩 MA250（窗口不足时降级 MA120）。"""
    out = []
    need = 255
    have = max((len(u.bars_upto(c, date, 300)) for c in list(u.bars)[:50]), default=0)
    window = 250 if have >= need else 120
    ma_n = window
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, ma_n + 40)
        r = _pass_basic(u, code, hist, min_len=ma_n + 5)
        if not r:
            continue
        cur, st = r
        closes = [b["c"] for b in hist]
        man = _sma(closes, ma_n)
        man_prev = _sma(closes[:-20], ma_n)
        if None in (man, man_prev) or man <= 0:
            continue
        # 长期均线本身在抬升（牛市环境）
        if man < man_prev * 1.005:
            continue
        # 前期大涨段：60~20日前涨幅≥25%
        leg = closes[-61:-20]
        if len(leg) < 35 or leg[-1] / leg[0] - 1 < 0.25:
            continue
        # 当前价贴近长期均线（±5%）且回踩缩量
        ratio = cur["c"] / man
        if not (0.95 <= ratio <= 1.06):
            continue
        vol_now = _vma(hist[-6:-1], 5)
        vol_leg = _vma(hist[-45:-21], 20) or vol_now
        if vol_leg and vol_now > vol_leg * 0.85:
            continue  # 回踩未缩量
        vr = _vol_ratio(hist)
        sc = 3.4 + min(1.2, (leg[-1] / leg[0] - 1 - 0.25) * 2)
        if ratio <= 1.02:
            sc += 0.6  # 贴线越紧越佳
        ma_name = "年线" if ma_n == 250 else "半年线"
        tag = ("%.2f元 回踩%s(%d日) 缩量企稳 前段+%.0f%%"
               % (cur["c"], ma_name, ma_n, (leg[-1] / leg[0] - 1) * 100))
        out.append(_mk(code, st, "回踩长线", round(sc, 2), tag, hist, code2boards, vr))
    return out


def det_turtle60(u, date, code2boards):
    """海龟·唐奇安通道突破：收盘 ≥ 前60日最高收盘。"""
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 80)
        r = _pass_basic(u, code, hist, min_len=65)
        if not r:
            continue
        cur, st = r
        hi60 = max(b["c"] for b in hist[-61:-1])
        if cur["c"] < hi60:
            continue
        vr = _vol_ratio(hist)
        sc = 3.0
        if cur["c"] > hi60:
            sc += 0.8  # 有效突破（非持平）
        if vr >= 1.5:
            sc += 0.8
        if engine.is_limit_up(cur, u.lim.get(code)):
            sc += 0.6
        tag = ("%.2f元 突破60日箱顶%s 量比%.1f"
               % (cur["c"], "(收盘新高)" if cur["c"] > hi60 else "(持平)", vr))
        out.append(_mk(code, st, "海龟突破", round(sc, 2), tag, hist, code2boards, vr))
    return out


def det_narrow_flag(u, date, code2boards):
    """高而窄的旗形：25~10日前连续两日涨停级大涨，此后窄幅整理、现价较起点近翻倍。"""
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 40)
        r = _pass_basic(u, code, hist, min_len=30)
        if not r:
            continue
        cur, st = r
        win = hist[:-10]          # 排除最近10日，找旗杆
        if len(win) < 26:
            continue
        pole_i = None
        for i in range(len(win) - 25, len(win) - 1):
            a, b = win[i], win[i + 1]
            if (a["pct"] or 0) >= 9.5 and (b["pct"] or 0) >= 9.5:
                pole_i = i
                break
        if pole_i is None:
            continue
        seg = hist[pole_i:]
        lo = min(b["l"] for b in seg)
        if not lo:
            continue
        mult = cur["c"] / lo
        if mult < 1.85:
            continue
        # 旗形部分（旗杆后）应相对窄幅：区间振幅 < 25%
        post_hi = max(b["h"] for b in seg[2:])
        post_lo = min(b["l"] for b in seg[2:])
        mid = (post_hi + post_lo) / 2
        flag_w = (post_hi - post_lo) / mid if mid else 9
        if flag_w > 0.28:
            continue
        vr = _vol_ratio(hist)
        sc = 3.6 + min(1.2, (mult - 1.85) * 1.5) + max(0.0, (0.28 - flag_w) * 4)
        tag = ("%.2f元 双大阳旗形 x%.2f 旗宽%.0f%% 量比%.1f"
               % (cur["c"], mult, flag_w * 100, vr))
        out.append(_mk(code, st, "高窄旗形", round(sc, 2), tag, hist, code2boards, vr))
    return out


def det_steady_up(u, date, code2boards):
    """稳健上行·无大幅回撤：60日涨≥15%，期间无单日≤-7%、无两日累计≤-10%。"""
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 90)
        r = _pass_basic(u, code, hist, min_len=65)
        if not r:
            continue
        cur, st = r
        seg = hist[-61:-1]
        gain = cur["c"] / seg[0]["c"] - 1
        if gain < 0.15:
            continue
        bad = False
        for i in range(len(seg)):
            p0 = seg[i]["pct"] or 0
            if p0 <= -7:
                bad = True
                break
            if i >= 2:
                # 两日累计：直接用收盘比
                c2 = seg[i]["c"] / seg[i - 2]["c"] - 1
                if c2 <= -0.10:
                    bad = True
                    break
        if bad:
            continue
        closes = [b["c"] for b in hist]
        ma20 = _sma(closes, 20)
        if ma20 and cur["c"] < ma20:
            continue
        vr = _vol_ratio(hist)
        sc = 2.8 + min(1.4, (gain - 0.15) * 3)
        if vr >= 1.2 and cur["pct"] > 0:
            sc += 0.5
        tag = ("%.2f元 60日+%.0f%% 全程无深回撤 量比%.1f" % (cur["c"], gain * 100, vr))
        out.append(_mk(code, st, "稳健上行", round(sc, 2), tag, hist, code2boards, vr))
    return out


def det_low_atr(u, date, code2boards):
    """低ATR慢牛：波动率极低(ATR14/价≤2.2%) + 近40日缓步爬升(5%~30%)。"""
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 70)
        r = _pass_basic(u, code, hist, min_len=45)
        if not r:
            continue
        cur, st = r
        atr = _atr_pct(hist, 14)
        if atr is None or atr > 0.022:
            continue
        seg = hist[-41:-1]
        if len(seg) < 35:
            continue
        g40 = cur["c"] / seg[0]["c"] - 1
        if not (0.05 <= g40 <= 0.30):
            continue
        # 区间最大回撤要小（<8%）
        peak, mdd = seg[0]["c"], 0.0
        for b in seg:
            peak = max(peak, b["c"])
            mdd = min(mdd, b["c"] / peak - 1)
        if mdd < -0.08:
            continue
        vr = _vol_ratio(hist)
        sc = 2.6 + min(1.2, (0.022 - atr) * 200) + min(0.8, g40 * 3)
        if cur["pct"] > 0:
            sc += 0.4
        tag = ("%.2f元 低波动(ATR%.1f%%) 40日+%0.0f%% 回撤%.0f%%"
               % (cur["c"], atr * 100, g40 * 100, mdd * 100))
        out.append(_mk(code, st, "低ATR慢牛", round(sc, 2), tag, hist, code2boards, vr))
    return out


def det_vol_limit_down(u, date, code2boards):
    """放量跌停·博反观察：当日跌停且显著放量（次日反包机会参照，低分观察位）。"""
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 15)
        r = _pass_basic(u, code, hist, min_len=10)
        if not r:
            continue
        cur, st = r
        lim = u.lim.get(code)
        if lim is None or not engine.is_limit_down(cur, lim):
            continue
        vr = _vol_ratio(hist)
        if vr < 1.8:
            continue
        sc = 1.5 + min(0.8, (vr - 1.8) * 0.5)  # 观察信号，分数压低
        tag = ("%.2f元 放量跌停 量比%.1f（次日反包观察，勿急接）" % (cur["c"], vr))
        out.append(_mk(code, st, "放量跌停", round(sc, 2), tag, hist, code2boards, vr))
    return out


STRATEGIES = [det_vol_up, det_ma_bull, det_helipad, det_pull_long_ma, det_turtle60,
              det_narrow_flag, det_steady_up, det_low_atr, det_vol_limit_down]

SIGNAL_NAMES = {
    "det_vol_up": "放量上涨", "det_ma_bull": "均线多头", "det_helipad": "停机坪",
    "det_pull_long_ma": "回踩长线", "det_turtle60": "海龟突破", "det_narrow_flag": "高窄旗形",
    "det_steady_up": "稳健上行", "det_low_atr": "低ATR慢牛", "det_vol_limit_down": "放量跌停",
}


# ----------------------------------------------------------------- 汇总
def scan(u, date, con, code2boards=None, topn=12):
    """跑全部策略探测器，按 code 聚合多信号共振，按综合得分排序。"""
    code2boards = code2boards or store.code_boards(con)
    hits_by_code = {}
    for det in STRATEGIES:
        try:
            hits = det(u, date, code2boards)
        except Exception:
            hits = []
        for h in hits:
            hits_by_code.setdefault(h["code"], []).append(h)

    merged = []
    for code, hits in hits_by_code.items():
        score = round(sum(h["score"] for h in hits), 2)
        signals = [h["signal"] for h in hits]
        base = hits[0]
        merged.append({
            "code": code,
            "name": base["name"],
            "signals": signals,
            "multi": len(hits),
            "score": score,
            "price": base["price"],
            "pct": base["pct"],
            "vol_ratio": base["vol_ratio"],
            "ind": base["ind"],
            "dd60": base["dd60"],
            "tags": "；".join(h["tag"] for h in hits[:3]),
        })
    merged.sort(key=lambda x: (-x["score"], -x["multi"]))
    return merged[:topn]


if __name__ == "__main__":
    import json
    cc = store.connect()
    u = engine.Universe(cc, days=130)
    d = u.dates[-1] if u.dates else None   # 库内最后一个交易日（周末运行=周五）
    rep = scan(u, d, cc)
    print("经典策略 Top%d (%s)" % (len(rep), d))
    for it in rep:
        print(json.dumps(it, ensure_ascii=False))
