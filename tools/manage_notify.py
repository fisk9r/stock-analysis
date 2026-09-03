#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/manage_notify.py — 自助管理推送分级（scope / 昵称）。

需求5：用户自己决定哪个 PushPlus / ServerChan 号推所有信息、只推模拟盘、只推盘前盘后，
并设置昵称方便分辨。本工具直接编辑 config/notify.json，写前自动备份。

scope 取值：
  all      全部信息（盘前盘后 + 模拟盘操作 + 盘中异动 等）
  sim      仅模拟盘操作（买卖/开仓/尾盘/满仓买点提示）
  prepost  仅盘前盘后（盘前预判/竞价/收盘/复盘/周末），不含盘中异动与模拟盘操作
  none     不接收任何推送

用法：
  python tools/manage_notify.py list
  python tools/manage_notify.py set-scope --channel pushplus  --name "我" --scope all
  python tools/manage_notify.py set-scope --channel serverchan --key SCTxxx --scope none
  python tools/manage_notify.py set-scope --user owner --scope all
  python tools/manage_notify.py set-name  --channel pushplus --token xxxx --name "新昵称"
"""
import argparse
import datetime
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "config", "notify.json")

SCOPES = ("all", "sim", "prepost", "none")
# 通道 → 条目列表字段名（兼容 sendkey/sendkeys）
_CHAN_FIELDS = {
    "serverchan": ("wechat_serverchan", ("sendkey", "sendkeys"), "key"),
    "pushplus":   ("wechat_pushplus", ("token",), "token"),
    "wecom":      ("wecom", ("webhook",), "webhook"),
    "telegram":   ("telegram", ("token",), "token"),
    "email":      ("email", ("to", "addr"), "to"),
}


def _load():
    if not os.path.exists(CFG):
        print("✗ 找不到配置文件：%s" % CFG)
        sys.exit(1)
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def _save(cfg):
    os.makedirs(os.path.dirname(CFG), exist_ok=True)
    bak = CFG + ".bak-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(CFG, bak)
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("✓ 已写入 %s（备份：%s）" % (CFG, os.path.basename(bak)))


def _entry_id(channel, entry):
    """返回一个条目的标识（key/token/webhook/名称），用于匹配与展示。"""
    if isinstance(entry, dict):
        return (entry.get("key") or entry.get("token") or entry.get("webhook")
                or entry.get("to") or entry.get("chat_id") or entry.get("name") or "?")
    return str(entry)


def _iter_entries(cfg, channel):
    """遍历某通道下所有条目，yield (field, index, entry)。"""
    real, fields, _ = _CHAN_FIELDS[channel]
    node = cfg.get(real)
    if not isinstance(node, dict):
        return
    for fld in fields:
        arr = node.get(fld)
        if isinstance(arr, list):
            for i, e in enumerate(arr):
                yield fld, i, e


def _match(entry, args):
    if isinstance(entry, dict):
        if args.name and entry.get("name") == args.name:
            return True
        if args.user and entry.get("user") == args.user:
            return True
        if args.key and (entry.get("key") == args.key):
            return True
        if args.token and (entry.get("token") == args.token):
            return True
    elif isinstance(entry, str):
        if args.key and entry == args.key:
            return True
        if args.token and entry == args.token:
            return True
    return False


def cmd_list(args, cfg):
    print("=" * 78)
    print("推送分级配置（%s）" % CFG)
    print("=" * 78)
    for channel in ("serverchan", "pushplus", "wecom", "telegram", "email"):
        real, fields, _ = _CHAN_FIELDS[channel]
        node = cfg.get(real)
        if not isinstance(node, dict):
            continue
        print("\n[%s]" % real)
        any_e = False
        for fld in fields:
            arr = node.get(fld)
            if not isinstance(arr, list):
                continue
            for e in arr:
                any_e = True
                if isinstance(e, dict):
                    scope = e.get("scope", "all(默认)")
                    line = "  · name=%-12s user=%-8s scope=%-10s id=%s" % (
                        e.get("name", "-"), e.get("user", "-"),
                        scope, _entry_id(channel, e))
                else:
                    line = "  · (旧式字符串) scope=all(默认) id=%s" % e
                print(line)
        if not any_e:
            print("  （无条目）")
    print("\n说明：scope=all 全收 / sim 仅模拟盘 / prepost 仅盘前盘后 / none 不接收")


def cmd_set_scope(args, cfg):
    if args.scope not in SCOPES:
        print("✗ 非法 scope：%s（可选：%s）" % (args.scope, "/".join(SCOPES)))
        sys.exit(1)
    targets = []
    if args.user:
        # 按 user 跨通道批量设置
        for channel in ("serverchan", "pushplus", "wecom", "telegram", "email"):
            for fld, i, e in _iter_entries(cfg, channel):
                if isinstance(e, dict) and e.get("user") == args.user:
                    targets.append((channel, fld, i, e))
        if not targets:
            print("✗ 未找到 user=%s 的条目" % args.user)
            sys.exit(1)
    else:
        if not args.channel:
            print("✗ 请指定 --channel（或改用 --user 批量设置）")
            sys.exit(1)
        found = False
        for fld, i, e in _iter_entries(cfg, args.channel):
            if _match(e, args):
                targets.append((args.channel, fld, i, e))
                found = True
        if not found:
            print("✗ 在通道 %s 下未找到匹配条目（name/key/token/user 需命中）"
                  % _CHAN_FIELDS[args.channel][0])
            sys.exit(1)
    for channel, fld, i, e in targets:
        real = _CHAN_FIELDS[channel][0]
        if not isinstance(e, dict):
            # 旧式字符串条目：升级为 dict 再设 scope
            e = {"key" if channel == "serverchan" else "token": e}
            cfg[real][fld][i] = e
        e["scope"] = args.scope
        print("✓ [%s] %s → scope=%s" % (_CHAN_FIELDS[channel][0],
                                         _entry_id(channel, e), args.scope))
    _save(cfg)


def cmd_set_name(args, cfg):
    if not args.channel:
        print("✗ 请指定 --channel")
        sys.exit(1)
    if not args.name:
        print("✗ 请指定 --name（新昵称）")
        sys.exit(1)
    found = False
    for fld, i, e in _iter_entries(cfg, args.channel):
        if _match(e, args):
            if not isinstance(e, dict):
                e = {"key" if args.channel == "serverchan" else "token": e}
                cfg[_CHAN_FIELDS[args.channel][0]][fld][i] = e
            e["name"] = args.name
            print("✓ [%s] %s → name=%s" % (_CHAN_FIELDS[args.channel][0],
                                            _entry_id(args.channel, e), args.name))
            found = True
    if not found:
        print("✗ 在通道 %s 下未找到匹配条目" % _CHAN_FIELDS[args.channel][0])
        sys.exit(1)
    _save(cfg)


def build_parser():
    p = argparse.ArgumentParser(description="推送分级（scope/昵称）管理")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出当前所有通道条目的 scope/昵称").set_defaults(func=cmd_list)

    sp = sub.add_parser("set-scope", help="设置某个条目的 scope")
    sp.add_argument("--channel", choices=list(_CHAN_FIELDS.keys()),
                    help="通道：serverchan / pushplus / wecom / telegram / email")
    sp.add_argument("--scope", choices=SCOPES, required=True, help="目标 scope")
    sp.add_argument("--name", help="按昵称匹配")
    sp.add_argument("--key", help="按 ServerChan key 匹配")
    sp.add_argument("--token", help="按 PushPlus token 匹配")
    sp.add_argument("--user", help="按绑定用户批量设置（跨通道）")
    sp.set_defaults(func=cmd_set_scope)

    sn = sub.add_parser("set-name", help="设置某个条目的昵称")
    sn.add_argument("--channel", choices=list(_CHAN_FIELDS.keys()), required=True)
    sn.add_argument("--name", required=True, help="新昵称")
    sn.add_argument("--key", help="按 ServerChan key 匹配")
    sn.add_argument("--token", help="按 PushPlus token 匹配")
    sn.add_argument("--user", help="按绑定用户匹配")
    sn.set_defaults(func=cmd_set_name)
    return p


def main():
    args = build_parser().parse_args()
    cfg = _load()
    args.func(args, cfg)


if __name__ == "__main__":
    main()
