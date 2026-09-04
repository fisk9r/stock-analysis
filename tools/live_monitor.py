# -*- coding: utf-8 -*-
"""live_monitor —— 本机秒级盯盘（中期升级2：盘中实时化）。

为什么需要：CI 巡逻（run_scan）受 GitHub Actions 分钟级延迟限制，最快也要几十秒，
且额度有限。本脚本跑在用户自己电脑上，5 秒一轮腾讯实时行情，秒级捕捉：

  ① 急拉/急跌：现价较昨收 ±threshold%（默认 ±3%）
  ② 冲高回落：距当日最高点回落 ≥ falldown%（默认 3%）——炸板/出货前兆
  ③ 破成本：持仓现价 < 成本（可配置）
  ④ 5分钟急拉：滚动窗口内涨幅 ≥2%（对比脚本自己的历史快照，不依赖接口额外字段）

推送：PushPlus(html) 3 次退避重试 → 失败回落 ServerChan（复用 pipeline/notifier）。
去重：同类警报（code+类型）每日只推 1 次，绝不轰炸（用户红线）。

用法：
  python tools/live_monitor.py                 # 常驻，交易时段自动生效
  python tools/live_monitor.py --once          # 单轮扫描后退出（测试）
  python tools/live_monitor.py --interval 3    # 自定轮询间隔秒
  python tools/live_monitor.py --dry-run       # 只打印不推送

Windows 常驻：双击 tools/live_monitor.bat（后台 pythonw）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---------------------------------------------------------------- 配置
CONF_PATH = os.path.join(ROOT, "config", "live_monitor.json")
DEFAULT_CONF = {
    "threshold_pct": 3.0,       # 较昨收 ±3% 报警
    "falldown_pct": 3.0,        # 距当日高点回落 3% 报警
    "surge_5m_pct": 2.0,        # 5分钟窗口急拉 2% 报警
    "alert_cost_break": True,   # 持仓跌破成本报警
    "min_interval_alert": 1800  # 同类警报最小间隔秒（每日一次的兜底）
}


def load_conf():
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            c = json.load(f)
            for k, v in DEFAULT_CONF.items():
                c.setdefault(k, v)
            return c
    except Exception:
        return dict(DEFAULT_CONF)


def load_codes():
    """持仓 + 关注 代码集合 → {code: name_or_empty}。"""
    codes = {}
    try:
        import holdings
        for p in (holdings.load_positions() or []):
            codes[p["code"]] = p.get("name") or ""
    except Exception:
        pass
    try:
        import watchlist
        for c in (watchlist.load_watch_codes() or []):
            codes.setdefault(str(c), "")
    except Exception:
        pass
    return codes


def tencent_symbol(code):
    code = str(code)
    return ("sh" if code[:1] in ("6", "9", "5") else "sz") + code


def fetch_quotes(codes):
    """腾讯实时行情 → {code: {name, price, prev, open, high, low, ts}}。失败返回 {}。"""
    syms = ",".join(tencent_symbol(c) for c in codes)
    url = "https://qt.gtimg.cn/q=%s" % syms
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=6) as r:
            body = r.read().decode("gbk", "ignore")
        out = {}
        for line in body.strip().split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            var, val = line.split("=", 1)
            val = val.strip('"')
            f = val.split("~")
            if len(f) < 34:
                continue
            code = f[2]
            try:
                out[code] = {
                    "name": f[1], "price": float(f[3]), "prev": float(f[4]),
                    "open": float(f[5]), "high": float(f[33]), "low": float(f[34]),
                    "ts": f[30],
                }
            except (ValueError, IndexError):
                continue
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------- 时段闸
def in_trading_time(dt=None):
    """A股交易时段（本地即北京时间；仅判周一~五 + 09:30-11:30 / 13:00-15:00）。"""
    dt = dt or datetime.now()
    if dt.weekday() >= 5:
        return False
    hm = dt.hour * 100 + dt.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


# ---------------------------------------------------------------- 推送
def push_alert(title, html_lines, dry_run=False):
    """PushPlus(html) 3次退避重试 → 回落 ServerChan。返回 True=至少一通道成功。"""
    text = "\n".join(html_lines)
    if dry_run:
        print("[dry-run] %s\n%s" % (title, text))
        return True
    try:
        import notifier
        cfg = notifier.load_notify_cfg() if hasattr(notifier, "load_notify_cfg") else None
        if cfg is None:
            with open(os.path.join(ROOT, "config", "notify.json"), encoding="utf-8") as f:
                raw = json.load(f)
            cfg = raw
        pp = (cfg.get("wechat_pushplus") or {}).get("token") or []
        ok = False
        body = "<br/>".join(html_lines)
        for ch in pp:
            tok = ch.get("token") if isinstance(ch, dict) else ch
            for i in range(3):
                try:
                    if notifier.send_wechat_pushplus(
                            {"token": [tok] if tok else []}, title, body):
                        ok = True
                        break
                except Exception:
                    time.sleep(2 * (i + 1))
            if ok:
                break
        if not ok:
            sc = (cfg.get("wechat_serverchan") or {}).get("sendkey") or []
            for ch in sc:
                key = ch.get("key") if isinstance(ch, dict) else ch
                try:
                    if notifier.send_wechat_serverchan({"sendkey": [key] if key else []},
                                                       title, text):
                        ok = True
                        break
                except Exception:
                    continue
        return ok
    except Exception as e:
        print("[live_monitor] push failed: %r" % e)
        return False


# ---------------------------------------------------------------- 主循环
class Monitor:
    def __init__(self, conf, dry_run=False):
        self.conf = conf
        self.dry_run = dry_run
        self.hist = {}       # code -> list[(ts, price)] 近10分钟窗口
        self.alerted = {}    # (code, kind) -> date_str（每日一次去重）
        self.costs = {}      # code -> 成本价（持仓才有）
        try:
            import holdings
            for p in (holdings.load_positions() or []):
                if p.get("cost"):
                    self.costs[p["code"]] = p["cost"]
        except Exception:
            pass

    def _should_alert(self, code, kind):
        today = datetime.now().strftime("%Y-%m-%d")
        key = (code, kind)
        if self.alerted.get(key) == today:
            return False
        self.alerted[key] = today
        return True

    def scan_once(self, codes):
        q = fetch_quotes(codes)
        if not q:
            return []
        now = time.time()
        alerts = []
        th = self.conf["threshold_pct"]
        fd = self.conf["falldown_pct"]
        sg = self.conf["surge_5m_pct"]
        for code, d in q.items():
            name, price, prev = d["name"], d["price"], d["prev"]
            if not price or not prev:
                continue
            pct = (price / prev - 1) * 100
            # ① 较昨收阈值
            if abs(pct) >= th:
                kind = "surge" if pct > 0 else "plunge"
                if self._should_alert(code, kind):
                    alerts.append("📈 <b>%s(%s)</b> %+.2f%% 现价%.2f（%s）"
                                  % (name, code, pct, price,
                                     "急拉" if pct > 0 else "急跌"))
            # ② 冲高回落
            if d["high"] and d["high"] > 0:
                dd = (price / d["high"] - 1) * 100
                if dd <= -fd:
                    if self._should_alert(code, "falldown"):
                        alerts.append("🚨 <b>%s(%s)</b> 冲高回落：最高%.2f → 现%.2f（-%.1f%%）"
                                      % (name, code, d["high"], price, -dd))
            # ③ 持仓破成本
            if self.conf.get("alert_cost_break"):
                cost = self.costs.get(code)
                if cost and price < cost and self._should_alert(code, "costbreak"):
                    alerts.append("🔻 <b>%s(%s)</b> 破成本：成本%.2f → 现%.2f（%.1f%%）"
                                  % (name, code, cost, price, (price / cost - 1) * 100))
            # ④ 5分钟急拉（脚本自记录窗口）
            self.hist.setdefault(code, []).append((now, price))
            self.hist[code] = [x for x in self.hist[code] if now - x[0] <= 600]
            h = self.hist[code]
            if len(h) >= 2:
                for t0, p0 in h:
                    if now - t0 <= 300 and p0 and (price / p0 - 1) * 100 >= sg:
                        if self._should_alert(code, "surge5m"):
                            alerts.append("⚡ <b>%s(%s)</b> 5分钟急拉 %+.1f%%（%.2f→%.2f）"
                                          % (name, code, (price / p0 - 1) * 100, p0, price))
                        break
        return alerts

    def run(self, codes, interval):
        print("[live_monitor] 监控 %d 只：%s" % (len(codes), ",".join(sorted(codes))))
        while True:
            if in_trading_time():
                try:
                    alerts = self.scan_once(codes)
                    if alerts:
                        title = "盘中实时盯盘 %d 条警报" % len(alerts)
                        push_alert(title, alerts, dry_run=self.dry_run)
                        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), title))
                except Exception as e:
                    print("[live_monitor] scan error: %r" % e)
            else:
                print("[%s] 非交易时段待机" % datetime.now().strftime("%H:%M:%S"))
            time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="单轮扫描后退出")
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略交易时段闸（测试用）")
    args = ap.parse_args()

    conf = load_conf()
    codes = load_codes()
    if not codes:
        print("[live_monitor] 无监控标的（config/holdings.json + watchlist）")
        return
    m = Monitor(conf, dry_run=args.dry_run)
    if args.once:
        if not (in_trading_time() or args.force):
            print("非交易时段（--force 可强制）")
            return
        alerts = m.scan_once(codes)
        print(json.dumps(alerts, ensure_ascii=False, indent=1) if alerts else "本轮无警报")
        return
    m.run(codes, max(3, args.interval))


if __name__ == "__main__":
    main()
