#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 tools/cronjob-config.json 里的定时任务注册到 cron-job.org（免费云端定时器），
由它独立直打 GitHub 的 workflow_dispatch API，彻底绕开 GitHub 自带 schedule
（高负载会被延迟甚至整体丢弃，正是本次漏发盘前推送的根因）。

用法（在仓库根目录执行）：
    CRONJOB_API_KEY=xxxx  GH_PAT=ghp_xxxx  python tools/setup_cronjob.py

参数说明：
    CRONJOB_API_KEY : cron-job.org 账号的 API Key（免费注册 https://cron-job.org →
                      Dashboard → Account → API Key）。纯云端运行，与本地开机无关。
    GH_PAT          : GitHub 个人访问令牌，需 workflow 作用域（复用本项目已有的 PAT 即可）。
                      仅用于调用 dispatch API，不写入任何文件。

注册后：这 5 个任务会成为「权威触发器」，GitHub 自带 schedule + 看门狗 + 备份订阅
变成冗余保险。notifier.push 另有 mode+当日 幂等去重，多路同时点火也只发一次。
"""
import os
import sys
import json
import urllib.request
import urllib.error

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cronjob-config.json")
API = "https://api.cron-job.org/jobs"


def main():
    key = os.environ.get("CRONJOB_API_KEY")
    pat = os.environ.get("GH_PAT")
    if not key or not pat:
        sys.exit("用法：CRONJOB_API_KEY=xxx GH_PAT=xxx python tools/setup_cronjob.py")
    cfg = json.load(open(CFG, encoding="utf-8"))

    # 注入真实 PAT（配置里是占位符 __GH_PAT__）
    headers = {k: (v.replace("__GH_PAT__", pat) if isinstance(v, str) else v)
               for k, v in cfg["headers"].items()}

    ok = 0
    for job in cfg["jobs"]:
        body = {
            "job": {
                "url": cfg["github_api"],
                "enabled": True,
                "schedule": job["schedule"],
                "timezone": "Asia/Shanghai",
                "requestMethod": "POST",
                "requestHeaders": [{"name": k, "value": v} for k, v in headers.items()],
                "requestBody": json.dumps({"ref": "main", "inputs": {"task": job["task"]}}),
                "saveResponses": True,
                "title": job["title"],
            }
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(API, data=data, method="POST")
        req.add_header("Authorization", "Bearer " + key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print("✅ 已创建: %s  (cron=%s, task=%s, HTTP %s)"
                      % (job["title"], job["schedule"], job["task"], r.status))
                ok += 1
        except urllib.error.HTTPError as e:
            print("❌ 失败: %s  HTTP %s  %s" % (job["title"], e.code,
                                                e.read().decode("utf-8", "ignore")[:200]))
    print("完成：%d/%d 个任务已注册到 cron-job.org" % (ok, len(cfg["jobs"])))
    if ok:
        print("此后盘前/竞价/收盘/异动将由云端定时器独立触发，不再依赖 GitHub 自带 schedule。")


if __name__ == "__main__":
    main()
