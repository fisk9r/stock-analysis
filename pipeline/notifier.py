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
        # 免费档接口偶发 5xx/网络抖动会静默吞掉整条推送；加 2 次重试 + 退避，避免『该到的没到』。
        _done = False
        for _attempt in range(2):
            try:
                url = "https://sctapi.ftqq.com/%s.send" % key
                resp = http_post_form(url, {"title": title, "desp": text})
                try:
                    j = json.loads(resp)
                    if j.get("code") == 0:
                        ok_list.append(label)
                        _done = True
                        break
                    else:
                        _err = "%s:%s" % (label, str(j.get("message", resp))[:40])
                        if _attempt == 0:
                            time.sleep(3)   # 首次失败退避后重试
                            continue
                        fail_list.append(_err)
                except Exception:
                    ok_list.append("%s(响应未解析)" % label)
                    _done = True
                    break
            except Exception as e:
                if _attempt == 0:
                    time.sleep(3)
                    continue
                fail_list.append("%s:%r" % (label, e))
        if not _done and label not in [x.split(":", 1)[0] for x in ok_list]:
            pass
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

    返回 [ {id, name, sc, pp, holdings}, ... ]；忽略无关字段。无文件/解析失败返回 []。
    sc = 该用户的 ServerChan 推送密钥；pp = 该用户的 PushPlus 令牌；
    holdings = 该用户的个性化持仓列表（与 config/holdings.json 同构）。"""
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
        out.append({
            "id": u.get("id"),
            "name": u.get("name") or u.get("id"),
            "sc": (u.get("sc") or "").strip(),
            "pp": (u.get("pp") or "").strip(),
            "holdings": u.get("holdings") if isinstance(u.get("holdings"), list) else None,
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


def _push_personalized(data, mode, users, analysis_date, results):
    """为每个『配置了专属通道 + 持股』的用户单独发送个性化消息（市場概述 + 其本人持股体检）。

    去重：同一用户同一 mode 当天只发一次（防多调度器重复烧 ServerChan 5条/天额度）。"""
    import engine as _engine
    import holdings as _hd
    con = store.connect()
    u = _engine.Universe(con, days=130)
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
      - "open_anomaly" 竞价后开盘前异动（09:26，ServerChan 固定关键节点 + PushPlus 冗余兜底）
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
    if (not dry_run and mode != "weekend" and trade_calendar is not None
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
    #    ServerChan 固定 4 条/工作日 = 盘前(preauction) + 竞价后开盘前异动(open_anomaly) +
    #    收盘(close) + 复盘(close_again)，均为关键节点，占 ServerChan 单 key 5 条/天额度中的 4 条；
    #    PushPlus 随时推送（200 条/天几乎不限）：盘中异动(anomaly)、竞价(auction)、周末发酵(weekend)
    #    以及 open_anomaly 的冗余兜底，绝不挤占 ServerChan 的固定 4 条名额。
    _all = [
        ("wechat_serverchan", send_wechat_serverchan, cfg_shared.get("wechat_serverchan")),
        ("wechat_pushplus", send_wechat_pushplus, cfg_shared.get("wechat_pushplus")),
        ("wecom", send_wecom, cfg_shared.get("wecom")),
        ("telegram", send_telegram, cfg_shared.get("telegram")),
        ("email", send_email, cfg_shared.get("email")),
    ]
    if mode == "open_anomaly":
        # 竞价后开盘前异动：ServerChan 固定四条之一（与盘前/收盘并列的关键节点），
        # 同时 PushPlus 随时冗余推送（不冲突：ServerChan 仅占 1/4 额度，其余走 PushPlus）。
        _prefer = ["wechat_serverchan", "wechat_pushplus", "wecom", "telegram", "email"]
    elif mode == "close_again":
        # 复盘补发让出 ServerChan 额度（单 key 5条/天）：复盘非生死节点，
        # 优先走 PushPlus/企微，把 SC 名额留给盘前/竞价/收盘这 3 个关键节点；
        # 仅当这些通道都未配置时才退回 SC，避免漏发。
        _has_alt = any(cfg_shared.get(n) for n in ("wechat_pushplus", "wecom", "telegram", "email"))
        _prefer = (["wechat_pushplus", "wecom", "telegram", "email"]
                   if _has_alt else
                   ["wechat_serverchan", "wechat_pushplus", "wecom", "telegram", "email"])
    elif mode in ("anomaly", "auction", "weekend", "panic", "yaogu", "stoploss"):
        # 盘中异动 / 竞价确认 / 周末发酵 / 止损即时：全部走 PushPlus 系（200 条/天几乎不限），
        # 绝不占 ServerChan 的固定 3~4 条额度。竞价确认主动让出 ServerChan 给关键节点。
        _prefer = ["wechat_pushplus", "wecom", "telegram", "email"]
    else:
        # 盘前 / 收盘：ServerChan 为主（关键节点），PushPlus 冗余兜底。
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
        s = "%s — %s" % (g.get("signal"), g.get("detail", "外围数据缺失，按中性处理"))
        etfs = g.get("etfs") or []
        if etfs:
            s += "；ETF：" + "、".join("%s%s%%" % (e["name"], ("+" if e["pct"] >= 0 else "") + str(round(e["pct"], 1)))
                                   for e in etfs)
        return s
    return "数据缺失，按中性处理"


def _pct(v):
    return ("%.0f%%" % v) if v is not None else "—"


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
        # 盘前策略（聚合仓位/主线/接力/关注池/风险）
        pp = data.get("preopen_plan") or {}
        if pp.get("position"):
            L.append("")
            L.append("**盘前策略**")
            L.append("- 建议仓位：**%s**" % pp["position"])
            if pp.get("main_line"):
                L.append("- 主线预判：%s" % "、".join(pp["main_line"]))
            if pp.get("relay_dir"):
                L.append("- 接力方向：%s" % "、".join(pp["relay_dir"]))
            for _s in (pp.get("strategies") or [])[:4]:
                L.append("- %s" % _s)
            if pp.get("watch"):
                L.append("- 关注池：" + "、".join("%s(%s)" % (w["name"], w.get("reason", "")) for w in pp["watch"][:8]))
            if pp.get("risks"):
                L.append("- 风险提醒：%s" % "；".join(pp["risks"]))
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
    # 短线情绪微观结构（首板/断层/晋级率分档/炸板率/赚钱效应细分）
    micro = data.get("micro") or {}
    if micro:
        p = micro.get("profit") or {}
        pt = micro.get("promote_tiered") or {}
        fb = micro.get("first_board", {})
        L.append("**赚钱效应**：昨涨停今均 %s（翻红 %s / 再涨停 %s）｜ 亏钱效应(翻绿) %s"
                 % (_pct(p.get("avg_pct")), _pct(p.get("red_rate")),
                    _pct(p.get("again_rate")), _pct(p.get("green_rate"))))
        L.append("**晋级率**：1进2 %s ｜ 2进3 %s ｜ 3板+ %s ｜ **首板** %d 只 ｜ **炸板率** %s"
                 % (_pct(pt.get("1进2")), _pct(pt.get("2进3")), _pct(pt.get("3板及以上")),
                    fb.get("count", 0), _pct(micro.get("zhaban_rate"))))
        gap = micro.get("gap") or []
        if gap:
            L.append("**梯队断层**：缺 %s 板（中位断档，警惕高位分歧）" % "/".join("%d" % g for g in gap))
    # 竞价强度定调（涨停股集合竞价强弱）
    auc = data.get("auction") or {}
    mv = auc.get("market_view") or {}
    if mv and mv.get("avg_score") is not None:
        L.append("**竞价强度**：涨停股竞价定调 %s 分（%s）｜ 平均高开 %s ｜ 弱转强 %d / 强转弱 %d ｜ 抢筹 %d / 派发预警 %d"
                 % (mv.get("avg_score"), mv.get("strength"),
                    _pct(auc.get("summary", {}).get("avg_open_pct")),
                    mv.get("momentum", {}).get("weak_strong", 0),
                    mv.get("momentum", {}).get("strong_weak", 0),
                    len(mv.get("qiangchou", [])), len(mv.get("paifa", []))))
    # 主力/北向资金流向
    money = data.get("money") or {}
    if money and money.get("boards_in"):
        n = money.get("north")
        L.append("**资金流向**：全市场主力净流入 %s 亿（净流入板块 %d / 净流出 %d）｜ 主力净流入行业Top：%s"
                 % (("+" if money.get("total_main_net", 0) >= 0 else "") + str(money.get("total_main_net")),
                    money.get("net_in_boards", 0), money.get("net_out_boards", 0),
                    "、".join((b.get("name") or "") + _signed(b.get("net"), 0) + "亿" for b in money.get("boards_in", [])[:3])))
        if n:
            L.append("**北向资金**：沪 %s / 深 %s / 合计 %s 亿（净流入）" % (n.get("sh"), n.get("sz"), n.get("total")))
        else:
            L.append("**北向资金**：数据源停更（东财调整口径，暂不可用）")
    # 选股回测
    bt = data.get("backtest") or {}
    if bt and bt.get("total"):
        h1 = bt.get("h1") or {}; h3 = bt.get("h3") or {}; h5 = bt.get("h5") or {}
        L.append("**选股回测**（样本 %d）：次日胜率 %s%% / 持有3日 %s%% / 持有5日 %s%%（均收益 %s%% / %s%% / %s%%）"
                 % (bt["total"], h1.get("win", "-"), h3.get("win", "-"), h5.get("win", "-"),
                    h1.get("avg", "-"), h3.get("avg", "-"), h5.get("avg", "-")))
    # 板块接力 / 主线切换（断板→接力）
    rl = data.get("sector_relay") or {}
    if rl.get("available"):
        if rl.get("broken"):
            b = rl["broken"]
            rn = "、".join(x["name"] for x in rl.get("relay", []))
            L.append("**板块接力**：【%s】断板退潮（峰值 %d→现 %d 只涨停），资金切向【%s】"
                     % (b["name"], b["peak_zt"], b["latest_zt"], rn or "混沌轮动"))
        elif rl.get("relay"):
            rn = "、".join(x["name"] for x in rl.get("relay", []))
            L.append("**板块接力**：无单一退潮主线，当前资金聚焦【%s】" % rn)
    L.append("")

    # 恐慌 / 崩盘扫描
    pn = data.get("panic") or {}
    if pn.get("level") in ("升温", "恐慌"):
        L.append("**⚠️ 恐慌扫描**：%s（跌停 %d 家｜ 大面 %d 只｜ 昨涨停收绿 %.0f%%）"
                 % (pn.get("level"), pn.get("dt_count", 0), pn.get("bigface_count", 0),
                    pn.get("yest_green") or 0))

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
    # 妖股潜力 Top3（收盘主推送内联，按潜力分降序；数据来自上一交易日涨停池，永不空推）
    if mode in ("close", "close_again"):
        try:
            import yaogu as _yg
            yg = data.get("yaogu")
            if yg and yg.get("ranked"):
                L.append("### ⚡ 妖股潜力 Top3（上一交易日 · 按潜力分降序）")
                blk = _yg.top3_block(yg)
                if blk:
                    L.append(blk)
                    L.append("> 完整妖股潜力榜（含龙虎榜游资合力/板块联动解读）见 PushPlus 推送与站点「妖股潜力」页签。")
                L.append("")
            # 妖股双确认（基因∩潜力 交集，确认度=√(基因×潜力)）：资金+形态共振 =
            # 早期妖股确认度最高的票；两者任一塌方则被排除。无交集不展示，绝不空推。
            dms = data.get("demons")
            if yg and yg.get("ranked") and dms:
                oblk = _yg.overlap_top3_block(dms, yg.get("ranked"))
                if oblk:
                    L.append("### 🔗 妖股双确认 Top3（基因∩潜力 · 确认度=√(基因×潜力)）")
                    L.append(oblk)
                    L.append("> 资金+形态共振的最强早期妖股信号；完整双确认榜见站点「妖股交集」页签。")
                    L.append("")
        except Exception:
            pass
    if url:
        L.append("---")
        L.append("完整数据看板：%s" % url)
    return {"title": "A股盘后复盘 %s" % date, "text": "\n".join(L)}


# ServerChan 免费档单条 desp 硬上限 8192 字，超长会被静默丢弃（导致收盘/复盘只到 PushPlus）。
# 因此给 ServerChan 单独生成「只给结果、紧凑有逻辑」的版本：去掉判断过程与冗长叙述，
# 仅保留盘面数据 + 牛股雷达 + 推荐 + 趋势 + 妖股 + 风险，天然压到 8K 以内。
# 这同时满足了"排版精美、逻辑强、只给结果"的诉求。
SC_CAP = 8000


def format_sc(data, url="", mode="close"):
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
        L.append("## 竞价前 · %s" % date)
        g = data.get("global_market") or {}
        L.append("外围：%s" % _global_signal(g))
        if regime.get("note"):
            L.append("连板：%s" % regime.get("note", ""))
        pool = _top_recs(rec.get("core"), rec.get("relay"), rec.get("all"), MAX_RECS)
        L.append("昨日推荐 · 今日竞价强弱：")
        for i, it in enumerate(pool, 1):
            L.append(_rec_line(it, i, tag="续强"))
        pp = data.get("preopen_plan") or {}
        if pp.get("position"):
            L.append("盘前策略：仓位 %s ｜ 主线 %s ｜ 接力 %s"
                     % (pp["position"], "、".join(pp.get("main_line", []) or []),
                        "、".join(pp.get("relay_dir", []) or [])))
        if url:
            L.append("看板：%s" % url)
        return {"title": "竞价前 %s" % date, "text": "\n".join(L)}

    # 收盘后复盘
    L = []
    if mode == "close_again":
        # 复盘补发必须与 15:20 收盘内容有差异：ServerChan 免费档对『重复/极相似 desp』会静默拒收，
        # 之前连续两天 20:00 复盘补发收不到，根因就是 close 与 close_again 的 desp 字节级相同。
        L.append("## 复盘补发 · %s" % date)
        L.append("（盘后数据已最终定格 · 含你的持仓跟踪）")
    else:
        L.append("## 盘后复盘 · %s" % date)
    L.append("情绪 %.1f(%s) ｜ 周期 %s" % (sent.get("score", 0), sent.get("label", ""), cyc.get("phase", "")))
    L.append("涨停 %d ｜ 最高 %d板 ｜ 晋级 %s ｜ 封板 %s"
             % (len(lus), max([r.get("streak", 0) for r in lus], default=0),
                _pct(sent.get("promote_rate")), _pct(sent.get("seal_rate"))))
    if regime.get("note"):
        L.append("连板研判：%s" % regime.get("note", ""))
    micro = data.get("micro") or {}
    if micro:
        p = micro.get("profit") or {}
        pt = micro.get("promote_tiered") or {}
        fb = micro.get("first_board", {})
        L.append("赚钱效应 昨涨停今均 %s(翻红 %s/再板 %s) ｜ 亏钱 %s"
                 % (_pct(p.get("avg_pct")), _pct(p.get("red_rate")),
                    _pct(p.get("again_rate")), _pct(p.get("green_rate"))))
        L.append("晋级 1进2 %s ｜ 2进3 %s ｜ 3板+ %s ｜ 首板 %d ｜ 炸板率 %s"
                 % (_pct(pt.get("1进2")), _pct(pt.get("2进3")), _pct(pt.get("3板及以上")),
                    fb.get("count", 0), _pct(micro.get("zhaban_rate"))))
    money = data.get("money") or {}
    if money and money.get("boards_in"):
        L.append("主力净流入 %s亿 ｜ 净流入行业 %s"
                 % (("+" if money.get("total_main_net", 0) >= 0 else "") + str(money.get("total_main_net")),
                    "、".join((b.get("name") or "") + _signed(b.get("net"), 0) + "亿"
                              for b in money.get("boards_in", [])[:3])))
    # 龙虎榜·游资合力（盘后公开数据，无需密钥；失败则跳过）
    lhb = data.get("lhb")
    if lhb:
        try:
            rows = sorted(lhb.items(), key=lambda kv: -(kv[1].get("net_amt") or 0))[:5]
            if rows:
                L.append("龙虎榜·游资合力 Top%d：" % len(rows))
                for code, r in rows:
                    seats = r.get("buy_seat", 0)
                    L.append("- %s %s 净买%.2f亿 ｜ %d买方席位 ｜ %s"
                             % (code, r.get("name", ""), (r.get("net_amt") or 0) / 1e8,
                                seats, (r.get("explanation") or "")[:24]))
        except Exception:
            pass
    # 牛股雷达（本次新增，多维度共振候选）
    bull = data.get("bull")
    if bull:
        L.append("牛股雷达 Top%d：" % min(MAX_RECS, len(bull)))
        for it in bull[:MAX_RECS]:
            L.append("- %s %s【%s】%.2f元 %+.1f%% 量比%.1f"
                     % (it["code"], it["name"], "+".join(it["signals"]),
                        it["price"] or 0, it["pct"] or 0, it["vol_ratio"] or 0))
    # 个股推荐
    core = rec.get("core") or []
    relay = rec.get("relay") or []
    allit = rec.get("all") or []
    recs = _top_recs(core, relay, allit, MAX_RECS)
    L.append("个股推荐 Top%d：" % MAX_RECS)
    if recs:
        for i, it in enumerate(recs, 1):
            L.append(_rec_line(it, i))
    else:
        L.append("（今日无明确推荐，建议控仓或低位试错）")
    # 趋势主升
    trend = rec.get("trend") or []
    if trend:
        L.append("趋势主升 Top%d：" % min(MAX_RECS, len(trend)))
        for it in trend[:MAX_RECS]:
            tm = it.get("trend_meta") or {}
            L.append("- %s(%s) %.2f ｜ 多头 MA5/10/20=%.2f/%.2f/%.2f ｜ 近5日%d涨 ｜ 量能%.1f倍"
                     % (it.get("name"), it.get("industry", "—"), it.get("close", 0),
                        tm.get("ma5", 0), tm.get("ma10", 0), tm.get("ma20", 0),
                        tm.get("up_days", 0), tm.get("vol_ratio", 0)))
    # 妖股潜力 Top3
    if mode in ("close", "close_again"):
        try:
            import yaogu as _yg
            yg = data.get("yaogu")
            if yg and yg.get("ranked"):
                blk = _yg.top3_block(yg)
                if blk:
                    L.append("妖股潜力 Top3：")
                    L.append(blk)
        except Exception:
            pass
    # 恐慌
    pn = data.get("panic") or {}
    if pn.get("level") in ("升温", "恐慌"):
        L.append("⚠ 恐慌：%s 跌停%d 大面%d 昨涨停收绿%.0f%%"
                 % (pn.get("level"), pn.get("dt_count", 0), pn.get("bigface_count", 0),
                    pn.get("yest_green") or 0))
    # 冷启修复预判
    cw = data.get("cold")
    if cw and cw.get("forecast"):
        f0 = cw["forecast"]
        L.append("冷启预判：%s · 预计 %s（T+1 %.0f%% / 两日内 %.0f%% / 三日内 %.0f%%）"
                 % (f0["state"], f0["expect"], f0.get("p_t1", 0) * 100,
                    f0.get("cum_t2", 0) * 100, f0.get("cum_t3", 0) * 100))
        cands = cw.get("candidates") or []
        if cands:
            L.append("冷后候选：")
            for it in cands[:3]:
                L.append("- %s %s %.2f元 ｜ %s" % (it["code"], it["name"], it["price"] or 0, it.get("why", "")))
    # 持股监测
    hrep = data.get("holdings")
    if hrep and hrep.get("enabled"):
        try:
            import holdings as _hd
            for ln in _hd.summary_lines(hrep, limit=6):
                L.append(ln)
        except Exception:
            pass
    # 复盘补发专享（20:00，与 15:20 收盘相比补上更完整的盘后增量解读）
    if mode == "close_again":
        zb = data.get("market", {}).get("zhaban_stats")
        if zb and zb.get("samples"):
            L.append("炸板规律参考：近 %d 只『触板未封住』样本，次日平均收 %s%%（收绿率 %s%%）——"
                     "高位烂板隔日风险偏高，打板需看封单质量。"
                     % (zb.get("samples", 0), zb.get("avg_next_close"), _pct(zb.get("green_rate"))))
        ro = (data.get("cold") or {}).get("rotation") or {}
        if ro.get("pairs"):
            L.append("冷后方向轮动：相邻两次冷后换方向概率 %.0f%%；最近领涨方向 %s"
                     % (ro["switch_rate"] * 100, "→".join(ro.get("last_inds") or []) or "—"))
        # 持股监测评级变化（相对上一交易日的降级预警）
        if hrep and hrep.get("enabled") and hrep.get("alerts"):
            L.append("⚠ 你的持仓评级变化：" + "；".join(hrep["alerts"][:4]))
    if url:
        L.append("看板：%s" % url)
    _title = ("复盘补发 %s" % date) if mode == "close_again" else ("盘后复盘 %s" % date)
    return {"title": _title, "text": "\n".join(L)}


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
    # 测试：python notifier.py [--send] [--preauction|--auction|--anomaly|--close_again|--weekend]
    #   不加 --send 只打印不发送（安全默认）；加了 --send 才真正推送
    mode = "close"
    for kw in ("preauction", "auction", "anomaly", "close_again", "weekend"):
        if ("--" + kw) in sys.argv:
            mode = kw
    dry_run = ("--send" not in sys.argv)
    push({"title": "测试", "text": "这是一条测试推送"}, dry_run=dry_run, mode=mode)
