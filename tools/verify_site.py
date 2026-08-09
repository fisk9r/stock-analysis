"""线上站点体检：确认能访问、明文数据确实不存在、密文能下载。

用法：python tools/verify_site.py https://<user>.github.io/stock-analysis
下载的密文会存到 _livecheck/ 供 verify_decrypt.js 解密验证。
"""
import os
import sys
import ssl
import json
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "_livecheck")

MUST_EXIST = ["index.html", "auth.js", "users.json", "app.js", "charts.js", "styles.css"]
MUST_MISS = ["data.js", "data.js.bak", "push_log.jsonl", "config/allowed_users.json"]


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "site-check"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return 0, str(e).encode()


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
        ok = st == 200
        bad += 0 if ok else 1
        print("  %s %-14s HTTP %s  %d 字节" % ("✅" if ok else "❌", f, st, len(body)))
        if ok:
            with open(os.path.join(OUT, f), "wb") as fh:
                fh.write(body)

    print("== 必须不存在（存在即泄露） ==")
    for f in MUST_MISS:
        st, _ = fetch(base + "/" + f)
        ok = st != 200
        bad += 0 if ok else 1
        print("  %s %-24s HTTP %s" % ("✅" if ok else "❌ 泄露！", f, st))

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

    print("\n%s" % ("✅ 全部通过" if bad == 0 else "❌ 有 %d 项不通过" % bad))
    print("密文已存到 %s，可用 verify_decrypt.js 验证解密。" % OUT)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
