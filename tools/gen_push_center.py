# -*- coding: utf-8 -*-
"""推送中心 · 数据生成器（供站点内「推送中心」视图消费）。

读取：
  - dist/push_log.jsonl                  （pipeline/notifier.py 写入的推送账本）
  - tools/executor/state/sim_push_log.jsonl （executor 模拟盘操作类推送账本）
  - config/notify.json / NOTIFY_JSON    （收件人 + 通道）
  - config/recipients_runtime.json       （按人 scope 覆盖层，与 notifier/runner 共用）

产出：
  dist/push_center.json  —— 合并后的推送历史 + 收件人当前 scope 配置 + scope/mode 元信息。
  该文件随后由 pipeline/encrypt_data.py 用 owner 口令加密为 dist/data/push_center.bin，
  站内 owner 登录后解密渲染（公开不泄露，非 owner 看不到）。

用法：
  python tools/gen_push_center.py            # 生成 dist/push_center.json
  python tools/gen_push_center.py --check   # 仅校验能正常读取配置，不写文件
"""
import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# mode -> (中文标签, 主色)，与 tools/gen_push_panel.py 保持一致
MODE_META = {
    "preauction":   ("盘前预判", "#2f6fed"),
    "auction":      ("竞价确认", "#19c3d6"),
    "close":        ("收盘复盘", "#8b5cf6"),
    "close_again":  ("复盘补发", "#8b5cf6"),
    "weekend":      ("周末前瞻", "#6b7280"),
    "anomaly":      ("盘中异动", "#f59e0b"),
    "open_anomaly": ("竞价异动", "#f59e0b"),
    "open_discipline": ("竞价纪律", "#f59e0b"),
    "anomaly_basis": ("异动基线", "#f59e0b"),
    "panic":        ("盘中恐慌", "#e02020"),
    "stoploss":     ("止损提醒", "#e02020"),
    "yaogu":        ("妖股潜力", "#ec4899"),
    "sim":          ("模拟盘", "#0a8f3c"),
}
# mode -> 允许接收的 scope 集合（与 pipeline/notifier.py.MODE_SCOPE 对齐）
MODE_SCOPE = {
    "preauction":   {"all", "prepost"},
    "auction":      {"all", "prepost"},
    "close":        {"all", "prepost"},
    "close_again":  {"all", "prepost"},
    "weekend":      {"all", "prepost"},
    "anomaly":      {"all"},
    "open_anomaly": {"all"},
    "panic":        {"all"},
    "yaogu":        {"all"},
    "stoploss":     {"all"},
}
# scope -> 元信息（供 UI 展示说明）
SCOPE_META = {
    "all":     {"label": "全部内容", "desc": "盘前/竞价/收盘/复盘/盘中异动/风险类全部接收"},
    "sim":     {"label": "仅模拟盘", "desc": "只接收模拟盘操作类推送"},
    "prepost": {"label": "仅盘前盘后", "desc": "只接收盘前预判/竞价/收盘/复盘/周末，不接收盘中异动与风险类"},
    "none":    {"label": "不接收", "desc": "该接收人不再接收任何推送"},
}


def _load(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def load_history():
    recs = _load(os.path.join(ROOT, "dist", "push_log.jsonl"))
    recs += _load(os.path.join(ROOT, "tools", "executor", "state", "sim_push_log.jsonl"))
    seen = set()
    uniq = []
    for r in recs:
        key = (r.get("ts"), r.get("mode"), r.get("title"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    uniq.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return uniq


def _load_notify():
    raw = os.environ.get("NOTIFY_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    rp = os.path.join(ROOT, "config", "notify.json")
    if os.path.exists(rp):
        try:
            return json.load(open(rp, encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_runtime():
    p = os.path.join(ROOT, "config", "recipients_runtime.json")
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def build_recipients(cfg, rt):
    """合并 notify 配置与 runtime 覆盖层，产出收件人当前 scope 视图。

    runtime 覆盖层按 name 优先：若 runtime 列出某 name，则以其 scope 为准；
    否则沿用 notify 条目中的 scope（缺省 all）。
    """
    rt_map = {}
    for r in (rt.get("recipients") or []):
        if isinstance(r, dict) and r.get("name"):
            rt_map[r["name"].strip()] = r
    recs = {}
    ch_defs = (("wechat_serverchan", "ServerChan"),
               ("wechat_pushplus", "PushPlus"),
               ("wecom", "企业微信"),
               ("telegram", "Telegram"))
    for ch_key, ch_label in ch_defs:
        cc = cfg.get(ch_key) or {}
        if not isinstance(cc, dict):
            continue
        for field in ("sendkey", "sendkeys", "token", "keys"):
            for x in (cc.get(field) or []):
                if not isinstance(x, dict):
                    continue
                nm = (x.get("name") or "").strip()
                if not nm:
                    continue
                base_scope = (x.get("scope") or "all")
                ov = rt_map.get(nm)
                eff = (ov.get("scope") or base_scope) if ov else base_scope
                rec = recs.setdefault(nm, {"name": nm, "scope": eff, "channels": [], "user": None})
                if ch_label not in rec["channels"]:
                    rec["channels"].append(ch_label)
                rec["scope"] = eff
                if ov and ov.get("user"):
                    rec["user"] = ov.get("user")
    # 若 runtime 列出了某 name 但 notify 里没有对应通道（理论不出现），仍保留其 scope 信息
    for nm, r in rt_map.items():
        recs.setdefault(nm, {"name": nm, "scope": r.get("scope") or "all",
                             "channels": [], "user": r.get("user")})
    return [recs[n] for n in recs]


def build():
    history = load_history()
    cfg = _load_notify()
    rt = _load_runtime()
    recipients = build_recipients(cfg, rt)
    return {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "history": history,
        "recipients": recipients,
        "scope_meta": SCOPE_META,
        "mode_scope": {k: sorted(list(v)) for k, v in MODE_SCOPE.items()},
        "mode_meta": {k: {"label": v[0], "color": v[1]} for k, v in MODE_META.items()},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="仅校验配置可读取，不写文件")
    args = ap.parse_args()

    data = build()
    if args.check:
        print("[gen_push_center] 校验通过：history=%d 收件人=%d"
              % (len(data["history"]), len(data["recipients"])))
        print("[gen_push_center] 收件人 scope：%s"
              % ", ".join("%s=%s" % (r["name"], r["scope"]) for r in data["recipients"]))
        return 0

    out_path = os.path.join(ROOT, "dist", "push_center.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("[gen_push_center] 已写出 %s（history=%d 收件人=%d）"
          % (out_path, len(data["history"]), len(data["recipients"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
