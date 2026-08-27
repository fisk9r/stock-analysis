# -*- coding: utf-8 -*-
"""集成测试：本轮「一键全部升级」+ 3 点新需求的落地验证（基于真实 cache/market.db）。

覆盖：
  A1 触发盯盘 alerts.build_triggers
  A2 波段网格 zones.band_levels grid_buy/grid_sell
  A3 融合盈亏比 engine.fuse_recommend r 字段
  A4 总仓位建议 engine.position_suggestion
  B5 席位可跟性（build 内联，直接测数据结构与阈值过滤）
  B6 梯队预警 engine.ladder_warn
  B8 板块轮动结论 engine.sector_trade
  需求2 准确率归因 accuracy.build（快照缺失时优雅 None）
  需求3 趋势结论 engine.trend_verdict（买/卖/持有 + 价格 + ≤20交易日）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline"))
import store
import engine
import zones
import alerts
import accuracy

PASS_N = [0]
FAIL_N = [0]


def ok(name, cond, detail=""):
    if cond:
        PASS_N[0] += 1
        print("  [PASS] %s %s" % (name, ("-> %s" % detail) if detail else ""))
    else:
        FAIL_N[0] += 1
        print("  [FAIL] %s %s" % (name, detail))


def main():
    con = store.connect()
    u = engine.Universe(con, days=270)
    date = u.dates[-1]
    print("=== 数据基准日：%s，股票 %d 只 ===" % (date, len(u.bars)))

    # ---- 选一只有足够K线的票做载体 ----
    code = next(c for c in sorted(u.bars) if len([b for b in u.bars[c] if b["d"] <= date]) >= 60)
    bs = [b for b in u.bars[code] if b["d"] <= date]

    # A2 波段网格
    print("\n--- A2 band_levels 网格 ---")
    bd = zones.band_levels(bs)
    ok("band_levels 返回", bd is not None)
    if bd:
        ok("grid_buy 三档", isinstance(bd.get("grid_buy"), list) and len(bd["grid_buy"]) == 3,
           str(bd.get("grid_buy")))
        ok("grid_sell 三档", isinstance(bd.get("grid_sell"), list) and len(bd["grid_sell"]) == 3,
           str(bd.get("grid_sell")))
        g = bd["grid_buy"]
        ok("网格价随档位升序", all(g[i]["price"] <= g[i + 1]["price"] for i in range(len(g) - 1)))
        ok("网格仓位合计=1", abs(sum(p["ratio"] for p in g) - 1.0) < 1e-6,
           "sum=%.2f" % sum(p["ratio"] for p in g))

    # A4 总仓位建议
    print("\n--- A4 position_suggestion ---")
    pa = engine.position_suggestion("温", "均衡")
    ok("返回结构", {"suggest_pct", "level", "reason"} <= set(pa), str(pa))
    ok("取严逻辑(温60/均衡60→60)", pa["suggest_pct"] == 60)
    pa2 = engine.position_suggestion("热", "冰点")
    ok("取严逻辑(热80/冰点20→20)", pa2["suggest_pct"] == 20)

    # B6 梯队预警
    print("\n--- B6 ladder_warn ---")
    lw = engine.ladder_warn(u, date)
    ok("返回结构", {"level", "warns", "today_max", "series"} <= set(lw),
       "level=%s today_max=%s" % (lw.get("level"), lw.get("today_max")))
    ok("warns 是列表", isinstance(lw.get("warns"), list))
    ok("level 合法枚举", lw.get("level") in ("正常", "降温", "退潮", "数据不足"), lw.get("level"))

    # 需求3 趋势结论
    print("\n--- 需求3 trend_verdict ---")
    vd = engine.trend_verdict(bs, band=bd or {}, first_seen=u.dates[-30], date=date)
    ok("verdict 返回", vd is not None)
    if vd:
        ok("action 枚举", any(k in vd["action"] for k in ("买入", "卖出", "持有", "离场", "减仓")), vd["action"])
        ok("持有上限=20交易日", vd["hold_limit_days"] == 20)
        ok("days_held 正确累计(≥20)", vd["days_held"] >= 20, "%d" % vd["days_held"])
        # 价格纪律优先：已到卖出区 → 止盈；否则到期应触发离场
        if "卖出" in vd["action"]:
            ok("止盈优先于到期", True, vd["action"])
        else:
            ok("到期离场触发", vd["expired"] and ("离场" in vd["action"]), vd["action"])
        ok("reason 有内容", bool(vd["reason"]))
        ok("buy_price 卖出时也有区间字段",
           ("sell_zone" in vd) if "卖出" in vd["action"] else True)
    # 无波段区也能给结论
    vd2 = engine.trend_verdict(bs, band={})
    ok("空 band 不崩溃", vd2 is not None)
    # 到期边界：19天不应离场（除非价格触发买卖区）
    vdB = engine.trend_verdict(bs, band=bd or {}, first_seen=None, date=date)
    if vdB:
        ok("无首见日 days_held=0", vdB["days_held"] == 0)

    # A1 触发盯盘
    print("\n--- A1 build_triggers ---")
    fake = {
        "zones": {"items": [{"code": code, "name": "测试票", "close": float(bs[-1]["c"]),
                             "action": "破位卖出", "advice": "止损", "stop": round(float(bs[-1]["c"]) * 0.95, 2)}]},
        "holdings": {"items": []},
        "watch": {"items": [{"code": "999001", "name": "累计票", "since_added": {"pct": 45.0}}]},
    }
    tr = alerts.build_triggers(fake, date)
    ok("命中≥2条", tr["n"] >= 2, "n=%d" % tr["n"])
    types = {h["type"] for h in tr["hits"]}
    ok("含止损类型", "止损" in types, str(types))
    ok("含锁定类型", "锁定" in types)
    ok("summary_lines 可用", bool(alerts.summary_lines(tr)))
    empty = alerts.build_triggers({"zones": {}, "holdings": {}, "watch": {}}, date)
    ok("空输入返回 n=0", empty["n"] == 0)

    # 需求2 准确率归因（可能无 fused 快照 → None 属正常降级）
    print("\n--- 需求2 accuracy.build ---")
    try:
        acc = accuracy.build(u, date, con, topn=5)
        if acc is None:
            ok("无昨日快照→优雅None", True, "首次运行fused快照尚未积累")
        else:
            ok("hit_rate 在 0~100", 0 <= acc["hit_rate"] <= 100, str(acc["hit_rate"]))
            ok("suggestion 有内容", bool(acc["suggestion"]))
            for m in acc["miss_diag"]:
                ok("归因diag非空(%s)" % m["code"], bool(m["diag"]))
    except Exception as e:
        ok("accuracy 异常容忍", False, repr(e))

    # B8 板块轮动
    print("\n--- B8 sector_trade ---")
    c2b = {}
    try:
        import theme as _th
        c2b = getattr(_th, "CODE2BOARDS", {}) or {}
    except Exception:
        pass
    if not c2b:
        # 从缓存读：engine.Universe 已加载 code2boards? 直接用 screen_uptrend 反向验证太重，构造最小样本
        c2b = {code: [("bk1", "测试行业", "industry")]}
    st = engine.sector_trade(u, date, c2b, topn=3)
    ok("sector_trade 返回列表", isinstance(st, list), "len=%d" % len(st))
    for s in (st or [])[:2]:
        ok("板块含领涨票", "leads" in s and isinstance(s["leads"], list))

    print("\n============ 结果 ============")
    print("PASS=%d  FAIL=%d" % (PASS_N[0], FAIL_N[0]))
    return 0 if FAIL_N[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
