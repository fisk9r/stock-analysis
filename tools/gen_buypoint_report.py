#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从分析结果数据(或线上解密快照 live_dump.json) 挖掘「上升趋势中处于买点」的票。

不按"趋势"标签呈现，按买点信号归类：
  - bull（二波启动 / 均线发散 / N字回踩）= 上升途中回踩企稳后再起 → 买点
  - strategies（均线多头 / 海龟突破 / 稳健上行 / 低ATR慢牛）= 多头趋势刚突破 → 买点
  - chanlun.buys（一/二/三买）= 缠论结构买点
zones 多为持仓管理(正常持有/逼近卖出)，不纳入新挖掘。

输出：dist/trend_buy_points.html（稳定名，线上站点可直达）+ dist/trend_buy_points_<date>.html（按日归档）
用法：
  - 每日构建(build.py)自动调用 generate(data, DIST) 产出线上报告；
  - 调试：node tools/live_inspect.js owner <pass> --deep 后 python tools/gen_buypoint_report.py
"""
import json
import os

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
    """合并 bull + strategies 去重，返回按评分降序的列表。"""
    merged = {}
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
                }
            else:
                rec["signals"] = list(set(rec["signals"] + list(x.get("signals") or [])))
                rec["score"] = max(rec["score"], x.get("score") or 0)
                rec["btype"] = bt if bt != "其它" else rec["btype"]
                rec["tags"] = rec["tags"] or x.get("tags", "")
    return sorted(merged.values(), key=lambda r: -r["score"])


def _render(data, date):
    trend_buy = _merge(data)
    chan = (data.get("chanlun") or {}).get("buys") or []
    chan = [c for c in chan if c.get("signal")]

    rows_a = []
    for r in trend_buy:
        pct = r["pct"]
        pcls = "up" if (pct or 0) >= 0 else "down"
        pct_s = ("+%.2f%%" % pct) if pct is not None else "—"
        dd = r["dd60"]
        dd_s = ("%.1f%%" % dd) if dd is not None else "—"
        rows_a.append(
            "<tr><td class='nm'>%s</td><td class='cd'>%s</td>"
            "<td><span class='bt'>%s</span></td>"
            "<td>%.2f</td><td class='%s'>%s</td><td>%.2f</td>"
            "<td>%s</td><td>%s</td><td class='sc'>%.1f</td>"
            "<td class='feat'>%s</td></tr>"
            % (esc(r["name"]), esc(r["code"]), esc(r["btype"]),
               r["price"] or 0, pcls, pct_s, r["vol_ratio"] or 0,
               esc(r["ind"]), dd_s, r["score"],
               esc((r["tags"] or "")[:90]))
        )
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
.sec { color:#ffcf5c; font-size:15px; margin:18px 0 8px; font-weight:700; }
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
.tag { color:#39d0ff; }
</style></head><body>
<h1>◈ 买点候选 · 上升趋势中的介入机会</h1>
<div class="sub">数据日期 %s · 来源 stock-analysis 站点收盘快照 · 量化信号筛选</div>
<div class="note">
  <b>筛选逻辑</b>：从「二波启动 / 均线发散 / 均线多头 / 海龟突破 / 稳健上行 / 缠论买点」中提炼——
  全部为<b>上升结构中的买点</b>（回踩企稳后再起、多头刚突破、下跌末端底背驰），
  而非已大涨的卖点票。归类按<b>买点信号</b>而非"趋势"标签呈现，便于盘前选股。<br>
  <b>非买卖建议</b>，仅作选股线索；介入仍需结合次日竞价（高开≥2%%才跟进、低开≤-2%%放弃）与量能确认。
</div>

<div class="sec">① 回踩后再起 / 多头趋势突破（按评分降序，共 %d 只）</div>
<table>
<tr><th>名称</th><th>代码</th><th>买点类型</th><th>现价</th><th>涨跌幅</th><th>量比</th><th>行业</th><th>距60高</th><th>评分</th><th>关键特征</th></tr>
%s
</table>

<div class="sec">② 缠论结构买点（一/二/三买，共 %d 只）</div>
<table>
<tr><th>名称</th><th>代码</th><th>买点</th><th>中枢区间</th><th>现价</th><th>说明</th></tr>
%s
</table>

<div class="foot">
  生成：stock-analysis · gen_buypoint_report.py ｜ 数据基准 %s 收盘<br>
  涨跌幅按 A股惯例 红涨绿跌。买点信号由 bull / strategies / chanlun 引擎产出，存在假突破与背驰失效风险，务必次日竞价验证。
</div>
</body></html>
""" % (date, date, len(trend_buy), "\n".join(rows_a), len(chan), "\n".join(rows_b), date)
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
    print("写出生买点报告:", path)
    print("趋势买点(合并去重) %d 只 | 缠论买点 %d 只" % (
        len(_merge(d)), len([c for c in ((d.get("chanlun") or {}).get("buys") or []) if c.get("signal")])))
    print("Top5:", ", ".join("%s(%.1f)" % (r["name"], r["score"]) for r in _merge(d)[:5]))


if __name__ == "__main__":
    main()
