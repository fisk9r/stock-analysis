"""一次性修复：把 pipeline/*.py 用正确的正斜杠路径推上去，同时删除仓库根下
因 Windows 反斜杠误推产生的「pipeline\\xxx.py」垃圾文件。

背景（务必牢记）：Windows 上 glob.glob("pipeline/*.py") 返回 "pipeline\\x.py"，
若直接作为 Git 树 path 提交，Git 会当成仓库根目录下一个文件名含反斜杠的文件，
真正的 pipeline/x.py 不会被更新 —— 表现为「推送成功但线上没变」。

用法：python tools/fix_backslash_push.py
"""
import base64
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gh_api  # noqa: E402

OWNER, REPO, BRANCH = gh_api.OWNER, gh_api.REPO, "main"
ROOT = gh_api.ROOT


def main():
    # 1. 当前分支头 + 基础树
    st, body = gh_api.api("GET", "/repos/%s/%s/git/ref/heads/%s" % (OWNER, REPO, BRANCH))
    assert st == 200, body[:300]
    base_sha = json.loads(body)["object"]["sha"]
    st, body = gh_api.api("GET", "/repos/%s/%s/git/commits/%s" % (OWNER, REPO, base_sha))
    base_tree = json.loads(body)["tree"]["sha"]
    print("base commit:", base_sha[:12])

    # 2. 找出仓库里所有「路径含反斜杠」的垃圾 blob
    st, body = gh_api.api(
        "GET", "/repos/%s/%s/git/trees/%s?recursive=1" % (OWNER, REPO, base_tree))
    remote = json.loads(body)["tree"]
    garbage = [e["path"] for e in remote if e["type"] == "blob" and "\\" in e["path"]]
    print("发现反斜杠垃圾文件 %d 个" % len(garbage))

    tree = []

    # 3. 正确路径重推 pipeline/*.py（正斜杠）
    locals_ = sorted(glob.glob(os.path.join(ROOT, "pipeline", "*.py")))
    print("准备推送 pipeline 模块 %d 个" % len(locals_))
    for ab in locals_:
        gitpath = "pipeline/" + os.path.basename(ab)
        with open(ab, "rb") as f:
            content = f.read()
        st, b = gh_api.api("POST", "/repos/%s/%s/git/blobs" % (OWNER, REPO),
                           {"content": base64.b64encode(content).decode("ascii"),
                            "encoding": "base64"})
        if st != 201:
            raise SystemExit("blob 失败 %s: HTTP %d %s" % (gitpath, st, b[:200]))
        tree.append({"path": gitpath, "mode": "100644", "type": "blob",
                     "sha": json.loads(b)["sha"]})
        print("  blob ok:", gitpath)

    # 4. 顺带把修好的 gh_api.py 本身推上去
    ab = os.path.join(ROOT, "tools", "gh_api.py")
    with open(ab, "rb") as f:
        content = f.read()
    st, b = gh_api.api("POST", "/repos/%s/%s/git/blobs" % (OWNER, REPO),
                       {"content": base64.b64encode(content).decode("ascii"),
                        "encoding": "base64"})
    tree.append({"path": "tools/gh_api.py", "mode": "100644", "type": "blob",
                 "sha": json.loads(b)["sha"]})

    # 5. 删除垃圾文件（sha=None）
    for p in garbage:
        tree.append({"path": p, "mode": "100644", "type": "blob", "sha": None})

    # 6. 建树 / 提交 / 更新引用
    st, body = gh_api.api("POST", "/repos/%s/%s/git/trees" % (OWNER, REPO),
                          {"base_tree": base_tree, "tree": tree})
    if st != 201:
        raise SystemExit("建树失败 HTTP %d: %s" % (st, body[:300]))
    new_tree = json.loads(body)["sha"]

    msg = ("fix(push): 修正 Windows 反斜杠路径导致 pipeline 模块未真正入库；"
           "补齐缠论/席位/题材/连续信号/回测引擎并清理 %d 个垃圾文件" % len(garbage))
    st, body = gh_api.api("POST", "/repos/%s/%s/git/commits" % (OWNER, REPO),
                          {"message": msg, "tree": new_tree, "parents": [base_sha]})
    if st != 201:
        raise SystemExit("提交失败 HTTP %d: %s" % (st, body[:300]))
    new_commit = json.loads(body)["sha"]
    st, body = gh_api.api("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (OWNER, REPO, BRANCH),
                          {"sha": new_commit, "force": False})
    if st not in (200, 201):
        raise SystemExit("更新引用失败 HTTP %d: %s" % (st, body[:300]))
    print("✅ 已提交 %s：新增/更新 %d，删除 %d"
          % (new_commit[:12], len(tree) - len(garbage), len(garbage)))


if __name__ == "__main__":
    main()
