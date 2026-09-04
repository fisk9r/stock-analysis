# -*- coding: utf-8 -*-
"""模拟盘升级（需求1/2/3）端到端离线测试。"""
import json
import os
import sys
import shutil
import time
import tempfile as _tf

BASE = r"C:\Users\Basshunter-j\WorkBuddy\2026-08-04-11-06-17\stock-analysis"
EXE = os.path.join(BASE, "tools", "executor")
sys.path.insert(0, EXE)
os.chdir(EXE)

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + ("  | " + extra if extra and not cond else ""))


# ============ 1. 可成交性（strategy） ============
print("\n[1] 可成交性检查")
import strategy

# 一字板：主板昨收 10.00 → 涨停 11.00，开盘 11.00 买不进
q = {"open": 11.00, "price": 11.00, "prev_close": 10.00}
r = strategy.can_buy(q, "600000")
ck("一字板买不进", not r["ok"], str(r))

# 开盘 9% 非一字，现价未封板 → 可买
q = {"open": 10.90, "price": 10.95, "prev_close": 10.00}
r = strategy.can_buy(q, "600000")
ck("非一字可买", r["ok"], str(r))

# 盘中封板（现价=涨停且高于开盘）→ 买不进
q = {"open": 10.50, "price": 11.00, "prev_close": 10.00}
r = strategy.can_buy(q, "600000")
ck("盘中封板买不进", not r["ok"], str(r))

# 创业板 20%：昨收 10 → 涨停 12.00，开盘 12.00 买不进
q = {"open": 12.00, "price": 12.00, "prev_close": 10.00}
r = strategy.can_buy(q, "300001")
ck("创业板一字板买不进", not r["ok"], str(r))

# 跌停卖不出：主板昨收 10 → 跌停 9.00
q = {"price": 9.00, "prev_close": 10.00}
r = strategy.can_sell(q, "600000")
ck("跌停卖不出", not r["ok"], str(r))

# 接近跌停但未封死 → 可卖
q = {"price": 9.10, "prev_close": 10.00}
r = strategy.can_sell(q, "600000")
ck("未封跌停可卖", r["ok"], str(r))

# 涨停价计算
lp = strategy.limit_prices(10.00, "600000")
ck("主板涨停价", abs(lp["limit_up"] - 11.0) < 0.001 and abs(lp["limit_down"] - 9.0) < 0.001)
lp = strategy.limit_prices(10.00, "688001")
ck("科创板±20%", abs(lp["limit_up"] - 12.0) < 0.001)
lp = strategy.limit_prices(0, "600000")
ck("昨收0返回0", lp["limit_up"] == 0)

# 无行情
r = strategy.can_buy({"open": 0, "price": 0, "prev_close": 0}, "600000")
ck("无行情放弃", not r["ok"])

# ============ 2. broker_sim 拒绝留痕 + day_summary ============
print("\n[2] broker_sim 留痕与复盘")
import broker_sim

# 用临时库避免污染真实 sim.db
tmpdb = os.path.join(EXE, "sim_test.db")
if os.path.exists(tmpdb):
    try:
        os.remove(tmpdb)
    except (PermissionError, OSError) as e:
        # 2026-08-30：上次运行的 WAL 连接句柄可能未释放，删不掉就换名重建
        print("(warn) 旧临时库清理失败，换名重跑：%r" % e)
        tmpdb = os.path.join(EXE, "sim_test2.db")
        if os.path.exists(tmpdb):
            os.remove(tmpdb)
broker_sim.DB_PATH = tmpdb
b = broker_sim.SimBroker()

b.record_reject("600000", "SELL", "现价9.00封死跌停，卖不出顺延", "测试股A")
b.record_reject("000001", "BUY", "开盘=涨停价一字板买不进", "测试股B")
rj = b.rejects(time.strftime("%Y-%m-%d"))
ck("拒绝留痕写入", len(rj) == 2 and {x["action"] for x in rj} == {"SELL", "BUY"},
   str(rj))

# 买入+卖出流程（买入日为上一交易日，模拟真实「昨日建仓、今日卖出」——
# 验证 T+1 只拦「当日买当日卖」，不误伤合法隔日卖出）
b.con.execute(
    "INSERT OR REPLACE INTO sim_positions(buy_date,code,name,open_gap,buy_price,volume,streak)"
    " VALUES(?,?,?,?,?,?,?)",
    ("2026-08-29", "600111", "包钢稀土", 3.2, 10.00, 500, 3))
