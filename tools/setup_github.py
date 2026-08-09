"""一键上云：用你提供的 GitHub Personal Access Token 自动完成全部部署。

仅在“第一次搭建”时由助手运行一次。完成后你可以（也应该）立即撤销该令牌：
  GitHub → Settings → Developer settings → Personal access tokens → revoke。
之后每天的自动运行由 GitHub 自带临时令牌驱动，与该 PAT 无关。

流程：
  1. 校验令牌，取得用户名
  2. 创建公开仓库 stock-analysis（已存在则跳过）
  3. git 推送当前代码（密钥文件已被 .gitignore 排除，不会进仓库）
  4. 设置仓库 Secrets：NOTIFY_JSON（读本地 config/notify.json）、
     ALLOWED_USERS_JSON（自动生成一个 owner 账户 + 随机强口令）
  5. 在 tag=data-snapshot 的 Release 上传 market.db.gz 与 state.tar.gz
  6. 开启 GitHub Pages（GitHub Actions 作为源）
  7. 触发首次 build

依赖：PyNaCl（用于按 GitHub 要求用仓库公钥密封加密 Secret）。
      脚本会自动 pip install pynacl（仅首次）。
"""
import sys
import os
import io
import json
import base64
import secrets
import string
import subprocess
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE_DEFAULT = os.path.join(ROOT, "..", ".ghtoken")  # 仓库外的保管位置
REPO = "stock-analysis"
API = "https://api.github.com"


def log(m):
    print("[setup] " + m)


def ensure_pynacl():
    try:
        import nacl  # noqa
        return
    except Exception:
        log("首次运行需要 PyNaCl，正在安装（仅此一次）…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pynacl"])


def api(method, path, token, data=None, is_json=True, binary=None):
    url = API + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "stock-setup")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode("utf-8")
        req.data = body
    elif binary is not None:
        req.add_header("Content-Type", "application/gzip")
        req.data = binary
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, (r.read().decode("utf-8", "replace") if not binary else b"")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get_user(token):
    st, body = api("GET", "/user", token)
    if st != 200:
        raise SystemExit("令牌校验失败（HTTP %d）：%s" % (st, body[:200]))
    return json.loads(body)["login"]


def create_repo(token, owner):
    st, body = api("POST", "/user/repos", token, {
        "name": REPO, "private": False, "auto_init": False,
        "description": "A股盘后复盘分析（多源交叉校验 + 加密口令访问）",
    })
    if st in (201, 200):
        log("仓库已创建：https://github.com/%s/%s" % (owner, REPO))
        return
    if st == 422:
        log("仓库已存在，跳过创建。")
        return
    raise SystemExit("创建仓库失败（HTTP %d）：%s" % (st, body[:200]))


def set_secret(token, owner, name, value):
    st, body = api("GET", "/repos/%s/%s/actions/secrets/public-key" % (owner, REPO), token)
    if st != 200:
        raise SystemExit("获取仓库公钥失败（HTTP %d）：%s" % (st, body[:200]))
    pk = json.loads(body)
    from nacl.public import PublicKey, SealedBox
    box = SealedBox(PublicKey(base64.b64decode(pk["key"])))
    enc = base64.b64encode(box.encrypt(value.encode("utf-8"))).decode("ascii")
    st, body = api("PUT", "/repos/%s/%s/actions/secrets/%s" % (owner, REPO, name),
                   token, {"encrypted_value": enc, "key_id": pk["key_id"]})
    if st in (201, 204):
        log("Secret %s 已设置。" % name)
    else:
        raise SystemExit("设置 Secret %s 失败（HTTP %d）：%s" % (name, st, body[:200]))


def push_code(token, owner):
    url = "https://%s@github.com/%s/%s.git" % (token, owner, REPO)
    bare = "https://github.com/%s/%s.git" % (owner, REPO)
    run = lambda cmd: subprocess.run(cmd, cwd=ROOT, shell=True, capture_output=True,
                                      encoding="utf-8", errors="replace")
    if not os.path.exists(os.path.join(ROOT, ".git")):
        run("git init -b main")
    run('git config user.email "setup@stock.local"')
    run('git config user.name "stock-setup"')
    run("git remote remove origin 2>/dev/null || true")
    run("git remote add origin %s" % url)
    r = run("git add -A && git commit -q -m \"init: A股盘后分析（多源校验 + 加密访问）\" || true")
    log("commit 输出：%s" % (r.stderr.strip() or r.stdout.strip() or "(无变更)"))
    run("git config --global http.sslBackend openssl")
    r = run("git -c http.sslBackend=openssl push -u origin main")
    log("push 输出：%s" % ((r.stdout or "") .strip() or (r.stderr or "").strip() or "(无输出)"))
    if r.returncode != 0:
        raise SystemExit("代码推送失败，请检查上方输出。")
    # 立即剥离令牌，避免遗留在本地 remote URL
    run("git remote set-url origin %s" % bare)
    log("远程地址中的令牌已剥离。")


def get_release_id(token, owner):
    st, body = api("GET", "/repos/%s/%s/releases/tags/%s" % (owner, REPO, "data-snapshot"), token)
    if st == 200:
        return json.loads(body)["id"]
    return None


