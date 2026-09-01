# -*- coding: utf-8 -*-
"""2026-09-01 模拟盘重置：清空持仓/交易/复盘/账本，恢复 100000 初始资金全新开始。

背景：09:26 开仓因 days_held 误算 + 无 T+1 守卫，楚天龙/勤上股份当日买入当日卖出
（T+1 违规），且已产生错误持仓。用户要求「额度设置回 100000，重新开始模拟」。
本脚本从 Release 下载最新 executor_state.tar.gz → 重建空 sim.db（schema 保留、
无任何持仓/交易）+ 空 sim_review + 空 task_ledger + 重置风控/连亏 → 重传。
"""
import io
import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.request
import ssl

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXE = os.path.dirname(os.path.abspath(__file__))  # tools/executor
sys.path.insert(0, os.path.join(ROOT, "tools"))
import gh_api  # noqa: E402

tok = gh_api._token()
ctx = ssl._create_unverified_context()
EXEC_STATE = "executor_state.tar.gz"


def _dl_asset(asset_id):
    req = urllib.request.Request(
        "https://api.github.com/repos/fisk9r/stock-analysis/releases/assets/%d" % asset_id,
        headers={"Authorization": "Bearer " + tok, "Accept": "application/octet-stream"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
                return r.read()
        except Exception as e:
            if i == 2:
                raise


def _get_latest_asset():
    st, body = gh_api.api("GET", "/repos/fisk9r/stock-analysis/releases/tags/data-snapshot")
    rel = json.loads(body) if isinstance(body, str) else body
    return next(a for a in rel["assets"] if a["name"] == EXEC_STATE)


def _fresh_simdb():
    """重建空 sim.db（schema 完整，无任何行）。"""
    src = open(os.path.join(EXE, "broker_sim.py"), encoding="utf-8").read()
    schema = re.search('SCHEMA = """(.*?)"""', src, re.S).group(1)
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "sim.db")
    con = sqlite3.connect(db)
    con.executescript(schema)
    con.commit()
    con.close()
    return db, tmp


def main():
    # 1. 下载当前状态包
    asset = _get_latest_asset()
    print("当前资产 id:", asset["id"], asset["updated_at"])
    blob = _dl_asset(asset["id"])

    # 2. 解包到临时目录
    tmp_root = tempfile.mkdtemp()
    tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")
    members = []
    for m in tf.getmembers():
        name = m.name.replace("\\", "/").lstrip("./")
        if m.issym() or m.islnk():
            continue
        m.name = name
        members.append(m)
    tf.extractall(tmp_root, members=members)
    tf.close()
    print("解包成员:", [m.name for m in members])

    # 3. 重置 sim.db（空）
    fresh_db, _ = _fresh_simdb()
    os.replace(fresh_db, os.path.join(tmp_root, "sim.db"))

    # 4. 重置 sim_review.json（空 days）
    rev = os.path.join(tmp_root, "sim_review.json")
    with open(rev, "w", encoding="utf-8") as f:
        json.dump({"days": {}, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False)

    # 5. 重置 task_ledger.json（空，今日 now 可重跑）
    led = os.path.join(tmp_root, "state", "task_ledger.json")
    os.makedirs(os.path.dirname(led), exist_ok=True)
    with open(led, "w", encoding="utf-8") as f:
        json.dump({}, f)

    # 6. 重置 risk_state.json（清熔断）与 loss_streak.json（清连亏）
    rs = os.path.join(tmp_root, "risk_state.json")
    if os.path.exists(rs):
        with open(rs, "w", encoding="utf-8") as f:
            json.dump({}, f)
    ls = os.path.join(tmp_root, "state", "loss_streak.json")
    if os.path.exists(ls):
        with open(ls, "w", encoding="utf-8") as f:
            json.dump({}, f)

    # 7. 重新打包
    out = os.path.join(tmp_root, EXEC_STATE)
    with tarfile.open(out, "w:gz") as tf2:
        for m in members:
            if m.name in ("risk_state.json", "state/loss_streak.json"):
                continue  # 已重建
            p = os.path.join(tmp_root, m.name)
            if os.path.exists(p):
                tf2.add(p, arcname=m.name)
        # 追加重建/新增的
        for name in ("sim.db", "sim_review.json", "state/task_ledger.json",
                     "risk_state.json", "state/loss_streak.json"):
            p = os.path.join(tmp_root, name)
            if os.path.exists(p):
                tf2.add(p, arcname=name)
    print("重打包完成，新状态包成员:")
    with tarfile.open(out, "r:gz") as tv:
        for m in tv.getmembers():
            print("  ", m.name)

    # 8. 删除旧资产 + 上传新资产（uploads.github.com）
    st, body = gh_api.api("GET", "/repos/fisk9r/stock-analysis/releases/tags/data-snapshot")
    rel = json.loads(body) if isinstance(body, str) else body
    for a in rel["assets"]:
        if a["name"] == EXEC_STATE:
            gh_api.api("DELETE", "/repos/fisk9r/stock-analysis/releases/assets/%d" % a["id"])
            print("已删除旧资产", a["id"])
    up_url = rel["upload_url"].split("{")[0]
    data = open(out, "rb").read()
    req = urllib.request.Request(
        "%s?name=%s" % (up_url, EXEC_STATE), data=data, method="POST",
        headers={"Authorization": "Bearer " + tok,
                 "Content-Type": "application/gzip",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
        r.read()
    print("✅ 新状态包已上传（HTTP 201）")


if __name__ == "__main__":
    main()
