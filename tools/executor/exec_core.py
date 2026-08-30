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


def fetch_user_data(user_id: str, passwd: str, timeout: int = 30) -> dict:
    """拉线上 data/<id>.bin 并解密为 data dict。失败抛异常。"""
    url = "%s/data/%s.bin" % (SITE, user_id)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 executor"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        blob = r.read()
    plain = decrypt_blob(blob, passwd)
    txt = plain.decode("utf-8")
    if txt.startswith("window.__STOCK_DATA__ = "):
        txt = txt[len("window.__STOCK_DATA__ = "):].rstrip().rstrip(";")
    return json.loads(txt)


def fetch_json(path: str, timeout: int = 30):
    """拉线上任意静态 json（如 users.json）。"""
    url = "%s/%s" % (SITE, path)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 executor"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def extract_signals(data: dict) -> list:
    """从 data dict 提取带竞价决策线的可执行信号。

    来源优先级：recommend.core > recommend.relay > recommend.fused。
    每条信号：{code, name, streak, action, auction_rule, close, tag}
    """
    rec = data.get("recommend") or {}
    out, seen = [], set()

    def _add(it, src):
        code = it.get("code")
        if not code or code in seen:
            return
        ar = it.get("auction_rule") or {}
        # action 由决策线规则 + 高度决定；执行器 9:25 竞价后按实时开盘价最终裁决
        out.append({
            "code": code, "name": it.get("name") or "",
            "streak": it.get("streak") or 0,
            "close": it.get("close"),
            "tag": it.get("tag") or src,
            "auction_rule": ar.get("rule") or "",
            "source": src,
        })
        seen.add(code)

    for it in (rec.get("core") or []):
        _add(it, "core")
    for it in (rec.get("relay") or []):
        _add(it, "relay")
    for it in (rec.get("fused") or []):
        _add(it, "fused")
    return out


def realtime_quote(codes: list, timeout: int = 10) -> dict:
    """腾讯实时行情批量接口（CORS 友好，无需鉴权）。

    返回 {code: {"open": 开盘价, "price": 现价, "prev_close": 昨收}}。
    竞价结束后（9:25+）open 即当日开盘价。
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
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        txt = r.read().decode("gbk", errors="ignore")
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
    if (sig.get("streak") or 0) == 2:
        # 二板接力只认强确认（2-5% 弱高开是派发陷阱）
        if gap >= 5:
            return dict(sig, verdict="BUY", open_gap=round(gap, 2),
                        reason="st=2强高开%.2f%%≥5%%（二板只认强确认：gap≥5%%胜率66.7%%/2-5%%仅14.3%%）" % gap)
        if gap <= -2:
            return dict(sig, verdict="ABORT", open_gap=round(gap, 2),
                        reason="st=2低开%.2f%%≤-2%%，弱势确认，回避" % gap)
        return dict(sig, verdict="WATCH", open_gap=round(gap, 2),
                    reason="st=2高开仅%.2f%%（<5%%），弱高开接二板历史胜率14%%，观望" % gap)
    if gap >= 2:
        return dict(sig, verdict="BUY", open_gap=round(gap, 2),
                    reason="高开%.2f%%≥2%%，强势确认（13个月回测胜率70%%+）" % gap)
    if gap <= -2:
        return dict(sig, verdict="ABORT", open_gap=round(gap, 2),
                    reason="低开%.2f%%≤-2%%，弱势确认，回避（回测胜率仅26.5%%）" % gap)
    return dict(sig, verdict="WATCH", open_gap=round(gap, 2),
                reason="平开%.2f%%，观望等方向" % gap)


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
    min_gap = 5 if (sig.get("streak") or 0) == 2 else 2
    if gap < min_gap:
        return dict(base, verdict="ABORT",
                    reason="开盘溢价%.2f%%<%d%%（st=%s门槛），尾盘通道只处理竞价纪律通过的票"
                           % (gap, min_gap, sig.get("streak") or 0))
    if fade <= -3:
        return dict(base, verdict="ABORT",
                    reason="尾盘较开盘%.2f%%深亏，次日负期望（胜率44.9%%），不接飞刀" % fade)
    if 0 <= fade <= 2:
        return dict(base, verdict="BUY",
                    reason="高开%.2f%%+尾盘微红%.2f%%横盘不回补：最强过夜形态（62.9%%/+3.01%%，14个月全正）"
                           % (gap, fade))
    if fade > 5:
        return dict(base, verdict="WATCH",
                    reason="尾盘较开盘+%.2f%%强拉，隔夜溢价透支（次日仅+0.62%%），不追" % fade)
    return dict(base, verdict="WATCH",
                reason="尾盘较开盘%+.2f%%，形态中性观望" % fade)
