# -*- coding: utf-8 -*-
"""信息推送模块：微信(ServerChan/PushPlus/企业微信机器人) / Telegram / SMTP 邮件

配置：stock-analysis/config/notify.json 或环境变量。示例：
{
  "wechat_serverchan": {"sendkey": "SCTxxxx"},
  "wechat_pushplus":   {"token": "xxxx"},
  "wecom":             {"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"},
  "telegram":          {"bot_token": "123:abc", "chat_id": "-100xxx"},
  "email":             {"smtp_host":"smtp.qq.com","smtp_port":465,"user":"x@qq.com","pass":"授权码","to":"x@qq.com"}
}
未配置任何通道时 push() 仅打印（dry-run），不报错。
"""
import json
import os
import re
import sys
import time
import datetime
import urllib.request
import urllib.parse
import smtplib
import ssl
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
try:
    import trade_calendar
except Exception:      # 日历缺失时不阻断推送（宁可多推，不可漏推）
    trade_calendar = None

# CI runner 时区是 UTC，本机是北京时间；而看门狗解析 push_log 用的是北京日期。
# 三者必须统一，否则跨日边界（北京 00:00~08:00）会导致去重判定与看门狗判定错位。
# 全模块的"当前时间/当天"一律以北京时间为准。
_BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _bj_now():
    """北京时间的 naive datetime（去掉 tzinfo，便于与历史 ts 字符串直接比较）。"""
    return datetime.datetime.now(_BJ_TZ).replace(tzinfo=None)


def _in_anomaly_window():
    """盘中异动仅在交易时段（周一至周五 09:15–15:00 北京时间）允许推送。

    其余时间（早盘前 / 午夜 / 非交易日）一律拦截，避免被任意触发器误当成
    『盘中异动』推出去——曾出现中国时间凌晨 4 点误推盘中异动的事故：外部定时器
    / 看门狗在休市时段点火，而原代码只对『交易日』做判断（凌晨仍是交易日），
    于是把空数据当『盘中异动』发出。交易时段闸从根上堵死这类误推。"""
    now = _bj_now()
    if now.weekday() >= 5:          # 周六(5) / 周日(6) 休市
        return False
    t = now.time()
    return datetime.time(9, 15) <= t <= datetime.time(15, 0)


ROOT = store.ROOT
CFG_PATH = os.path.join(ROOT, "config", "notify.json")
DIST = os.path.join(ROOT, "dist")
# 去重账本权威位置：放在 state/ 下（构建过程绝不改写 dist/，故与构建产物解耦，
# 跨 run 续存更稳）。state.tar.gz Release 资产会随每次运行回存该文件，
# 下个 run 恢复步骤再解包出来，去重判定在多次触发（主调度/看门狗/外部定时器）间真正生效。
STATE_DIR = os.path.join(ROOT, "state")
LEDGER = os.path.join(STATE_DIR, "push_ledger.jsonl")


