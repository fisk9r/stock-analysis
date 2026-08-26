# -*- coding: utf-8 -*-
"""K线相似形态检索：把焦点股（关注池 / 涨停 / 推荐池）最近 N 日的价格形态，
与全市场其他个股同期形态做归一化比对，找出「长得最像」的标的，并回看这些相似标的
后续 10 日真实涨跌——回答「历史上这种形态之后通常怎么走」。

纯本地、零网络；计算量集中在 CI（焦点股通常 <200 只，市场取样 1500 只已足够）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store


WIN = 20          # 比对窗口
SAMPLE_CAP = 1500 # 市场取样上限（控制算力）
TOPK = 5


def _norm(series):
    """去均值、按幅度归一，得到「纯形态」向量（平移/缩放不变）"""
    if len(series) < WIN:
        return None
    s = series[-WIN:]
    mean = sum(s) / len(s)
    dev = [x - mean for x in s]
    var = sum(d * d for d in dev) / len(s)
    if var <= 1e-9:
        return None
    std = var ** 0.5
    return [d / std for d in dev]


def _corr(a, b):
    sa, sb = 0.0, 0.0
    for x, y in zip(a, b):
        sa += x * y
    return sa  # 已归一化 => 内积即余弦/相关系数


def _fwd_ret(u, code, date, days=10):
    bs = [b for b in u.bars.get(code, []) if b["d"] <= date]
    idx = None
    for i, b in enumerate(bs):
        if b["d"] == date:
            idx = i
            break
    if idx is None or idx + days >= len(bs):
        return None
    c0 = bs[idx]["c"]
    c1 = bs[idx + days]["c"]
    if not c0:
        return None
    return round((c1 / c0 - 1) * 100, 1)


def build_matrix(u, codes):
    out = {}
    for c in codes:
        bs = [b for b in u.bars.get(c, []) if b.get("c")]
        if len(bs) < WIN:
            continue
        v = _norm([b["c"] for b in bs])
        if v:
            out[c] = v
    return out


def scan(u, date, focus=None):
    if focus is None:
        focus = set()
        for c in (u.zt.get(date) or set()):
            focus.add(c)
        for c in (u.stocks.keys()):
            pass
    # 焦点池：关注股 + 涨停 + 推荐池（若有）
    if not focus:
        focus = set(u.zt.get(date) or set())
    # 抽样市场（优先流通市值适中、有成交的票）
    all_codes = [c for c in u.bars.keys() if len(u.bars.get(c, [])) >= WIN]
    if SAMPLE_CAP and len(all_codes) > SAMPLE_CAP:
        import random
        random.seed(20260825)
        all_codes = random.sample(all_codes, SAMPLE_CAP)

    focus_vecs = build_matrix(u, focus)
    mkt_vecs = build_matrix(u, all_codes)
    if not focus_vecs or not mkt_vecs:
        return None

    result = {}
    for fc, fv in focus_vecs.items():
        scored = []
        for c, v in mkt_vecs.items():
            if c == fc:
                continue
            sc = _corr(fv, v)
            scored.append((sc, c))
        scored.sort(reverse=True)
        matches = []
        for sc, c in scored[:TOPK]:
            fwd = _fwd_ret(u, c, date, 10)
            matches.append({
                "code": c,
                "name": (u.stocks.get(c, {}) or {}).get("name", "") or c,
                "corr": round(sc, 3),
                "fwd10": fwd,
            })
        result[fc] = {
            "code": fc,
            "name": (u.stocks.get(fc, {}) or {}).get("name", "") or fc,
            "matches": matches,
            "matches_up": sum(1 for m in matches if (m["fwd10"] or 0) > 0),
        }
    if not result:
        return None
    # 仅保留后续表现可回溯的（避免全是未来票）
    items = [v for v in result.values() if v["matches"]]
    return {"date": date, "win": WIN, "focus_n": len(focus_vecs), "items": items[:60]}


def summary_lines(ps):
    if not ps:
        return []
    out = ["相似形态检索：%d 只焦点股比对全市场，相似标的 10 日上涨占比统计："
           % ps.get("focus_n", 0)]
    shown = 0
    for it in ps.get("items", [])[:6]:
        n = len(it["matches"])
        up = it["matches_up"]
        if n:
            out.append("- %s：最相似 %s 等 %d 只，其中 %d 只后续10日收涨"
                       % (it["name"], "、".join(m["name"] for m in it["matches"][:2]), n, up))
            shown += 1
    if not shown:
        out.append("（历史样本不足，暂无可回溯的相似段）")
    return out
