# -*- coding: utf-8 -*-
"""线上数据解密（执行器专用，与 pipeline/encrypt_data.py 算法完全对应）。

算法：
  key  = PBKDF2-HMAC-SHA256(pass, salt, 200000) -> 32 字节
  密钥流 = HMAC-SHA256(key, i:uint32) 拼接
  明文 = 密文 XOR 密钥流
  文件 = salt(16) + 密文
"""
import hashlib
import hmac
import json
import time
import urllib.request
import ssl

SITE = "https://stock-analysis-8zm.pages.dev"
ITER = 200000

# MITM 代理环境（本机 127.0.0.1:10808 自签 CA）下需要跳过证书校验
_CTX = ssl._create_unverified_context()


def _key(passwd: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passwd.encode("utf-8"), salt, ITER, dklen=32)


def _keystream(key: bytes, n: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(key, i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def decrypt_blob(blob: bytes, passwd: str) -> bytes:
    salt, ct = blob[:16], blob[16:]
    key = _key(passwd, salt)
    return bytes(a ^ b for a, b in zip(ct, _keystream(key, len(ct))))


_RETRY = 3


def _fetch_bytes(url, timeout=30, retries=_RETRY):
    """带重试的线上拉取。403/5xx/网络抖动均重试（CF Pages 偶发 bot 拦截返回 403，
    记忆踩坑：curl 必须 -4 -k、通道优先 gh_api > raw；urllib 间歇性 403 需重试）。
    用完整浏览器 UA 降低被 CF 识别为 bot 的概率。"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0 Safari/537.36 stock-executor"),
                "Accept": "*/*",
            })
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429, 500, 502, 503, 504) and i < retries - 1:
                time.sleep(1.5 * (i + 1))   # 退避重试
                continue
            raise
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
                continue
            raise
    raise last if last else RuntimeError("fetch failed")


def fetch_user_data(user_id: str, passwd: str, timeout: int = 30) -> dict:
    """拉线上 data/<id>.bin 并解密为 data dict。失败抛异常（带 403/5xx 重试）。"""
    url = "%s/data/%s.bin" % (SITE, user_id)
    blob = _fetch_bytes(url, timeout=timeout)
    plain = decrypt_blob(blob, passwd)
    txt = plain.decode("utf-8")
    if txt.startswith("window.__STOCK_DATA__ = "):
        txt = txt[len("window.__STOCK_DATA__ = "):].rstrip().rstrip(";")
    return json.loads(txt)


def data_date(data: dict):
    """取线上数据的分析日期：meta.date 优先，scan_coverage.date 兜底。"""
    d = (data or {}).get("meta") or {}
    if d.get("date"):
        return d["date"]
    d = (data or {}).get("scan_coverage") or {}
    if d.get("date"):
        return d["date"]
    return None


def prev_trade_date(timeout: int = 15, retries: int = 2):
    """上一交易日（腾讯上证指数日 K，权威日历）。

    纯云端托管下这是「数据新鲜度」的锚：build 在 15:20 跑完、站点部署后，
    次日 09:25 执行器理应读到上一交易日的数据。若 build 或 CF 部署任一环节失败，
    站点上的 data/*.bin 会停留在更早的日期——没有这道闸门，执行器会拿过期信号
    在今天开盘真金白银下单（用户看不到原因，只看到「买了奇怪的票」）。

    返回 "YYYY-MM-DD" 或 None（取不到时返回 None，调用方放行——网络抖动误杀
    比陈旧数据交易的伤害小，但两者都应推送告知）。
    """
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=sh000001,day,,,10,qfq")
    last = None
    for i in range(1 + max(0, retries)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                js = json.loads(r.read().decode("utf-8"))
            node = (js.get("data") or {}).get("sh000001") or {}
            rows = node.get("qfqday") or node.get("day") or []
            if not rows:
                return None
            days = [str(x[0]) for x in rows if x and x[0]]
            days = [d[:4] + "-" + d[4:6] + "-" + d[6:8] if len(d) == 8 and "-" not in d
                    else d for d in days]
            days = sorted(set(days))
            today = time.strftime("%Y-%m-%d")
            # 若最后一根是今天（盘后已更新），上一交易日取倒数第二根；否则取最后一根
            if days[-1] <= today and len(days) >= 2 and days[-1] == today:
                return days[-2]
            return days[-1]
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(1.5 * (i + 1))
    return None


def assert_data_fresh(data: dict, force: bool = False):
    """数据新鲜度闸门：线上数据日期必须 ≥ 上一交易日，否则抛异常拒交易。

    覆盖两种纯托管故障（用户不开电脑，看不到 CI 失败）：
      ① build workflow 失败（数据源异常/Secret 缺失/代码错误）
      ② build 成功但 CF Pages 部署失败（站点停在旧版本）
    两者都表现为「线上数据是旧日期」→ 一律拒绝开仓，不记任务账本（冗余触发会
    在几分钟后自动重试，若届时数据已更新则正常交易）。
    """
    if force:
        return None
    d = data_date(data)
    if not d:
        return None       # 数据无日期字段（老版本）→ 放行，避免误杀
    prev = prev_trade_date()
    if not prev:
        return None       # 日历取不到（网络问题）→ 放行，避免误杀
    if str(d) >= str(prev):
        return None
    raise RuntimeError(
        "线上数据日期 %s 早于上一交易日 %s（build 或 CF 部署未成功，数据已过期）"
        % (d, prev))


def fetch_json(path: str, timeout: int = 30):
    """拉线上任意静态 json（如 users.json）。带 403/5xx 重试。"""
    url = "%s/%s" % (SITE, path)
    body = _fetch_bytes(url, timeout=timeout)
    return json.loads(body.decode("utf-8"))


def extract_signals(data: dict) -> list:
    """从 data dict 提取带竞价决策线的可执行信号。

    来源优先级：recommend.core > recommend.relay > recommend.fused。
    每条信号：{code, name, streak, action, auction_rule, close, tag}

    2026-09-01：st=0（趋势/动量引擎票）与 st≥1（涨停体系）均可交易，
    但走各自的决策线（market_type 字段区分），详见下方 _add 注释。
    """
    rec = data.get("recommend") or {}
    out, seen = [], set()

    def _add(it, src):
        code = it.get("code")
        if not code or code in seen:
            return
        ar = it.get("auction_rule") or {}
        # 2026-09-01 用户拍板：模拟盘什么票都能买，不只连板票。
        # 区分市场类型（market_type）走各自的决策线，而不是一刀切过滤：
        #   limitup（st≥1，昨日涨停/连板体系）→ 竞价纪律（高开≥2% 跟进，118 万
        #     K 线回测，gap≥2 条件下核心龙头 86.7%/+7.16%、st=1 72.2%/+4.21%）
        #   trend（st=0，趋势/动量引擎票）→ 趋势专用纪律（不追高开、尾盘确认优先），
        #     半仓 + 止损收紧。原因：竞价纪律建立在涨停体系回测上，趋势票套用会
        #     追在高开溢价最贵处（实证 920087 st=0 高开 2.2% 跟进次日 -6.03%）。
        mt = "limitup" if (it.get("streak") or 0) else "trend"
        out.append({
            "code": code, "name": it.get("name") or "",
            "streak": it.get("streak") or 0,
            "close": it.get("close"),
            "tag": it.get("tag") or src,
            "auction_rule": ar.get("rule") or "",
            "source": src,
            "market_type": mt,
        })
        seen.add(code)

    for it in (rec.get("core") or []):
        _add(it, "core")
    for it in (rec.get("relay") or []):
        _add(it, "relay")
    for it in (rec.get("fused") or []):
        _add(it, "fused")
    return out


def realtime_quote(codes: list, timeout: int = 10, retries: int = 2) -> dict:
    """腾讯实时行情批量接口（CORS 友好，无需鉴权）。

    返回 {code: {"open": 开盘价, "price": 现价, "prev_close": 昨收}}。
    竞价结束后（9:25+）open 即当日开盘价。
    2026-08-31 升级：CI 弱网偶发 URLError/超时曾致整轮「行情失败，平仓顺延」
    （持仓该卖的没卖成）——加 3 次指数退避重试（0/2/4s），失败才向上抛。
    """
    if not codes:
        return {}
    qcodes = []
    for c in codes:
        if c[:2] in ("sh", "sz", "bj"):
            # 2026-08-30 CI 托管：指数码（如 sh000001 上证指数）直接带前缀传入，
            # 透传不二次映射（否则会拼成 szsh000001 查无此码）
            qcodes.append(c)
            continue
        prefix = "sh" if c[0] in ("6", "9") else ("bj" if c[0] in ("4", "8") else "sz")
        qcodes.append(prefix + c)
    url = "https://qt.gtimg.cn/q=" + ",".join(qcodes)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    txt = None
    last_err = None
    for attempt in range(1 + max(0, retries)):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                txt = r.read().decode("gbk", errors="ignore")
            break
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))  # 2s / 4s
    if txt is None:
        raise last_err  # 全部重试失败，保持原有异常语义
    out = {}
    for line in txt.split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        var, val = line.split("=", 1)
        val = val.strip('"').strip()
        fields = val.split("~")
        if len(fields) < 6:
            continue
        full = var.split("_")[-1]  # sh600000
        code = full[2:]
        try:
            # 腾讯字段表（factor_ext.py 完整版；a-stock-data 实测知识）：
            # 1=名称 3=现价 4=昨收 5=今开 6=成交量(手) 37=成交额(万) 38=换手率
            # 39=PE(TTM) 43=振幅 44=流通市值(亿) 45=总市值(亿) 46=市净率PB
            def _fl(i):
                try:
                    return float(fields[i] or 0) if len(fields) > i else 0.0
                except ValueError:
                    return 0.0
            out[code] = {
                "name": fields[1],
                "price": float(fields[3] or 0),
                "prev_close": float(fields[4] or 0),
                "open": float(fields[5] or 0),
                "high": _fl(33),          # 当日最高价（尾盘冲高回落判定）
                "low": _fl(34),           # 当日最低价
                "float_mv": _fl(44),      # 流通市值（亿元）
                "turnover": _fl(38),      # 换手率 %
                "amplitude": _fl(43),     # 振幅 %
                "pb": _fl(46),            # 市净率
                "pe_ttm": _fl(39),        # 市盈率 TTM
                "amount_wan": _fl(37),    # 成交额（万元）
                # 2026-08-30 CI 托管：行情时间戳 YYYYMMDDHHMMSS（field 30），
                # 用于交易日判定（节假日行情停在上个交易日 → 执行器整轮跳过）
                "stamp": (fields[30] if len(fields) > 30 else ""),
            }
        except (ValueError, IndexError):
            continue
    return out


def market_gate(data: dict) -> dict:
    """大盘环境闸门（2026-08-29 用户需求：不是每天都该交易，空仓也是操作）。

    数据源全部来自线上 build.py 已算好的指标，无本地依赖：
      · sector_forecast.__market__  大盘当日预判（dir 震荡/偏强/偏弱, env -10..+10, score）
      · market.sentiment            情绪温度计（score 0-100）
    规则（保守优先，宁错过不做）：
      · env <= -5 或 dir 偏弱        → FREEZE（不开新仓；持仓按卖出策略独立裁决）
      · env <= -2 或 情绪分 < 40     → CAUTION（新仓减半）
      · 其余                        → NORMAL
    返回 {mode, dirn, score, env, reason}。缺数据时返回 NORMAL（不因数据缺失卡死交易）。
    """
    sfc = (data.get("sector_forecast") or {}).get("__market__") or {}
    sent = (data.get("market") or {}).get("sentiment") or {}
    dirn = sfc.get("dir") or ""
    env = sfc.get("env")
    score = sent.get("score")
    try:
        env = float(env) if env is not None else None
    except (TypeError, ValueError):
        env = None
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None

    if (env is not None and env <= -5) or "弱" in dirn:
        return {"mode": "FREEZE", "dirn": dirn, "score": score, "env": env,
                "reason": "大盘预判「%s」（env=%s）：弱势环境开新仓为负期望，空仓观望"
                          % (dirn or "?", env)}
    if (env is not None and env <= -2) or (score is not None and score < 40):
        return {"mode": "CAUTION", "dirn": dirn, "score": score, "env": env,
                "reason": "大盘预判「%s」（env=%s，情绪%.0f）：环境偏谨慎，新仓减半"
                          % (dirn or "?", env, score if score is not None else 0)}
    return {"mode": "NORMAL", "dirn": dirn, "score": score, "env": env,
            "reason": "大盘预判「%s」（env=%s）：环境允许正常开仓"
                      % (dirn or "?", env)}


def auction_gate(sig: dict, quote: dict) -> dict:
    """竞价决策线裁决：高开≥2%跟进 / 低开≤-2%放弃 / 平开观望。

    2026-08-30 同步引擎归因结论：st=2 二板接力的弱高开（2-5%）历史胜率仅
    14.3% / -2.35%（rec_picks 44 条实测，全样本同桶 +2.26% 显著反向），
    gap≥5% 子桶才正常（66.7% / +5.84%）→ st=2 门槛收紧到 5%，
    与 engine.auction_discipline 保持一致。

    返回 {code, name, verdict: BUY/WATCH/ABORT, open_gap, reason}
    """
    q = quote.get(sig["code"]) or {}
    prev_close = q.get("prev_close") or sig.get("close") or 0
    cur_open = q.get("open") or 0
    if not prev_close or not cur_open:
        return dict(sig, verdict="ABORT", open_gap=None, reason="无有效行情")
    gap = (cur_open / prev_close - 1) * 100
    # 2026-09-01 趋势票专用纪律（st=0，非涨停体系，用户要求「什么票都可以买」）：
    # 不套用高开≥2% 跟进（该规则建立在涨停体系回测上，趋势票追高开溢价最贵——
    # 实证 920087 st=0 高开 2.2% 跟进次日 -6.03%）。趋势票只在「平开微红 0~2%」
    # 或「尾盘微红横盘确认（late_gate）」介入，且一律半仓（T 级）。
    if sig.get("market_type") == "trend":
        if gap <= -2:
            return dict(sig, verdict="ABORT", open_gap=round(gap, 2),
                        reason="趋势走弱（低开%.2f%%），回避" % gap)
        if 0 <= gap <= 2:
            return dict(sig, verdict="BUY", open_gap=round(gap, 2),
                        reason="买点（平开微红%.2f%%）· 趋势延续，T级半仓" % gap)
        if gap > 2:
            return dict(sig, verdict="WATCH", open_gap=round(gap, 2),
                        reason="高开%.2f%%偏贵 · 等 14:45 尾盘确认再入" % gap)
        return dict(sig, verdict="WATCH", open_gap=round(gap, 2),
                    reason="微低开%.2f%% · 等方向确认" % gap)
    if (sig.get("streak") or 0) == 2:
        # 二板接力只认强确认（2-5% 弱高开是派发陷阱）
        if gap >= 5:
            return dict(sig, verdict="BUY", open_gap=round(gap, 2),
                        reason="买点（st=2强高开%.2f%%）· 二板强确认" % gap)
        if gap <= -2:
            return dict(sig, verdict="ABORT", open_gap=round(gap, 2),
                        reason="st=2低开%.2f%%弱势，回避" % gap)
        return dict(sig, verdict="WATCH", open_gap=round(gap, 2),
                    reason="st=2弱高开%.2f%% · 接二板胜率低，观望" % gap)
    if gap >= 2:
        return dict(sig, verdict="BUY", open_gap=round(gap, 2),
                    reason="买点（高开%.2f%%）· 强势确认" % gap)
    if gap <= -2:
        return dict(sig, verdict="ABORT", open_gap=round(gap, 2),
                    reason="低开%.2f%%弱势，回避" % gap)
    return dict(sig, verdict="WATCH", open_gap=round(gap, 2),
                reason="平开%.2f%% · 观望等方向" % gap)


def late_gate(sig: dict, quote: dict) -> dict:
    """尾盘确认门（14:45 版，2026-08-30 回测落地）。

    实证（market.db 309 个交易日全市场涨停票样本，前提：当日通过竞价纪律）：
      14:45 现价在开盘价上方 0~2%（微红横盘不回补）→ 次日 +3.01% / 胜率 62.9%
        （1623 样本，14 个月逐月全正 +2.28%~+4.11%——最强过夜形态）
      现价低于开盘 3% 以上（尾盘深亏）  → 次日 -0.31% / 胜率 44.9%（11/14 个月为负）
      现价高于开盘 5% 以上（尾盘强拉）  → 次日仅 +0.62%（尾盘大拉透支隔夜溢价）
    结论：尾盘通道的正确形态是「确认横盘强」而非「追尾盘拉升」。

    2026-08-30：st=2 的开盘溢价门槛同步收紧到 5%（弱高开接二板是派发陷阱，
    与 auction_gate 同口径），其余高度维持 ≥2%。

    返回 {code, name, verdict: BUY/WATCH/ABORT, open_gap, day_fade, reason}
    day_fade = 现价相对今日开盘的偏移 %（正=红盘，负=走弱）
    """
    q = quote.get(sig["code"]) or {}
    prev_close = q.get("prev_close") or sig.get("close") or 0
    opn = q.get("open") or 0
    cur = q.get("price") or 0
    if not prev_close or not opn or not cur:
        return dict(sig, verdict="ABORT", open_gap=None, day_fade=None, reason="无有效行情")
    gap = (opn / prev_close - 1) * 100
    fade = (cur / opn - 1) * 100
    base = dict(sig, open_gap=round(gap, 2), day_fade=round(fade, 2))
    # 2026-09-01 趋势票：尾盘通道是主要入场口——不要求开盘溢价≥2%（趋势票不涨停），
    # 只要求「当日微红横盘、不回补」（形态确认），半仓。
    if sig.get("market_type") == "trend":
        if fade is None or fade <= -3:
            return dict(base, verdict="ABORT",
                        reason="尾盘走弱（较开盘%.2f%%），不接" % fade)
        if 0 <= fade <= 2:
            return dict(base, verdict="BUY",
                        reason="买点（尾盘微红%.2f%%横盘）· 趋势延续，T级半仓" % fade)
        return dict(base, verdict="WATCH",
                    reason="尾盘较开盘%+.2f%% · 形态未确认，观望" % fade)
    min_gap = 5 if (sig.get("streak") or 0) == 2 else 2
    if gap < min_gap:
        return dict(base, verdict="ABORT",
                    reason="开盘溢价%.2f%%不足（st=%s需≥%d%%），非竞价纪律票" % (gap, sig.get("streak") or 0, min_gap))
    if fade <= -3:
        return dict(base, verdict="ABORT",
                    reason="尾盘深亏（较开盘%.2f%%）· 不接飞刀" % fade)
    if 0 <= fade <= 2:
        return dict(base, verdict="BUY",
                    reason="买点（高开%.2f%%+尾盘微红%.2f%%横盘）· 最强过夜形态" % (gap, fade))
    if fade > 5:
        return dict(base, verdict="WATCH",
                    reason="尾盘强拉+%.2f%% · 溢价透支，不追" % fade)
    return dict(base, verdict="WATCH",
                reason="尾盘较开盘%+.2f%% · 中性观望" % fade)
