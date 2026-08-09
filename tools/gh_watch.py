"""盯住最近一次 Actions 运行，直到出结果；失败时打印是哪一步挂的。

用法：python tools/gh_watch.py [--minutes 25]
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(ROOT, "..", ".ghtoken")
REPO = "stock-analysis"


def get(path, token):
    req = urllib.request.Request("https://api.github.com" + path)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "stock-watch")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
    except Exception as e:
        return {"_err": str(e)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", default=TOKEN_FILE)
    ap.add_argument("--minutes", type=int, default=25)
    ap.add_argument("--run-id", default="")
    a = ap.parse_args()
    token = open(a.token_file, encoding="utf-8").read().strip()
    owner = get("/user", token).get("login", "")
    base = "/repos/%s/%s" % (owner, REPO)

    rid = a.run_id
    if not rid:
        runs = get(base + "/actions/runs?per_page=1", token).get("workflow_runs") or []
        if not runs:
            print("还没有运行记录")
            return 1
        rid = runs[0]["id"]

    deadline = time.time() + a.minutes * 60
    last = None
    while time.time() < deadline:
        run = get(base + "/actions/runs/%s" % rid, token)
        status, concl = run.get("status"), run.get("conclusion")
        cur = "%s/%s" % (status, concl)
        if cur != last:
            print("[%s] run #%s  %s  %s" % (
                time.strftime("%H:%M:%S"), run.get("run_number"), status, concl or ""))
            last = cur
        if status == "completed":
            jobs = get(base + "/actions/runs/%s/jobs" % rid, token).get("jobs") or []
            for j in jobs:
                print("\n作业：%s → %s" % (j["name"], j.get("conclusion")))
                for s in j.get("steps") or []:
                    mark = {"success": "✅", "failure": "❌", "skipped": "–",
                            "cancelled": "⊘"}.get(s.get("conclusion"), "·")
                    print("  %s %s" % (mark, s["name"]))
            print("\n详情：%s" % run.get("html_url"))
            return 0 if concl == "success" else 2
        time.sleep(20)

    print("等待超时（%d 分钟），任务可能还在跑：%s" % (a.minutes, run.get("html_url")))
    return 3


if __name__ == "__main__":
    sys.exit(main())