def _env_config():
    """从环境变量构造与 notify.json 同构的配置（云端/CI 场景，密钥走 Secrets 注入）。

    支持两种写法：
    1) NOTIFY_JSON  —— 直接给整份 JSON 字符串，结构与 config/notify.json 完全一致（推荐）
    2) 分离变量     —— 多个 key 用英文逗号或换行分隔：
       NOTIFY_WX_SENDKEY = "SCTxxx,SCTyyy"
       NOTIFY_PP_TOKEN   = "tokenA,tokenB"
    注意：历史版本这里返回的是 {"NOTIFY_WX_SENDKEY": ...} 这类扁平键，
    与下游 cfg.get("wechat_serverchan") 的读法对不上，等于环境变量根本不生效。此处修正。
    """
    raw = os.environ.get("NOTIFY_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass

    def _split(v):
        out = []
        for part in re.split(r"[,\n;]+", v or ""):
            part = part.strip()
            if part:
                out.append(part)
        return out

    cfg = {}
    sk = _split(os.environ.get("NOTIFY_WX_SENDKEY"))
    if sk:
        cfg["wechat_serverchan"] = {
            "sendkey": [{"key": k, "name": "接收人%d" % (i + 1)} for i, k in enumerate(sk)]
        }
    pp = _split(os.environ.get("NOTIFY_PP_TOKEN"))
    if pp:
        cfg["wechat_pushplus"] = {
            "token": [{"token": t, "name": "接收人%d" % (i + 1)} for i, t in enumerate(pp)]
        }
    if os.environ.get("NOTIFY_TG_TOKEN") and os.environ.get("NOTIFY_TG_CHAT"):
        cfg["telegram"] = {"token": os.environ["NOTIFY_TG_TOKEN"],
                           "chat_id": os.environ["NOTIFY_TG_CHAT"]}
    if os.environ.get("NOTIFY_WECOM"):
        cfg["wecom"] = {"webhook": os.environ["NOTIFY_WECOM"]}
    return cfg


def load_config():
    """优先级：NOTIFY_JSON / 环境变量 > config/notify.json。

    云端运行时仓库里不该存在明文 notify.json，靠 Secrets 注入；
    本地开发仍然读文件，两边互不干扰。

    末尾叠加 config/recipients_runtime.json 的按人 scope 覆盖层
    （推送中心交互编辑器的落点）。文件缺失 / 损坏 / 异常 -> 原样返回（安全阀）。
    """
    env_cfg = _env_config()
    if env_cfg:
        cfg = env_cfg
    elif os.path.exists(CFG_PATH):
        try:
            cfg = json.load(open(CFG_PATH, encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}
    return _apply_runtime_scopes(cfg)


def _apply_runtime_scopes(cfg):
    """把 config/recipients_runtime.json 的 {name: scope} 覆盖层叠加到各通道条目。

    这是「推送中心」交互编辑器（站点内 owner 改某人收什么内容）的运行时落点。
    设计铁律（用户底线：改动绝不能让明天推送出问题）：
      · 文件缺失            -> 返回原 cfg（等于全量推送，与今完全一致）
      · JSON 损坏 / 异常    -> 返回原 cfg（no-op，绝不抛错中断推送）
      · 某 name 不在覆盖层  -> 该人 scope 不变（沿用 NOTIFY_JSON 中的设定）
    覆盖层结构：{"recipients":[{"name":"我","scope":"all"}, ...]}
    scope 只接受 all/sim/prepost/none，其它值忽略（不污染配置）。
    """
    if not cfg:
        return cfg
    try:
        p = os.path.join(ROOT, "config", "recipients_runtime.json")
        if not os.path.exists(p):
            return cfg
        with open(p, encoding="utf-8") as fh:
            rt = json.load(fh)
        ov = {}
        for r in (rt.get("recipients") or []):
            if not isinstance(r, dict):
                continue
            name = (r.get("name") or "").strip()
            scope = r.get("scope")
            if name and scope in ("all", "sim", "prepost", "none"):
                ov[name] = scope
        if not ov:
            return cfg
        import copy
        c = copy.deepcopy(cfg)
        for ch in ("wechat_serverchan", "wechat_pushplus", "wecom", "telegram", "email"):
            cc = c.get(ch)
            if not isinstance(cc, dict):
                continue
            for field in ("sendkey", "sendkeys", "token", "keys"):
                items = cc.get(field)
                if not isinstance(items, list):
                    continue
                for x in items:
                    if isinstance(x, dict):
                        nm = (x.get("name") or "").strip()
                        if nm in ov:
                            x["scope"] = ov[nm]
        return c
    except Exception:
        # 任何异常都回退到原始配置，保证推送流水线不被覆盖层逻辑打断
        return cfg


def _opener(direct):
    if direct:
        # 直连：绕过可能破坏部分主机 TLS 握手的本地代理（如 127.0.0.1:10808）
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()  # 走环境变量中的代理


def _open(req, timeout):
    """先直连，失败再退回代理（兼容不同部署环境的网络出口）。"""
    last = None
    for direct in (True, False):
        try:
            with _opener(direct).open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
    raise last


def _post_json(url, payload, token=None, timeout=15):
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    return _open(req, timeout)


def _post_form(url, data, timeout=15):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode("utf-8"),
                                 headers={"User-Agent": "Mozilla/5.0"}, method="POST")
    return _open(req, timeout)


def _decode(resp_bytes):
    """把响应字节稳妥解码：优先 utf-8，失败尝中文编码，再兜底忽略。"""
    for enc in ("utf-8", "gbk", "gb2312"):
        try:
            return resp_bytes.decode(enc)
        except Exception:
            continue
    return resp_bytes.decode("utf-8", "ignore")


def _curl_post_form(url, data, timeout=15):
    """curl 兜底：本机 OpenSSL 与部分主机 TLS 协商失败时，用 curl(schannel) 直连。"""
    import subprocess, tempfile, os
    body = urllib.parse.urlencode(data)
    fd, path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        cmd = ["curl", "-sS", "--compressed", "--ssl-no-revoke", "--max-time", str(timeout),
               "--noproxy", "*", "-X", "POST", "--data-binary", "@" + path, url]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
        return _decode(r.stdout or b"")
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def _curl_post_json(url, payload, token=None, timeout=15):
    import subprocess, tempfile, os
    body = json.dumps(payload)
    fd, path = tempfile.mkstemp(suffix=".json", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        cmd = ["curl", "-sS", "--compressed", "--ssl-no-revoke", "--max-time", str(timeout),
               "--noproxy", "*", "-X", "POST", "-H", "Content-Type: application/json"]
        if token:
            cmd += ["-H", "Authorization: Bearer %s" % token]
        cmd += ["--data-binary", "@" + path, url]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
        return _decode(r.stdout or b"")
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def _with_retry(fn, fallback, timeout):
    """先直连重试（应对境外 runner 出口抖动），全部失败再退回 curl 兜底。"""
    last = None
    for _ in range(3):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(1.2)
    try:
        return fallback()
    except Exception:
        raise last


def http_post_form(url, data, timeout=15):
    return _with_retry(lambda: _post_form(url, data, timeout),
                       lambda: _curl_post_form(url, data, timeout), timeout)


def http_post_json(url, payload, token=None, timeout=15):
    return _with_retry(lambda: _post_json(url, payload, token, timeout),
                       lambda: _curl_post_json(url, payload, token, timeout), timeout)


def _iter_sendkeys(cfg):
    """把 wechat_serverchan 配置归一为多组 (label, key) 列表，兼容多种写法：
    - 单字符串：{"sendkey": "SCTxxx"}
    - 字符串列表：{"sendkey": ["SCTxxx", "SCTyyy"]}
    - 具名对象列表（推荐，便于区分接收人）：
      {"sendkey": [{"key": "SCTxxx", "name": "我"}, {"key": "SCTyyy", "name": "朋友A"}]}
    - 另支持别名字段 sendkeys: [...]（同结构）
    返回 [(label, key), ...]，无有效 key 返回 []。"""
    if not cfg:
        return []
    out = []
    for field in ("sendkey", "sendkeys"):
        v = cfg.get(field)
        if v is None:
            continue
        items = v if isinstance(v, list) else [v]
        for x in items:
            if isinstance(x, dict):
                k = (x.get("key") or x.get("sendkey") or "").strip()
                if not k:
                    continue
                name = (x.get("name") or k[:6] + "…").strip()
                out.append((name, k))
            elif isinstance(x, str) and x.strip():
                k = x.strip()
                out.append((k[:6] + "…", k))
    return out


def _iter_pushplus(cfg):
    """把 wechat_pushplus 配置归一为多组 token 列表，兼容多种写法：
    - 单字符串：{"token": "xxxx"}
    - 字符串列表：{"token": ["xxxx", "yyyy"]}
    - 具名对象列表：{"token": [{"token":"xxxx","name":"我"}, {"token":"yyyy","name":"朋友A"}]}
    推送正文不携带接收人信息（按用户要求）。"""
    if not cfg:
        return []
    v = cfg.get("token")
    if v is None:
        return []
    items = v if isinstance(v, list) else [v]
    out = []
    for x in items:
        if isinstance(x, dict):
            t = (x.get("token") or x.get("key") or "").strip()
            if t:
                out.append(t)
        elif isinstance(x, str) and x.strip():
            out.append(x.strip())
    return out


def _entry_scope(entry):
    """读取通道条目的 scope 字段（推送分级，用户需求5）。
    取值：all（全部）/ sim（仅模拟盘）/ prepost（仅盘前盘后）/ none（不接收）。
    缺省视为 all（兼容旧配置）；无效值回退 all。"""
    if isinstance(entry, dict):
        s = (entry.get("scope") or "all")
        if s in ("all", "sim", "prepost", "none"):
            return s
    return "all"


# mode → 允许接收的 scope 集合。不在集合内的 scope 不接收该 mode：
#   prepost 类：盘前/竞价/收盘/复盘/周末
#   all-only 类：盘中异动/竞价异动/恐慌/妖股/止损（仅 scope=all 接收）
# runner 模拟盘操作类推送不走 notifier.push，由 runner.py 自行按 {all, sim} 过滤。
MODE_SCOPE = {
    "preauction":   {"all", "prepost"},
    "auction":      {"all", "prepost"},
    "close":        {"all", "prepost"},
    "close_again":  {"all", "prepost"},
    "weekend":      {"all", "prepost"},
    "anomaly":      {"all"},
    "open_anomaly": {"all"},
    "panic":        {"all"},
    "yaogu":        {"all"},
    "stoploss":     {"all"},
}


def _scope_filter_channel(chan_cfg, allowed):
    """返回通道配置副本，仅保留 scope∈allowed 的条目；无匹配则移除该字段（上层视为未配置）。"""
    if not chan_cfg or not isinstance(chan_cfg, dict):
        return chan_cfg
    out = dict(chan_cfg)
    changed = False
    for field in ("sendkey", "sendkeys", "token"):
        v = chan_cfg.get(field)
        if v is None:
            continue
        items = v if isinstance(v, list) else [v]
        kept = [x for x in items if _entry_scope(x) in allowed]
        out[field] = kept
        changed = True
    if changed:
        for field in ("sendkey", "sendkeys", "token"):
            if field in out and not out[field]:
                out.pop(field)
    return out


def _scope_filter_all(cfg, allowed):
    """对整个 notify 配置按 scope 过滤（所有通道）。"""
    if not cfg:
        return cfg
    import copy
    c = copy.deepcopy(cfg)
    for ch in ("wechat_serverchan", "wechat_pushplus", "wecom", "telegram", "email"):
        if ch in c and c[ch]:
            c[ch] = _scope_filter_channel(c[ch], allowed)
    return c


def send_wechat_serverchan(cfg, title, text):
    keys = _iter_sendkeys(cfg)
    if not keys:
        return False, "未配置 sendkey"
    ok_list, fail_list = [], []
    for label, key in keys:
        # 免费档接口偶发 5xx/网络抖动/WAF 拦截页会静默吞掉整条推送；
        # 加 3 次重试 + 退避。响应必须解析为 JSON 且 code==0 才算成功——
        # 非 JSON（如 WAF 拦截页）一律视为失败重试，绝不静默当成功（曾导致丢推无据可查）。
        _done = False
        for _attempt in range(3):
            try:
                url = "https://sctapi.ftqq.com/%s.send" % key
                resp = http_post_form(url, {"title": title, "desp": text})
                try:
                    j = json.loads(resp)
                except Exception:
                    j = None
                if isinstance(j, dict) and j.get("code") == 0:
                    ok_list.append(label)
                    _done = True
                    break
                _err = "%s:%s" % (label, str(
                    (j or {}).get("message") if isinstance(j, dict) else
                    ("非JSON响应:" + resp[:40]) if resp else "空响应")[:60])
                if _attempt < 2:
                    time.sleep(3 * (_attempt + 1))   # 退避后重试
                    continue
                fail_list.append(_err)
            except Exception as e:
                if _attempt < 2:
                    time.sleep(3 * (_attempt + 1))
                    continue
                fail_list.append("%s:%r" % (label, e))
    msg = "ServerChan 成功 %d/%d（%s）" % (len(ok_list), len(keys), "、".join(ok_list) or "无")
    if fail_list:
        msg += " 失败：" + "；".join(fail_list)
    return (len(ok_list) > 0), msg


def md2html(title, text):
    """推送文本 → HTML（2026-09-01 深色模式自适应 + 语义化动作徽标 + 买点高亮）。

    此前 PushPlus 走 markdown 模板，纯文本一大段、关键动作不醒目。做真正的
    markdown→HTML 转换后走 html 模板：分区标题带色条、列表分行、动作关键字
    上色（A 股习惯：买入=红／卖出=绿／持有=蓝／观望=灰），买点/重点行高亮。

    2026-09-01 升级（用户反馈「黑乎乎看不见」）：
      · 深色模式自适应：CSS 变量 + @media(prefers-color-scheme:dark)——此前硬编码
        #222/#333/#f4f7ff/#fafafa 在手机深色模式下不可读。
      · 语义化动作徽标：每行识别 🟢买点/🔴卖出/🔵持有/⚪观望，买点/卖点行用高亮卡片。
    """
    def _esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def _bold(s):
        import re as _re
        return _re.sub(r"\*\*(.+?)\*\*", r'<strong>\1</strong>', s)

    def _action_badge(line):
        if any(k in line for k in ("SELL", "卖出", "止损", "清仓", "减仓", "割肉")):
            return ("卖出", "var(--sell)", "var(--sec-bg)")
        if any(k in line for k in ("BUY", "买入", "买点", "建仓", "加仓")):
            return ("买入", "var(--buy)", "var(--sec-bg)")
        if any(k in line for k in ("HOLD", "持有", "确认持有")):
            return ("持有", "var(--hold)", "var(--sec-bg)")
        if any(k in line for k in ("WATCH", "观望", "等", "不追")):
            return ("观望", "var(--mute)", "var(--sec-bg)")
        return (None, None, None)

    def _badge_html(txt, fg, bg):
        return ('<span style="display:inline-block;margin-right:6px;padding:1px 7px;'
                'border-radius:10px;font-size:12px;font-weight:700;color:%s;background:%s">%s</span>'
                % (fg, bg, txt))

    def _color(line):
        out = line
        for kw, css in (("买入", "var(--buy)"), ("加仓", "var(--buy)"), ("建仓", "var(--buy)"),
                        ("买点", "var(--buy)"), ("BUY", "var(--buy)"),
                        ("卖出", "var(--sell)"), ("止损", "var(--sell)"), ("清仓", "var(--sell)"),
                        ("减仓", "var(--sell)"), ("割肉", "var(--sell)"), ("SELL", "var(--sell)"),
                        ("持有", "var(--hold)"), ("HOLD", "var(--hold)"),
                        ("观望", "var(--mute)"), ("WATCH", "var(--mute)")):
            if kw in out:
                out = out.replace(
                    kw, '<span style="color:%s;font-weight:600">%s</span>' % (css, kw))
        return out

    body_css = (
        "--sans:'Segoe UI',system-ui,-apple-system,sans-serif;"
        "--c:#222;--c-sec:#555;--sec-bg:#f4f7ff;--sec-bd:#2f6fed;"
        "--divider:#eceef2;--buy:#e02020;--sell:#0a8f3c;--hold:#2f6fed;--mute:#6b7280;"
        "--hi-bg:#fff8e6;--quote-bg:#fafafa;--quote-bd:#d0d0d0;"
    )
    dark_css = (
        "@media (prefers-color-scheme:dark){"
        ".p{--c:#e6e6e6;--c-sec:#b3b3b3;--sec-bg:#1e2740;--sec-bd:#4f7df9;"
        "--divider:#2c2f36;--buy:#ff5252;--sell:#34d399;--hold:#60a5fa;--mute:#9ca3af;"
        "--hi-bg:#3a2f10;--quote-bg:#23262c;--quote-bd:#3a3f46;}"
        "}")

    parts = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        act, afg, abg = _action_badge(s)
        is_buy_pt = ("买点" in s or "买入" in s or "BUY" in s)
        is_sell_pt = ("卖出" in s or "SELL" in s or "止损" in s or "割肉" in s)
        hi = ("买点" in s or "重点" in s or "到价" in s)
        if s.startswith("## "):
            parts.append(
                '<div style="margin:12px 0 6px;padding:5px 9px;border-left:4px solid var(--sec-bd);'
                'background:var(--sec-bg);font-weight:700;font-size:15px;color:var(--c)">%s</div>'
                % _esc(s[3:]))
        elif s.startswith("> "):
            parts.append(
                '<div style="margin:6px 0;padding:4px 8px;background:var(--quote-bg);'
                'border-left:3px solid var(--quote-bd);color:var(--c-sec)">%s</div>'
                % _color(_bold(_esc(s[2:]))))
        elif s.startswith("- ") or s.startswith("* "):
            body = _color(_bold(_esc(s[2:])))
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
            elif hi:
                parts.append(
                    '<div style="margin:3px 0;line-height:1.65;background:var(--hi-bg);'
                    'padding:3px 6px;border-radius:6px">• %s</div>' % body)
            elif act:
                parts.append(
                    '<div style="margin:3px 0;line-height:1.65">%s%s</div>'
                    % (_badge_html(act, afg, abg), body))
            else:
                parts.append(
                    '<div style="margin:3px 0;line-height:1.65;color:var(--c)">• %s</div>' % body)
        elif s.startswith("|"):
            parts.append(
                '<div style="font-family:monospace;font-size:12px;color:var(--c-sec);'
                'white-space:pre-wrap">%s</div>' % _esc(s))
        elif set(s) <= set("-—= "):
            parts.append('<hr style="border:none;border-top:1px solid var(--divider);margin:10px 0">')
        else:
            body = _color(_bold(_esc(s)))
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
                    '<div style="margin:4px 0;line-height:1.65;color:var(--c)">%s</div>' % body)
    return ('<style>%s%s</style>'
            '<div class="p" style="font-family:var(--sans);font-size:14px;color:var(--c)">%s</div>'
            % (body_css, dark_css, "".join(parts)))


def send_wechat_pushplus(cfg, title, text):
    tokens = _iter_pushplus(cfg)
    if not tokens:
        return False, "未配置 token"
    url = "https://www.pushplus.plus/send"   # 2026-09-01：http 明文 → https
    ok_list, fail_list = [], []
    for token in tokens:
        sent, last_msg = False, None
        # 2026-09-01：三色 HTML 排版（md2html 转换后走 html 模板；
        # 早期直接把 markdown 塞进 html 模板会裸显符号，现在已完成真转换）
        _content = md2html(title, text)
        payload = {"token": token, "title": title, "content": _content,
                   # 可在 notify.json wechat_pushplus.template 覆盖（万一需要回退 markdown）
                   "template": cfg.get("template") or "html"}
        # topic 为群组编码，缺省走一对一推送；配置了才带上
        topic = cfg.get("topic")
        if topic:
            payload["topic"] = topic
        # 2026-09-02：业务失败（code!=200，如间歇性「服务端验证错误」）也退避重试——
        # 此前仅网络异常走 _with_retry，业务拒绝一次即丢（08:45 盘前实失败、09:26 同 token 成功=间歇性）
        for attempt in range(3):
            try:
                resp = http_post_json(url, payload)
                try:
                    import json as _json
                    j = _json.loads(resp)
                    if j.get("code") == 200:
                        sent = True
                        break
                    last_msg = str(j.get("msg", resp))[:40]
                except Exception:
                    sent = True   # 响应未解析按成功处理（保持原语义）
                    break
            except Exception as e:
                last_msg = repr(e)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
        if sent:
            ok_list.append(token[:6] + "…")
        else:
            fail_list.append("%s:%s" % (token[:6], last_msg))
    msg = "PushPlus 成功 %d/%d" % (len(ok_list), len(tokens))
    if fail_list:
        msg += " 失败：" + "；".join(fail_list)
    return (len(ok_list) > 0), msg


def send_wecom(cfg, title, text):
    wh = cfg.get("webhook")
    if not wh:
        return False, "未配置 webhook"
    http_post_json(wh, {"msgtype": "markdown", "markdown": {"content": "## %s\n%s" % (title, text)}})
    return True, "企业微信机器人已推送"


def send_telegram(cfg, title, text):
    bt = cfg.get("bot_token")
    cid = cfg.get("chat_id")
    if not bt or not cid:
        return False, "未配置 bot_token/chat_id"
    url = "https://api.telegram.org/bot%s/sendMessage" % bt
    http_post_json(url, {"chat_id": cid, "text": "%s\n\n%s" % (title, text),
                        "parse_mode": "Markdown", "disable_web_page_preview": True})
    return True, "Telegram 已推送"


def send_email(cfg, title, text):
    user, pwd = cfg.get("user"), cfg.get("pass")
    to = cfg.get("to") or user
    host = cfg.get("smtp_host") or "smtp.qq.com"
    port = int(cfg.get("smtp_port") or 465)
    if not (user and pwd and to):
        return False, "未配置邮件账号"
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = title
    msg["From"] = user
    msg["To"] = to
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    return True, "邮件已发送"


# 这些 mode 每天只应推送一次；多次触发（GitHub 主调度 + 看门狗 + 备份订阅 +
# 外部定时器）时由本去重保证不重复轰炸。盘中异动(anomaly)刻意多次推送，不在此列。
# close 与 close_again 必须分开：15:20 收盘用 "close"，20:00 复盘用 "close_again"。
# 若共用 "close"，复盘会被 once-per-day 当成“今日已推送”直接吞掉（已复现：run 58 复盘静默）。
_ONCE_PER_DAY = {"preauction", "auction", "open_anomaly", "close", "close_again", "weekend"}


def _ledger_path():
    """去重账本权威路径（state/ 下，跨 run 续存）"""
    return LEDGER


def _append_ledger(rec):
    """写一条去重账本记录：权威落 state/push_ledger.jsonl，并镜像一份到 dist/push_log.jsonl
    供看板展示（看板只读镜像，去重判定只看 state 账本，互不干扰）。任一写入失败均不影响推送。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except Exception:
        pass
    try:
        with open(LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[notifier] 权威账本写入失败（不影响推送）：%r" % e)
    try:
        os.makedirs(DIST, exist_ok=True)
        with open(os.path.join(DIST, "push_log.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _already_pushed_today(mode, analysis_date=None):
    """同一 mode 当天是否已推送过（读 push_log.jsonl 判定）。

    close / close_again 按『分析日(adate)』去重，而非自然日——
    否则前一日复盘补发若在次日零点后运行，会吃掉当日补发的去重名额，
    致使用户在 16:10 主推送失败时收不到 20:00 安全网（已复现）。
    其余 mode（preauction/auction/open_anomaly/weekend）仍按自然日去重。
    """
    if mode not in _ONCE_PER_DAY:
        return False
    try:
        logp = _ledger_path()
        if not os.path.exists(logp):
            return False
        today = _bj_now().strftime("%Y-%m-%d")
        with open(logp, encoding="utf-8") as fh:
            for line in fh:
                try:
                    p = json.loads(line)
                except Exception:
                    continue
                if p.get("mode") != mode:
                    continue
                if mode in ("close", "close_again"):
                    # 按分析日去重（修复跨自然日污染）
                    if analysis_date:
                        if p.get("adate") == analysis_date:
                            return True
                        # 无 adate 的历史遗留记录按自然日兜底；修复后不再产生此类记录
                        if p.get("adate") is None and str(p.get("ts", "")).startswith(today):
                            return True
                    else:
                        if str(p.get("ts", "")).startswith(today):
                            return True
                else:
                    if str(p.get("ts", "")).startswith(today):
                        return True
        return False
    except Exception:
        return False


def _last_push_ts(mode, analysis_date=None):
    """返回该 mode 今日（按分析日）最近一次推送的时间戳字符串，无则 None。

    供 close_again 安全网判断「距上次补发是否已超过冷却」——超过阈值则放行重推，
    避免 once-per-day 去重把 20:00 兜底补发静默吞掉。
    """
    try:
        logp = _ledger_path()
        if not os.path.exists(logp):
            return None
        today = _bj_now().strftime("%Y-%m-%d")
        last = None
        with open(logp, encoding="utf-8") as fh:
            for line in fh:
                try:
                    p = json.loads(line)
                except Exception:
                    continue
                if p.get("mode") != mode:
                    continue
                if mode in ("close", "close_again"):
                    if analysis_date:
                        if p.get("adate") != analysis_date:
                            continue
                    elif not str(p.get("ts", "")).startswith(today):
                        continue
                else:
                    if not str(p.get("ts", "")).startswith(today):
                        continue
                ts = p.get("ts")
                if ts and (last is None or ts > last):
                    last = ts
        return last
    except Exception:
        return None


def _minutes_since(ts):
    """时间戳字符串距现在的分钟数；无时间戳返回极大值（视为「很久以前」）。"""
    if not ts:
        return 10 ** 9
    try:
        then = time.strptime(ts, "%Y-%m-%d %H:%M:%S")
        now = time.strptime(_bj_now().strftime("%Y-%m-%d %H:%M:%S"),
                            "%Y-%m-%d %H:%M:%S")
        return (time.mktime(now) - time.mktime(then)) / 60.0
    except Exception:
        return 10 ** 9


def _anomaly_recently_pushed(cooldown_min=12):
    """盘中异动(anomaly)允许日内多次推送，但外部定时器(cron-job.org)与 GitHub 主调度
    会在同一时点各自触发 → 重复轰炸。用『最近 N 分钟内已推送过则跳过』去重。
    N 必须小于 GitHub 盘中 anomaly 时点的最小间隔（09:50→10:07 为 17 分钟），
    否则会误伤合法盘中信号。"""
    try:
        logp = _ledger_path()
        if not os.path.exists(logp):
            return False
        now = _bj_now()
        with open(logp, encoding="utf-8") as fh:
            for line in fh:
                try:
                    p = json.loads(line)
                except Exception:
                    continue
                if p.get("mode") != "anomaly":
                    continue
                ts = p.get("ts")
                if not ts:
                    continue
                try:
                    dt = datetime.datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                if (now - dt).total_seconds() < cooldown_min * 60:
                    return True
    except Exception:
        return False
    return False


def reported_anomaly_codes_today():
    """返回今天（北京时间）已推送过的盘中异动标的代码集合，用于内容去重：
    同一标的当天只首次提示，避免 15 分钟巡查把已报过的票反复刷屏。"""
    try:
        logp = _ledger_path()
        if not os.path.exists(logp):
            return set()
        today = _bj_now().strftime("%Y-%m-%d")
        out = set()
        with open(logp, encoding="utf-8") as fh:
            for line in fh:
                try:
                    p = json.loads(line)
                except Exception:
                    continue
                if p.get("mode") != "anomaly":
                    continue
                if str(p.get("ts", "")).startswith(today):
                    for c in (p.get("codes") or []):
                        out.add(str(c))
        return out
    except Exception:
        return set()


def _recently_pushed(mode, cooldown_min):
    """通用冷却去重：最近 N 分钟内已推送过指定 mode 则跳过（用于 panic 等日内多次推送）。"""
    try:
        logp = _ledger_path()
        if not os.path.exists(logp):
            return False
        now = _bj_now()
        with open(logp, encoding="utf-8") as fh:
            for line in fh:
                try:
                    p = json.loads(line)
                except Exception:
                    continue
                if p.get("mode") != mode:
                    continue
                ts = p.get("ts")
                if not ts:
                    continue
                try:
                    dt = datetime.datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                if (now - dt).total_seconds() < cooldown_min * 60:
                    return True
    except Exception:
        return False
    return False


# ============================================================== 用户级个性化推送
def load_users():
    """读取 config/allowed_users.json（CI 运行时由 ALLOWED_USERS_JSON 密钥解密后写出，明文）。

    返回 [ {id, name, sc, pp, holdings, watch}, ... ]；忽略无关字段。无文件/解析失败返回 []。
    sc = 该用户的 ServerChan 推送密钥；pp = 该用户的 PushPlus 令牌；
    holdings = 该用户的个性化持仓列表（与 config/holdings.json 同构）；
    watch = 该用户自选关注股代码列表（6位数字字符串）——「谁关注谁收到」，管理员(owner)
    的关注清单另从 config/holdings.json 的 watch=true 条目自动并入。"""
    p = os.path.join(ROOT, "config", "allowed_users.json")
    if not os.path.exists(p):
        return []
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return []
    us = d.get("users") if isinstance(d, dict) else None
    if not isinstance(us, list):
        return []
    out = []
    for u in us:
        if not isinstance(u, dict):
            continue
        _watch = []
        for c in (u.get("watch") if isinstance(u.get("watch"), list) else []):
            s = str(c).strip()
            if s.isdigit() and len(s) == 6:
                _watch.append(s)
        out.append({
            "id": u.get("id"),
            "name": u.get("name") or u.get("id"),
            "sc": (u.get("sc") or "").strip(),
            "pp": (u.get("pp") or "").strip(),
            "holdings": u.get("holdings") if isinstance(u.get("holdings"), list) else None,
            "watch": _watch,
        })
    return out


def _filter_claimed(cfg, claimed_sc, claimed_pp):
    """把已被某用户『个人认领』的 sendkey/token 从共享广播配置中剔除，避免重复发送给同一人。"""
    if not (claimed_sc or claimed_pp) or not cfg:
        return cfg
    import copy
    c = copy.deepcopy(cfg)
    sc = c.get("wechat_serverchan")
    if isinstance(sc, dict) and isinstance(sc.get("sendkey"), list):
        sc["sendkey"] = [x for x in sc["sendkey"]
                         if not ((isinstance(x, dict) and x.get("key") in claimed_sc)
                                 or (isinstance(x, str) and x in claimed_sc))]
    pp = c.get("wechat_pushplus")
    if isinstance(pp, dict) and isinstance(pp.get("token"), list):
        pp["token"] = [x for x in pp["token"]
                       if not ((isinstance(x, dict) and (x.get("token") or x.get("key")) in claimed_pp)
                               or (isinstance(x, str) and x in claimed_pp))]
    return c


def _log_personal_send(pmode, title):
    """写一条极简去重记录，供 _already_pushed_today 判定『该用户该 mode 今日已发』。"""
    _append_ledger({"ts": _bj_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "mode": pmode, "title": title})


# ===================== 分用户推送路由（2026-08-26 用户拍板） =====================
# 规则：管理员(owner)关注的股票只推给管理员自己；其余用户只收到自己关注股票的提示。
# 实现通道绑定：notify.json 里的 sendkey/token 条目可加 "user":"<id>" 字段，
#   如 {"key":"SCTxxx","name":"我","user":"owner"}。
# 绑定通道收到的正文 = 公共盘面（剥离 ⭐关注股雷达 / 🎯买卖区间 两个个人分区）
#                   + 「📌 你的自选跟踪」专属附录（按该用户 watch∪holdings 过滤）。
# 未绑定通道照旧收完整广播（兼容旧配置）；完全无绑定时行为与旧版一致。
ADMIN_UID = "owner"

# 个人分区哨兵标记（不可见控制字符包裹，仅内部使用；_strip_personal_sections 按此成对删除）
_MARK_WL = "\x01SAWL\x02"
_MARK_WL_END = "\x01/SAWL\x02"
_MARK_ZN = "\x01SAZN\x02"
_MARK_ZN_END = "\x01/SAZN\x02"


def _chan_user(entry):
    """读取通道条目的可选 user 绑定（notify.json 中 {"key":...,"name":...,"user":"owner"}）。"""
    if isinstance(entry, dict):
        v = entry.get("user")
        return str(v).strip() if v else None
    return None


def _bound_uids(cfg):
    """notify.json 中所有绑定了 user 的通道对应的用户 id 集合。"""
    out = set()
    sc = (cfg or {}).get("wechat_serverchan") or {}
    for field in ("sendkey", "sendkeys"):
        v = sc.get(field)
        for x in (v if isinstance(v, list) else [v] if v else []):
            u = _chan_user(x)
            if u:
                out.add(u)
    pp = (cfg or {}).get("wechat_pushplus") or {}
    tv = pp.get("token")
    for x in (tv if isinstance(tv, list) else [tv] if tv else []):
        u = _chan_user(x)
        if u:
            out.add(u)
    return out


def _has_any_binding(cfg):
    """notify.json 中是否存在至少一条绑定了 user 的通道。"""
    sc = (cfg or {}).get("wechat_serverchan") or {}
    for field in ("sendkey", "sendkeys"):
        v = sc.get(field)
        for x in (v if isinstance(v, list) else [v] if v else []):
            if _chan_user(x):
                return True
    pp = (cfg or {}).get("wechat_pushplus") or {}
    tv = pp.get("token")
    for x in (tv if isinstance(tv, list) else [tv] if tv else []):
        if _chan_user(x):
            return True
    return False


def _strip_personal_sections(text):
    """按哨兵标记剥离个人分区（⭐关注股雷达 / 🎯买卖区间），并清掉孤立标记行。
    无标记的正文（如 ServerChan 精简版）原样返回——它本就不含这两个分区。"""
    if not text or "\x01" not in text:
        return text
    import re as _re
    _m1, _m1e = _re.escape(_MARK_WL), _re.escape(_MARK_WL_END)
    _m2, _m2e = _re.escape(_MARK_ZN), _re.escape(_MARK_ZN_END)
    out = _re.sub(_m1 + r"[\s\S]*?" + _m1e + r"\n?", "", text)
    out = _re.sub(_m2 + r"[\s\S]*?" + _m2e + r"\n?", "", out)
    out = _re.sub(r"\x01/?SA(?:WL|ZN)\x02\n?", "", out)  # 孤立标记兜底
    out = _re.sub(r"\n{3,}", "\n\n", out)
    return out.rstrip() + "\n" if out.strip() else ""


def _admin_watch_codes():
    """管理员关注清单 = config/holdings.json 里 watch=true 的代码（含持仓观察）。"""
    try:
        import holdings as _hd
        pos = _hd.load_positions() or []
        out = []
        for p in pos:
            if isinstance(p, dict) and p.get("code") and (
                    p.get("watch") or p.get("enabled")):
                s = str(p["code"]).strip()
                if s.isdigit() and len(s) == 6:
                    out.append(s)
        return out
    except Exception:
        return []


def _effective_watch(uu):
    """用户的完整关注集：ALLOWED_USERS_JSON 的 watch ∪ holdings 代码；owner 额外并入
    config/holdings.json 的管理员关注清单。"""
    codes = set(uu.get("watch") or [])
    for c in (uu.get("holdings") or []):
        s = c.strip() if isinstance(c, str) else \
            (str(c.get("code")) if isinstance(c, dict) and c.get("code") else "")
        if s.isdigit() and len(s) == 6:
            codes.add(s)
    if (uu.get("id") or "") == ADMIN_UID:
        codes.update(_admin_watch_codes())
    return codes


def _personal_appendix(data, codes):
    """按用户关注集过滤 zones 区间提示，拼「📌 你的自选跟踪」附录；无命中返回空串。"""
    if not codes:
        return ""
    z = (data or {}).get("zones") or {}
    items = [x for x in (z.get("items") or []) if x.get("code") in codes]
    if not items:
        return ""
    try:
        import zones as _zmod
        al = {"sell": [x for x in items if x.get("action") == "破位卖出"],
              "add": [x for x in items if x.get("action") in ("加仓提示", "回踩买入区")],
              "take_profit": [x for x in items if x.get("action") == "逼近卖出"],
              "time": [x for x in items if x.get("time_alert")],
              "rotate": [x for x in items if x.get("rotate")]}
        z2 = dict(z)
        z2["items"], z2["alerts"] = items, al
        lines = list(_zmod.summary_lines(z2) or [])
    except Exception:
        lines = []
    normal = [x for x in items if x.get("action") == "正常持有"][:3]
    for x in normal:
        if len(lines) >= 8:
            break
        try:
            lines.append("%s(%s) 收%s · 买%s~%s / 卖%s~%s / 止损%s [%s]"
                         % (x.get("name"), x["code"], x["close"],
                            x["buy_zone"][0], x["buy_zone"][1],
                            x["sell_zone"][0], x["sell_zone"][1],
                            x["stop"], x["action"]))
        except Exception:
            pass
    if not lines:
        return ""
    return "\n\n📌 **你的自选跟踪**（仅推送给你）\n" + \
        "\n".join("- " + l for l in lines[:8])


def _push_personalized(data, mode, users, analysis_date, results):
    """为每个『配置了专属通道 + 持股』的用户单独发送个性化消息（市場概述 + 其本人持股体检）。

    去重：同一用户同一 mode 当天只发一次（防多调度器重复烧 ServerChan 5条/天额度）。"""
    import engine as _engine
    import holdings as _hd
    con = store.connect()
    u = _engine.Universe(con, days=270)
    date = analysis_date or (u.dates[-1] if u.dates else None)
    if not date:
        return
    for uu in users:
        _uid = uu.get("id") or uu.get("name") or "?"
        _pmode = "personal_%s_%s" % (mode, _uid)
        if _already_pushed_today(_pmode, analysis_date):
            results.append("wechat_personal:%s 跳过(今日已发)" % (uu.get("name") or _uid))
            continue
        positions = uu.get("holdings")
        if isinstance(positions, str):
            positions = [positions]
        if not isinstance(positions, list) or not positions:
            # 只有自选清单（watch）而无持仓记录的用户，同样按其自选做个性化跟踪
            positions = uu.get("watch")
        if not isinstance(positions, list) or not positions:
            continue
        # 归一化：前端/ALLOWED_USERS_JSON 里 holdings 是「代码字符串列表」（如 ["600519","000001"]），
        # 而 holdings.monitor(positions=) 经 _norm_pos 处理、要求 {code:...} 字典列表。
        # 这里做桥接，否则字符串会被 _norm_pos 整批过滤成 enabled=False，个性化整段跳过。
        norm_pos = []
        for c in positions:
            if isinstance(c, dict):
                norm_pos.append(c)
            elif isinstance(c, str) and c.strip().isdigit() and len(c.strip()) == 6:
                norm_pos.append({"code": c.strip()})
        if not norm_pos:
            continue
        kind = "sc" if uu.get("sc") else ("pp" if uu.get("pp") else None)
        if not kind:
            continue
        key = uu["sc"] if kind == "sc" else uu["pp"]
        uname = uu.get("name") or uu.get("id")
        try:
            urep = _hd.monitor(u, date, con, positions=norm_pos, persist=False)
            if not (urep and urep.get("enabled")):
                continue
            d2 = dict(data)
            d2["holdings"] = urep
            fmt = format_sc(d2, "", mode)
            text = fmt["text"]
            title = fmt["title"]
            if kind == "sc":
                if len(text) > SC_CAP:
                    text = text[:SC_CAP - 120].rstrip() + "\n…（完整版见站点看板）"
                ok, msg = send_wechat_serverchan(
                    {"sendkey": [{"key": key, "name": uname}]}, title, text)
            else:
                ok, msg = send_wechat_pushplus(
                    {"token": [{"token": key, "name": uname}]}, title, text)
            results.append("wechat_%s:%s → %s" % (kind, uname, msg))
            print("[notifier][personal][%s] %s(%s): %s" % (kind, uname, key[:6] + "…", msg))
            if ok:
                _log_personal_send(_pmode, title)
        except Exception as e:
            results.append("wechat_%s:%s 失败 %r" % (kind, uname, e))
            print("[notifier][personal] %s 失败：%r" % (uname, e))


def push(summary, dry_run=False, mode="close", codes=None, analysis_date=None, data=None):
    """summary: {"title": str, "text": str}。返回已送达通道列表。
    mode 取值与去重（同一 mode 当天只发一次）对应：
      - "preauction"  盘前预判（08:50）
      - "auction"     竞价后确认（09:25）
      - "close"       收盘后完整复盘（15:20）
      - "close_again" 复盘补发（20:00，与 close 独立，不可共用否则被去重吞掉）
      - "weekend"     周末发酵/周一前瞻（周日/周一）
      - "anomaly"     盘中异动（随时，走 PushPlus 冷却去重，不占 ServerChan 额度）
      - "open_anomaly" 竞价后开盘前异动（09:26，个股检测类——走 PushPlus，不占 SC 额度）
      - "panic"       盘中恐慌/崩盘预警（突发快速下杀，走 PushPlus 随时推送，不占 ServerChan 额度）
      - "stoploss"    持仓止损即时提醒（评级 D 触发，走 PushPlus/企微，30分钟冷却，不占 SC 额度）
    ServerChan 额度规避：close_again 复盘补发主动让出 SC 名额（优先 PushPlus/企微），
    使 SC 单 key 5条/天只承载 preauction+open_anomaly+close 三个关键节点，预留余量。
    无论是否配置通道，都会把推送内容落地为可见文件（last_push_<mode>.md），避免『啥都看不到』。
    注意：去重账本 state/push_ledger.jsonl 仅在『至少一条通道真实送达』后才写，失败不污染去重。"""
    # 非交易日拦截：cron 写的是「周一至周五」，法定节假日（春节/国庆等）照样点火。
    # 若不拦，节假日会连日把「节前那根K线」当『今日复盘』推出去，既误导又白烧
    # ServerChan 额度（5 条/天）。weekend 模式本就在周末推，豁免。
    # 逃生阀：设环境变量 SA_FORCE_PUSH=1 可强制推送（手工补发/测试用）。
    if (not dry_run and mode not in ("weekend", "weekattr") and trade_calendar is not None
            and os.environ.get("SA_FORCE_PUSH") != "1"
            and not trade_calendar.is_trade_day()):
        print("[notifier][%s] %s（%s），跳过推送——避免把节前数据当『今日』发出"
              % (mode, trade_calendar.why_closed() or "非交易日",
                 _bj_now().strftime("%Y-%m-%d")))
        return ["skipped:not-trade-day"]
    # 幂等去重：同一 mode 当天已推送过则跳过通道发送，避免多路触发重复轰炸
    # （GitHub 自带 schedule 常被丢弃，故叠加了看门狗/备份订阅/外部定时器多重触发，
    #  这里统一兜底：先到先发，后到静默）。
    if not dry_run and _already_pushed_today(mode, analysis_date):
        # close_again 复盘补发安全网：距上次推送 >2 小时放行重推——
        # 主推(16:36)与兜底(20:00+)拉开时差，避免「已发过就静默」致安全网失效
        # （已复现：20:00 兜底被 once-per-day 吞掉，用户收不到补发）。
        # SA_FORCE_PUSH=1 显式强制绕过（手工补发/测试）。
        _force = os.environ.get("SA_FORCE_PUSH") == "1"
        _last_ts = _last_push_ts(mode, analysis_date)
        # 仅允许「同一分析日、且发生在当日(自然日)内」的 >2h 重推：
        # 覆盖合法场景 16:36 主推 → 20:00 兜底(同自然日, ~3.5h)。
        # 禁止跨自然日重发「昨日复盘」——本地缺当日数据致 build 退化到上一交易日时，
        # 25h 的间隔会误触发 gap 放行，把昨日复盘当成今日补发重推（2026-09-02 已复现）。
        _gap_ok = (mode == "close_again"
                   and _last_ts is not None
                   and str(_last_ts).startswith(_bj_now().strftime("%Y-%m-%d"))
                   and _minutes_since(_last_ts) > 120)
        if not (_force or _gap_ok):
            print("[notifier][%s] 今日已推送，跳过通道发送（防重复触发）" % mode)
            return ["skipped:dup"]
    # 交易时段闸：盘中异动(anomaly)只有在 09:15–15:00 北京时间（且为交易日）才允许推送。
    # 外部定时器 / 看门狗误在休市时段（如凌晨 4 点）点火时，绝不推送『盘中异动』，
    # 从根上根治「中国时间 4 点误推盘中异动」的事故。
    if not dry_run and mode == "anomaly" and not _in_anomaly_window():
        print("[notifier][anomaly] 当前非交易时段（北京 %s），跳过盘中异动推送"
              % _bj_now().strftime("%H:%M"))
        return ["skipped:off-hours"]
    # 盘中异动：允许日内多次，但外部定时器与 GitHub 同窗口触发会重复，
    # 用 12 分钟冷却去重（小于盘中时点最小间隔 17 分钟，不误伤合法信号）。
    if not dry_run and mode == "anomaly" and _anomaly_recently_pushed(12):
        print("[notifier][anomaly] 12分钟内已推送盘中异动，跳过（防外部定时器与GitHub同窗口重复）")
        return ["skipped:dup-anomaly"]
    if not dry_run and mode == "panic":
        if not _in_anomaly_window():
            print("[notifier][panic] 当前非交易时段（北京 %s），跳过盘中恐慌推送"
                  % _bj_now().strftime("%H:%M"))
            return ["skipped:off-hours"]
        if _recently_pushed("panic", 15):
            print("[notifier][panic] 15分钟内已推送恐慌预警，跳过（防重复）")
            return ["skipped:dup-panic"]
    if not dry_run and mode == "stoploss":
        # 止损即时提醒：持仓触发硬止损（评级 D）时经 PushPlus/企微即时告警，
        # 不占 ServerChan 额度；仅交易时段推送（持仓仅在交易日变动），30 分钟冷却防重复。
        if not _in_anomaly_window():
            print("[notifier][stoploss] 当前非交易时段（北京 %s），跳过止损推送"
                  % _bj_now().strftime("%H:%M"))
            return ["skipped:off-hours"]
        if _recently_pushed("stoploss", 30):
            print("[notifier][stoploss] 30分钟内已推送止损提醒，跳过（防重复）")
            return ["skipped:dup-stoploss"]
    # 输出兜底：避免 print 带 emoji 在非 UTF-8 控制台（如 GBK）抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = load_config()
    # 2026-09-03 推送分级（用户需求5）：按 mode 映射的 scope 过滤所有通道，
    # 只把消息发给 scope 匹配的接收人（owner=all 全收；scope=none 不收；prepost 仅盘前盘后）。
    _allowed = MODE_SCOPE.get(mode, {"all"})
    cfg = _scope_filter_all(cfg, _allowed)
    # 用户级个性化：按各人『专属通道 + 本人持仓』给每个人发专属复盘（盘面 + 其本人持仓跟踪）。
    # 规则：
    #  - 仅当某用户配置了 专属通道(sc/pp) 且 有持仓 时，才从『共享广播』剔除其密钥，
    #    避免同一人既收广播又收专属（重复 + 烧 ServerChan 5条/天额度）。
    #  - 仅 close / close_again（每日主复盘）做个性化替换；盘前/竞价仍走共享广播，
    #    保证每个人都能收到盘面节奏，不会因被过滤而漏掉。
    _users = load_users()
    _users_with_pos = [u for u in _users
                       if (u.get("sc") or u.get("pp")) and u.get("holdings")]
    _claimed_sc = {u["sc"] for u in _users_with_pos if u.get("sc")}
    _claimed_pp = {u["pp"] for u in _users_with_pos if u.get("pp")}
    _personal_modes = (mode in ("close", "close_again"))
    cfg_shared = _filter_claimed(cfg, _claimed_sc, _claimed_pp) if _personal_modes else cfg
    # ---- 分用户路由准备：绑定用户表 + 是否剥离广播中的个人分区 ----
    _users_by_id = {}
    for _u in _users:
        if _u.get("id"):
            _users_by_id[_u["id"]] = _u
    _uids = _bound_uids(cfg)
    for _uid in _uids:
        if _uid not in _users_by_id:
            _users_by_id[_uid] = {"id": _uid, "name": _uid,
                                  "watch": [], "holdings": None}
    # 只有存在「绑定了且确有自选」的通道才剥离个人分区；否则保持旧版完整广播不丢信息。
    # 剥离后：未绑定通道收纯盘面，绑定通道各自附加「📌 你的自选跟踪」（见发送循环）。
    _strip_personal = bool(_uids) and mode in (
        "close", "close_again", "preauction", "auction", "weekend") \
        and any(_effective_watch(_users_by_id[u]) for u in _uids)
    # 收件人维度（供推送面板可视化：谁收到、走哪个通道、是否含本人自选跟踪）。
    # 取自 scope 过滤后的共享广播配置 + 个性化复盘专属通道（持仓用户）。
    _recips = []
    def _collect(ch_name, cc):
        if not cc or not isinstance(cc, dict):
            return
        for fld in ("sendkey", "sendkeys", "token"):
            items = cc.get(fld)
            if not items:
                continue
            items = items if isinstance(items, list) else [items]
            for x in items:
                if isinstance(x, dict):
                    _recips.append({"channel": ch_name, "name": x.get("name"),
                                    "scope": x.get("scope", "all"), "user": x.get("user")})
    _collect("serverchan", cfg_shared.get("wechat_serverchan"))
    _collect("pushplus", cfg_shared.get("wechat_pushplus"))
    for _u in _users_with_pos:
        _recips.append({"channel": "专属通道", "name": _u.get("name"),
                        "scope": "all", "personalized": True})
    title = summary.get("title", "A股盘后复盘")
    text = summary.get("text", "")
    results = []
    if dry_run:
        print("[notifier][dry-run][%s] title=%s\n%s" % (mode, title, text))
        return results

    # 1) 始终落地可见文件痕迹（看板内可直接查看，不等同于“已推送”）。
    #    注意：此处只写 last_push_<mode>.md 可见文件，绝不写去重账本 push_log.jsonl——
    #    去重账本只在“通道真实送达后”才记（见下方 block 3），避免“先记账再发通道”导致
    #    通道发送失败时账本已记“今日已推”，把当天重试永久挡掉（复盘/收盘再次跑成功却零送达）。
    _ts = _bj_now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(DIST, exist_ok=True)
        art = os.path.join(DIST, "last_push_%s.md" % mode)
        with open(art, "w", encoding="utf-8") as fh:
            fh.write("# %s\n\n> 生成时间：%s\n\n```\n%s\n```\n" % (title, _ts, text))
        results.append("file:%s" % os.path.relpath(art, ROOT))
    except Exception as e:
        print("[notifier] 文件痕迹写入失败：%r" % e)

    # 2) 通道推送（按 mode 路由）：
    #    ServerChan 固定 4 条/工作日 = 盘前(preauction) + 竞价(auction) + 收盘(close)
    #    + 复盘补发(close_again)，占单 key 5 条/天额度、余 1 条机动；周末(weekend)在
    #    周日发、独立计额度。2026-08-26 用户拍板：这四类全部以 SC 为主通道。
    #    PushPlus 随时推送（200 条/天几乎不限）：盘中异动(anomaly)、竞价后开盘前异动
    #    (open_anomaly)、恐慌(panic)、妖股(yaogu)、止损(stoploss)
    #    ——个股/关注股类检测推送全部走 PushPlus（绝不挤占 SC 关键节点名额）。
    _all = [
        ("wechat_serverchan", send_wechat_serverchan, cfg_shared.get("wechat_serverchan")),
        ("wechat_pushplus", send_wechat_pushplus, cfg_shared.get("wechat_pushplus")),
        ("wecom", send_wecom, cfg_shared.get("wecom")),
        ("telegram", send_telegram, cfg_shared.get("telegram")),
        ("email", send_email, cfg_shared.get("email")),
    ]
    if mode in ("anomaly", "open_anomaly", "panic", "yaogu", "stoploss"):
        # 盘中异动 / 竞价后开盘前异动 / 恐慌 / 妖股 / 止损即时：
        # 全部走 PushPlus 系（200 条/天几乎不限），绝不占 ServerChan 的固定额度。
        # open_anomaly 属个股竞价异动检测——用户拍板：个股/关注股检测一律 PushPlus。
        _prefer = ["wechat_pushplus", "wecom", "telegram", "email"]
    else:
        # 盘前 / 竞价确认 / 收盘 / 复盘补发 / 周末发酵：ServerChan 为主（2026-08-26 用户拍板：
        # 工作日固定 4 条 = 盘前+竞价+收盘+复盘，占单 key 5 条/天额度、余 1 条机动；
        # 周末消息在周日发，独立计额度不冲突），PushPlus 冗余兜底。
        _prefer = ["wechat_serverchan", "wechat_pushplus", "wecom", "telegram", "email"]
    dispatchers = [(n, fn, c) for (n, fn, c) in _all if n in _prefer and c]
    for name, fn, c in dispatchers:
        if not c:
            continue
        try:
            body = text
            if name == "wechat_serverchan":
                # ServerChan 单条 desp 硬上限 8192 字：用精简结果版（format_sc），
                # 仍超限则硬截断兜底，确保这条关键推送不静默丢失。
                sc_text = summary.get("sc_text")
                if sc_text:
                    body = sc_text
                if len(body) > SC_CAP:
                    body = body[:SC_CAP - 120].rstrip() + "\n…（完整版见 PushPlus 与站点看板）"
            if _strip_personal:
                # 分用户逐条发送：未绑定通道收纯盘面；绑定通道 = 盘面 + 本人自选跟踪。
                base_body = _strip_personal_sections(body)
                if name == "wechat_serverchan":
                    entries = []
                    for field in ("sendkey", "sendkeys"):
                        v = c.get(field)
                        entries += (v if isinstance(v, list) else [v] if v else [])
                else:
                    v = c.get("token")
                    entries = (v if isinstance(v, list) else [v] if v else [])
                ok_n, tot, parts = 0, 0, []
                for x in entries:
                    if not x:
                        continue
                    tot += 1
                    uid = _chan_user(x)
                    sub_body = base_body
                    if uid and uid in _users_by_id:
                        sub_body = base_body + _personal_appendix(
                            data, _effective_watch(_users_by_id[uid]))
                    if name == "wechat_serverchan":
                        if len(sub_body) > SC_CAP:
                            sub_body = sub_body[:SC_CAP - 120].rstrip() + \
                                "\n…（完整版见 PushPlus 与站点看板）"
                        sub_cfg = {"sendkey": [x]}
                    else:
                        sub_cfg = {"token": [x]}
                        if isinstance(c, dict) and c.get("topic"):
                            sub_cfg["topic"] = c["topic"]
                    ok1, m1 = fn(sub_cfg, title, sub_body)
                    if ok1:
                        ok_n += 1
                    lbl = (x.get("name") if isinstance(x, dict) else None) or "?"
                    parts.append("%s:%s" % (lbl, "OK" if ok1 else str(m1)[-40:]))
                    time.sleep(1)  # 同通道逐条间隔，防频控
                results.append("%s:分用户路由 成功%d/%d（%s）"
                               % (name, ok_n, tot, "；".join(parts)))
            else:
                ok, msg = fn(c, title, body)
                results.append("%s:%s" % (name, msg))
        except Exception as e:
            results.append("%s:失败 %r" % (name, e))
    if not any(r.startswith(("wechat", "wecom", "telegram", "email")) for r in results):
        print("[notifier] 未配置任何推送通道，仅落地文件痕迹（可在 config/notify.json 配置微信/Telegram/邮件）")
    else:
        for r in results:
            if not r.startswith("file:"):
                print("[notifier] %s" % r)

    # 4) 用户级个性化推送（仅 close / close_again）：给每位『专属通道 + 持仓』用户发本人专属复盘，
    #    其密钥已从上方共享广播中剔除（避免重复），这里单独走其本人通道。
    if _personal_modes and _users_with_pos:
        _push_personalized(data, mode, _users_with_pos, analysis_date, results)

    # 3) 仅在『至少一条通道真实送达』后才写去重账本 push_log.jsonl。
    #    关键修复：绝不能“先记账再发通道”。若通道发送失败（弱网偶发），账本已记“今日已推”，
    #    会导致当天其余重试被 once-per-day 去重永久挡掉，复盘/收盘再次“跑成功却零送达”。
    #    把记账后置到真实送达之后，失败的推送不污染去重，允许后续重跑补发——与 close_again
    #    独立 mode 修复配合，彻底根治“收不到复盘”。
    delivered = any(r.startswith(("wechat_serverchan", "wechat_pushplus", "wecom",
                                  "telegram", "email")) and "失败" not in r
                     for r in results)
    if delivered:
        try:
            # 记录实际成功送达的通道（不含“失败”项），便于事后核查某条推送到底走了哪个通道，
            # 例如定位“盘中异动收不到”究竟是 PushPlus 静默还是 ServerChan 兜底生效。
            _ch = [r.split(":", 1)[0] for r in results
                   if r.startswith(("wechat_serverchan", "wechat_pushplus", "wecom",
                                    "telegram", "email")) and "失败" not in r]
            _append_ledger({"ts": _ts, "mode": mode, "title": title,
                            "text": text, "channels": _ch,
                            "recipients": _recips,
                            "codes": list(codes) if codes else [],
                            "adate": analysis_date if mode in ("close", "close_again") else None})
        except Exception as e:
            print("[notifier] 去重账本写入失败（不影响已送达）：%r" % e)
        _rotate_push_log()
    return results


def _rotate_push_log(keep_days=90, full_text_days=7, brief_len=300):
    """滚动清理去重账本，防止无限膨胀。

    账本每条含推送全文（实测约 3.2KB/条），每交易日 4~6 条 → 一年约 3.9MB，
    而它会被打进 state.tar.gz 每次 CI 上传/下载，且看门狗每个检查点都要解析。
    策略（在保证功能前提下尽量瘦身）：
      - 丢弃 keep_days 天之前的记录（去重只看当天，看板只取每 mode 最近一条）；
      - full_text_days 天之前的记录把正文压到 brief_len 字符（保留 mode/ts 供审计）。
    安全：任何异常都静默放弃清理，绝不影响已完成的推送与当天去重。
    """
    try:
        logp = _ledger_path()
        if not os.path.exists(logp):
            return
        now = _bj_now()
        keep_before = now - datetime.timedelta(days=keep_days)
        brief_before = now - datetime.timedelta(days=full_text_days)
        rows, changed = [], False
        with open(logp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                except Exception:
                    changed = True          # 丢弃损坏行
                    continue
                try:
                    dt = datetime.datetime.strptime(str(p.get("ts", "")), "%Y-%m-%d %H:%M:%S")
                except Exception:
                    rows.append(p)          # 时间戳异常的保留，交由人工判断
                    continue
                if dt < keep_before:
                    changed = True
                    continue
                t = p.get("text") or ""
                if dt < brief_before and len(t) > brief_len:
                    p["text"] = t[:brief_len] + "…（历史记录已压缩）"
                    changed = True
                rows.append(p)
        if not changed:
            return
        tmp = logp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for p in rows:
                fh.write(json.dumps(p, ensure_ascii=False) + "\n")
        os.replace(tmp, logp)
        print("[notifier] 去重账本已滚动清理：保留 %d 条（%.1f KB）"
              % (len(rows), os.path.getsize(logp) / 1024.0))
    except Exception as e:
        print("[notifier] 去重账本清理跳过（不影响推送）：%r" % e)


def last_text_for_mode(mode):
    """返回 push_log.jsonl 中最近一次指定 mode 推送的正文；无则返回 None。
    用于『复盘补发』与『收盘后』推送去重：内容相同时跳过，节省 ServerChan 额度。
    复盘(close_again)与收盘(close)必须用各自独立的 mode，否则复盘会被 once-per-day 吞掉。"""
    try:
        logp = _ledger_path()
        if not os.path.exists(logp):
            return None
        last = None
        with open(logp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                except Exception:
                    continue
                if p.get("mode") == mode:
                    last = p.get("text")
        return last
    except Exception:
        return None


def last_close_text():
    """兼容旧调用：返回最近一次 mode=='close' 推送的正文。"""
    return last_text_for_mode("close")


def get_prev_rec_codes(con, date):
    """返回某交易日推荐标的 {code: {name,streak,p_break,tag}}，用于竞价『续强』判定。"""
    out = {}
    try:
        for code, name, streak, p_break, tag in con.execute(
                "SELECT code,name,streak,p_break,tag FROM rec_picks WHERE date=?", (date,)):
            out[code] = {"name": name, "streak": streak, "p_break": p_break, "tag": tag}
    except Exception:
        pass
    return out


MAX_RECS = 10  # 推送中最多展示的推荐只数（前10）；超过则精简并提示看网页


def _push_gate():
    """推送双重认证阈值（config/notify.json 加 "push_gate": {"min_score":..,"min_p_continue":..} 可覆盖）。"""
    cfg = load_config() or {}
    g = cfg.get("push_gate") or {}
    try:
        return float(g.get("min_score", 55)), float(g.get("min_p_continue", 40))
    except Exception:
        return 55.0, 40.0


def _dual_ok(it):
    """双重认证：买入价值评分 与 晋级率 同时达标，才允许进入『认证推送』名单。
    任一字段缺失视为未认证（宁缺毋滥，杜绝低分票混进推荐位）。"""
    s_min, p_min = _push_gate()
    try:
        sc = it.get("worth_score") or 0
        pc = it.get("p_continue") or 0
        return sc >= s_min and pc >= p_min
    except Exception:
        return False


def _rec_line(it, idx, tag=""):
    """单只推荐（markdown 单行；必须用「- 」列表语法——SC/PP 渲染器对裸 "1. " 行不换行，
    这是盘前板式曾『糊成一团』的根因）。"""
    mark = ("[%s] " % tag) if tag else ""
    head = "- %s**%d. %s**(%s) · 买入价值**%.0f分** · 晋级**%.0f%%**" % (
        mark, idx, it.get("name", "?"), _board(it),
        it.get("worth_score", 0), it.get("p_continue", 0))
    rs = it.get("reasons") or []
    extra = (" ｜ %s" % "、".join(rs[:1])) if rs else ""
    return head + extra


def _top_recs(core, relay, allit, n):
    """推荐去重合并：优先 core→relay，不足 n 则用全量按分数补齐，确保展示前 n 只最佳标的。

    2026-08-28 修复排序错乱：core 与 relay 是不同分数段的两只桶，直接拼接会出现
    「core 46 分排在 relay 70 分前面」的乱序——合并去重后必须统一按
    worth_score（买入价值，与展示分一致）降序重排，保证第一名永远是最值得买的。"""
    out, seen = [], set()
    for it in (core or []) + (relay or []):
        c = it.get("code")
        if c in seen:
            continue
        seen.add(c)
        out.append(it)
    if len(out) < n and allit:
        srt = sorted(allit, key=lambda x: -x.get("score", 0))
        for it in srt:
            c = it.get("code")
            if c in seen:
                continue
            seen.add(c)
            out.append(it)
            if len(out) >= n:
                break
    out = out[:n]
    out.sort(key=lambda x: -(x.get("worth_score") or x.get("score") or 0))
    return out


def _global_signal(g):
    if g.get("available"):
        s = "%s — %s" % (g.get("signal"), g.get("detail", "外围数据缺失，按中性处理"))
        etfs = g.get("etfs") or []
        if etfs:
            s += "；ETF：" + "、".join("%s%s%%" % (e["name"], ("+" if e["pct"] >= 0 else "") + str(round(e["pct"], 1)))
                                   for e in etfs)
        return s
    return "数据缺失，按中性处理"


def _pct(v):
    return ("%.0f%%" % v) if v is not None else "—"


def _junk_free(s, minlen=4):
    """字符串是否有实质内容（去掉标点/空白后仍有 ≥minlen 个中英文字符）。

    用途：过滤 AI 偶发返回的 "..."、"—"、"无" 之类占位输出，避免推送里出现空壳行。"""
    if not s:
        return False
    t = "".join(ch for ch in str(s)
                if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return len(t) >= minlen


def _board(it):
    s = it.get("streak", 0) or 0
    return ("%d板" % s) if s else "首板"


def _signed(v, nd=0):
    """带符号数值格式化（资金流向等）：None → '—'，正数补 '+'。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    return ("%+." + str(int(nd)) + "f") % x



def _wd(date):
    """'2026-08-21' -> '08-21(周五)'，解析失败原样返回。"""
    try:
        import datetime as _dt
        d = _dt.date.fromisoformat(date)
        return "%s(%s)" % (date[5:], "周" + "一二三四五六日"[d.weekday()])
    except Exception:
        return date


def _fmt_close_compact(data, url="", mode="close", con=None):
    """全新简洁板式（ServerChan / PushPlus 统一）：一屏读完、只给结果。

    设计原则：盘面 4 行定调 → 要点 ≤3 条 → 推荐 ≤5 只 → 其余榜单一律单行内联，
    砍掉全部长字段叙述（MA 全值/多级晋级分档/回测样本说明等，看板里都有）。
    """
    m = data.get("meta", {})
    date = m.get("date", "")
    sent = data.get("market", {}).get("sentiment", {}) or {}
    cyc = data.get("market", {}).get("cycle", {}) or {}
    lus = data.get("limit_ups", []) or []
    rec = data.get("recommend", {}) or {}
    regime = data.get("regime") or {}
    micro = data.get("micro") or {}
    money = data.get("money") or {}
    narr = data.get("narrative") or {}

    # 板块自选：config/notify.json 加 "sections": {"trend":false,...} 可关闭对应小节；
    # 未配置默认全开。可选键：bullets/rec/trend/bull/strat/yaogu/avoid/money/cold/gaps/
    # dry/nh/glue/sword/style/tail/wl/recperf/lhb/risk/block/margin/etf/patsim
    _sec_cfg = (load_config() or {}).get("sections") or {}

    def _on(k):
        return bool(_sec_cfg.get(k, True))

    L = []
    head = "复盘补发 · %s" % _wd(date) if mode == "close_again" else "盘后复盘 · %s" % _wd(date)
    L.append("## 📊 " + head)
    # ---- 盘面速览（≤4 行）----
    L.append("情绪 **%.1f %s** ｜ 周期 **%s**"
             % (sent.get("score", 0), sent.get("label", ""), cyc.get("phase", "")))
    hi_streak = max([r.get("streak", 0) for r in lus], default=0)
    row2 = "涨停 **%d** · 最高 **%d板** · 晋级 **%s** · 封板 **%s**" % (
        len(lus), hi_streak, _pct(sent.get("promote_rate")), _pct(sent.get("seal_rate")))
    L.append(row2)
    if micro:
        p = micro.get("profit") or {}
        L.append("赚钱效应 昨涨停今均 **%s**（翻红%s/再板%s）· 炸板率 %s"
                 % (_signed(p.get("avg_pct"), 1) + "%", _pct(p.get("red_rate")),
                    _pct(p.get("again_rate")), _pct(micro.get("zhaban_rate"))))
    if _on("money") and money and money.get("boards_in"):
        L.append("主力净流入 **%s亿** ｜ 流入行业 %s"
                 % (("+" if money.get("total_main_net", 0) >= 0 else "")
                    + str(money.get("total_main_net")),
                    "、".join((b.get("name") or "") + _signed(b.get("net"), 0) + "亿"
                              for b in money.get("boards_in", [])[:3])))
    # ---- 定调 ----
    pp = data.get("preopen_plan") or {}
    tone = (regime.get("note") or "").strip()
    tone = tone.split("（数据源")[0].split("(数据源")[0].rstrip(" ｜，,")  # 内部字段不外发
    if len(tone) > 46:
        tone = tone[:45] + "…"
    bits = []
    if tone:
        bits.append(tone)
    if pp.get("position"):
        bits.append("建议仓位 %s" % pp["position"])
    if bits:
        L.append("")
        L.append("🧭 %s" % " ｜ ".join(bits))
    # ---- 要点（≤3 条）----
    # 过滤占位/无效要点（历史数据里可能残留 "..." 之类，渲染出来很难看）
    bl = [_b for _b in (narr.get("bullets") or [])
          if _b and _junk_free(_b)][:3] if _on("bullets") else []
    if bl:
        L.append("")
        for b in bl:
            L.append("- ✨ %s" % b)
    # ---- 推荐 Top5（双重认证前置：评分+晋级率双达标排前并打 ✅，未认证殿后） ----
    L.append("")
    recs = _top_recs(rec.get("core"), rec.get("relay"), rec.get("all"), 5) if _on("rec") else []
    # 信号实测胜率（2026-09-03 用户要求「要成功率」）：按 tag 统计近 N 笔次日表现，
    # 让推荐带上历史实证，低胜率标签一眼可见（样本<10 不出结论，避免小样本误导）。
    _tw = _tag_winrate(con, days=90)
    if _on("rec") and _tw:
        L.append("")
        L.append("📊 **信号实测（近90日 · 次日收红率）**：%s"
                 % " ｜ ".join("%s %.0f%%(n=%d) 均值%+.2f%%"
                               % (k, v["win_rate"], v["n"], v["avg_pct"]) for k, v in _tw))
        _worst = min(_tw.items(), key=lambda kv: kv[1]["win_rate"])
        if _worst[1]["win_rate"] < 45:
            L.append("> ⚠ 最差标签 **%s**（胜率 %.0f%%）：仅在其出现强确认时才考虑。"
                     % (_worst[0], _worst[1]["win_rate"]))
    # 双确认置顶；组内严格按买入价值分降序、再按晋级率降序
    # （修复：此前只分组不排序，组内保持 core→relay 原始顺序，出现「低分在前」的分数错乱）
    recs = sorted(recs, key=lambda x: (0 if _dual_ok(x) else 1,
                                       -(x.get("worth_score") or 0),
                                       -(x.get("p_continue") or 0)))
    # 负反馈闭环：被否决器拦下的不再出现在推荐位（数据源端已隔离，这里兜底再滤一次）
    try:
        import recveto as _rvt
        recs = [x for x in recs if not _rvt.veto(x)]
    except Exception:
        pass
    if recs:
        n_gated = sum(1 for x in recs if _dual_ok(x))
        L.append("🔥 **推荐 Top%d**%s"
                 % (len(recs), ("（✅双重认证 %d 只）" % n_gated) if n_gated else "（今日无双重认证标的）"))
        # 连板计划索引：code → 计划单（预期高度/买卖区）
        _lp = {p.get("code"): p for p in (rec.get("ladder_plans") or [])}
        for i, it in enumerate(recs, 1):
            # 注意：必须用「- 」列表语法，ServerChan/PushPlus 渲染器对裸 "1. " 行不换行
            rs = it.get("reasons") or []
            extra = (" ｜ %s" % "、".join(rs[:1])) if rs else ""
            okmark = "✅ " if _dual_ok(it) else ""
            warnmark = (" ⚠%s" % (it.get("veto_reason") or "")[:26]) if it.get("risk_flag") else ""
            plan_sfx = ""
            _pl = _lp.get(it.get("code"))
            if _pl:
                bz, sz, sp = _pl.get("buy_zone") or [0, 0], _pl.get("sell_zone") or [0, 0], _pl.get("stop")
                plan_sfx = " ｜ 🎯%s 买%.2f~%.2f 卖%.2f 止损%.2f" % (
                    _pl.get("expected_top", ""), bz[0], bz[1], sz[0], sp or 0)
            L.append("- 🔴 %s**%d. %s**(%s) · 价值 **%.0f分** · 晋级 **%.0f%%**%s%s%s"
                     % (okmark, i, it.get("name", "?"), _board(it),
                        it.get("worth_score", 0), it.get("p_continue", 0), extra,
                        warnmark, plan_sfx))
    else:
        L.append("🔥 今日无明确推荐，建议控仓或低位试错")
    # ---- 分区榜单：每板块独立标题，每股单独一行（用户要求：分区清晰不糊在一起）----
    def _sec(icon_title, rows):
        """rows 为空则整块省略；否则输出『**标题**』+ 每行一只。

        行本身已带 Markdown 标记（'- ' 列表项 / '> ' 引用 / '#' 标题）时不再重复加前缀，
        否则会出现「- - 自选 XX」「- > 提示」这类嵌套错乱（2026-08-28 修复）。"""
        if not rows:
            return
        L.append("")
        L.append("**%s**" % icon_title)
        for r in rows:
            if not r:
                continue
            if isinstance(r, str) and r[:2] in ("- ", "> ", "# ", "  "):
                L.append(r)
            else:
                L.append("- %s" % r)

    # ---- 连板机会计划单（用户需求：挖掘连板第二天有机会买的票 + 买卖区间 + 预期高度）----
    lps = rec.get("ladder_plans") or [] if _on("rec") else []
    if lps:
        _sec("🎯 连板机会计划（次日竞价介入口径）", [
            "**%s** %s ｜ 买 %.2f~%.2f · 目标 %s~%.2f · 止损 %.2f · 持有%d日 · R=%.1f · 到10%%率%d%%%s"
            % (p.get("name", "?"), p.get("expected_top", ""),
               p["buy_zone"][0], p["buy_zone"][1],
               p["sell_zone"][0], p["sell_zone"][1], p["stop"],
               p.get("hold_days", 2), p.get("rr", 0),
               p.get("reach10", 0),
               (" ｜" + p["evidence"]) if p.get("evidence") and p.get("sample_n") else "")
            for p in lps[:6]])

    trend = rec.get("trend") or [] if _on("trend") else []

    def _trend_line(t):
        meta = t.get("trend_meta") or {}
        vd = t.get("verdict") or {}
        act = vd.get("action") or t.get("advice") or ""
        # 2026-09-02（用户拍板：我没买的票不要推送卖点）——趋势跟踪池的票默认未持仓，
        # 「卖出（分批止盈）」本是持仓者语境（现价进入卖出区）→ 对未持仓者即「已过买点」。
        if act.startswith("卖出"):
            act = "回避（趋势走坏）" if "止损" in act else "不追（已过买点）"
        # 趋势早期票：显式「建议买入·持有X天」（用户需求：前期就给买入+持有期限）
        sfx = ""
        if act:
            if vd.get("suggested_hold_days"):
                sfx = " → %s **%s·持有%d天**" % (_act_emoji(act), act, vd["suggested_hold_days"])
            elif act.startswith("持有"):
                rest = max(0, (vd.get("hold_limit_days") or 20) - (vd.get("days_held") or 0))
                sfx = " → %s **持有·剩%d个交易日**" % (_act_emoji(act), rest)
            else:
                sfx = " → %s **%s**" % (_act_emoji(act), act)
        zone_s = (" ｜ 买%s~%s 卖%s~%s" % (
            t.get("buy_zone", ["", ""])[0], t.get("buy_zone", ["", ""])[1],
            t.get("sell_zone", ["", ""])[0], t.get("sell_zone", ["", ""])[1])
            if t.get("buy_zone") else "")
        # 机构/主力介入徽标（用户需求：机构介入情况及时点名）
        inst = t.get("institution") or {}
        inst_s = ""
        if inst.get("level") in ("强", "中"):
            _tg = (inst.get("tags") or [""])[0]
            inst_s = " ｜ 🏦%s介入%s" % (inst["level"], ("·" + _tg) if _tg else "")
        # 历史/新推荐标记（压缩为短标，避免行过长）
        _mark = ""
        if t.get("is_new"):
            _mark = " 🆕"
        elif (t.get("times") or 0) > 1:
            _mark = " (跟踪%d天)" % t.get("times")
        # 2026-09-03（用户拍板：不要再推没到买点的票）——行尾统一标注买点状态：
        # ✅现价可买 X.XX~X.XX / 🟡小仓试 / ⏳等回踩 X.XX(需回落Y%) / 🚫过热勿追。
        _buy_s = entry_badge(t)
        if not _buy_s:
            _can = zone_buyable(t.get("close"), t.get("buy_zone"))
            _buy_s = " ｜ ✅**现价可买**" if _can is True else ""
        return ("**%s**%s(%s) %.2f ｜ 日均%.1f%%·%d涨%s%s%s%s"
                % (t.get("name", "?"), _mark,
                   t.get("industry", "—") or "—",
                   t.get("close", 0) or 0,
                   meta.get("avg_daily", 0) or 0,
                   meta.get("up_days", 0) or 0,
                   zone_s, inst_s, sfx, _buy_s))

    def _trend_group(pred, n, exclude=None):
        out = []
        for t in trend:
            if exclude and id(t) in exclude:
                continue
            if pred(t):
                out.append(t)
            if len(out) >= n:
                break
        return out

    if trend:
        _acc = _trend_group(
            lambda t: (t.get("trend_meta") or {}).get("trend_state") == "加速上行", 3)
        _used = set(id(x) for x in _acc)
        # 「缓」的判据：涨得慢（趋势平缓）或涨速在衰减（增速放缓）——
        # 不能用 slow_channel 字段：该字段只说明它靠缓坡通道入选，可能日均 4%+（并不慢）。
        _slow = _trend_group(
            lambda t: (t.get("trend_meta") or {}).get("band") == "趋势平缓"
            or (t.get("trend_meta") or {}).get("trend_state") == "增速放缓", 3, _used)
        _used |= set(id(x) for x in _slow)
        _steady = _trend_group(lambda t: True, 4, _used)
        # 分档推送（用户需求：趋势票要同时给「缓」与「加速」两类，别只给强趋势）
        # 2026-09-02（用户拍板）：组内「现价可买」的票排最前，飞在天上的靠后。
        _bf = lambda lst: buyable_first(lst, lambda t: t.get("close"), lambda t: t.get("buy_zone"))
        if _acc:
            _sec("🚀 趋势 · 加速主升", [_trend_line(t) for t in _bf(_acc)])
        if _steady:
            _sec("📈 趋势 · 稳健上行", [_trend_line(t) for t in _bf(_steady)])
        if _slow:
            _sec("🐢 趋势 · 缓坡慢牛", [_trend_line(t) for t in _bf(_slow)])
    # ---- 买点候选（趋势加速优先）：从 data.buy_points 取加速组，红=买/优先；避免与牛股雷达/经典策略重复 ----
    _bp = data.get("buy_points") or {}
    # 2026-08-31 升级（可操作性）：买点候选反查 zones 引擎的买区/目标区，附价格区间，
    # 让推送里的「买点」可直接对照挂单。无 zones 买区的票退化为只显现价（不报错）。
    _zone_bz = {}
    for _z in ((data.get("zones") or {}).get("items") or []):
        _zc = _z.get("code")
        if not _zc:
            continue
        _zb = _z.get("buy_zone")
        _zs = _z.get("sell_zone")
        _bz = _zb if (_zb and _zb[0] is not None) else None
        _sz = _zs if (_zs and _zs[0] is not None) else None
        if _bz or _sz:
            _zone_bz[_zc] = (_bz, _sz)
    def _bp_zt(code):
        _bz, _sz = _zone_bz.get(code) or (None, None)
        _parts = []
        if _bz:
            _parts.append("买点%.2f~%.2f" % (_bz[0], _bz[1]))
        if _sz:
            _parts.append("目标%.2f~%.2f" % (_sz[0], _sz[1]))
        return (" ｜ " + " ／ ".join(_parts)) if _parts else ""
    # ---- 扫描覆盖度（2026-09-01 用户要求：结论必须是全 A 股全部过一遍后的结果）----
    # 口径：screen_uptrend 遍历 u.bars 全量 5544 只 → TopN；连板体系扫当日涨停池。
    # 显式展示覆盖度，避免用户误以为只是抽样样本。
    _cov = (data or {}).get("scan_coverage") or {}
    if _cov.get("universe"):
        _sec("🔍 扫描范围", [
            "全市场 **%d** 只股票全部参与计算（当日涨停 %d 只）"
            % (_cov["universe"], _cov.get("limit_up_today") or 0),
            "> 以下推荐与买点均为全量扫描后排序得出，不是抽样样本。",
        ])
    # 剔除「已临卖点」矛盾票（warn_sell）：不把引擎判卖出的票当买点推荐
    # 2026-09-02 用户拍板：飞在天上的票（现价>买区上沿5%）不再作为买点推荐——
    # 买点候选只留「现价就在买点附近、可以直接买」的票，可买的排最前并重点标注。
    # 2026-09-03 口径升级：buy_points.accel/others 在 build 侧已按 entry_state
    # （近端可执行买点）过滤过一遍，这里只做二次保险 + 排序 + 标注。
    # 关键修正：不再用 MA20 买区容差二次筛（那个口径对强势票天然偏远，会把
    # 真正「现价可买」的票也误杀），改用 entry_buyable，缺 entry 时才回退。
    def _bp_keep(b):
        ok = entry_buyable(b)
        if ok is not None:
            return ok
        return zone_buyable(b.get("price"),
                            (_zone_bz.get(b.get("code")) or (None, None))[0]) is not False

    def _bp_flag(b):
        s = entry_badge(b)
        if s:
            return s.replace(" ｜ ", "", 1) + " ｜ "
        _bz = (_zone_bz.get(b.get("code")) or (None, None))[0]
        return "✅现价可买 ｜ " if zone_buyable(b.get("price"), _bz) is True else ""

    _all_acc = [b for b in (_bp.get("accel") or []) if not b.get("warn_sell")]
    _all_acc = [b for b in _all_acc if _bp_keep(b)]
    _all_acc = buyable_first(_all_acc,
                             lambda b: b.get("price"),
                             lambda b: (_zone_bz.get(b.get("code")) or (None, None))[0])
    _acc_bp = _all_acc[:5]
    if _acc_bp:
        _acc_rows = []
        for b in _acc_bp:
            _acc_rows.append(
                "🔴 **%s**(%s) %.2f ｜ %s%s%s ｜ 评分%.0f%s"
                % (b.get("name", "?"), b.get("ind") or "—", b.get("price") or 0,
                   _bp_flag(b),
                   b.get("btype") or "",
                   (" ×%.2f" % b["accel"] if b.get("accel") is not None else ""),
                   b.get("score") or 0,
                   _bp_zt(b.get("code"))))
        if len(_all_acc) > len(_acc_bp):
            _acc_rows.append("…（加速买点共 %d 只，列评分前 %d；完整清单见站点「买点候选」视图）"
                             % (len(_all_acc), len(_acc_bp)))
        _sec("🎯 买点候选 · 加速优先", _acc_rows)
    # 2026-08-31 升级（用户要求：所有内容均需要推送）：加速优先之外的「其他买点」
    # （趋势多头/回踩/突破）也全部推送，避免买点候选只发头部几只而漏掉其余。
    # 同样剔除矛盾票（warn_sell），按评分取前 6 只控量（缠论买点另有独立段）。
    # 2026-09-02：同样只留「现价在买点附近」的票，可买排前并标注。
    _all_oth = [b for b in (_bp.get("others") or []) if not b.get("warn_sell")]
    _all_oth = [b for b in _all_oth if _bp_keep(b)]
    _all_oth = buyable_first(_all_oth,
                             lambda b: b.get("price"),
                             lambda b: (_zone_bz.get(b.get("code")) or (None, None))[0])
    _oth_bp = _all_oth[:6]
    if _oth_bp:
        _oth_rows = []
        for b in _oth_bp:
            _oth_rows.append(
                "🔴 **%s**(%s) %.2f ｜ %s%s ｜ 评分%.0f%s"
                % (b.get("name", "?"), b.get("ind") or "—", b.get("price") or 0,
                   _bp_flag(b), b.get("btype") or "", b.get("score") or 0,
                   _bp_zt(b.get("code"))))
        if len(_all_oth) > len(_oth_bp):
            _oth_rows.append("…（趋势多头买点共 %d 只，列评分前 %d；完整清单见站点「买点候选」视图）"
                             % (len(_all_oth), len(_oth_bp)))
        _sec("📈 买点候选 · 其他（趋势多头）", _oth_rows)
    # ---- 等回踩清单（2026-09-03 用户拍板：没到买点的票不混进买点候选，
    #      但也不能凭空消失——单列一段给明确挂单价，到价才动手）----
    _wait_bp = (_bp.get("waiting") or [])[:8]
    if _wait_bp:
        _wrows = []
        for b in _wait_bp:
            _e = b.get("entry") or {}
            _wp = _e.get("wait_price") or b.get("wait_price")
            _wrows.append(
                "⏳ **%s**(%s) 现价%.2f → 挂 **%.2f**（需回落%.1f%%）｜ %s ｜ 评分%.0f"
                % (b.get("name", "?"), b.get("ind") or "—", b.get("price") or 0,
                   _wp or 0, abs(_e.get("wait_drop_pct") or 0.0),
                   b.get("btype") or "", b.get("score") or 0))
        _gate = _bp.get("gate") or {}
        _wrows.append("> 趋势没问题但**现价高于短线买点**，不追高：到价成交才算买点。"
                      "本次共剔除 %d 只过热/已临卖点票。" % (_gate.get("skipped") or 0))
        _sec("⏳ 趋势可以但要等回踩（挂单价）", _wrows)
    # ---- 自选/持仓操作结论（P1/P4：跟着做）----
    _wr = rec.get("watch_reco")
    if _wr and _wr.get("items") and _on("rec"):
        # 2026-09-01：自选/持仓票已排序前置（watchreco.distill），条数 6→10
        # 保证用户加入自选的票（如中化国际 600500）每天都能收到操作说明。
        _sec("⭐ 自选/持仓操作", watchreco_lines({"recommend": rec}, n=10))
    # ---- 综合最优解（融合连板/趋势/席位/题材/连续信号/区间，多引擎共振优先）----
    fused = rec.get("fused") or []
    if fused:
        _sec("🏆 综合最优解", [
            "**%s**(%s) 综合%.0f分·%d引擎共振%s ｜ %s"
            % (f.get("name", "?"), f.get("industry", "—") or "—",
               f.get("fusion_score", 0), f.get("n_engine", 0),
               ("｜R=%s" % f.get("r")) if f.get("r") is not None else "",
               "、".join(("%s%s" % (e["engine"], ("+%d" % e["score"] if e["score"] >= 0 else "%d" % e["score"]))
                          for e in f.get("evidence", [])[:3])))
            for f in fused[:6]])
    # ---- 今日作战指令（升级模块聚合：仓位/触发/梯队/归因）----
    try:
        _brief = []
        pa = data.get("position_advice")
        if pa:
            _brief.append("建议总仓位 **%d%%**（%s，热度%s/情绪%s）"
                          % (pa.get("suggest_pct", 50), pa.get("level", ""),
                             pa.get("heat", "—"), pa.get("sentiment", "—")))
        lw = data.get("ladder_warn")
        if lw and lw.get("warns"):
            _brief.append("梯队[%s·高%d板]：%s" % (lw["level"], lw.get("today_max", 0),
                                                  "；".join(lw["warns"][:2])))
        tg = data.get("triggers")
        if tg and tg.get("hits"):
            for h in tg["hits"][:4]:
                _brief.append("🔔【%s】%s（%s）%s" % (h["type"], h["name"], h["pool"], h["detail"]))
        ac = data.get("accuracy")
        if ac:
            _brief.append("昨日Top%d兑现率 %s%%%s" % (ac.get("topn"), ac.get("hit_rate"),
                                                     ("，失准%d只已归因" % ac["n_miss"]) if ac.get("n_miss") else ""))
        if _brief:
            _sec("🧭 今日作战指令", _brief[:8])
    except Exception:
        pass
    # 去重：买点候选(加速优先/其他)与牛股雷达/经典策略同源 bull/strategies，
    # 已作为买点展示的票不再在雷达里重复列，避免同一只票在推送里出现两次。
    _bp_codes = set()
    for _g in (_bp.get("accel") or []):
        if not _g.get("warn_sell") and _g.get("code"):
            _bp_codes.add(_g["code"])
    for _g in (_bp.get("others") or []):
        if not _g.get("warn_sell") and _g.get("code"):
            _bp_codes.add(_g["code"])
    bull = [b for b in (data.get("bull") or []) if b.get("code") not in _bp_codes] if _on("bull") else []
    _sec("🐂 牛股雷达", [
        "**%s**〔%s〕%.2f元 %+.1f%%"
        % (b.get("name", "?"), "+".join((b.get("signals") or [])[:2]),
           b.get("price") or 0, b.get("pct") or 0)
        for b in bull[:4]])
    strat = [s for s in (data.get("strategies") or []) if s.get("code") not in _bp_codes] if _on("strat") else []
    _sec("🎯 经典策略", [
        "**%s**〔%s〕%.2f元 %+.1f%%"
        % (s.get("name", "?"), "+".join((s.get("signals") or [])[:2]),
           s.get("price") or 0, s.get("pct") or 0)
        for s in strat[:4]])
    if mode in ("close", "close_again"):
        try:
            import yaogu as _yg
            rk = ((data.get("yaogu") or {}).get("ranked")) or [] if _on("yaogu") else []
            _sec("⚡ 妖股潜力", [
                "**%s** %.0f分（%s）"
                % (r.get("name", "?"), r.get("score", 0) or 0, r.get("sector", "—") or "—")
                for r in rk[:3]])
        except Exception:
            pass
    avoid = (rec.get("avoid") or []) if _on("avoid") else []
    if not avoid and _on("avoid") and rec.get("all"):
        avoid = sorted(rec["all"], key=lambda x: -(x.get("p_break") or 0))[:2]
    # 负反馈闭环：高位回避行标注否决原因（放量接力高风险 vs 普通高位）
    _sec("🚫 高位回避", [
        "**%s**(%s·断板%.0f%%)%s"
        % (a.get("name", "?"), _board(a), a.get("p_break", 0) or 0,
           (" ⛔%s" % a["veto_reason"][:30] if a.get("veto_reason") else ""))
        for a in avoid[:3]])
    # 负反馈闭环：历史胜率透明化（真实的过滤改造证据，随 rec_picks 增长自动更新）
    if _on("rec"):
        try:
            import recveto as _rvt2
            _hint = _rvt2.quality_hint(con)
            if _hint:
                L.append("")
                L.append("🧪 **筛选改进实证**：%s（已剔除高位放量接力票——"
                         "该画像历史胜率仅 33%%）" % _hint)
        except Exception:
            pass
    pn = data.get("panic") or {}
    if pn.get("level") in ("升温", "恐慌"):
        L.append("")
        L.append("⚠️ 恐慌扫描：**%s**（跌停%d 大面%d 昨涨停收绿%.0f%%）"
                 % (pn.get("level"), pn.get("dt_count", 0), pn.get("bigface_count", 0),
                    pn.get("yest_green") or 0))
    # ---- 冷启修复节奏预判（coldwave，仅冷中/冷后窗口才有预判行）----
    if _on("cold"):
        try:
            import coldwave as _cwmod
            cw = data.get("cold")
            clines = _cwmod.summary_lines(cw) if cw else []
            if clines:
                L.append("")
                L.append("**❄️ 冷启修复预判**")
                for x in clines:
                    L.append("- %s" % x)
                cands = (cw.get("candidates") or [])[:3]
                if cands:
                    L.append("- 冷后风格候选：%s" % "、".join(
                        "**%s**(%.0f分·%s)" % (c.get("name", "?"), c.get("score", 0) or 0,
                                               c.get("ind") or "—")
                        for c in cands))
        except Exception:
            pass
    # ---- 跳空缺口扫描（回补规律 + 当前未回补清单）----
    if _on("gaps"):
        try:
            import gapscan as _gpmod
            gp = data.get("gaps")
            glines = _gpmod.summary_lines(gp) if gp else []
            if glines:
                L.append("")
                L.append("**🕳 跳空缺口扫描**")
                for x in glines[:4]:
                    L.append("- %s" % x)
        except Exception:
            pass
    # ---- 六维检测雷达：市场风格/地量/52周广度/均线粘合/铡刀芙蓉/尾盘偷袭 ----
    if _on("style"):
        try:
            import stylereg as _stymod
            sty = data.get("stylereg")
            slines = _stymod.summary_lines(sty) if sty else []
            if slines:
                L.append("")
                L.append("**🧭 市场风格判定**")
                for x in slines[:4]:
                    L.append("- %s" % x)
        except Exception:
            pass
    if _on("dry"):
        try:
            import dryvol as _dvmod
            dv = data.get("dryvol")
            dlines = _dvmod.summary_lines(dv) if dv else []
            if dlines:
                L.append("")
                L.append("**💧 地量/缩量变盘窗口**")
                for x in dlines[:3]:
                    L.append("- %s" % x)
        except Exception:
            pass
    if _on("nh"):
        try:
            import newhigh as _nhmod
            nb = data.get("newhigh")
            nlines = _nhmod.summary_lines(nb) if nb else []
            if nlines:
                L.append("")
                L.append("**🏔 52周新高新低广度**")
                for x in nlines[:3]:
                    L.append("- %s" % x)
        except Exception:
            pass
    if _on("glue"):
        try:
            import maglue as _glmod
            gg = data.get("maglue")
            glines2 = _glmod.summary_lines(gg) if gg else []
            if glines2:
                L.append("")
                L.append("**🧲 均线粘合待变盘**")
                for x in glines2[:3]:
                    L.append("- %s" % x)
        except Exception:
            pass
    if _on("sword"):
        try:
            import trendsword as _swmod
            cf = data.get("trendsword")
            swlines = _swmod.summary_lines(cf) if cf else []
            if swlines:
                L.append("")
                L.append("**⚔️ 断头铡刀 / 出水芙蓉**")
                for x in swlines[:3]:
                    L.append("- %s" % x)
        except Exception:
            pass
    if _on("tail") and data.get("tailraid"):
        try:
            import tailraid as _trmod
            tr = data["tailraid"]
            # 只在有实际异动时占用推送条数；全无异动不推
            has_move = (tr.get("raid_n", 0) + tr.get("dump_n", 0)) > 0
            tlines = _trmod.summary_lines(tr) if has_move else []
            if tlines:
                L.append("")
                L.append("**🌙 尾盘偷袭监测**")
                for x in tlines[:3]:
                    L.append("- %s" % x)
        except Exception:
            pass
    if _on("wl"):
        try:
            import watchlist as _wlmod
            wl = data.get("watch")
            wlines = _wlmod.summary_lines(wl) if wl else []
            if wlines:
                L.append("")
                L.append(_MARK_WL)
                L.append("**⭐ 关注股雷达**")
                for x in wlines[:3]:
                    L.append("- %s" % x)
                L.append(_MARK_WL_END)
        except Exception:
            pass

    # ---- 新引擎分区（统一：先收集 lines，标题只加一次；行已带 "- " 则不重复加）----
    # mark=(起标记, 止标记)：个人分区（zones）用，供 _strip_personal_sections 成对剥离
    def _engine_sec(title, mod, key, cap=4, mark=None):
        d = data.get(key)
        if not d:
            return
        try:
            lines = mod.summary_lines(d) or []
            if not lines:
                return
            L.append("")
            if mark:
                L.append(mark[0])
            L.append(title)
            for ln in lines[:cap]:
                L.append(ln if ln.startswith("- ") else "- %s" % ln)
            if mark:
                L.append(mark[1])
        except Exception:
            pass

    import recperf as _rpm
    _engine_sec("**📈 推荐池胜率回溯**", _rpm, "recperf", cap=3)

    # ---- 推荐多维归因（2026-08-30：st=2 监控/特征分桶/落袋挽回，随 rec_picks 积累自动更新）----
    if data.get("rec_attr"):
        try:
            import recattr as _rattr
            _al = _rattr.summary_lines(data["rec_attr"])
            if _al:
                L.append("")
                L.append("**🔬 推荐归因**")
                for ln in _al[:4]:
                    L.append("- %s" % ln)
        except Exception:
            pass

    if _on("style") and data.get("style_switch"):
        try:
            sb = data["style_switch"]
            L.append("")
            L.append("**🔁 风格切换回测**")
            L.append("- 历史 %d 次切换，其后 %d 日上涨占比 **%s%%**（平均收益 %s%%）"
                     % (sb["n"], sb["look"], sb["up_rate"], sb["avg_ret"]))
            bt = sb.get("by_target") or {}
            if bt:
                top_bt = sorted(bt.items(), key=lambda kv: (kv[1]["up_rate"] or 0), reverse=True)[:2]
                L.append("- 切换去向占优风格：" + "；".join(
                    "%s（%s%% 涨）" % (stylereg.style_cn(k), v["up_rate"]) for k, v in top_bt))
        except Exception:
            pass

    import lhbseats as _lhb
    _engine_sec("**🏦 龙虎榜席位**", _lhb, "lhbseats", cap=5)
    import riskcal as _rc
    _engine_sec("**⚠️ 解禁/财报雷区**", _rc, "riskcal", cap=4)
    import blocktrade as _bt
    _engine_sec("**📜 大宗交易折价**", _bt, "blocktrade", cap=4)
    import margin as _mg
    _engine_sec("**💳 两融余额**", _mg, "margin", cap=2)
    import etfflow as _ef
    _engine_sec("**🧺 ETF 资金流**", _ef, "etfflow", cap=4)
    import patsim as _ps
    _engine_sec("**🧬 相似形态检索**", _ps, "patsim", cap=4)

    import seats as _st
    _engine_sec("**🐉 游资席位画像**", _st, "seats", cap=5)
    import theme as _th
    _engine_sec("**🧭 题材主线**", _th, "theme", cap=4)
    import signals as _sg
    _engine_sec("**📡 连续信号**", _sg, "signals", cap=5)
    import chanlun as _cl
    _engine_sec("**🌀 缠论结构**", _cl, "chanlun", cap=6)
    import zones as _zn
    _engine_sec("**🎯 买卖区间与操作提示**", _zn, "zones", cap=6,
                mark=(_MARK_ZN, _MARK_ZN_END))

    # ---- Batch1 升级：已算好但未推送的高信噪比段（席位跟随/回避、模拟盘、板块轮动、K线/筹码、龙虎榜、资金流）----
    # 全段 try/except 包裹：任一字段缺失或解析异常都不影响主流程（零回归）。
    try:
        # 1) 游资席位·可跟 / 回避
        _sf = data.get("seat_follow") or {}
        if _sf.get("items"):
            _sec("🐉 游资席位·可跟", [
                "**%s** 胜率%.0f%%（%d次）%s" % (
                    it.get("label", "?"), it.get("win_rate", 0) or 0, it.get("n", 0),
                    ("｜代表：" + "、".join("%s" % (r.get("name") or r.get("code")) for r in (it.get("reps") or [])[:3]))
                    if (it.get("reps")) else "")
                for it in _sf["items"][:4]])
        _sa = data.get("seat_avoid") or {}
        if _sa.get("items"):
            _sec("🚫 游资席位·回避（负期望慎跟）", [
                "**%s** 胜率%.0f%%（%d次）%s" % (
                    it.get("label", "?"), it.get("win_rate", 0) or 0, it.get("n", 0),
                    ("｜代表：" + "、".join("%s" % (r.get("name") or r.get("code")) for r in (it.get("reps") or [])[:3]))
                    if (it.get("reps")) else "")
                for it in _sa["items"][:4]])
    except Exception:
        pass
    try:
        # 2) 模拟盘每日战绩
        _sim = data.get("sim") or {}
        _sl = _sim.get("last") or {}
        if _sl:
            _hb = (_sim.get("heartbeat") or {})
            _rows = ["累计 %+.2f%% ｜ 现金%.0f ｜ 持仓%d只%s" % (
                _sl.get("total_pct") or 0, _sl.get("cash") or 0, _sl.get("n_holding") or 0,
                "（⚠数据滞后）" if _hb.get("stale") else "")]
            _nt = len(_sl.get("trades") or [])
            _nc = len(_sl.get("closed") or [])
            _nr = len(_sl.get("rejects") or [])
            if (_nt or _nc or _nr):
                _rows.append("今日：开/加%d笔 ｜ 平仓%d笔 ｜ 被拒%d笔" % (_nt, _nc, _nr))
            if _sl.get("summary_line"):
                _rows.append(_sl["summary_line"])
            # #440（2026-09-03）：持仓/成交/平仓明细——让用户看清买了哪几只、成本、数量、实时盈亏
            _hp = _sl.get("holding_plans") or []
            _trd = _sl.get("trades") or []
            _cls = _sl.get("closed") or []
            if _hp:
                _rows.append("**持仓明细（%d 只）**" % len(_hp))
                for hp in _hp[:8]:
                    _mv = (" ｜ %d股·%.0f元" % (hp.get("volume") or 0, hp.get("market_value") or 0)) \
                        if hp.get("market_value") else ""
                    _rows.append("- %s(%s) 成本%.2f→现%.2f %s%s"
                                 % (hp.get("name") or hp.get("code"), hp.get("code"),
                                    hp.get("avg_price") or 0, hp.get("price") or 0,
                                    ("浮盈%+.2f%%" % hp["pnl_pct"]) if hp.get("pnl_pct") is not None else "浮盈—",
                                    _mv))
            if _trd:
                _rows.append("**今日成交（%d 笔）**" % len(_trd))
                for t in _trd[:6]:
                    _act = "🟢买" if t.get("action") == "BUY" else "🔴卖"
                    _rows.append("- %s %s(%s) %.2f×%d股"
                                 % (_act, t.get("name") or t.get("code"), t.get("code"),
                                    t.get("price") or 0, t.get("volume") or 0))
            if _cls:
                _rows.append("**今日平仓（%d 笔）**" % len(_cls))
                for c in sorted(_cls, key=lambda x: -(x.get("pnl_pct") or 0))[:6]:
                    _rows.append("- %s(%s) %+.2f%%" % (c.get("name") or c.get("code"),
                                                       c.get("code"), c.get("pnl_pct") or 0))
            _sec("🤖 模拟盘战绩", _rows)
    except Exception:
        pass
    try:
        # 3) 板块轮动·主线强度 + 可操作 + 接力/退潮
        _rot = data.get("rotation") or []
        if _rot:
            _sec("🔄 板块轮动·主线强度", [
                "%s 涨停%d家 %s%s" % (
                    s.get("name"), s.get("today") or 0, s.get("trend"),
                    "（持续）" if s.get("persistent") else ("（新晋）" if s.get("is_new") else ""))
                for s in _rot[:4]])
        _st = data.get("sector_trade") or []
        if _st:
            _sec("🎯 板块轮动·可操作", [
                "%s（%s）%s" % (
                    s.get("sector"), s.get("trend"),
                    ("领涨：" + "、".join("%s(+%.1f%%)" % (l.get("name"), (l.get("chg") or 0)) for l in (s.get("leads") or [])[:3]))
                    if (s.get("leads")) else "")
                for s in _st[:3]])
        _rel = data.get("sector_relay") or {}
        if _rel.get("available") and (_rel.get("broken") or _rel.get("relay")):
            _rrows = []
            if _rel.get("broken"):
                _b = _rel["broken"]
                _rrows.append("退潮旧主线：**%s**（峰值%d→%d家）" % (_b.get("name"), _b.get("peak_zt"), _b.get("latest_zt")))
            for _r in (_rel.get("relay") or [])[:3]:
                _rrows.append("接力方向：**%s**（%s，确定性%d%%）" % (_r.get("name"), _r.get("kind"), _r.get("certainty") or 0))
            if _rel.get("phase_desc"):
                _rrows.append(_rel["phase_desc"])
            _sec("↔️ 主线接力 / 退潮", _rrows)
    except Exception:
        pass
    try:
        # 4) K线形态 + 筹码
        _cd = data.get("candles") or {}
        _ch = _cd.get("hits") or []
        if _ch:
            _bull = [h for h in _ch if (h.get("direction") or "") == "bull"][:3]
            _bear = [h for h in _ch if (h.get("direction") or "") == "bear"][:2]
            _rows = []
            for h in _bull:
                _rows.append("看多 %s **%s**(%.1f%%)" % (h.get("pattern"), h.get("name"), (h.get("pct") or 0)))
            for h in _bear:
                _rows.append("看空 %s **%s**(%.1f%%)" % (h.get("pattern"), h.get("name"), (h.get("pct") or 0)))
            if _rows:
                _sec("🕯 K线形态", _rows)
        _cp = data.get("chips") or {}
        _tl = _cp.get("top_low") or []
        if _tl:
            _sec("🧩 筹码·套牢盘最重（超跌区）", [
                "**%s**（获利盘%.0f%%）" % (h.get("name"), (h.get("ratio") or 0) * 100)
                for h in _tl[:3]])
    except Exception:
        pass
    try:
        # 5) 龙虎榜上榜个股（游资合力）
        _lhb = data.get("lhb") or {}
        if _lhb:
            _items = sorted(_lhb.items(), key=lambda kv: -(kv[1].get("net_amt") or 0))[:5]
            _sec("🏛 龙虎榜上榜个股（游资合力）", [
                "**%s** %+.1f%% 净买%.0f万 %d席｜%s" % (
                    it.get("name") or code, it.get("change_rate") or 0,
                    (it.get("net_amt") or 0) / 1e4, it.get("buy_seat") or 0,
                    (it.get("explanation") or "")[:18])
                for code, it in _items])
    except Exception:
        pass
    try:
        # 6) 主力资金流（近5日，东财 fflow daykline 字符串：date,开,收,高,低,量,额,振,涨%,涨跌,换手,主力净流入(f62·元),...）
        _ff = (data.get("market") or {}).get("fundflow") or []
        if _ff:
            _vals = []
            for _k in _ff[-5:]:
                try:
                    if isinstance(_k, str) and "," in _k:
                        _parts = _k.split(",")
                        if len(_parts) > 11:
                            _v = float(_parts[11])  # f62 主力净流入(元)
                            if abs(_v) > 1e4:       # 量级保护：避免误抓换手率/涨跌幅字段
                                _vals.append(_v / 1e8)
                except Exception:
                    pass
            if _vals:
                _sec("💰 主力资金流（近5日·亿）", [" ".join("%+.1f" % v for v in _vals)])
    except Exception:
        pass

    try:
        # 7) 运维告警（2026-09-01 升级 #16）：仅异常时提示，避免刷屏
        _ops = data.get("ops") or {}
        _notes = _ops.get("notes") or []
        if _notes:
            _ops_rows = []
            if _ops.get("data_age_days") is not None:
                _ops_rows.append("行情库数据年龄 %d 日" % _ops["data_age_days"])
            if _ops.get("fetch_success_rate") is not None:
                _ops_rows.append("抓取成功率 %.0f%%（%d/%d 池）"
                                 % (_ops["fetch_success_rate"], _ops["fetch_pool_ok"],
                                    _ops["fetch_pool_total"]))
            for _n in _notes:
                _ops_rows.append("⚠ %s" % _n)
            _sec("🛠 运维告警", _ops_rows)
    except Exception:
        pass

    try:
        # Batch3 #12：盘中异动上墙（仅当日有异动时提示）
        _intra = data.get("intraday") or {}
        _ial = _intra.get("alerts") or []
        if _ial:
            _intra_rows = []
            for _a in _ial[:8]:
                _intra_rows.append("%s（%s）%s @ %.2f"
                                   % (_a.get("name") or _a.get("code"), _a.get("code"),
                                      _a.get("type"), _a.get("price") or 0))
            _sec("📡 盘中异动（%d 条）" % len(_ial), _intra_rows)
    except Exception:
        pass

    # 数据完整性体检（仅异常时告警，健康时静默，避免刷屏）
    ig = data.get("integrity")
    if ig and not ig.get("ok") and (ig.get("warnings") or ig.get("scale_anomalies")):
        L.append("")
        L.append("**🔍 数据完整性告警**")
        for w in (ig.get("warnings") or [])[:6]:
            L.append("- ⚠ %s" % w)
        sa = ig.get("scale_anomalies") or []
        if sa:
            L.append("- 量纲错乱交易日：%s" % ",".join(b.get("date", "?") for b in sa[:8]))
        aa = ig.get("amount_anomalies") or []
        if aa:
            L.append("- 成交额跳变交易日：%s"
                     % ",".join("%s(%.1fx)" % (b.get("date", "?"), b.get("amount_ratio") or 0)
                                for b in aa[:8]))
        L.append("- 样本 %s 天，最新 %s 覆盖 %s 只"
                 % (ig.get("trade_days"), ig.get("last_date"), ig.get("last_day_rows")))

    if mode == "close_again":
        hrep = data.get("holdings")
        alerts = (hrep or {}).get("alerts") or []
        if hrep and hrep.get("enabled") and alerts:
            _sec("📡 持仓预警", [a for a in alerts[:4] if a])
    # ---- 尾盘决策通道（2026-08-29）：次日开盘「双确认」清单 ----
    _ls = data.get("late_session") or {}
    if _ls.get("watch_tomorrow") or _ls.get("exit_warn"):
        _ls_rows = []
        for w in (_ls.get("watch_tomorrow") or [])[:6]:
            _ls_rows.append("**%s**(%s%s) %s｜次日竞价：高开≥2%%跟进/低开弃"
                            % (w.get("name"), w.get("streak") and "%d板" % w["streak"] or "",
                               ("/" + w["tag"] if w.get("tag") else ""),
                               (w.get("auction_rule") or "")[:0]))
        for w in (_ls.get("exit_warn") or [])[:3]:
            _ls_rows.append("⚠ %s 竞价疑似派发 → 次日按弱势预案，低开即弃" % w.get("name"))
        if _ls_rows:
            _sec("⏱ 尾盘确认 · 次日竞价双确认", _ls_rows)
    # ---- 收尾纪律条：一句话记住今天怎么干（排版清爽化：结论前置、纪律收尾）----
    _disc = _discipline_line(data, rec)
    if _disc:
        L.append("")
        L.append(_disc)
    if url:
        L.append("")
        L.append("🔗 看板 %s" % url)
    return "\n".join(L)


def _discipline_line(data, rec):
    """收尾『纪律一句话』：仓位 + 卖压 + 买卖触发，避免读者抓不住重点。"""
    rec = rec or {}
    pa = data.get("position_advice") or {}
    pp = data.get("preopen_plan") or {}
    pos = pa.get("suggest_pct")
    if pos:
        pos_s = "总仓位≤%d%%" % pos
    elif pp.get("position"):
        pos_s = "仓位 %s" % pp["position"]
    else:
        pos_s = "控仓为主"
    wr = rec.get("watch_reco") or {}
    n_sell = wr.get("sell_n") or 0
    sell_s = ("🟢 关注票 %d 只触发卖出 → 先处理卖" % n_sell) if n_sell else "无强制卖出信号"
    return "> 📌 纪律：%s ｜ %s ｜ 买点只认买区、低开< -0.1%%弃 ｜ 破止损无条件走" % (pos_s, sell_s)


def _ladder_plan_lines(rec, aitems=None, n=6, compact=False):
    """连板机会计划 → 推送行（盘前/竞价共用）。

    aitems：竞价数据 {code: {...open_pct...}}。有竞价时按实际开盘做 gate 复核：
      低开( <recveto.LOW_OPEN )计划标 🚫放弃；其余标注低/高开与是否达标买区。
    compact：SC 紧凑板式（去掉 evidence 长尾）。
    """
    LP = rec.get("ladder_plans") or []
    if not LP:
        return []
    lines = []
    for i, p in enumerate(LP[:n], 1):
        bz = p.get("buy_zone") or [0, 0]
        sz = p.get("sell_zone") or [0, 0]
        mark = ""
        opv = None
        if aitems:
            aq = aitems.get(p.get("code")) or {}
            opv = aq.get("open_pct")
        if p.get("gate") == "avoid" or (opv is not None and opv < -0.1):
            mark = " 🚫低开放弃"
        elif opv is not None:
            inzone = bz[0] <= (aq.get("open") or 0) <= bz[1] if aq.get("open") else None
            mark = " ✅达标买区" if inzone else " 竞价%.1f%%" % opv
        line = "- %d. **%s**(%s→%s) 买%.2f~%.2f 卖%.2f~%.2f 止损%.2f%s" % (
            i, p.get("name", "?"), p.get("entry_streak", 0),
            (p.get("expected_top") or "").split("→")[-1],
            bz[0], bz[1], sz[0], sz[1], p.get("stop", 0), mark)
        if not compact and p.get("evidence"):
            line += "\n  · %s" % str(p["evidence"])[:60]
        lines.append(line)
    return lines


def _rec_action_line(it, lp_map):
    """推荐候选第二行：明确「跟着做」的动作 + 买卖价格（P4 用户需求）。

    lp_map：{code: 连板计划单}——有计划单直接引用其买卖区/止损/目标；
    无计划按断板概率/风险标注给通用竞价纪律。"""
    code = it.get("code")
    p = (lp_map or {}).get(code)
    if p:
        bz = p.get("buy_zone") or [0, 0]
        sz = p.get("sell_zone") or [0, 0]
        return ("→ ✅ 竞价达标买 **%.2f~%.2f** ｜ 目标 %.2f~%.2f ｜ 止损 %.2f ｜ 持有%d日（低开<-0.1%%弃）"
                % (bz[0], bz[1], sz[0], sz[1], p.get("stop") or 0, p.get("hold_days", 2)))
    if it.get("risk_flag"):
        return "→ ⚠ 高危（%s）：≤2成仓快进快出，竞价弱直接放弃" % ((it.get("veto_reason") or "放量接力")[:12])
    if (it.get("p_break") or 0) >= 78:
        return "→ 冲高兑现为主不追高；低开(<-0.1%)放弃，破均价线离场"
    return "→ 竞价强势可跟（首仓≤3成），低开(<-0.1%)放弃，破-5%止损"


def watchreco_lines(data, n=6, compact=False):
    """自选/持仓操作结论 → 推送行（watchreco.distill 产物渲染）。"""
    wr = (data.get("recommend") or {}).get("watch_reco")
    if not wr or not (wr.get("items")):
        return []
    try:
        import watchreco as _wc
    except Exception:
        try:
            from pipeline import watchreco as _wc
        except Exception:
            return []
    # 2026-09-03：把推荐池一并传入（replace 候选被过滤清空时，用连板计划兜底补上可买的票）
    out = _wc.lines(wr, n=n, compact=compact, rec=(data.get("recommend") or {}))
    if out and not compact:
        out.insert(0, "> 持仓给卖出/加仓/持有动作，自选给买入时机；完整买卖区见看板。")
    return out


def _watch_action_by_sector(act, dirn):
    """关注票动作 × 板块当日预判 → 可执行指令（用户需求：盘前结合板块预测当日涨跌）。

    纪律优先原则：个股卖出信号 > 板块方向；板块只调节「买入/持有」的力度。
    """
    act = act or "观望"
    dirn = dirn or "震荡"
    if act.startswith("卖出") or act == "离场换强":
        if dirn == "偏强":
            return "%s（板块偏强，但个股已触发卖出纪律 → 纪律优先，冲高兑现）" % act
        return "%s（板块%s → 卖出纪律优先，不恋战）" % (act, dirn)
    if act in ("建议买入", "回踩买入", "加仓"):
        if dirn == "偏强":
            return "%s（板块偏强 → 可在买区上沿介入）" % act
        if dirn == "偏弱":
            return "轻仓%s（板块偏弱 → 等回踩买区下沿再介入）" % act
        return "%s（板块震荡 → 按买区执行，不追高）" % act
    if act == "减仓":
        return "减仓（板块%s → 按计划降低仓位）" % dirn
    if dirn == "偏强":
        return "持有待涨（板块偏强 → 可持股不动）"
    if dirn == "偏弱":
        return "冲高减仓（板块偏弱 → 反弹兑现）"
    return "持有（板块震荡 → 按买卖区执行）"


def zone_buyable(price, buy_zone, tol=1.05):
    """2026-09-02 用户拍板：现价是否「可以直接买」——现价 ≤ 买区上沿×tol（默认5%容差）。
    飞在天上的票（现价远超买区，等回落到买点也不值得追）返回 False。
    无买区信息时返回 None（调用方决定不误杀）。

    注意：buy_zone 锚在 MA20/结构低点，对强势票天然偏远（实测 09-03 十只趋势票
    九只现价高出买区 8%~47%）。因此优先用 entry_state（近端可执行买点）判定，
    见 entry_buyable / item_buyable。本函数保留为无 entry 数据时的兼容路径。"""
    try:
        if not buy_zone or buy_zone[1] is None or price is None:
            return None
        return float(price) <= float(buy_zone[1]) * tol
    except Exception:
        return None


def entry_buyable(item):
    """2026-09-03 用户拍板（不要再推「在天上」的票）：以 zones.entry_plan 的
    entry_state 为准判「现价能不能买」。

    可买/微超 → True；等回踩/过热勿追 → False；无 entry 数据 → None（交给
    调用方回退到 zone_buyable，避免误杀数据不足的票）。"""
    if not isinstance(item, dict):
        return None
    st = item.get("entry_state") or ((item.get("entry") or {}).get("entry_state"))
    if not st:
        vd = item.get("verdict")
        if isinstance(vd, dict):
            st = vd.get("entry_state")
    if not st:
        return None
    return st in ("可买", "微超")


def item_buyable(item, price_key=None, zone_key=None):
    """统一「可买」判定：entry_state 优先，缺失时回退买区容差判定。"""
    ok = entry_buyable(item)
    if ok is not None:
        return ok
    p = price_key(item) if price_key else item.get("close")
    z = zone_key(item) if zone_key else item.get("buy_zone")
    return zone_buyable(p, z)


def entry_badge(item):
    """行尾买点徽标：✅现价可买 / 🟡小仓试 / ⏳等回踩X.XX(-Y%) / 🚫过热勿追(+Z%)。"""
    if not isinstance(item, dict):
        return ""
    ent = item.get("entry") or {}
    st = item.get("entry_state") or ent.get("entry_state")
    if not st:
        vd = item.get("verdict") or {}
        st = vd.get("entry_state") if isinstance(vd, dict) else None
        ent = ent or {}
    if not st:
        return ""
    gap = item.get("entry_gap_pct")
    if gap is None:
        gap = ent.get("entry_gap_pct")
    wait = ent.get("wait_price") or item.get("wait_price")
    if ent.get("broken"):
        return " ｜ 🚫**已破位不接**"
    if st == "可买":
        nz = ent.get("now_zone") or item.get("now_zone")
        return (" ｜ ✅**现价可买 %.2f~%.2f**" % (nz[0], nz[1])) if nz else " ｜ ✅**现价可买**"
    if st == "微超":
        return " ｜ 🟡**小仓试**(超买点%+.1f%%)" % (gap or 0.0)
    if st == "等回踩":
        return (" ｜ ⏳等回踩 **%.2f**(需回落%.1f%%)"
                % (wait, abs(ent.get("wait_drop_pct") or 0.0))) if wait else " ｜ ⏳等回踩"
    return " ｜ 🚫**过热勿追**(超买点%+.1f%%)" % (gap or 0.0)


def buyable_first(seq, price_key, zone_key):
    """把「现价在买点上/贴近买点」的票排到最前（可买优先，飞在天上的靠后）。
    price_key/zone_key: 从元素取现价与买区 [lo,hi] 的 callable（entry 缺失时的回退）。
    2026-09-03：优先看 entry_state，其次才用买区容差。无信息的票排最后不误杀。"""
    def k(x):
        st = (x.get("entry_state") if isinstance(x, dict) else None) \
            or ((x.get("entry") or {}).get("entry_state") if isinstance(x, dict) else None)
        if st:
            return {"可买": 0, "微超": 1, "等回踩": 3, "过热勿追": 4}.get(st, 2)
        ok = zone_buyable(price_key(x), zone_key(x))
        if ok is True:
            return 0
        if ok is None:
            return 2
        return 3
    return sorted(seq, key=k)


def drop_unbuyable(seq):
    """从「买入推荐」序列里剔除 等回踩/过热勿追/已破位 的票（推送门禁）。
    entry 缺失的票保守保留。返回 (保留列表, 被剔除列表)。"""
    keep, cut = [], []
    for x in seq:
        ok = entry_buyable(x)
        (keep if ok is not False else cut).append(x)
    return keep, cut


def _act_emoji(act):
    """买卖动作 → 红绿 emoji 标记（A股惯例：红=买/涨/推荐，绿=卖/跌/回避）。

    推送为 Markdown，无法着色文字，用 🔴/🟢/⚪ 圆形 emoji 作视觉区分。
    """
    a = act or ""
    if a.startswith("卖出") or "卖出" in a or a in ("离场换强", "减仓", "止损", "跌破警示") \
            or a.startswith("回避"):
        return "🟢"
    if a in ("建议买入", "回踩买入", "加仓") or "买入" in a:
        return "🔴"
    return "⚪"


def watch_forecast_lines(data, n=8):
    """盘前「关注票操作说明」行：每只关注票 = 板块当日预判 + 明确动作。"""
    rec = data.get("recommend") or {}
    wr = rec.get("watch_reco") or {}
    items = wr.get("items") or []
    if not items:
        return []
    sfc = data.get("sector_forecast") or {}
    mkt = sfc.get("__market__") or {}
    out = []
    if mkt:
        out.append("> 大盘环境：**%s**（%d分）· %s"
                   % (mkt.get("dir"), mkt.get("score", 50), mkt.get("why", "—")))
    for x in items[:n]:
        ind = x.get("industry") or "—"
        f = sfc.get(ind) or {}
        dirn = f.get("dir") or "震荡"
        icon = {"偏强": "🔴", "偏弱": "🟢", "震荡": "⚪"}.get(dirn, "⚪")
        tag = "持仓" if x.get("is_holding") else "自选"
        seg = "- %s **%s**" % (tag, x.get("name") or "?")
        if x.get("close"):
            seg += " %.2f" % x["close"]
        if x.get("pnl_pct") is not None:
            seg += " 浮盈%+.1f%%" % x["pnl_pct"]
        seg += " ｜ %s%s 预判%s" % (ind if ind != "—" else "无板块", icon, dirn)
        if f.get("score") is not None:
            seg += "(%d)" % f["score"]
        seg += " → %s **%s**" % (_act_emoji(x.get("action")), _watch_action_by_sector(x.get("action"), dirn))
        bz = x.get("buy_zone") or [None, None]
        if bz[0] and (x.get("action") or "") in ("建议买入", "回踩买入", "加仓"):
            seg += " ｜ 买区%.2f~%.2f" % (bz[0], bz[1])
        elif (x.get("action") or "").startswith("卖出") and x.get("stop"):
            seg += " ｜ 止损%.2f" % x["stop"]
        out.append(seg)
    return out


def _market_outlook(data):
    """盘前「今日研判」：基于短期形势给出预期判断（用户 2026-08-31 需求：
    开盘前一日/当日要的是研判推送，而不是把周五的复盘再推一遍）。

    纯聚合已有引擎结论（不新增行情计算）：
      · sector_forecast.__market__  大盘环境定调（dir/env/score）
      · market.sentiment / cycle    情绪温度 + 情绪周期位置
      · market.profit               昨日赚钱效应（昨涨停今日表现，短期接力参考）
      · regime.level                连板情绪阶段（过热=兑现压力/退潮=防守）
      · global_market.signal        外围定调
      · preopen_plan.position       仓位建议

    返回 (预期判断 str, 摘要 dict)；数据缺失时各部分诚实降级。
    """
    d = data or {}
    mkt = d.get("sector_forecast", {}).get("__market__") or {}
    sent = d.get("market", {}).get("sentiment", {}) or {}
    cyc = d.get("market", {}).get("cycle", {}) or {}
    micro = d.get("micro") or {}
    profit = micro.get("profit") or {}
    regime = d.get("regime") or {}
    g = d.get("global_market") or {}
    pp = d.get("preopen_plan") or {}

    dirn = mkt.get("dir") or ""
    env = mkt.get("env")
    s_score = sent.get("score")
    phase = cyc.get("phase") or ""
    rl = regime.get("level") or ""
    gs = (g.get("signal") or "") if g.get("available") else ""

    # ---- 逐维度收集证据 ----
    evid = []
    if dirn:
        evid.append("大盘预判「%s」" % dirn)
    if env is not None:
        try:
            evid.append("环境系数 %+.0f" % float(env))
        except (TypeError, ValueError):
            pass
    if s_score is not None:
        try:
            evid.append("情绪 %.0f/100" % float(s_score))
        except (TypeError, ValueError):
            pass
    if phase:
        evid.append("周期「%s」" % phase)
    if rl:
        evid.append("连板「%s」" % rl)
    if gs:
        evid.append("外围%s" % gs)
    avg = profit.get("avg_pct")
    if avg is not None:
        try:
            evid.append("昨涨停今均 %+.1f%%" % float(avg))
        except (TypeError, ValueError):
            pass

    # ---- 拼预期判断（方向 + 仓位 + 节奏）----
    bits = []
    # 方向判断
    if dirn == "偏弱" or (env is not None and _f(env) <= -5):
        bits.append("**偏防守**：今日以控风险为主，不追高、不做弱修复")
    elif dirn == "偏强" or (env is not None and _f(env) >= 5):
        bits.append("**偏进攻**：主线与接力方向可正常参与，但竞价弱一律不接")
    else:
        bits.append("**震荡预期**：结构性机会为主，聚焦资金集中的 1~2 个方向")
    # 仓位
    if pp.get("position"):
        bits.append("建议仓位 %s" % pp["position"])
    # 节奏（赚钱效应 / 情绪阶段）
    if isinstance(avg, (int, float)) and avg < -2:
        bits.append("昨日接力亏钱（昨涨停今均%+.1f%%），今日接力降档" % avg)
    elif isinstance(avg, (int, float)) and avg > 2:
        bits.append("昨日接力赚钱效应好（昨涨停今均%+.1f%%），可适度积极" % avg)
    if "过热" in rl or "高潮" in rl:
        bits.append("连板情绪过热，警惕高位兑现")
    elif "退潮" in rl or "冰点" in rl:
        bits.append("连板情绪退潮，优先低位新题材")
    head = "、".join(evid[:6])
    judge = "%s\n> 依据：%s" % ("；".join(bits), head or "引擎数据暂缺，保守处理")
    return judge, {"dirn": dirn, "env": env, "sentiment": s_score, "phase": phase}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def format_stock_summary(data, url="", mode="close", con=None):
    """ServerChan/PushPlus(markdown) 排版：盘面数据 / 复盘要点 / 推荐 / 风险，分区清晰、留白克制。
    mode='close' 收盘后；mode='preauction' 竞价前。"""
    m = data.get("meta", {})
    date = m.get("date", "")
    sent = data.get("market", {}).get("sentiment", {}) or {}
    cyc = data.get("market", {}).get("cycle", {}) or {}
    g = data.get("global_market") or {}
    lus = data.get("limit_ups", []) or []
    rec = data.get("recommend", {}) or {}
    narr = data.get("narrative", {}) or {}
    regime = data.get("regime") or {}

    if mode == "preauction":
        L = []
        L.append("## 今日研判 · %s（盘前预期，非昨日复盘）" % date)
        L.append("")
        L.append("**外围定调**：%s" % _global_signal(g))
        if regime.get("level"):
            L.append("**连板热度**：%s" % regime.get("note", ""))
        # ⓪ 今日研判（用户 2026-08-31：盘前要的是短期形势预期判断，
        #     不是把上一交易日复盘再发一遍——复盘在看板/收盘推送里都有）
        try:
            _judge, _ = _market_outlook(data)
        except Exception:
            _judge = ""
        if _judge:
            L.append("")
            L.append("**⓪ 今日研判（短期形势 → 预期）**")
            for _ln in _judge.split("\n"):
                L.append(_ln)
        L.append("")
        # ① 今日候选（按买入价值降序，_top_recs 已统一排序）
        pool = _top_recs(rec.get("core"), rec.get("relay"), rec.get("all"), MAX_RECS)
        gated = [x for x in pool if _dual_ok(x)]
        ungated = [x for x in pool if not _dual_ok(x)]
        _lp_map = {p.get("code"): p for p in (rec.get("ladder_plans") or [])}
        L.append("**① 今日候选（价值分降序 · 评分%.0f+晋级%.0f%%双认证）**"
                 % _push_gate())
        L.append("> 每票两行：第一行评分与标签，第二行明确动作与价格。竞价弱=回避。")
        L.append("")
        if gated:
            for i, it in enumerate(gated, 1):
                L.append(_rec_line(it, i, tag="✅续强"))
                L.append("  %s" % _rec_action_line(it, _lp_map))
            if ungated:
                names = "、".join("%s(%.0f分)" % (u.get("name", "?"),
                                                  u.get("worth_score", 0))
                                  for u in ungated[:6])
                L.append("- ⚠ 未认证观察（只看不动）：%s" % names)
            if len(rec.get("all") or []) > MAX_RECS:
                L.append("")
                L.append("> 共 %d 只候选，仅列前 %d 只；完整强弱判定见看板。" % (len(rec.get("all") or []), MAX_RECS))
        else:
            L.append("今日无通过双重认证的标的，建议观望。")
        # ② 🎯 连板机会计划（昨日收盘产出的次日竞价介入单）
        _lp_lines = _ladder_plan_lines(rec, n=6)
        if _lp_lines:
            L.append("")
            L.append("**② 🎯 连板机会计划（次日竞价介入口径）**")
            L.append("> 低开(< -0.1%)一律放弃；只在不低开时按买区接力，止损-8%铁律。")
            L.extend(_lp_lines)
        # ③ ⭐ 自选/持仓操作结论（P1/P4：自选进推荐体系、持仓给明确动作）
        _wr_lines = watchreco_lines(data, n=6)
        if _wr_lines:
            L.append("")
            L.append("**③ ⭐ 自选/持仓操作（跟着做）**")
            L.extend(_wr_lines)
        # ④ 📌 关注票操作说明 = 板块当日涨跌预判 × 个股动作（用户需求5）
        _wf_lines = watch_forecast_lines(data, n=8)
        if _wf_lines:
            L.append("")
            L.append("**④ 📌 关注票操作（板块当日预判 → 动作）**")
            L.append("> 预判口径：板块昨日涨停高度 + 接力方向 + 主力资金 + 情绪/外围，仅作当日节奏参考。")
            L.extend(_wf_lines)
        # ⑤ 盘前策略（聚合仓位/主线/接力/关注池/风险——研判在⓪已给，这里只留执行清单）
        pp = data.get("preopen_plan") or {}
        if pp.get("position"):
            L.append("")
            L.append("**⑤ 执行清单**")
            L.append("- 建议仓位：**%s**" % pp["position"])
            if pp.get("main_line"):
                L.append("- 主线预判：%s" % "、".join(pp["main_line"]))
            if pp.get("relay_dir"):
                L.append("- 接力方向：%s" % "、".join(pp["relay_dir"]))
            for _s in (pp.get("strategies") or [])[:4]:
                L.append("- %s" % _s)
            if pp.get("watch"):
                # 2026-08-29 竞价决策线（13 个月全样本回测：高开≥2%胜率70%+/低开≤-2%仅17~38%）
                _ws = []
                for w in pp["watch"][:8]:
                    _ar = (w.get("auction_rule") or "").replace("（", "(").replace("）", ")")
                    _ws.append("%s(%s%s)" % (
                        w["name"], w.get("reason", ""),
                        ("｜" + _ar) if _ar else ""))
                L.append("- 关注池：" + "、".join(_ws))
                L.append(basis_once("open_discipline", _OPEN_DISCIPLINE_FULL, _OPEN_DISCIPLINE_BRIEF))
            if pp.get("risks"):
                L.append("- 风险提醒：%s" % "；".join(pp["risks"]))
        # ⑥ ⚡ 短线/超短线盘前操作提示（追板回落/破位/停滞 当日离场或换强）
        _zs = (data.get("zones") or {}).get("items") or []
        _zst = [x for x in _zs if x.get("horizon") in ("短线", "超短线")]
        if _zst:
            _zst.sort(key=lambda x: (0 if x.get("zhuiban") else 1,
                                     0 if x.get("rotate") else 1,
                                     -(x.get("pnl_pct") or 0)))
            L.append("")
            L.append("**⑥ ⚡ 短线/超短线盘前操作（持仓/关注）**")
            L.append("> 追板回落/破位/停滞一律当日离场或换强；完整买卖区见看板。")
            for x in _zst[:8]:
                nm = x.get("name", "?")
                hz = x.get("horizon")
                zb = x.get("zhuiban")
                seg = "- **%s**[%s] 现%.2f %+.1f%%" % (nm, hz, x.get("close", 0), x.get("pct", 0))
                if zb:
                    seg += " 🚨追板回落(%s炸板收%.2f,较涨停-%s%%)→**离场**" % (
                        zb["date"], zb["close"], zb["fallback_pct"])
                elif x.get("rotate") in ("止损", "割肉"):
                    seg += " → **%s**：%s" % (x["rotate"], (x.get("rotate_reason") or "")[:28])
                elif x.get("rotate") == "更换":
                    seg += " → 离场换强"
                elif x.get("action") in ("破位卖出", "跌破警示"):
                    seg += " → 弱势观望"
                t = x.get("targets") or {}
                ts = t.get("短线") or t.get("超短线") or {}
                if ts:
                    seg += " ｜短目标%.2f(%d日)" % (ts["price"], ts["days"])
                if x.get("time_status"):
                    seg += " ｜%s" % x["time_status"]
                L.append(seg)
        if url:
            L.append("")
            L.append("完整看板：%s" % url)
        return {"title": "竞价前观察 %s" % date, "text": "\n".join(L)}

    # 收盘后复盘：统一走「简洁板式」（与 ServerChan 同源，一屏读完只给结果）
    return {"title": "A股盘后复盘 %s" % date,
            "text": _fmt_close_compact(data, url, mode, con=con)}


# ServerChan 免费档单条 desp 硬上限 8192 字，超长会被静默丢弃（导致收盘/复盘只到 PushPlus）。
# 因此给 ServerChan 单独生成「只给结果、紧凑有逻辑」的版本：去掉判断过程与冗长叙述，
# 仅保留盘面数据 + 牛股雷达 + 推荐 + 趋势 + 妖股 + 风险，天然压到 8K 以内。
# 这同时满足了"排版精美、逻辑强、只给结果"的诉求。
SC_CAP = 8000


def format_sc(data, url="", mode="close", con=None):
    """ServerChan 专用精简版（只给结果、强结构、< 8K）。"""
    m = data.get("meta", {})
    date = m.get("date", "")
    sent = data.get("market", {}).get("sentiment", {}) or {}
    cyc = data.get("market", {}).get("cycle", {}) or {}
    lus = data.get("limit_ups", []) or []
    rec = data.get("recommend", {}) or {}
    regime = data.get("regime", {}) or {}

    if mode == "preauction":
        L = []
        L.append("## 今日研判 · %s" % date)
        g = data.get("global_market") or {}
        L.append("外围：%s" % _global_signal(g))
        if regime.get("note"):
            L.append("连板：%s" % regime.get("note", ""))
        # ⓪ 今日研判（SC 精简：研判置顶，替代旧复盘复读）
        try:
            _judge, _ = _market_outlook(data)
        except Exception:
            _judge = ""
        if _judge:
            L.append("⓪研判：" + _judge.split("\n")[0])
        # ① 今日候选（SC 紧凑：每票一行 = 序号+名+分+动作尾巴）
        pool = _top_recs(rec.get("core"), rec.get("relay"), rec.get("all"), MAX_RECS)
        gated = [x for x in pool if _dual_ok(x)]
        ungated = [x for x in pool if not _dual_ok(x)]
        _lp_map = {p.get("code"): p for p in (rec.get("ladder_plans") or [])}
        L.append("①候选（价值分降序·双认证 评分%.0f+晋级%.0f%%）：" % _push_gate())
        if gated:
            for i, it in enumerate(gated, 1):
                code = it.get("code")
                p = _lp_map.get(code)
                if p:
                    bz = p.get("buy_zone") or [0, 0]
                    act = "买%.2f~%.2f 止损%.2f" % (bz[0], bz[1], p.get("stop") or 0)
                elif it.get("risk_flag") or (it.get("p_break") or 0) >= 78:
                    act = "⚠轻仓/冲高兑现"
                else:
                    act = "强势可跟·低开弃"
                L.append("- %d. **%s**(%s) %.0f分/%.0f%% %s" % (
                    i, it.get("name", "?"), _board(it),
                    it.get("worth_score", 0), it.get("p_continue", 0), act))
            if ungated:
                L.append("- ⚠未认证只看不动：" + "、".join(u.get("name", "?") for u in ungated[:8]))
        else:
            L.append("无双重认证标的，建议观望")
        # ② 🎯 连板机会计划（SC 紧凑版）
        _lp_lines = _ladder_plan_lines(rec, n=4, compact=True)
        if _lp_lines:
            L.append("②🎯连板计划（低开即弃·止损-8%）：")
            L.extend(_lp_lines)
        # ③ ⭐ 自选/持仓操作
        _wr_lines = watchreco_lines(data, n=5, compact=True)
        if _wr_lines:
            L.append("③⭐自选/持仓操作：")
            L.extend(_wr_lines)
        # ③b 📌 关注票 × 板块当日预判（用户需求5）
        _wf_lines = watch_forecast_lines(data, n=5)
        if _wf_lines:
            L.append("③b📌关注票·板块当日预判：")
            L.extend(_wf_lines)
        # ④ 执行清单（研判在⓪已给）
        pp = data.get("preopen_plan") or {}
        if pp.get("position"):
            L.append("④执行：仓位 %s ｜ 主线 %s ｜ 接力 %s"
                     % (pp["position"], "、".join(pp.get("main_line", []) or []),
                        "、".join(pp.get("relay_dir", []) or [])))
        # ⑤ ⚡ 短线/超短线盘前操作（追板回落/破位/停滞 当日离场或换强）
        _zs = (data.get("zones") or {}).get("items") or []
        _zst = [x for x in _zs if x.get("horizon") in ("短线", "超短线")]
        if _zst:
            _zst.sort(key=lambda x: (0 if x.get("zhuiban") else 1,
                                     0 if x.get("rotate") else 1))
            L.append("⑤⚡短线操作：")
            for x in _zst[:6]:
                zb = x.get("zhuiban")
                if zb:
                    L.append("- %s 追板回落(%s炸板收%.2f,-%s%%)→离场"
                             % (x.get("name"), zb["date"], zb["close"], zb["fallback_pct"]))
                elif x.get("rotate") in ("止损", "割肉", "更换"):
                    L.append("- %s %s" % (x.get("name"), x["rotate"]))
                else:
                    L.append("- %s %s" % (x.get("name"), x.get("action")))
        if url:
            L.append("看板：%s" % url)
        return {"title": "竞价前 %s" % date, "text": "\n".join(L)}

    # 收盘后复盘：与 PushPlus 共用同一简洁板式（天然远小于 8K 上限）
    _title = ("复盘补发 %s" % date) if mode == "close_again" else ("盘后复盘 %s" % date)
    return {"title": _title, "text": _fmt_close_compact(data, url, mode, con=con)}


# ── 通用「判断依据只说一次」去重 ──────────────────────────────────────────────
# 同源复用 push_ledger.jsonl 的按北京日判定（与异常冷却去重同一套账本、零新增状态）。
# 用法：同一 ledger_mode 当日首条推送给 full_text，后续推送给 brief_text。
# 已用于盘中异动(anomaly_basis)；现扩展到盘前(preauction)/竞价(auction)的竞价纪律说明，
# 避免同一段回测纪律在 08:50 盘前与 09:25 竞价两条推送里重复刷屏。
_OPEN_DISCIPLINE_FULL = (
    "- ⏰ 竞价总纪律（13个月全样本回测）：涨停票次日**高开≥2%才跟进**、**低开≤-2%直接放弃**"
    "——高开>5%胜率85.9%/+6.8%，低开<-2%仅26.5%/-3.0%；连板低开一律回避（收红率仅24%）。")
_OPEN_DISCIPLINE_BRIEF = (
    "- ⏰ 竞价纪律（见今日盘前首条推送，此处不重复展开）：高开≥2%跟进 / 低开≤-2%放弃 / 连板低开回避。")


def basis_once(ledger_mode, full_text, brief_text):
    """同一 ledger_mode 当日首条给 full_text，后续给 brief_text（按北京日去重）。

    依赖 state/push_ledger.jsonl（_ledger_path / _bj_now / _append_ledger，与异常冷却同源）。
    任何异常都退化为 full_text（不静默丢失说明，仅丧失去重收益）。"""
    try:
        logp = _ledger_path()
        today = _bj_now().strftime("%Y-%m-%d")
        if os.path.exists(logp):
            with open(logp, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        p = json.loads(line)
                    except Exception:
                        continue
                    if p.get("mode") == ledger_mode and str(p.get("ts", "")).startswith(today):
                        return brief_text
        _append_ledger({"ts": _bj_now().strftime("%Y-%m-%d %H:%M:%S"),
                        "mode": ledger_mode, "title": ledger_mode})
        return full_text
    except Exception:
        return full_text


def tag_winrate(con, days=90, min_n=10):
    """按推荐标签统计次日实测表现（2026-09-03 用户要求「要成功率」）：
    rec_picks.next_pct 非空即已兑现，按 tag 聚合胜率(收红占比)/均值/样本数。
    样本 < min_n 不出结论（小样本误导比不给信息更糟）。返回 {tag: {win_rate,n,avg_pct}}。
    """
    if con is None:
        return {}
    try:
        rows = con.execute(
            "SELECT tag, COUNT(*) n, AVG(next_pct) avg_pct, "
            "SUM(CASE WHEN next_pct > 0 THEN 1 ELSE 0 END) wins "
            "FROM rec_picks WHERE next_pct IS NOT NULL AND tag IS NOT NULL "
            "AND date >= date('now', ?) GROUP BY tag", ("-%d days" % int(days),)).fetchall()
        out = {}
        for tag, n, avg_pct, wins in rows:
            if not tag or (n or 0) < int(min_n):
                continue
            out[str(tag)] = {"n": int(n),
                             "win_rate": round(float(wins or 0) * 100.0 / int(n), 1),
                             "avg_pct": round(float(avg_pct or 0), 2)}
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["win_rate"]))
    except Exception:
        return {}


