"""GitHub 运维助手（零依赖，仅用标准库 + pynacl）。

封装项目在「git 无法直连 github.com」时的可用操作：
  - 设置/更新仓库 Secret（按 GitHub 要求用仓库公钥 NaCl 密封）
  - 通过 Git Data API 把本地文件推送到默认分支（绕过 git 传输层）
  - 触发 workflow_dispatch、查询最近的运行

用法：
  python tools/gh_api.py set-secret NOTIFY_JSON config/notify.json
  python tools/gh_api.py set-secret ALLOWED_USERS_JSON config/allowed_users.json
  python tools/gh_api.py dispatch build
  python tools/gh_api.py push "chore: 同步" dist/app.js dist/styles.css
  python tools/gh_api.py runs
"""
import sys
import os
import json
import base64
import argparse

import urllib.request
import urllib.error
import ssl
import time

# 弱网/双栈环境下 urllib 常因 IPv6 黑洞或 IPv4 节点抖动而连接超时。
# 强制所有 DNS 解析只返回 IPv4 地址（与能通的 curl -4 行为一致），提升推送/触发稳定性。
import socket
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_getaddrinfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(ROOT, "..", ".ghtoken")
REPO = "stock-analysis"
OWNER = "fisk9r"
API = "https://api.github.com"


def _token():
    p = TOKEN_FILE
    if not os.path.exists(p):
        raise SystemExit("找不到令牌文件：%s（请先放回 .ghtoken）" % p)
    return open(p, encoding="utf-8").read().strip()


def _ssl_ctx(verify=True):
    """本机出口有 MITM 代理（HTTPS_PROXY=127.0.0.1:10808）时会用自己的 CA 重签证书，
    urllib 严格校验会直接 SSL 握手失败（curl 需 -k 才通）。这里做一次探测：
    严格校验失败则自动降级为不校验，只影响本机脚本、不降低仓库安全性。"""
    if not verify:
        return ssl._create_unverified_context()
    return None


_VERIFY = os.environ.get("GH_API_VERIFY", "1") != "0"


def api(method, path, data=None, binary=None, base=API, timeout=120):
    url = base + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Bearer " + _token())
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "stock-gh-api")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode("utf-8")
    elif binary is not None:
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Content-Length", str(len(binary)))
        req.data = binary
    last_err = None
    _ctx = None if _VERIFY else ssl._create_unverified_context()
    for _attempt in range(3):
        try:
            r = urllib.request.urlopen(req, timeout=timeout, context=_ctx)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            # MITM 代理导致证书校验失败（CERTIFICATE_VERIFY_FAILED）→ 降级重试一次
            if (_ctx is None and "CERTIFICATE_VERIFY_FAILED" in str(e)
                    and "SSL" in str(type(e))):
                _ctx = ssl._create_unverified_context()
                last_err = e
                continue
        except urllib.error.HTTPError as e:
            code = e.code
            # 5xx 为服务端瞬时故障（如 GitHub 503 No server available），应重试而非立即失败
            if code in (500, 502, 503, 504) and _attempt < 2:
                last_err = e
                time.sleep(2 * (_attempt + 1))
                continue
            return code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if _attempt < 2:
                time.sleep(2 * (_attempt + 1))
                continue
    if last_err is not None:
        raise last_err
    raise SystemExit("API 请求失败：3 次重试均未成功且无明确异常（网络/代理问题）")


# ----------------------------- Secrets -----------------------------
def set_secret(name, value):
    st, body = api("GET", "/repos/%s/%s/actions/secrets/public-key" % (OWNER, REPO))
    if st != 200:
        raise SystemExit("获取仓库公钥失败（HTTP %d）：%s" % (st, body[:200]))
    pk = json.loads(body)
    from nacl.public import PublicKey, SealedBox
    box = SealedBox(PublicKey(base64.b64decode(pk["key"])))
    enc = base64.b64encode(box.encrypt(value.encode("utf-8"))).decode("ascii")
    st, body = api("PUT", "/repos/%s/%s/actions/secrets/%s" % (OWNER, REPO, name),
                   {"encrypted_value": enc, "key_id": pk["key_id"]})
    if st in (201, 204):
        print("✅ Secret %s 已更新" % name)
    else:
        raise SystemExit("设置 Secret %s 失败（HTTP %d）：%s" % (name, st, body[:200]))


