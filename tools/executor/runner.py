# -*- coding: utf-8 -*-
"""stock-analysis 本地执行器主入口（runner）——完整 T+1 模拟买卖循环。

每日 09:26（竞价结束后）自动执行一轮：
  Phase 1 平仓：对全部未平仓持仓跑策略引擎（strategy.sell_decision）
    - 昨日断板 → 今日开盘卖（回测：拖到 T+2 平均 -1.18%）
    - 昨日续板但今日高开低走/日内涨≥5% → 锁定利润
    - 持仓 ≥3 交易日 → 无条件清仓
  Phase 2 开仓：新信号 → 决策线裁决（gap≥2% 才买）→ 最优变体分级过滤
    - A级 gap>5%+st≥3+市值60-150亿（胜率 62.2%/+2.71%）
    - B级 st≥3+60-150亿（61.8%）、C级 gap>5%+60-150亿 半仓（55.5%）
    - 其余放弃（全样本仅 48.7%/+0.37%，不值得占仓位）
  → 过风控闸门 → broker 下单 → 记录 → 可选推送

用法：
  python tools/executor/runner.py --now        # 立即执行一轮（测试/手动）
  python tools/executor/runner.py --scan       # 立即执行盘中巡逻（交易时段内有效）
  python tools/executor/runner.py --tail       # 立即执行尾盘确认通道（14:45 版，测试/手动）
  python tools/executor/runner.py --loop       # 常驻模式：09:26 开仓 + 盘中每15分钟巡逻 + 14:45 尾盘确认自动执行
  python tools/executor/runner.py --summary    # 查看模拟盘持仓概览
  python tools/executor/runner.py --report     # 月度盈亏报告（全流水+统计）
  python tools/executor/runner.py --review     # 当日复盘总结（收盘后跑，推送 PushPlus）

全时段可操作（2026-08-31 用户需求）：模拟盘不再只有三个时点——
交易时段内 executor.yml 每 30 分钟触发 --scan 盘中巡逻
（持仓卖出裁决 + 今日买入炸板保护 + 熔断监控），本地 --loop 每 15 分钟一轮。

交易纪律（用户 2026-08-29 拍板）：
  1. 操作前先判可成交性：一字板/封板买不进、跌停封死卖不出，全部留痕记录
  2. 先预判后成交：按实时价成交（不是预设数值无脑成交），预判后价格已升高
     也只能以当前实时价买入，卖出同理
  3. 买卖理由与明细推送 PushPlus（模拟盘操作段），每日复盘总结盈亏
  4. 每日操作+复盘写入网站「模拟盘」模块（build.py 从 sim_review.json 读）
"""
import json
import os
import re
import sys
import time
import atexit
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exec_core import (fetch_user_data, extract_signals, realtime_quote,
                       auction_gate, late_gate, market_gate, SITE,
                       apply_seat_avoid, apply_ladder_avoid, refine_buy_zone,
                       position_cap, late_session_maps,
                       assert_data_fresh, data_date)
from risk_gate import RiskGate
import broker_sim
import strategy

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
REVIEW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_review.json")


def _flush_at_exit():
    """进程退出兜底：把未 flush 的合并队列发出去（config 已不可得时读一次）。"""
    try:
        if _PENDING:
            _flush_pending(load_cfg())
    except Exception:
        pass


atexit.register(_flush_at_exit)


def load_cfg():
    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    # ---- account 凭据注入（2026-08-30 安全加固：public 仓库 config.json 不得存口令）----
    # 优先级：EXEC_ACCOUNT_JSON Secret > ALLOWED_USERS_JSON Secret(owner 的 pass) >
    #         config.local.json（本地开发，gitignore）> config.json 本体
    acc = cfg.get("account") or {}
    if not acc.get("user_id") or not acc.get("passwd"):
        env_acc = os.environ.get("EXEC_ACCOUNT_JSON", "").strip()
        if env_acc:
            try:
                js = json.loads(env_acc)
                acc["user_id"] = js.get("user_id") or acc.get("user_id")
                acc["passwd"] = js.get("passwd") or js.get("pass") or acc.get("passwd")
            except Exception as e:
                _log("EXEC_ACCOUNT_JSON 解析失败：%r" % e)
        if not acc.get("passwd"):
            env_au = os.environ.get("ALLOWED_USERS_JSON", "").strip()
            if env_au:
                try:
                    for u in (json.loads(env_au).get("users") or []):
                        if u.get("id") == "owner" and u.get("pass"):
                            acc["user_id"] = acc.get("user_id") or "owner"
                            acc["passwd"] = u["pass"]
                            break
                except Exception as e:
                    _log("ALLOWED_USERS_JSON 解析失败：%r" % e)
        if not acc.get("passwd"):
            local_cfg = os.path.join(os.path.dirname(CFG_PATH), "config.local.json")
            if os.path.exists(local_cfg):
                try:
                    lacc = (json.load(open(local_cfg, encoding="utf-8")).get("account") or {})
                    acc["user_id"] = acc.get("user_id") or lacc.get("user_id")
                    acc["passwd"] = acc.get("passwd") or lacc.get("passwd")
                except Exception as e:
                    _log("config.local.json 解析失败：%r" % e)
        cfg["account"] = acc
    return cfg


def _log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg))


def _notify(cfg, title, text, defer=False):
    """双通道推送：PushPlus（模拟盘主通道，owner+接收人2）+ ServerChan（可选）。
    任一通道失败不影响执行。

    2026-08-30 合并推送（用户要求：与其他推送时间重合时合并）：
      defer=True 的消息（汇总/尾盘回报等非操作前推送）先入队，
      进程退出时或下一条 immediate 推送前统一 flush——多条合并成一条，
      避免同一分钟轰炸多条。操作前推送（defer=False）永远立即发，保证「先推送后执行」。"""
    if defer:
        _PENDING.append((title, text))
        return
    _flush_pending(cfg)
    _notify_now(cfg, title, text)


_PENDING = []  # [(title, text)] 待合并推送队列


def _flush_pending(cfg):
    """把队列里的待发推送合并成一条发出。空队列无事。"""
    if not _PENDING:
        return
    if len(_PENDING) == 1:
        t, x = _PENDING.pop(0)
        _notify_now(cfg, t, x)
        return
    n = len(_PENDING)
    lines = []
    for i, (t, x) in enumerate(_PENDING):
        lines.append("### %s" % t)
        lines.append(x)
        if i < n - 1:
            lines.append("")
    _PENDING.clear()
    _notify_now(cfg, "📦 合并推送（%d 条）" % n, "\n".join(lines))


def _serverchan_key(ncfg):
    """ServerChan sendkey 三级查找（2026-09-01 推送加固）：
    cfg.notify.serverchan_key > NOTIFY_JSON.wechat_serverchan.sendkey（CI Secret）
    > 根目录 config/notify.json（CI 由 workflow 注入）。支持 str 或 [str]。"""
    def _norm(k):
        if isinstance(k, list):
            k = k[0] if k else ""
        return (str(k).strip() if k else "")
    k = _norm(ncfg.get("serverchan_key") or "")
    if k:
        return k
    env_ncfg = os.environ.get("NOTIFY_JSON", "").strip()
    if env_ncfg:
        try:
            k = _norm((json.loads(env_ncfg).get("wechat_serverchan") or {}).get("sendkey"))
            if k:
                return k
        except Exception:
            pass
    try:
        root_ncfg = os.path.join(ROOT, "config", "notify.json")
        if os.path.exists(root_ncfg):
            with open(root_ncfg, encoding="utf-8") as f:
                k = _norm((json.load(f).get("wechat_serverchan") or {}).get("sendkey"))
            if k:
                return k
    except Exception:
        pass
    return ""


def _md2html(title, text):
    """推送文本 → PushPlus html 模板（深色模式自适应 + 语义化动作提示 + 买点高亮）。

    2026-09-01 升级：
      · 深色模式自适应：用 CSS 变量 + @media(prefers-color-scheme:dark)，手机深色下
        不再全黑（此前硬编码 #222/#333/#f4f7ff 在深色模式不可读）。
      · 精简重复判断词：不再把「高开>2%、-2~0」这类判断条件原样复述，而是用
        语义化动作徽标（🟢买点 / 🔴卖点 / 🔵持有 / ⚪观望）告诉用户该做什么。
      · 买点行高亮：到达买点的票用红底黄字卡片凸显，一眼可见。
    配色遵循 A 股习惯：买入=红（涨）／卖出=绿（跌）／持有=蓝／观望=灰。
    """
    def _esc(s):
        # 去 markdown 加粗星号（`**` 在 html 里会裸显），转义特殊字符
        s = s.replace("**", "")
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    # ---- 语义化动作徽标：从一行文本里识别「该做什么」----
    def _action_badge(line):
        # 顺序：卖出/止损 > 买入/买点 > 持有 > 观望 > 无
        if any(k in line for k in ("SELL", "卖出", "止损", "清仓", "减仓", "割肉")):
            return ("卖出", "#0a8f3c", "#eafaf1")
        if any(k in line for k in ("BUY", "买入", "买点", "建仓", "加仓")):
            return ("买入", "#e02020", "#fff1f0")
        if any(k in line for k in ("HOLD", "持有", "确认持有")):
            return ("持有", "#2f6fed", "#eef3ff")
        if any(k in line for k in ("WATCH", "观望", "等", "不追")):
            return ("观望", "#6b7280", "#f3f4f6")
        return (None, None, None)

    def _badge_html(txt, fg, bg):
        return ('<span style="display:inline-block;margin-right:6px;padding:1px 7px;'
                'border-radius:10px;font-size:12px;font-weight:700;color:%s;background:%s">%s</span>'
                % (fg, bg, txt))

    def _word(line):
        # 关键词着色（语义词保持彩字，深色下用更亮的变体）
        pairs = (("买入", "#e02020"), ("加仓", "#e02020"), ("建仓", "#e02020"),
                 ("买点", "#e02020"), ("BUY", "#e02020"),
                 ("卖出", "#0a8f3c"), ("止损", "#0a8f3c"), ("清仓", "#0a8f3c"),
                 ("减仓", "#0a8f3c"), ("割肉", "#0a8f3c"), ("SELL", "#0a8f3c"),
                 ("持有", "#3b82f6"), ("HOLD", "#3b82f6"),
                 ("观望", "#9ca3af"), ("WATCH", "#9ca3af"))
        out = line
        for kw, css in pairs:
            if kw in out:
                out = out.replace(
                    kw, '<span style="color:%s;font-weight:600">%s</span>' % (css, kw))
        return out

        # 文案精简：去掉重复的「≥2%、-2~0、>5」等判断条件字面（提示已被徽标承载）

    # CSS 变量：浅/深两套
    body_css = (
        "--sans:'Segoe UI',system-ui,-apple-system,sans-serif;"
        "--c:#222;--c-sec:#555;--sec-bg:#f4f7ff;--sec-bd:#3b82f6;"
        "--divider:#eceef2;--buy:#e02020;--sell:#0a8f3c;--hold:#2f6fed;"
    )
    dark_css = (
        "@media (prefers-color-scheme:dark){"
        ".sa{--c:#e6e6e6;--c-sec:#b3b3b3;--sec-bg:#1e2740;--sec-bd:#4f7df9;"
        "--divider:#2c2f36;--buy:#ff5252;--sell:#34d399;--hold:#60a5fa;}"
        "}"
    )

    parts = ['<div class="sa" style="font-family:var(--sans);font-size:14px;color:var(--c)">']
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        act, afg, abg = _action_badge(s)
        # 买点/卖点到达 → 高亮卡片（红底黄字/绿底）
        is_buy_pt = ("买点" in s or "买入" in s or "BUY" in s)
        is_sell_pt = ("卖出" in s or "SELL" in s or "止损" in s or "割肉" in s)
        if s.startswith("## "):
            parts.append(
                '<div style="margin:12px 0 6px;padding:6px 10px;border-left:4px solid var(--sec-bd);'
                'background:var(--sec-bg);font-weight:700;font-size:15px;color:var(--c)">%s</div>'
                % _esc(s[3:]))
        elif s.startswith("- ") or s.startswith("* "):
            body = _word(_esc(s[2:]))
            if is_buy_pt:
                parts.append(
                    '<div style="margin:6px 0;padding:8px 10px;border-radius:8px;'
                    'border:1px solid var(--buy);background:color-mix(in srgb,var(--buy) 12%%,transparent);'
                    'line-height:1.65">%s%s</div>' % (_badge_html("🟢 买点", "var(--buy)", "var(--sec-bg)"), body))
            elif is_sell_pt:
                parts.append(
                    '<div style="margin:6px 0;padding:8px 10px;border-radius:8px;'
                    'border:1px solid var(--sell);background:color-mix(in srgb,var(--sell) 10%%,transparent);'
                    'line-height:1.65">%s%s</div>' % (_badge_html("🔴 卖出", "var(--sell)", "var(--sec-bg)"), body))
            elif act:
                parts.append(
                    '<div style="margin:4px 0;line-height:1.65">%s%s</div>'
                    % (_badge_html(act, afg, abg), body))
            else:
                parts.append(
                    '<div style="margin:4px 0;line-height:1.65;color:var(--c-sec)">• %s</div>' % body)
        else:
            body = _word(_esc(s))
            if act:
                parts.append(
                    '<div style="margin:4px 0;line-height:1.65">%s%s</div>'
                    % (_badge_html(act, afg, abg), body))
            else:
                parts.append(
                    '<div style="margin:4px 0;line-height:1.65;color:var(--c)">%s</div>' % body)
    parts.append('</div>')
    return ('<style>%s%s</style>%s' % (body_css, dark_css, "".join(parts)))


