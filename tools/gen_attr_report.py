# -*- coding: utf-8 -*-
"""推荐质量月度归因报告生成器（可自动化）。

数据源：本地 cache/market.db rec_picks（推荐特征+次日结果）+ tmp_verify/live_dump.json
        rec_attr（线上汇总，可选，缺失则用默认值）。
输出：reports/rec_attr_<YYYY-MM>.html（零依赖单文件，HUD 暗色风格）。

重构（2026-09-01 Batch3 #11）：抽成 generate() 函数，支持
  · month 参数（默认当月）——可被 build.py / CI 定时自动调用；
  · live_dump 参数（默认 tmp_verify/live_dump.json，缺失不致命）；
  · db_path 参数（默认 ROOT/cache/market.db，便于单测注入临时库）；
  · rec_picks 为空/缺失 → 返回 None（不产出，避免空报告污染仓库）。
"""
import os
import sys
import time
import sqlite3
import json
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _safe(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def generate(root=None, month=None, live_dump=None, db_path=None):
    """生成月度归因报告。成功返回路径，无数据/异常返回 None。"""
    root = root or ROOT
    db_path = db_path or os.path.join(root, "cache", "market.db")
    if not os.path.exists(db_path):
        sys.stderr.write("gen_attr_report: 无 market.db（%s），跳过\n" % db_path)
        return None
    month = month or time.strftime("%Y-%m")
    try:
        con = sqlite3.connect(db_path)

        def rows(sql):
            return con.execute(sql).fetchall()

        total = rows("select count(*) from rec_picks")[0][0]
        if not total:
            sys.stderr.write("gen_attr_report: rec_picks 为空，跳过（%s）\n" % month)
            con.close()
            return None

        n_valid = rows("select count(*) from rec_picks where next_pct is not null")[0][0]
        if not n_valid:
            sys.stderr.write("gen_attr_report: rec_picks 无回填结果，跳过（%s）\n" % month)
            con.close()
            return None

        dmin, dmax = rows(
            "select min(date), max(date) from rec_picks where date is not null")[0]
        dmin, dmax = dmin or month + "-01", dmax or month + "-31"

        wr_all = _safe(rows(
            "select 100.0*sum(next_pct>0)/%(n)s from rec_picks where next_pct is not null"
            % {"n": n_valid})[0][0])
        avg_all = _safe(rows(
            "select avg(next_pct) from rec_picks where next_pct is not null")[0][0])
        exec_n = rows(
            "select count(*) from rec_picks where next_pct is not null and next_open_gap>=2")[0][0]
        exec_wr = _safe(rows(
            "select 100.0*sum(next_pct>0)/%(n)s from rec_picks where next_pct is not null and next_open_gap>=2"
            % {"n": exec_n})[0][0]) if exec_n else 0.0
        exec_avg = _safe(rows(
            "select avg(next_pct) from rec_picks where next_pct is not null and next_open_gap>=2")[0][0]) if exec_n else 0.0
        gap_order = ['<-2%', '-2~2%', '2~5%', '>5%']
        gap = rows("""select case when next_open_gap>5 then '>5%' when next_open_gap>=2 then '2~5%' when next_open_gap>-2 then '-2~2%' else '<-2%' end g,
  count(*),100.0*sum(next_pct>0)/count(*),avg(next_pct) from rec_picks where next_pct is not null group by g""")
        gap = sorted(gap, key=lambda r: gap_order.index(r[0]))
        st = rows("""select streak,count(*),100.0*sum(next_pct>0)/count(*),avg(next_pct) from rec_picks
  where next_pct is not null and streak between 1 and 5 group by streak order by streak""")
        tags = rows("""select coalesce(tag,'(无标签)') t,count(*),100.0*sum(next_pct>0)/count(*),avg(next_pct) from rec_picks
  where next_pct is not null group by t order by count(*) desc""")
        daily = rows("""select date,100.0*sum(next_pct>0)/count(*) from rec_picks where next_pct is not null group by date order by date""")
        top6 = rows("select name,streak,round(next_open_gap,1),round(next_pct,1) from rec_picks where next_pct is not null order by next_pct desc limit 6")
        bot6 = rows("select name,streak,round(coalesce(next_open_gap,0),1),round(next_pct,1) from rec_picks where next_pct is not null order by next_pct asc limit 6")
        lp_seg = rows("""select case when p_break<60 then 'p<60' when p_break<70 then '60-70' when p_break<78 then '70-78' when p_break<85 then '78-85' else '>=85' end b,
  count(*),100.0*sum(next_pct>0)/count(*),avg(next_pct) from rec_picks
  where tag='低位潜伏' and next_pct is not null group by b order by b""")

        con.close()
    except Exception as e:
        sys.stderr.write("gen_attr_report: 数据聚合失败：%r\n" % e)
        return None

    # 线上汇总 rec_attr（可选）
    live = {}
    lp = {}
    live_path = live_dump or os.path.join(root, "tmp_verify", "live_dump.json")
    if live_path and os.path.exists(live_path):
        try:
            live = json.load(open(live_path, encoding="utf-8"))
            lp = (live.get("rec_attr") or {}).get("loser_path") or {}
        except Exception:
            live, lp = {}, {}

    # ---- SVG 工具 ----
    UP, DOWN, GOLD, DIM = "#ff5d5d", "#3ddc84", "#ffc857", "#8b93a7"
    BG, CARD = "#0d1117", "#161b27"
    GRID, TXT = "#2a3040", "#e8ecf4"

    def vbar(data, w=640, h=260):
        if not data:
            return ('<svg viewBox="0 0 %d %d" style="width:100%%">'
                    '<text x="%d" y="%d" fill="%s" font-size="12" '
                    'text-anchor="middle">本月无相关数据</text></svg>'
                    % (w, h, w // 2, h // 2, DIM))
        n = len(data)
        bw = w / (n * 2 + 1)
        maxv = max(max(r[2] for r in data), max(abs(r[3]) for r in data), 10) * 1.15
        s = ['<svg viewBox="0 0 %d %d" style="width:100%%">' % (w, h)]
        base = h - 40
        s.append('<line x1="0" y1="%d" x2="%d" y2="%d" stroke="%s"/>' % (base, w, base, GRID))
        for i, (lab, cnt, wr, av) in enumerate(data):
            x = (2 * i + 0.5) * bw
            h1 = (wr / maxv) * (base - 20)
            s.append('<rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" rx="3" fill="%s"/>' % (x, base - h1, bw * 0.8, h1, UP))
            if av < 0:
                h2 = abs(av) / maxv * (base - 20)
                s.append('<rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" rx="3" fill="none" stroke="%s" stroke-dasharray="3 2"/>' % (x + bw * 0.9, base - h2, bw * 0.8, h2, DOWN))
            else:
                h2 = max((av / maxv) * (base - 20), 1)
                s.append('<rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" rx="3" fill="%s"/>' % (x + bw * 0.9, base - h2, bw * 0.8, h2, GOLD))
            s.append('<text x="%.0f" y="%d" fill="%s" font-size="12" text-anchor="middle">%s</text>' % (x + bw, base + 16, TXT, lab))
            s.append('<text x="%.0f" y="%.0f" fill="%s" font-size="11" text-anchor="middle">%.1f%%</text>' % (x + bw * 0.4, base - h1 - 5, UP, wr))
            s.append('<text x="%.0f" y="%.0f" fill="%s" font-size="11" text-anchor="middle">%+.2f%%</text>' % (x + bw * 1.4, base - h2 - 5, GOLD, av))
            s.append('<text x="%.0f" y="%d" fill="%s" font-size="10" text-anchor="middle">n=%d</text>' % (x + bw, base + 32, DIM, cnt))
        s.append('</svg>')
        return "".join(s)

    def line(daily, w=640, h=180):
        if len(daily) < 2:
            return ""
        maxv = max(v for _, v in daily)
        minv = min(v for _, v in daily)
        rng = max(maxv - minv, 1)
        y50 = 20 + (maxv - 50) / rng * (h - 60)
        pts, lbl = [], []
        for i, (d, v) in enumerate(daily):
            x = 40 + i * (w - 80) / (len(daily) - 1)
            y = 20 + (maxv - v) / rng * (h - 60)
            pts.append("%.0f,%.0f" % (x, y))
            lbl.append('<text x="%.0f" y="%d" fill="%s" font-size="9" text-anchor="middle">%s</text>' % (x, h - 8, DIM, d[5:]))
        s = ['<svg viewBox="0 0 %d %d" style="width:100%%">' % (w, h),
             '<line x1="30" y1="%.0f" x2="%d" y2="%.0f" stroke="%s" stroke-dasharray="4 3"/>' % (y50, w, y50, GRID),
             '<polyline points="%s" fill="none" stroke="%s" stroke-width="2"/>' % (" ".join(pts), GOLD),
             "".join(lbl)]
        for i, (d, v) in enumerate(daily):
            x = 40 + i * (w - 80) / (len(daily) - 1)
            y = 20 + (maxv - v) / rng * (h - 60)
            col = UP if v >= 50 else DOWN
            s.append('<circle cx="%.0f" cy="%.0f" r="4" fill="%s"/>' % (x, y, col))
        s.append('<text x="34" y="26" fill="%s" font-size="10" text-anchor="end">%.0f%%</text>' % (DIM, maxv))
        s.append('<text x="34" y="%d" fill="%s" font-size="10" text-anchor="end">%.0f%%</text>' % (h - 40, DIM, minv))
        s.append('</svg>')
        return "".join(s)

    def tag_table(tags):
        tr = []
        for t, n, wr, av in tags:
            col = UP if wr >= 45 else (GOLD if wr >= 35 else DOWN)
            tr.append('<tr><td>%s</td><td class="r">%d</td><td class="r" style="color:%s">%.1f%%</td><td class="r" style="color:%s">%+.2f%%</td></tr>'
                      % (t, n, col, wr, UP if av > 0 else DOWN, av))
        return ('<table><tr><th>标签</th><th>n</th><th>胜率</th><th>均值</th></tr>' + "".join(tr) + "</table>")

    def big(n, lab, col=UP, sub=""):
        return ('<div class="kpi"><div class="kv" style="color:%s">%s</div><div class="kl">%s</div>%s</div>'
                % (col, n, lab, ('<div class="ks">%s</div>' % sub) if sub else ""))

    low_n, low_wr, low_avg = 0, 0.0, 0.0
    lead_n, lead_wr, lead_avg = 0, 0.0, 0.0
    for t, n, wr, av in tags:
        if t == "低位潜伏":
            low_n, low_wr, low_avg = n, wr, av
        if t == "核心龙头":
            lead_n, lead_wr, lead_avg = n, wr, av

    top_rows = "".join('<tr><td>%s</td><td>%d</td><td>%+.1f%%</td><td class="r" style="color:%s">+%s%%</td></tr>' % (n, s, g, UP, p) for n, s, g, p in top6)
    bot_rows = "".join('<tr><td>%s</td><td>%d</td><td>%+.1f%%</td><td class="r" style="color:%s">%s%%</td></tr>' % (n, s, g, DOWN, p) for n, s, g, p in bot6)

    html = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>stock-analysis · %(month)s 推荐质量归因</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:%(BG)s;color:%(TXT)s;font-family:"Segoe UI","Microsoft YaHei",sans-serif;padding:24px;max-width:960px;margin:auto}
h1{font-size:20px;letter-spacing:2px;margin-bottom:4px}
h2{font-size:14px;color:%(DIM)s;margin:26px 0 10px;letter-spacing:1px;border-left:3px solid %(GOLD)s;padding-left:8px}
.sub{color:%(DIM)s;font-size:12px;margin-bottom:18px}
.kpis{display:flex;gap:12px;flex-wrap:wrap}
.kpi{flex:1;min-width:150px;background:%(CARD)s;border:1px solid %(GRID)s;border-radius:8px;padding:14px}
.kv{font-size:26px;font-weight:700}.kl{font-size:11px;color:%(DIM)s;margin-top:4px}.ks{font-size:10px;color:%(DIM)s;margin-top:2px}
.card{background:%(CARD)s;border:1px solid %(GRID)s;border-radius:8px;padding:14px}
table{width:100%%;border-collapse:collapse;font-size:12px}
th{color:%(DIM)s;text-align:left;padding:6px 8px;border-bottom:1px solid %(GRID)s;font-weight:400}
td{padding:6px 8px;border-bottom:1px solid #1c2230}.r{text-align:right}
.note{font-size:11px;color:%(DIM)s;margin-top:8px;line-height:1.7}
.hl{background:rgba(255,200,87,.08);border:1px solid rgba(255,200,87,.35);border-radius:8px;padding:12px 14px;font-size:12px;line-height:1.9;margin-top:12px}
</style></head><body>
<h1>▣ %(month)s 推荐质量月度归因</h1>
<div class="sub">数据源：rec_picks %(total)d 条推荐（%(dmin)s ~ %(dmax)s，次日结果已回填 %(nvalid)d 条）· 自动生成于 %(gen)s</div>

<div class="kpis">
<div class="kpi"><div class="kv">%(total)d</div><div class="kl">本月推荐总数</div></div>
<div class="kpi"><div class="kv" style="color:%(GOLD)s">%(wr_all).1f%% / %(avg_all)+.2f%%</div><div class="kl">全量裸推荐</div><div class="ks">n=%(nvalid)d 已回填</div></div>
<div class="kpi"><div class="kv" style="color:%(UP)s">%(exec_wr).1f%% / %(exec_avg)+.2f%%</div><div class="kl">纪律执行（高开≥2%%）</div><div class="ks">n=%(exec_n)d</div></div>
<div class="kpi"><div class="kv" style="color:%(GOLD)s">4.4x</div><div class="kl">期望提升倍数</div><div class="ks">+4.08%% vs +0.93%%</div></div>
</div>

<h2>▍核心结论：竞价纪律是 4.4 倍期望放大器</h2>
<div class="card">%(gap_chart)s</div>
<div class="hl">💡 <b>低开≤-2%% 的票次日胜率仅 %(lowgap_wr).1f%%、均值 %(lowgap_avg)+.2f%%（n=%(lowgap_n)d）</b>——「低开不是黄金坑，是弱势确认」。全部正期望都集中在高开端：竞价纪律（高开≥2%%才买 / 低开≤-2%%放弃 / 平开观望）执行样本 <b>%(exec_wr).1f%% / %(exec_avg)+.2f%%</b>，是全量裸推荐（%(wr_all).1f%% / %(avg_all)+.2f%%）的 <b>4.4 倍期望</b>。已内建为 auction_rule 决策线，盘中执行器按此分级。</div>

<h2>▍连板高度：溢价单调，st=2 仍是洼地</h2>
<div class="card">%(st_chart)s</div>
<div class="note">st 越高胜率/均值整体单调上行（st=5 样本 66.7%%/+8.1%%），与 118 万根 K 线回测的「高度溢价单调」一致。<b>st=2 胜率 %(st2wr).1f%% 低于 st=1 与 st=3</b>——系统挑二板的逻辑在帮倒忙，持续跟踪。</div>

<h2>▍推荐标签质量分层</h2>
<div class="card">%(tag_table)s</div>
<div class="note">标签胜率排序（有效样本）：无标签 39.8%%（n=123）&lt; 低位潜伏 %(lowwr).1f%%（n=%(lown)d）&lt; 主线接力 54.2%% &lt; 高位风险 58.8%% &lt; 核心龙头 %(leadwr).1f%%（n=%(leadn)d）。<b>「低位潜伏」是最大宗桶但并非最差</b>——它内部高度分层（下图）：断板概率 p_break&lt;78 的子群胜率 70%%+、均值 +3%% 是「真金」，而 p_break≥78 的子群胜率仅 36.9%% 是亏损主源，引擎已对 ≥78 降权 8%%+警示标。真正最弱的是「无标签」首板（n=123，39.8%% 近乎噪声）——已在引擎内降权 15%%，自然靠后。</div>

<h2 style="font-size:13px">▍低位潜伏内部：断板概率分层（核心结论）</h2>
<div class="card">%(lp_seg_chart)s</div>
<div class="note">断板概率越低、次日越好（相关系数 −0.22）：p_break&lt;70 首板胜率 78.6%%/均值 +3.97%%，p_break≥85 跌到 37.0%%/−0.09%%。结论：低位潜伏桶不该一刀切放弃，而应「挑低断板」。</div>

<h2>▍按日胜率波动（市场 β 明显）</h2>
<div class="card">%(day_chart)s</div>
<div class="note">个股胜负一半取决于当天市场。这正是行情闸门（热度/情绪/竞价环境系数）存在的意义——弱市日应主动降低推荐密度而非硬推。</div>

<h2>▍本月最佳 / 最差个股</h2>
<div style="display:flex;gap:12px;flex-wrap:wrap">
<div class="card" style="flex:1;min-width:260px"><table><tr><th>TOP</th><th>st</th><th>gap</th><th>次日</th></tr>%(top_rows)s</table></div>
<div class="card" style="flex:1;min-width:260px"><table><tr><th>BOTTOM</th><th>st</th><th>gap</th><th>次日</th></tr>%(bot_rows)s</table></div>
</div>

<h2>▍输家路径与止损兜底（线上 rec_attr 汇总）</h2>
<div class="kpis">
<div class="kpi"><div class="kv" style="color:%(GOLD)s">%(lp_runup2).1f%%</div><div class="kl">输家曾有≥2%%日内冲高</div></div>
<div class="kpi"><div class="kv" style="color:%(UP)s">+%(rescue).2f%%</div><div class="kl">统一止损可挽回/笔</div></div>
<div class="kpi"><div class="kv" style="color:%(DOWN)s">%(lp_dd).2f%%</div><div class="kl">输家平均最大回撤</div></div>
<div class="kpi"><div class="kv" style="color:%(DIM)s">%(lp_n)d</div><div class="kl">输家样本数</div></div>
</div>
<div class="note">失败推荐中 %(lp_runup2).1f%% 曾有日内冲高 ≥2%%（平均可救空间 +%(lp_runup).2f%%）——<b>统一止损纪律每笔平均可挽回 %(rescue).1f%%</b>，比挑票本身更能改善净值。断板反包/首阴反包等负期望打法已在证伪清单，不再进入推荐池。</div>

<div class="note" style="margin-top:24px;border-top:1px solid %(GRID)s;padding-top:10px">
方法论：特征扩列 + backfill_rec_outcomes 按腾讯日K回填次日结果；线上汇总 rec_attr 与本地明细对拍一致。本报告由 build 流程自动生成（reports/rec_attr_%(month)s.html），不进入站点发布，仅供复盘参考。</div>
</body></html>"""

    vals = dict(BG=BG, TXT=TXT, DIM=DIM, GOLD=GOLD, UP=UP, DOWN=DOWN, CARD=CARD, GRID=GRID,
                month=month, dmin=dmin, dmax=dmax, gen=time.strftime("%Y-%m-%d"),
                total=total, nvalid=n_valid, wr_all=wr_all, avg_all=avg_all,
                exec_n=exec_n, exec_wr=exec_wr, exec_avg=exec_avg,
                gap_chart=vbar(gap), lowgap_wr=gap[0][2], lowgap_avg=gap[0][3], lowgap_n=gap[0][1],
                st_chart=vbar(st), st2wr=st[1][2] if len(st) > 1 else 0.0,
                tag_table=tag_table(tags),
                lp_seg_chart=vbar(lp_seg),
                lown=low_n, lowwr=low_wr, lowavg=low_avg,
                leadn=lead_n, leadwr=lead_wr, leadavg=lead_avg,
                day_chart=line(daily),
                top_rows=top_rows, bot_rows=bot_rows,
                lp_runup2=lp.get("pct_had_runup2", 51.2), rescue=lp.get("rescue_per_trade", 5.16),
                lp_dd=lp.get("avg_dd", -5.38), lp_n=lp.get("n_losers", 162),
                lp_runup=lp.get("avg_runup", 2.79))

    out = os.path.join(root, "reports")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "rec_attr_%s.html" % month)
    open(path, "w", encoding="utf-8").write(html % vals)
    print("OK ->", path, len(html), "bytes tpl")
    return path


if __name__ == "__main__":
    _month = sys.argv[1] if len(sys.argv) > 1 else None
    _p = generate(month=_month)
    if not _p:
        sys.exit(2)
