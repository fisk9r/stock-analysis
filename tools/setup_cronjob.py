#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 tools/cronjob-config.json 里的定时任务注册到 cron-job.org（免费云端定时器），
由它独立直打 GitHub 的 workflow_dispatch API，彻底绕开 GitHub 自带 schedule
（高负载会被延迟甚至整体丢弃，正是本次漏发盘前推送的根因）。

⚠ 关键：cron-job.org 的「创建任务」端点是 **PUT /jobs**（不是 POST！POST 会 404），
   schedule 是数组结构 {minutes,hours,mdays,months,wdays}（不是 cron 字符串），
   requestMethod 用整数（1 = POST），自定义请求头与 body 放在 extendedData 里。

用法（在仓库根目录执行）：
    CRONJOB_API_KEY=xxxx  GH_PAT=ghp_xxxx  python tools/setup_cronjob.py

参数说明：
    CRONJOB_API_KEY : cron-job.org 账号的 API Key（免费注册 https://cron-job.org →
                      Dashboard → Account → API Key）。纯云端运行，与本地开机无关。
    GH_PAT          : GitHub 个人访问令牌，需 workflow 作用域（复用本项目已有的 PAT 即可）。
                      仅用于调用 dispatch API，不写入任何文件。

注册后：这 10 个任务会成为「权威触发器」，GitHub 自带 schedule + 看门狗 + 备份订阅
变成冗余保险。notifier.push 另有 mode+当日 幂等去重 + anomaly 12分钟冷却，
多路同时点火也只发一次，绝不重复轰炸。

限速：创建接口限 5 次/分钟，故每创建 1 个 sleep 13 秒留余量。

幂等：重复运行不会累积重复定时器——先 GET /jobs 列出已有任务，按 title 匹配，
      已存在的更新（PUT /jobs/{id}），不存在的才创建（PUT /jobs）。
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cronjob-config.json")
API = "https://api.cron-job.org/jobs"


def cron_to_schedule(cron):
    """'M H DOM MON DOW' → cron-job.org 的 schedule 数组结构。
    -1 表示『所有/每』；wdays 0=周日..6=周六（与标准 cron 一致）。"""
    p = cron.split()

    def field(f):
        if f == "*":
            return [-1]
        if "-" in f:
            a, b = f.split("-")
            return list(range(int(a), int(b) + 1))
        if "," in f:
            return [int(x) for x in f.split(",")]
        return [int(f)]

    return {
        "timezone": "Asia/Shanghai",
        "expiresAt": 0,
        "minutes": field(p[0]),
        "hours": field(p[1]),
        "mdays": field(p[2]),
        "months": field(p[3]),
        "wdays": field(p[4]),
    }


def _api(key, url, data=None, method="GET"):
    """带 3 次重试的 cron-job.org 调用。HTTP 错误立即返回（确定性），
    仅网络异常重试。返回 (status, parsed_json)。"""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8", "ignore"))
            except Exception:
                return e.code, {}
        except Exception as e:
            if attempt == 2:
                return 0, {"error": str(e)}
            time.sleep(3)
    return 0, {}


def list_jobs(key):
    """返回 {title: jobId}，用于幂等匹配。"""
    st, resp = _api(key, API)
    if st != 200:
        print("⚠️ 列出已有任务失败（HTTP %s），将按全量创建处理。" % st)
        return {}
    return {j.get("title"): j.get("jobId") for j in (resp.get("jobs") or [])}


def main():
    key = os.environ.get("CRONJOB_API_KEY")
    pat = os.environ.get("GH_PAT")
    if not key or not pat:
        sys.exit("用法：CRONJOB_API_KEY=xxx GH_PAT=xxx python tools/setup_cronjob.py")
    cfg = json.load(open(CFG, encoding="utf-8"))

    # 注入真实 PAT（配置里是占位符 __GH_PAT__），构造 extendedData.headers（dict 结构）
    headers = {k: (v.replace("__GH_PAT__", pat) if isinstance(v, str) else v)
               for k, v in cfg["headers"].items()}

    existing = list_jobs(key)
    ok = 0
    for job in cfg["jobs"]:
        body = {
            "job": {
                "url": cfg["github_api"],
                "enabled": True,
                "title": job["title"],
                "saveResponses": True,
                "schedule": cron_to_schedule(job["cron"]),
                "requestMethod": 1,  # 1 = POST（GitHub dispatch 需要 POST）
                "extendedData": {
                    "headers": headers,
                    "body": json.dumps({"ref": "main", "inputs": {"task": job["task"]}}),
                },
            }
        }
        data = json.dumps(body).encode("utf-8")
        eid = existing.get(job["title"])
        if eid:
            url, action = "%s/%s" % (API, eid), "更新"
        else:
            url, action = API, "创建"
        # ⚠ 创建/更新都用 PUT（不是 POST）
        st, resp = _api(key, url, data, method="PUT")
        new_id = resp.get("jobId") or eid
        if st in (200, 201) and new_id:
            print("✅ 已%s: %s  (cron=%s, task=%s, jobId=%s)"
                  % (action, job["title"], job["cron"], job["task"], new_id))
            ok += 1
        else:
            print("❌ 失败: %s  HTTP %s  %s" % (job["title"], st, str(resp)[:200]))
        # 创建限流 5 次/分钟 → 间隔 13 秒留余量
        time.sleep(13)
    print("完成：%d/%d 个任务已注册/更新到 cron-job.org" % (ok, len(cfg["jobs"])))
    if ok:
        print("（幂等：已存在的同名任务会被更新而非重复创建）")
        print("此后盘前/竞价/盘中(6次)/收盘/复盘 由云端定时器独立触发，不再依赖 GitHub 自带 schedule。")


if __name__ == "__main__":
    main()