def _tag_winrate(con, days=90, min_n=10):
    return tag_winrate(con, days=days, min_n=min_n)


def fetch_open_snapshot(codes):
    """竞价确认后实时拉自选/持仓票的今开/昨收/现价（腾讯 qt.gtimg，9:25 集合竞价成交价）。
    返回 {code: {"open":x,"prev":x,"price":x,"open_pct":x}}；失败返回 {}（调用方降级）。"""
    if not codes:
        return {}
    try:
        import urllib.request, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        pref = [("-sh" if c[0] in "69" else "-sz") + c for c in codes if len(c) == 6]
        if not pref:
            return {}
        url = "https://qt.gtimg.cn/q=" + ",".join(x.replace("-", "=") for x in
                                                  [("s_" + p) for p in pref])
        # s_ 前缀精简行情：v_s_sh600396="1~华电辽能~600396~现价~涨跌~涨跌幅~成交量~成交额~总市值";
        # 精简格式无今开 → 改用完整行情（f[5]=今开）
        url = "https://qt.gtimg.cn/q=" + ",".join(
            ("sh" if c[0] in "69" else "sz") + c for c in codes if len(c) == 6)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15, context=ctx).read().decode("gbk", "ignore")
        out = {}
        for line in raw.strip().split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            val = line.split("=", 1)[1].strip().strip('"')
            f = val.split("~")
            if len(f) < 6 or not f[2]:
                continue
            code = f[2]
            try:
                price, prev, opx = float(f[3]), float(f[4]), float(f[5])
            except Exception:
                continue
            open_pct = round((opx / prev - 1) * 100, 2) if prev > 0 and opx > 0 else None
            out[code] = {"open": opx if opx > 0 else None, "prev": prev,
                         "price": price, "open_pct": open_pct}
        return out
    except Exception:
        return {}


