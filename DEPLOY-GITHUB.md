# 把分析系统搬上云 —— GitHub Actions 部署手册

电脑不用开机，服务器不用买，每天定时自动跑分析、自动推送微信、自动更新网站。

全程在浏览器里完成，**不需要装任何软件、不需要敲命令**。

---

## 一、先说清楚：为什么是 GitHub，不是 Cloudflare / v0.app

你问的这几个平台我都实测评估过，结论如下。

| 平台 | 免费额度 | 能不能跑这个项目 | 原因 |
|---|---|---|---|
| **GitHub Actions** ✅ | 公开仓库定时任务**完全免费**、不限分钟数 | **可以，推荐** | 完整 Linux 环境，能跑 Python、能存 76MB 数据库、能发微信、还自带 Pages 托管网站 |
| Cloudflare Workers | 10 万次请求/天 | ❌ **不行** | 定时触发（Cron Trigger）在免费版**只给 10 毫秒 CPU 时间**。本项目一次分析要跑 5 秒以上，差了 500 倍。而且 Workers 没有文件系统，SQLite 数据库根本放不下 |
| Cloudflare Pages | 无限静态托管 | 🔸 只能放网站 | 它是纯静态托管，不能跑 Python 定时任务。可以当网站的备用托管，但算不完数据 |
| **v0.app** | 每月 $5 额度 | ❌ **不行** | 这是 Vercel 家的「AI 生成前端界面」工具，本质是聊天生成 React 页面，**它不是服务器、也不是定时器**。它生成的代码最终还是要部署到 Vercel |
| Vercel Cron | 免费版**每天只能触发 1 次**，且函数最长 10 秒 | ❌ **不行** | 我们一天要跑 11 次，且收盘分析要跑几十秒 |
| 腾讯云函数 SCF | 100 万次/月免费 | ✅ 可以，且**国内 IP 最稳** | 备选方案。需要实名认证、配置比 GitHub 麻烦，但行情源访问质量最好 |

**另外有一个坑，我提前替你趟平了：**

东方财富的主行情域名 `push2.eastmoney.com` 会**直接拒绝境外和云服务器的 IP**（连接被强制重置，重试多少次都没用）。GitHub 的服务器在美国，必然踩中。

我实测发现它的**延迟行情域名 `push2delay.eastmoney.com` 没有这个限制**，且：

- 字段完全一致，全市场 5892 只股票，59 页并发拉取 4.36 秒，零失败
- 盘中数据延迟约 15 分钟，**但收盘后的数据和实时完全相同**

所以代码已经改成**自动探测**：能连主域名就用实时的，连不上自动切延迟的，你不用管。

> 顺带修了一个隐藏 Bug：板块列表接口也走的这个被封的域名，导致数据库里**板块成分表一直是空的**——
> 这就是你之前说"板块推荐全部为空"的真正根因。现在已修复。

**对你的实际影响：**

| 任务 | 受不受延迟影响 |
|---|---|
| 收盘完整分析（16:10）| ❌ 不受影响，收盘数据已定格 |
| 复盘补发（20:00）| ❌ 不受影响 |
| 周末发酵提示 | ❌ 不受影响 |
| 盘中异动捕捉 | ⚠️ 会晚约 15 分钟。想要实时，得换腾讯云 SCF |

---

## 二、准备工作（5 分钟）

1. 注册一个 GitHub 账号：https://github.com/signup
2. 记住你的用户名，下面会用到

---

## 三、第 1 步：创建仓库

1. 打开 https://github.com/new
2. **Repository name** 填：`stock-analysis`
3. 选择 **Public（公开）**
   - ⚠️ 必须选公开。私有仓库每月只有 2000 分钟免费额度，而且 60 天不活动会停掉定时任务；公开仓库定时任务不限量
   - 🔒 别担心密钥泄露：微信推送的 token 走 GitHub Secrets 加密存储，**不会出现在代码里**（`.gitignore` 已经把 `config/notify.json` 排除了）
4. 其余保持默认，点 **Create repository**

---

## 四、第 2 步：上传代码

最省事的办法是网页拖拽：