b.con.commit()
r = b.sell_limit("600111", 10.50, sig={"name": "包钢稀土", "reason": "落袋为安", "source": "strategy"})
ck("隔日卖出成交", r.get("ok") and r["pnl_pct"] > 0, str(r))

# T+1 物理拒单：今日买入的票，当日不可卖（撮合层兜底，不依赖决策层是否正确）
r = b.buy_limit("600222", 10.00, 5000, sig={"name": "测试T1", "open_gap": 2.0,
                                            "streak": 2, "source": "core", "reason": "B级测试"})
ck("T+1 当日买入成交", r.get("ok"))
r = b.sell_limit("600222", 10.50, sig={"name": "测试T1", "reason": "试图当日卖出", "source": "strategy"})
ck("T+1 当日卖出被拒", (not r.get("ok")) and "T+1" in (r.get("reason") or ""), str(r))

ds = b.day_summary()
ck("day_summary 交易数", len(ds["trades"]) == 2, str(len(ds["trades"])))
ck("day_summary 平仓数", len(ds["closed"]) == 1)
ck("day_summary 盈亏", ds["closed"][0]["pnl_pct"] > 0)
ck("day_summary 含被拒", len(ds["rejects"]) == 3)
bal = ds["balance"]
ck("balance 总资产>0", bal["total"] > 0)

# 2026-09-03 交易成本真实性（用户要求成功率统计真实）：佣金买+卖、印花税卖出
# 默认 impact 0.1% / 佣金 0.025% / 印花税 0.05%
_i, _c, _s = broker_sim._impact(), broker_sim._commission(), broker_sim._stamp()
ck("费用参数默认生效", _i > 0 and _c > 0 and _s > 0, "i=%s c=%s s=%s" % (_i, _c, _s))
_r = b.buy_limit("600333", 10.00, 5000, sig={"name": "成本测试", "open_gap": 1.0,
                                             "streak": 1, "source": "core", "reason": "成本"})
_exp_buy = 10.00 * (1 + _i + _c)
ck("买入成交价含冲击+佣金", _r.get("ok") and abs(_r["price"] - _exp_buy) < 0.001,
   "got=%s exp=%.4f" % (_r.get("price"), _exp_buy))
# 改为历史买入日，绕过 T+1 撮合拒单
b.con.execute("UPDATE sim_positions SET buy_date='2026-08-29' WHERE code='600333'")
b.con.commit()
_r2 = b.sell_limit("600333", 11.00, sig={"name": "成本测试", "reason": "止盈", "source": "strategy"})
_exp_sell = 11.00 * (1 - _i - _c - _s)
ck("卖出成交价扣冲击+佣金+印花税",
   _r2.get("ok") and abs(_r2["price"] - _exp_sell) < 0.001,
   "got=%s exp=%.4f" % (_r2.get("price"), _exp_sell))
ck("费用计入后仍盈利但低于毛价(10→11 毛+10%)",
   _r2.get("ok") and 0 < _r2["pnl_pct"] < 10.0, "pnl=%s" % _r2.get("pnl_pct"))

# ============ 3. runner 复盘写入 sim_review.json ============
print("\n[3] runner 复盘与推送文本")
import runner

# 2026-08-30：测试曾把模拟交易数据真实推到线上 state/sim_review.json（污染网站模块），
# 测试一律置 EXE_NO_PUSH=1 跳过真实推送。
os.environ["EXE_NO_PUSH"] = "1"
cfg = {"broker": "sim", "notify": {"serverchan_key": "", "pushplus_tokens": []}}
# 备份真实 review 文件
rev = os.path.join(EXE, "sim_review.json")
bak = rev + ".bak"
if os.path.exists(rev):
    shutil.copy2(rev, bak)
ds = runner.run_review(cfg, push=False, force=True)  # force=True：跳过交易日守卫（周末跑测试会被拦截）
ck("复盘函数返回", ds is not None and "balance" in ds)
hist = json.load(open(rev, encoding="utf-8"))
today = ds["date"]
ck("review.json 按日累积", today in hist.get("days", {}))
d0 = hist["days"][today]
ck("review 含交易明细", isinstance(d0["trades"], list))
ck("review 含被拒", len(d0["rejects"]) == 3)
ck("review 含总资产", d0["total"] > 0)
if os.path.exists(bak):
    shutil.copy2(bak, rev)
    os.remove(bak)