# ----------------------------- Push -----------------------------
def _blob(content_bytes):
    st, body = api("POST", "/repos/%s/%s/git/blobs" % (OWNER, REPO),
                   {"content": base64.b64encode(content_bytes).decode("ascii"),
                    "encoding": "base64"})
    if st != 201:
        raise SystemExit("创建 blob 失败（HTTP %d）：%s" % (st, body[:200]))
    return json.loads(body)["sha"]


def push_files(message, paths, branch="main"):
    # 1. 当前分支头
    st, body = api("GET", "/repos/%s/%s/git/ref/heads/%s" % (OWNER, REPO, branch))
    if st != 200:
        raise SystemExit("读取分支引用失败（HTTP %d）：%s" % (st, body[:200]))
    base_sha = json.loads(body)["object"]["sha"]
    # 2. 基础树
    st, body = api("GET", "/repos/%s/%s/git/commits/%s" % (OWNER, REPO, base_sha))
    base_tree = json.loads(body)["tree"]["sha"]
    # 3. 逐文件建 blob + 树节点
    tree = []
    for rel in paths:
        ab = os.path.join(ROOT, rel)
        if not os.path.isfile(ab):
            print("⚠ 跳过不存在的文件：%s" % rel)
            continue
        # Git 树路径必须用正斜杠。Windows 上 glob/os.path 会产生反斜杠，
        # 若原样提交，Git 会把 "pipeline\x.py" 当作仓库根目录下一个「文件名含反斜杠」
        # 的文件，真正的 pipeline/x.py 不会被更新（历史踩坑，务必保留此规范化）。
        # 注意：不能用 lstrip("./") 去前导 "./" —— lstrip 按字符集合剥离，
        # 会把 ".github/..." 的开头的 "." 一并剥掉，导致工作流被写到
        # github/workflows/... 垃圾路径、真正的 .github/workflows/x.yml
        # 永远不更新（2026-08-27 踩坑）。只在前缀确实是 "./" 时才去掉。
        gitpath = rel.replace("\\", "/")
        while gitpath.startswith("./"):
            gitpath = gitpath[2:]
        gitpath = gitpath.lstrip("/")
        with open(ab, "rb") as f:
            content = f.read()
        sha = _blob(content)
        tree.append({"path": gitpath, "mode": "100644", "type": "blob", "sha": sha})
    if not tree:
        raise SystemExit("没有可推送的文件")
    # 4. 新树（以旧树为基底，覆盖同名文件）
    st, body = api("POST", "/repos/%s/%s/git/trees" % (OWNER, REPO),
                   {"base_tree": base_tree, "tree": tree})
    if st != 201:
        raise SystemExit("创建树失败（HTTP %d）：%s" % (st, body[:200]))
    new_tree = json.loads(body)["sha"]
    # 5. 新提交
    st, body = api("POST", "/repos/%s/%s/git/commits" % (OWNER, REPO),
                   {"message": message, "tree": new_tree, "parents": [base_sha]})
    if st != 201:
        raise SystemExit("创建提交失败（HTTP %d）：%s" % (st, body[:200]))
    new_commit = json.loads(body)["sha"]
    # 6. 更新引用
    st, body = api("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (OWNER, REPO, branch),
                   {"sha": new_commit, "force": False})
    if st not in (200, 201):
        raise SystemExit("更新引用失败（HTTP %d）：%s" % (st, body[:200]))
    print("✅ 已推送 %d 个文件 -> %s（%s）" % (len(tree), new_commit[:10], message))