1. 在新建好的仓库页面，点 **uploading an existing file**
2. 打开本机文件夹 `C:\Users\Basshunter-j\WorkBuddy\2026-08-04-11-06-17\stock-analysis`
3. **全选所有文件和文件夹**，拖进网页
4. ⚠️ 拖进去之后，在文件列表里**确认没有这两个**（有就删掉）：
   - `config/notify.json` ← 你的微信密钥，绝对不能传
   - `cache/market.db` ← 76MB 数据库，走第 5 步单独传
5. 下方 Commit message 随便填，点 **Commit changes**

> 网页上传有时会漏掉 `.github` 这种以点开头的文件夹。传完后检查仓库根目录有没有 `.github/workflows/stock.yml`，
> 没有的话：点 **Add file → Create new file**，文件名直接输 `.github/workflows/stock.yml`（斜杠会自动建目录），
> 再把本机同名文件的内容整个粘贴进去。`.gitignore` 同理。

---

## 五、第 3 步：配置微信推送密钥

1. 仓库页面 → **Settings**（顶部齿轮）→ 左侧 **Secrets and variables** → **Actions**
2. 点 **New repository secret**
3. **Name** 填：`NOTIFY_JSON`（一个字都不能错）
4. **Secret** 填入你本机 `config/notify.json` 的**完整内容**，长这样：

```json
{
  "wechat_serverchan": {
    "sendkey": [
      {"key": "SCT122xxxxxxxx", "name": "我"},
      {"key": "SCT393xxxxxxxx", "name": "接收人2"}
    ]
  },
  "wechat_pushplus": {
    "token": [
      {"token": "a3b1e9xxxxxxxx", "name": "我"},
      {"token": "40235fxxxxxxxx", "name": "接收人2"}
    ]
  }
}
```

5. 点 **Add secret**

> 存进去之后 GitHub 界面上也再看不到明文了，只能覆盖。这是正常的。

---

## 六、第 4 步：打开 Pages 网站托管

1. **Settings** → 左侧 **Pages**
2. **Source** 选 **GitHub Actions**（不是 Deploy from a branch）
3. 保存

网站地址会是：`https://你的用户名.github.io/stock-analysis/`

---

## 七、第 5 步：上传数据快照（关键一步）

GitHub 的运行机器**每次都是全新的，跑完就销毁，什么都不留**。所以有三样东西必须靠 Release 续存：

| 文件 | 里面是什么 | 丢了会怎样 |
|---|---|---|
| `cache/market.db` | 130 个交易日、55 万行 K 线 | 云端要从零重抓，几十分钟起步 |
| `dist/data.js` | 最近一次分析结果 | 盘前/竞价/异动推送直接报错，它们都读这个 |
| `dist/push_log.jsonl` | 推送去重记录 | 周日晚发过周一早再发一遍，白烧 ServerChan 额度 |

我已经在本机把它们打包好了，就在 `stock-analysis\cache\` 里：

- **`market.db.gz`** — 23.7 MB（原 80MB，压掉 70%）
- **`state.tar.gz`** — 55 KB（含 data.js + 推送日志 + 6 份推送存档）

**上传步骤：**

1. 仓库首页右侧 → **Releases** → **Create a new release**
2. **Choose a tag** → 输入框里手打 `data-snapshot` → 点下方出现的 **Create new tag: data-snapshot**
3. **Release title** 填：`行情数据快照`
4. 把 **`market.db.gz` 和 `state.tar.gz` 两个文件**一起拖到下方 **Attach binaries** 区域，等上传完（23MB 大概几十秒）
5. 点 **Publish release**

> ⚠️ tag 必须**一字不差**是 `data-snapshot`，两个文件名也不能改。流水线靠这三个名字找数据。

以后每天 16:10 收盘分析跑完，流水线会自动把新的快照覆盖回这个 Release，你不用再管。

---

## 八、第 6 步：手动跑一次，验证全链路

1. 仓库顶部 → **Actions** 标签
2. 首次进入会提示 "Workflows aren't being run on this forked repository" 之类，点 **I understand my workflows, go ahead and enable them**
3. 左侧点 **A股盘后分析流水线**
4. 右侧 **Run workflow** → 任务选 `build` → 绿色 **Run workflow**
5. 等 1~3 分钟，点进那次运行看日志

**成功的样子：**

```
✅ 数据库已恢复：80M
✅ 状态已恢复：data.js 有 · 推送日志 312 条
push2   : push2delay.eastmoney.com  → 延迟行情（盘后数据与实时一致）
  PASS [关键] 涨停池 zt_pool
  PASS [关键] 全市场分页 clist_paged
  PASS [关键] 指数快照 index_snapshot
  推送配置: ServerChan 2 个 key / PushPlus 2 个 token
