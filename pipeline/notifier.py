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

ROOT = store.ROOT
CFG_PATH = os.path.join(ROOT, "config", "notify.json")
DIST = os.path.join(ROOT, "dist")


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
    """
    env_cfg = _env_config()
    if env_cfg:
        return env_cfg
    if os.path.exists(CFG_PATH):
        try:
            return json.load(open(CFG_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}


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


def send_wechat_serverchan(cfg, title, text):
    keys = _iter_sendkeys(cfg)
    if not keys:
        return False, "未配置 sendkey"
    ok_list, fail_list = [], []
    for label, key in keys:
        try:
            url = "https://sctapi.ftqq.com/%s.send" % key
            resp = http_post_form(url, {"title": title, "desp": text})
            try:
                j = json.loads(resp)
                if j.get("code") == 0:
                    ok_list.append(label)
                else:
                    fail_list.append("%s:%s" % (label, str(j.get("message", resp))[:40]))
            except Exception:
                ok_list.append("%s(响应未解析)" % label)
        except Exception as e:
            fail_list.append("%s:%r" % (label, e))
    msg = "ServerChan 成功 %d/%d（%s）" % (len(ok_list), len(keys), "、".join(ok_list) or "无")
    if fail_list:
        msg += " 失败：" + "；".join(fail_list)
    return (len(ok_list) > 0), msg


def send_wechat_pushplus(cfg, title, text):
    tokens = _iter_pushplus(cfg)
    if not tokens:
        return False, "未配置 token"
    url = "http://www.pushplus.plus/send"
    ok_list, fail_list = [], []
    for token in tokens:
        try:
            payload = {"token": token, "title": title, "content": text}
            # topic 为群组编码，缺省走一对一推送；配置了才带上
            topic = cfg.get("topic")
            if topic:
                payload["topic"] = topic
            resp = http_post_json(url, payload)
            try:
                import json as _json
                j = _json.loads(resp)
                if j.get("code") == 200:
                    ok_list.append(token[:6] + "…")
                else:
                    fail_list.append("%s:%s" % (token[:6], str(j.get("msg", resp))[:40]))
            except Exception:
                ok_list.append(token[:6] + "…(响应未解析)")
        except Exception as e:
            fail_list.append("%s:%r" % (token[:6], e))
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
_ONCE_PER_DAY = {"preauction", "auction", "close", "weekend"}


def _already_pushed_today(mode):
    """同一 mode 当天是否已推送过（读 push_log.jsonl 判定）。"""
    if mode not in _ONCE_PER_DAY:
        return False
    try:
        logp = os.path.join(DIST, "push_log.jsonl")
        if not os.path.exists(logp):
            return False
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        with open(logp, encoding="utf-8") as fh:
            for line in fh:
                try:
                    p = json.loads(line)
                    if p.get("mode") == mode and str(p.get("ts", "")).startswith(today):
                        return True
                except Exception:
                    pass
        return False
    except Exception:
        return False


def push(summary, dry_run=False, mode="close"):
    """summary: {"title": str, "text": str}。返回已送达通道列表。
    mode: "close"（收盘后复盘）或 "preauction"（竞价前观察）。
    无论是否配置通道，都会把推送内容落地为可见文件，避免『啥都看不到』。"""
    # 幂等去重：同一 mode 当天已推送过则跳过通道发送，避免多路触发重复轰炸
    # （GitHub 自带 schedule 常被丢弃，故叠加了看门狗/备份订阅/外部定时器多重触发，
    #  这里统一兜底：先到先发，后到静默）。
    if not dry_run and _already_pushed_today(mode):
        print("[notifier][%s] 今日已推送，跳过通道发送（防重复触发）" % mode)
        return ["skipped:dup"]
    # 输出兜底：避免 print 带 emoji 在非 UTF-8 控制台（如 GBK）抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = load_config()
    title = summary.get("title", "A股盘后复盘")
    text = summary.get("text", "")
    results = []
    if dry_run:
        print("[notifier][dry-run][%s] title=%s\n%s" % (mode, title, text))
        return results

    # 1) 始终落地可见文件痕迹
    try:
        os.makedirs(DIST, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        art = os.path.join(DIST, "last_push_%s.md" % mode)
        with open(art, "w", encoding="utf-8") as fh:
            fh.write("# %s\n\n> 生成时间：%s\n\n```\n%s\n```\n" % (title, ts, text))
        logp = os.path.join(DIST, "push_log.jsonl")
        with open(logp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": ts, "mode": mode, "title": title,
                                 "text": text}, ensure_ascii=False) + "\n")
        results.append("file:%s" % os.path.relpath(art, ROOT))
    except Exception as e:
        print("[notifier] 文件痕迹写入失败：%r" % e)

    # 2) 通道推送（按 mode 路由：关键节点走 ServerChan，异动走 PushPlus 省额度）
    # 关键节点（close/preauction/auction）：ServerChan 为主，其他已配置通道兜底
    # 异动（anomaly）：走 PushPlus（200 条/天，几乎不限），不占 ServerChan 的 5 条额度
    _all = [
        ("wechat_serverchan", send_wechat_serverchan, cfg.get("wechat_serverchan")),
        ("wechat_pushplus", send_wechat_pushplus, cfg.get("wechat_pushplus")),
        ("wecom", send_wecom, cfg.get("wecom")),
        ("telegram", send_telegram, cfg.get("telegram")),
        ("email", send_email, cfg.get("email")),
    ]
    if mode == "anomaly":
        # 盘中异动只走 PushPlus（200 条/天，几乎不限），不占 ServerChan 的 5 条/天免费额度。
        # 「盘中收不到」的根因是 GitHub 定时会跳过盘中 cron，已用 watchdog.yml 兜底触发解决，
        # 而非改通道——否则 5 条/天的 ServerChan 额度会被盘中 5 次推送耗尽，反而挤掉盘前/收盘。
        _prefer = ["wechat_pushplus", "wecom", "telegram", "email"]
    else:
        _prefer = ["wechat_serverchan", "wechat_pushplus", "wecom", "telegram", "email"]
    dispatchers = [(n, fn, c) for (n, fn, c) in _all if n in _prefer and c]
    for name, fn, c in dispatchers:
        if not c:
            continue
        try:
            ok, msg = fn(c, title, text)
            results.append("%s:%s" % (name, msg))
        except Exception as e:
            results.append("%s:失败 %r" % (name, e))
    if not any(r.startswith(("wechat", "wecom", "telegram", "email")) for r in results):
        print("[notifier] 未配置任何推送通道，仅落地文件痕迹（可在 config/notify.json 配置微信/Telegram/邮件）")
    else:
        for r in results:
            if not r.startswith("file:"):
                print("[notifier] %s" % r)
    return results


def last_close_text():
    """返回 push_log.jsonl 中最近一次 mode=='close' 推送的正文；无则返回 None。
    用于『收盘补发』与『收盘后』推送去重：内容相同时跳过，节省 ServerChan 额度。"""
    try:
        logp = os.path.join(DIST, "push_log.jsonl")
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
                if p.get("mode") == "close":
                    last = p.get("text")
        return last
    except Exception:
        return None


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


def _rec_line(it, idx, tag=""):
    """单只推荐（markdown，单行克制：序号 名称(板) · 买入价值 · 晋级率 · 一条简因）。"""
    mark = ("[%s] " % tag) if tag else ""
    head = "%d. %s**%s**(%s) · 买入价值**%.0f分** · 晋级**%.0f%%**" % (
        idx, mark, it.get("name", "?"), _board(it),
        it.get("worth_score", 0), it.get("p_continue", 0))
    rs = it.get("reasons") or []
    extra = (" ｜ %s" % "、".join(rs[:1])) if rs else ""
    return head + extra


def _top_recs(core, relay, allit, n):
    """推荐去重合并：优先 core→relay，不足 n 则用全量按分数补齐，确保展示前 n 只最佳标的。"""
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
    return out[:n]


def _global_signal(g):
    if g.get("available"):
        return "%s — %s" % (g.get("signal"), g.get("detail", "外围数据缺失，按中性处理"))
    return "数据缺失，按中性处理"


def _pct(v):
    return ("%.0f%%" % v) if v is not None else "—"


def _board(it):
    s = it.get("streak", 0) or 0
    return ("%d板" % s) if s else "首板"


def format_stock_summary(data, url="", mode="close"):
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
        L.append("## 竞价前观察 · %s" % date)
        L.append("")
        L.append("**外围定调**：%s" % _global_signal(g))
        if regime.get("level"):
            L.append("**连板热度**：%s" % regime.get("note", ""))
        L.append("")
        pool = _top_recs(rec.get("core"), rec.get("relay"), rec.get("all"), MAX_RECS)
        L.append("**昨日推荐 · 今日竞价强弱（Top%d）**" % MAX_RECS)
        L.append("> 昨日入选标的，今日竞价定强弱：竞价强=续强确认，竞价弱/爆量派发=回避。")
        L.append("")
        if pool:
            for i, it in enumerate(pool, 1):
                L.append(_rec_line(it, i, tag="续强"))
            if len(rec.get("all") or []) > MAX_RECS:
                L.append("")
                L.append("> 共 %d 只候选，仅列前 %d 只；完整强弱判定见看板。" % (len(rec.get("all") or []), MAX_RECS))
        else:
            L.append("（暂无昨日推荐标的）")
        if url:
            L.append("")
            L.append("完整看板：%s" % url)
        return {"title": "竞价前观察 %s" % date, "text": "\n".join(L)}

    # 收盘后复盘（Markdown 分区）
    L = []
    L.append("## A股盘后复盘 · %s" % date)
    L.append("")
    L.append("**情绪温度计**：%.1f（%s）｜ **周期**：%s" % (sent.get("score", 0), sent.get("label", ""), cyc.get("phase", "")))
    L.append("**涨停**：%d 只 ｜ **最高**：%d 连板 ｜ **晋级率**：%s ｜ **封板率**：%s"
             % (len(lus), max([r.get("streak", 0) for r in lus], default=0),
                _pct(sent.get("promote_rate")), _pct(sent.get("seal_rate"))))
    if regime.get("level"):
        L.append("**连板热度研判**：%s" % regime.get("note", ""))
    L.append("")
    if narr.get("bullets"):
        L.append("### 复盘要点")
        for b in narr["bullets"][:4]:
            L.append("- %s" % b)
        L.append("")
    core = rec.get("core") or []
    relay = rec.get("relay") or []
    avoid = rec.get("avoid") or []
    allit = rec.get("all") or []
    if not avoid and allit:
        avoid = sorted(allit, key=lambda x: -(x.get("p_break") or 0))[:3]
    L.append("### 个股推荐 Top%d" % MAX_RECS)
    recs = _top_recs(core, relay, allit, MAX_RECS)
    if recs:
        for i, it in enumerate(recs, 1):
            L.append(_rec_line(it, i))
    else:
        L.append("（今日无明确推荐标的，建议控仓或低位试错）")
    if len(allit) > MAX_RECS:
        L.append("")
        L.append("> 共 %d 只候选，仅列前 %d 只；完整推荐与原因见看板。" % (len(allit), MAX_RECS))
    L.append("")
    if avoid:
        L.append("### 高位风险回避")
        for it in avoid[:3]:
            rk = "；".join(it.get("risks") or []) or "高位断板风险"
            L.append("- **%s**(%d板) 断板概率%.0f%%：%s" % (it.get("name"), it.get("streak", 0), it.get("p_break", 0), rk))
        L.append("")
    # 趋势向上（主升段趋势票，独立板块）
    trend = rec.get("trend") or []
    if trend:
        L.append("### 趋势向上 · 主升候选 Top%d" % min(MAX_RECS, len(trend)))
        for it in trend[:MAX_RECS]:
            tm = it.get("trend_meta") or {}
            L.append("- **%s**(%s) 收%.2f ｜ 多头排列 MA5/10/20=%.2f/%.2f/%.2f ｜ 近5日%d涨 ｜ 偏离MA20 +%.1f%% ｜ 量能%.1f倍"
                     % (it.get("name"), it.get("industry", "—"), it.get("close", 0),
                        tm.get("ma5", 0), tm.get("ma10", 0), tm.get("ma20", 0),
                        tm.get("up_days", 0), tm.get("momentum_pct", 0), tm.get("vol_ratio", 0)))
            rs = it.get("reasons") or []
            if rs:
                L.append("   简因：%s" % "、".join(rs[:2]))
        L.append("")
    if url:
        L.append("---")
        L.append("完整数据看板：%s" % url)
    return {"title": "A股盘后复盘 %s" % date, "text": "\n".join(L)}


def format_auction_summary(data, url="", con=None):
    """竞价后确认（9:25）：结合前一交易日推荐名单，对比今日竞价强弱，判定续强/掉队/新晋（Markdown 分区）。"""
    m = data.get("meta", {})
    date = m.get("date", "")
    # 昨日推荐 = 本次分析日系统给出的推荐标的（即上一交易日推荐名单），直接取自 data，
    # 避免依赖 rec_picks 历史表缺失导致对比为空。
    recommended = data.get("recommend", {}).get("all") or []
    prev = {it.get("code"): it for it in recommended}
    aitems = (data.get("auction") or {}).get("items") or {}
    recs = recommended
    g = data.get("global_market") or {}
    L = []
    L.append("## A股竞价后确认 · %s" % date)
    L.append("")
    L.append("**对比对象**：分析日(%s)推荐名单 vs 今日竞价强弱" % date)
    L.append("**外围定调**：%s" % _global_signal(g))
    L.append("")

    strong, weak, newstrong, warn = [], [], [], []
    for it in recs[:60]:
        code = it.get("code")
        aq = aitems.get(code) or {}
        pat = aq.get("pattern")
        va = aq.get("vol_anomaly") or {}
        is_prev = code in prev
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
    if warn:
        L.append("### 竞价异动预警（爆量派发 / 强转弱）")
        for it, aq in warn[:6]:
            va = aq.get("vol_anomaly") or {}
            extra = (" 量能%s(%.1f倍)" % (va.get("flag"), va.get("ratio", 1))) if va.get("flag") not in (None, "正常") else ""
            action = " → 疑似派发，回避" if va.get("warn") else " → 谨慎"
            L.append("- **%s**(%s) 竞价%s%s%s" % (it.get("name"), _board(it), aq.get("pattern", "-"), extra, action))
        L.append("")
    if not (strong or weak or newstrong or warn):
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


def format_weekend_summary(data, url="", news_items=None):
    """周末发酵 / 周一前瞻（条件推送专用）：仅在有周末要闻时由 push_weekend 调用。"""
    news_items = news_items or []
    m = data.get("meta", {})
    date = m.get("date", "")
    narr = data.get("narrative", {}) or {}
    L = []
    L.append("## 周末发酵 · 周一前瞻（%s）" % date)
    L.append("")
    L.append("### 周末要闻（发酵信息）")
    for it in news_items[:12]:
        t = it.get("title") or it.get("content") or it.get("summary") or ""
        if t:
            L.append("- %s" % t)
    L.append("")
    if narr.get("outlook"):
        L.append("### 周一策略关注")
        L.append(narr["outlook"])
        L.append("")
    if url:
        L.append("---")
        L.append("完整数据看板：%s" % url)
    return {"title": "周末发酵 · 周一前瞻 %s" % date, "text": "\n".join(L)}


if __name__ == "__main__":
    # 测试：python notifier.py [--send] [--preauction|--auction|--anomaly]
    #   不加 --send 只打印不发送（安全默认）；加了 --send 才真正推送
    mode = "close"
    for kw in ("preauction", "auction", "anomaly"):
        if ("--" + kw) in sys.argv:
            mode = kw
    dry_run = ("--send" not in sys.argv)
    push({"title": "测试", "text": "这是一条测试推送"}, dry_run=dry_run, mode=mode)
