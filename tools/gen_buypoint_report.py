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
    # ── 买点门禁（2026-09-03 用户拍板）：把 build.py 已算好的 entry（近端可执行
    # 买点阶梯）挂到每条候选上。来源优先级 trend > bull > strategies > zones，
    # 任一命中即用（同一只票 entry 口径一致，取到即可）。
    _emap = {}
    for _src in (((data.get("recommend") or {}).get("trend") or []),
                 (data.get("bull") or []),
                 (data.get("strategies") or []),
                 ((data.get("zones") or {}).get("items") or [])):
        for _x in _src:
            if not isinstance(_x, dict):
                continue
            _c, _e = _x.get("code"), _x.get("entry")
            if _c and _e and _c not in _emap:
                _emap[_c] = _e
    for _rec in merged.values():
        _e = _emap.get(_rec["code"])
        if not _e:
            _rec["entry_state"] = None
            _rec["buyable_now"] = None
            continue
        _rec["entry"] = _e
        _rec["entry_state"] = _e.get("entry_state")
        _rec["entry_gap_pct"] = _e.get("entry_gap_pct")
        _rec["entry_label"] = _e.get("label")
        _rec["now_zone"] = _e.get("now_zone")
        _rec["wait_price"] = _e.get("wait_price")
        _rec["wait_drop_pct"] = _e.get("wait_drop_pct")
        _rec["buyable_now"] = bool(_e.get("buyable"))
        # 现价补齐（bull/strategies 用 price，trend 用 close；统一暴露 close）
        if not _rec.get("price"):
            _rec["price"] = _e.get("close")
    for _rec in merged.values():
        _rec.setdefault("close", _rec.get("price"))
    return sorted(merged.values(), key=lambda r: -r["score"])


def build_data(data):
    """返回结构化买点数据，由 build.py 注入 data["buy_points"]（自动进加密 bin + 本地 data.js）。

    分组：accel（趋势加速优先）/ others（其他买点）；附缠论买点。供前端原生渲染与推送复用。
    """
    date = (data.get("meta") or {}).get("date", "") or ""
    merged = _merge(data)
    # ══════════════════════════════════════════════════════════════════
    # 买点硬门禁（2026-09-03 用户拍板：「我不希望再次看到没有达到买点的股票推荐，
    # 推荐给我一堆在天上的股票根本不切实际」）
    # ══════════════════════════════════════════════════════════════════
    # 「买点候选」的定义收紧为：现价确实处于/贴近短线可执行买点（可买/微超）。
    #   · 等回踩   → 移到 waiting 组（保留可见 + 明确挂单价，不混进买点）
    #   · 过热勿追 → 移到 skipped 组（默认不展示、不推送）
    #   · 无 entry 数据 → 保守放行（不误杀），但标 entry_state=None
    # warn_sell（已临卖点）矛盾票一律不算买点，直接归 skipped。
    def _bucket(r):
        if r.get("warn_sell"):
            return "skipped"
        st = r.get("entry_state")
        if st in ("可买", "微超"):
            return "buy"
        if st == "等回踩":
            return "waiting"
        if st == "过热勿追":
            return "skipped"
        return "buy"          # entry 缺失（数据不足）→ 不误杀

    _by = {"buy": [], "waiting": [], "skipped": []}
    for r in merged:
        _by[_bucket(r)].append(r)

    def _sk(r):
        # 可买优先 → 分数降序
        return (0 if r.get("entry_state") == "可买" else 1, -(r.get("score") or 0))

    buy = sorted(_by["buy"], key=_sk)
    accel = [r for r in buy if r.get("accel_flag")]
    others = [r for r in buy if not r.get("accel_flag")]
    waiting = sorted(_by["waiting"],
                     key=lambda r: (r.get("wait_drop_pct") is None,
                                    -(r.get("score") or 0)))
    skipped = sorted(_by["skipped"], key=lambda r: -(r.get("score") or 0))
    chan = [c for c in ((data.get("chanlun") or {}).get("buys") or []) if c.get("signal")]
    return {
        "date": date,
        "accel": accel,
        "others": others,
        "waiting": waiting,        # 等回踩：给挂单价，不算买点
        "skipped": skipped,        # 过热勿追 / 已临卖点：默认不展示
        "chanlun": chan,
        "total": len(buy) + len(chan),
        "accel_count": len(accel),
        "gate": {
            "buyable": len(buy),
            "waiting": len(waiting),
            "skipped": len(skipped),
            "raw": len(merged),
            "note": "买点候选=现价处于/贴近短线可执行买点；等回踩与过热勿追已剔除",
        },
    }


