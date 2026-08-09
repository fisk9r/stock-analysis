"""管理谁能看这个站：增删人员 → 更新 GitHub 密钥 → 触发重建。

    python tools/manage_users.py list
    python tools/manage_users.py add --name 张三
    python tools/manage_users.py add --name 李四 --pass 我自己定的口令
    python tools/manage_users.py remove --id friend1
    python tools/manage_users.py passwd --id owner            # 换口令
    python tools/manage_users.py sync                         # 只同步不改人

改完会自动触发一次重建（约 2 分钟），期间旧口令仍可用；重建完成后：
  · 新增的人 → 用新口令即可进
  · 删掉的人 → 他那份密文文件从站点消失，旧链接再也解不开

口令明文只存在本机 config/allowed_users.json（已被 .gitignore 排除）
和 GitHub 加密密钥里，公网上永远只有密文。
"""
import os
import sys
import json
import string
import secrets
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup_github import (  # noqa: E402
    ROOT, REPO, ensure_pynacl, get_user, set_secret, dispatch_build, log,
    TOKEN_FILE_DEFAULT,
)

CFG = os.path.join(ROOT, "config", "allowed_users.json")


def load():
    if not os.path.exists(CFG):
        return {"users": []}
    return json.load(open(CFG, encoding="utf-8"))


def save(cfg):
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def gen_pass(n=12):
    # 去掉容易看错的 0/O/1/l/I，方便口头或微信转达
    alphabet = "".join(c for c in (string.ascii_letters + string.digits)
                       if c not in "0O1lI")
    return "".join(secrets.choice(alphabet) for _ in range(n))


def next_id(cfg, want=""):
    used = {u["id"] for u in cfg["users"]}
    if want:
        if want in used:
            raise SystemExit("id 已存在：%s" % want)
        if not all(c.isalnum() or c in "_-" for c in want):
            raise SystemExit("id 只能用字母数字和 _-")
        return want
    i = 1
    while ("friend%d" % i) in used:
        i += 1
    return "friend%d" % i


def show(cfg):
    print("\n当前可访问人员（共 %d 人）：" % len(cfg["users"]))
    print("-" * 52)
    for u in cfg["users"]:
        print("  %-10s %-14s 口令 %s" % (u["id"], u.get("name", ""), u.get("pass", "")))
    print("-" * 52)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["list", "add", "remove", "passwd", "sync"])
    ap.add_argument("--name", default="")
    ap.add_argument("--id", default="")
    ap.add_argument("--pass", dest="pw", default="")
    ap.add_argument("--token-file", default=TOKEN_FILE_DEFAULT)
    ap.add_argument("--no-rebuild", action="store_true", help="只改密钥，不触发重建")
    a = ap.parse_args()

    cfg = load()
    changed = False

    if a.action == "list":
        show(cfg)
        return 0

    if a.action == "add":
        if not a.name:
            raise SystemExit("请用 --name 指定这个人的显示名，例如 --name 张三")
        uid = next_id(cfg, a.id)
        pw = a.pw or gen_pass()
        cfg["users"].append({"id": uid, "name": a.name, "pass": pw})
        changed = True
        log("已添加 %s（id=%s），口令：%s" % (a.name, uid, pw))

    elif a.action == "remove":
        if not a.id:
            raise SystemExit("请用 --id 指定要删除的人（先跑 list 查看）")
        before = len(cfg["users"])
        cfg["users"] = [u for u in cfg["users"] if u["id"] != a.id]
        if len(cfg["users"]) == before:
            raise SystemExit("没找到 id=%s" % a.id)
        if not cfg["users"]:
            raise SystemExit("不能删光所有人，否则站点将无任何可解密数据。")
        changed = True
        log("已删除 id=%s" % a.id)

    elif a.action == "passwd":
        if not a.id:
            raise SystemExit("请用 --id 指定要改口令的人")
        hit = [u for u in cfg["users"] if u["id"] == a.id]
        if not hit:
            raise SystemExit("没找到 id=%s" % a.id)
        pw = a.pw or gen_pass()
        hit[0]["pass"] = pw
        changed = True
        log("已为 %s 设置新口令：%s" % (a.id, pw))

    if changed:
        save(cfg)
        # owner 的口令单独存一份，方便自己找回
        for u in cfg["users"]:
            if u["id"] == "owner":
                with open(os.path.join(ROOT, "config", "owner_pass.txt"), "w",
                          encoding="utf-8") as f:
                    f.write(u["pass"])

    if not cfg["users"]:
        raise SystemExit("人员列表为空，已中止（站点需要至少一个人）。")

    ensure_pynacl()
    if not os.path.exists(a.token_file):
        raise SystemExit(
            "找不到令牌文件 %s。\n"
            "本地改动已保存，但没能同步到云端。放好令牌后重跑：\n"
            "  python tools/manage_users.py sync" % a.token_file)
    tok = open(a.token_file, encoding="utf-8").read().strip()
    owner = get_user(tok)
    set_secret(tok, owner, "ALLOWED_USERS_JSON", json.dumps(cfg, ensure_ascii=False))

    if not a.no_rebuild:
        dispatch_build(tok, owner)
        log("重建约需 2 分钟。可用 python tools/gh_watch.py 盯进度。")

    show(cfg)
    print("站点：https://%s.github.io/%s/" % (owner, REPO))
    return 0


if __name__ == "__main__":
    sys.exit(main())