def auction_action(code, name, is_holding, zone_it, snap):
    """单票竞价操作判定（2026-09-02 用户需求：竞价确认后给减半/加仓/买入/观望明确动作）。
    zone_it: zones item（买卖区/动作判定，可为 None）；snap: fetch_open_snapshot 条目（可为 None）。
    返回 (emoji, 动作短语, 依据) 或 None（无任何信息可判）。"""
    bz = (zone_it or {}).get("buy_zone") or [None, None]
    sz = (zone_it or {}).get("sell_zone") or [None, None]
    stop = (zone_it or {}).get("stop")
    act = (zone_it or {}).get("action") or ""
    rot = (zone_it or {}).get("rotate")
    opx = (snap or {}).get("open")
    open_pct = (snap or {}).get("open_pct")
    # ── 持仓票：卖出/减半/加仓/持有 ──
    if is_holding:
        if act == "破位卖出" or rot in ("止损", "割肉"):
            return "🟢", "止损离场", ((zone_it or {}).get("rotate_reason") or act)[:28]
        if open_pct is not None and open_pct <= -2:
            return "🟢", "低开%.1f%%·开盘走弱即减半" % open_pct, "竞价低开 T+1 收红率仅24%"
        if opx and sz[0] and opx >= sz[0]:
            return "🟢", "竞价进卖点区·分批止盈/减半", "开盘 %.2f ≥ 卖点 %.2f" % (opx, sz[0])
        if open_pct is not None and open_pct >= 2:
            return "🔴", "竞价强势确认·持有为主，回踩不破MA20可加仓", "高开 %.1f%%" % open_pct
        if open_pct is not None:
            return "⚪", "平开·按计划持有", ("破 %s 止损" % ("%.2f" % stop)) if stop else "盯卖点区"
        return "⚪", "持有·竞价未采集", ("破 %s 止损" % ("%.2f" % stop)) if stop else ""
    # ── 关注票：买入/观望 ──
    if act == "破位卖出" or rot in ("止损", "割肉"):
        return "🟢", "回避·趋势走坏", ((zone_it or {}).get("rotate_reason") or act)[:28]
    if opx and bz[0] and bz[1] and bz[0] <= opx <= float(bz[1]) * 1.05:
        seg = "可买入 %.2f~%.2f" % (bz[0], float(bz[1]))
        return "🔴", "竞价进买区✅ " + seg, ("破 %s 止损" % ("%.2f" % stop)) if stop else ""
    if opx and bz[1] and opx > float(bz[1]) * 1.05:
        return "⚪", "高开已过买点·观望不追（回落 %.2f~%.2f 再看）" % (bz[0], bz[1]), ""
    if opx and bz[0] and opx < bz[0]:
        return "⚪", "低开破买区·观望（企稳回买区 %.2f~%.2f 再看）" % (bz[0], bz[1]), ""
    if opx and open_pct is not None:
        return "⚪", "竞价开 %.1f%%·观望等方向" % open_pct, ""
    return None