def create_release_and_upload(token, owner):
    rid = get_release_id(token, owner)
    if rid is None:
        st, body = api("POST", "/repos/%s/%s/releases" % (owner, REPO), token, {
            "tag_name": "data-snapshot", "name": "数据快照",
            "body": "数据库与状态快照，由流水线自动更新。请勿手动修改。",
        })
        if st != 201:
            raise SystemExit("创建 Release 失败（HTTP %d）：%s" % (st, body[:200]))
        rid = json.loads(body)["id"]
        log("Release data-snapshot 已创建。")
    else:
        log("Release data-snapshot 已存在，复用。")

    for fname in ("market.db.gz", "state.tar.gz"):
        fpath = os.path.join(ROOT, "cache", fname)
        if not os.path.exists(fpath):
            log("⚠ 找不到 %s，跳过上传（请先在本机运行 update.bat 生成）。" % fname)
            continue
        # 若已存在同名资产先删后传
        lst, _ = api("GET", "/repos/%s/%s/releases/%s/assets" % (owner, REPO, rid), token)
        try:
            for a in json.loads(lst):
                if a["name"] == fname:
                    api("DELETE", "/repos/%s/%s/releases/assets/%s" % (owner, REPO, a["id"]), token)
        except Exception:
            pass
        with open(fpath, "rb") as f:
            data = f.read()
        st, body = api("POST",
                       "/repos/%s/%s/releases/%s/assets?name=%s" % (owner, REPO, rid, fname),
                       token, binary=data)
        if st in (201, 200):
            log("✅ 已上传 %s（%.1f MB）" % (fname, len(data) / 1e6))
        else:
            log("⚠ 上传 %s 失败（HTTP %d）：%s" % (fname, st, body[:160]))


def enable_pages(token, owner):
    for method, payload in (("POST", {"build_type": "workflow"}),
                            ("PATCH", {"build_type": "workflow"})):
        st, body = api("POST" if method == "POST" else "PATCH",
                       "/repos/%s/%s/pages" % (owner, REPO), token,
                       {"build_type": "workflow"})
        if st in (201, 204, 200):
            log("✅ GitHub Pages 已开启（源：GitHub Actions）。")
            return
    # API 失败时给出人工兜底（Settings 里点一下即可）
    log("⚠ 自动开启 Pages 失败，请手动操作一次：")
    log("   Settings → Pages → Build and deployment → Source 选 “GitHub Actions”。")
    log("   完成后回来告诉我，我触发首次 build。")


def dispatch_build(token, owner):
    st, body = api("POST", "/repos/%s/%s/actions/workflows/stock.yml/dispatches" % (owner, REPO),
                   token, {"ref": "main", "inputs": {"task": "build"}})
    if st in (204, 202, 200):
        log("✅ 已触发首次 build（task=build）。可在 Actions 标签页查看进度。")
    else:
        log("⚠ 触发 build 失败（HTTP %d）：%s" % (st, body[:200]))


def gen_owner_user():
    """生成 owner 账户（随机强口令），写入本地 config（gitignore 已排除）并作为 Secret。"""
    alphabet = string.ascii_letters + string.digits
    pw = "".join(secrets.choice(alphabet) for _ in range(16))
    cfg = {"users": [{"id": "owner", "name": "我（管理员）", "pass": pw}]}
    p = os.path.join(ROOT, "config", "allowed_users.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    with open(os.path.join(ROOT, "config", "owner_pass.txt"), "w", encoding="utf-8") as f:
        f.write(pw)
    return json.dumps(cfg, ensure_ascii=False), pw


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", default=TOKEN_FILE_DEFAULT)
    ap.add_argument("--token", default="")
    ap.add_argument("--owner", default="")
    args = ap.parse_args()

    ensure_pynacl()
    tok = args.token.strip()
    if not tok and os.path.exists(args.token_file):
        tok = open(args.token_file, encoding="utf-8").read().strip()
    if not tok:
        raise SystemExit("未提供令牌：--token 或 --token-file")
    if tok.lower().startswith("ghp_") is False and not tok.startswith("github_pat_"):
        pass  # 不强制前缀，交给 API 校验

    owner = args.owner or get_user(tok)
    if not args.owner:
        log("已校验令牌，登录账户：%s" % owner)

    create_repo(tok, owner)

    # 生成 owner 账户 + 读取本地推送配置
    au_json, owner_pw = gen_owner_user()
    notify_path = os.path.join(ROOT, "config", "notify.json")
    if not os.path.exists(notify_path):
        raise SystemExit("找不到 config/notify.json（推送配置）。请先在本机配置。")
    notify_json = open(notify_path, encoding="utf-8").read().strip()

    push_code(tok, owner)
    set_secret(tok, owner, "NOTIFY_JSON", notify_json)
    set_secret(tok, owner, "ALLOWED_USERS_JSON", au_json)
    create_release_and_upload(tok, owner)
    enable_pages(tok, owner)
    dispatch_build(tok, owner)

    print("\n" + "=" * 60)
    print("部署已发起 🎉")
    print("仓库：  https://github.com/%s/%s" % (owner, REPO))
    print("站点：  https://%s.github.io/%s/  （首次构建约 1-3 分钟）" % (owner, REPO))
    print("登录：  账户 owner  /  口令 %s" % owner_pw)
    print("提示：  拿到站点链接后，建议立即撤销本令牌（Settings → Developer settings → revoke）。")
    print("增删人员：告诉我“加/删某人”，我会更新 ALLOWED_USERS_JSON 密钥并触发重建。")
    print("=" * 60)


if __name__ == "__main__":
    main()
