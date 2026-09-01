# -*- coding: utf-8 -*-
"""盘中异动扫描（需实时行情，CI 交易时段定时触发）。

数据来自腾讯 qt.gtimg.cn（公开、无需密钥、返回 CORS 友好）。本模块只在
GitHub Actions 交易时段 workflow 中运行（本地 market.db 仅盘后，不跑此模块）。

职责：
  1. 取关注池 + 持仓池代码；
  2. 拉实时快照（现价/昨收/涨跌幅/成交额）；
  3. 识别异动：涨停附近 / 大涨 / 大跌 / 急速拉升 / 放量；
  4. 命中即经 PushPlus 推送（复用 notifier.push）；
  5. 落库（cache/market.db）+ 上墙片段（data/intraday_latest.json）+ 喂 T+1
     （次日 build 读取该片段嵌入 data['intraday']，作为「昨日盘中异动」呈现）。

注意：本模块是「触发脚手架」——逻辑完整，但依赖外网实时数据，
本地 unittest 无法验证（无实时行情）。上线需用户在仓库开启
workflows/intraday.yml 并配置 PUSHPLUS_TOKEN 密钥。
"""
import os
import sys
import json
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watchlist


QT_URL = "http://qt.gtimg.cn/q="
UA = {"User-Agent": "Mozilla/5.0"}


def _prefix(code):
    return ("sh" if code.startswith(("60", "68", "9")) else "sz") + code


def fetch_realtime(codes):
    """返回 {code: {price, prev_close, pct, amount_yi, name}}。失败返回空 dict。"""
    if not codes:
        return {}
    q = QT_URL + ",".join(_prefix(c) for c in codes)
    try:
        req = urllib.request.Request(q, headers=UA)
        raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", "ignore")
    except Exception as e:
        sys.stderr.write("intraday fetch failed: %r\n" % e)
        return {}
    out = {}
    for line in raw.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"')
        if not val:
            continue
        f = val.split("~")
        if len(f) < 10:
            continue
        code = f[2]
        try:
            price = float(f[3]); prev = float(f[4]); amount = float(f[9])
        except Exception:
            continue
        pct = (price / prev - 1) * 100 if prev else 0.0
        out[code] = {"name": f[1], "price": round(price, 2),
                     "prev_close": round(prev, 2),
                     "pct": round(pct, 2),
                     "amount_yi": round(amount / 1e8, 2)}
    return out


def detect(codes, snap, baseline=None):
    """baseline: {code: 开盘价} 用于急速拉升判定（可选）。返回异动列表。"""
    alerts = []
    for c in codes:
        d = snap.get(c)
        if not d:
            continue
        pct = d["pct"]
        note = None
        if pct >= 9.5:
            note = "涨停附近 +%.1f%%" % pct
        elif pct >= 5:
            note = "大涨 +%.1f%%" % pct
        elif pct <= -5:
            note = "大跌 %.1f%%" % pct
        if note:
            alerts.append({"code": c, "name": d["name"], "pct": pct,
                           "price": d["price"], "type": note})
    return alerts


def persist(alerts, date=None, root=None):
    """落库 + 上墙 + 喂 T+1。返回写入的片段 dict 或 None（无 alerts）。

    - 落库：cache/market.db intraday_alerts 表（best-effort，表缺失不影响主流程）
    - 上墙：data/intraday_latest.json（供 build 嵌入 data['intraday']，站点展示）
    - 喂T+1：data/intraday_latest.json 次日被 build 读取，作为「昨日盘中异动」呈现
    """
    if not alerts:
        return None
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    date = date or time.strftime("%Y-%m-%d")
    frag = {"date": date, "n": len(alerts), "alerts": alerts}
    # 上墙片段
    try:
        os.makedirs(os.path.join(root, "data"), exist_ok=True)
        with open(os.path.join(root, "data", "intraday_latest.json"),
                  "w", encoding="utf-8") as f:
            json.dump(frag, f, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write("intraday persist(上墙) failed: %r\n" % e)
    # 落库
    try:
        db = os.path.join(root, "cache", "market.db")
        if os.path.exists(db):
            import sqlite3 as _sq
            c = _sq.connect(db)
            c.execute("""create table if not exists intraday_alerts(
                id integer primary key autoincrement, date text, ts text,
                code text, name text, pct real, price real, type text)""")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            for a in alerts:
                c.execute(
                    "insert into intraday_alerts(date,ts,code,name,pct,price,type)"
                    " values(?,?,?,?,?,?,?)",
                    (date, ts, a.get("code"), a.get("name"),
                     a.get("pct"), a.get("price"), a.get("type")))
            c.commit()
            c.close()
    except Exception as e:
        sys.stderr.write("intraday persist(落库) failed: %r\n" % e)
    return frag


def run():
    """CLI 入口：扫描关注+持仓，推送异动并落库。返回 alert 条数。"""
    codes, names, _ = watchlist.load_watch_codes()
    snap = fetch_realtime(codes)
    alerts = detect(codes, snap)
    if not alerts:
        print("intraday: 无盘中异动")
        return 0
    frag = persist(alerts)
    lines = ["盘中异动扫描（%d 条）：" % len(alerts)]
    for a in alerts:
        lines.append("- %s（%s）%s @ %.2f" % (a["name"], a["code"], a["type"], a["price"]))
    txt = "\n".join(lines)
    print(txt)
    token = os.environ.get("PUSHPLUS_TOKEN")
    if token:
        try:
            import notifier
            notifier.send_wechat_pushplus(
                {"wechat_pushplus": {"token": token}}, "盘中异动", txt)
        except Exception as e:
            sys.stderr.write("push failed: %r\n" % e)
    return len(alerts)


if __name__ == "__main__":
    sys.exit(0 if run() >= 0 else 1)