def _today_action_lines(data, snap):
    """「⚡ 今日操作提示」行：持仓+自选逐票竞价动作。持仓在前，买入提示次之，观望殿后。"""
    try:
        import holdings as _hd
        positions = _hd.load_positions() or []
    except Exception:
        positions = []
    zmap = {x.get("code"): x for x in ((data.get("zones") or {}).get("items") or [])
            if x.get("code")}
    rows = []
    for p in positions:
        code = str(p.get("code") or "").strip()
        if not code or len(code) != 6:
            continue
        z = zmap.get(code)
        name = p.get("name") or (z or {}).get("name") or code
        is_holding = bool(p.get("cost")) or (z or {}).get("cost") is not None
        r = auction_action(code, name, is_holding, z, snap.get(code))
        if not r:
            continue
        emo, actv, why = r
        seg = "- %s **%s** → %s" % (emo, name, actv)
        if why:
            seg += "（%s）" % why
        rows.append((0 if is_holding else 1,
                     0 if actv.startswith(("止损", "低开", "竞价进卖点", "回避")) else
                     (1 if "可买" in actv or actv.startswith("竞价强势") else 2),
                     seg))
    rows.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in rows]


def format_auction_summary(data, url="", con=None):
    """竞价后确认（9:25）：结合前一交易日推荐名单，对比今日竞价强弱，判定续强/掉队/新晋（Markdown 分区）。"""
    m = data.get("meta", {})
    date = m.get("date", "")
    # ── ⚡ 今日操作提示（2026-09-02 用户需求：竞价确认后给持仓/自选逐票明确动作——
    #    减半/加仓/买入/止损/观望，不错过行情也不冒进）──
    _pos_codes = set()
    try:
        import holdings as _hd0
        _pos_codes = {str(p.get("code") or "").strip()
                      for p in (_hd0.load_positions() or []) if p.get("code")}
    except Exception:
        pass
    _snap = fetch_open_snapshot(_pos_codes) if _pos_codes else {}
    _act_rows = _today_action_lines(data, _snap)
    L0 = []
    if _act_rows:
        L0.append("## ⚡ 今日操作提示 · 竞价确认 %s" % time.strftime("%H:%M"))
        L0.append("")
        L0.append("> 持仓=止损/减半/加仓/持有；关注=买入/观望。低开-2%以下 T+1 收红率仅 24%，宁可错过不可深套。")
        L0.append("")
        L0.extend(_act_rows)
        L0.append("")
    # 昨日推荐 = 本次分析日系统给出的推荐标的（即上一交易日推荐名单），直接取自 data，
    # 避免依赖 rec_picks 历史表缺失导致对比为空。
    recommended = data.get("recommend", {}).get("all") or []
    prev = {it.get("code"): it for it in recommended}
    aitems = (data.get("auction") or {}).get("items") or {}
    recs = recommended
    g = data.get("global_market") or {}
    L = L0  # 操作提示置顶
    L.append("## A股竞价后确认 · %s" % date)
    L.append("")
    L.append("**对比对象**：分析日(%s)推荐名单 vs 今日竞价强弱" % date)
    L.append("**外围定调**：%s" % _global_signal(g))
    L.append("")

    strong, weak, newstrong, warn, lowopen = [], [], [], [], []
    for it in recs[:60]:
        code = it.get("code")
        aq = aitems.get(code) or {}
        pat = aq.get("pattern")
        va = aq.get("vol_anomaly") or {}
        is_prev = code in prev
        # ── 负反馈闭环 G1：竞价纪律（回测实证）──
        # 低开 → T+1 收红率仅 24%/-2.24%；不低开+龙头/缩量 → 78%/+5.50%
        try:
            import recveto as _rv
            _gate = _rv.auction_gate(aq.get("open_pct"), {"tag": it.get("tag"),
                                                          "vol_anomaly": va})
        except Exception:
            _gate = None
        open_pct_v = aq.get("open_pct")
        if _gate and _gate.get("action") == "avoid" and pat != "一字板":
            # 一字竞价通常无成交量可判，除一字外低开一律进回避段
            if open_pct_v is not None and open_pct_v < 0:
                it2 = dict(it)
                it2["gate_evidence"] = "竞价低开 %.1f%% · 历史 T+1 收红率仅 24%%/均值 -2.24%%" % open_pct_v
                warn.append((it2, aq))
                if is_prev:
                    weak.append((it2, aq))
                else:
                    lowopen.append((it2, aq))
                continue
        if va.get("warn") or pat == "强转弱":
            warn.append((it, aq))
            if is_prev:
                weak.append((it, aq))
            continue
        if is_prev and pat not in ("强转弱",):
            tag = "续强确认" if (aq.get("yizi") or pat in ("一字板", "弱转强", "高开高走", "换手板")) else "续强观察"
            strong.append((it, aq, tag))
        elif (not is_prev) and pat and pat not in ("强转弱",) and (aq.get("yizi") or pat in ("一字板", "弱转强", "高开高走")):
            newstrong.append((it, aq))

    # 🎯 连板计划 · 竞价复核：昨日生成的买卖区 vs 今日实际竞价
    _lp = {p.get("code"): p for p in ((data.get("recommend") or {}).get("ladder_plans") or [])}
    if _lp:
        lp_hit, lp_avoid = [], []
        for _c, _p in _lp.items():
            _aq = aitems.get(_c) or {}
            _opv = _aq.get("open_pct")
            if _opv is None:
                continue
            if _opv < -0.1 or (_p.get("gate") == "avoid"):
                lp_avoid.append("%s(开%.1f%%)" % (_p.get("name", "?"), _opv))
            else:
                bz = _p.get("buy_zone") or [0, 0]
                opx = _aq.get("open") or 0
                inz = bool(opx) and bz[0] <= opx <= bz[1]
                lp_hit.append("%s(%d→%s 开%.1f%% %s) 卖%.2f~%.2f 止损%.2f" % (
                    _p.get("name", "?"), _p.get("entry_streak", 0),
                    (_p.get("expected_top") or "").split("→")[-1], _opv,
                    "达标买区✅" if inz else "买区外观望",
                    ((_p.get("sell_zone") or [0, 0])[0]),
                    ((_p.get("sell_zone") or [0, 0])[1]), _p.get("stop", 0)))
        if lp_hit or lp_avoid:
            L.append("")
            L.append("### 🎯 连板计划竞价复核")
            for x in lp_hit[:6]:
                L.append("- **%s**" % x)
            if lp_avoid:
                L.append("- 🚫 低开放弃：%s" % "、".join(lp_avoid[:8]))

    L.append(basis_once("open_discipline", _OPEN_DISCIPLINE_FULL, _OPEN_DISCIPLINE_BRIEF))
    L.append("")

    L.append("### 续强确认（昨日推荐 · 今日竞价强）")
    if strong:
        for i, (it, aq, tag) in enumerate(strong[:MAX_RECS], 1):
            va = aq.get("vol_anomaly") or {}
            L.append("%d. [%s] **%s**(%s) 竞价=%s 量能=%s 晋级**%.0f%%**"
                     % (i, tag, it.get("name"), _board(it),
                        aq.get("pattern", "-"), va.get("flag", "正常"), it.get("p_continue", 0)))
            rs = it.get("reasons") or []
            if rs:
                L.append("   简因：%s" % "、".join(rs[:1]))
    else:
        L.append("（暂无续强标的）")
    L.append("")
    L.append("### 掉队 / 转弱（昨日推荐 · 今日竞价弱或派发）")
    if weak:
        for it, aq in weak[:6]:
            va = aq.get("vol_anomaly") or {}
            extra = (" 量能%s(%.1f倍)" % (va.get("flag"), va.get("ratio", 1))) if va.get("flag") not in (None, "正常") else ""
            L.append("- **%s**(%s) 竞价=%s%s → 回避" % (it.get("name"), _board(it), aq.get("pattern", "-"), extra))
    else:
        L.append("（昨日推荐今日无掉队，整体强势延续）")
    L.append("")
    L.append("### 新晋强势（今日竞价强 · 昨日未推荐）")
    if newstrong:
        for i, (it, aq) in enumerate(newstrong[:MAX_RECS], 1):
            va = aq.get("vol_anomaly") or {}
            L.append("%d. **%s**(%s) 竞价=%s 量能=%s 晋级**%.0f%%**"
                     % (i, it.get("name"), _board(it),
                        aq.get("pattern", "-"), va.get("flag", "正常"), it.get("p_continue", 0)))
    else:
        L.append("（今日无新晋强势标的）")
    if len(strong) + len(newstrong) > MAX_RECS:
        L.append("")
        L.append("> 强势标的较多，仅列前 %d 只；完整判定见看板。" % MAX_RECS)
    L.append("")
    if lowopen:
        L.append("### 🚫 今日低开回避（新面孔低开 · 历史收红率 24%）")
        for it, aq in lowopen[:8]:
            L.append("- **%s**(%s) %s" % (it.get("name"), _board(it),
                                          it.get("gate_evidence", "竞价低开")))
        L.append("")
    if warn:
        L.append("### 竞价异动预警（爆量派发 / 强转弱）")
        for it, aq in warn[:6]:
            va = aq.get("vol_anomaly") or {}
            extra = (" 量能%s(%.1f倍)" % (va.get("flag"), va.get("ratio", 1))) if va.get("flag") not in (None, "正常") else ""
            action = " → 疑似派发，回避" if va.get("warn") else " → 谨慎"
            L.append("- **%s**(%s) 竞价%s%s%s" % (it.get("name"), _board(it), aq.get("pattern", "-"), extra, action))
        L.append("")
    if not (strong or weak or newstrong or warn or lowopen):
        L.append("竞价整体平稳，暂无明显续强/掉队/异动信号。")
        L.append("")
    if url:
        L.append("---")
        L.append("完整数据看板：%s" % url)
    return {"title": "A股竞价后确认 %s" % date, "text": "\n".join(L)}


