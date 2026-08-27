# -*- coding: utf-8 -*-
"""推荐准确率归因：昨日 Top 推荐次日未晋级时，自动诊断「哪个环节出了问题」。

数据全部本地（engine_snapshots 中 fuse_recommend 历史 + rec_picks 结局），零网络。

工作方式：
  1. 取昨日快照里的 fused TopN（综合分最高的 N 只）；
  2. 用当日 bars 计算它们的 T+1 实际表现（晋级=次日涨幅≥2% 且收阳）；
  3. 未晋级的逐票归因：哪个引擎给了它分数、该引擎「昨日证据」今天是否失效
     （如连板引擎给了高 worth_score 但次日炸板 → 环境判据错，非个股错）；
  4. 输出 env_warning / engine_adjust 建议，随 build 落库并推送。

诚实边界：单日样本不构成统计显著性；本模块产出的是「当日诊断+提示」，
不自动改 fuse 权重（权重优化用 tools/optimize_weights.py 长周期做）。
"""
import json
import store


def _pct(b0, b1):
    if not b0 or not b0.get("c") or not b1 or not b1.get("c"):
        return None
    try:
        return (float(b1["c"]) / float(b0["c"]) - 1) * 100
    except Exception:
        return None


def build(u, date, con=None, topn=5):
    """返回 {
      topn, hits:[{code,name,fusion_score,pct,advanced,engines}], hit_rate,
      miss_diag:[{code,name,engines,diag}], suggestion:str
    } 或 None（无昨日快照）。
    """
    if con is None:
        con = store.connect()
    prev = store.snapshot_history(con, "fused", days=3)
    # prev: [(date, payload_dict)] 正序；找严格早于 date 的最近一份
    yesterday, ydate = None, None
    for d, pj in prev:
        if d < date:
            yesterday = pj
            ydate = d
            break
    if not yesterday:
        return None

    def bars_of(code):
        bs = [b for b in (u.bars.get(code) or []) if b["d"] <= date]
        return bs

    items = []
    for it in (yesterday or [])[:topn]:
        code = it.get("code")
        if not code:
            continue
        bs = bars_of(code)
        # 昨日收盘 = 快照日(ydate) 之后、≤date 的最后一根 = date 这根；前一根为基准
        today_b = bs[-1] if bs and bs[-1]["d"] == date else None
        yest_b = next((b for b in reversed(bs) if b["d"] == ydate), None)
        pct = _pct(yest_b, today_b) if (yest_b and today_b) else None
        advanced = bool(pct is not None and pct >= 2 and today_b and float(today_b["c"]) >= float(today_b.get("o") or today_b["c"]))
        engines = [e.get("engine") for e in (it.get("evidence") or [])]
        items.append({"code": code, "name": it.get("name", ""),
                      "fusion_score": it.get("fusion_score"),
                      "pct": round(pct, 2) if pct is not None else None,
                      "advanced": advanced,
                      "engines": engines})

    rated = [x for x in items if x["pct"] is not None]
    if not rated:
        return None
    hit_rate = round(100.0 * sum(1 for x in rated if x["advanced"]) / len(rated), 1)

    # ---- 归因：对未晋级票检查各引擎证据是否反向 ----
    miss_diag = []
    for x in rated:
        if x["advanced"]:
            continue
        pcts = x["pct"]
        diag = []
        for eng in x["engines"]:
            if eng == "连板接力" and pcts <= 0:
                diag.append("连板接力证据失效：推荐依据『接力动量』但次日转跌，属环境问题——留意梯队断板/炸板率")
            elif eng == "趋势主升" and pcts <= -3:
                diag.append("趋势主升证据受损：跌破短期支撑，回撤大于正常波段，检查 MA20 是否守住")
            elif eng == "游资席位" and pcts <= 0:
                diag.append("席位跟随未兑现：知名席位净买后次日走弱，跟风盘不足")
            elif eng == "区间破位":
                diag.append("区间引擎早已给出破位警告，其他引擎与之矛盾——融合惩罚力度可加强")
        if not diag:
            diag.append("多引擎共振但次日平淡：可能题材热度退潮或大盘拖累，个股层面无明确错误信号")
        miss_diag.append({"code": x["code"], "name": x["name"],
                          "pct": pcts, "engines": x["engines"], "diag": diag})

    n_miss = len(miss_diag)
    if hit_rate >= 60:
        sug = "Top%d 次日命中率 %s%%：融合体系状态健康" % (len(rated), hit_rate)
    elif n_miss > len(rated) / 2:
        bad_engs = {}
        for m in miss_diag:
            for e in m["engines"]:
                bad_engs[e] = bad_engs.get(e, 0) + 1
        worst = sorted(bad_engs.items(), key=lambda kv: -kv[1])[:2]
        sug = ("命中率仅 %s%%：主要失准环节 [%s]。建议：① 若梯队断板/炸板率高 → 环境退潮，"
               "压低连板/席位引擎权重；② 用 tools/optimize_weights.py 按长期胜率重校。"
               % (hit_rate, "、".join("%s(%d次)" % kv for kv in worst) if worst else "整体"))
    else:
        sug = "命中率 %s%%：个别标的失准，属正常波动，无需调整引擎" % hit_rate

    return {"yesterday": ydate, "today": date, "topn": len(rated),
            "hits": items, "hit_rate": hit_rate,
            "n_miss": n_miss, "miss_diag": miss_diag, "suggestion": sug}


def summary_lines(acc):
    if not acc:
        return []
    out = ["推荐准确率归因（%s → %s）：Top%d 次日晋级率 **%s%%**"
           % (acc.get("yesterday"), acc.get("today"), acc.get("topn"), acc.get("hit_rate"))]
    for m in acc.get("miss_diag", [])[:4]:
        out.append("- ✗ %s（%s）%+.1f%%：%s" % (m["name"], m["code"], m["pct"], m["diag"][0]))
    out.append("- " + acc.get("suggestion", ""))
    return out