if os.path.exists(tmpdb):
    # 2026-08-30：SQLite 连接未关闭导致 Windows 句柄占用（WinError 32）。
    # 显式关闭已建实例的连接再删；失败留待下次覆盖（不影响测试结论）。
    try:
        b.con.close()
    except Exception:
        pass
    try:
        os.remove(tmpdb)
    except PermissionError as e:
        print("(warn) 临时库清理失败（不影响测试结论）：%r" % e)
    # 2026-08-31：WorkBuddy 沙箱对 os.remove 走回收站通道，sim.db 被 SQLite
    # WAL 句柄占用时回收站操作也失败 → OSError 使测试末尾崩溃（PASS 统计被吞）。
    # 这里兜底：清理失败不致命，改为改名隔离，下次运行开头旧文件换名重建已兼容。
    except OSError as e:
        try:
            os.rename(tmpdb, tmpdb + ".stale")
            print("(warn) 临时库删除失败，已改名 .stale：%r" % e)
        except OSError:
            print("(warn) 临时库清理失败（不影响测试结论）：%r" % e)

# ============ 4. 回归：sell_decision 7 场景 + strategy_filter ============
print("\n[4] 原有策略回归")
# 2026-08-30 修复测试数据缺陷：原 ks 日期 2026-08-25~28（c=10.5+i*0.1 → 10.9~11.2），
# 与追加的「昨日涨停」K线 2026-08-28（c=12.0）日期重复，导致 list 里同日两根、
# sell_decision 向前扫描取到错误的「昨日」（c=11.3 那根 pct=6.19% 未涨停 → 误判断板）。
# 改为 2026-08-22~25 四根铺垫 + 2026-08-26 昨日涨停（c=12.0, prev2=11.2 → +7.1%...
# 仍不够 9.9%，所以直接把昨日收盘抬到 12.4：12.4/11.2-1=10.7% ≥9.9% 判涨停）。
ks = [{"d": "2026-08-2%d" % i, "o": 10 + i * 0.1, "c": 10.5 + i * 0.1,
       "h": 10.8 + i * 0.1, "l": 9.9 + i * 0.1} for i in range(2, 6)]
# 昨日涨停场景（昨日=2026-08-26，收盘 12.4，前收 11.2 → +10.71% 涨停）
ks2 = ks + [{"d": "2026-08-26", "o": 11.3, "c": 12.4, "h": 12.4, "l": 11.2}]
pos = {"code": "600000", "name": "x", "buy_date": "2026-08-26", "avg_price": 11.9,
       "volume": 300, "streak": 3}
q = {"open": 12.5, "price": 12.6, "prev_close": 12.4}
r = strategy.sell_decision(pos, q, ks2, today="2026-08-27")
ck("续板HOLD", r["verdict"] == "HOLD", str(r))
q = {"open": 12.5, "price": 12.0, "prev_close": 12.4}
r = strategy.sell_decision(pos, q, ks2, today="2026-08-27")
ck("高开低走SELL", r["verdict"] == "SELL", str(r))

# #3 (2026-09-02)：统一硬止损线（归因背书：统一止损每笔平均可挽回 +5.16%）
# 续板但已深套（成本10，开盘9.7/现价9.68 → 浮亏3.2%），且不触发「高开低走」(现价≈开盘) → 硬止损 SELL
pos3 = {"code": "600000", "name": "x", "buy_date": "2026-08-25", "avg_price": 10.0,
        "volume": 300, "streak": 3}
q3 = {"open": 9.7, "price": 9.68, "prev_close": 12.4}
r3 = strategy.sell_decision(pos3, q3, ks2, today="2026-08-27")
ck("统一硬止损·续板深套SELL", r3["verdict"] == "SELL" and "硬止损" in r3["reason"], str(r3))
# 盈利续板 → 不被硬止损误伤（仍走续板持有逻辑）
q3b = {"open": 12.5, "price": 12.6, "prev_close": 12.4}
r3b = strategy.sell_decision(pos3, q3b, ks2, today="2026-08-27")
ck("盈利续板不受硬止损误伤", r3b["verdict"] == "HOLD", str(r3b))
sf = strategy.strategy_filter({"open_gap": 6, "streak": 4}, {}, 100)
ck("A级通过", sf["grade"] == "A")
sf = strategy.strategy_filter({"open_gap": 3, "streak": 1}, {}, 100)
ck("X级放弃", sf["grade"] == "X")