def format_anomaly_summary(data, url="", con=None):
    """盘中异动提醒：竞价量能异动(疑似派发) + 高位断板风险，可随时触发（Markdown 分区）。"""
    m = data.get("meta", {})
    date = m.get("date", "")
    aitems = (data.get("auction") or {}).get("items") or {}
    risks = data.get("break_risk") or []
    L = []
    L.append("## A股盘中异动提醒 · %s" % date)
    L.append("")
    L.append("### 竞价量能异动 / 疑似派发")
    anom = []
    for code, aq in aitems.items():
        va = aq.get("vol_anomaly") or {}
        if va.get("warn"):
            anom.append("%s(%s) 竞价爆量%.1f倍+%s，疑似派发，次日回落风险高"
                        % (aq.get("name"), _board(aq), va.get("ratio", 1), aq.get("pattern", "—")))
        elif aq.get("pattern") == "强转弱":
            anom.append("%s(%s) 竞价强转弱，诱多分歧明显" % (aq.get("name"), _board(aq)))
    if anom:
        for x in anom[:8]:
            L.append("- %s" % x)
    else:
        L.append("（竞价量能整体平稳，未见明显派发信号）")
    L.append("")
    L.append("### 高位断板风险")
    hi = [r for r in risks if (r.get("p_break") or 0) >= 78]
    if hi:
        for r in hi[:5]:
            L.append("- **%s**(%s) 模型测算断板概率%.0f%%" % (r.get("name"), _board(r), r.get("p_break", 0)))
    else:
        L.append("（高位断板概率整体可控）")
    L.append("")
    if url:
        L.append("---")
        L.append("完整数据看板：%s" % url)
    return {"title": "A股盘中异动提醒 %s" % date, "text": "\n".join(L)}


