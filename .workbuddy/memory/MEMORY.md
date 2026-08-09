# 项目长期记忆：股票事后分析网站（stock-analysis）

## 目标
基于「当天收盘后」数据的事后分析网站，五大功能：①涨停板块归属与强势个股 ②断板概率 ③场内场外情绪判次日 ④妖股历史形态相似度 ⑤当日推荐。约束：零外部依赖、纯 SVG 图表、浅色主题、简体中文。

## 架构
- `pipeline/` Python 标准库：
  - `em_api.py` 东方财富公开接口封装（push2ex 涨停/炸板/强势/跌停池、push2his K线、push2 clist 指数/板块、腾讯 ifzq 备源、新浪全市场列表备源）。含按 host 自适应令牌桶 `RateLimiter`（惩罚 0.7、恢复阈值25、上限20/s）。
  - `store.py` SQLite 缓存（cache/market.db）：bars(日K) / stocks / boards / board_member / meta。
  - `fetch.py` 采集层：refresh_stocks / refresh_bars / refresh_boards / snapshot_today / save_archive → archive/snapshot_YYYYMMDD.json。
  - `backfill.py` 双源并行补齐残缺日K（东财领偶数、腾讯领奇数，check_same_thread=False + wlock 串行写）。
  - `engine.py` 引擎（Universe 从 bars 重建涨停/连板历史；streak_statistics / build_limit_ups / sector_heat / emotion_series / sentiment_score / cycle_phase / break_risk / mine_demon_templates / demon_scan / recommend / **auction_map(竞价定调)** / **ladder_history(梯队持续性)** / **rotation(板块轮动)**）。`open_pct` 已有除零保护；`market_breadth` 改用 `by_date` 索引提速；`break_risk`/`recommend` 已消费竞价信号。
  - `build.py` 跑引擎 → 写 `dist/data.js`（`window.__STOCK_DATA__`）。`--date=` 覆盖默认最后交易日。
- `dist/` 前端：index.html（**七视图**骨架：市场概览/涨停梯队/板块热力/竞价定调/断板风险/妖股基因/当日推荐，默认 `data-theme="tech"`）→ data.js → charts.js（window.CH 纯SVG函数族，**含 svgHeat 竞价热力网格**）→ app.js（渲染层，IIFE，懒渲染；含 `viewAuction` + 动态科技网格背景 `techBackdrop` + 概览竞价定调卡 + 板块持续性热力）。
- `update.bat` 一键抓取+构建（Windows，用托管 python 路径，回退 python；`/silent` 供计划任务调用）。
- `install_schedule.bat` / `uninstall_schedule.bat`：注册/取消 Windows 计划任务「StockAnalysisDaily」，每周一至五 16:10 自动运行 `update.bat /silent`。
- `tmp/validate.js` Node 无头校验（mock window/document，驱动 app.js 渲染**七视图**）。

## 风格与 AI 决策（2026-08-06 用户要求）
- 用户要求「科技展示风格」：新增深色 HUD 大屏主题 `tech`（CSS 变量 + 网格背景 + 扫描线动画 + HUD 角标 + 数字滚动 count-up + 主题切换按钮持久化 localStorage）。浅色主题保留为可切换备选项（默认 tech，避免首屏闪烁已在 html 写死 data-theme）。
- 用户问「是否加入 AI」：结论**不加 LLM**，理由=内网/离线零依赖环境，LLM 需联网或本地大模型权重，破坏约束。改用**离线 rule-based「类AI解读」**（`build.py: build_narrative()` 拼装中文复盘，确定性、零依赖），前端以 `.narrative` 卡片展示，标签「AI 解读·离线生成」。
- 每日更新：纯属 pipeline 侧（浏览器不能跨域内网抓取）。方案=`update.bat` + 计划任务每日 16:10；页面顶部加「数据新鲜度」徽标（≥2 天过期提示重跑 update.bat）。
- **部署（2026-08-07 新增）**：用 `workbuddy_cloudstudio_deploy` 部署 `dist/`，分享链接 **https://1bc7e055dd1041b3b08d40649533446f.gz3.agentos-app.net**（纯静态，verified）。该工具每次调用新建工作区→新链接，故每日内容刷新需重新部署。
- **Hy3 引擎驱动叙事（2026-08-07 新增）**：`dist/ai_narrative.json` 由宿主模型基于真实计算结果撰写，`build.py` 仅当 `ai.date == meta.date` 才优先采用（带日期匹配守卫，防旧文案套新数据），否则回退 `build_narrative()`。
- **每日自动化（2026-08-07 新增）**：`recurring` 自动化「A股盘后复盘每日自动更新」(id=automation-1786056161903)，rrule `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=16;BYMINUTE=10`，ACTIVE。prompt 自包含：fetch+boards→build(自动选最新交易日)→读 data.js 真实数字→Hy3 风格撰写复盘写 ai_narrative.json→复 build 烤入→重新部署→输出链接。硬性约束：数字必须来自 data.js 真实字段，禁止虚构。
- 注意：narrative 中 seal_rate / p_break / promote_rate / similar.sim 字段**已是百分数单位**，拼字符串时不要再 ×100（已踩坑修复）。