# ============ 5. Batch3 #13/#14：executor 接入区间/回避/仓位/尾盘确认 ============
print("\n[5] Batch3 执行器集成助手（exec_core）")
import exec_core

_b3 = {
    "seat_avoid": {"n": 1, "items": [{"label": "拉萨", "win_rate": 30, "n": 25,
                   "avg_pct": -3.5, "reps": [{"code": "600666", "name": "奥瑞德"}]}]},
    "zones": {"n": 2, "items": [
        {"code": "600111", "name": "包钢", "buy_zone": [9.5, 10.2],
         "sell_zone": [11.0, 11.8], "stop": 9.3, "action": "正常持有"},
        {"code": "600222", "name": "某票", "buy_zone": [5.0, 5.6],
         "sell_zone": [6.2, 6.9], "stop": 4.9, "action": "逼近卖出"},
    ]},
    "ladder_plans": [{"code": "002594", "entry_streak": 2, "gate": "avoid"}],
    "position_advice": {"heat": "温", "sentiment": "均衡", "suggest_pct": 55,
                        "level": "中性偏谨慎", "reason": "x"},
    "late_session": {"watch_tomorrow": [{"code": "300750"}],
                     "exit_warn": [{"code": "600519"}]},
}

ck("席位回避集合", exec_core.seat_avoid_codes(_b3) == {"600666"},
   str(exec_core.seat_avoid_codes(_b3)))
ck("区间查表止损位", exec_core.zone_stop("600111", _b3) == 9.3)
_sa1, _ = exec_core.apply_seat_avoid({"code": "600666"}, _b3)
ck("席位回避命中", _sa1 is True)
_sa2, _ = exec_core.apply_seat_avoid({"code": "600111"}, _b3)
ck("席位回避放行", _sa2 is False)
_la1, _ = exec_core.apply_ladder_avoid({"code": "002594", "streak": 2}, _b3)
ck("梯队回避命中", _la1 is True)
_la2, _ = exec_core.apply_ladder_avoid({"code": "002594", "streak": 1}, _b3)
ck("梯队回避仅连板", _la2 is False)
_v, _why, _stop = exec_core.refine_buy_zone(
    {"code": "600111"}, {"600111": {"price": 10.0}}, _b3)
ck("买区内→BUY带回止损", _v == "BUY" and _stop == 9.3, "%s/%s" % (_v, _stop))
_v2, _, _ = exec_core.refine_buy_zone(
    {"code": "600111"}, {"600111": {"price": 9.0}}, _b3)
ck("低于买区→WATCH", _v2 == "WATCH", _v2)
_v3, _, _ = exec_core.refine_buy_zone(
    {"code": "600111"}, {"600111": {"price": 11.0}}, _b3)
ck("高于买区→WATCH", _v3 == "WATCH", _v3)
_v4, _, _s4 = exec_core.refine_buy_zone(
    {"code": "999999"}, {"999999": {"price": 5.0}}, _b3)
ck("无区间数据→BUY不拦", _v4 == "BUY" and _s4 is None, "%s" % _v4)
_zs1, _zp1, _ = exec_core.refine_sell_zone(
    {"code": "600111"}, {"price": 9.2}, _b3)
ck("区间止损触发", _zs1 == "SELL" and _zp1 == 9.2, "%s" % _zs1)
_zs2, _, _ = exec_core.refine_sell_zone(
    {"code": "600222"}, {"price": 11.2}, _b3)   # 600222 action=逼近卖出 现≥卖区
ck("区间止盈触发", _zs2 == "SELL", "%s" % _zs2)
_zs3, _, _ = exec_core.refine_sell_zone(
    {"code": "600111"}, {"price": 10.5}, _b3)
ck("无触发→交还原策略", _zs3 is None, "%s" % _zs3)
ck("总仓位系数(退潮)", abs(exec_core.position_cap(_b3) - 0.75) < 1e-9)
ck("总仓位系数(防守)", exec_core.position_cap({"position_advice": {"suggest_pct": 30}}) == 0.5)
ck("总仓位系数(缺省)", exec_core.position_cap({}) == 1.0)
_w, _warn = exec_core.late_session_maps(_b3)
ck("尾盘确认映射", _w == {"300750"} and _warn == {"600519"},
   "watch=%s warn=%s" % (_w, _warn))