def _notify_cfg():
    """读取 notify 配置（与 pipeline/notifier.py 同构）：优先 NOTIFY_JSON 环境变量
    （CI 场景密钥走 Secrets 注入），否则回落项目根 config/notify.json。"""
    raw = os.environ.get("NOTIFY_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    rp = os.path.join(ROOT, "config", "notify.json")
    if os.path.exists(rp):
        try:
            return json.load(open(rp, encoding="utf-8"))
        except Exception:
            pass
    return {}


def _iter_notify_pp():
    """返回 [(token, scope)]；scope 缺省视为 all（兼容旧配置）。"""
    out = []
    pp = (_notify_cfg().get("wechat_pushplus") or {}).get("token") or []
    for x in (pp if isinstance(pp, list) else [pp]):
        if isinstance(x, dict):
            t = (x.get("token") or x.get("key") or "").strip()
            sc = (x.get("scope") or "all")
            if t:
                out.append((t, sc))
        elif isinstance(x, str) and x.strip():
            out.append((x.strip(), "all"))
    return out


def _iter_notify_sc():
    """返回 [(key, scope)]；scope 缺省视为 all。"""
    out = []
    sc = _notify_cfg().get("wechat_serverchan") or {}
    for fld in ("sendkey", "sendkeys"):
        v = sc.get(fld)
        for x in (v if isinstance(v, list) else [v] if v else []):
            if isinstance(x, dict):
                k = (x.get("key") or x.get("sendkey") or "").strip()
                scp = (x.get("scope") or "all")
                if k:
                    out.append((k, scp))
            elif isinstance(x, str) and x.strip():
                out.append((x.strip(), "all"))
    return out


def _serverchan_key_filtered(ncfg, allowed):
    """ServerChan sendkey 三级查找 + scope 过滤（2026-09-03 推送分级）：
    cfg.notify.serverchan_key（旧式→视为 all）> NOTIFY_JSON/config notify wechat_serverchan（按 scope）。
    只返回第一个 scope∈allowed 或 all 的 key。"""
    def _norm(k):
        if isinstance(k, list):
            k = k[0] if k else ""
        return (str(k).strip() if k else "")
    if "all" in allowed:
        k = _norm(ncfg.get("serverchan_key") or "")
        if k:
            return k
    for kk, sc in _iter_notify_sc():
        if sc in allowed or sc == "all":
            return kk
    return ""


def _notify_now(cfg, title, text, allowed=None):
    """实际执行推送（原 _notify 主体）。
    2026-09-01 推送加固（用户底线：不允许「跑了却没收到」）：
      · PushPlus 改走 https（原 http 明文，可能被中间设备拦截）；
      · 单 token 3 次尝试 + 退避重试（此前单次失败即丢）；
      · PushPlus 全部失败（或未配置）时自动回落 ServerChan 兜底——
        SC 每日 5 条额度紧张，仅在主通道全灭时才消耗。
    2026-09-03 推送分级（用户需求5）：推送按 scope 过滤——模拟盘操作类推送
      （本函数所有调用）只发给 scope∈{all, sim} 的通道；scope=none（如接收人2）
      或 scope=prepost 的通道不接收；owner（scope=all）始终接收。
    """
    if allowed is None:
        allowed = {"all", "sim"}   # runner 推送均为模拟盘操作类
    results = []
    ncfg = cfg.get("notify") or {}
    # --- PushPlus（按 scope 过滤）---
    pp_candidates = []  # (token, scope)
    # 1) config.json notify.pushplus_tokens（旧式，无 scope → 视为 all）
    _legacy = ncfg.get("pushplus_tokens") or []
    if isinstance(_legacy, str):
        _legacy = [_legacy]
    for _t in _legacy:
        _t = (_t or "").strip()
        if _t:
            pp_candidates.append((_t, "all"))
    # 2) notify.json / NOTIFY_JSON（带 scope）
    for _t, _sc in _iter_notify_pp():
        pp_candidates.append((_t, _sc))
    # 去重 + 按 scope 过滤（none / prepost 排除）
    _seen, pp_tokens = set(), []
    for _t, _sc in pp_candidates:
        if _t in _seen:
            continue
        if _sc not in allowed and _sc != "all":
            continue
        _seen.add(_t)
        pp_tokens.append(_t)
    pp_ok = 0
    if pp_tokens:
        try:
            import urllib.request
            ok = 0
            for tk in pp_tokens:
                sent = False
                # 2026-09-01：单 token 3 次尝试 + 退避（此前单次失败即丢推送）
                for attempt in range(3):
                    try:
                        # 2026-09-01：PushPlus 改 html 模板（三色标注 + 分区排版）
                        _content = _md2html(title, text)
                        payload = json.dumps({"token": tk, "title": title, "content": _content,
                                              "template": "html"}).encode()
                        req = urllib.request.Request(
                            "https://www.pushplus.plus/send", data=payload,
                            headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=15) as r:
                            js = json.loads(r.read().decode("utf-8"))
                        if js.get("code") == 200:
                            sent = True
                            break
                        _log("PushPlus 返回异常 code=%s msg=%s"
                             % (js.get("code"), js.get("msg")))
                    except Exception as e:
                        _log("PushPlus 尝试 %d/3 失败：%r" % (attempt + 1, e))
                    if attempt < 2:
                        time.sleep(1.5 * (attempt + 1))
                if sent:
                    ok += 1
            pp_ok = ok
            results.append("PushPlus %d/%d" % (ok, len(pp_tokens)))
        except Exception as e:
            results.append("PushPlus失败:%r" % e)
    # --- ServerChan（兜底通道）---
    # 2026-09-01 语义变更：仅当 PushPlus 全部失败（或未配置）时才发送——
    # SC 每日 5 条额度留给异动/强晋级推送，正常情况不再双通道重复消耗。
    if pp_ok == 0:
        key = _serverchan_key_filtered(ncfg, allowed)
        if key:
            try:
                import urllib.request
                import urllib.parse
                data = urllib.parse.urlencode({"title": title, "desp": text[:4000]}).encode()
                urllib.request.urlopen(
                    "https://sctapi.ftqq.com/%s.send" % key, data=data, timeout=15)
                results.append("ServerChan ok（兜底）")
            except Exception as e:
                results.append("ServerChan兜底失败:%r" % e)
    if not results:
        _log("（未配置任何推送通道，跳过推送）")
    else:
        _log("已推送 %s：%s" % (" + ".join(results), title))


def pick_broker(cfg):
    mode = cfg.get("broker") or "sim"
    if mode == "qmt":
        import broker_qmt
        if not broker_qmt.is_available():
            _log("⚠ broker=qmt 但 xtquant/账户未配置，回落到模拟盘 sim")
            mode = "sim"
        else:
            return broker_qmt.QmtBroker(), "qmt"
    return broker_sim.SimBroker(), mode


# ---------------- 交易日判定（2026-08-30 CI 托管） ----------------

# 2026-08-30 CI 全程托管（用户电脑不常开机且开机无网）：
# 执行器改跑在 GitHub Actions，以下状态文件必须跨 run 续存——
#   sim.db            持仓/流水（核心资产）
#   state/risk_state.json     风控幂等与熔断
#   state/notify_dedup.json   推送冷却
#   state/loss_streak.json    连亏纪律
#   sim_review.json           复盘历史（网站模块数据源）
# 打包为 Release data-snapshot 的 executor_state.tar.gz 附件；
# 本地开发模式（有本地 sim.db）自动跳过恢复/回存，互不干扰。
# 注意 risk_state.json 在 executor 根目录（risk_gate.STATE_PATH），不在 state/ 子目录。
EXEC_STATE_FILE = "executor_state.tar.gz"
_EXEC_STATE_MEMBERS = [
    "sim.db", "sim.db-wal", "sim.db-shm",
    "risk_state.json",
    "state/notify_dedup.json", "state/loss_streak.json",
    "sim_review.json",
    # 2026-08-31 升级：明日竞价关注清单（当日 WATCH/SKIP 的 st≥3 高度票，
    # 高度溢价单调 st=1→8 胜率 55.6%→82.4%——当天没买到的票明天竞价给好开价
    # 仍是机会，跨 run 持久化后次日 09:25 开仓通道前即时提醒）
    "state/auction_watch.json",
    # 2026-08-31 升级：任务账本（幂等守卫）——记录各任务当日是否已执行。
    # 背景：executor.yml 的 11 个 cron 实测被 GitHub 漏投递（2026-08-31 十个时点
    # 只送达 1 个，且延迟 24 分钟落到 07:07 UTC 被判成 review），因此新增
    # stock.yml 冗余触发链；多触发源必然产生重复 dispatch，幂等必须内建在执行器，
    # 而不是靠触发端的去重文件（触发端去重挡不住手动 dispatch 和 PC 计划任务）。
    "state/task_ledger.json",
]


def _in_ci():
    """是否运行在 GitHub Actions（或任何需要云持久化的环境）。"""
    return bool(os.environ.get("CI") or os.environ.get("GH_TOKEN"))


def _exec_state_paths():
    ex = os.path.dirname(os.path.abspath(__file__))
    return [os.path.join(ex, m) for m in _EXEC_STATE_MEMBERS if
            os.path.exists(os.path.join(ex, m))]


# ---------------- 任务账本（2026-08-31 幂等守卫） ----------------
# 多触发源（executor cron / stock.yml 冗余链 / PC 计划任务 / 手动 dispatch）下，
# 同一任务同一天可能被触发多次。重复开仓会双倍建仓、重复复盘会重复推送，
# 因此每个任务执行前查账本、执行成功后记账（失败不记账 → 允许自动重试）。
def _ledger_path():
    ex = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(ex, "state")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "task_ledger.json")


def _ledger_load():
    try:
        with open(_ledger_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _ledger_save(led):
    try:
        with open(_ledger_path(), "w", encoding="utf-8") as f:
            json.dump(led, f, ensure_ascii=False)
    except Exception as e:
        _log("任务账本写入失败：%r" % e)


def _ledger_done(task, within_sec=None, date=None):
    """该任务当日是否已执行过；within_sec 给定则只在该时间窗内算「已执行」。"""
    led = _ledger_load()
    d = date or time.strftime("%Y-%m-%d")
    ts = (led.get(d) or {}).get(task)
    if not ts:
        return False
    if within_sec and (time.time() - float(ts)) > within_sec:
        return False
    return True


def _ledger_mark(task):
    led = _ledger_load()
    d = time.strftime("%Y-%m-%d")
    led.setdefault(d, {})[task] = int(time.time())
    for k in sorted(led.keys())[:-10]:      # 只留最近 10 天
        led.pop(k, None)
    _ledger_save(led)


def _ledger_missing(date=None):
    """当日缺失的必备任务（用于复盘时自检「调度缺口」并上报）。"""
    d = date or time.strftime("%Y-%m-%d")
    miss = [t for t in ("now", "scan") if not _ledger_done(t, date=d)]
    return miss


# ---------------- 云端托管：状态持久化告警（2026-09-01）----------------
# 纯托管场景下用户不开电脑，看不到 CI 日志：状态包读/写失败会让模拟盘「静默
# 失忆」——持仓表空（该卖的没卖）、风控幂等表空（同票重复建仓）、资金回到初始
# 值。此前这类失败只写日志，用户只感觉「模拟盘又出问题了」却不知原因。现在统一
# 收集并在进程退出前 dedup 推送（每日最多一条，不轰炸）。
_PENDING_ALERTS = []
_LAST_CFG = None


def _flush_state_alerts(cfg=None):
    """进程退出前把状态读写告警一次性推送（dedup：同日同 key 只推一次）。"""
    global _PENDING_ALERTS
    cfg = cfg or _LAST_CFG
    if not _PENDING_ALERTS or not cfg:
        return
    msgs, _PENDING_ALERTS = list(_PENDING_ALERTS), []
    try:
        _act_notify(
            cfg,
            "⚠ 模拟盘状态持久化异常（云端托管告警）",
            "模拟盘状态在云端读写异常，可能影响持仓与建仓，建议核查：\n\n"
            + "\n".join("- " + m for m in msgs)
            + "\n\n- 说明：本条每日最多推送一次；状态包在 Release 资产"
              " executor_state.tar.gz，随下一轮自动重建。",
            dedup_key="state_io_fail")
    except Exception as e:
        _log("状态告警推送失败：%r" % e)


def exec_state_restore(force=False):
    """从 Release 恢复执行器状态（CI 专用；本地已有 sim.db 则跳过）。"""
    if not _in_ci() and not force:
        return False
    ex = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(ex, "sim.db")) and not force:
        _log("exec_state: 本地已有 sim.db，跳过恢复")
        return False
    try:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import gh_api
        import io
        st, body = gh_api.api("GET", "/repos/fisk9r/stock-analysis/releases/tags/data-snapshot")
        rel = json.loads(body) if isinstance(body, str) else body
        asset = next((a for a in rel.get("assets", []) if a["name"] == EXEC_STATE_FILE), None)
        if not asset:
            _log("exec_state: 无历史状态包（首次运行正常），从空账本开始")
            _PENDING_ALERTS.append(
                "状态包缺失（Release 无 executor_state.tar.gz）：模拟盘以空账本启动，"
                "历史持仓与风控幂等表为空，可能出现重复建仓")
            return False
        req = urllib.request.Request(asset["browser_download_url"],
                                     headers={"User-Agent": "executor"})
        with urllib.request.urlopen(req, timeout=120) as r:
            blob = r.read()
        import tarfile
        tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")
        # 安全：只解包白名单成员，拒绝绝对路径/.. 穿越（恶意或损坏的压缩包）
        _allow = set(_EXEC_STATE_MEMBERS)
        # 2026-09-03 修复（重大）：sim_review.json 是「本轮输出」不是「状态」——
        # 此前每次运行都把 Release 包里的旧复盘文件解包出来，workflow 第 7 步又把它
        # 原样推回 state/sim_review.json，导致 9/1 的旧占位文件永远循环覆盖线上
        # （复盘因时段闸失败后，新数据永远写不进来）。恢复时排除它，并删除本地残留。
        _allow.discard("sim_review.json")
        safe_members = []
        for m in tf.getmembers():
            name = m.name.replace("\\", "/").lstrip("./")
            if name not in _allow or m.issym() or m.islnk():
                if name == "sim_review.json":
                    continue  # 静默跳过：旧复盘不落地
                _log("exec_state: 跳过非法成员 %r" % m.name)
                continue
            m.name = name
            safe_members.append(m)
        tf.extractall(ex, members=safe_members)
        tf.close()
        # 防御：清掉可能的历史残留（旧包/手工放置），保证第 7 步只在
        # 本轮复盘真正生成时才回传
        _stale = os.path.join(ex, "sim_review.json")
        if os.path.exists(_stale):
            os.remove(_stale)
        _log("exec_state: 状态已恢复（sim.db %s）"
             % os.path.getsize(os.path.join(ex, "sim.db")))
        return True
    except BaseException as e:
        # BaseException：gh_api 网络失败会抛 SystemExit（不继承 Exception），
        # 必须兜住——恢复失败只能以空账本继续，绝不能让 CI job 直接死掉
        _log("exec_state 恢复失败（以空账本继续，不阻断）：%s %r" % (type(e).__name__, e))
        _PENDING_ALERTS.append(
            "状态恢复失败（%s）：本轮以空账本运行，历史持仓该卖的可能没卖、"
            "同票可能重复建仓" % type(e).__name__)
        return False


