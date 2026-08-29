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
            # 腾讯字段：1=名称 2=代码 3=现价 4=昨收 5=今开
            out[code] = {
                "name": fields[1],
                "price": float(fields[3] or 0),
                "prev_close": float(fields[4] or 0),
                "open": float(fields[5] or 0),
            }
        except (ValueError, IndexError):
            continue
    return out


def auction_gate(sig: dict, quote: dict) -> dict:
    """竞价决策线裁决：高开≥2%跟进 / 低开≤-2%放弃 / 平开观望。

    st>=3 高度票：高开≥2% 积极跟进（同阈值，但备注不同）。
    返回 {code, name, verdict: BUY/WATCH/ABORT, open_gap, reason}
    """
    q = quote.get(sig["code"]) or {}
    prev_close = q.get("prev_close") or sig.get("close") or 0
    cur_open = q.get("open") or 0
    if not prev_close or not cur_open:
        return dict(sig, verdict="ABORT", open_gap=None, reason="无有效行情")
    gap = (cur_open / prev_close - 1) * 100
    if gap >= 2:
        return dict(sig, verdict="BUY", open_gap=round(gap, 2),
                    reason="高开%.2f%%≥2%%，强势确认（13个月回测胜率70%%+）" % gap)
    if gap <= -2:
        return dict(sig, verdict="ABORT", open_gap=round(gap, 2),
                    reason="低开%.2f%%≤-2%%，弱势确认，回避（回测胜率仅26.5%%）" % gap)
    return dict(sig, verdict="WATCH", open_gap=round(gap, 2),
                reason="平开%.2f%%，观望等方向" % gap)
