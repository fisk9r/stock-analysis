# -*- coding: utf-8 -*-
"""模拟盘升级（需求1/2/3）端到端离线测试。"""
import json
import os
import sys
import shutil
import time

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
sf = strategy.strategy_filter({"open_gap": 6, "streak": 4}, {}, 100)
ck("A级通过", sf["grade"] == "A")
sf = strategy.strategy_filter({"open_gap": 3, "streak": 1}, {}, 100)
ck("X级放弃", sf["grade"] == "X")

print("\n" + "=" * 50)
print("PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
