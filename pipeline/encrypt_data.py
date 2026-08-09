"""把 dist/data.js 的明文负载，按 allowed_users 中每个用户的口令加密。

输出：
  dist/data/<id>.bin   每个用户一份（salt16 + 密文），公网可见但无口令无法解密
  dist/users.json      仅含 [{id, name}]，不含口令，供登录页展示用户列表

加密算法（与 dist/auth.js 的 WebCrypto 完全对应）：
  key  = PBKDF2-HMAC-SHA256(pass, salt, 200000) -> 32 字节
  密钥流 = HMAC-SHA256(key, i:uint32) 依次拼接，取前 len(明文) 字节
  密文  = 明文 XOR 密钥流
  文件  = salt(16) + 密文

说明：这是“口令门禁”而非服务端账号系统——数据公开但加密，只有持口令者能解密。
对“小范围分享 + 防链接泄露”足够；若需服务端强管控请改用 Cloudflare/Render 方案。
"""
import sys
import os
import json
import hashlib
import hmac

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
CONFIG = os.path.join(ROOT, "config")
ITER = 200000
SALTB = 16


def _load_payload():
    p = os.path.join(DIST, "data.js")
    if not os.path.exists(p):
        raise SystemExit("找不到 dist/data.js，请先运行 pipeline/build.py")
    txt = open(p, encoding="utf-8").read()
    marker = "window.__STOCK_DATA__ = "
    if not txt.startswith(marker):
        raise SystemExit("data.js 格式异常（缺少 %r 前缀）" % marker)
    js = txt[len(marker):]
    if js.endswith(";\n"):
        js = js[:-2]
    elif js.endswith(";"):
        js = js[:-1]
    return js.encode("utf-8")


def _key(passwd, salt):
    return hashlib.pbkdf2_hmac("sha256", passwd.encode("utf-8"), salt, ITER, dklen=32)


def _keystream(key, n):
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(key, i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def encrypt_user(payload, passwd):
    salt = os.urandom(SALTB)
    key = _key(passwd, salt)
    ct = bytes(a ^ b for a, b in zip(payload, _keystream(key, len(payload))))
    return salt + ct


def main():
    users_path = os.path.join(CONFIG, "allowed_users.json")
    if not os.path.exists(users_path):
        sys.stderr.write(
            "[encrypt] 未找到 config/allowed_users.json（来自 ALLOWED_USERS_JSON 密钥）。\n"
            "          跳过加密：本此发布的站点将没有可解密的数据，登录后会提示“无数据”。\n"
        )
        return 0
    try:
        cfg = json.load(open(users_path, encoding="utf-8"))
    except Exception as e:
        sys.stderr.write("[encrypt] allowed_users.json 解析失败：%r\n" % e)
        return 0
    users = cfg.get("users") or []
    if not users:
        sys.stderr.write("[encrypt] allowed_users.json 中没有用户，跳过加密。\n")
        return 0

    try:
        payload = _load_payload()
    except SystemExit as e:
        sys.stderr.write("[encrypt] %s\n" % e)
        return 0

    out_dir = os.path.join(DIST, "data")
    os.makedirs(out_dir, exist_ok=True)
    meta = []
    for u in users:
        uid = (u.get("id") or "").strip()
        name = u.get("name") or uid
        pw = u.get("pass") or ""
        if not uid or not pw:
            sys.stderr.write("[encrypt] 跳过无效用户（缺 id 或 pass）：%r\n" % uid)
            continue
        if not all(c.isalnum() or c in "_-" for c in uid):
            sys.stderr.write("[encrypt] 用户 id 只能含字母数字/_-：%r\n" % uid)
            continue
        blob = encrypt_user(payload, pw)
        with open(os.path.join(out_dir, uid + ".bin"), "wb") as f:
            f.write(blob)
        meta.append({"id": uid, "name": name})
        print("[encrypt] 已加密用户 %s（%s）→ data/%s.bin" % (name, uid, uid))

    if not meta:
        sys.stderr.write("[encrypt] 没有有效用户，未生成任何密文。\n")
        return 0

    with open(os.path.join(DIST, "users.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=0)
    print("[encrypt] 共 %d 个用户，已写出 dist/users.json" % len(meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