[news] 去重后 209 条，达到相关性门槛(≥3) 30 条
[build] 板块归属映射 5620 只
[build] 写出 dist/data.js
✅ 数据库已回存（24M）
✅ 状态已回存（56K）
```

同时你的微信会收到推送，网站也会更新。

**如果失败，对照这张表：**

| 报错 | 原因 | 怎么修 |
|---|---|---|
| `找不到数据库快照` | 第 5 步没做或 tag 名写错 | 检查 Release 的 tag 是不是 `data-snapshot`，附件名是不是 `market.db.gz` |
| `dist/data.js 不存在` | 状态包没传，且还没跑过 build | 先手动跑一次 `build`，或把 `state.tar.gz` 补传上去 |
| `推送配置: ServerChan 0 个` | Secret 没配好 | 检查名字是不是 `NOTIFY_JSON`，内容是不是完整合法 JSON（可贴到 jsonlint.com 验一下）|
| `关键行情接口不可达` | 东财封了这台 runner 的出口 IP | 重跑一次会换台机器；连续多次失败就转腾讯云 SCF |
| Pages 部署报 environment 错 | 第 4 步 Source 没选 GitHub Actions | 回去改 |
| 板块归属映射 0 只 | 板块成分表是空的 | 手动跑一次 `build`，fetch 会自动重建（约 5 分钟）|

---

## 九、定时表（自动运行，你什么都不用做）

| 北京时间 | 做什么 |
|---|---|
| 周一~周五 09:02 | 盘前预判推送 |
| 周一~周五 09:25 | 集合竞价定调 |
| 周一~周五 10:07 / 11:07 / 13:07 / 14:07 / 15:07 | 盘中异动捕捉（PushPlus）|
| 周一~周五 16:10 | **收盘完整分析** + 更新网站 + 回存数据库 |
| 周一~周五 20:00 | 复盘补发（内容与收盘相同则自动跳过，省额度）|
| 周日 20:00 / 周一 08:00 | 周末发酵提示（现抓财经要闻，有料才发）|

收盘分析和周末推送前，流水线会先跑 `news_fetch.py` 抓东方财富 + 同花顺快讯，
按对 A 股的相关性打分过滤（政策/资金/监管加权，台风楼盘体育直接剔除），
取前 30 条写进 `news.json`。周末没有够格的要闻就自动不发，符合你要的"有则发、无则跳过"。

### 关于定时精度 —— 必须提前知道

GitHub 的定时是 **"尽力而为"**，不是精确闹钟：

- 平时误差 **3~10 分钟**
- 全球高峰期可能延迟 **15~30 分钟**
- 极端拥堵时**会直接丢弃这次触发**

所以：

- ✅ 收盘分析、复盘、周末提示 —— 晚十几分钟毫无影响，数据早定格了
- ⚠️ **集合竞价（09:25）风险最大** —— 它必须赶在 09:30 开盘前送到，延迟就废了
- ⚠️ 盘中异动 —— 叠加 15 分钟行情延迟，实际可能滞后 20~40 分钟

### 想要精确定时（强烈建议，根治漏发）

GitHub 自带 `schedule` 是「尽力而为」，高负载时会延迟甚至**整体丢弃**某次触发
（2026-08-12 盘前推送漏发即此根因：主调度 + 看门狗当天一次都没点火）。
用**免费的外部云端定时器**戳 GitHub 的 dispatch API，完全绕开 GitHub 自带调度器：

1. 到 https://github.com/settings/tokens 建一个 PAT，勾选 **workflow** 作用域
   （复用本项目已有的 PAT 即可，它本就能调 dispatch）
2. 注册 https://cron-job.org （免费，精确到分钟，纯云端运行，与本地开机无关）
3. 在 cron-job.org → Account → API Key 拿到你的 API Key
4. 仓库根目录一键注册全部定时任务：

   ```bash
   CRONJOB_API_KEY=你的cronjob_key  GH_PAT=你的github_pat  python tools/setup_cronjob.py
   ```

   脚本会按 `tools/cronjob-config.json` 创建 5 个任务（盘前 08:45 / 竞价 09:20 /
   收盘分析 15:15 / 复盘 19:55 / 盘中异动 10:05），直打 `stock.yml` 的
   `workflow_dispatch`。**无需手动改任何东西。**

> **为什么会重复？不会。** 本项目已做两层幂等：
> - 看门狗 / 备份订阅用「当日是否已推送该 mode」判定，**先到先发，后到跳过**；
> - `notifier.push` 额外加了 **mode+当日** 去重，多路同时点火也只真正发一次。
> 所以 **保留** `stock.yml` / `watchdog.yml` / `scheduler-backup.yml` 的全部 cron
> 作为冗余保险即可，**不要删**（删了反而少一层兜底）。外部定时器是权威触发源。
>
> 当前已是「三重独立订阅 + 外部定时器」四重保险，漏发概率趋近于零。

---

## 十、日常维护

**基本不用管。** 只有两件事偶尔留意：

1. **每次运行结果**：Actions 页面能看到绿勾/红叉。失败了 GitHub 默认会发邮件给你
2. **数据库快照**：每天 16:10 自动回存，不用手动备份

**想临时跑一次**：Actions → 选流水线 → Run workflow → 挑任务。

**想改推送时间**：编辑 `.github/workflows/stock.yml` 里的 cron。记住换算规则 —— **UTC = 北京时间 − 8 小时**，比如北京 16:10 写成 `10 8 * * 1-5`。

**想停掉自动运行**：Actions → 左侧选流水线 → 右上 `⋯` → Disable workflow。

---

## 十一、什么时候该换腾讯云 SCF

出现下面任一情况，说明 GitHub 不够用了：

- 盘中异动的 15 分钟延迟你无法接受，需要实时行情
- 集合竞价推送经常迟到，外部定时器也救不回来
- 东财开始连 `push2delay` 也封境外 IP

腾讯云 SCF 免费额度（每月 100 万次调用 + 40 万 GB·秒）对这个项目**绰绰有余**，而且是国内 IP，行情源零障碍。真到那一步跟我说，我帮你迁。

---

## 附：为上云顺带修掉的三个问题

搬上云的过程中挖出三个本来就存在、但一直没暴露的问题，都已修复。

**1. 板块数据一直是空的（就是你说的"板块推荐全部为空"）**

板块列表接口走的正是被封的 `push2.eastmoney.com`，抓取静默失败 → 数据库里
`boards` / `board_member` 两张表**一条记录都没有** → 板块热力、板块归属全线空转。

修复后：板块 **0 个 → 1000 个**，成分映射 **0 条 → 85418 条**，个股板块归属 **0 只 → 5620 只**。

**2. 指数快照也在静默失败**

`index_snapshot()` 同样走 push2。全市场清单因为有新浪主源顶着所以看不出问题，
但指数数据一直拿不到。现在走降级域名，8 个指数全部正常。

**3. 周末推送在云端会永久静默**

`news.json` 之前只有一条测试用的假新闻。搬到云上后，这个静态文件永远不会更新，
`_weekend_window_items` 过几天就筛不出任何条目 → 周末推送再也不会触发，
而且**不报错**，看日志一切正常。已新增 `pipeline/news_fetch.py` 接真实新闻源。

**另外 workflow 本身差点踩两个坑**（已在上线前修掉）：

- 本地是 `fetch.py` → `build.py` 两步走，云端只跑 `build.py` 的话会**永远在啃旧数据**
- `deploy-pages` 必须运行在 `github-pages` 这个 environment 下，而 environment 是
  job 级属性，写在 step 上会部署失败，所以部署拆成了独立 job
