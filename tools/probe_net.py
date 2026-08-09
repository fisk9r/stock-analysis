# -*- coding: utf-8 -*-
"""行情源 / 推送通道连通性探针 —— 判断某个环境（本机 / CI runner / 云函数）
能否稳定支撑本项目的数据抓取。

为什么需要它：东方财富对云服务器与境外 IP 存在频控和连接重置，社区甚至有专门的
代理补丁来绕过。把项目搬到 GitHub Actions 之前，必须用真实 runner 实测，而不是凭猜。

设计要点：探针**直接调用项目自己的 em_api 函数**，而不是另写一套 HTTP 请求。
项目内置了自定义 SSL context、连接池和 5 次重试；裸 urllib 复刻会得到假阴性
（实测本机裸请求 0/3 失败，但走 em_api 却完全正常）。

用法：
    python tools/probe_net.py            # 完整探测
    python tools/probe_net.py --quick    # 只测关键项

退出码：0=可用（含"带重试可用"）；1=关键行情源不可达，该环境不可用
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import em_api  # noqa: E402


def run_case(name, fn, critical, rounds=3, min_count=1):
    """多轮调用真实接口，统计成功率与耗时。频控往往在第 N 次才暴露，故多轮。"""
    oks, lats, errs, counts = 0, [], [], []
    for i in range(rounds):
        t0 = time.time()
        try:
            n = fn()
            dt = time.time() - t0
            if n is not None and n >= min_count:
                oks += 1
                lats.append(dt)
                counts.append(n)
            else:
                errs.append("返回条数不足：%s" % n)
        except Exception as e:
            errs.append(repr(e)[:90])
        if i < rounds - 1:
            time.sleep(1.0)

    rate = oks / float(rounds)
    avg = (sum(lats) / len(lats)) if lats else 0
    mark = "PASS" if rate == 1 else ("WARN" if rate > 0 else "FAIL")
    flag = "[关键]" if critical else "[次要]"
    tail = ("平均 %.1fs，样本量 %s" % (avg, counts[0])) if lats else "—"
    print("  %-4s %s %-30s %d/%d  %s" % (mark, flag, name, oks, rounds, tail))
    for e in errs[:2]:
        print("            └─ %s" % e)
    return rate, critical


def main():
    quick = "--quick" in sys.argv
    rounds = 2 if quick else 3

    print("=" * 70)
    print("行情源 / 推送通道连通性探针（调用项目真实 em_api）")
    print("=" * 70)
    try:
        import json as _json
        import urllib.request as _u
        ip = _json.loads(_u.urlopen("https://api.ipify.org?format=json", timeout=8).read())
        print("出口 IP : %s" % ip.get("ip", "?"))
    except Exception:
        print("出口 IP : （查询失败，不影响探测）")
    print("时间    : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    # push2 主域名对境外/云 IP 会 RST 断连，em_api 会自动降级到延迟域名。
    # 这里提前触发探测，让日志明确显示本环境实际走的是哪个域名。
    try:
        h = em_api.push2_host()
        note = "实时行情" if h == em_api.PUSH2_PRIMARY else "延迟行情（盘后数据与实时一致）"
        print("push2   : %s  → %s" % (h, note))
    except Exception as e:
        print("push2   : 探测失败 %s" % repr(e)[:60])
    print("-" * 70)

    cases = [
        ("涨停池 zt_pool", lambda: len(em_api.zt_pool() or []), True, 0),
        ("全市场分页 clist_paged",
         lambda: len(em_api.clist_paged("m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                                        "f12,f14,f2,f3", max_pages=1)[0] or []), True, 1),
        ("指数快照 index_snapshot", lambda: len(em_api.index_snapshot() or []), True, 1),
    ]
    if not quick:
        cases.append(
            ("历史K线 kline_batch",
             lambda: len(em_api.kline_batch([("000001", "0.000001")], limit=5, workers=1)[0] or {}),
             False, 1))

    results = []
    for name, fn, critical, minc in cases:
        results.append(run_case(name, fn, critical, rounds, minc))

    # 推送通道（不真发，只验证域名可达 + TLS 握手）
    print("-" * 70)
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline"))
        import notifier  # noqa
        cfg = notifier.load_config()
        sc = len(notifier._iter_sendkeys(cfg.get("wechat_serverchan")))
        pp = len(notifier._iter_pushplus(cfg.get("wechat_pushplus")))
        print("  推送配置: ServerChan %d 个 key / PushPlus %d 个 token" % (sc, pp))
        if sc == 0 and pp == 0:
            print("            ⚠ 未读到任何推送凭据（CI 中请确认 Secrets 已注入）")
    except Exception as e:
        print("  推送配置读取失败: %s" % repr(e)[:80])

    print("-" * 70)
    crit = [r for r, c in results if c]
    if crit and all(r == 1 for r in crit):
        print("结论：关键行情接口全部稳定 → 该环境可以承载数据抓取")
        code = 0
    elif crit and any(r > 0 for r in crit):
        print("结论：关键接口时通时断 → 有频控/抖动，但项目自带 5 次重试可兜住")
        print("      可用；建议保留失败回退，勿把它当唯一数据来源")
        code = 0
    else:
        print("结论：关键行情接口不可达 → 该环境【不能】用于抓取东财数据")
        print("      建议改用国内节点（腾讯云 SCF / 轻量服务器）")
        code = 1
    print("=" * 70)
    return code


if __name__ == "__main__":
    sys.exit(main())
