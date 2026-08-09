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
# 上传 Release 附件必须走这个域名，api.github.com 不接受附件体
UPLOADS = "https://uploads.github.com"


def log(m):
    print("[setup] " + m)


def ensure_pynacl():
    try:
        import nacl  # noqa
        return
    except Exception:
        log("首次运行需要 PyNaCl，正在安装（仅此一次）…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pynacl"])


def api(method, path, token, data=None, is_json=True, binary=None,
        timeout=60, base=None):
    url = (base or API) + path
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
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Content-Length", str(len(binary)))
        req.data = binary
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
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


def list_assets(token, owner, rid):
    st, body = api("GET", "/repos/%s/%s/releases/%s/assets?per_page=100" % (owner, REPO, rid), token)
    try:
        return json.loads(body)
    except Exception:
        return []


def del_asset(token, owner, aid):
    api("DELETE", "/repos/%s/%s/releases/assets/%s" % (owner, REPO, aid), token)


def put_asset(token, owner, rid, name, blob, tries=3):
    """上传单个附件，失败自动清残留再重试。返回 True/False。"""
    for attempt in range(1, tries + 1):
        log("  上传 %s（%.1f MB）尝试 %d/%d…" % (name, len(blob) / 1e6, attempt, tries))
        try:
            st, body = api("POST",
                           "/repos/%s/%s/releases/%s/assets?name=%s" % (owner, REPO, rid, name),
                           token, binary=blob, timeout=900, base=UPLOADS)
        except Exception as e:
            st, body = 0, "%s: %s" % (type(e).__name__, e)
        if st in (200, 201):
            log("  ✅ %s 完成" % name)
            return True
        log("  ⚠ 失败（HTTP %s）：%s" % (st, str(body)[:140]))
        for a in list_assets(token, owner, rid):
            if a["name"] == name:
                del_asset(token, owner, a["id"])
    return False


def upload_chunked(token, owner, rid, fpath, base_name, chunk_mb=6):
    """把大文件切片上传（每片单独重试）。云端 restore 时会自动拼回。

    单文件直传在国内网络下经常半路断，切片后每片只有几 MB，
    断了也只重传那一片，不用从头再来。
    """
    size = os.path.getsize(fpath)
    step = chunk_mb * 1024 * 1024
    total = (size + step - 1) // step
    log("改用分片上传：%s → %d 片（每片约 %d MB）" % (base_name, total, chunk_mb))

    have = {a["name"]: a for a in list_assets(token, owner, rid)}
    # 清掉单文件残留，避免云端 restore 时误取到半截文件
    if base_name in have:
        del_asset(token, owner, have[base_name]["id"])

    with open(fpath, "rb") as f:
        for i in range(total):
            blob = f.read(step)
            name = "%s.part%02d" % (base_name, i)
            old = have.get(name)
            if old is not None and old.get("size") == len(blob) and old.get("state") == "uploaded":
                log("  ↩ %s 已存在且大小一致，跳过" % name)
                continue
            if old is not None:
                del_asset(token, owner, old["id"])
            if not put_asset(token, owner, rid, name, blob):
                raise SystemExit("分片 %s 上传失败，请稍后重跑脚本（已传的片会跳过）。" % name)

    # 清理多余的旧分片（上次文件更大时留下的）
    for a in list_assets(token, owner, rid):
        n = a["name"]
        if n.startswith(base_name + ".part"):
            try:
                idx = int(n.rsplit("part", 1)[1])
            except Exception:
                continue
            if idx >= total:
                del_asset(token, owner, a["id"])
                log("  清理多余分片 %s" % n)

    # 记录清单，云端据此校验拼接结果
    manifest = json.dumps({"file": base_name, "parts": total, "size": size}).encode("utf-8")
    for a in list_assets(token, owner, rid):
        if a["name"] == base_name + ".manifest.json":
            del_asset(token, owner, a["id"])
    put_asset(token, owner, rid, base_name + ".manifest.json", manifest)
    log("✅ %s 分片上传完成（%d 片 / %.1f MB）" % (base_name, total, size / 1e6))


def create_release_and_upload(token, owner, chunk_mb=0):
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
        size = os.path.getsize(fpath)
        have = {a["name"]: a for a in list_assets(token, owner, rid)}

        # 已完整存在就跳过——脚本可反复运行，不重传
        cur = have.get(fname)
        if cur is not None and cur.get("size") == size and cur.get("state") == "uploaded":
            log("↩ %s 已在 Release 上且大小一致（%.1f MB），跳过。" % (fname, size / 1e6))
            continue
        # 分片版是否已完整？（上次走了分片路径）
        man = have.get(fname + ".manifest.json")
        if man is not None and chunk_mb <= 0:
            log("↩ %s 已有分片版本，跳过（云端会自动拼接）。" % fname)
            continue

        # 小文件（state.tar.gz 几十 KB）永远直传；大文件按开关决定
        if chunk_mb > 0 and size > chunk_mb * 1024 * 1024:
            upload_chunked(token, owner, rid, fpath, fname, chunk_mb)
            continue

        if cur is not None:
            del_asset(token, owner, cur["id"])
            log("已清除残留的 %s（上次传了一半）。" % fname)
        with open(fpath, "rb") as f:
            data = f.read()
        log("上传 %s（%.1f MB），大文件请耐心等待…" % (fname, size / 1e6))
        if not put_asset(token, owner, rid, fname, data):
            raise SystemExit(
                "上传 %s 连续失败，多半是本机到 GitHub 的网络不稳。\n"
                "改用分片重传：python tools/setup_github.py --chunk-mb 6\n"
                "（脚本可重复运行，已完成的步骤会自动跳过）" % fname)


def enable_pages(token, owner):
    # 先 POST 创建；若已存在（409）再 PUT 改成 workflow 源
    for method in ("POST", "PUT"):
        st, body = api(method, "/repos/%s/%s/pages" % (owner, REPO), token,
                       {"build_type": "workflow"})
        if st in (201, 204, 200):
            log("✅ GitHub Pages 已开启（源：GitHub Actions）。")
            return
        log("Pages %s → HTTP %d %s" % (method, st, str(body)[:120]))
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
    """生成 owner 账户（随机强口令），写入本地 config（gitignore 已排除）并作为 Secret。

    重复运行时复用已有口令——否则每跑一次脚本口令就变，你刚记下的就失效了。
    """
    pw_file = os.path.join(ROOT, "config", "owner_pass.txt")
    if os.path.exists(pw_file):
        pw = open(pw_file, encoding="utf-8").read().strip()
        log("复用已生成的管理员口令（config/owner_pass.txt）。")
    else:
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
    ap.add_argument("--chunk-mb", type=int, default=0,
                    help="大文件切片上传的每片 MB 数（网络不稳时用，建议 6）")
    ap.add_argument("--only", default="",
                    help="只跑某一步：upload / pages / build")
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

    # --only 用于中途某步失败后单独重跑，不必从头再来
    if args.only == "upload":
        create_release_and_upload(tok, owner, args.chunk_mb)
        return
    if args.only == "pages":
        enable_pages(tok, owner)
        return
    if args.only == "build":
        dispatch_build(tok, owner)
        return

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
    create_release_and_upload(tok, owner, args.chunk_mb)
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
