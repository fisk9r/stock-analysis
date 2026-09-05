# -*- coding: utf-8 -*-
"""reco_push 新模式三段式数据准备的回归测试。

覆盖：
  - board_strength_map / board_bonus：主线/强势/净流入加权 + 模糊匹配
  - compute_holdings_ops：卖出类置顶、决策映射、字段齐全
  - compute_buy_candidates：趋势只留买点(排除 等回踩/过热/破位)；波段分数拉开差距(不恒100)；连板可用
  - _mk_cand：综合分裁剪 [0,100]
用法：python tools/test_reco_push.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import reco_push as rp

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, extra))


# ───────────── ③ 板块强度图 ─────────────
rec = {
    "sector_trend": [
        {"sector": "光伏", "tier": "主线", "leads": [{"name": "阳光电源"}]},
        {"sector": "白酒", "tier": "强势", "leads": []},
    ],
}
money = {
    "boards_in": [{"name": "光伏", "net": 12.0}, {"name": "芯片", "net": 20.0}],
    "boards_out": [{"name": "地产", "net": -9.0}],
}
bmap = rp.board_strength_map(rec, money)
check("板块强度: 光伏主线+净流入为正", bmap.get("光伏", 0) > 30, "光伏=%s" % bmap.get("光伏"))
check("板块强度: 白酒强势为正(15)", bmap.get("白酒", 0) >= 15, "白酒=%s" % bmap.get("白酒"))
check("板块强度: 地产净流出为负", bmap.get("地产", 0) < 0, "地产=%s" % bmap.get("地产"))
check("板块强度: 芯片净流入封顶<=12", bmap.get("芯片", 0) <= 12, "芯片=%s" % bmap.get("芯片"))

# board_bonus 模糊匹配
bonus_exact = rp.board_bonus("光伏", bmap)
bonus_fuzzy = rp.board_bonus("光伏设备", bmap)  # 子串应包含
check("板块bonus: 模糊匹配(光伏设备吃到光伏强度)", bonus_fuzzy > 0, "fuzzy=%s exact=%s" % (bonus_fuzzy, bonus_exact))
check("板块bonus: 无匹配返回0", rp.board_bonus("—", bmap) == 0)
check("板块bonus: 裁剪[-25,25]", -25 <= rp.board_bonus("光伏", bmap) <= 25)


# ───────────── ① 持仓操作决策映射 ─────────────
class _FakeU:
    def __init__(self, bars):
        self.bars = bars
        self.stocks = {}


def _fake_bars(prices):
    return [{"d": "2026-0%d-01" % (i + 1), "o": p, "h": p * 1.02, "l": p * 0.98,
             "c": p, "v": 1e6} for i, p in enumerate(prices)]


# 构造一条持仓：成本10，现价12，浮盈，趋势上行 → 继续持有·格局
bars_up = _fake_bars([9, 9.5, 10, 10.5, 11, 11.5, 12])
u = _FakeU({"X": bars_up})

# 直接测 _map_holding_decision 映射
check("决策映射: 破位卖出→卖出", rp._map_holding_decision({"action": "破位卖出", "rotate": None})[0] == "卖出")
check("决策映射: 止损rotate→卖出", rp._map_holding_decision({"action": "x", "rotate": "止损"})[0] == "卖出")
check("决策映射: 更换rotate→卖出换股", rp._map_holding_decision({"action": "x", "rotate": "更换"})[0] == "卖出换股")
check("决策映射: 加仓提示→加仓低吸", rp._map_holding_decision({"action": "加仓提示", "rotate": None})[0] == "加仓低吸")
check("决策映射: 逼近卖出→格局持有·注意止盈", rp._map_holding_decision({"action": "逼近卖出", "rotate": None})[0] == "格局持有·注意止盈")
check("决策映射: 默认→继续持有·格局", rp._map_holding_decision({"action": "运行买区上方", "rotate": None})[0] == "继续持有·格局")


# ───────────── ② 买点候选 ─────────────
rec2 = {
    "ladder_plans": [
        {"code": "001", "name": "连板A", "buy_zone": [10.0, 10.3], "sell_zone": [11.5, 12.0],
         "stop": 9.5, "worth_score": 60, "entry_streak": 3, "expected_top": "13.0"},
    ],
    "trend": [
        # 可买
        {"code": "002", "name": "趋势B", "entry_state": "可买", "close": 20.0,
         "buy_zone": [18, 19], "sell_zone": [23, 24], "stop": 17,
         "worth_score": 71, "industry": "光伏", "streak": 2,
         "trend_meta": {"trend_state": "加速上行"}},
        # 等回踩（应被剔除）
        {"code": "003", "name": "趋势C", "entry_state": "等回踩", "close": 30.0,
         "buy_zone": [25, 26], "sell_zone": [33, 34], "stop": 24, "worth_score": 70},
        # 过热勿追（应被剔除）
        {"code": "004", "name": "趋势D", "entry_state": "过热勿追", "close": 40.0,
         "buy_zone": [30, 31], "sell_zone": [43, 44], "stop": 29, "worth_score": 70},
    ],
    "band_trade": [
        # 反复底：touches 高
        {"code": "005", "name": "波段E", "board": "电力", "close": 8.0,
         "buy_zone": [7.8, 8.1], "sell_zone": [9.5, 10.0], "stop": 7.2,
         "bottom": 7.9, "touches": 15, "bounce": 2.0},
        # 远离买区（close > bz*1.05，应剔除）
        {"code": "006", "name": "波段F", "board": "医药", "close": 20.0,
         "buy_zone": [9.0, 9.5], "sell_zone": [12, 13], "stop": 8.5,
         "bottom": 9.2, "touches": 6, "bounce": 1.0},
    ],
}
cands = rp.compute_buy_candidates(rec2, u, "2026-07-01", {}, bmap)
check("买点候选: 连板1只", len(cands["ladder"]) == 1, cands["ladder"])
check("买点候选: 趋势只留可买(剔除等回踩/过热)=1", len(cands["trend"]) == 1, [c["name"] for c in cands["trend"]])
check("买点候选: 波段剔除远离买区=1", len(cands["band"]) == 1, [c["name"] for c in cands["band"]])

be = cands["band"][0]
check("波段: 综合分非100(拉开差距)", be["score"] < 100 and be["score"] > 0, "score=%s" % be["score"])
check("波段: 基础分<=85", be["base_score"] <= 85, "base=%s" % be["base_score"])
check("波段: 有回踩买区/卖区", be["buy_zone"] and be["sell_zone"])

# 连板买点含 close 派生
check("连板: 派生 close≈buy_zone[0]/0.995", abs(cands["ladder"][0]["close"] - 10.0/0.995) < 0.1)

# _mk_cand 综合分裁剪
c1 = rp._mk_cand("x", "t", "趋势", "光伏", [1, 2], [3, 4], 0.9, 90, 30, "可买")
check("_mk_cand: 综合分裁剪<=100 (90+30=120→100)", c1["score"] == 100, c1["score"])
c2 = rp._mk_cand("x", "t", "趋势", "光伏", [1, 2], [3, 4], 0.9, -5, -30, "可买")
check("_mk_cand: 综合分裁剪>=0", c2["score"] == 0, c2["score"])
check("_mk_cand: buy_now/buy_pull 透传", c1.get("buy_now") is None and c1.get("buy_pull") is None)

# ---- 2026-09-05 #490 今日该买什么：环境加权 + 统一排序 ----
print("\n[#490 今日该买什么]")
_w_strong, _n_strong = rp.env_bias({"market": {"sentiment": {"promote_rate": 0.67, "score": 65}},
                                     "micro": {"zhaban_rate": 0.2}})
check("env_bias 强市连板加权1.25", _w_strong["连板"] > 1.2, str(_w_strong))
check("env_bias 强市环境注含接力", "接力" in _n_strong, _n_strong)
_w_cold, _n_cold = rp.env_bias({"market": {"sentiment": {"promote_rate": 0.35, "score": 38}},
                                 "micro": {"zhaban_rate": 0.45}})
check("env_bias 退潮连板降权", _w_cold["连板"] < 0.8 and _w_cold["波段"] > 1.1, str(_w_cold))
_tp_cands = {
    "ladder": [{"code": "1", "name": "连板票", "kind": "连板", "score": 80, "action": "等回踩",
                "buy_price": 10, "target": 11, "upside": 10, "stop": 9.5, "reason": "x"}],
    "trend": [{"code": "2", "name": "趋势票", "kind": "趋势", "score": 70, "action": "现在买",
               "buy_price": 20, "target": 23, "upside": 15, "stop": 19, "reason": "y"}],
    "band": [{"code": "3", "name": "波段票", "kind": "波段", "score": 60, "action": "现在买",
              "buy_price": 5, "target": 6, "upside": 20, "stop": 4.7, "reason": "z"}],
}
_tp = rp.compute_top_picks(_tp_cands, {"market": {"sentiment": {"promote_rate": 0.35, "score": 38}},
                                        "micro": {"zhaban_rate": 0.45}}, topn=3)
check("top_picks 现在买优先于等回踩", _tp["items"][0]["action"] == "现在买", str(_tp["items"][0]))
check("top_picks 退潮环境波段升第一", _tp["items"][0]["kind"] == "波段", _tp["items"][0]["kind"])
check("top_picks 连板退潮降分", _tp["items"][2]["score"] < 60, str(_tp["items"][2]["score"]))
check("top_picks 含环境注", "炸板" in _tp["env_note"], _tp["env_note"])

# ---- #489 自选池云端回传链路（云端侧）----
print("\n[#489 watch_user.json 云端合并]")
import json as _json, tempfile, os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", "pipeline"))
try:
    import watchlist as _wl
    _td = tempfile.mkdtemp(prefix="wluser_")
    _cfg = _os.path.join(_td, "config")
    _os.makedirs(_cfg, exist_ok=True)
    _wl.store.ROOT = _td
    _json.dump({"items": [{"code": "600500", "name": "中化国际", "at": "2026-09-05"}]},
               open(_os.path.join(_cfg, "watch_user.json"), "w", encoding="utf-8"))
    _cs, _ns, _as = _wl.load_watch_codes()
    check("watch_user.json 并入关注池", "600500" in _cs, str(_cs))
    check("watch_user.json 锚点日生效", _as.get("600500") == "2026-09-05", str(_as))
except Exception as _e:
    check("watch_user.json 合并", False, repr(_e))


print("\nPASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
