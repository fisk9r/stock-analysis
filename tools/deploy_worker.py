# -*- coding: utf-8 -*-
"""Cloudflare Worker 一键自动部署（无需网页操作、无需命令行工具 wrangler）。

用法：
  python tools/deploy_worker.py <CF_API_TOKEN>

Token 权限要求（创建时勾选）：
  - Account | Workers Scripts | Edit
  - Account | Account Settings | Read（用于自动发现 account_id / workers.dev 子域）
创建入口：dash.cloudflare.com → 右上角头像 → My Profile → API Tokens → Create Token。

脚本自动完成：
  1) 发现账户 account_id 与 *.workers.dev 子域
  2) 上传 tools/worker/index.js（ES Module）为 Worker「stock-admin」
  3) 自动注入绑定：
     - GH_TOKEN     ← 本机 ../.ghtoken 文件（GitHub PAT，绝不经浏览器）
     - ADMIN_KEY    ← config/worker_admin_key.txt
     - ALLOW_ORIGIN = https://fisk9r.github.io
     - REPO         = fisk9r/stock-analysis
  4) 启用 workers.dev 路由
  5) 用管理密钥做一次真实 ping 冒烟测试
仅依赖 Python 标准库。
"""
import io
import json
import os
import sys
import urllib.request
import uuid

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CF = "https://api.cloudflare.com/client/v4"
WORKER_NAME = "stock-admin"
REPO = "fisk9r/stock-analysis"
ALLOW_ORIGIN = "https://fisk9r.github.io"


def cf_req(token, method, path, data=None, headers=None, raw=None, content_type=None):
    url = path if path.startswith("http") else CF + path
    h = {"Authorization": "Bearer %s" % token}
    if headers:
        h.update(headers)
    body = None
    if raw is not None:
        body = raw
        h["Content-Type"] = content_type
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return e.code, {}


def multipart(parts):
    """parts: [(name, filename, content_type, bytes)] -> (body, content_type)"""
    b = "----saboundary%s" % uuid.uuid4().hex
    buf = io.BytesIO()
    for name, fn, ct, payload in parts:
        buf.write(("--%s\r\n" % b).encode())
        cd = 'Content-Disposition: form-data; name="%s"' % name
        if fn:
            cd += '; filename="%s"' % fn
        buf.write((cd + "\r\n").encode())
        buf.write(("Content-Type: %s\r\n\r\n" % ct).encode())
        buf.write(payload)
        buf.write(b"\r\n")
    buf.write(("--%s--\r\n" % b).encode())
    return buf.getvalue(), "multipart/form-data; boundary=%s" % b


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("用法：python tools/deploy_worker.py <CF_API_TOKEN>")
        sys.exit(1)
    token = sys.argv[1].strip()

    # ---- 0. 读本地凭据 ----
    gh_path = os.path.normpath(os.path.join(ROOT, "..", ".ghtoken"))
    gh_token = open(gh_path, encoding="utf-8").read().strip()
    ak_path = os.path.join(ROOT, "config", "worker_admin_key.txt")
    admin_key = open(ak_path, encoding="utf-8").read().strip()
    assert gh_token and admin_key, "缺少 .ghtoken 或 worker_admin_key.txt"

    # ---- 1. 账户 ----
    st, d = cf_req(token, "GET", "/accounts")
    assert st == 200 and d.get("success"), "Token 无效或无 Account Settings 读权限：%s" % json.dumps(d)[:300]
    acc = d["result"][0]["id"]
    print("[1/5] 账户 OK：%s" % acc)

    # ---- 2. workers.dev 子域 ----
    st, d = cf_req(token, "GET", "/accounts/%s/workers/subdomain" % acc)
    sub = (d.get("result") or {}).get("subdomain") if st == 200 else None
    print("[2/5] workers.dev 子域：%s" % (sub or "未启用（稍后尝试开启）"))

    # ---- 3. 上传脚本 + 绑定 ----
    code = open(os.path.join(ROOT, "tools", "worker", "index.js"), encoding="utf-8").read()
    meta = {
        "main_module": "index.js",
        "compatibility_date": "2024-09-23",
        "bindings": [
            {"type": "secret_text", "name": "GH_TOKEN", "text": gh_token},
            {"type": "secret_text", "name": "ADMIN_KEY", "text": admin_key},
            {"type": "plain_text", "name": "ALLOW_ORIGIN", "text": ALLOW_ORIGIN},
            {"type": "plain_text", "name": "REPO", "text": REPO},
        ],
    }
    body, ct = multipart([
        ("metadata", None, "application/json", json.dumps(meta).encode()),
        ("index.js", "index.js", "application/javascript+module", code.encode()),
    ])
    st, d = cf_req(token, "PUT", "/accounts/%s/workers/scripts/%s" % (acc, WORKER_NAME),
                   raw=body, content_type=ct)
    assert st in (200, 201) and d.get("success"), "上传失败(%s)：%s" % (st, json.dumps(d)[:400])
    print("[3/5] Worker 脚本已上传（含 GH_TOKEN/ADMIN_KEY 密钥绑定）")

    # ---- 4. 启用 workers.dev 访问 ----
    if sub:
        st, d = cf_req(token, "POST", "/accounts/%s/workers/scripts/%s/subdomain" % (acc, WORKER_NAME),
                       data={"enabled": True, "previews_enabled": True})
        print("[4/5] workers.dev 路由：%s" % ("已启用" if st in (200, 201) else "开启失败 %s %s" % (st, json.dumps(d)[:200])))
    else:
        print("[4/5] 跳过（账户未启用 workers.dev 子域，可在面板 Workers & Pages 里开启）")

    # ---- 5. 冒烟测试 ----
    url = "https://%s.%s.workers.dev" % (WORKER_NAME, sub) if sub else "(未知)"
    if sub:
        req = urllib.request.Request(url, data=json.dumps({
            "action": "ping", "admin_key": admin_key}).encode(),
            headers={"Content-Type": "application/json",
                     # 注意：Cloudflare 浏览器完整性检查(Bot Fight)会拦 Python 默认 UA（error 1010）
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                     "Origin": ALLOW_ORIGIN}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                ok = json.loads(r.read().decode()).get("ok") is True
            print("[5/5] 冒烟测试 ping：%s" % ("PASS ✓" if ok else "返回异常"))
        except Exception as e:
            print("[5/5] 冒烟测试网络异常（刚部署可能需几秒生效）：%r" % e)
    print("\n✅ 部署完成！Worker 地址：%s" % url)
    print("下一步：打开站点 → 管理面板 → 「⚙ 免令牌模式」→ 填入上面地址 → 管理密钥 → 测试连接。")


if __name__ == "__main__":
    main()
