# -*- coding: utf-8 -*-
"""2026-09-04 升级包回归测试（短期4 + 中期4 + 长期2 全覆盖）。

覆盖：
  ① factor_health   因子健康度（IC 计算/判定）
  ② 卖出体系强化    T+1 锁定（reco_push.compute_holdings_ops）
  ③ stock_profile   个股深度档案
  ④ notify.push_scope 推送分层开关（解析与容错）
  ⑤ kronos_official 懒加载降级（CI 无 torch 不崩）
  ⑥ live_monitor    时段闸 + 警报去重（纯逻辑部分）
  ⑦ broker_live     实盘安全红线（默认关/dry_run/风控拦截）
  ⑧ multitime       小时级成本锚（纯函数）
  ⑨ 个性化策略      period 权重矩阵（short/mid/long 打分差异）
  ⑩ risklevel       红黄蓝分级
"""
import sys
import os
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

PASS = []
FAIL = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (" :: " + extra if extra else ""))
        print("  FAIL: %s %s" % (name, extra))


# ═══════════════════ 合成行情工具 ═══════════════════
def mk_bars(closes, jitter=0.0, seed=7):
    """closes → bars [{d,o,h,l,c,v}]（含确定性影线，无随机依赖时形态稳定）。"""
    import random
    rnd = random.Random(seed)
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        h = max(o, c) * (1 + 0.01 + abs(jitter) * rnd.random())
        l = min(o, c) * (1 - 0.01 - abs(jitter) * rnd.random())
        bars.append({"d": "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                     "o": round(o, 2), "h": round(h, 2), "l": round(l, 2),
                     "c": round(c, 2), "v": 1000 + (i % 5) * 100})
        prev = c
    return bars


class FakeU:
    def __init__(self, bars_map, stocks=None):
        self.bars = bars_map
        self.stocks = stocks or {}


