"""线上站点体检：确认能访问、明文数据确实不存在、密文能下载。

用法：python tools/verify_site.py <站点URL>
适配 GitHub Pages 与 Cloudflare Pages（后者对不存在路径回退 index.html，
以「200 + 非 HTML 内容」判定真文件，避免把回退壳误报为泄露）。
下载的密文会存到 _livecheck/ 供 verify_decrypt.js 解密验证。
"""
import os
import sys
import ssl
import json
import hashlib
import hmac
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "_livecheck")
ITER = 200000

MUST_EXIST = ["index.html", "auth.js", "users.json", "app.js", "charts.js", "styles.css"]
MUST_MISS = ["data.js", "data.js.bak", "push_log.jsonl", "config/allowed_users.json"]


def fetch(url, timeout=30):
    """返回 (status, body)。CF Pages 对不存在路径会回退 index.html（200 + text/html），
    调用方须用 is_real() 区分真文件与回退壳。"""
    req = urllib.request.Request(url, headers={"User-Agent": "site-check"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return 0, str(e).encode()


def is_real(st, body):
    """真文件判定：非 text/html（SPA 回退壳是 text/html 的 index.html）。"""
    if st != 200:
        return False
    head = body[:400].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return False
    return True


def _owner_password():
    """从 config/owner_pass.txt（gitignored）或环境变量取 owner 口令。"""
    p = os.environ.get("OWNER_PASS") or os.environ.get("SA_OWNER_PASS")
    if p:
        return p.strip()
    pp = os.path.join(ROOT, "config", "owner_pass.txt")
    if os.path.exists(pp):
        try:
            return open(pp, encoding="utf-8").read().strip()
        except Exception:
            return ""
    return ""


def _decrypt_bin(blob, passwd):
    """复刻 encrypt_data.py：salt(16)+密文，PBKDF2-HMAC-SHA256(20万)->HMAC密钥流 XOR。"""
    salt, ct = blob[:16], blob[16:]
    key = hashlib.pbkdf2_hmac("sha256", passwd.encode("utf-8"), salt, ITER, dklen=32)
    ks = bytearray()
    i = 0
    while len(ks) < len(ct):
        ks += hmac.new(key, i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    ks = bytes(ks[:len(ct)])
    return bytes(a ^ b for a, b in zip(ct, ks))


def decrypt_check(base, users):
    """用 owner 口令真实解密一份密文，证明「密文可解密」契约。返回失败计数。

    无口令时仅告警、不计失败（CI 等无口令环境仍可跑体检）。
    """
    fails = 0
    passwd = _owner_password()
    if not passwd:
        print("  ⚠️ 未提供 owner 口令（config/owner_pass.txt 或 OWNER_PASS），跳过解密自检。")
        print("     演示：OWNER_PASS=xxx python tools/verify_site.py %s" % base)
        return 0
    owner = next((u for u in users if u.get("id") == "owner"), None)
    uid = (owner or {}).get("id") or (users[0].get("id") if users else None)
    if not uid:
        print("  ⚠️ 无用户可供解密自检")
        return 0
    st, body = fetch(base + "/data/" + uid + ".bin")
    if st != 200 or len(body) <= 16:
        print("  ❌ 无法下载 data/%s.bin 做解密自检（HTTP %s）" % (uid, st))
        return 1
    plain = _decrypt_bin(body, passwd)
    try:
        obj = json.loads(plain.decode("utf-8"))
    except Exception as e:
        print("  ❌ 口令解密 data/%s.bin 后不是合法 JSON（口令错或算法不匹配）：%s" % (uid, e))
        return 1
    meta = obj.get("meta") or {}
    print("  ✅ 用口令成功解密 data/%s.bin → JSON（字段：%s）" % (uid, ", ".join(list(obj.keys())[:6])))
    print("     日期=%s  数据源=%s  涨停=%d" % (meta.get("date"), meta.get("source"), len(obj.get("limit_ups") or [])))
    # 错误口令必须失败
    bad_plain = _decrypt_bin(body, passwd + "x")
    try:
        json.loads(bad_plain.decode("utf-8"))
        print("  ❌ 错误口令竟然也能解出 JSON！门禁失效。")
        fails += 1
    except Exception:
        print("  ✅ 错误口令无法解密（符合预期）")
    return fails


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/verify_site.py <站点URL>")
        return 2
    base = sys.argv[1].rstrip("/")
    os.makedirs(os.path.join(OUT, "data"), exist_ok=True)
    bad = 0

    print("== 必须存在 ==")
    for f in MUST_EXIST:
        st, body = fetch(base + "/" + f)
        # index.html 本身就是 HTML，只查状态码；其余用 is_real 排除 SPA 回退壳
        ok = (st == 200 and len(body) > 0) if f == "index.html" else is_real(st, body)
        bad += 0 if ok else 1
        print("  %s %-14s HTTP %s  %d 字节%s" % ("✅" if ok else "❌", f, st, len(body),
              "" if ok else "（SPA回退壳，真缺失）"))
        if ok:
            with open(os.path.join(OUT, f), "wb") as fh:
                fh.write(body)

    print("== 必须不存在（存在即泄露） ==")
    for f in MUST_MISS:
        st, body = fetch(base + "/" + f)
        # CF Pages 对不存在路径回退 index.html（200+HTML 壳），不是真实文件 → 不算泄露
        leaked = is_real(st, body)
        bad += 1 if leaked else 0
        print("  %s %-24s HTTP %s" % ("✅ 不存在" if not leaked else "❌ 泄露！", f, st))

    print("== 加密数据 ==")
    users = []
    p = os.path.join(OUT, "users.json")
    if os.path.exists(p):
        try:
            users = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            print("  ❌ users.json 解析失败：%s" % e)
            bad += 1
    if any("pass" in u for u in users):
        print("  ❌ users.json 里含 pass 字段，口令泄露！")
        bad += 1
    for u in users:
        st, body = fetch(base + "/data/" + u["id"] + ".bin")
        ok = st == 200 and len(body) > 32
        bad += 0 if ok else 1
        print("  %s data/%s.bin  HTTP %s  %.0f KB" % ("✅" if ok else "❌", u["id"], st, len(body) / 1024))
        if ok:
            with open(os.path.join(OUT, "data", u["id"] + ".bin"), "wb") as fh:
                fh.write(body)
            head = body[:200]
            if b"__STOCK_DATA__" in head or head.lstrip()[:1] in (b"{", b"["):
                print("     ❌ 密文开头像明文 JSON，加密可能没生效！")
                bad += 1

    if users:
        print("== 解密自检（需 owner 口令） ==")
        bad += decrypt_check(base, users)

    print("\n%s" % ("✅ 全部通过" if bad == 0 else "❌ 有 %d 项不通过" % bad))
    print("密文已存到 %s，可用 verify_decrypt.js 验证解密。" % OUT)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