def _entry_cell(r):
    """近端可执行买点单元格：现价可买价带 / 需回踩到的挂单价 / 过热幅度。

    2026-09-03 用户拍板「不要在天上的票」——报告页也必须把「现价到底能不能买」
    显式写出来，而不是只给一个遥不可及的 MA20 买区。
    """
    st = r.get("entry_state")
    if not st:
        return "<span class='badge none'>—</span>"
    gp = r.get("entry_gap_pct")
    gp_s = ("%+.1f%%" % gp) if gp is not None else ""
    nz = r.get("now_zone") or []
    wp = r.get("wait_price")
    wd = r.get("wait_drop_pct")
    if st == "可买":
        z = ("%.2f~%.2f" % (nz[0], nz[1])) if len(nz) == 2 and nz[0] is not None else ""
        return "<span class='badge ok'>✅ 现价可买 %s</span>" % z
    if st == "微超":
        return "<span class='badge soft'>🟡 小仓试 %s</span>" % gp_s
    if st == "等回踩":
        s = "⏳ 等回踩 %.2f" % wp if wp else "⏳ 等回踩"
        if wd is not None:
            s += "（需回落 %.1f%%）" % abs(wd)
        return "<span class='badge wait'>%s</span>" % s
    if st == "过热勿追":
        return "<span class='badge hot'>🚫 过热勿追 %s</span>" % gp_s
    return "<span class='badge none'>%s</span>" % esc(st)


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
        "<td>%s</td><td>%s</td><td class='feat'>%s</td></tr>"
        % (row_cls, esc(r["name"]), esc(r["code"]), esc(r["btype"]),
           r["price"] or 0, pcls, pct_s, r["vol_ratio"] or 0,
           esc(r["ind"]), dd_s, r["score"],
           badge, _entry_cell(r), esc((r["tags"] or "")[:90]))
    )


def _render(data, date):
    # 2026-09-03：报告页与站点/推送共用同一份买点门禁（build_data），
    # 避免「网页说可买、报告页还在推天上的票」两套口径。
    bd = build_data(data)
    accel = bd["accel"]
    others = bd["others"]
    waiting = bd["waiting"]
    skipped = bd["skipped"]
    gate = bd["gate"]
    chan = bd["chanlun"]

    rows_accel = "\n".join(_row_a(r) for r in accel)
    rows_others = "\n".join(_row_a(r) for r in others)
    rows_wait = "\n".join(_row_a(r) for r in waiting)
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
.badge.ok { background:#3a1010; color:#ff6b6b; border:1px solid #5a1c1c; font-weight:700; }
.badge.soft { background:#3a2e0a; color:#ffd76b; border:1px solid #5a4a16; }
.badge.wait { background:#10203a; color:#5fd0ff; border:1px solid #1d3c5a; }
.badge.hot { background:#2a1030; color:#c98cff; border:1px solid #43205a; }
.sec.wait { color:#5fd0ff; }
.gate { background:#101c14; border-left:3px solid #3fcf6b; padding:9px 13px;
  border-radius:6px; font-size:12.5px; color:#9fc8ad; line-height:1.7; margin-bottom:16px; }
.gate b { color:#3fcf6b; }
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
<div class="gate">
  <b>买点硬门禁已生效</b>：原始候选 %d 只 → <b>现价可买 %d 只</b>，等回踩 %d 只（给挂单价，另列），
  过热勿追 / 已临卖点 %d 只<b>已剔除</b>。<br>
  判定口径：短线成本锚 ref = max(MA5, MA10)；现价 ≤ ref×1.03 → 可买；≤ ref×1.06 → 微超（小仓试）；
  ≤ ref×1.12 → 等回踩（挂单等）；再高 → 过热勿追。跌破止损 → 回避不接刀。
</div>

<div class="sec accel">① 趋势加速优先 · 主升加速中的买点（共 %d 只 🚀）</div>
<table>
<tr><th>名称</th><th>代码</th><th>买点类型</th><th>现价</th><th>涨跌幅</th><th>量比</th><th>行业</th><th>距60高</th><th>评分</th><th>趋势状态</th><th>近端买点</th><th>关键特征</th></tr>
%s
</table>

<div class="sec other">② 其他买点（回踩后再起 / 多头突破，共 %d 只）</div>
<table>
<tr><th>名称</th><th>代码</th><th>买点类型</th><th>现价</th><th>涨跌幅</th><th>量比</th><th>行业</th><th>距60高</th><th>评分</th><th>趋势状态</th><th>近端买点</th><th>关键特征</th></tr>
%s
</table>

<div class="sec wait">③ 趋势可以但要等回踩 · 不是现在的买点（共 %d 只 ⏳）</div>
<table>
<tr><th>名称</th><th>代码</th><th>买点类型</th><th>现价</th><th>涨跌幅</th><th>量比</th><th>行业</th><th>距60高</th><th>评分</th><th>趋势状态</th><th>挂单价</th><th>关键特征</th></tr>
%s
</table>

<div class="sec chan">④ 缠论结构买点（一/二/三买，共 %d 只）</div>
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
""" % (date, date,
       gate.get("raw", 0), gate.get("buyable", 0), gate.get("waiting", 0), len(skipped),
       len(accel), rows_accel, len(others), rows_others,
       len(waiting), rows_wait,
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
    g = bd["gate"]
    print("门禁：原始 %d → 可买 %d / 等回踩 %d / 剔除 %d"
          % (g["raw"], g["buyable"], g["waiting"], g["skipped"]))
    print("Top5:", ", ".join("%s(%.1f)" % (r["name"], r["score"]) for r in _merge(d)[:5]))


if __name__ == "__main__":
    main()
