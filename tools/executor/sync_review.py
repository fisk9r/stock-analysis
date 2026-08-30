# -*- coding: utf-8 -*-
"""把 tools/executor/sim_review.json 回传到仓库 config/sim_review.json。

CI 构建时 pipeline/build.py 读取该文件生成网站「模拟盘」模块数据（data["sim"]）。
用法：python tools/executor/sync_review.py [--force]
  --force: 即使 review 未更新也推送（调试用）
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # 项目根
sys.path.insert(0, os.path.join(ROOT, "tools"))

REVIEW = os.path.join(HERE, "sim_review.json")
MARK = os.path.join(HERE, ".last_sync_review")
DEST = "state/sim_review.json"   # 2026-08-31 修复：build.py 读的是 state/（此前误写 config/，推了也白推）


def main():
    force = "--force" in sys.argv
    if not os.path.exists(REVIEW):
        print("无 sim_review.json，跳过回传")
        return
    # 幂等：内容未变不推
    cur = open(REVIEW, "rb").read()
    if not force and os.path.exists(MARK):
        if open(MARK, "rb").read() == cur:
            print("sim_review.json 未变化，跳过回传")
            return
    import gh_api
    # 校验 JSON 合法再推
    json.loads(cur.decode("utf-8"))
    msg = "sim: 模拟盘复盘数据回传 %s" % time.strftime("%Y-%m-%d %H:%M")
    gh_api.push_files(msg, [DEST])
    with open(MARK, "wb") as f:
        f.write(cur)
    print("OK：%s 已回传" % DEST)


if __name__ == "__main__":
    main()