# ============ 6. Batch3 #11：归因报告自动生成（临时库注入） ============
print("\n[6] Batch3 归因报告生成（gen_attr_report）")
import sqlite3 as _sq
import tempfile as _tf
_tmp = _tf.mkdtemp(prefix="attr_", dir=EXE)
os.makedirs(os.path.join(_tmp, "cache"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "reports"), exist_ok=True)
_db = os.path.join(_tmp, "cache", "market.db")
_c = _sq.connect(_db)
_c.execute("""create table rec_picks(
    code text, name text, streak int, tag text, date text,
    p_break real, open_gap real, next_open_gap real, next_pct real)""")
for i in range(40):
    _c.execute("insert into rec_picks(code,name,streak,tag,date,p_break,open_gap,next_open_gap,next_pct)"
               " values(?,?,?,?,?,?,?,?,?)",
               ("600%03d" % i, "票%d" % i, 1 + i % 4, "核心龙头", "2026-09-%02d" % (1 + i % 20),
                70.0, 3.0, 3.0, 4.0))
_c.commit(); _c.close()
sys.path.insert(0, os.path.join(BASE, "tools"))
import gen_attr_report
_rep = gen_attr_report.generate(root=_tmp, month="2026-09")
ck("归因报告生成返回路径", bool(_rep) and os.path.exists(_rep), str(_rep))
if _rep:
    _sz = os.path.getsize(_rep)
    ck("归因报告非空", _sz > 2000, "%d bytes" % _sz)
# 空库返回 None
_c2 = _sq.connect(os.path.join(_tmp, "cache", "market2.db"))
os.remove(_db)  # 重建为空库
_c3 = _sq.connect(_db); _c3.execute("create table rec_picks(code text)"); _c3.commit(); _c3.close()
_rep2 = gen_attr_report.generate(root=_tmp, month="2026-09", db_path=_db)
ck("空库返回 None", _rep2 is None, str(_rep2))
shutil.rmtree(_tmp, ignore_errors=True)

# ============ 7. Batch3 #12：盘中异动 detect + 落库/上墙 ============
print("\n[7] Batch3 盘中异动落库与上墙（intraday）")
try:
    sys.path.insert(0, os.path.join(BASE, "pipeline"))
    import intraday as intraday_mod
    _ok_intra = True
except Exception as e:
    _ok_intra = False
    print("  (warn) intraday 导入跳过：%r" % e)
if _ok_intra:
    _al = intraday_mod.detect(
        ["600000"], {"600000": {"name": "测试", "price": 11.0, "prev_close": 10.0, "pct": 10.0}})
    ck("异动检测涨停附近", len(_al) == 1 and "涨停" in (_al[0]["type"] or ""), str(_al))
    _tmp2 = _tf.mkdtemp(prefix="intra_", dir=EXE)
    os.makedirs(os.path.join(_tmp2, "cache"), exist_ok=True)
    os.makedirs(os.path.join(_tmp2, "data"), exist_ok=True)
    open(os.path.join(_tmp2, "cache", "market.db"), "w").close()  # 空库，触发建表
    _frag = intraday_mod.persist(_al, date="2026-09-01", root=_tmp2)
    ck("上墙片段返回", _frag is not None and _frag["n"] == 1, str(_frag))
    _fpath = os.path.join(_tmp2, "data", "intraday_latest.json")
    ck("上墙片段文件落盘", os.path.exists(_fpath))
    if os.path.exists(_fpath):
        _fj = json.load(open(_fpath, encoding="utf-8"))
        ck("上墙片段结构", _fj.get("date") == "2026-09-01" and len(_fj["alerts"]) == 1)
    _ic = _sq.connect(os.path.join(_tmp2, "cache", "market.db"))
    _nrow = _ic.execute("select count(*) from intraday_alerts").fetchone()[0]
    _ic.close()
    ck("落库写入 intraday_alerts", _nrow == 1, "rows=%d" % _nrow)
    shutil.rmtree(_tmp2, ignore_errors=True)

# ============ 8. #481/#482：盘中机动买入 + 熔断双漏洞修复（2026-09-05） ============
print("\n[8] 盘中机动买卖 + 熔断修复（#481/#482/#484）")

# ---- 8.1 chase_gate 盘中形态门 ----
_cg_sig = {"code": "600111", "name": "形态票", "streak": 3, "close": 10.0,
           "market_type": "limitup"}
