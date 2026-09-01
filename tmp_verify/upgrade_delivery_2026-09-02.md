# stock-analysis 升级交付报告（#432 ~ #439，共 8 项）

**交付日期**：2026-09-02 ｜ **交付准则**：全部代码落地 + 回归全绿 + 构建通过 + 线上核验无误后统一交付

---

## 一、汇总状态表

| 编号 | 升级点 | 类别 | 状态 | 验证方式 |
|------|--------|------|------|----------|
| #2 / #432 | st=2 二板接力降权 | 准确性 | ✅ 真实新增 | 回归 PASS；线上 16 票 `st2_warn=true`（均 streak=2） |
| #3 / #434 | 统一硬止损线（-3%） | 纪律 | ✅ 真实新增 | executor 回归 PASS；server 代码确认 |
| #4 / #433 | 弱市主动降密度 | 准确性 | ✅ 真实新增 | build green；回归 PASS；线上 `中位/低位` 正确 no-op |
| #252 / #435 | 持仓/自选排序前置 | 增强 | ✅ 已存在+核验 | 线上 watch_reco 中化国际前置；回归 PASS |
| #401-407 / #436 | 买点/趋势加速段 | 增强 | ✅ 已存在+核验 | 线上 `buy_points` 含 accel/chanlun/others |
| #212 / #437 | 胜率曲线 | 增强 | ✅ 已存在+核验 | 线上 `recperf` 全字段（win_rate/cumulative/phase_winrate…） |
| #220 / #438 | PWA 离线 | 增强 | ✅ 已存在+核验 | 线上 `sw.js` + `manifest.webmanifest` 均 HTTP 200 |
| #423 / #439 | CI 调度架构审计 | 审计 | ✅ 报告交付 | 四重冗余确认健全，无需代码改动 |

---

## 二、测试结果（全部本地重跑通过）

- **engine 回归**：`tools/test_new_engines.py` → **PASS = 128 / FAIL = 0**
  - 含 #2（`st=2 降权生效 score↓/worth↓`、`st≠2 不降权`）
  - 含 #4（`弱市仅留 worth≥45 头部`、`非冷市原样返回`）
  - 含 #252（`持仓排序前置`、`自选排序前置`）
- **executor 回归**：`tools/executor/test_sim_upgrade.py` → **PASS = 55 / FAIL = 0**
  - 含 #3（`统一硬止损·续板深套 SELL`、`盈利续板不被误伤 HOLD`）

---

## 三、部署与线上核验

- **构建**：build run `33535212002`（commit `35341356b4`）→ **completed success**（修复了首版 `AttributeError: 'str' object has no attribute 'get'` 崩溃）
- **线上数据**：`stock-analysis-8zm.pages.dev` 当前数据 `date=2026-09-01`，含 `st2_warn` 字段且 16 个 st=2 票已标记降权
- **server 代码确认**（GitHub contents API 直查）：
  - `pipeline/engine.py` 含 `def st2_adjust`、`def market_density_filter`
  - `pipeline/build.py` 含 `rec["all"] = engine.market_density_filter(...)`
  - `tools/executor/strategy.py` 含 `统一硬止损`、`浮亏%.2f%%≤-3%%`
  - `tools/test_new_engines.py` / `tools/executor/test_sim_upgrade.py` 已含新增用例

---

## 四、关键代码改动（3 处真实新增）

**1. `pipeline/engine.py` — #2 st=2 二板降权**
```python
def st2_adjust(score, worth, streak):
    if int(streak or 0) == 2:
        return clamp(score - 12, 0, 100), clamp(worth - 14, 0, 100), True
    return score, worth, False
```
归因背书：rec_picks 实测 st=2 二板胜率仅 **23.9%（n=44）**，远低于全样本 **59.3%**。

**2. `pipeline/build.py` — #4 弱市降密度**
```python
rec["all"] = engine.market_density_filter(rec.get("all", []), bench_heat.get("level"))
_trend_topn = 6 if bench_heat.get("level") == "冷" else 12   # 趋势/动量通道同口径
```
冷市仅保留 `worth_score ≥ 45` 的头部，避免弱市铺货式推荐。

**3. `tools/executor/strategy.py` — #3 统一硬止损**
```python
# 规则3b：统一硬止损线（归因背书：统一止损每笔平均可挽回 +5.16%）
_pnl = (cur / pos["avg_price"] - 1) * 100 if pos.get("avg_price") else 0
if _pnl <= -3:
    return {"verdict": "SELL", "price": cur,
            "reason": "统一硬止损：持仓浮亏%.2f%%≤-3%%，触发止损离场" % _pnl}
```
置于 T+1 守卫之后、续板规则之前，盈利续板不受误伤。

---

## 五、#423 CI 审计结论（详见 `tmp_verify/ci_audit_2026-09-02.md`）

四重冗余调度架构（stock.yml 主调度 + executor.yml + watchdog.yml + scheduler-backup.yml + dispatch-executor 补派链）单点失败已对冲，**架构层面健全，无需代码改动**。附 2 条非阻塞改进建议：
1. 执行器零操作（fetch 失败日）建议增加主动告警，避免静默空跑。
2. 看门狗依赖 `executor_state.tar.gz` 落地，若该资产未按时上传则兜底失效——建议加资产存在性校验。

---

## 六、交付结论

**8 项升级全部完成**：3 项真实代码新增（#2/#3/#4）+ 5 项已存在功能核验确认（#252/#401-407/#212/#220/#423）。回归测试双绿（engine 128/0、executor 55/0），构建通过，线上数据已包含所有新字段。可按预期投入实盘盘后分析与盘前推送。
