#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从分析结果数据(或线上解密快照 live_dump.json) 挖掘「上升趋势中处于买点」的票。

不按"趋势"标签呈现，按买点信号归类：
  - bull（二波启动 / 均线发散 / N字回踩）= 上升途中回踩企稳后再起 → 买点
  - strategies（均线多头 / 海龟突破 / 稳健上行 / 低ATR慢牛）= 多头趋势刚突破 → 买点
  - chanlun.buys（一/二/三买）= 缠论结构买点

趋势加速优先（用户 2026-08-31 需求）：
  交叉引用 recommend.trend（engine.screen_uptrend 产出，带 trend_meta.trend_state/accel），
  对「已处于加速上行」的买点候选打 accel_flag 并排到最前，优先推荐。

输出：
  - build_data(data)        返回结构化 dict，由 build.py 注入 data["buy_points"]（自动进加密 bin + 本地 data.js），
                            供前端原生 SPA 渲染（viewBuypoint）与推送复用；
  - generate(data, DIST)    产出零依赖单文件报告 dist/trend_buy_points.html（线上站点可直达，作为原生视图的「完整报告」兜底）。
用法：
  - 每日构建(build.py)自动调用 build_data() 注入 + generate() 产出线上报告；
  - 调试：node tools/live_inspect.js owner <pass> --deep 后 python tools/gen_buypoint_report.py