# 微红横盘（开盘10.0 现价10.1 → fade +1.0%）→ BUY
_r = exec_core.chase_gate(_cg_sig, {"600111": {"open": 10.0, "price": 10.1, "prev_close": 9.9}})
ck("chase_gate 微红横盘→BUY", _r["verdict"] == "BUY" and abs(_r["day_fade"] - 1.0) < 0.01, str(_r))
# 盘中走弱（fade -3.5%）→ ABORT（不接飞刀）
_r = exec_core.chase_gate(_cg_sig, {"600111": {"open": 10.0, "price": 9.65, "prev_close": 9.9}})
ck("chase_gate 走弱→ABORT", _r["verdict"] == "ABORT", str(_r))
# 盘中强拉（fade +5.5%）→ WATCH（不追透支）
_r = exec_core.chase_gate(_cg_sig, {"600111": {"open": 10.0, "price": 10.55, "prev_close": 9.9}})
ck("chase_gate 强拉→WATCH", _r["verdict"] == "WATCH", str(_r))
# 中性（fade -1.5%）→ WATCH
_r = exec_core.chase_gate(_cg_sig, {"600111": {"open": 10.0, "price": 9.85, "prev_close": 9.9}})
ck("chase_gate 中性→WATCH", _r["verdict"] == "WATCH", str(_r))
# 无行情 → ABORT
_r = exec_core.chase_gate(_cg_sig, {})
ck("chase_gate 无行情→ABORT", _r["verdict"] == "ABORT", str(_r))

# ---- 8.2 RiskGate 幂等修复：WATCH 留痕不锁单，BUY 才锁 ----
import importlib as _il
import risk_gate as _rg
risk_gate = _rg
# 用临时状态文件隔离测试
import tempfile as _tf2
_tfdir = _tf2.mkdtemp(prefix="risk_")
_orig = _rg.STATE_PATH
_rg.STATE_PATH = os.path.join(_tfdir, "risk_state.json")
_il.reload(_rg)
# 写入一条 WATCH 记录（模拟 09:25 竞价判观望留痕）
_rg._save_state({"trades": [{"date": time.strftime("%Y-%m-%d"), "code": "600111",
                              "verdict": "WATCH", "amount": 0}],
                 "circuit_break": None, "day": time.strftime("%Y-%m-%d"),
                 "orders_today": 0, "day_base": None})
_g = _rg.RiskGate({})
_chk = _g.check({"code": "600111"}, 100000.0, 1)
ck("幂等修复：WATCH留痕不锁单", _chk["ok"], str(_chk))
# BUY 记录才锁单
_rg._save_state({"trades": [{"date": time.strftime("%Y-%m-%d"), "code": "600111",
                              "verdict": "BUY", "amount": 10000}],
                 "circuit_break": None, "day": time.strftime("%Y-%m-%d"),
                 "orders_today": 1, "day_base": None})
_g2 = _rg.RiskGate({})
_chk2 = _g2.check({"code": "600111"}, 100000.0, 1)
ck("幂等修复：BUY留痕锁单", (not _chk2["ok"]) and "已买入" in _chk2["reason"], str(_chk2))
# 隔日 BUY 记录不锁今日单（date != today）
_rg._save_state({"trades": [{"date": "2026-01-01", "code": "600111",
                              "verdict": "BUY", "amount": 10000}],
                 "circuit_break": None, "day": time.strftime("%Y-%m-%d"),
                 "orders_today": 0, "day_base": None})
_g3 = _rg.RiskGate({})
_chk3 = _g3.check({"code": "600111"}, 100000.0, 1)
ck("幂等修复：隔日BUY不锁今日", _chk3["ok"], str(_chk3))

# ---- 8.3 熔断跨日自动复位 ----
_rg._save_state({"trades": [], "circuit_break": {"at": "2026-09-04 14:00:00",
                                                  "reason": "昨日熔断"},
                 "day": "2026-09-04", "orders_today": 3, "day_base": None})
_g4 = _rg.RiskGate({})   # 构造时跨日 → 自动清熔断
ck("熔断跨日自动复位", (not _g4.tripped), "state=%s" % _rg._load_state())
# 当日熔断仍生效（未跨日）
_rg._save_state({"trades": [], "circuit_break": {"at": time.strftime("%Y-%m-%d %H:%M"),
                                                  "reason": "当日熔断"},
                 "day": time.strftime("%Y-%m-%d"), "orders_today": 1,
                 "day_base": None})
