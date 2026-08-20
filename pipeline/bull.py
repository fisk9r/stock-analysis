# -*- coding: utf-8 -*-
"""牛股雷达 bull.py —— 多维度独立抓牛股信号

设计目标（用户诉求）：把"还能怎么挖到牛股"的方法全部加进来，每个维度相互独立，
互补而非重复。每个探测器返回命中候选（code/name/signal/score/tag/关键数值），
scan() 汇总去重、按综合得分排序，输出「牛股雷达」榜。

维度一览（均为由本地日K库自重建，无外部依赖）：
  1. new_high        阶段新高突破（近 60/120 日新高 + 放量确认 + 仍有空间）
  2. platform        平台突破（长箱体窄幅整理后放量突破）
  3. second_wave     二波启动（一波拉升后回踩，再次放量突破前高）
  4. reversal        反包（昨日下跌/跌停，今日强势反包/涨停反包）
  5. ma_diverge      均线粘合后发散（多头发散 = 蛟龙出海）
  6. deep_limit      深水拉板 / 地天（低开后封死涨停，强反转）
  7. low_first_board 低位首板 + 题材发酵（长期低位后首板，身处当日热点行业）
  8. n_pullback      N字缩量回调企稳（升-缩量回-再放量起）
  9. gap_hold        缺口不补（向上跳空且当日不回补）
 10. trend_accel     趋势加速（均线多头 + 斜率与量能同步放大）

推送只给"结果"（信号名 + 关键数值），判断逻辑只在引擎内部，不外发。
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


def _dd60(hist):
    """当前价距窗口内最高价的回撤（负=低于高点）"""
    if len(hist) < 61:
        return 0.0
    hi = max(b["h"] for b in hist[-61:-1]) or hist[-1]["c"]
    return hist[-1]["c"] / hi - 1.0


def _hot_industries(u, date, code2boards):
    """当日涨停股所属行业计数，作为『题材热点』参照"""
    cnt = {}
    for code in u.zt.get(date, set()):
        for bk, name, kind in (code2boards.get(code) or []):
            if kind == "industry" and name:
                cnt[name] = cnt.get(name, 0) + 1
    return cnt


def _industry(code, code2boards):
    for bk, name, kind in (code2boards.get(code) or []):
        if kind == "industry" and name:
            return name
    return ""


def _pass_basic(u, code, hist):
    """通用过滤：ST / 退市 / 无价 / 历史过短"""
    if len(hist) < 62:
        return None
    st = u.stocks.get(code, {})
    name = st.get("name") or ""
    if "ST" in name.upper() or "退" in name:
        return None
    cur = hist[-1]
    if not cur["c"] or cur["c"] <= 0:
        return None
    return cur, st


# ----------------------------------------------------------------- 探测器
def det_new_high(u, date, code2boards):
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 121)
        r = _pass_basic(u, code, hist)
        if not r:
            continue
        cur, st = r
        lim = u.lim.get(code)
        if lim is None:
            continue
        h60 = max(b["h"] for b in hist[-61:-1]) or cur["c"]
        h120 = max(b["h"] for b in hist[-121:-1]) or cur["c"]
        ma20 = _sma([b["c"] for b in hist], 20)
        vr = _vol_ratio(hist)
        # 触及/突破 60日新高，且仍在 120日新高 3% 以内（仍有空间未彻底透支）
        near60 = cur["h"] >= h60 * 0.995
        room = cur["c"] <= h120 * 1.03
        if not (near60 and room):
            continue
        if (ma20 is None) or cur["c"] < ma20:
            continue
        if vr < 1.4:
            continue
        sealed = engine.is_limit_up(cur, lim)
        sc = 2.0
        if cur["c"] >= h60:
            sc += 1.5
        if cur["c"] >= h120:
            sc += 1.0
        sc += min(1.5, (vr - 1.4) * 1.2)
        if not sealed:
            sc += 0.8  # 未封板=还有空间
        tag = ("%.2f元 创60日新高 量比%.1f%s" % (cur["c"], vr, " 已封板" if sealed else " 未封"))
        out.append(_mk(code, st, "阶段新高突破", round(sc, 2), cur, vr, tag, hist, code2boards))
    return out


def det_platform(u, date, code2boards):
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 60)
        r = _pass_basic(u, code, hist)
        if not r:
            continue
        cur, st = r
        lim = u.lim.get(code)
        if lim is None:
            continue
        base = hist[-21:-1]  # 前 20 日作为平台
        if len(base) < 18:
            continue
        hi = max(b["h"] for b in base)
        lo = min(b["l"] for b in base)
        mc = sum(b["c"] for b in base) / len(base)
        tight = (hi - lo) / mc if mc else 9
        if tight > 0.15:
            continue  # 不是窄幅平台
        vr = _vol_ratio(hist)
        if cur["c"] <= hi * 1.0 or vr < 1.5:
            continue  # 未突破或无量
        if engine.is_limit_up(cur, lim):
            sc = 4.2
        else:
            sc = 3.2
        sc += min(1.2, (vr - 1.5) * 0.9)
        sc += max(0.0, (0.15 - tight) * 6)  # 越窄越纯
        tag = ("%.2f元 箱体%.0f%% 突破量比%.1f" % (cur["c"], tight * 100, vr))
        out.append(_mk(code, st, "平台突破", round(sc, 2), cur, vr, tag, hist, code2boards))
    return out


def det_second_wave(u, date, code2boards):
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 90)
        r = _pass_basic(u, code, hist)
        if not r:
            continue
        cur, st = r
        lim = u.lim.get(code)
        if lim is None:
            continue
        closes = [b["c"] for b in hist]
        # 第一波：40~70日前出现明显高点（较 70日前 +25% 以上）
        pre = closes[-70:-40]
        if len(pre) < 25:
            continue
        base0 = pre[0]
        if not base0:
            continue
        peak_idx = max(range(len(closes) - 60, len(closes) - 20),
                       key=lambda i: closes[i])
        peak = closes[peak_idx]
        first_wave = peak / base0 - 1
        if first_wave < 0.25:
            continue
        # 回踩：近 20 日最低较峰值回撤 >=15%
        recent = closes[-20:]
        drawdown = min(recent) / peak - 1
        if drawdown > -0.15:
            continue
        # 再起：今日突破近 20 日最高 + 放量
        rhi = max(b["h"] for b in hist[-21:-1])
        vr = _vol_ratio(hist)
        ma20 = _sma(closes, 20)
        if cur["c"] < rhi * 1.0 or vr < 1.3 or (ma20 and cur["c"] < ma20):
            continue
        sc = 3.0 + min(1.5, (first_wave - 0.25) * 2)
        sc += min(1.0, (-drawdown - 0.15) * 3)
        sc += min(1.0, (vr - 1.3))
        tag = ("一波+%.0f%% 回踩%.0f%% 再起量比%.1f" % (first_wave * 100, drawdown * 100, vr))
        out.append(_mk(code, st, "二波启动", round(sc, 2), cur, vr, tag, hist, code2boards))
    return out


def det_reversal(u, date, code2boards):
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
        y = hist[-2] if len(hist) >= 2 else None
        if not y or not y["c"]:
            continue
        y_down = y["pct"] < -2 or engine.is_limit_down(y, lim)
        if not y_down:
            continue
        engulf = cur["c"] >= y["h"]  # 反包昨日最高
        up = cur["pct"] > 3
        sealed = engine.is_limit_up(cur, lim)
        if not (up and (engulf or sealed)):
            continue
        sc = 3.0
        if sealed:
            sc += 1.8
        if engulf:
            sc += 1.0
        if engine.is_limit_down(y, lim):
            sc += 0.6  # 跌停反包更强
        vr = _vol_ratio(hist)
        sc += min(1.0, (vr - 1.2))
        tag = ("昨%s 今反包%s 量比%.1f" % ("跌停" if engine.is_limit_down(y, lim) else "跌%.1f%%" % y["pct"],
                                         "涨停" if sealed else "涨%.1f%%" % cur["pct"], vr))
        out.append(_mk(code, st, "反包", round(sc, 2), cur, vr, tag, hist, code2boards))
    return out


def det_ma_diverge(u, date, code2boards):
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 65)
        r = _pass_basic(u, code, hist)
        if not r:
            continue
        cur, st = r
        closes = [b["c"] for b in hist]
        ma5, ma10, ma20, ma60 = (_sma(closes, 5), _sma(closes, 10),
                                 _sma(closes, 20), _sma(closes, 60))
        ma5b, ma10b, ma20b = (_sma(closes[:-5], 5), _sma(closes[:-5], 10),
                               _sma(closes[:-5], 20))
        if None in (ma5, ma10, ma20, ma60, ma5b, ma10b, ma20b):
            continue
        # 5日前均线粘合（最大/最小 <=6%）且今日发散（多头排列 + 短均抬升）
        conv = max(ma5b, ma10b, ma20b) / min(ma5b, ma10b, ma20b) - 1
        if conv > 0.06:
            continue
        if not (ma5 > ma10 > ma20 and cur["c"] > ma5 > ma10 > ma20):
            continue
        if ma5 <= ma5b:
            continue  # 短均未抬升=未发散
        vr = _vol_ratio(hist)
        sc = 3.2 + min(1.5, (0.06 - conv) * 18) + min(1.0, (vr - 1.0))
        tag = ("均线粘合后多头发散 量比%.1f" % vr)
        out.append(_mk(code, st, "均线发散", round(sc, 2), cur, vr, tag, hist, code2boards))
    return out


def det_deep_limit(u, date, code2boards):
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 10)
        r = _pass_basic(u, code, hist)
        if not r:
            continue
        cur, st = r
        lim = u.lim.get(code)
        if lim is None:
            continue
        y = hist[-2] if len(hist) >= 2 else None
        if not y or not y["c"]:
            continue
        open_down = cur["o"] <= y["c"] * (1 - 0.02)
        sealed = engine.is_limit_up(cur, lim)
        if not (open_down and sealed):
            continue
        sc = 4.0
        dive = (cur["o"] / y["c"] - 1) * 100
        if dive <= -5:
            sc += 1.0  # 越深越经典（地天）
        sc += min(1.0, (cur["pct"] - 9) * 0.3)
        tag = ("低开%.1f%%→涨停（深水拉板）" % dive)
        out.append(_mk(code, st, "深水拉板", round(sc, 2), None, None, tag, hist, code2boards))
    return out


def det_low_first_board(u, date, code2boards):
    out = []
    hot = _hot_industries(u, date, code2boards)
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 65)
        r = _pass_basic(u, code, hist)
        if not r:
            continue
        cur, st = r
        lim = u.lim.get(code)
        if lim is None:
            continue
        if not engine.is_limit_up(cur, lim):
            continue
        streak = u.streak.get(code, {}).get(date, 0)
        if streak >= 2:
            continue  # 只取首板（非连板）
        dd = _dd60(hist)
        if dd > -0.08:
            continue  # 不在低位
        vr = _vol_ratio(hist)
        ind = _industry(code, code2boards)
        sc = 3.0
        if dd <= -0.20:
            sc += 1.6
        elif dd <= -0.12:
            sc += 1.0
        if cur["c"] < 10:
            sc += 1.2
        elif cur["c"] < 20:
            sc += 0.6
        if ind and hot.get(ind, 0) >= 2:
            sc += 2.0  # 身处当日热点行业
        sc += min(0.8, (vr - 1.5))
        tag = ("%.2f元 低位首板 距高%.0f%% %s%s" % (cur["c"], dd * 100, ind,
                                                 "【热点】" if (ind and hot.get(ind, 0) >= 2) else ""))
        out.append(_mk(code, st, "低位首板", round(sc, 2), cur, vr, tag, hist, code2boards))
    return out


def det_n_pullback(u, date, code2boards):
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 30)
        r = _pass_basic(u, code, hist)
        if not r:
            continue
        cur, st = r
        closes = [b["c"] for b in hist]
        # 升：15日前 > 8日前
        if closes[-15] <= closes[-22] or closes[-8] <= closes[-15]:
            pass
        # 升段（15~8日前上涨）
        up_leg = closes[-8] / closes[-15] - 1
        if up_leg < 0.08:
            continue
        # 回踩：近 6 日最低低于升段起点，且缩量
        pb_lo = min(closes[-7:-1])
        if pb_lo >= closes[-15]:
            continue  # 没回踩
        vol_up = _vma(hist[-15:-8], 7)
        vol_pb = _vma(hist[-7:-1], 6)
        if vol_pb > vol_up * 0.95:
            continue  # 回踩不缩量
        # 再起：今日涨且突破回踩区间
        vr = _vol_ratio(hist)
        if cur["pct"] < 2 or cur["c"] < max(closes[-7:-1]) or vr < 1.3:
            continue
        dd = _dd60(hist)
        sc = 3.0 + min(1.2, up_leg * 4) + min(1.0, (vr - 1.3))
        if dd <= -0.10:
            sc += 0.6
        tag = ("升%.0f%% 缩量回踩 再起量比%.1f" % (up_leg * 100, vr))
        out.append(_mk(code, st, "N字回调", round(sc, 2), cur, vr, tag, hist, code2boards))
    return out


def det_gap_hold(u, date, code2boards):
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 30)
        r = _pass_basic(u, code, hist)
        if not r:
            continue
        cur, st = r
        if len(hist) < 3:
            continue
        y = hist[-2]
        if not y["c"] or not y["h"]:
            continue
        if cur["o"] <= y["h"]:
            continue  # 非向上跳空
        gap = cur["o"] / y["h"] - 1
        if gap < 0.01:
            continue
        filled = cur["l"] < y["h"]  # 当日最低回补了缺口
        if filled:
            continue
        vr = _vol_ratio(hist)
        ma10 = _sma([b["c"] for b in hist], 10)
        if (ma10 and cur["c"] < ma10) or vr < 1.2:
            continue
        sc = 3.0 + min(1.5, gap * 60) + min(1.0, (vr - 1.2))
        tag = ("跳空+%.1f%% 缺口未补 量比%.1f" % (gap * 100, vr))
        out.append(_mk(code, st, "缺口不补", round(sc, 2), cur, vr, tag, hist, code2boards))
    return out


def det_trend_accel(u, date, code2boards):
    out = []
    for code, _ in u.bars.items():
        hist = _hist(u, code, date, 40)
        r = _pass_basic(u, code, hist)
        if not r:
            continue
        cur, st = r
        closes = [b["c"] for b in hist]
        ma5, ma10, ma20 = (_sma(closes, 5), _sma(closes, 10), _sma(closes, 20))
        ma5b = _sma(closes[:-5], 5)
        if None in (ma5, ma10, ma20, ma5b) or ma5 <= ma5b:
            continue
        if not (ma5 > ma10 > ma20 and cur["c"] > ma5):
            continue
        slope = (ma5 / ma5b - 1)
        if slope < 0.02:
            continue  # 斜率未放大
        vr = _vol_ratio(hist)
        vlong = _vma(hist, 20)
        if vr < 1.3 or (cur["v"] or 0) < vlong * 1.1:
            continue
        if cur["pct"] <= 0:
            continue
        sc = 3.0 + min(1.5, slope * 40) + min(1.0, (vr - 1.3))
        tag = ("多头加速 斜率+%.1f%% 量比%.1f" % (slope * 100, vr))
        out.append(_mk(code, st, "趋势加速", round(sc, 2), cur, vr, tag, hist, code2boards))
    return out


DETECTORS = [det_new_high, det_platform, det_second_wave, det_reversal, det_ma_diverge,
             det_deep_limit, det_low_first_board, det_n_pullback, det_gap_hold, det_trend_accel]


def _mk(code, st, signal, score, cur, vr, tag, hist, code2boards):
    price = cur["c"] if cur else None
    pct = cur["pct"] if cur else None
    return {
        "code": code, "name": st.get("name", ""),
        "signal": signal, "score": score,
        "price": round(price, 2) if price else None,
        "pct": round(pct, 2) if pct is not None else None,
        "vol_ratio": vr,
        "ind": _industry(code, code2boards),
        "dd60": round(_dd60(hist) * 100, 1) if len(hist) >= 61 else None,
        "tag": tag,
    }


# ----------------------------------------------------------------- 汇总
def scan(u, date, con, code2boards=None, topn=12):
    """跑全部探测器，按 code 聚合多信号，按综合得分排序输出牛股雷达榜。"""
    code2boards = code2boards or store.code_boards(con)
    hits_by_code = {}
    for det in DETECTORS:
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


def summary_lines(rep, topn=8):
    """推送用的『只给结果』紧凑摘要：代码 名称 信号 关键数值"""
    if not rep:
        return ["牛股雷达：今日无明确信号（市场偏冷或风格混沌，建议控仓）"]
    L = []
    for it in rep[:topn]:
        sig = "+".join(it["signals"])
        extra = []
        if it["price"] is not None:
            extra.append("%.2f元" % it["price"])
        if it["pct"] is not None:
            extra.append("%+.1f%%" % it["pct"])
        if it["vol_ratio"]:
            extra.append("量比%.1f" % it["vol_ratio"])
        if it["multi"] >= 2:
            extra.append("共振%d维" % it["multi"])
        L.append("%s %s 【%s】 %s" % (it["code"], it["name"], sig, " ".join(extra)))
    return L


if __name__ == "__main__":
    import json
    import trade_calendar as tc
    cc = store.connect()
    d = tc.last_trade_date(cc)
    u = engine.Universe(cc, days=130)
    rep = scan(u, d, cc)
    print("牛股雷达 Top%d (%s)" % (len(rep), d))
    for it in rep:
        print(json.dumps(it, ensure_ascii=False))
