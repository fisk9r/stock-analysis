"""查看云端部署现状：Release 附件 / Pages / 最近一次 Actions 运行。

一键部署跑完后想确认"到底成没成"，跑这个就够了：
    python tools/gh_status.py
"""
import os
import json
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(ROOT, "..", ".ghtoken")
REPO = "stock-analysis"


def get(path, token, owner):
    url = "https://api.github.com" + path.replace("{o}", owner).replace("{r}", REPO)
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "stock-status")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return {"_err": "%s: %s" % (type(e).__name__, e)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", default=TOKEN_FILE)
    ap.add_argument("--owner", default="")
    a = ap.parse_args()
    token = open(a.token_file, encoding="utf-8").read().strip()
    owner = a.owner or get("/user", token, "x").get("login", "")
    print("账户：%s" % owner)

    rel = get("/repos/{o}/{r}/releases/tags/data-snapshot", token, owner)
    if "id" in rel:
        print("\n[Release data-snapshot]")
        if not rel.get("assets"):
            print("  （空，尚无附件）")
        for x in rel["assets"]:
            print("  %-16s %8.1f MB  %s" % (x["name"], x["size"] / 1e6, x["state"]))
    else:
        print("\n[Release] 未找到：%s" % rel)

    pg = get("/repos/{o}/{r}/pages", token, owner)
    print("\n[Pages] %s" % (
        "已开启，源=%s，地址=%s" % (pg.get("build_type"), pg.get("html_url"))
        if "html_url" in pg else pg))

    runs = get("/repos/{o}/{r}/actions/runs?per_page=3", token, owner)
    print("\n[最近的 Actions 运行]")
    for w in (runs.get("workflow_runs") or [])[:3]:
        print("  #%s %-12s %-12s %s" % (
            w["run_number"], w["status"], w.get("conclusion") or "-", w["created_at"]))
        print("     %s" % w["html_url"])
    if not (runs.get("workflow_runs") or []):
        print("  （还没有运行记录）%s" % (runs if "_err" in runs or "_http" in runs else ""))


if __name__ == "__main__":
    main()