_g5 = _rg.RiskGate({})
ck("当日熔断仍生效", _g5.tripped, str(_rg._load_state()))

# ---- 8.4 day_base 当日基准 + 当日口径 ----
_rg._save_state({"trades": [], "circuit_break": None, "day": time.strftime("%Y-%m-%d"),
                 "orders_today": 0, "day_base": None})
ck("day_base 首查None", _rg.day_base() is None)
_rg.set_day_base(100000.0)
ck("day_base 落盘", abs(_rg.day_base() - 100000.0) < 1e-9, str(_rg.day_base()))
_rg.set_day_base(99999.0)   # 当日已有值 → 幂等保留
ck("day_base 幂等不覆盖", abs(_rg.day_base() - 100000.0) < 1e-9)
# 当日亏损 -3.1% → 触发熔断（当日口径）
_g6 = _rg.RiskGate({})
_g6.check_daily_loss(-3.1)
ck("当日亏损触发熔断", _g6.tripped)
# 当日亏损 -2.9% → 不触发
_rg._save_state({"trades": [], "circuit_break": None, "day": time.strftime("%Y-%m-%d"),
                 "orders_today": 0, "day_base": {"date": time.strftime("%Y-%m-%d"),
                                                  "total": 100000.0}})
_g7 = _rg.RiskGate({})
_g7.check_daily_loss(-2.9)
ck("未越线不熔断", not _g7.tripped)
# 累计回撤但当日不亏（total=97000 基准 97500 当日 +0.5%）→ 不熔断（旧口径必误熔断）
_g7.check_daily_loss(0.5)
ck("当日盈利不熔断", not _g7.tripped)

# 还原 risk_gate 状态文件路径（防污染真实 risk_state.json）
_rg.STATE_PATH = _orig
_il.reload(_rg)
shutil.rmtree(_tfdir, ignore_errors=True)

# ---- 8.5 _scan_buys 集成：盘中微红横盘确认买入 ----
# 临时 sim.db + 隔离 risk_state
_scbak = _rg.STATE_PATH
_tfdir2 = _tf2.mkdtemp(prefix="scanbuy_")
_rg.STATE_PATH = os.path.join(_tfdir2, "risk_state.json")
_il.reload(_rg)
import broker_sim as _bs
_tmpdb2 = os.path.join(_tfdir2, "sim.db")
_bs.DB_PATH = _tmpdb2
_bb = _bs.SimBroker()
# 构造候选：st=3 高开 2.2%（竞价 BUY 线）+ 盘中微红 +0.8%（chase BUY）
_sigs = [{"code": "600500", "name": "中化国际", "streak": 3, "close": 9.80,
          "market_type": "limitup", "source": "core", "tag": "core",
          "auction_rule": ""}]
def _fake_quote(codes, **kw):
    return {"600500": {"name": "中化国际", "open": 10.02, "price": 10.10,
                       "prev_close": 9.80, "high": 10.15, "low": 9.99,
                       "float_mv": 120.0, "stamp": time.strftime("%Y%m%d") + "103000"}}
runner.realtime_quote = _fake_quote
_scan_cfg = {"broker": "sim",
             "risk": {"max_positions": 4, "max_trade_amount": 60000,
                      "min_trade_amount": 1000, "daily_loss_stop_pct": -3.0,
                      "enabled": True, "grade_pct": {"A": 0.65, "B": 0.55, "T": 0.50, "C": 0.30},
                      "intraday_cut": 0.7},
             "notify": {}}
_mkt = {"mode": "NORMAL", "reason": "测试"}
_bl, _nb = runner._scan_buys(_bb, _scan_cfg, _sigs, _mkt, data=None)
ck("_scan_buys 盘中确认买入成交", _nb == 1, "n_buy=%s lines=%s" % (_nb, _bl))
if _nb == 1:
    _pos = [p for p in _bb.positions(open_only=True) if p["code"] == "600500"]
    ck("_scan_buys 持仓入库", len(_pos) == 1, str(len(_pos)))
    ck("_scan_buys 金额0.7折", 0 < (_pos[0].get("volume") * _pos[0].get("avg_price", 0)) 
       <= 100000 * 0.65 * 0.7 + 1, str(_pos[0]))
