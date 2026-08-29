# stock-analysis 本地执行器（模拟盘 + miniQMT 实盘）

从线上站点拉最新推荐票 → 竞价决策线裁决 → 过风控闸门 → 下单（模拟/实盘）。

## 文件说明

| 文件 | 作用 |
|---|---|
| `runner.py` | 主入口：`--now` 立即执行 / `--loop` 每天 09:26 自动 / `--summary` 战绩 |
| `exec_core.py` | 线上数据解密（PBKDF2+XOR）+ 信号提取 + 腾讯实时行情 + 决策线裁决 |
| `risk_gate.py` | 风控闸门：单票仓位上限/单日委托数/幂等去重/亏损熔断（持久化，重启不复位） |
| `broker_sim.py` | 模拟盘：本地 SQLite 成交记录（开盘价+0.1% 冲击成本） |
| `broker_qmt.py` | miniQMT 实盘适配（xtquant），账户信息在 config.json 填写 |
| `config.json` | 全部配置：账户口令/broker 切换/风控参数/推送 key |
| `sim.db` | 模拟盘数据库（自动生成） |
| `risk_state.json` | 风控状态（自动生成，含熔断标记；删除即复位） |

## 快速开始（模拟盘）

```bash
cd tools/executor
python runner.py --now      # 立即执行一轮（测试）
python runner.py --summary  # 查看模拟盘战绩
python runner.py --loop     # 常驻：每天 09:26 自动执行（挂机）
```

模拟盘默认 10 万初始资金、单票 ≤1.5 万、每天最多 6 笔——都在 `config.json` 的 `risk` 段调。

## 切到 miniQMT 实盘（权限开通后）

1. 券商开通 miniQMT（国金/华西 10 万日均即可，APP 内申请，1-3 天审核）
2. 下载 QMT 客户端登录，**勾选「独立交易/极简模式」**
3. `pip install xtquant`
4. 填 `config.json` 的 `qmt` 段：
   ```json
   "qmt": {
     "qmt_path": "D:\\你的QMT目录\\userdata_mini",
     "account_id": "资金账号"
   }
   ```
5. `"broker": "qmt"` 切换。建议先保持 `risk.enabled=true`、`max_trade_amount` 小额（如 5000）试跑一周。

## 决策线（执行核心逻辑）

| 次日开盘 | 动作 | 回测依据（13 个月方向 100% 一致） |
|---|---|---|
| 高开 ≥2% | 买入 | 全量推荐票实测：胜率 67.4% / +4.08% |
| 低开 ≤-2% | 放弃 | 胜率仅 26.5% / -3.04% |
| 平开 ±2% | 观望 | 无方向 |

st≥3 高度票：高开≥2% 积极跟进（高度票次日胜率 64%~82%）。

## 安全设计

- **闸门先于下单**：broker 收到的指令 100% 已过风控
- **熔断持久化**：单日委托超限/亏损超线 → 自动熔断，**需人工删除 `risk_state.json` 中的 `circuit_break` 才恢复**
- **幂等**：同票同日只委托一次，重复信号直接拒绝
- **审计**：每条信号（含拒绝）都记录在 risk_state.json / qmt_orders.jsonl