def format_open_anomaly_summary(data, url="", con=None):
    """竞价后开盘前异动（9:26，开盘前最后提醒）：聚焦竞价异动标的——
    一字板/弱转强（开盘强势信号）+ 爆量派发/强转弱（回避信号）+ 高位断板风险，
    给开盘前 3 分钟的最终操作清单（Markdown 分区）。"""
    m = data.get("meta", {})
    date = m.get("date", "")
    aitems = (data.get("auction") or {}).get("items") or {}
    risks = data.get("break_risk") or []
    L = []
    L.append("## 🔗 竞价后开盘前异动 · %s" % date)
    L.append("")
    L.append("> 开盘前最后 3 分钟异动清单：竞价强势=开盘可关注，竞价派发/强转弱=回避。")
    L.append("")

    # 开盘强势（一字板 / 弱转强 / 高开高走）
    strong = []
    for code, aq in aitems.items():
        pat = aq.get("pattern")
        if aq.get("yizi") or pat in ("一字板", "弱转强", "高开高走", "换手板"):
            strong.append((aq, pat))
    L.append("### 🔥 开盘强势（竞价一字/弱转强，可关注）")
    if strong:
        for aq, pat in strong[:8]:
            va = aq.get("vol_anomaly") or {}
            ratio = (" 量能%s(%.1f倍)" % (va.get("flag"), va.get("ratio", 1))) if va.get("flag") not in (None, "正常") else ""
            L.append("- **%s**(%s) 竞价=%s%s" % (aq.get("name"), _board(aq), pat, ratio))
    else:
        L.append("（竞价强势标的较少）")
    L.append("")

    # 竞价异动预警（爆量派发 / 强转弱）→ 回避
    warn = []
    for code, aq in aitems.items():
        va = aq.get("vol_anomaly") or {}
        if va.get("warn") or aq.get("pattern") == "强转弱":
            warn.append((aq, va))
    L.append("### ⚠ 竞价异动预警（爆量派发 / 强转弱，回避）")
    if warn:
        for aq, va in warn[:8]:
            extra = (" 量能%s(%.1f倍)" % (va.get("flag"), va.get("ratio", 1))) if va.get("flag") not in (None, "正常") else ""
            action = " → 疑似派发，回避" if va.get("warn") else " → 谨慎"
            L.append("- **%s**(%s) 竞价%s%s%s" % (aq.get("name"), _board(aq), aq.get("pattern", "-"), extra, action))
    else:
        L.append("（竞价量能整体平稳，未见明显派发信号）")
    L.append("")

    # 高位断板风险（开盘冲高回落预警）
    hi = [r for r in risks if (r.get("p_break") or 0) >= 78]
    L.append("### 📉 高位断板风险")
    if hi:
        for r in hi[:5]:
            L.append("- **%s**(%s) 模型测算断板概率%.0f%%" % (r.get("name"), _board(r), r.get("p_break", 0)))
    else:
        L.append("（高位断板概率整体可控）")
    L.append("")

    # 🚨 追板回落·开盘前离场（追板资金被套，短线当日走）
    _zs = (data.get("zones") or {}).get("items") or []
    _zb = [x for x in _zs if x.get("zhuiban") and x.get("horizon") in ("短线", "超短线")]
    if _zb:
        L.append("### 🚨 追板回落·开盘前离场")
        for x in _zb[:5]:
            zb = x["zhuiban"]
            L.append("- **%s**(%s) %s炸板收%.2f(较涨停-%s%%) → 开盘宜离场"
                     % (x.get("name"), x.get("code"), zb["date"], zb["close"], zb["fallback_pct"]))
        L.append("")

    if url:
        L.append("---")
        L.append("完整数据看板：%s" % url)
    return {"title": "竞价后开盘前异动 %s" % date, "text": "\n".join(L)}