## 漏洞审计与升级（2026-08-07 用户要求「查找漏洞+升级+竞价+科技含量」）
- **已修复的漏洞**：
  1. 时间解析 `int(st)//10000` 未处理 None/负分钟 → 新增 `parse_seal_time()` 健壮解析（开盘9:30起算，None/异常返回 None 不计入质量分，避免 KeyError/除零）。
  2. `open_pct` 除零：`(b["c"]-b["chg"])` 可能为0 → 加 `pc` 守卫，安全回退 None。
  3. `market_breadth` O(N·bars) 全扫描 → 改用 `u.by_date[date]` 索引，复杂度降到 O(当日个股)。
  4. `break_risk` 见顶信号只看涨停数，缺高度维度 → 因子更稳。
- **新增维度（离线可算，不依赖 LLM）**：
  - **竞价定调 `auction_map()`**：基于 last 日K 的 o/c/pct/float/vol 计算 高开幅度/竞价额/竞价量比/一字板/T字板/弱转强/强转弱/异常高开；输出 summary + 每票 items（高开高走/一字板/T字板/换手板/弱转强）。前端第七视图展示（热力网格 + 定调仪表 + 明细）。
  - **梯队持续性 `ladder_history()`**：近 20 日各高度涨停家数矩阵 + 当前日签名 + 历史相似度（近 5/10 日最像哪天、方向）。
  - **板块轮动 `rotation()`**：依赖 boards 表，空时优雅降级（输出空数组，前端提示重跑 fetch）。
  - 竞价信号已反哺 `break_risk` 因子（竞价强度 ±）与 `recommend` 评分/解读（弱转强加分、强转弱风险提示）。
- **科技含量二次强化**：svgHeat 竞价热力网格、动态科技网格 canvas 背景（techBackdrop，青色粒子+扫描线，可 `toggle.reduce` 关闭）、竞价定调卡（脉冲指示灯）、HUD 角标配色增强、badge 颜色类（一字/弱转强/强转弱/异常）。
- **校验**：`node tmp/validate.js` 七视图全部 PASS（overview/ladder/sectors/auction/risk/demon/rec），无渲染出错。
- **环境缺口仍存**：push2 板块/指数接口限流 → boards 表空 → 题材/指数/轮动维度暂空（行业维度正常）。限流解除后双击 `update.bat` 或计划任务到点自动补全。

## 关键约定 / 坑
- **分析日选择**：需 bars 稠密 + 同日 snapshot 命中 same_day（load_snapshot 按文件名匹配）。次优日会因缺快照降级。
- **板块成分依赖 store.boards 表**（board_members_batch 走 push2 clist）。若该接口被限流，boards 表为空 → 题材维度与指数表在输出中为空；行业维度仍可用（zt 记录自带 hybk）。补救：重跑 fetch.py + build.py。
- **build_limit_ups** 行业优先取 boards，缺失回退快照 hybk；concepts 仅来自 boards 表。
- 前端对空数组做了 `|| []` 兜底，空 concept/index 显示友好提示而非崩溃。

## 运行
1. 全量建库/日常更新：`python pipeline/fetch.py`（首次建库数分钟；含板块成分）
2. 构建数据：`python pipeline/build.py [--date=YYYY-MM-DD]`
3. 打开 `dist/index.html`（离线可用）
4. 校验：`node tmp/validate.js`
- 一键：`update.bat`