"""
import json
import os
import re

_SELL_RE = re.compile(r"卖出|止盈|离场|减仓|清仓")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, "tmp_verify", "live_dump.json")
OUT = os.path.join(ROOT, "reports")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def buy_type(signals):
    s = set(signals or [])
    if s & {"二波启动", "均线发散", "N字回踩"}:
        return "回踩后再起"
    if s & {"均线多头", "海龟突破", "稳健上行", "低ATR慢牛"}:
        return "多头趋势突破"
    if s & {"低位首板", "阶段新高突破"}:
        return "突破启动"
    return "其它"


def _merge(data):
    """合并 bull + strategies + recommend.trend 去重，附加趋势加速维度，按评分降序。

    recommend.trend（engine.screen_uptrend 产出）本身即「上升趋势中的介入机会」：
    已处于「加速上行」的票直接作为买点候选并优先推荐（用户 2026-08-31 需求）。
    """
    merged = {}
    # 交叉引用多源逐票「卖出/止盈/离场/减仓/逼近卖出」动作，标记矛盾买点票 warn_sell：
    #  - recommend.trend.verdict.action（趋势引擎实判）
    #  - recommend.watch_reco.items.action（自选/持仓操作结论，持仓票止盈尤其关键）
    #  - zones.items.action（买卖区间「逼近卖出」；注意仅 action 命中才算，
    #    仅存在 sell_zone 但 action=正常持有 不算矛盾，如冠龙节能）
    # 矛盾票保留可见但打 warn_sell：前端黄标⚠区分、排序沉底、推送剔除（不把卖出票当买点推荐）。
    _conflict = {}  # code -> [(src, action), ...]
    for _t in ((data.get("recommend") or {}).get("trend") or []):
        _v = _t.get("verdict")
        _act = _v.get("action") if isinstance(_v, dict) else None
        if _act:
            _conflict.setdefault(_t.get("code"), []).append(("trend", _act))
    for _x in ((data.get("recommend") or {}).get("watch_reco") or {}).get("items") or []:
        _act = _x.get("action")
        if _act:
            _conflict.setdefault(_x.get("code"), []).append(("watch", _act))
    for _z in ((data.get("zones") or {}).get("items") or []):
        _act = _z.get("action")
        if _act:
            _conflict.setdefault(_z.get("code"), []).append(("zone", _act))
    # 来源1/2：bull（回踩后再起） + strategies（多头突破）
    for src in ("bull", "strategies"):
        for x in (data.get(src) or []):
            code = x.get("code")
            if not code:
                continue
            bt = buy_type(x.get("signals"))
            rec = merged.get(code)
            if rec is None:
                merged[code] = {
                    "code": code,
                    "name": x.get("name", ""),
                    "signals": list(x.get("signals") or []),
                    "price": x.get("price"),
                    "pct": x.get("pct"),
                    "vol_ratio": x.get("vol_ratio"),
                    "ind": x.get("ind", ""),
                    "dd60": x.get("dd60"),
                    "score": x.get("score") or 0,
                    "tags": x.get("tags", ""),
                    "btype": bt,
                    "src": src,
                    "trend_state": None,
                    "accel": None,
                    "accel_flag": False,
                }
            else:
                rec["signals"] = list(set(rec["signals"] + list(x.get("signals") or [])))
                rec["score"] = max(rec["score"], x.get("score") or 0)
                rec["btype"] = bt if bt != "其它" else rec["btype"]
                rec["tags"] = rec["tags"] or x.get("tags", "")
    # 来源3：recommend.trend（趋势向上选股）—— 加速上行即买点，优先推荐
    for x in ((data.get("recommend") or {}).get("trend") or []):
        code = x.get("code")
        if not code:
            continue
        tm = x.get("trend_meta") or {}
        ts = tm.get("trend_state")
        ac = tm.get("accel")
        flag = (ts == "加速上行") or (ac or 0) >= 1.45
        bt = "主升加速" if ts == "加速上行" else "趋势多头"
        rec = merged.get(code)
        if rec is None:
            merged[code] = {
                "code": code,
                "name": x.get("name", ""),
                "signals": [bt],
                "price": x.get("close"),
                "pct": None,
                "vol_ratio": tm.get("vol_ratio"),
                "ind": x.get("industry") or "",
                "dd60": None,
                "score": x.get("score") or 0,
                "tags": "；".join((x.get("reasons") or [])[:3]),
                "btype": bt,
                "src": "trend",
                "trend_state": ts,
                "accel": ac,
                "accel_flag": flag,
            }
        else:
            # 已在 bull/strategies：叠加趋势加速维度，升格为优先
            rec["trend_state"] = ts
            rec["accel"] = ac
            rec["accel_flag"] = flag
            rec["signals"] = list(set(rec["signals"] + [bt]))
            rec["tags"] = rec["tags"] or "；".join((x.get("reasons") or [])[:3])
    # 标记「已临卖点」矛盾票（任一类 sell 动作命中即算）
    for _rec in merged.values():
        _acts = _conflict.get(_rec["code"], [])
        _sells = [a for _s, a in _acts if _SELL_RE.search(a)]
        _rec["live_action"] = _sells[0] if _sells else ""
        _rec["warn_sell"] = bool(_sells)
        _rec["warn_src"] = ",".join(sorted({_s for _s, a in _acts if _SELL_RE.search(a)}))
    return sorted(merged.values(), key=lambda r: -r["score"])


def build_data(data):
    """返回结构化买点数据，由 build.py 注入 data["buy_points"]（自动进加密 bin + 本地 data.js）。

    分组：accel（趋势加速优先）/ others（其他买点）；附缠论买点。供前端原生渲染与推送复用。
    """
    date = (data.get("meta") or {}).get("date", "") or ""
    merged = _merge(data)
    # warn_sell（已临卖点）矛盾票沉到各组末尾，纯买点排最前
    accel = sorted([r for r in merged if r.get("accel_flag")],
                   key=lambda r: (1 if r.get("warn_sell") else 0, -(r.get("score") or 0)))
    others = sorted([r for r in merged if not r.get("accel_flag")],
                    key=lambda r: (1 if r.get("warn_sell") else 0, -(r.get("score") or 0)))
    chan = [c for c in ((data.get("chanlun") or {}).get("buys") or []) if c.get("signal")]
    return {
        "date": date,
        "accel": accel,
        "others": others,
        "chanlun": chan,
        "total": len(merged) + len(chan),
        "accel_count": len(accel),
    }


def _row_a(r):
    """买点候选行（回踩后再起 / 多头突破）。"""
    pct = r["pct"]
    pcls = "up" if (pct or 0) >= 0 else "down"
    pct_s = ("+%.2f%%" % pct) if pct is not None else "—"
    dd = r["dd60"]
    dd_s = ("%.1f%%" % dd) if dd is not None else "—"
    # 趋势加速徽标
    if r.get("warn_sell"):
        badge = "<span class='badge warn'>⚠ 已临卖点 %s</span>" % esc(r.get("live_action") or "卖出")
        row_cls = " warn-row"
    elif r.get("accel_flag"):
        ts = r.get("trend_state") or "加速上行"
        ac = r.get("accel")
        ac_s = ("%.2f" % ac) if ac is not None else ""
        badge = "<span class='badge accel'>🚀 %s%s</span>" % (esc(ts), (" ×%s" % ac_s) if ac_s else "")
        row_cls = ""
    elif r.get("trend_state"):
        badge = "<span class='badge tstate'>%s</span>" % esc(r["trend_state"])
        row_cls = ""
    else:
        badge = "<span class='badge none'>—</span>"
        row_cls = ""
    return (
        "<tr class='%s'><td class='nm'>%s</td><td class='cd'>%s</td>"
        "<td><span class='bt'>%s</span></td>"
        "<td>%.2f</td><td class='%s'>%s</td><td>%.2f</td>"
        "<td>%s</td><td>%s</td><td class='sc'>%.1f</td>"
        "<td>%s</td><td class='feat'>%s</td></tr>"
        % (row_cls, esc(r["name"]), esc(r["code"]), esc(r["btype"]),
           r["price"] or 0, pcls, pct_s, r["vol_ratio"] or 0,
           esc(r["ind"]), dd_s, r["score"],
           badge, esc((r["tags"] or "")[:90]))
    )


def _render(data, date):
    trend_buy = _merge(data)
    accel = [r for r in trend_buy if r.get("accel_flag")]
    others = [r for r in trend_buy if not r.get("accel_flag")]
    chan = [c for c in ((data.get("chanlun") or {}).get("buys") or []) if c.get("signal")]

    rows_accel = "\n".join(_row_a(r) for r in accel)
    rows_others = "\n".join(_row_a(r) for r in others)
    rows_b = []
    for c in chan:
        zs = c.get("zhongshu")
        zs_s = ("[%.2f, %.2f]" % (zs[0], zs[1])) if zs else "—"
        rows_b.append(
            "<tr><td class='nm'>%s</td><td class='cd'>%s</td>"
            "<td><span class='bt'>%s</span></td><td>%s</td>"
            "<td>%.2f</td><td class='feat'>%s</td></tr>"
            % (esc(c.get("name")), esc(c.get("code")), esc(c.get("signal")),
               zs_s, c.get("last_close") or 0, esc(c.get("reason", "")[:60]))
        )
    chan_rows = "\n".join(rows_b)

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>买点候选 · 上升趋势中的介入机会 %s</title>
<style>
* { box-sizing: border-box; }
body { margin:0; background:#0a0e1a; color:#c9d4e3;
  font-family:-apple-system,"Segoe UI","Microsoft YaHei",monospace; padding:22px; }
h1 { color:#39d0ff; font-size:20px; margin:0 0 4px; letter-spacing:1px; }
.sub { color:#6b7a90; font-size:12px; margin-bottom:14px; }
.note { background:#10203a; border-left:3px solid #39d0ff; padding:10px 14px;
  border-radius:6px; font-size:12.5px; color:#9fb3c8; line-height:1.7; margin-bottom:18px; }
.note b { color:#39d0ff; }
.sec { font-size:15px; margin:18px 0 8px; font-weight:700; }
.sec.accel { color:#ff7a45; }
.sec.other { color:#ffcf5c; }
.sec.chan { color:#5fd0ff; }
table { width:100%%; border-collapse:collapse; font-size:12px; margin-bottom:8px; }
th { text-align:left; color:#6b7a90; font-weight:600; padding:7px 8px;
  border-bottom:1px solid #1d2c44; }
td { padding:7px 8px; border-bottom:1px solid #13203a; vertical-align:top; }
.nm { color:#e8f0fb; font-weight:600; }
.cd { color:#5b6b82; font-family:monospace; }
.bt { background:#13314e; color:#5fd0ff; padding:1px 7px; border-radius:10px;
  font-size:11px; white-space:nowrap; }
.up { color:#ff5b5b; font-weight:600; }      /* 涨=红 */
.down { color:#3fcf6b; font-weight:600; }    /* 跌=绿 */
.sc { color:#ffcf5c; font-weight:600; }
.feat { color:#8aa0b8; font-size:11px; line-height:1.5; }
.foot { color:#5b6b82; font-size:11px; margin-top:20px; line-height:1.6; }
.badge { font-size:11px; padding:1px 7px; border-radius:10px; white-space:nowrap; }
.badge.accel { background:#3a1c10; color:#ff7a45; border:1px solid #5a2a16; font-weight:700; }
.badge.tstate { background:#10203a; color:#9fb3c8; }
.badge.warn { background:#3a2e0a; color:#ffcf5c; border:1px solid #5a4a16; font-weight:700; }
.badge.none { color:#5b6b82; }
tr.accel-row { background:rgba(255,122,69,0.06); }
tr.warn-row { background:rgba(255,207,92,0.07); }
</style></head><body>
<h1>◈ 买点候选 · 上升趋势中的介入机会</h1>
<div class="sub">数据日期 %s · 来源 stock-analysis 站点收盘快照 · 量化信号筛选</div>
<div class="note">
  <b>筛选逻辑</b>：从「二波启动 / 均线发散 / 均线多头 / 海龟突破 / 稳健上行 / 缠论买点」中提炼——
  全部为<b>上升结构中的买点</b>（回踩企稳后再起、多头刚突破、下跌末端底背驰），而非已大涨的卖点票。<br>
  <b>趋势加速优先</b>：交叉引用趋势双态引擎，对<b>已处于「加速上行」</b>的买点候选打 🚀 徽标并排到最前，
  优先推荐正在主升加速的票；其余按评分降序。<br>
  <b>非买卖建议</b>，仅作选股线索；介入仍需结合次日竞价（高开≥2%%才跟进、低开≤-2%%放弃）与量能确认。
</div>

<div class="sec accel">① 趋势加速优先 · 主升加速中的买点（共 %d 只 🚀）</div>
<table>
<tr><th>名称</th><th>代码</th><th>买点类型</th><th>现价</th><th>涨跌幅</th><th>量比</th><th>行业</th><th>距60高</th><th>评分</th><th>趋势状态</th><th>关键特征</th></tr>
%s
</table>

<div class="sec other">② 其他买点（回踩后再起 / 多头突破，共 %d 只）</div>
<table>
<tr><th>名称</th><th>代码</th><th>买点类型</th><th>现价</th><th>涨跌幅</th><th>量比</th><th>行业</th><th>距60高</th><th>评分</th><th>趋势状态</th><th>关键特征</th></tr>
%s
</table>

<div class="sec chan">③ 缠论结构买点（一/二/三买，共 %d 只）</div>
<table>
<tr><th>名称</th><th>代码</th><th>买点</th><th>中枢区间</th><th>现价</th><th>说明</th></tr>
%s
</table>

<div class="foot">
  生成：stock-analysis · gen_buypoint_report.py ｜ 数据基准 %s 收盘<br>
  涨跌幅按 A股惯例 红涨绿跌。买点信号由 bull / strategies / chanlun 引擎产出，趋势加速维度由
  recommend.trend（engine.screen_uptrend）交叉引用。存在假突破与背驰失效风险，务必次日竞价验证。
</div>
</body></html>
""" % (date, date, len(accel), rows_accel, len(others), rows_others,
       len(chan), chan_rows, date)
    return html


def generate(data, out_dir=None):
    """从分析结果 dict 生成买点候选报告。返回生成的稳定名路径（dist/trend_buy_points.html）。"""
    if out_dir is None:
        out_dir = OUT
    date = (data.get("meta") or {}).get("date", "") or ""
    html = _render(data, date)
    os.makedirs(out_dir, exist_ok=True)
    # 稳定名（线上站点直达）
    stable = os.path.join(out_dir, "trend_buy_points.html")
    open(stable, "w", encoding="utf-8").write(html)
    # 按日归档
    if date:
        arch = os.path.join(out_dir, "trend_buy_points_%s.html" % date)
        open(arch, "w", encoding="utf-8").write(html)
    return stable


def main():
    d = json.load(open(SNAP, encoding="utf-8"))
    path = generate(d)
    bd = build_data(d)
    print("写出生买点报告:", path)
    print("趋势加速优先 %d 只 | 其他买点 %d 只 | 缠论买点 %d 只"
          % (len(bd["accel"]), len(bd["others"]), len(bd["chanlun"])))
    print("Top5:", ", ".join("%s(%.1f)" % (r["name"], r["score"]) for r in _merge(d)[:5]))


if __name__ == "__main__":
    main()