def format_panic_summary(data, url="", con=None):
    """盘中/盘后恐慌专送（PushPlus）：聚焦崩盘信号——跌停潮、大面榜(天地板/墓碑线)、
    亏钱效应、炸板率、广度。"""
    p = data.get("panic") or {}
    m = data.get("meta", {})
    date = m.get("date", "")
    if not p:
        return {"title": "盘面恐慌扫描 %s" % date, "text": "（今日无恐慌分析数据）"}
    L = []
    L.append("## ⚠️ 盘面恐慌 / 崩盘扫描 · %s" % date)
    L.append("")
    L.append("**综合等级**：%s（评分 %d）" % (p.get("level"), p.get("score", 0)))
    L.append("> %s" % p.get("hint", ""))
    L.append("")
    L.append("**核心指标**：跌停 %d 家（基线 %.0f，z=%.1f）｜ 昨日涨停收绿 %.0f%% ｜ 炸板率 %.0f%% ｜ 涨跌比下跌 %.0f%%"
             % (p.get("dt_count", 0), p.get("dt_base", 0), p.get("dt_z", 0),
                p.get("yest_green") or 0, p.get("zb_rate", 0), p.get("down_ratio", 0)))
    bf = p.get("bigface") or []
    if bf:
        L.append("")
        L.append("### 🔻 大面榜（冲高回落 / 天地板 / 墓碑线）Top%d" % min(8, len(bf)))
        for x in bf[:8]:
            L.append("- **%s** 收 %.2f%%（开盘 %.2f%%／最高 %.2f%%）· %s · 较高点回落 %.1f%%"
                     % (x.get("name"), x.get("pct", 0), x.get("open_pct", 0),
                        x.get("high_pct", 0), x.get("kind"), x.get("drop_from_high", 0)))
    if url:
        L.append("")
        L.append("完整数据看板：%s" % url)
    return {"title": "⚠️ 盘面恐慌扫描 %s" % date, "text": "\n".join(L)}


def format_weekend_summary(data, url="", news_items=None):
    """周末发酵 / 周一前瞻（条件推送专用）：仅在有周末要闻时由 push_weekend 调用。

    2026-08-31 重构（用户要求：不要把周五的复盘再推一次）——
    主体改为「周一前瞻研判」（基于短期形势的预期判断 + 周末要闻催化），
    周五盘面数据不再复读（看板/周五收盘推送里都有）。"""
    news_items = news_items or []
    m = data.get("meta", {})
    date = m.get("date", "")
    L = []
    L.append("## 周一前瞻 · 周末要闻催化（周五数据 %s）" % date)
    L.append("")
    # 周一研判：外围发酵 → 周一预期（盘前研判引擎复用）
    try:
        judge, meta = _market_outlook(data)
    except Exception:
        judge, meta = "", {}
    if judge:
        L.append("### 周一预期研判")
        for ln in judge.split("\n"):
            L.append(ln)
        L.append("")
    if news_items:
        L.append("### 周末要闻（对周一的催化方向，非复盘）")
        for it in news_items[:10]:
            t = it.get("title") or it.get("content") or it.get("summary") or ""
            if t:
                L.append("- %s" % t)
        L.append("")
        L.append("> 要闻只列催化线索：利好主线→竞价高开则跟，利空→低开不接飞刀，")
        L.append("> 具体操作仍以周一 08:50 盘前研判与 09:25 竞价确认为准。")
    if url:
        L.append("")
        L.append("---")
        L.append("完整数据看板：%s" % url)
    # SC 精简版（ServerChan 8K 上限；研判 1 行 + 要闻前 5 条）
    sc = []
    sc.append("## 周一前瞻 %s" % date)
    if judge:
        sc.append("研判：" + judge.split("\n")[0])
    for it in news_items[:5]:
        t = it.get("title") or it.get("content") or it.get("summary") or ""
        if t:
            sc.append("- %s" % t[:60])
    if url:
        sc.append("看板：%s" % url)
    return {"title": "周一前瞻 · 周末要闻 %s" % date, "text": "\n".join(L),
            "sc_text": "\n".join(sc)}


def format_weekattr_summary(data, con=None, url=""):
    """每周胜率归因推送（#4，2026-09-03）：基于 rec_picks 全量实证做多维归因，
    给出「哪些信号类型真正赚钱、哪些该收手」，附近90日标签级胜率。
    纯本地、零网络依赖（区别于周末前瞻的「需有要闻才发」）；周日定时推送。"""
    m = (data or {}).get("meta", {}) or {}
    date = m.get("date", "")
    L = []
    L.append("## 📊 每周胜率归因 · 信号实测（截至 %s）" % date)
    L.append("")
    L.append("> 数据源：rec_picks 全量实证（推荐次日真实涨跌幅）。样本<8 不统计，避免小样本误导。")
    L.append("")
    if con is None:
        try:
            from pipeline import store
            con = store.connect()
        except Exception:
            con = None
    if con is not None:
        # 1) 标签级胜率（近90日）
        tw = tag_winrate(con, days=90, min_n=8)
        if tw:
            L.append("### 一、信号类型次日收红率（近90日）")
            for k, v in tw.items():
                _flag = " ⚠️**低于45%该收手**" if v["win_rate"] < 45 else ""
                L.append("- **%s**：胜率 **%.0f%%**（n=%d，均值%+.2f%%）%s"
                         % (k, v["win_rate"], v["n"], v["avg_pct"], _flag))
            L.append("")
        else:
            L.append("> 近90日暂无足够样本，归因暂不可见。")
            L.append("")
        # 2) 多维归因（recattr：st=2 开盘溢价分桶 / 特征 / 盘中路径）
        try:
            from pipeline import recattr
            _ra = recattr.build(con)
            if _ra:
                for ln in recattr.summary_lines(_ra):
                    if ln:
                        L.append(ln)
                L.append("")
        except Exception as e:
            L.append("> 多维归因生成失败：%r" % e)
            L.append("")
    else:
        L.append("> 数据库未就绪，跳过归因。")
        L.append("")
    if url:
        L.append("---")
        L.append("完整看板（含逐标签历史曲线）：%s" % url)
    # ServerChan 精简版（8K 上限）
    sc = ["## 每周胜率归因 %s" % date]
    if con is not None:
        tw = tag_winrate(con, days=90, min_n=8)
        for k, v in list(tw.items())[:10]:
            sc.append("- %s %.0f%%(n=%d)" % (k, v["win_rate"], v["n"]))
    if url:
        sc.append("看板：%s" % url)
    return {"title": "每周胜率归因 %s" % date, "text": "\n".join(L),
            "sc_text": "\n".join(sc)}


if __name__ == "__main__":
    # 测试：python notifier.py [--send] [--preauction|--auction|--anomaly|--close_again|--weekend]
    #   不加 --send 只打印不发送（安全默认）；加了 --send 才真正推送
    mode = "close"
    for kw in ("preauction", "auction", "anomaly", "close_again", "weekend"):
        if ("--" + kw) in sys.argv:
            mode = kw
    dry_run = ("--send" not in sys.argv)
    push({"title": "测试", "text": "这是一条测试推送"}, dry_run=dry_run, mode=mode)
