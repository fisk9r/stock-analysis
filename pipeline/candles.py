# -*- coding: utf-8 -*-
"""K线组合形态识别 candles.py —— 经典蜡烛图形态（InStock 61 形态中的高频核心集）

纯标准库，由本地日K重建。每日全市场扫描一次，输出：
  · 今日命中个股（形态 / 多空方向 / 关键数值）
  · 各形态今日命中数统计

已实现 12 种（多空标注）：
  看多：锤头线、看涨吞没、早晨之星、红三兵、刺透线、低位十字星
  看空：上吊线、看跌吞没、黄昏之星、三只黑乌鸦、乌云盖顶
  中性：长腿十字星

形态判定基于经典定义的工程化近似（实体=|收-开|，影线上下沿），趋势背景用
近5日涨跌区分「低位/高位」出现场景。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store


def _body(b):
    return abs(b["c"] - b["o"])


def _range(b):
    return (b["h"] - b["l"]) or 1e-9


def _upper(b):
    return b["h"] - max(b["c"], b["o"])


def _lower(b):
    return min(b["c"], b["o"]) - b["l"]


def _is_green(b):   # A股红涨绿跌：收>开为阳线
    return b["c"] > b["o"]


def _trend(closes, n=5):
    """近 n 日涨跌幅（不含当日），<-3% 视为跌势，>3% 涨势。"""
    if len(closes) < n + 1:
        return 0.0
    return closes[-1] / closes[-n - 1] - 1.0


# ---------------- 单形态判定（hist 含当日，至少 4 根） ----------------

def p_hammer(hist):
    """锤头线：跌势末期的长下影小实体（下影≥2×实体，上影≈无）。"""
    c = hist[-1]
    t = _trend([b["c"] for b in hist[:-1]])
    return (t <= -0.03 and _lower(c) >= 2 * _body(c)
            and _upper(c) <= _body(c) * 0.6 and _range(c) > 0.01)


def p_hanging_man(hist):
    """上吊线：涨势之后的长下影小实体（预警）。"""
    c = hist[-1]
    t = _trend([b["c"] for b in hist[:-1]])
    return (t >= 0.03 and _lower(c) >= 2 * _body(c)
            and _upper(c) <= _body(c) * 0.6 and _range(c) > 0.01)


def p_bull_engulf(hist):
    """看涨吞没：阳线实体完全包住昨日阴线实体。"""
    if len(hist) < 2:
        return False
    y, c = hist[-2], hist[-1]
    return (_is_green(y) is False and _is_green(c)
            and c["o"] <= y["c"] and c["c"] >= y["o"]
            and _body(c) > _body(y))


def p_bear_engulf(hist):
    """看跌吞没：阴线实体完全包住昨日阳线实体。"""
    if len(hist) < 2:
        return False
    y, c = hist[-2], hist[-1]
    return (_is_green(y) and not _is_green(c)
            and c["o"] >= y["c"] and c["c"] <= y["o"]
            and _body(c) > _body(y))


def p_doji_low(hist):
    """低位十字星：跌势中出现开收几乎相等的十字（变盘信号）。"""
    c = hist[-1]
    t = _trend([b["c"] for b in hist[:-1]])
    return (t <= -0.03 and _body(c) <= _range(c) * 0.08
            and _lower(c) > _body(c) * 2)


def p_morning_star(hist):
    """早晨之星：大阴 → 跳低小实体 → 阳线收复前阴实体一半以上。"""
    if len(hist) < 3:
        return False
    a, m, c = hist[-3], hist[-2], hist[-1]
    return (not _is_green(a) and _body(a) > _range(a) * 0.5
            and _body(m) < _body(a) * 0.4
            and _is_green(c) and c["c"] > (a["o"] + a["c"]) / 2)


def p_evening_star(hist):
    """黄昏之星：大阳 → 高位小实体 → 阴线跌破前阳实体一半。"""
    if len(hist) < 3:
        return False
    a, m, c = hist[-3], hist[-2], hist[-1]
    return (_is_green(a) and _body(a) > _range(a) * 0.5
            and _body(m) < _body(a) * 0.4
            and not _is_green(c) and c["c"] < (a["o"] + a["c"]) / 2)


def p_three_soldiers(hist):
    """红三兵：连续三阳，逐级走高，涨幅温和（非涨停连板）。"""
    if len(hist) < 3:
        return False
    a, b, c = hist[-3], hist[-2], hist[-1]
    if not all(_is_green(x) for x in (a, b, c)):
        return False
    if not (b["c"] > a["c"] and c["c"] > b["c"]):
        return False
    pcts = [x.get("pct") for x in (a, b, c)]
    return all(p is not None and 0 < p < 8 for p in pcts)


def p_three_crows(hist):
    """三只黑乌鸦：连续三阴，逐级走低。"""
    if len(hist) < 3:
        return False
    a, b, c = hist[-3], hist[-2], hist[-1]
    return (all(not _is_green(x) for x in (a, b, c))
            and b["c"] < a["c"] and c["c"] < b["c"]
            and all(-8 < (x["pct"] or 0) < 0 for x in (a, b, c)))


def p_piercing(hist):
    """刺透线：跌势中大阴后，阳线低开但收过前阴实体中点。"""
    if len(hist) < 2:
        return False
    y, c = hist[-2], hist[-1]
    t = _trend([b["c"] for b in hist[:-2] + [y]])
    return (not _is_green(y) and _body(y) > _range(y) * 0.4
            and _is_green(c) and c["o"] < y["c"]
            and c["c"] > (y["o"] + y["c"]) / 2 and c["c"] < y["o"])


def p_dark_cloud(hist):
    """乌云盖顶：涨势中大阳后，阴线高开且收进前阳实体下半。"""
    if len(hist) < 2:
        return False
    y, c = hist[-2], hist[-1]
    return (_is_green(y) and _body(y) > _range(y) * 0.4
            and not _is_green(c) and c["o"] > y["c"]
            and y["o"] < c["c"] < (y["o"] + y["c"]) / 2)


def p_long_legs(hist):
    """长腿十字：开收接近且上下影都很长的变盘警示。"""
    c = hist[-1]
    return (_body(c) <= _range(c) * 0.06
            and _upper(c) >= _range(c) * 0.33 and _lower(c) >= _range(c) * 0.33)


PATTERNS = [
    ("锤头线", "bull", p_hammer),
    ("看涨吞没", "bull", p_bull_engulf),
    ("早晨之星", "bull", p_morning_star),
    ("红三兵", "bull", p_three_soldiers),
    ("刺透线", "bull", p_piercing),
    ("低位十字星", "bull", p_doji_low),
    ("上吊线", "bear", p_hanging_man),
    ("看跌吞没", "bear", p_bear_engulf),
    ("黄昏之星", "bear", p_evening_star),
    ("三只黑乌鸦", "bear", p_three_crows),
    ("乌云盖顶", "bear", p_dark_cloud),
    ("长腿十字", "neutral", p_long_legs),
]


def scan(u, date, limit_per_pattern=8):
    """全市场扫描当日 K 线形态。返回 {stats:[{pattern,direction,n}], hits:[{code,name,pattern,direction,close,pct}]}"""
    st_map = {}
    hits_by_pat = {}
    for code, _ in u.bars.items():
        st = u.stocks.get(code, {})
        name = st.get("name") or ""
        if "ST" in name.upper() or "退" in name:
            continue
        hist = u.bars_upto(code, date, 8)
        if len(hist) < 4:
            continue
        cur = hist[-1]
        if not cur["c"] or cur["c"] <= 0:
            continue
        for pname, direction, fn in PATTERNS:
            try:
                ok = fn(hist)
            except Exception:
                ok = False
            if not ok:
                continue
            s = st_map.setdefault(pname, {"pattern": pname, "direction": direction, "n": 0})
            s["n"] += 1
            lst = hits_by_pat.setdefault(pname, [])
            if len(lst) < limit_per_pattern:
                lst.append({
                    "code": code, "name": name, "pattern": pname,
                    "direction": direction,
                    "close": round(cur["c"], 2),
                    "pct": round(cur.get("pct") or 0, 2),
                })
    stats = sorted(st_map.values(), key=lambda x: -x["n"])
    hits = []
    for pname, _, _ in PATTERNS:
        hits.extend(hits_by_pat.get(pname, []))
    return {"stats": stats, "hits": hits}


if __name__ == "__main__":
    import json
    import engine
    cc = store.connect()
    u = engine.Universe(cc, days=270)
    d = u.dates[-1] if u.dates else None
    rep = scan(u, d)
    print("K线形态统计 (%s)：%s" % (d, {s["pattern"]: s["n"] for s in rep["stats"]}))
    for h in rep["hits"][:20]:
        print(json.dumps(h, ensure_ascii=False))