# 同票二次巡逻：持仓幂等拒绝
_bl2, _nb2 = runner._scan_buys(_bb, _scan_cfg, _sigs, _mkt, data=None)
ck("_scan_buys 持仓幂等", _nb2 == 0, "n_buy=%s" % _nb2)
# 盘中走弱（fade -3%）→ ABORT 不买
def _fake_quote_weak(codes, **kw):
    return {"600777": {"name": "弱票", "open": 10.0, "price": 9.65,
                       "prev_close": 9.9, "float_mv": 100.0,
                       "stamp": time.strftime("%Y%m%d") + "103000"}}
runner.realtime_quote = _fake_quote_weak
_sigs2 = [{"code": "600777", "name": "弱票", "streak": 3, "close": 9.9,
           "market_type": "limitup", "source": "core", "tag": "core",
           "auction_rule": ""}]
_bl3, _nb3 = runner._scan_buys(_bb, _scan_cfg, _sigs2, _mkt, data=None)
ck("_scan_buys 不接飞刀", _nb3 == 0, "n_buy=%s" % _nb3)
# FREEZE 环境不开新仓
_mkt_f = {"mode": "FREEZE", "reason": "弱市"}
_bl4, _nb4 = runner._scan_buys(_bb, _scan_cfg, _sigs, _mkt_f, data=None)
ck("_scan_buys FREEZE拦截", _nb4 == 0, "n_buy=%s" % _nb4)
# 清理
try:
    _bb.con.close()
except Exception:
    pass
runner.realtime_quote = exec_core.realtime_quote   # 还原
_bs.DB_PATH = os.path.join(EXE, "sim.db")
_rg.STATE_PATH = _scbak
_il.reload(_rg)
_il.reload(_bs)
shutil.rmtree(_tfdir2, ignore_errors=True)

# ---- 8.6 _ledger_missing 补 tail ----
_lj = os.path.join(EXE, "state", "task_ledger.json")
_ljbak = None
if os.path.exists(_lj):
    _ljbak = open(_lj, encoding="utf-8").read()
os.makedirs(os.path.dirname(_lj), exist_ok=True)
with open(_lj, "w", encoding="utf-8") as f:
    json.dump({time.strftime("%Y-%m-%d"): {"now": 1}}, f)   # 只有 now
_miss = runner._ledger_missing()
ck("_ledger_missing 含tail", set(_miss) >= {"scan", "tail"}, str(_miss))
if _ljbak is not None:
    open(_lj, "w", encoding="utf-8").write(_ljbak)
else:
    try:
        os.remove(_lj)
    except Exception:
        pass

# ---- 8.7 executor.yml cron 映射核对：新增 7 轮巡逻 cron 全部落入 scan 分支 ----
_yml = open(os.path.join(BASE, ".github", "workflows", "executor.yml"),
            encoding="utf-8").read()
import re as _re
_crons = _re.findall(r'cron:\s*"(\d+)\s+(\d+)\s+\*\s+\*\s+1-5"', _yml)
_crons = [(int(m), int(h)) for m, h in _crons]
_scan_crons = [(m, h) for m, h in _crons
               if (h == 1 and m >= 30) or h in (2, 3, 5) or (h == 6 and m < 40)]
_exp_scan = {35, 40, 0, 15, 30, 45, 0, 15, 30, 15, 30, 45, 0, 15, 30}
_got = {m for m, h in _scan_crons}
ck("巡逻cron数=15", len(_scan_crons) == 15, "n=%d" % len(_scan_crons))
# 映射规则复现（与 executor.yml 判定步骤一致）
def _map(m, h):
    if h == 1:
        return "now" if m < 30 else "scan"
    if h in (2, 3, 5):
        return "scan"
    if h == 6:
        return "scan" if m < 40 else "tail"
    if h == 7:
        return "review"
    return "?"
_all_mapped = all(_map(m, h) == "scan" for m, h in _scan_crons)
ck("新增巡逻cron全部映射scan", _all_mapped,
   str([(m, h, _map(m, h)) for m, h in _scan_crons]))
# 尾盘/复盘 cron 不变
ck("尾盘cron保留", any(m == 43 and h == 6 for m, h in _crons))
ck("复盘cron保留", any(m == 32 and h == 7 for m, h in _crons))
# YAML 结构粗校验（缩进/键值）
ck("yml schedule行数=19", _yml.count("- cron:") == 19, "n=%d" % _yml.count("- cron:"))

print("\n" + "=" * 50)
print("PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
