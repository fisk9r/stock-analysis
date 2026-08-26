# -*- coding: utf-8 -*-
"""缠论（Chan's Theory）结构引擎。

算法严格遵循缠论标准定义（因技能页 skillhub.cn 为 SPA 无法抓取正文，按方法学实现）：
  1) K线包含关系处理 → 标准K线（上升取上沿、下降取下沿）
  2) 顶/底分型识别
  3) 笔（Bi）：相邻反向分型且必须创新高/新低，否则合并
  4) 中枢（Zhongshu）：≥3 笔区间重叠
  5) 背驰（Beichi）：价格创新高(低)而 MACD(DIF) 未同步 → 趋势衰竭
  6) 买卖点：一买(背驰底)/二买(不破前低)/三买(回踩不进中枢) 及对称卖点

输入：某股票的日K序列（date, open, high, low, close）。
输出：当前结构（方向/中枢/背驰）+ 买卖点信号，供推荐池加分与前端「缠论结构」卡。

纯本地计算（基于 market.db 的日K），无需外部接口；序列过短自动跳过。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store


def _ema(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for i in range(1, len(vals)):
        out.append(vals[i] * k + out[-1] * (1 - k))
    return out


def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow:
        return [0] * len(closes), [0] * len(closes), [0] * len(closes)
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    dif = [ef[i] - es[i] for i in range(len(closes))]
    dea = _ema(dif, signal)
    hist = [dif[i] - dea[i] for i in range(len(closes))]
    return dif, dea, hist


def process_inclusion(bars):
    """bars: [(idx, high, low), ...] → 标准K线 [(idx, high, low), ...]（无包含）。"""
    if len(bars) <= 2:
        return list(bars)
    proc = [list(bars[0])]
    for b in bars[1:]:
        h, l = b[1], b[2]
        ph, pl = proc[-1][1], proc[-1][2]
        inclusive = (h <= ph and l >= pl) or (h >= ph and l <= pl)
        if not inclusive:
            proc.append(list(b))
            continue
        # 合并：方向由 proc[-2] 与 proc[-1] 决定
        if len(proc) >= 2:
            pph, ppl = proc[-2][1], proc[-2][2]
            if pph <= ph and ppl <= pl:
                direction = 1      # 上升
            elif pph >= ph and ppl >= pl:
                direction = -1     # 下降
            else:
                direction = 0
        else:
            direction = 0
        if direction == 1:
            nh, nl = max(ph, h), max(pl, l)
        elif direction == -1:
            nh, nl = min(ph, h), min(pl, l)
        else:
            nh, nl = max(ph, h), min(pl, l)
        proc[-1] = [proc[-1][0], nh, nl]
    return [tuple(p) for p in proc]


def find_fractals(proc):
    """返回 [(idx, 'top'/'bottom', price), ...] 按出现顺序。"""
    out = []
    for i in range(1, len(proc) - 1):
        h, l = proc[i][1], proc[i][2]
        hp = proc[i - 1][1] < h and proc[i + 1][1] < h
        lp = proc[i - 1][2] > l and proc[i + 1][2] > l
        if hp:
            out.append((proc[i][0], "top", h))
        elif lp:
            out.append((proc[i][0], "bottom", l))
    return out


def build_bi(fractals):
    """分型 → 笔：反向且创新高/新低才成笔，否则合并。返回 [(idx,'top'/'bottom',high,low),...]"""
    if len(fractals) < 2:
        return []
    bi = [list(fractals[0]) + [fractals[0][2], fractals[0][2]]]  # idx,type,price,high,low
    for f in fractals[1:]:
        ftype, fprice = f[1], f[2]
        cur = bi[-1]
        if ftype == cur[1]:
            # 同型：取更极值
            if (ftype == "top" and fprice > cur[2]) or (ftype == "bottom" and fprice < cur[2]):
                bi[-1] = [f[0], ftype, fprice, max(cur[3], f[2]), min(cur[4], f[2])]
            continue
        if ftype == "top":
            prev = max([b[2] for b in bi if b[1] == "top"] or [fprice - 1])
            if fprice > prev:
                bi.append([f[0], ftype, fprice, fprice, f[2]])
        else:
            prev = min([b[2] for b in bi if b[1] == "bottom"] or [fprice + 1])
            if fprice < prev:
                bi.append([f[0], ftype, fprice, f[2], fprice])
    return [tuple(b) for b in bi]


def bi_segments(bi):
    """笔端点序列 → 每一笔所张成的价格区间 [(high, low), ...]。

    注意：bi 的元素是「端点」（idx,type,price,high,low），其 high==low==分型价，
    单个端点是退化的点，不构成区间。一笔 = 相邻两个端点之间的线段，
    其区间为 [min(两端价), max(两端价)]。中枢必须基于线段区间求重叠。
    """
    segs = []
    for i in range(len(bi) - 1):
        a, b = bi[i][2], bi[i + 1][2]
        segs.append((max(a, b), min(a, b)))
    return segs


def _overlap(segs):
    """若给定线段区间全部重叠，返回 (upper, lower)，否则 None。"""
    upper = min(s[0] for s in segs)
    lower = max(s[1] for s in segs)
    return (upper, lower) if lower < upper else None


def find_zhongshu(bi):
    """最近一个笔中枢：≥3 笔线段区间的公共重叠 → (upper, lower) 或 None。

    先看末尾 3 段是否重叠；若重叠则尽量向前扩展（最多 7 段）以得到完整中枢；
    若末尾 3 段不重叠（说明刚离开中枢），则向前滑动窗口最多 4 次寻找最近中枢。
    """
    segs = bi_segments(bi)
    if len(segs) < 3:
        return None
    for back in range(0, 5):                     # 窗口末端向前滑动
        end = len(segs) - back
        if end < 3:
            break
        base = _overlap(segs[end - 3:end])
        if not base:
            continue
        best = base
        for size in range(4, 8):                 # 能扩展就扩展
            if end - size < 0:
                break
            ext = _overlap(segs[end - size:end])
            if not ext:
                break
            best = ext
        return (round(best[0], 2), round(best[1], 2))
    return None


def detect_beichi(bi, dif):
    """比较末尾两个同向笔的端点：价格创新极而 DIF 未同步 → 背驰。返回 'down'/'up'/None。"""
    tops = [b for b in bi if b[1] == "top"]
    bots = [b for b in bi if b[1] == "bottom"]
    # 底背驰：最后两个底，价格更低但 DIF 更高
    if len(bots) >= 2:
        a, b = bots[-2], bots[-1]
        if b[2] < a[2] and dif[b[0]] > dif[a[0]]:
            return "down"
    if len(tops) >= 2:
        a, b = tops[-2], tops[-1]
        if b[2] > a[2] and dif[b[0]] < dif[a[0]]:
            return "up"
    return None


def classify(bi, zhongshu, beichi):
    """返回 (signal, reason)。signal ∈ 一买/二买/三买/一卖/二卖/三卖/无。"""
    if len(bi) < 3:
        return "无", "笔数不足"
    last = bi[-1]
    last2 = bi[-2]
    # 收集历史极值
    bot_prices = [b[2] for b in bi if b[1] == "bottom"]
    top_prices = [b[2] for b in bi if b[1] == "top"]
    min_bot = min(bot_prices)
    max_top = max(top_prices)

    if last[1] == "bottom":
        # 下跌笔结束于底部
        if last[2] <= min_bot and beichi == "down":
            return "一买", "下跌末端底背驰，趋势衰竭"
        if last[2] > min_bot and beichi == "down":
            return "二买", "回踩不破前低且底背驰"
        if zhongshu and last[4] > zhongshu[0]:
            return "三买", "回踩不进中枢（站上中枢上沿）"
        if last[2] > min_bot:
            return "二买", "回踩未破前低"
    else:
        # 上升笔结束于顶部
        if last[2] >= max_top and beichi == "up":
            return "一卖", "上升末端顶背驰"
        if last[2] < max_top and beichi == "up":
            return "二卖", "反抽不过前高且顶背驰"
        if zhongshu and last[3] < zhongshu[1]:
            return "三卖", "反抽不进中枢（跌破中枢下沿）"
    return "无", ""


def analyze(code, bars):
    """bars: [{d,o,h,l,c,...}] 按日期升序。返回结构字典或 None（数据不足）。"""
    if not bars or len(bars) < 30:
        return None
    closes = [float(b["c"]) for b in bars]
    raw = [(i, float(b["h"]), float(b["l"])) for i, b in enumerate(bars)]
    dif, dea, hist = macd(closes)
    proc = process_inclusion(raw)
    if len(proc) < 5:
        return None
    fractals = find_fractals(proc)
    if len(fractals) < 4:
        return None
    bi = build_bi(fractals)
    if len(bi) < 3:
        return None
    zhongshu = find_zhongshu(bi)
    beichi = detect_beichi([(b[0], b[1], b[2]) for b in bi], dif)
    signal, reason = classify(bi, zhongshu, beichi)
    last_dir = bi[-1][1]
    return {
        "code": code,
        "n_bi": len(bi),
        "last_dir": "上升笔" if last_dir == "top" else "下降笔",
        "zhongshu": list(zhongshu) if zhongshu else None,
        "beichi": beichi,
        "signal": signal,
        "reason": reason,
        "last_close": closes[-1],
    }


def scan(u, con, codes, top_n=12):
    """对给定股票列表跑缠论，返回带买点信号的候选（一/二/三买优先）。"""
    bars_map = store.load_bars(con, codes=codes)
    if not bars_map:
        return None
    # 名称映射（优先用 universe，回退 store.stocks 表）
    names = {}
    try:
        for code, name, _m, _t, _f in con.execute(
                "SELECT code,name,market,total_mv,float_mv FROM stocks"):
            names[code] = name
    except Exception:
        pass
    def get_name(code):
        if hasattr(u, "name"):
            try:
                nm = u.name(code)
                if nm:
                    return nm
            except Exception:
                pass
        return names.get(code, code)
    rank = {"一买": 3, "二买": 2, "三买": 2, "一卖": 0, "二卖": 0, "三卖": 0, "无": 1}
    cands = []
    for code in codes:
        bars = bars_map.get(code)
        if not bars:
            continue
        r = analyze(code, bars)
        if not r:
            continue
        r["name"] = get_name(code)
        cands.append(r)
    cands.sort(key=lambda x: (-rank.get(x["signal"], 1), -(x["beichi"] is not None)))
    buys = [c for c in cands if x_has_buy(c)]
    return {
        "date": None,
        "n_analyzed": len(cands),
        "candidates": cands[:top_n],
        "buys": buys[:8],
    }


def x_has_buy(c):
    return c["signal"] in ("一买", "二买", "三买")


def summary_lines(r):
    if not r or not r.get("candidates"):
        return []
    out = ["缠论结构：分析 %d 只，买点候选 %d 只"
           % (r.get("n_analyzed", 0), len(r.get("buys") or []))]
    for c in (r.get("buys") or [])[:6]:
        extra = ""
        if c.get("zhongshu"):
            extra += " · 中枢[%s,%s]" % (c["zhongshu"][0], c["zhongshu"][1])
        if c.get("beichi"):
            extra += " · 底背驰" if c["beichi"] == "down" else " · 顶背驰"
        out.append("- %s（%s）%s：%s%s"
                    % (c.get("name"), c.get("code"), c.get("signal"), c.get("reason", ""), extra))
    return out
