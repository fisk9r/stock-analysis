# -*- coding: utf-8 -*-
"""轻量权重自优化：用推荐池历史胜率反推各引擎（标签）的相对权重建议。

读 store.rec_picks_all 历史（每次 build 已 upsert 当日推荐及次日结局），
按推荐标签分组统计 T+1 胜率 / 平均收益，给出「建议权重」，可写盘供
engine.fuse_recommend 读取（若未来支持外部权重）。纯本地、零网络。

用法：python tools/optimize_weights.py [--write]
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline"))
import store


TAG_ALIAS = {
    "连板": "连板接力", "趋势": "趋势主升", "强动量": "趋势主升",
    "价值": "价值修复", "首板": "连板接力", "反包": "连板接力",
}


def analyze():
    con = store.connect()
    rows = store.rec_picks_all(con, limit=4000)
    if not rows:
        return None
    by = {}
    for date, code, name, streak, p_break, tag, ncont, npct in rows:
        t = TAG_ALIAS.get(tag, tag or "其他")
        d = by.setdefault(t, {"n": 0, "win": 0, "pn": 0.0})
        d["n"] += 1
        try:
            npct = float(npct or 0)
        except Exception:
            npct = 0.0
        d["pn"] += npct
        if npct > 0:
            d["win"] += 1
    out = {}
    for t, d in by.items():
        if d["n"] < 10:
            continue
        wr = d["win"] / d["n"] * 100
        ap = d["pn"] / d["n"]
        # 建议权重：胜率 × (1 + 平均收益/5)，归一到 0~1
        w = max(0.0, wr / 100.0 * (1 + ap / 5.0))
        out[t] = {"n": d["n"], "win_rate": round(wr, 1),
                  "avg_pct": round(ap, 2), "weight": round(w, 3)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写入 config/fuse_weights.json")
    args = ap.parse_args()
    res = analyze()
    if not res:
        print("无足够历史样本，无法优化权重。")
        return
    print("推荐标签历史表现 → 建议权重：")
    print("%-10s %6s %8s %10s %8s" % ("标签", "样本", "胜率%", "均收益%", "建议权重"))
    for t, v in sorted(res.items(), key=lambda kv: -kv[1]["weight"]):
        print("%-10s %6d %8.1f %10.2f %8.3f" % (t, v["n"], v["win_rate"], v["avg_pct"], v["weight"]))
    if args.write:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "fuse_weights.json")
        json.dump(res, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("\n已写入 %s" % path)


if __name__ == "__main__":
    main()