def exec_state_save():
    """把执行器状态打包回传 Release（CI 专用；best-effort 不阻断）。"""
    if not _in_ci():
        return False
    ex = os.path.dirname(os.path.abspath(__file__))
    paths = _exec_state_paths()
    if not any(p.endswith("sim.db") for p in paths):
        return False
    try:
        import tarfile, tempfile
        tmp = os.path.join(tempfile.gettempdir(), EXEC_STATE_FILE)
        with tarfile.open(tmp, "w:gz") as tf:
            for p in paths:
                tf.add(p, arcname=os.path.relpath(p, ex))
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import gh_api, urllib.request, json as _json, ssl
        st, body = gh_api.api("GET", "/repos/fisk9r/stock-analysis/releases/tags/data-snapshot")
        rel = _json.loads(body) if isinstance(body, str) else body
        up_url = rel["upload_url"].split("{")[0]
        # 同名 asset 先删再传（GH 不允许直接覆盖）
        for a in rel.get("assets", []):
            if a["name"] == EXEC_STATE_FILE:
                gh_api.api("DELETE", "/repos/fisk9r/stock-analysis/releases/assets/%d" % a["id"])
        data = open(tmp, "rb").read()
        tok = gh_api._token()
        req = urllib.request.Request(
            "%s?name=%s" % (up_url, EXEC_STATE_FILE), data=data, method="POST",
            headers={"Authorization": "Bearer " + tok,
                     "Content-Type": "application/gzip",
                     "Content-Length": str(len(data))})
        ctx = ssl._create_unverified_context() if hasattr(ssl, "_create_unverified_context") else None
        with urllib.request.urlopen(req, timeout=180, context=ctx) if ctx else \
                urllib.request.urlopen(req, timeout=180) as r:
            r.read()
        _log("exec_state: 状态已回存 Release（%d 个文件，%d KB）"
             % (len(paths), len(data) // 1024))
        return True
    except BaseException as e:
        # BaseException 同 restore：gh_api 失败抛 SystemExit，必须兜住
        # （回存失败下轮仍有本轮前状态，但不影响交易结果与推送）
        _log("exec_state 回存失败（不影响交易结果，下轮仍有本轮前状态）：%s %r"
             % (type(e).__name__, e))
        _PENDING_ALERTS.append(
            "状态回存失败（%s）：本轮成交未写回云端，下一轮会回到本轮前的状态，"
            "可能出现同票重复成交" % type(e).__name__)
        return False


def _sh_now():
    """当前上海时间 (hour, minute)。CI 默认 UTC，需 +8 修正。"""
    import datetime as _dt
    utc = _dt.datetime.utcnow() + _dt.timedelta(hours=8)
    return utc.hour, utc.minute


def is_trading_now(force=False, check_window=True):
    """今日是否 A 股交易日（用上证指数行情时间戳判定，节假日/停市=否）+ 交易时段闸。

    CI 托管后执行器跑在 GitHub Actions（周末 cron 已排除，但法定节假日排除不了）：
    节假日腾讯行情时间戳停在上个交易日 → 日期不匹配 → 整轮跳过，绝不拿旧数据下单。

    时段闸（2026-09-01 加固，用户要求「纯云端、不再出操作层错误」）：
    实测 GitHub cron 曾延迟投递——14:43 的尾盘任务 19:59 才跑。仅判交易日不够，
    收盘后的 "尾盘" 会拿收盘价误下单。这里额外卡上海交易时段
    （09:15-11:35 / 13:00-15:05），非时段一律拒单，不依赖 cron 准时。
    force=True（workflow_dispatch 手动测试）跳过时段判定。

    check_window=False（2026-09-03 修复）：只读任务（复盘 run_review）专用——
    此前 15:32 的复盘被「15:32 已收盘 → 禁止下单」时段闸误杀，模拟盘复盘
    自 9/1 起再未成功写入，网站永远显示 9/1 空占位日。复盘不下单，
    只需交易日判定（节假日/停市仍然拦截），收盘后 15:00-23:59 均可跑。
    """
    if force:
        return True, "force"
    # 时段闸：非交易时段禁止下单（防 cron 延迟投递拿收盘价误交易）
    if check_window:
        h, m = _sh_now()
        now_min = h * 60 + m
        morning = (9 * 60 + 15) <= now_min <= (11 * 60 + 35)
        afternoon = (13 * 60) <= now_min <= (15 * 60 + 5)
        if not (morning or afternoon):
            return False, "非交易时段（上海时间 %02d:%02d，禁止下单）" % (h, m)
    try:
        # 指数行情：realtime_quote 按首码映射 sh/sz/bj 前缀，指数码 000001 会被
        # 误映射成 sz000001（不存在）。上证指数的行情码是 sh000001——直接传
        # 带前缀的码，realtime_quote 对已带前缀的码不做二次映射（见 exec_core）。
        q = realtime_quote(["sh000001"])
        if not q:
            return True, "指数行情为空，放行（宁可多看一眼）"
        stamp = (q.get("000001") or q.get("sh000001") or {}).get("stamp") or ""
        today = time.strftime("%Y%m%d")
        if stamp[:8] == today:
            return True, "行情日期匹配"
        return False, "行情日期 %s ≠ 今日 %s（节假日/停市）" % (stamp[:8] or "空", today)
    except Exception as e:
        # 判定异常放行：网络抖动误杀比节假日误交易伤害更大（CI 主战场网络稳定）
        _log("交易日判定异常（放行）：%r" % e)
        return True, "判定异常放行"


# ---------------- Phase 1：平仓 ----------------

def _act_notify(cfg, title, text, dedup_key=None):
    """操作前推送（2026-08-29 用户要求：每次操作前都推送并说明理由）。
    best-effort：推送失败不阻断交易（推送本身已有失败留痕）。
    dedup_key：同日同 key 只推一次（HOLD 类推送冷却，避免重复轰炸）。"""
    try:
        if dedup_key and not _notify_should_send(dedup_key):
            _log("推送冷却跳过（今日已推）：%s" % dedup_key)
            return
        _notify(cfg, title, text)
        if dedup_key:
            _notify_mark_sent(dedup_key)
    except Exception as e:
        _log("操作前推送失败（不阻断）：%r" % e)


_DEDUP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state",
                           "notify_dedup.json")


def _notify_should_send(key):
    """同日同 key 只发一次（HOLD 冷却）。状态文件损坏视为可发。"""
    try:
        with open(_DEDUP_PATH, encoding="utf-8") as f:
            st = json.load(f)
        return st.get("date") != time.strftime("%Y-%m-%d") or key not in (st.get("keys") or [])
    except Exception:
        return True


def _notify_mark_sent(key):
    try:
        os.makedirs(os.path.dirname(_DEDUP_PATH), exist_ok=True)
        today = time.strftime("%Y-%m-%d")
        try:
            with open(_DEDUP_PATH, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
        if st.get("date") != today:
            st = {"date": today, "keys": []}
        if key not in st["keys"]:
            st["keys"].append(key)
        with open(_DEDUP_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass


def run_sells(broker, mode, cfg, data=None):
    """对全部持仓跑卖出策略。返回 (lines, n_sold)。

    data（可选，Batch3 #13）：提供时叠加区间止损/止盈第二道闸——
    策略引擎判 HOLD 但现价触及 zone.stop 或进入卖出区 → 升级为 SELL。
    """
    lines, n_sold = [], 0
    try:
        poss = broker.positions(open_only=True)
    except Exception as e:
        _log("持仓读取失败：%r" % e)
        return lines, 0
    if not poss:
        _log("Phase1 平仓：无持仓")
        return ["- 无持仓"], 0
    _log("Phase1 平仓：%d 笔持仓待裁决" % len(poss))

    codes = [p["code"] for p in poss]
    try:
        quote = realtime_quote(codes)
    except Exception as e:
        _log("行情失败：%r" % e)
        return ["- 行情失败，平仓顺延"], 0

    for p in poss:
        q = quote.get(p["code"])
        klines = []
        try:
            klines = strategy._tencent_kline(p["code"], n=12)
        except Exception as e:
            _log("K线失败 %s：%r" % (p["code"], e))
        try:
            dec = strategy.sell_decision(p, q, klines)
        except Exception as e:
            _log("策略异常 %s：%r" % (p["code"], e))
            dec = {"verdict": "HOLD", "price": 0, "reason": "策略异常，顺延"}
        # Batch3 #13：区间止损/止盈第二道闸（策略引擎判 HOLD 时再补一刀）
        if data is not None and dec.get("verdict") not in ("SELL",):
            try:
                _zv, _zp, _zr = refine_sell_zone(p, q, data)
                if _zv == "SELL":
                    dec = {"verdict": "SELL", "price": _zp, "reason": _zr}
                    _log("区间增强卖出 %s：%s" % (p["code"], _zr))
            except Exception as e:
                _log("区间增强异常 %s：%r" % (p["code"], e))
        pnl_pct = ((q.get("price") / p["avg_price"] - 1) * 100) if (q and q.get("price")) else None
        if dec["verdict"] == "SELL" and dec.get("price"):
            # 操作前推送：说明为什么卖（用户要求：每次操作前推送+理由）
            _act_notify(cfg,
                        "🔔 操作前确认：卖出 %s(%s)" % (p.get("name"), p["code"]),
                        "**卖出理由**：%s\n\n- 现价 %.2f｜成本 %.2f｜浮盈 %s\n- 纪律依据：策略引擎回测规则，先推送后执行"
                        % (dec["reason"], dec["price"], p["avg_price"],
                           ("%.2f%%" % pnl_pct) if pnl_pct is not None else "—"))
            # 可卖性检查（操作前留痕）：跌停封死卖不出 → 顺延并记录
            cs = strategy.can_sell(q or {}, p["code"])
            if not cs["ok"]:
                _log("卖出被拒 %s：%s" % (p["code"], cs["reason"]))
                if hasattr(broker, "record_reject"):
                    broker.record_reject(p["code"], "SELL", cs["reason"], p.get("name") or "")
                if hasattr(broker, "record_decision"):
                    broker.record_decision(p["code"], "HOLD", "拟卖被拒顺延：%s" % cs["reason"],
                                           p.get("name") or "", dec.get("price") or 0, pnl_pct)
                lines.append("- ⛔ **顺延** %s(%s)｜%s"
                             % (p.get("name"), p["code"], cs["reason"]))
                continue
            r = broker.sell_limit(p["code"], dec["price"], sig={
                "name": p.get("name"), "reason": dec["reason"], "source": "strategy"})
            if r.get("ok"):
                n_sold += 1
                lines.append("- **SELL %s**(%s) @%.2f 盈亏 %+.2f%%｜%s"
                             % (p.get("name"), p["code"], r["price"],
                                r["pnl_pct"], dec["reason"]))
                _log("卖出 %s：%s" % (p["code"], dec["reason"]))
            else:
                if hasattr(broker, "record_reject"):
                    broker.record_reject(p["code"], "SELL",
                                         "委托失败：%s" % r.get("reason"), p.get("name") or "")
                lines.append("- %s(%s) 卖出失败：%s" % (p.get("name"), p["code"], r.get("reason")))
        else:
            # HOLD 也是决策（用户要求：持有同样记录+推送理由）
            # 2026-08-31 推送降噪：HOLD 只留痕+入当日决策汇总，不再逐笔即时推送——
            # 持有是常态而非动作，逐条推是「推送杂乱」的主要来源；当日 14:45 尾盘
            # 回报与 15:30 复盘都会带完整持仓明细。真正倾向卖出（SELL）的才即时推。
            if hasattr(broker, "record_decision"):
                broker.record_decision(p["code"], "HOLD", dec["reason"],
                                       p.get("name") or "", dec.get("price") or 0, pnl_pct)
            lines.append("- HOLD %s(%s)：%.2f%%｜%s"
                         % (p.get("name"), p["code"],
                            pnl_pct if pnl_pct is not None else 0,
                            dec["reason"]))
            _log("持有 %s：%s" % (p["code"], dec["reason"]))
    return lines, n_sold


# ---------------- Phase 2：开仓 ----------------

def run_buys(broker, mode, cfg, sigs, mkt=None, data=None):
    """开仓主流程（09:25 竞价通道）。返回 (lines, n_buy)。

    data（可选，Batch3 #13）：提供时叠加
      · 席位回避（seat_avoid）+ 连板梯队回避（ladder gate=avoid）
      · 买入区间精修（refine_buy_zone：价在买区内才买，并带回止损位）
      · 总仓位系数（position_cap：热度/情绪退潮时压减新仓金额）
    """
    lines, n_buy = [], 0
    mkt = mkt or {"mode": "NORMAL", "reason": ""}
    # 大盘环境闸门（2026-08-29）：不是每天都该交易——环境偏弱直接不开新仓，
    # 持仓与否由 Phase1 卖出策略独立裁决；FREEZE 也留痕+推送，空仓是主动决策。
    if mkt["mode"] == "FREEZE":
        _log("大盘环境 FREEZE：%s" % mkt["reason"])
        if hasattr(broker, "record_decision"):
            broker.record_decision("__market__", "FREEZE", mkt["reason"], "大盘环境闸门")
        _act_notify(cfg, "🧊 今日不开新仓（大盘环境闸门）",
                    "**空仓理由**：%s\n\n- 持仓不受影响，仍按卖出策略独立裁决\n"
                    "- 纪律：环境偏弱时开新仓期望为负，持股/空仓等待是更优操作" % mkt["reason"])
        return ["- 🧊 **FREEZE** %s" % mkt["reason"]], 0
    if not sigs:
        return ["- 今日无新信号"], 0
    codes = [s["code"] for s in sigs]
    try:
        quote = realtime_quote(codes)
    except Exception as e:
        _log("✗ 行情失败：%r" % e)
        return ["- 行情失败，开仓顺延"], 0
    if not quote:
        return ["- 行情为空（可能非交易时段）"], 0

    gate = RiskGate((cfg.get("risk") or {}))
    bal = broker.balance() if hasattr(broker, "balance") else {}
    total = bal.get("total")
    _log("broker=%s | 总资产 %.0f | 熔断=%s | 环境=%s"
         % (mode, total or 0, "YES" if gate.tripped else "no", mkt["mode"]))
    # CAUTION：新仓减半（保留参与度，同时控制环境不确定时的敞口）
    caution_cut = 0.5 if mkt["mode"] == "CAUTION" else 1.0
    # 连亏纪律真正生效（2026-08-31 升级：此前只在复盘文案里写，买入端从未执行）：
    #   连亏≥3日 → 今日暂停开新仓；连亏2日 → 新仓金额减半。盈利日清零。
    loss_streak = 0
    try:
        with open(_LOSS_STATE_PATH, encoding="utf-8") as f:
            loss_streak = int((json.load(f) or {}).get("streak") or 0)
    except Exception:
        loss_streak = 0
    if loss_streak >= 3:
        _log("连亏 %d 日 → 今日暂停开新仓（连亏纪律）" % loss_streak)
        if hasattr(broker, "record_decision"):
            broker.record_decision("__market__", "SKIP",
                                   "连亏%d日纪律：暂停开新仓1日" % loss_streak,
                                   "连亏纪律", 0)
        return ["- ⛔ 连亏 %d 日 → 今日暂停开新仓（连亏纪律，只做持仓卖出裁决）"
                % loss_streak], 0
    if loss_streak == 2:
        caution_cut *= 0.5
        _log("连亏 2 日 → 新仓金额再减半（caution_cut=%.2f）" % caution_cut)

    def _track(code, name, action, reason, price=0.0):
        """观望/放弃/拒绝也全部留痕（用户要求：无论怎么操作都要记录）。"""
        if hasattr(broker, "record_decision"):
            broker.record_decision(code, action, reason, name or "", price)

    # 需求2：满仓时仍收集「到达买点但被持仓上限挡住」的票，收盘后去重提示 owner
    _full_blocked = []
    _max_pos = int(gate.cfg.get("max_positions", 4))

    for s in sigs:
        verdict = auction_gate(s, quote)
        if verdict["verdict"] != "BUY":
            gate.record(verdict, verdict["verdict"], 0, verdict["reason"])
            _track(verdict["code"], verdict.get("name"), "WATCH",
                   "竞价决策线：%s" % verdict["reason"])
            lines.append("- %s(%s) %s：%.2f%%｜%s"
                         % (verdict["name"], verdict["code"], verdict["verdict"],
                            verdict["open_gap"] or 0, verdict["reason"]))
            continue
        # Batch3 #13：席位回避 + 连板梯队回避（在分级之前，便宜跳过）
        if data is not None:
            _sk, _sw = apply_seat_avoid(verdict, data)
            if _sk:
                gate.record(verdict, "SKIP", 0, _sw)
                _track(verdict["code"], verdict.get("name"), "SKIP",
                       "席位回避：" + _sw)
                lines.append("- %s(%s) 席位回避：%s"
                             % (verdict["name"], verdict["code"], _sw))
                continue
            _lk, _lw = apply_ladder_avoid(verdict, data)
            if _lk:
                gate.record(verdict, "SKIP", 0, _lw)
                _track(verdict["code"], verdict.get("name"), "SKIP",
                       "梯队回避：" + _lw)
                lines.append("- %s(%s) 梯队回避：%s"
                             % (verdict["name"], verdict["code"], _lw))
                continue
        # 决策线通过 → 最优变体分级
        q = quote.get(verdict["code"]) or {}
        mc = q.get("float_mv") or None
        sf = strategy.strategy_filter(verdict, q, mc)
        if sf["grade"] == "X":
            gate.record(verdict, "SKIP", 0, sf["reason"])
            _track(verdict["code"], verdict.get("name"), "SKIP",
                   "分级过滤放弃：%s" % sf["reason"])
            lines.append("- %s(%s) 放弃：%.2f%%｜%s"
                         % (verdict["name"], verdict["code"],
                            verdict["open_gap"] or 0, sf["reason"]))
            continue
        # 可买性检查（操作前留痕）：一字板/封板买不进
        cb = strategy.can_buy(q, verdict["code"])
        if not cb["ok"]:
            gate.record(verdict, "SKIP", 0, cb["reason"])
            if hasattr(broker, "record_reject"):
                broker.record_reject(verdict["code"], "BUY", cb["reason"], verdict.get("name") or "")
            _track(verdict["code"], verdict.get("name"), "SKIP",
                   "买不进：%s" % cb["reason"])
            lines.append("- ⛔ **买不进** %s(%s)：%.2f%%｜%s"
                         % (verdict["name"], verdict["code"],
                            verdict["open_gap"] or 0, cb["reason"]))
            _log("买入被拒 %s：%s" % (verdict["code"], cb["reason"]))
            continue
        # 2026-09-01 云端托管加固：数据库层持仓幂等。
        # 风控幂等表（risk_state.json）存在 Release 资产里，状态恢复失败时会丢失
        # → 同一只票在同一轮/同一天被重复建仓。这里用 sim_positions 做第二道闸：
        # 已持有未平仓的票一律不再买（不依赖任何外部文件）。
        try:
            held = [p for p in (broker.positions(open_only=True) or [])
                    if p.get("code") == verdict["code"]]
        except Exception:
            held = []
        if held:
            reason = "已持有 %d 股（未平仓），不重复建仓" % (held[0].get("volume") or 0)
            gate.record(verdict, "REJECT", 0, reason)
            _track(verdict["code"], verdict.get("name"), "SKIP", "持仓幂等：%s" % reason)
            lines.append("- %s(%s) 已持仓，跳过重复建仓" % (verdict["name"], verdict["code"]))
            _log("买入跳过 %s：%s" % (verdict["code"], reason))
            continue
        # 2026-09-03 仓位重构：最多同时持仓 max_positions 只（用户拍板 3331/3322 分仓）。
        # 几千块的小仓位对 10 万本金涨跌无意义，单票按总资产百分比建仓。
        try:
            n_open = len(broker.positions(open_only=True) or [])
        except Exception:
            n_open = 0
        _max_pos = int(gate.cfg.get("max_positions", 4))
        if n_open >= _max_pos:
            # 需求2：满仓时仍收集「真正到达买点」的票，收盘后去重提示 owner（scope=all/sim）。
            # 若 data 可用，先用 refine_buy_zone 确认当前价在买区内，避免把"等回踩"的票误报为买点。
            _is_buypoint = True
            if data is not None:
                _v, _why, _stop = refine_buy_zone(verdict, quote, data)
                _is_buypoint = (_v == "BUY")
            if _is_buypoint:
                _full_blocked.append({
                    "code": verdict["code"], "name": verdict.get("name"),
                    "grade": sf["grade"], "open_gap": verdict.get("open_gap"),
                    "price": (q or {}).get("price"), "reason": sf["reason"]})
            reason = "持仓已满 %d 只（上限 %d），不再开新仓" % (n_open, _max_pos)
            gate.record(verdict, "SKIP", 0, reason)
            _track(verdict["code"], verdict.get("name"), "SKIP", reason)
            lines.append("- %s(%s) %s" % (verdict["name"], verdict["code"], reason))
            _log("买入跳过 %s：%s" % (verdict["code"], reason))
            continue
        # Batch3 #13：买入区间精修（价在买区内才买，并带回止损位供 broker 记录）
        if data is not None:
            _v, _why, _stop = refine_buy_zone(verdict, quote, data)
            if _v != "BUY":
                gate.record(verdict, "WATCH", 0, _why)
                _track(verdict["code"], verdict.get("name"), "WATCH",
                       "区间精修：" + _why)
                lines.append("- %s(%s) 等回踩：%s"
                             % (verdict["name"], verdict["code"], _why))
                continue
            if _stop:
                verdict = dict(verdict, stop=_stop)
        # 2026-09-03 仓位重构（用户拍板：情况好可梭哈1支/分仓2支，不再死守3331/3322）：
        # 单票目标仓位按评级定（grade_pct），强信号可集中到 65%、弱信号 30%；
        # 仍受 单笔硬顶 / 单票上限 / 可用现金 三重封顶；最多同时持仓 max_positions 只。
        _gp = gate.cfg.get("grade_pct") or {"A": 0.65, "B": 0.55, "T": 0.50, "C": 0.30}
        _pct = _gp.get(sf["grade"], 0.25)
        if total:
            amount = int(total * _pct * caution_cut)
            try:
                _cash = float((bal or {}).get("cash") or 0)
            except Exception:
                _cash = 0
            amount = int(min(amount, gate.cfg["max_trade_amount"],
                             total * gate.cfg["max_position_pct"],
                             _cash * 0.95 if _cash > 0 else amount))
        else:
            amount = int(gate.cfg["max_trade_amount"] * _pct * caution_cut)
        # Batch3 #13：总仓位系数（热度/情绪退潮时压减新仓金额，与 market_gate 互补）
        if data is not None:
            _pc = position_cap(data)
            if _pc < 1.0:
                amount = int(amount * _pc)
                _log("总仓位系数 %.2f → 新仓金额 %d" % (_pc, amount))
        # 最多持仓只数约束（3331/3322 分仓总闸）
        cur_pos = len(broker.positions(open_only=True) or []) if hasattr(broker, "positions") else 0
        chk = gate.check(verdict, total, cur_pos)
        if not chk["ok"]:
            gate.record(verdict, "REJECT", 0, chk["reason"])
            if hasattr(broker, "record_reject"):
                broker.record_reject(verdict["code"], "BUY", "风控：%s" % chk["reason"],
                                     verdict.get("name") or "")
            _track(verdict["code"], verdict.get("name"), "SKIP",
                   "风控拒绝：%s" % chk["reason"])
            lines.append("- %s(%s) 过闸拒绝：%s" % (verdict["name"], verdict["code"], chk["reason"]))
            continue
        # 实时价成交（用户纪律2）：预判后价格已升高也只能以当前实时价买入，
        # 绝不能用昨日收盘价/预设数值无脑成交
        price = q.get("price") or verdict.get("close") or 0
        if not price or price <= 0:
            gate.record(verdict, "REJECT", 0, "无有效实时价")
            _track(verdict["code"], verdict.get("name"), "SKIP", "无有效实时价，放弃")
            continue
        # 操作前推送：说明为什么买（用户要求：每次操作前推送+理由）
        _act_notify(cfg,
                    "🔔 操作前确认：买入 %s(%s) [%s级]" % (verdict["name"], verdict["code"], sf["grade"]),
                    "**买入理由**：%s\n\n- 高开 %.2f%%｜实时价 %.2f｜金额 %d 元（占总资产约 %.1f%%）\n- 大盘环境：%s\n- 纪律依据：竞价决策线（高开≥2%% 胜率 67.4%%/期望 +4.08%%）+ 最优变体分级，先推送后执行"
                    % (sf["reason"], verdict["open_gap"] or 0, price, amount,
                       100.0 * amount / total if total else 0, mkt["reason"]))
        r = broker.buy_limit(verdict["code"], price, amount, sig=dict(
            verdict, reason="%s｜实时价%.2f成交" % (sf["reason"], price)))
        ok = "✓" if r.get("ok") else "✗ %s" % r.get("reason")
        gate.record(verdict, "BUY", amount, ok)
        lines.append("- **BUY %s**(%s) %s 高开 %.2f%% 实时价%.2f %d 元｜%s"
                     % (verdict["name"], verdict["code"], sf["grade"],
                        verdict["open_gap"], price, amount, sf["reason"]))
        if r.get("ok"):
            n_buy += 1
            _log("买入 %s %s @实时价%.2f：%s" % (verdict["code"], sf["grade"], price, sf["reason"]))
        else:
            if hasattr(broker, "record_reject"):
                broker.record_reject(verdict["code"], "BUY",
                                     "委托失败：%s" % r.get("reason"), verdict.get("name") or "")
            _track(verdict["code"], verdict.get("name"), "SKIP",
                   "委托失败：%s" % r.get("reason"))
            _log("买入失败 %s：%s" % (verdict["code"], r.get("reason")))
    # 需求2：满仓时到达买点但被持仓上限挡住的票，去重提示 owner（scope=all/sim）。
    # 接收人2 scope=none 不会收到；每日同 key 只推一次，避免重复轰炸。
    if _full_blocked:
        _fb_lines = []
        for b in _full_blocked:
            _fb_lines.append("- 🟢 **买点** %s(%s) [%s级] 高开%.2f%% 实时价%.2f ｜ %s"
                             % (b["name"], b["code"], b["grade"],
                                b["open_gap"] or 0, b["price"] or 0, b["reason"]))
        _fb_text = ("**持仓已满 %d 只，但以下股票今日到达买点（无法新建仓，供你手动关注 / 换仓参考）：**\n\n%s"
                    % (_max_pos, "\n".join(_fb_lines)))
        _act_notify(cfg, "🔔 满仓买点提示（%d 只）" % len(_full_blocked), _fb_text,
                    dedup_key="full_buypoint_%s" % time.strftime("%Y-%m-%d"))
    return lines, n_buy


# ---------------- 尾盘确认通道（14:45 版） ----------------

def run_tailgate(broker, mode, cfg):
    """14:45 尾盘确认：只做持仓管理，不开新仓（回测：尾盘追强期望仅 +0.6%）。

    规则（strategy.tailgate_decision，全部来自 309 日全样本回测）：
      · 深亏(现价较开盘≤-3%) → 尾盘止损（深亏过夜次日 -0.31%/红盘率 45%）
      · 微红(0~+2%) → 尾盘确认持有（最强过夜信号：+3.01%/红盘率 62.9%）
      · 其他 → 不干预（常规策略明日裁决）
    只影响「当日新买入」的持仓（尾盘确认的价值就在买入当天）；
    老持仓的卖出决策由次日早盘 Phase1 的 sell_decision 全权负责，避免双重裁决。
    """
    lines = []
    try:
        poss = broker.positions(open_only=True)
    except Exception as e:
        _log("尾盘通道：持仓读取失败：%r" % e)
        return ["- 持仓读取失败"], 0
    if not poss:
        return ["- 无持仓，尾盘通道无事可做"], 0
    today = time.strftime("%Y-%m-%d")
    # 只看当日买入的持仓（尾盘确认的语义边界）
    todays = [p for p in poss if (p.get("buy_date") or "") == today]
    if not todays:
        _log("尾盘通道：今日无新买入持仓（%d 笔老持仓交给明日早盘策略）" % len(poss))
        return ["- 今日无新买入持仓，老持仓由明日早盘策略裁决"], 0
    codes = [p["code"] for p in todays]
    try:
        quote = realtime_quote(codes)
    except Exception as e:
        _log("尾盘通道：行情失败：%r" % e)
        return ["- 行情失败，尾盘确认顺延"], 0
    if not quote:
        return ["- 行情为空（可能非交易时段）"], 0

    n_act = 0
    for p in todays:
        q = quote.get(p["code"]) or {}
        dec = strategy.tailgate_decision(p, q)
        if dec.get("verdict") is None:
            _log("尾盘 %s：%s" % (p["code"], dec["reason"]))
            if hasattr(broker, "record_decision"):
                broker.record_decision(p["code"], "HOLD",
                                       "尾盘确认：%s" % dec["reason"],
                                       p.get("name") or "",
                                       dec.get("price") or 0)
            lines.append("- %s(%s) 尾盘中性，不干预｜%s"
                         % (p.get("name"), p["code"], dec["reason"]))
            continue
        if dec["verdict"] == "SELL":
            # 2026-09-01 T+1 修复：run_tailgate 只处理「今日买入」的持仓，而 A 股
            # T+1 当日买不可卖——尾盘止损若执行就是 T+1 违规。改为锁 T+1 顺延，
            # 明日开盘按 sell_decision 规则裁决（止损信号保留留痕）。
            _log("尾盘 %s：%s（T+1 当日买不可卖，顺延明日）" % (p["code"], dec["reason"]))
            if hasattr(broker, "record_decision"):
                broker.record_decision(p["code"], "HOLD",
                                       "尾盘止损被 T+1 锁定（今日买入不可卖），顺延明日：%s"
                                       % dec["reason"], p.get("name") or "", dec["price"] or 0)
            lines.append("- 🔒 %s(%s) T+1 锁定（今日买入），止损顺延明日｜%s"
                         % (p.get("name"), p["code"], dec["reason"]))
            continue
        elif dec["verdict"] == "HOLD":
            if hasattr(broker, "record_decision"):
                broker.record_decision(p["code"], "HOLD",
                                       "尾盘确认：%s" % dec["reason"],
                                       p.get("name") or "", dec.get("price") or 0)
            lines.append("- ✅ **尾盘确认持有** %s(%s)：%s" % (p.get("name"), p["code"], dec["reason"]))
            # 2026-08-31 推送降噪：尾盘确认持有不再逐笔推送（与早盘 HOLD 同口径），
            # 只留痕，统一进当日 15:30 复盘；SELL 止损仍即时推
            _log("尾盘确认持有 %s：%s" % (p["code"], dec["reason"]))
    return lines, n_act


# ---------------- 主流程 ----------------

def run_once(cfg, force=False):
    acc = cfg.get("account") or {}
    if not acc.get("user_id") or not acc.get("passwd"):
        _log("未配置 account，退出")
        return
    # 交易日判定（CI 托管：节假日绝不拿旧行情下单；本地误判放行兜底）
    ok, why = is_trading_now(force=force)
    if not ok:
        # 2026-09-01 用户推送策略：非交易日 0 推送（连「⌛ 跳过」也不发——
        # 固定节奏 = 交易日早盘回报 + 下午盘复盘各 1 条，其余全按需）
        _log("非交易日，跳过开平仓：%s" % why)
        return
    # 幂等守卫：多触发源下当日开仓只做一次（重复 dispatch 不该双倍建仓）
    if not force and _ledger_done("now"):
        _log("今日开仓通道已执行（任务账本命中），跳过重复触发")
        return
    try:
        data_ok = _run_once_inner(cfg, force=force)
        if data_ok:
            _ledger_mark("now")     # 只有成功才记账，异常留给下轮自动重试
        else:
            # 2026-09-01 升级：数据拉取失败不记账——否则 09:25-09:45 窗口内所有
            # 冗余触发被「已执行」挡掉，全天无开仓（「挂起」的另一种形态）。
            # 不记账 → 09:28 备份 cron / stock.yml 冗余 dispatch 自动重试补开仓。
            _log("⚠ 开仓数据拉取失败：本轮不记任务账本（冗余触发将自动重试）")
    finally:
        # 2026-08-30 修复：run_once 抛异常时也必须回存状态，
        # 否则本轮成交/风控记录丢失，下轮以旧状态运行
        exec_state_save()


def _daily_loss_check(broker, cfg, gate=None, label=""):
    """当日亏损熔断检查（2026-08-31 修复：risk_gate.check_daily_loss 从未被调用，
    -3% 熔断线形同虚设）。用总资产相对初始资金回撤口径近似当日组合亏损；
    触发即写入 risk_state.json 熔断，之后所有 BUY 被 check() 拦截（SELL 不受限，
    持仓仍按卖出策略独立裁决——熔断保护的是开仓，不是把持仓锁死在亏损里）。"""
    try:
        bal = broker.balance()
        init = broker_sim._initial_cash()
        if not init or not bal.get("total"):
            return None
        pnl_pct = (bal["total"] / init - 1) * 100
        if gate is None:
            gate = RiskGate((cfg.get("risk") or {}))
        was = gate.tripped
        gate.check_daily_loss(pnl_pct)
        if gate.tripped and not was:
            _act_notify(cfg, "🛑 熔断触发：%s（组合回撤 %.2f%%）" % (label, pnl_pct),
                        "**当日组合回撤 %.2f%% 已触发熔断线 %.2f%%**\n\n"
                        "- 今日剩余时段不再开新仓（BUY 全部拦截）\n"
                        "- 持仓卖出裁决不受影响，止损照常执行\n"
                        "- 恢复方式：人工删除 risk_state.json 的 circuit_break"
                        % (pnl_pct, (cfg.get("risk") or {}).get("daily_loss_stop_pct", -3.0)))
        return pnl_pct
    except Exception as e:
        _log("熔断检查失败（不阻断）：%r" % e)
        return None


def _run_once_inner(cfg, force=False):
    """返回 data_ok：True=线上数据拉取成功（无论买卖几笔）；False=拉取失败。
    run_once 据此决定是否记任务账本（失败不记账 → 冗余触发自动重试）。"""
    broker, mode = pick_broker(cfg)
    # 2026-08-31 修复（致命）：此前 L894 直接引用 acc["user_id"]，但本函数无 acc
    # 变量/参数（acc 只在 run_once 作用域）→ 恒抛 NameError → fetch 失败降级 0 成交，
    # 即便 account Secret 注入成功也白搭。此处从 cfg 补取，与 run_once 入口一致。
    acc = cfg.get("account") or {}

    # 明日竞价关注清单提醒（2026-08-31 升级）：昨日复盘标记的高度票今日再审视。
    # 只提醒不自动买——最终仍由竞价决策线 + 分级 + 风控裁决。
    try:
        aw = _auction_watch_load()
        if aw:
            lines_aw = ["**昨日复盘标记的高度票，今日竞价重点观察：**", ""]
            for it in aw:
                lines_aw.append("- %s(%s) st=%d｜%s"
                                % (it.get("name"), it.get("code"),
                                   it.get("streak") or 0, it.get("reason") or ""))
            lines_aw.append("")
            lines_aw.append("- 竞价纪律：高开≥2%跟进 / st=2 需≥5% / 低开≤-2%放弃 / 平开观望")
            _act_notify(cfg, "🎯 今日竞价关注清单（%d 只）" % len(aw),
                        "\n".join(lines_aw), dedup_key="auction_watch")
            _auction_watch_consume()
            _log("竞价关注清单已推送并消费：%d 只" % len(aw))
    except Exception as e:
        _log("竞价关注清单提醒失败（不阻断）：%r" % e)

    # Phase 0：提前取线上数据（sell 端也用区间止损增强；失败不阻断卖出，区间增强跳过）
    _log("=" * 30 + " 拉取线上数据 " + "=" * 30)
    data = None
    data_ok = False
    mkt = {"mode": "NORMAL", "reason": "线上数据未取到，环境闸门放行（个股决策线仍生效）"}
    sigs = []
    if acc.get("user_id") and acc.get("passwd"):
        try:
            data = fetch_user_data(acc["user_id"], acc["passwd"])
            # 2026-09-01 云端托管加固：数据新鲜度闸门——build 失败或 CF 部署失败时
            # 线上数据会停留在旧日期，没有这道闸门就会拿过期信号在今天开盘下单。
            assert_data_fresh(data, force=force)
            _log("线上数据日期：%s（新鲜度校验通过）" % (data_date(data) or "未知"))
            sigs = extract_signals(data)
            mkt = market_gate(data)
            data_ok = True
            _log("大盘环境闸门：%s｜%s" % (mkt["mode"], mkt["reason"]))
            _log("信号 %d 条（core+relay+fused 去重）" % len(sigs))
        except Exception as e:
            _log("✗ 数据拉取/解密失败（卖出端照常，区间增强跳过）：%r" % e)
            data = None
    else:
        _log("未配置 account，跳过线上数据拉取（无信号来源）")

    # Phase 1：平仓（区间止损增强在 data 可用时生效）
    _log("=" * 30 + " Phase1 平仓 " + "=" * 30)
    sell_lines, n_sold = run_sells(broker, mode, cfg, data)

    # Phase 2：开仓（需要线上信号；先裁大盘环境，再裁个股）
    _log("=" * 30 + " Phase2 开仓 " + "=" * 30)
    buy_lines, n_buy = [], 0
    if data_ok:
        buy_lines, n_buy = run_buys(broker, mode, cfg, sigs, mkt, data)
    else:
        buy_lines = ["- 数据拉取失败，开仓顺延"]

    # 当日亏损熔断（开仓后复查：若本轮买入后组合回撤越线，立即熔断，
    # 尾盘通道与后续轮次的开仓全部拦截）
    _daily_loss_check(broker, cfg, label="早盘通道")

    # 汇总
    _log("=" * 60)
    all_lines = (["## 大盘环境：%s" % mkt["mode"], "- %s" % mkt["reason"], "",
                  "## 平仓（%d 笔）" % n_sold] + sell_lines +
                 ["", "## 开仓（%d 笔）" % n_buy] + buy_lines)
    for ln in all_lines:
        _log(ln)
    if mode == "sim":
        try:
            _log(broker_sim.SimBroker().summary())
        except Exception as e:
            _log("战绩汇总失败：%r" % e)
    # 2026-09-01 用户推送策略（固定 2 条/天：早盘回报 + 下午盘复盘）：
    #   · 成功轮无条件推汇总——0 成交也是当日状态，且账本幂等保证当日只此一条；
    #   · 失败轮不推汇总——重试期间可能连续多轮，重复「卖0买0/失败」是用户
    #     明确反感的噪音；改为每日 1 次失败告警（dedup），重试成功后由成功轮
    #     的回报补全当日状态。
    if data_ok:
        _notify(cfg, "执行器回报（卖%d 买%d）" % (n_sold, n_buy), "\n".join(all_lines))
    else:
        _act_notify(cfg, "⚠ 开仓数据拉取失败（将自动重试）",
                    "线上数据拉取失败，本轮不记账，冗余触发自动重试补开仓。\n\n%s"
                    % "\n".join(all_lines), dedup_key="now_fetch_fail")
    return data_ok


def _tail_buys(broker, cfg, sigs, mkt, data=None):
    """尾盘入场通道：late_gate（微红横盘）确认买入，半仓。

    依据（exec_core.late_gate 文档，309 交易日全市场涨停票，前提开盘≥2%）：
      高开≥2% + 14:45 现价较开盘 +0~2%（微红横盘不回补）→ 次日 +3.01%/62.9%
      （1623 样本，14 个月逐月全正）——14:45 买入价≈收盘价，口径一致。
      深亏/强拉桶分别 -0.31%/+0.62%，全部放弃。
    早盘已委托的票由 RiskGate 幂等拒绝，天然防重复。

    Batch3 #13/#14（data 可选）：叠加席位/梯队回避、买入区间精修、总仓位系数；
    趋势票额外读 late_session——命中 exit_warn（尾盘走弱警示）则不接（避免接飞刀），
    命中 watch_tomorrow（次日关注确认）则优先保留。
    """
    lines, n_buy = [], 0
    if not sigs:
        return lines, 0
    codes = [s["code"] for s in sigs]
    try:
        quote = realtime_quote(codes)
    except Exception as e:
        _log("✗ 尾盘行情失败：%r" % e)
        return lines, 0
    if not quote:
        return lines, 0
    gate = RiskGate((cfg.get("risk") or {}))
    bal = broker.balance() if hasattr(broker, "balance") else {}
    total = bal.get("total")
    cut = 0.5 if mkt.get("mode") == "CAUTION" else 1.0
    if mkt.get("mode") == "FREEZE":
        _log("尾盘入场：大盘环境 FREEZE，不开新仓")
        return ["- 🧊 尾盘 FREEZE：%s" % mkt.get("reason", "")], 0
    # Batch3 #14：尾盘确认读 late_session（趋势票次日确认）
    _ls_watch, _ls_warn = (late_session_maps(data) if data is not None else (set(), set()))

    def _track(code, name, action, reason):
        if hasattr(broker, "record_decision"):
            broker.record_decision(code, action, reason, name or "")

    for s in sigs:
        v = late_gate(s, quote)
        if v["verdict"] != "BUY":
            _track(v["code"], v.get("name"), "WATCH", "尾盘确认:%s" % v["reason"])
            continue
        # Batch3 #14：趋势票尾盘确认——命中 exit_warn 不接（尾盘走弱，次日谨慎）
        if data is not None and v.get("market_type") == "trend" and str(v["code"]) in _ls_warn:
            _track(v["code"], v.get("name"), "WATCH",
                   "尾盘确认:趋势票命中 late_session 走弱警示，不接")
            _log("尾盘买入跳过 %s：late_session 走弱警示" % v["code"])
            continue
        # Batch3 #13：席位回避 + 连板梯队回避
        if data is not None:
            _sk, _sw = apply_seat_avoid(v, data)
            if _sk:
                gate.record(v, "TAIL_SKIP", 0, "尾盘席位回避:" + _sw)
                _track(v["code"], v.get("name"), "SKIP", "尾盘席位回避:%s" % _sw)
                continue
            _lk, _lw = apply_ladder_avoid(v, data)
            if _lk:
                gate.record(v, "TAIL_SKIP", 0, "尾盘梯队回避:" + _lw)
                _track(v["code"], v.get("name"), "SKIP", "尾盘梯队回避:%s" % _lw)
                continue
        q = quote.get(v["code"]) or {}
        sf = strategy.strategy_filter(v, q, q.get("float_mv") or None)
        if sf["grade"] == "X":
            gate.record(v, "TAIL_SKIP", 0, "尾盘分级:" + sf["reason"])
            _track(v["code"], v.get("name"), "SKIP", "尾盘分级过滤:%s" % sf["reason"])
            continue
        cb = strategy.can_buy(q, v["code"])
        if not cb["ok"]:
            gate.record(v, "TAIL_SKIP", 0, "尾盘买不进:" + cb["reason"])
            _track(v["code"], v.get("name"), "SKIP", "尾盘买不进:%s" % cb["reason"])
            continue
        # 同开仓通道：数据库层持仓幂等（不依赖 risk_state 文件）
        try:
            held = [p for p in (broker.positions(open_only=True) or [])
                    if p.get("code") == v["code"]]
        except Exception:
            held = []
        if held:
            _track(v["code"], v.get("name"), "SKIP",
                   "持仓幂等：已持有未平仓，尾盘不重复建仓")
            _log("尾盘买入跳过 %s：已持仓" % v["code"])
            continue
        # Batch3 #13：买入区间精修（趋势票尤其需要，避免追在买区上沿）
        if data is not None:
            _zv, _zw, _zstop = refine_buy_zone(v, quote, data)
            if _zv != "BUY":
                gate.record(v, "WATCH", 0, "尾盘区间精修:" + _zw)
                _track(v["code"], v.get("name"), "WATCH", "尾盘区间精修:" + _zw)
                continue
            if _zstop:
                v = dict(v, stop=_zstop)
        # 2026-09-03：与开仓通道统一仓位口径（按评级 grade_pct 定单票目标仓位，允许强信号集中）
        _gp = gate.cfg.get("grade_pct") or {"A": 0.65, "B": 0.55, "T": 0.50, "C": 0.30}
        _pct = _gp.get(sf["grade"], 0.25)
        if total:
            amount = int(total * _pct * cut)
            try:
                _cash = float((bal or {}).get("cash") or 0)
            except Exception:
                _cash = 0
            amount = int(min(amount,
                             gate.cfg["max_trade_amount"],
                             total * gate.cfg["max_position_pct"],
                             _cash * 0.95 if _cash > 0 else amount))
        else:
            amount = int(gate.cfg["max_trade_amount"] * _pct * cut)
        # Batch3 #13：总仓位系数（热度/情绪退潮时压减新仓金额）
        if data is not None:
            _pc = position_cap(data)
            if _pc < 1.0:
                amount = int(amount * _pc)
        cur_pos = len(broker.positions(open_only=True) or []) if hasattr(broker, "positions") else 0
        chk = gate.check(v, total, cur_pos)
        if not chk["ok"]:
            if "幂等" in chk["reason"]:
                continue  # 早盘已买，正常
            gate.record(v, "REJECT", 0, "尾盘风控:" + chk["reason"])
            _track(v["code"], v.get("name"), "SKIP", "尾盘风控拒绝:%s" % chk["reason"])
            continue
        price = q.get("price") or 0
        if price <= 0:
            continue
        _act_notify(cfg,
                    "🌇 尾盘确认买入 %s(%s) [%s级]" % (v["name"], v["code"], sf["grade"]),
                    "**尾盘确认理由**：%s\n\n- 开盘 %.2f%%｜14:45 现价 %.2f（较开盘 +%.2f%% 微红横盘）\n"
                    "- 实证依据：高开+微红横盘桶次日 +3.01%%/红盘率 62.9%%（14个月全正）\n"
                    "- 金额 %d 元（占总资产约 %.1f%%）｜环境：%s"
                    % (v["reason"], v.get("open_gap") or 0, price,
                       v.get("day_fade") or 0, amount,
                       100.0 * amount / total if total else 0, mkt.get("reason", "")))
        r = broker.buy_limit(v["code"], price, amount, sig=dict(
            v, reason="尾盘确认｜%s" % v["reason"]))
        ok = "✓" if r.get("ok") else "✗ %s" % r.get("reason")
        gate.record(v, "BUY", amount, "尾盘:" + ok)
        if r.get("ok"):
            n_buy += 1
            lines.append("- **尾盘BUY %s**(%s) %s 开盘%.2f%% 现价%.2f %d 元"
                         % (v["name"], v["code"], sf["grade"],
                            v.get("open_gap") or 0, price, amount))
        else:
            _track(v["code"], v.get("name"), "SKIP", "尾盘委托失败:%s" % r.get("reason"))
    return lines, n_buy


def _in_trading_window():
    """是否在 A 股连续竞价时段（09:30-11:30 / 13:00-15:00）。
    盘中巡逻通道的时段闸——非交易时段调用直接跳过，不浪费 CI 时长。"""
    hm = time.strftime("%H:%M")
    return ("09:30" <= hm <= "11:30") or ("13:00" <= hm <= "15:00")


def run_scan(cfg, force=False):
    """盘中巡逻通道（2026-08-31 用户需求：模拟盘不只三个时点，全时段都可操作）。

    在交易时段内被反复触发（executor.yml 每 30 分钟一轮 cron）：
      A. 持仓卖出裁决：复用 sell_decision 全规则（断板卖/高开低走锁定/日内+5%
         落袋/-3% 止损/3 日清仓）——盘中触发比等 14:45 少承受一段回撤；
      B. 当日买入炸板保护：今日买入的票若盘中炸板（现价较开盘跌 ≥3%）即时止损
         （与 tailgate 深亏止损同口径，但时点提前到盘中任意时刻）；
      C. 熔断监控：_daily_loss_check 每轮喂组合回撤，越线立即熔断拦截后续开仓。

    降噪原则：无动作轮次只写 CI 日志留痕，不推送（推送杂乱是用户明确反对的）；
    有 SELL 成交才推「盘中巡逻回报」。开仓不在巡逻轮做——竞价决策线是开盘时点
    信号（gap 以开盘价计），盘中重跑会拿现价当开盘价误判，入场仍由 09:25 通道
    与 14:45 尾盘确认两个回测过的时点负责。
    """
    acc = cfg.get("account") or {}
    if not acc.get("user_id") or not acc.get("passwd"):
        return
    if not force and not _in_trading_window():
        _log("巡逻：非连续竞价时段（%s），跳过" % time.strftime("%H:%M"))
        return
    ok, why = is_trading_now(force=force)
    if not ok:
        _log("非交易日，巡逻跳过：%s" % why)
        return
    # 幂等降频：巡逻允许一天多轮，但 10 分钟内的重复触发直接跳过（省 CI 时长、
    # 也避免同一笔持仓在两轮里被重复裁决/重复推送）
    if not force and _ledger_done("scan", within_sec=600):
        _log("巡逻降频：距上一轮 <10 分钟，跳过")
        return
    try:
        _run_scan_inner(cfg)
        _ledger_mark("scan")
    finally:
        exec_state_save()


def _run_scan_inner(cfg):
    broker, mode = pick_broker(cfg)
    _log("=" * 30 + " 盘中巡逻 %s " % time.strftime("%H:%M") + "=" * 30)
    try:
        poss = broker.positions(open_only=True)
    except Exception as e:
        _log("巡逻：持仓读取失败：%r" % e)
        return
    if not poss:
        _log("巡逻：无持仓，仅做熔断监控")
        _daily_loss_check(broker, cfg, label="盘中巡逻")
        return

    codes = [p["code"] for p in poss]
    try:
        quote = realtime_quote(codes)
    except Exception as e:
        _log("巡逻：行情失败（本轮跳过）：%r" % e)
        return
    if not quote:
        _log("巡逻：行情为空，本轮跳过")
        return

    today = time.strftime("%Y-%m-%d")
    act_lines, n_sold = [], 0
    for p in poss:
        q = quote.get(p["code"]) or {}
        klines = []
        try:
            klines = strategy._tencent_kline(p["code"], n=12)
        except Exception as e:
            _log("巡逻：K线失败 %s：%r" % (p["code"], e))
        try:
            dec = strategy.sell_decision(p, q, klines)
        except Exception as e:
            _log("巡逻：策略异常 %s：%r" % (p["code"], e))
            continue
        pnl_pct = ((q.get("price") / p["avg_price"] - 1) * 100) \
            if (q and q.get("price") and p.get("avg_price")) else None
        # 2026-09-01 T+1 修复：今日买入的票（buy_date==today）已由 sell_decision
        # 顶部 T+1 守卫锁住（返回 HOLD），此处的「炸板保护」若对今日买入票强卖，
        # 就是 T+1 违规（用户实证：楚天龙/勤上股份 09:26 买入当日被卖）。
        # 原逻辑：今日买入票炸板（较开盘跌≥3%）→ 即时止损——已移除（A 股 T+1
        # 当日买不可卖）。炸板票最早明日按 sell_decision 规则裁决。
        if dec["verdict"] == "SELL" and dec.get("price"):
            cs = strategy.can_sell(q, p["code"])
            if not cs["ok"]:
                if hasattr(broker, "record_decision"):
                    broker.record_decision(p["code"], "HOLD",
                                           "巡逻拟卖被拒顺延：%s" % cs["reason"],
                                           p.get("name") or "", dec["price"], pnl_pct)
                act_lines.append("- ⛔ %s(%s) 拟卖被拒：%s"
                                 % (p.get("name"), p["code"], cs["reason"]))
                continue
            _act_notify(cfg,
                        "🔔 操作前确认：盘中卖出 %s(%s)" % (p.get("name"), p["code"]),
                        "**卖出理由**：%s\n\n- 现价 %.2f｜成本 %.2f｜浮盈 %s\n- 触发通道：盘中巡逻（%s 北京时间）"
                        % (dec["reason"], dec["price"], p["avg_price"],
                           ("%.2f%%" % pnl_pct) if pnl_pct is not None else "—",
                           time.strftime("%H:%M")))
            r = broker.sell_limit(p["code"], dec["price"], sig={
                "name": p.get("name"), "reason": dec["reason"], "source": "scan"})
            if r.get("ok"):
                n_sold += 1
                act_lines.append("- **SELL %s**(%s) @%.2f %+.2f%%｜%s"
                                 % (p.get("name"), p["code"], r["price"],
                                    r["pnl_pct"], dec["reason"]))
                _log("巡逻卖出 %s：%s" % (p["code"], dec["reason"]))
            else:
                act_lines.append("- %s(%s) 卖出失败：%s"
                                 % (p.get("name"), p["code"], r.get("reason")))
        else:
            if hasattr(broker, "record_decision"):
                broker.record_decision(p["code"], "HOLD",
                                       "巡逻复核：%s" % dec["reason"],
                                       p.get("name") or "", dec.get("price") or 0, pnl_pct)

    # 熔断监控每轮必跑（含无动作轮）
    pnl = _daily_loss_check(broker, cfg, label="盘中巡逻")

    if n_sold:
        _notify(cfg, "🛰 盘中巡逻回报（卖出 %d 笔）" % n_sold,
                "**%s 北京时间盘中巡逻**\n\n%s" % (time.strftime("%H:%M"),
                                                  "\n".join(act_lines)))
    else:
        _log("巡逻：本轮无动作（持仓 %d 笔，组合回撤 %s）"
             % (len(poss), ("%.2f%%" % pnl) if pnl is not None else "n/a"))


def run_tail(cfg, force=False):
    """14:45 尾盘确认通道（2026-08-30 定稿）——两条子通道合一：

    A. 持仓管理（run_tailgate，只管「今日买入」的持仓）：
       深亏(≤-3% vs 开盘)→尾盘止损（14个月11个月次日为负）；
       微红(+0~2%)→确认持有（最强过夜形态）；中性区间不干预。
    B. 尾盘入场（_tail_buys，late_gate）：高开≥2% + 微红横盘的信号票，
       14:45≈收盘价买入（该口径次日 +3.01%/62.9%，14个月全正），半仓。
       深亏/强拉桶明确放弃；早盘已买票被 RiskGate 幂等拒绝。
    老持仓由次日早盘 sell_decision 全权裁决，避免双重决策。
    """
    acc = cfg.get("account") or {}
    if not acc.get("user_id") or not acc.get("passwd"):
        _log("未配置 account，退出")
        return
    ok, why = is_trading_now(force=force)
    if not ok:
        _log("非交易日，尾盘通道跳过：%s" % why)
        return
    # 幂等守卫：尾盘通道当日只做一次
    if not force and _ledger_done("tail"):
        _log("今日尾盘通道已执行（任务账本命中），跳过重复触发")
        return
    try:
        data_ok = _run_tail_inner(cfg, force=force)
        if data_ok:
            _ledger_mark("tail")
        else:
            # 2026-09-01：同 run_once 语义——尾盘入场数据拉取失败不记账，
            # 14:40-15:09 窗口内的冗余 dispatch 自动重试补入场。
            _log("⚠ 尾盘数据拉取失败：本轮不记任务账本（冗余触发将自动重试）")
    finally:
        exec_state_save()


def _run_tail_inner(cfg, force=False):
    """返回 data_ok：True=尾盘入场数据拉取成功；False=失败（不记 tail 账本）。"""
    broker, mode = pick_broker(cfg)
    # 2026-09-01 修复（致命，与 _run_once_inner 08-31 同款）：本函数此前直接引用
    # acc["user_id"]，但 acc 只在 run_tail 作用域 → 恒抛 NameError → 尾盘入场
    # 通道的数据拉取从未成功过（14:45 微红横盘买入 0 笔的根因）。
    acc = cfg.get("account") or {}
    _log("=" * 30 + " 14:45 尾盘确认通道 " + "=" * 30)

    # A. 持仓管理
    hold_lines, n_act = run_tailgate(broker, mode, cfg)

    # B. 尾盘入场
    buy_lines, n_buy = [], 0
    mkt = {"mode": "NORMAL", "reason": "线上数据未取到"}
    data_ok = False
    try:
        data = fetch_user_data(acc["user_id"], acc["passwd"])
        # 同开仓通道：拒绝用过期数据入场（云端托管下 build/部署失败的自保闸门）
        assert_data_fresh(data, force=force)
        _log("线上数据日期：%s（新鲜度校验通过）" % (data_date(data) or "未知"))
        sigs = extract_signals(data)
        mkt = market_gate(data)
        buy_lines, n_buy = _tail_buys(broker, cfg, sigs, mkt, data)
        data_ok = True
    except Exception as e:
        _log("✗ 尾盘入场数据拉取失败：%r" % e)
        buy_lines = ["- 数据拉取失败：%r" % e]

    # 当日亏损熔断（尾盘复查，2026-08-31 补链）
    _daily_loss_check(broker, cfg, label="尾盘通道")

    out = (["## 尾盘确认（14:45）环境：%s" % mkt.get("mode"),
            "- %s" % mkt.get("reason", ""), "",
            "## 持仓管理"] + hold_lines +
           ["", "## 尾盘入场（%d 笔）" % n_buy] + (buy_lines or ["- 无"]))
    for ln in out:
        _log(ln)
    # 2026-09-01 用户推送策略：尾盘通道不是固定条——有动作（止损/入场成交）才推，
    # 无动作只写日志；固定 2 条 = 早盘回报 + 下午盘复盘。失败轮同理只发每日
    # 1 次的 dedup 告警（重试期间不重复轰炸）。
    if data_ok and (n_act or n_buy):
        _notify(cfg, "尾盘确认回报（止损%d 买%d）" % (n_act, n_buy), "\n".join(out))
    elif not data_ok:
        _act_notify(cfg, "⚠ 尾盘数据拉取失败（将自动重试）",
                    "尾盘入场数据拉取失败，本轮不记账，窗口内冗余触发自动重试。\n\n%s"
                    % "\n".join(out), dedup_key="tail_fetch_fail")
    else:
        _log("尾盘通道无动作（止损0 买%d），不推送（降噪）" % n_buy)
    exec_state_save()
    return data_ok


def run_summary():
    import broker_sim
    b = broker_sim.SimBroker()
    print(b.summary())
    print()
    print("== 持仓中 ==")
    for r in b.con.execute("SELECT buy_date,code,name,buy_price,volume,streak "
                           "FROM sim_positions WHERE sell_date IS NULL ORDER BY buy_date"):
        print("  %s %s %s 成本%.2f %d股 st=%s" % (r[0], r[1], r[2], r[3], r[4], r[5]))
    print()
    print("== 最近平仓 ==")
    for r in b.con.execute("SELECT buy_date,code,name,buy_price,sell_date,sell_price,pnl_pct,"
                           "sell_reason FROM sim_positions WHERE sell_date IS NOT NULL "
                           "ORDER BY sell_date DESC LIMIT 20"):
        print("  %s %s %s %.2f→%.2f (%+.2f%%) %s" % (r[0], r[1], r[2], r[3], r[5], r[6], r[7]))


def run_report(month: str = None):
    """月度盈亏报告：累计收益/已实现/浮盈/胜率/最大单笔盈亏/全流水。"""
    import broker_sim
    b = broker_sim.SimBroker()
    con = b.con
    month = month or time.strftime("%Y-%m")

    rows = con.execute(
        "SELECT buy_date,code,name,buy_price,volume,sell_date,sell_price,pnl_pct,sell_reason "
        "FROM sim_positions ORDER BY buy_date").fetchall()
    in_month = [r for r in rows if (r[0] or "").startswith(month)]
    closed = [r for r in in_month if r[5]]
    holding = [r for r in in_month if not r[5]]

    bal = b.balance()
    init = broker_sim._initial_cash()
    wins = [r for r in closed if (r[7] or 0) > 0]
    win_rate = len(wins) * 100.0 / len(closed) if closed else 0
    pnls = [r[7] for r in closed if r[7] is not None]
    best = max(pnls) if pnls else 0
    worst = min(pnls) if pnls else 0
    realized_pct = sum(pnls)

    print("=" * 62)
    print(" 模拟盘月度报告 %s" % month)
    print("=" * 62)
    print(" 初始资金      : %s 元" % format(int(init), ","))
    print(" 当前总资产    : %s 元（%+.2f%%）" % (format(int(bal["total"]), ","),
                                              (bal["total"] / init - 1) * 100))
    print(" 可用现金      : %s 元" % format(int(bal["cash"]), ","))
    print(" 持仓市值      : %s 元" % format(int(bal["market_value"]), ","))
    print("-" * 62)
    print(" 本月开仓      : %d 笔（已平仓 %d / 持仓中 %d）" % (len(in_month), len(closed), len(holding)))
    print(" 胜率          : %.1f%%（%d/%d）" % (win_rate, len(wins), len(closed)))
    print(" 平均盈亏      : %+.2f%%" % (sum(pnls) / len(pnls) if pnls else 0))
    print(" 最佳/最差单笔 : %+.2f%% / %+.2f%%" % (best, worst))
    print(" 本月累计盈亏  : %+.2f%%（逐笔算术和，费前）" % realized_pct)
    print("-" * 62)
    print(" 全部流水：")
    for r in rows:
        tag = "✓平仓" if r[5] else "·持仓"
        print("  [%s] %s %s(%s) 成本%.2f %d股 %s %s 盈亏%s %s" % (
            tag, r[0], r[2], r[1], r[3], r[4],
            ("→卖出%s" % r[5]) if r[5] else "       ",
            ("@%.2f" % r[6]) if r[6] else "     ",
            ("%+.2f%%" % r[7]) if r[7] is not None else "  --  ",
            r[8] or ""))
    # 逐笔交易明细
    print("-" * 62)
    print(" 委托流水（sim_trades）：")
    for r in con.execute("SELECT date,code,name,action,price,volume,amount,verdict_reason "
                         "FROM sim_trades ORDER BY ts"):
        print("  %s %s %s %s %.2f × %d = %.0f 元  %s" % (
            r[0], r[3], r[2], r[1], r[4], r[5], r[6], (r[7] or "")[:40]))


_LOSS_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state",
                                "loss_streak.json")

# 明日竞价关注清单（2026-08-31 升级）：当日留痕里 st≥3 的高度票，
# 复盘时写入、次日 09:25 开仓通道前读取并即时推送提醒。
# 依据：高度溢价单调（st=1→8 胜率 55.6%→82.4%），当日因低开/分级/风控
# 没上车的强趋势票，次日竞价给好开价就是二次入场机会。
_AUCTION_WATCH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "state", "auction_watch.json")


def _auction_watch_save(date, items):
    """写入明日竞价关注清单（覆盖式，每个交易日一份）。"""
    try:
        os.makedirs(os.path.dirname(_AUCTION_WATCH_PATH), exist_ok=True)
        with open(_AUCTION_WATCH_PATH, "w", encoding="utf-8") as f:
            json.dump({"date": date, "items": items}, f, ensure_ascii=False)
    except Exception as e:
        _log("竞价关注清单写入失败：%r" % e)


def _auction_watch_load():
    """读取竞价关注清单。

    语义：清单由「昨日复盘」写入（date=复盘当日），供「今日早盘」消费——
    所以校验 date != 今日（而非 == 今日），且消费成功后由调用方删除文件，
    防止陈旧清单跨多日重复推送。非交易日序列（周五复盘→周一早盘）天然兼容。
    """
    try:
        with open(_AUCTION_WATCH_PATH, encoding="utf-8") as f:
            st = json.load(f)
        if st.get("date") and st.get("date") != time.strftime("%Y-%m-%d"):
            return st.get("items") or []
    except Exception:
        pass
    return []


def _auction_watch_consume():
    """清单消费完毕后删除（best-effort；删除失败由 dedup_key 同日幂等兜底）。"""
    try:
        os.remove(_AUCTION_WATCH_PATH)
    except OSError:
        try:
            os.replace(_AUCTION_WATCH_PATH, _AUCTION_WATCH_PATH + ".stale")
        except Exception:
            pass
    except Exception:
        pass


def _loss_streak_update(day_pct):
    """连亏状态持久化：返回 (streak, yesterday_pct)。盈利清零。"""
    st = {}
    try:
        with open(_LOSS_STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    yest_pct = st.get("last_pct")
    if day_pct < 0:
        streak = (st.get("streak") or 0) + 1
    else:
        streak = 0
    out = {"streak": streak, "last_pct": day_pct,
           "updated": time.strftime("%Y-%m-%d")}
    try:
        os.makedirs(os.path.dirname(_LOSS_STATE_PATH), exist_ok=True)
        with open(_LOSS_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    return streak, yest_pct


def _loss_review_section(b, ds, cfg):
    """盈利/亏损都出总结；亏损日附加归因 + 明日操作方案（用户 2026-08-30 拍板）。

    归因维度（全部来自当日留痕，不猜）：
      1. 平仓原因分布（断板卖/止损/清仓期到/落袋）
      2. 被拒与放弃笔数（纪律执行质量）
      3. 连亏状态（跨日持久化）：连亏 2 日 → 降半仓，连亏 3 日 → 暂停开新仓 1 日
      4. 买入纪律自检：当日买入笔的决策线依据回看
    明日方案输出为可直接执行的纪律条目。
    """
    day_pct = ds.get("day_realized_pct") or 0
    streak, yest_pct = _loss_streak_update(day_pct)
    L = []

    if day_pct >= 0:
        # 盈利日：简要固化「做对了什么」，保持策略一致性
        wins = [c for c in (ds.get("closed") or []) if (c.get("pnl_pct") or 0) > 0]
        if wins:
            best = max(wins, key=lambda c: c["pnl_pct"])
            L.append("## ✅ 盈利固化")
            L.append("- 最佳平仓：%s(%s) %+.2f%%｜%s"
                     % (best.get("name"), best.get("code"), best["pnl_pct"],
                        (best.get("sell_reason") or "")[:50]))
            reasons = [c.get("sell_reason") or "" for c in wins]
            if any("断板" in r for r in reasons):
                L.append("- 断板开盘卖纪律有效（回测拖到 T+2 平均 -1.18%），明日继续执行")
        L.append("- 当前连胜状态：今日%s，保持既有分级与决策线，不因盈利放松门槛"
                 % ("盈利 %+.2f%%" % day_pct))
        return "\n".join(L)

    # ---- 亏损日深度归因 ----
    L.append("## 📉 亏损归因 + 明日方案")
    closed = ds.get("closed") or []
    lossers = [c for c in closed if (c.get("pnl_pct") or 0) < 0]
    if lossers:
        worst = min(lossers, key=lambda c: c["pnl_pct"])
        L.append("- 最差平仓：%s(%s) %+.2f%%｜原因：%s"
                 % (worst.get("name"), worst.get("code"), worst["pnl_pct"],
                    (worst.get("sell_reason") or "")[:60]))
        # 平仓原因聚类
        buckets = {}
        for c in lossers:
            r = c.get("sell_reason") or "其他"
            for key in ("断板", "止损", "清仓", "高开低走", "落袋", "尾盘"):
                if key in r:
                    buckets[key] = buckets.get(key, 0) + 1
                    break
            else:
                buckets["其他"] = buckets.get("其他", 0) + 1
        L.append("- 亏损笔原因分布：%s"
                 % "、".join("%s×%d" % (k, v) for k, v in
                             sorted(buckets.items(), key=lambda kv: -kv[1])))
    rej = ds.get("rejects") or []
    if rej:
        L.append("- 被拒留痕 %d 条（可成交性/风控拦截生效，属正常保护）" % len(rej))
    # 连亏纪律阶梯
    if streak >= 3:
        L.append("- ⛔ **连亏 %d 日 → 明日暂停开新仓**（只做持仓卖出裁决，空仓等待环境修复）" % streak)
    elif streak == 2:
        L.append("- ⚠️ **连亏 %d 日 → 明日新仓金额减半**（caution 模式），只做 A/B 级" % streak)
    else:
        L.append("- 连亏 %d 日（首亏）：维持正常仓位，但明日只做竞价决策线通过的票" % streak)
    # 落袋纪律提醒（recattr 实证：亏损票 51% 曾冲高≥2%）
    L.append("- 落袋纪律：回测显示亏损票 51.2% 曾冲高≥2%——持仓浮盈达 +2% 先减半仓锁定")
    # 决策线提醒
    L.append("- 竞价纪律（明日严格执行）：高开≥2%跟进（st=2 需 ≥5%）/ 低开≤-2%放弃 / 平开观望")
    return "\n".join(L)


def run_review(cfg=None, push=True, force=False):
    """当日复盘总结（收盘后 15:30 左右跑）：
    1. 汇总当日成交/平仓盈亏/被拒记录/总资产
    2. 写 tools/executor/sim_review.json（build.py 读它生成网站「模拟盘」模块）
    3. PushPlus 推送「模拟盘操作+当日复盘」

    2026-08-31 板式重构（用户要求：推送比较杂乱）：
      · 持仓明日计划只写留痕、不再逐笔操作前推送（此前每笔持仓会额外发一条
        「⏸ 持有」推送，复盘时又整段重发一遍 → 同一信息轰炸 3 次）
      · 复盘正文改分区结构：总览 → 今日操作 → 明日计划 → 归因，一屏读完
    """
    cfg = cfg or load_cfg()
    if mode_check_no_sim(cfg):
        return
    # 2026-09-03 修复：check_window=False——复盘是只读汇总+明日计划，不下单，
    # 15:32 收盘后运行不应被「禁止下单」时段闸挡掉（该闸导致 9/1 后复盘全灭）。
    ok, why = is_trading_now(force=force, check_window=False)
    if not ok:
        _log("非交易日，复盘跳过：%s" % why)
        return None
    # 幂等守卫：复盘当日只推一次（重复复盘 = 同一份战绩反复轰炸手机）
    if not force and _ledger_done("review"):
        _log("今日复盘已执行（任务账本命中），跳过重复推送")
        return None
    # exec_state_restore 已在 main() 入口统一调用（2026-08-31）
    b = broker_sim.SimBroker()
    ds = b.day_summary()
    bal = ds["balance"]
    init = broker_sim._initial_cash()
    total_pct = (bal["total"] / init - 1) * 100 if init else 0

    # ---- 明日持仓计划：只算留痕，不逐笔推送（fix：重复轰炸）----
    holding = b.positions(open_only=True)
    holding_plans = []
    hq = {}
    if holding:
        try:
            hq = realtime_quote([p["code"] for p in holding])
        except Exception:
            hq = {}
    for p in holding:
        q = hq.get(p["code"]) or {}
        try:
            kl = strategy._tencent_kline(p["code"], n=12)
        except Exception:
            kl = []
        try:
            dec = strategy.sell_decision(p, q, kl)
        except Exception:
            dec = {"verdict": "HOLD", "reason": "计划生成异常"}
        pnl_pct = ((q.get("price") / p["avg_price"] - 1) * 100) \
            if (q and q.get("price") and p.get("avg_price")) else None
        plan = ("✅ 继续持有：%s" % dec["reason"]) if dec["verdict"] == "HOLD" \
            else ("⚠️ 明日倾向卖出：%s" % dec["reason"])
        holding_plans.append({
            "code": p["code"], "name": p["name"],
            "avg_price": p["avg_price"],
            "volume": p.get("volume"),
            "price": q.get("price"),
            "market_value": round((q.get("price") or 0) * (p.get("volume") or 0), 0)
            if (q.get("price") and p.get("volume")) else None,
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "verdict": dec["verdict"], "plan": plan,
        })

    # ---- 组装推送文本（分区板式）----
    verdict = "今日盈利 ✅" if ds["day_realized_pct"] > 0 else (
        "今日亏损 ❌" if ds["day_realized_pct"] < 0 else "今日持平")
    lines = ["**总资产 %.0f 元（初始 %.0f，累计 %+.2f%%）｜当日 %+.2f%% %s**"
             % (bal["total"], init, total_pct, ds["day_realized_pct"], verdict),
             "- 可用现金 %.0f ｜ 持仓市值 %.0f ｜ 持仓 %d 笔"
             % (bal["cash"], bal["market_value"], len(holding))]

    # 区块1：今日成交（合并为一行区标，逐笔只留核心字段）
    if ds["trades"]:
        lines.append("")
        lines.append("**今日操作（%d 笔）**" % len(ds["trades"]))
        for t in ds["trades"]:
            icon = "🟢买入" if t["action"] == "BUY" else "🔴卖出"
            lines.append("- %s **%s**(%s) %.2f×%d股 %.0f元｜%s"
                         % (icon, t["name"], t["code"], t["price"], t["volume"],
                            t["amount"], (t["reason"] or "")[:50]))
    else:
        lines.append("")
        lines.append("**今日无成交**（纪律：没有信号就不动，也是操作）")

    # 区块2：当日平仓盈亏
    if ds["closed"]:
        lines.append("")
        lines.append("**当日平仓（%d 笔）**" % len(ds["closed"]))
        for c in sorted(ds["closed"], key=lambda x: -(x.get("pnl_pct") or 0)):
            lines.append("- %s(%s) %+.2f%%｜%s" % (c["name"], c["code"],
                                                   c["pnl_pct"], c["sell_reason"]))

    # 区块3：明日计划（持仓 + 操作倾向，收盘一次性给出，替代盘中逐笔轰炸）
    if holding_plans:
        lines.append("")
        lines.append("**明日计划（持仓 %d 笔）**" % len(holding_plans))
        for hp in holding_plans:
            _mv = (" ｜ 持仓%d股·%.0f元" % (hp.get("volume") or 0, hp.get("market_value") or 0)) \
                if hp.get("market_value") else ""
            lines.append("- %s(%s) 成本%.2f 浮盈%s｜%s%s"
                         % (hp["name"], hp["code"], hp["avg_price"],
                            ("%.2f%%" % hp["pnl_pct"]) if hp["pnl_pct"] is not None else "—",
                            hp["plan"], _mv))

    # 区块3.5：明日竞价关注清单（2026-08-31 升级）
    # 当日因低开/平开/分级/风控没上车的 st≥3 高度票——高度溢价单调
    # （st=1→8 胜率 55.6%→82.4%），次日竞价给好开价就是二次入场机会。
    # 同时写入 auction_watch.json 跨 run 持久化，次日 09:25 开仓通道前即时提醒。
    watch_items = []
    for dc in (ds.get("decisions") or []):
        if dc.get("action") not in ("WATCH", "SKIP"):
            continue
        m = re.search(r"st(\d+)", dc.get("reason") or "")
        st_n = int(m.group(1)) if m else 0
        if st_n >= 3:
            watch_items.append({"code": dc["code"], "name": dc.get("name") or "",
                                "streak": st_n, "reason": (dc.get("reason") or "")[:60]})
    if watch_items:
        # 按高度降序、同高度按代码去重，最多 5 条
        seen_c, uniq = set(), []
        for it in sorted(watch_items, key=lambda x: -x["streak"]):
            if it["code"] in seen_c:
                continue
            seen_c.add(it["code"])
            uniq.append(it)
        uniq = uniq[:5]
        lines.append("")
        lines.append("**🎯 明日竞价关注（今日未上车的高度票 %d 只）**" % len(uniq))
        for it in uniq:
            lines.append("- %s(%s) st=%d｜%s"
                         % (it["name"], it["code"], it["streak"], it["reason"]))
        lines.append("- 竞价纪律：高开≥2%跟进 / st=2 需≥5% / 低开≤-2%放弃 / 平开观望")
        _auction_watch_save(ds["date"], uniq)
        _log("明日竞价关注清单已写入：%d 只" % len(uniq))
    # 2026-08-31 升级：竞价关注清单同时写入 sim_review（此前只进推送+state json，
    # 网站模拟盘页拿不到 → 补上「明日竞价关注」卡片的数据源）
    ds["auction_watch"] = uniq if watch_items else []

    # 区块4：被拒/放弃留痕（压缩为一行汇总 + 最多 3 条明细）
    n_skip = sum(1 for dc in (ds.get("decisions") or [])
                 if dc.get("action") in ("WATCH", "SKIP", "FREEZE"))
    if ds["rejects"] or n_skip:
        lines.append("")
        lines.append("**纪律留痕**：被拒 %d 条、观望/放弃 %d 条（明细见网站模拟盘页）"
                     % (len(ds["rejects"]), n_skip))
        for rj in ds["rejects"][:3]:
            lines.append("- ⛔ %s %s(%s)：%s" % (rj["action"], rj["name"], rj["code"],
                                                 rj["reason"][:60]))

    # 区块5：归因 + 明日方案（盈利固化/亏损归因 + 连亏纪律）
    try:
        extra = _loss_review_section(b, ds, cfg)
        if extra:
            lines.append("")
            lines.append(extra)
    except Exception as e:
        _log("归因总结生成失败（不影响复盘）：%r" % e)

    # 2026-08-31 升级：调度缺口自检——「今天没成交」必须说清是纪律空仓还是根本没跑。
    # 背景：executor cron 被 GitHub 漏投递（10 个时点只送达 1 个），当天 0 成交 0 持仓，
    # 复盘却只报「今日持平」，用户无从判断模拟盘是没信号还是没执行。
    try:
        miss = _ledger_missing()
        if miss:
            name = {"now": "09:25 开仓通道", "scan": "盘中巡逻"}
            lines.append("")
            lines.append("**⚠ 调度缺口**：今日 %s 未执行（cron 未送达或容器故障），"
                         "模拟盘的「无成交」不代表无信号——请核对 Actions 运行记录。"
                         % "、".join(name[m] for m in miss))
    except Exception as e:
        _log("调度缺口自检失败（不影响复盘）：%r" % e)

    text = "\n".join(lines)
    _log(text)
    if push:
        _notify(cfg, "📊 模拟盘复盘 %s（%+.2f%%）%s" % (ds["date"], ds["day_realized_pct"], verdict),
                text)
        _ledger_mark("review")     # 推送成功才记账，失败允许下轮重推

    # ---- 写 sim_review.json（网站模块数据源；历史按日累积）----
    try:
        hist = {}
        if os.path.exists(REVIEW_PATH):
            try:
                hist = json.load(open(REVIEW_PATH, encoding="utf-8"))
            except Exception:
                hist = {}
        hist["days"] = hist.get("days") or {}
        hist["days"][ds["date"]] = {
            "date": ds["date"],
            "total": bal["total"], "cash": bal["cash"], "market_value": bal["market_value"],
            "total_pct": round(total_pct, 2),
            "day_realized_pct": ds["day_realized_pct"],
            "trades": ds["trades"], "closed": ds["closed"], "rejects": ds["rejects"],
            "decisions": ds.get("decisions") or [],
            "holding_plans": holding_plans,
            "auction_watch": ds.get("auction_watch") or [],
            "n_holding": len(holding),
            "summary_line": (b.summary() if hasattr(b, "summary") else ""),
        }
        # 只保留最近 120 个交易日
        keys = sorted(hist["days"].keys())
        for k in keys[:-120]:
            del hist["days"][k]
        hist["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(REVIEW_PATH, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)
        _log("复盘已写入 %s（累计 %d 个交易日）" % (os.path.basename(REVIEW_PATH), len(hist["days"])))
        # 上云：推到仓库 state/sim_review.json，CI build 时读它生成网站「模拟盘」模块
        _push_review_to_repo()
    except Exception as e:
        _log("sim_review.json 写入失败：%r" % e)
    exec_state_save()
    return ds


def _push_review_to_repo():
    """把 sim_review.json 推到仓库 state/（best-effort，失败只记日志）。

    2026-08-30 修复断链：此前只更新 tools/executor/sim_review.json，
    但推的是 state/sim_review.json（本地从未复制过去 → 推的是旧文件）。
    现在先把 REVIEW_PATH 内容写到 state/ 再推，保证网站读到最新复盘。
    EXE_NO_PUSH=1 时跳过真实推送（离线测试用；曾把测试交易数据误推上线）。"""
    if os.environ.get("EXE_NO_PUSH"):
        _log("EXE_NO_PUSH=1，跳过 sim_review 真实推送（测试模式）")
        return
    try:
        # 1) 同步内容到仓库路径
        state_dir = os.path.join(ROOT, "state")
        os.makedirs(state_dir, exist_ok=True)
        hist = json.load(open(REVIEW_PATH, encoding="utf-8"))
        days = hist.get("days") or {}
        ks = sorted(days.keys())
        for k in ks[:-60]:
            del days[k]
        hist["days"] = days
        with open(os.path.join(state_dir, "sim_review.json"), "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)
        # 2) 推送
        tools_dir = os.path.join(ROOT, "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import gh_api
        gh_api.push_files(
            "sim-review: 模拟盘每日复盘数据（executor 自动回传）", ["state/sim_review.json"])
        # push_files 内部直接 commit；无需检查返回（失败抛异常）
        _log("sim_review.json 已推送到仓库 state/")
    except SystemExit as e:
        _log("sim_review 推送失败（SystemExit）：%s" % e)
    except Exception as e:
        _log("sim_review 推送失败（不影响复盘）：%r" % e)


def mode_check_no_sim(cfg):
    """qmt 实盘模式下不写复盘文件（避免覆盖模拟盘数据）。"""
    return (cfg.get("broker") or "sim") != "sim"


def main():
    args = [a for a in sys.argv[1:]]
    force = "--force" in args          # CI workflow_dispatch 手动测试：跳过交易日判定
    args = [a for a in args if a != "--force"]
    cfg = load_cfg()
    global _LAST_CFG
    _LAST_CFG = cfg      # 供进程退出前统一推送状态告警使用
    # 2026-08-31 修复（全时段可操作 prerequisite）：状态恢复统一挪到入口。
    # 此前只有 run_review 调 exec_state_restore——CI 全新容器跑 --now/--tail
    # 会以「空账本」启动：看不到历史持仓（该卖的没卖）、RiskGate 幂等表为空
    # （同票可能重复开仓）。exec_state_restore 自带幂等（本地有 sim.db 跳过）。
    # summary/report 是只读查询，不恢复。
    if any(a in args for a in ("--now", "--tail", "--scan", "--review", "--loop")):
        exec_state_restore()   # 内置幂等：CI 无 sim.db 才拉取；本地已有则跳过
    if "--summary" in args:
        run_summary()
    elif "--report" in args:
        month = None
        for i, a in enumerate(args):
            if a == "--month" and i + 1 < len(args):
                month = args[i + 1]
        run_report(month)
    elif "--review" in args:
        run_review(cfg, force=force)
    elif "--now" in args:
        run_once(cfg, force=force)
    elif "--scan" in args:
        run_scan(cfg, force=force)
    elif "--tail" in args:
        run_tail(cfg, force=force)
    elif "--loop" in args:
        target = ((cfg.get("schedule") or {}).get("auction_time") or "09:26")
        ttarget = ((cfg.get("schedule") or {}).get("tail_time") or "14:45")
        rtarget = ((cfg.get("review") or {}).get("time") or "15:35")
        _log("常驻模式：每天 %s 执行（平仓+开仓），%s 尾盘确认通道，%s 复盘总结（Ctrl+C 退出）"
             % (target, ttarget, rtarget))
        fired = set()
        _scan = {"last": 0}   # 盘中巡逻节拍（2026-08-31 全时段可操作）
        while True:
            now = time.strftime("%H:%M")
            if now == target and "trade" not in fired:
                fired.add("trade")
                try:
                    run_once(cfg)
                except Exception as e:
                    _log("执行异常：%r" % e)
            if now == ttarget and "tail" not in fired:
                fired.add("tail")
                try:
                    run_tail(cfg)
                except Exception as e:
                    _log("尾盘通道异常：%r" % e)
            if now == rtarget and "review" not in fired and not mode_check_no_sim(cfg):
                fired.add("review")
                try:
                    run_review(cfg)
                except Exception as e:
                    _log("复盘异常：%r" % e)
            # 盘中巡逻（2026-08-31 用户需求：全时段可操作）：交易时段每 15 分钟一轮
            if _in_trading_window() and int(time.time()) - _scan.get("last", 0) >= 900:
                _scan["last"] = int(time.time())
                try:
                    run_scan(cfg)
                except Exception as e:
                    _log("巡逻异常：%r" % e)
            if now < target:  # 跨天重置
                fired.clear()
            time.sleep(5)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
    # 云端托管：把本轮积累的状态持久化告警统一 dedup 推送（用户不开电脑，
    # 看不到 CI 日志——静默失败必须变成一条推送）
    _flush_state_alerts()
