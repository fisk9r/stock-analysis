# -*- coding: utf-8 -*-
"""比对本地工程与 GitHub main 分支的文件差异，抓出「改了但没推上去」的文件。

背景（2026-08-28 重大事故）：本地 store.py 新增了 trend_track_*/watch_first_seen 四个函数
和两张表，但从未推送 → CI 长期静默降级（趋势持久化整块被 except 吞掉，线上 trend 缺
is_new/verdict，而日志只有一行「不影响主流程」）。本脚本就是为防这类漏推而写。

用法：python tools/diff_remote.py [--push]
      --push  对有差异的文件自动调用 gh_api 推送（默认只报告）
"""
import os
import sys
import io
import difflib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

RAW = "https://raw.githubusercontent.com/fisk9r/stock-analysis/main/"

# 需要比对的目录与扩展名（CI 产物/data.js、缓存、状态一律跳过）
SCAN_DIRS = ["pipeline", "tools", "dist", "config", ".github"]
SKIP_EXT = (".pyc", ".db", ".gz", ".zip", ".log", ".bin", ".jsonl")
SKIP_FILES = {"data.js", "push_log.jsonl", "users.json"}
SKIP_DIRS = {"__pycache__", "cache", "state", "tmp", "data", "assets", "archive"}


def _get(url):
    import urllib.request
    import ssl
    req = urllib.request.Request(url, headers={"User-Agent": "stock-diff-remote"})
    try:
        return urllib.request.urlopen(req, timeout=60).read()
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            ctx = ssl._create_unverified_context()
            return urllib.request.urlopen(req, timeout=60, context=ctx).read()
        raise


def local_files():
    out = []
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
            for fn in filenames:
                if fn in SKIP_FILES or fn.endswith(SKIP_EXT):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                out.append(rel)
    return sorted(out)


def main():
    do_push = "--push" in sys.argv
    diffs = []
    missing = []
    for rel in local_files():
        try:
            remote = _get(RAW + rel)
        except Exception as e:
            if "404" in str(e):
                missing.append(rel)
                continue
            print("  ! 拉取失败 %s: %s" % (rel, e))
            continue
        with io.open(os.path.join(ROOT, rel), "rb") as f:
            local = f.read()
        if local.replace(b"\r\n", b"\n") != remote.replace(b"\r\n", b"\n"):
            nl = len(local.splitlines())
            nr = len(remote.splitlines())
            diffs.append((rel, nl, nr))
    print("=" * 60)
    print("远端缺失（本地有、远端无）：")
    for m in missing:
        print("  + %s" % m)
    print("内容不一致：")
    for rel, nl, nr in diffs:
        print("  * %-38s 本地%5d行 / 远端%5d行" % (rel, nl, nr))
    if not diffs and not missing:
        print("  （无）本地与远端 main 完全一致 ✅")
    print("=" * 60)
    if do_push and (diffs or missing):
        import gh_api
        files = [d[0] for d in diffs] + missing
        print("推送 %d 个文件 ..." % len(files))
        gh_api.push_files("sync: 补齐与本地不一致/缺失的文件（tools/diff_remote.py 扫描）", files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