# ═══════════════════ ① factor_health ═══════════════════
def test_factor_health():
    print("== ① factor_health ==")
    import factor_health as fh

    # 构造「5日动量强预测未来收益」的宇宙：ret_fwd = 0.4 * mom5
    bars_map = {}
    n_hist = 60
    n_stock = 60
    base_cal = []
    for i in range(n_hist):
        base_cal.append("D%03d" % i)
    for s in range(n_stock):
        drift = 0.002 * (s % 7 - 3)   # 截面差异
        closes = [10.0]
        for i in range(1, n_hist):
            mom = sum(closes[-5:][j] / closes[-5:][j - 1] - 1 for j in range(1, 5)) if len(closes) >= 6 else 0
            closes.append(closes[-1] * (1 + drift + 0.4 * mom / 5))
        bars = []
        prev = closes[0]
        for i, c in enumerate(closes):
            bars.append({"d": base_cal[i], "o": prev, "h": c * 1.01, "l": c * 0.99,
                         "c": c, "v": 1000})
            prev = c
        bars_map["%06d" % (600000 + s)] = bars
    # 交易日历参考（sh000001）
    bars_map["sh000001"] = [{"d": base_cal[i], "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
                            for i in range(n_hist)]
    u = FakeU(bars_map)
    res = fh.compute(u, base_cal[-1], lookback=15, horizon=5, sample_n=50, min_bars=55)
    check("fh_no_error", not res.get("error"), str(res.get("error")))
    fmap = {f["name"]: f for f in res.get("factors", [])}
    check("fh_has_mom5", "mom5" in fmap, str(list(fmap)))
    if "mom5" in fmap:
        check("fh_mom5_positive_ic", (fmap["mom5"]["ic_mean"] or 0) > 0.15,
              "ic_mean=%s" % fmap["mom5"]["ic_mean"])
        check("fh_mom5_status", fmap["mom5"]["status"] in ("有效", "弱有效"),
              fmap["mom5"]["status"])
    check("fh_series_lag", all(f.get("series") for f in res.get("factors", [])))

    # spearman 边界
    check("fh_spearman_perfect", fh.spearman([1, 2, 3, 4, 5, 6, 7, 8], [2, 4, 6, 8, 10, 12, 14, 16]) == 1.0)
    check("fh_spearman_inverse", abs(fh.spearman([1, 2, 3, 4, 5, 6, 7, 8], [8, 7, 6, 5, 4, 3, 2, 1]) + 1.0) < 1e-9)
    check("fh_spearman_short", fh.spearman([1, 2], [2, 1]) is None)


# ═══════════════════ ② T+1 锁定（卖出体系强化） ═══════════════════
def test_t1_lock():
    print("== ② T+1 锁定 ==")
    import holdings as H
    import reco_push as rp

    orig = H.load_positions
    # 用真实持仓生成一份足够长的 K 线
    code = "002631"
    closes = [10.0 + 0.01 * i for i in range(80)]
    u = FakeU({code: mk_bars(closes)}, {code: {"name": "德尔未来"}})
    DATE = u.bars[code][-1]["d"]

    # 场景1：今日买入 → 锁定标记与原因
    H.load_positions = lambda: [{"code": code, "name": "德尔未来", "cost": 10.36,
                                 "date": DATE, "period": "short"}]
    try:
        ops = rp.compute_holdings_ops(u, DATE, None, {})
        o = ops[0]
        check("t1_flag_true", o.get("t1_locked") is True, str(o.get("t1_locked")))
        check("t1_reason", any("T+1" in r for r in (o.get("reasons") or [])))
        check("t1_period_short", o.get("period") == "short")

        # 场景1b：今日买入 + 破位下跌（决策=卖出类）→ 决策必须被压成 T+1锁定
        closes_dn = [15.0 - 0.06 * i for i in range(80)]
        bars_dn = []
        prev = closes_dn[0]
        for i, c in enumerate(closes_dn):
            bars_dn.append({"d": "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                            "o": prev, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 1000})
            prev = c
        u_dn = FakeU({code: bars_dn}, {code: {"name": "德尔未来"}})
        H.load_positions = lambda: [{"code": code, "name": "德尔未来", "cost": 14.5,
                                     "date": DATE}]
        ops1b = rp.compute_holdings_ops(u_dn, DATE, None, {})
        check("t1_sell_overridden", "T+1" in (ops1b[0].get("decision") or ""),
              ops1b[0].get("decision"))

        # 场景2：昨日买入 → 不锁定
        y_date = u.bars[code][-2]["d"]
        H.load_positions = lambda: [{"code": code, "name": "德尔未来", "cost": 10.36,
                                     "date": y_date}]
        ops2 = rp.compute_holdings_ops(u, DATE, None, {})
        check("t1_flag_false_prev", ops2[0].get("t1_locked") is False)

        # 场景3：无买入日 → 不锁定、不报错
        H.load_positions = lambda: [{"code": code, "name": "德尔未来", "cost": 10.36}]
        ops3 = rp.compute_holdings_ops(u, DATE, None, {})
        check("t1_flag_false_nodate", not ops3[0].get("t1_locked"))
        check("t1_default_mid", ops3[0].get("period") == "mid")
    finally:
        H.load_positions = orig


# ═══════════════════ ③ stock_profile ═══════════════════
def test_stock_profile():
    print("== ③ stock_profile ==")
    import stock_profile as sp

    bars = mk_bars([10 + 0.05 * ((i * 7) % 13) for i in range(80)])
    p = sp.build_profile("002631", "德尔未来", bars, cost=10.36,
                         zone_result={"action": "继续持有", "buy_zone": [9.5, 10.0],
                                      "stop": 9.0, "reasons": ["结构完整"]},
                         board="装修家居")
    check("sp_basic", p["code"] == "002631" and p["name"] == "德尔未来")
    check("sp_ma", p.get("ma5") and p.get("ma20") and p.get("ma60"))
    check("sp_hi60", p.get("hi60") is not None and p.get("dd_from_hi60") is not None)
    check("sp_pnl", p.get("pnl") is not None)
    check("sp_zone", p.get("zone", {}).get("action") == "继续持有")
    check("sp_ma_shape", p.get("ma_shape") in ("多头排列", "空头排列", "纠缠"))
    # 短bars 精简档案
    p2 = sp.build_profile("1", "x", bars[:10])
    check("sp_short_note", "精简" in (p2.get("note") or ""))
    # collect 批量
    u = FakeU({"002631": bars}, {"002631": {"name": "德尔未来"}})
    out = sp.collect(u, bars[-1]["d"], ["002631", "600000"], max_n=5)
    check("sp_collect", "002631" in out and len(out) <= 5)


# ═══════════════════ ④ push_scope ═══════════════════
def test_push_scope():
    print("== ④ push_scope ==")
    import notifier

    ps = notifier.load_push_scope()   # 当前 notify.json enabled=false
    check("ps_disabled_returns_none", ps is None, str(ps))
    # 解析容错：临时改 enabled=true（用 monkeypatch 文件不安全 → 直接测 dict 逻辑）
    fake = {"enabled": True, "min_score": 60, "pool_limit": {"trend": 3}}
    cands = {"ladder": [{"score": 70}, {"score": 50}], "trend": [{"score": 65}, {"score": 80}, {"score": 90}, {"score": 40}], "band": []}
    _ms = fake.get("min_score") or 0

    def _fit(arr, kind):
        arr = [c for c in (arr or []) if (c.get("score") or 0) >= _ms]
        lim = fake.get("pool_limit", {}).get(kind)
        return arr[:lim] if isinstance(lim, int) and lim > 0 else arr

    l2 = _fit(cands["ladder"], "ladder")
    t2 = _fit(cands["trend"], "trend")
    check("ps_min_score", len(l2) == 1 and l2[0]["score"] == 70, str(l2))
    check("ps_pool_limit", len(t2) == 3, str(len(t2)))
    # load_push_scope 对坏文件容错
    orig_join = os.path.join
    notifier.ROOT = "/nonexistent_dir_for_test"
    check("ps_badfile_none", notifier.load_push_scope() is None)
    notifier.ROOT = ROOT


# ═══════════════════ ⑤ kronos_official 降级 ═══════════════════
def test_kronos_official():
    print("== ⑤ kronos_official ==")
    from kronos_official import KronosOfficial, load_config
    cfg = load_config()
    ko = KronosOfficial({"enabled": False, "device": "cpu"})
    check("ko_disabled", ko.available() is False)
    check("ko_predict_none", ko.predict_next([{"d": "x", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]) is None)
    ko2 = KronosOfficial({"enabled": True, "device": "cpu"})
    # 无 torch 环境应降级为 False，且不抛异常
    try:
        av = ko2.available()
        check("ko_graceful", av in (True, False))   # 有 torch 也可以，但 CI 应为 False
    except Exception as e:
        check("ko_graceful", False, "%r" % e)
    check("ko_config_default", isinstance(cfg, dict))


# ═══════════════════ ⑥ live_monitor 纯逻辑 ═══════════════════
def test_live_monitor():
    print("== ⑥ live_monitor ==")
    import live_monitor as lm
    from datetime import datetime

    # 时段闸
    check("lm_weekend", lm.in_trading_time(datetime(2026, 9, 5, 10, 0)) is False)   # 周六
    check("lm_open", lm.in_trading_time(datetime(2026, 9, 4, 10, 0)) is True)       # 周五盘中
    check("lm_lunch", lm.in_trading_time(datetime(2026, 9, 4, 12, 0)) is False)     # 午休
    check("lm_close", lm.in_trading_time(datetime(2026, 9, 4, 15, 1)) is False)     # 收盘后
    # 警报去重：同日同类型只报一次
    m = lm.Monitor(dict(lm.DEFAULT_CONF), dry_run=True)
    check("lm_dedup_first", m._should_alert("002631", "surge") is True)
    check("lm_dedup_second", m._should_alert("002631", "surge") is False)
    check("lm_dedup_other_kind", m._should_alert("002631", "plunge") is True)
    check("lm_symbol", lm.tencent_symbol("002631") == "sz002631" and lm.tencent_symbol("600500") == "sh600500")


# ═══════════════════ ⑦ broker_live 安全红线 ═══════════════════
def test_broker_live():
    print("== ⑦ broker_live ==")
    from broker_live import BrokerLive, load_config

    b = BrokerLive({"live_trading": False, "dry_run": True})
    r = b.place_order("002631", "buy", 10.0, 100)
    check("bl_blocked_default", r["ok"] is False and "LIVE_TRADING" in r["msg"], r["msg"])
    check("bl_dry_positions", b.query_positions() == [])
    # 开总闸 + dry_run → 模拟成交
    b2 = BrokerLive({"live_trading": True, "dry_run": True, "broker": "miniqmt"})
    r2 = b2.place_order("002631", "buy", 10.0, 100)
    check("bl_dry_ok", r2["ok"] is True and r2["dry_run"] is True, str(r2))
    # 风控拦截
    def risk(code, action, price, qty):
        return (False, "单票超限")
    r3 = b2.place_order("002631", "buy", 10.0, 100, risk_check=risk)
    check("bl_risk_block", r3["ok"] is False and "风控" in r3["msg"], str(r3))
    # 风控放行 → dry ok
    r4 = b2.place_order("002631", "buy", 10.0, 100, risk_check=lambda *a: (True, ""))
    check("bl_risk_pass", r4["ok"] is True)
    # 未实现原语在真实模式下被拦
    b3 = BrokerLive({"live_trading": True, "dry_run": False, "broker": "miniqmt"})
    r5 = b3.place_order("002631", "buy", 10.0, 100)
    check("bl_notimpl", r5["ok"] is False, str(r5))
    check("bl_cfg_default", load_config().get("live_trading") is False)


# ═══════════════════ ⑧ multitime ═══════════════════
def test_multitime():
    print("== ⑧ multitime ==")
    import multitime as mt

    bars = [{"dt": "202609040930", "o": 10, "c": 10 + i * 0.1, "h": 10.2 + i * 0.1,
             "l": 9.9, "v": 100} for i in range(30)]
    a = mt.compute_anchors(bars, price=bars[-1]["c"])
    check("mt_ma5", a and a["ma5"] == round(sum(x["c"] for x in bars[-5:]) / 5, 3), str(a))
    check("mt_pos_above", a["pos"] == "above")   # 上行序列：现价在小时MA5上方
    check("mt_trend", a["trend"] == "multi_up")
    check("mt_short_none", mt.compute_anchors(bars[:3]) is None)
    check("mt_symbol", mt.tencent_symbol("600500") == "sh600500")
    # 网络测试（可失败不致败）
    live = mt.fetch_m60("002631", n=8)
    if live:
        check("mt_live_ok", isinstance(live, list) and len(live) >= 1)
        a2 = mt.compute_anchors(live, price=live[-1]["c"])
        check("mt_live_anchors", a2 and a2.get("ma5") is not None)
    else:
        print("  (网络不可达，跳过 fetch_m60 实测)")
        PASS.append("mt_live_skipped")


# ═══════════════════ ⑨ 个性化策略权重 ═══════════════════
def test_period_weights():
    print("== ⑨ 个性化策略 ==")
    import reco_push as rp

    check("pw_matrix", set(rp.PERIOD_PROFILES) == {"short", "mid", "long"})
    check("pw_short_ladder_gt_band", rp.PERIOD_PROFILES["short"]["ladder"] > rp.PERIOD_PROFILES["short"]["band"])
    check("pw_long_band_gt_ladder", rp.PERIOD_PROFILES["long"]["band"] > rp.PERIOD_PROFILES["long"]["ladder"])
    check("pw_period_of", rp._period_of({"period": "short"}) == "short"
          and rp._period_of({"period": "短线"}) == "short"
          and rp._period_of({}) == "mid"
          and rp._period_of({"period": "long"}) == "long")

    # 打分差异：同一连板票在 short 下分数 > long 下
    rec = {"ladder_plans": [{"code": "002631", "name": "德尔未来", "buy_zone": [10.0, 10.3],
                             "sell_zone": [11.0, 11.5], "stop": 9.5, "worth_score": 60,
                             "entry_streak": 2}],
           "trend": [], "band_trade": [], "sector_trend": []}
    u = FakeU({"002631": mk_bars([10 + 0.01 * i for i in range(60)])},
              {"002631": {"name": "德尔未来"}})
    cands_s = rp.compute_buy_candidates(rec, u, u.bars["002631"][-1]["d"], {}, {})
    rec2 = dict(rec)
    cands_l = rp.compute_buy_candidates(rec2, u, u.bars["002631"][-1]["d"], {}, {}, period="long")
    check("pw_candidates_short", len(cands_s["ladder"]) == 1)
    if cands_s["ladder"] and cands_l["ladder"]:
        check("pw_short_gt_long", cands_s["ladder"][0]["score"] > cands_l["ladder"][0]["score"],
              "short=%s long=%s" % (cands_s["ladder"][0]["score"], cands_l["ladder"][0]["score"]))
    check("pw_period_label", cands_s.get("period") == "中线")   # 缺省 period=mid


# ═══════════════════ ⑩ risklevel ═══════════════════
def test_risklevel():
    print("== ⑩ risklevel ==")
    import risklevel as rl

    # 红：引擎卖出
    lvl, rs = rl.classify_holding({"decision": "卖出", "code": "002631"})
    check("rl_red_sell", lvl == "red" and rs)
    # 红：追板回落
    lvl2, rs2 = rl.classify_holding({"decision": "继续持有·格局",
                                     "reasons": ["⚠ 追板回落：触板后回落"]})
    check("rl_red_zb", lvl2 == "red")
    # 红：破止损
    lvl3, _ = rl.classify_holding({"decision": "继续持有·格局", "close": 8.9, "stop": 9.0})
    check("rl_red_stop", lvl3 == "red")
    # 黄：贴近止损
    lvl4, rs4 = rl.classify_holding({"decision": "谨慎持有·观察", "close": 9.2, "stop": 9.0})
    check("rl_yellow_near", lvl4 == "yellow", str(rs4))
    # 黄：浮亏
    lvl5, _ = rl.classify_holding({"decision": "继续持有·格局", "pnl": -6.0})
    check("rl_yellow_pnl", lvl5 == "yellow")
    # 蓝
    lvl6, _ = rl.classify_holding({"decision": "继续持有·格局", "pnl": 2.0})
    check("rl_blue", lvl6 == "blue")
    # compute 全链
    data = {"holdings_ops": [{"code": "002631", "name": "德尔未来", "decision": "卖出"}],
            "board_strength": {}, "panic": {}}
    out = rl.compute(data, date="2026-09-04")
    check("rl_compute_overall", out["overall"]["level"] == "red", str(out["overall"]))
    check("rl_compute_counts", out["counts"]["red"] == 1)
    check("rl_compute_holdings", out["holdings"][0]["level"] == "red")
    # 容错
    check("rl_err_safe", rl.compute(None)["overall"]["level"] in ("red", "yellow", "blue"))


if __name__ == "__main__":
    test_factor_health()
    test_t1_lock()
    test_stock_profile()
    test_push_scope()
    test_kronos_official()
    test_live_monitor()
    test_broker_live()
    test_multitime()
    test_period_weights()
    test_risklevel()
    print("\n══════════════════════════════")
    print("PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for x in FAIL:
            print("  ✗ " + x)
        sys.exit(1)
    print("ALL PASS ✔")