# ----------------------------- Delete -----------------------------
def delete_files(message, paths, branch="main"):
    st, body = api("GET", "/repos/%s/%s/git/ref/heads/%s" % (OWNER, REPO, branch))
    if st != 200:
        raise SystemExit("读取分支引用失败（HTTP %d）：%s" % (st, body[:200]))
    base_sha = json.loads(body)["object"]["sha"]
    st, body = api("GET", "/repos/%s/%s/git/commits/%s" % (OWNER, REPO, base_sha))
    base_tree = json.loads(body)["tree"]["sha"]
    # sha 为 null 表示删除该路径（GitHub Git Data API 约定）
    # 注意：删除时不做 \\ -> / 规范化，因为仓库里可能真的存在「名字含反斜杠」的
    # 历史垃圾文件（Windows 误推产物），需要按原样精确删除。
    tree = [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in paths]
    st, body = api("POST", "/repos/%s/%s/git/trees" % (OWNER, REPO),
                   {"base_tree": base_tree, "tree": tree})
    if st != 201:
        raise SystemExit("创建树失败（HTTP %d）：%s" % (st, body[:200]))
    new_tree = json.loads(body)["sha"]
    st, body = api("POST", "/repos/%s/%s/git/commits" % (OWNER, REPO),
                   {"message": message, "tree": new_tree, "parents": [base_sha]})
    if st != 201:
        raise SystemExit("创建提交失败（HTTP %d）：%s" % (st, body[:200]))
    new_commit = json.loads(body)["sha"]
    st, body = api("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (OWNER, REPO, branch),
                   {"sha": new_commit, "force": False})
    if st not in (200, 201):
        raise SystemExit("更新引用失败（HTTP %d）：%s" % (st, body[:200]))
    print("✅ 已从仓库删除 %d 个文件 -> %s（%s）" % (len(tree), new_commit[:10], message))


# ----------------------------- Workflow -----------------------------
def dispatch(task="build"):
    # 取 workflow 文件名（stock.yml）
    st, body = api("GET", "/repos/%s/%s/actions/workflows" % (OWNER, REPO))
    wf = None
    for w in json.loads(body).get("workflows", []):
        if w["path"].endswith("stock.yml"):
            wf = w["id"]
    if not wf:
        raise SystemExit("找不到 stock.yml 工作流")
    st, body = api("POST", "/repos/%s/%s/actions/workflows/%s/dispatches" % (OWNER, REPO, wf),
                   {"ref": "main", "inputs": {"task": task}})
    if st in (204, 202):
        print("✅ 已触发 workflow_dispatch：task=%s" % task)
    else:
        raise SystemExit("触发失败（HTTP %d）：%s" % (st, body[:200]))


def runs(per_page=10):
    st, body = api("GET", "/repos/%s/%s/actions/runs?per_page=%d" % (OWNER, REPO, per_page))
    for r in json.loads(body).get("workflow_runs", []):
        print("%s  %-18s %-10s %s" % (r["id"], r["event"], r["status"], r.get("created_at")))
    if st != 200:
        print("查询失败（HTTP %d）" % st)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("set-secret"); s.add_argument("name"); s.add_argument("file")
    s = sub.add_parser("push"); s.add_argument("message"); s.add_argument("files", nargs="+")
    s = sub.add_parser("rm"); s.add_argument("message"); s.add_argument("files", nargs="+")
    s = sub.add_parser("dispatch"); s.add_argument("task", nargs="?", default="build")
    s = sub.add_parser("runs")
    args = ap.parse_args()
    if args.cmd == "set-secret":
        set_secret(args.name, open(os.path.join(ROOT, args.file), encoding="utf-8").read())
    elif args.cmd == "push":
        push_files(args.message, args.files)
    elif args.cmd == "rm":
        delete_files(args.message, args.files)
    elif args.cmd == "dispatch":
        dispatch(args.task)
    elif args.cmd == "runs":
        runs()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
