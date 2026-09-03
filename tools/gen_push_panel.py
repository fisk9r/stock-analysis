# -*- coding: utf-8 -*-
"""推送信息中心 · 可视化面板生成器。

读取两类推送账本并合并：
  - dist/push_log.jsonl        （pipeline/notifier.py 写入：盘前/竞价/收盘/复盘/异动/恐慌/止损/周末）
  - tools/executor/state/sim_push_log.jsonl  （executor 模拟盘操作类推送，随 Release 资产跨 run 累积）

输出一个【自包含】的 HUD 风格 HTML 面板 dist/push_panel.html：
  - 数据内嵌（双击即看，无需服务器、不依赖网络），可直接发给老板/自己本地查阅；
  - 因含推送正文（可能含持仓信息），该文件默认不发布到公网站点（见 .gitignore）。

用法：
  python tools/gen_push_panel.py                 # 生成 dist/push_panel.html
  python tools/gen_push_panel.py --open          # 生成并调用默认浏览器打开
"""
import argparse
import json
import os
import sys
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


# mode -> (中文标签, 主色)
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
CHANNEL_LABEL = {"serverchan": "ServerChan", "pushplus": "PushPlus",
                 "专属通道": "专属通道", "wechat_serverchan": "ServerChan",
                 "wechat_pushplus": "PushPlus"}


def _mode_meta(mode):
    return MODE_META.get(mode, ("其他·%s" % mode, "#64748b"))


def build():
    recs = _load(os.path.join(ROOT, "dist", "push_log.jsonl"))
    recs += _load(os.path.join(ROOT, "tools", "executor", "state", "sim_push_log.jsonl"))
    # 去重：同一 (ts, mode, title) 只留一条
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


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>📡 推送信息中心</title>
<style>
  :root{
    --bg:#0a0e14; --panel:#111722; --panel2:#0d131c; --line:#1e2a3a;
    --txt:#c7d2e0; --dim:#7d8da3; --cyan:#19c3d6; --green:#0a8f3c; --red:#e02020;
    --gold:#f59e0b; --purple:#8b5cf6; --blue:#2f6fed;
  }
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 600px at 70% -10%,#13202f 0%,var(--bg) 60%);
    color:var(--txt);font-family:'SF Mono',ui-monospace,'Cascadia Code',Consolas,Menlo,monospace;
    font-size:13px;line-height:1.55}
  header{padding:18px 22px 10px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,#0e1622,#0a0e14)}
  h1{margin:0;font-size:18px;letter-spacing:1px;color:#eaf2ff}
  h1 .dot{color:var(--cyan)}
  .sub{color:var(--dim);font-size:11px;margin-top:3px}
  .stats{display:flex;flex-wrap:wrap;gap:10px;margin:14px 22px 0}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:9px 13px;min-width:96px}
  .stat .n{font-size:20px;font-weight:700;color:#fff}
  .stat .l{font-size:10.5px;color:var(--dim);margin-top:2px;letter-spacing:.5px}
  .controls{display:flex;flex-wrap:wrap;gap:10px;margin:16px 22px 6px;align-items:center}
  input,select{background:var(--panel2);border:1px solid var(--line);color:var(--txt);
    border-radius:7px;padding:7px 10px;font-family:inherit;font-size:12.5px;outline:none}
  input:focus,select:focus{border-color:var(--cyan)}
  #q{width:260px}
  .legend{color:var(--dim);font-size:11px;margin-left:auto}
  .legend b{color:var(--cyan)}
  .wrap{margin:8px 22px 40px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:11px;
    margin-bottom:12px;overflow:hidden;transition:.15s}
  .card:hover{border-color:#2b3d54}
  .chead{display:flex;align-items:center;gap:10px;padding:11px 14px;cursor:pointer}
  .badge{font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px;color:#06121f;white-space:nowrap}
  .ts{color:var(--dim);font-size:11.5px;font-variant-numeric:tabular-nums}
  .ttl{font-weight:700;color:#eaf2ff;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .arrow{color:var(--dim);transition:.2s;font-size:12px}
  .card.open .arrow{transform:rotate(90deg)}
  .chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 11px}
  .chip{font-size:10.5px;padding:2px 8px;border-radius:6px;border:1px solid var(--line);
    background:var(--panel2);color:var(--dim)}
  .chip.r{color:#cfe9ff;border-color:#23415e}
  .chip.r.star{color:#ffd66b;border-color:#5a4a16}
  .chip.c{color:#bfe9d4;border-color:#1f4a39}
  .body{display:none;padding:4px 14px 14px;border-top:1px solid var(--line);max-height:60vh;overflow:auto}
  .card.open .body{display:block}
  .body h1,.body h2,.body h3{color:#eaf2ff;margin:14px 0 6px;line-height:1.3}
  .body h1{font-size:16px;border-left:3px solid var(--cyan);padding-left:9px}
  .body h2{font-size:14.5px;border-left:3px solid var(--blue);padding-left:8px}
  .body h3{font-size:13px;border-left:3px solid var(--purple);padding-left:7px}
  .body p{margin:7px 0}
  .body ul{margin:7px 0;padding-left:18px}
  .body li{margin:3px 0}
  .body blockquote{margin:8px 0;padding:6px 11px;border-left:3px solid var(--gold);
    background:rgba(245,158,11,.07);color:#e7d3a8;border-radius:0 6px 6px 0}
  .body code{background:#0c1420;border:1px solid var(--line);padding:1px 5px;border-radius:4px;
    color:#9fe0c0;font-size:12px}
  .body strong{color:#fff}
  .empty{color:var(--dim);text-align:center;padding:50px;font-size:13px}
  .foot{color:var(--dim);font-size:10.5px;text-align:center;padding:18px}
  mark{background:#3a2d00;color:#ffd66b;padding:0 2px;border-radius:3px}
</style>
</head>
<body>
<header>
  <h1><span class="dot">●</span> 推送信息中心</h1>
  <div class="sub">A股盘后分析系统 · 推送可视化面板 · 本地自包含视图（数据内嵌）</div>
</header>
<div class="stats" id="stats"></div>
<div class="controls">
  <input id="q" placeholder="🔍 搜索标题 / 正文 / 代码…">
  <select id="fMode"><option value="">全部类型</option></select>
  <select id="fRecv"><option value="">全部接收人</option></select>
  <span class="legend">类型色：<b>蓝</b>=盘前 <b>青</b>=竞价 <b>紫</b>=收盘 <b>橙</b>=异动 <b>红</b>=风险 <b style="color:var(--green)">绿</b>=模拟盘</span>
</div>
<div class="wrap" id="list"></div>
<div class="foot">由 tools/gen_push_panel.py 生成 · 数据来自 dist/push_log.jsonl 与 executor/state/sim_push_log.jsonl</div>

<script id="push-data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('push-data').textContent);
const MODE_LABEL = {__MODE_LABEL__};
const CH_LABEL = {"serverchan":"ServerChan","pushplus":"PushPlus","专属通道":"专属通道","wechat_serverchan":"ServerChan","wechat_pushplus":"PushPlus"};
const MODE_COLOR = {__MODE_COLOR__};

function esc(s){return (s||"").replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function mdInline(s){
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/`([^`]+?)`/g,'<code>$1</code>');
}
function mdToHtml(md){
  const lines=(md||'').split('\n'); let h='',inCode=false,code='',ul=false,par=[];
  const flushP=()=>{if(par.length){h+='<p>'+mdInline(par.join(' '))+'</p>';par=[];}};
  const flushUl=()=>{if(ul){h+='</ul>';ul=false;}};
  for(let ln of lines){
    if(/^```/.test(ln.trim())){
      if(inCode){h+='<pre style="background:#0c1420;border:1px solid var(--line);padding:10px;border-radius:7px;overflow:auto"><code>'+esc(code)+'</code></pre>';code='';inCode=false;}
      else{flushP();flushUl();inCode=true;}
      continue;
    }
    if(inCode){code+=ln+'\n';continue;}
    if(/^\s*$/.test(ln)){flushP();flushUl();continue;}
    let m;
    if(m=ln.match(/^###\s+(.*)/)){flushP();flushUl();h+='<h3>'+mdInline(m[1])+'</h3>';continue;}
    if(m=ln.match(/^##\s+(.*)/)){flushP();flushUl();h+='<h2>'+mdInline(m[1])+'</h2>';continue;}
    if(m=ln.match(/^#\s+(.*)/)){flushP();flushUl();h+='<h1>'+mdInline(m[1])+'</h1>';continue;}
    if(m=ln.match(/^>\s?(.*)/)){flushP();flushUl();h+='<blockquote>'+mdInline(m[1])+'</blockquote>';continue;}
    if(m=ln.match(/^[-•]\s+(.*)/)){flushP();if(!ul){h+='<ul>';ul=true;}h+='<li>'+mdInline(m[1])+'</li>';continue;}
    flushUl();par.push(ln);
  }
  flushP();flushUl();if(inCode){h+='<pre><code>'+esc(code)+'</code></pre>';}
  return h;
}

// 统计 + 下拉填充
const modes={},recv={};
DATA.forEach(r=>{
  modes[r.mode]=(modes[r.mode]||0)+1;
  (r.recipients||[]).forEach(x=>{const n=x.name||'?';recv[n]=(recv[n]||0)+1;});
});
const modeSel=document.getElementById('fMode');
Object.keys(modes).sort((a,b)=>modes[b]-modes[a]).forEach(m=>{
  const o=document.createElement('option');o.value=m;o.textContent=(MODE_LABEL[m]||m)+' ('+modes[m]+')';
  modeSel.appendChild(o);
});
const recvSel=document.getElementById('fRecv');
Object.keys(recv).sort().forEach(n=>{
  const o=document.createElement('option');o.value=n;o.textContent=n+' ('+recv[n]+')';
  recvSel.appendChild(o);
});

const today=new Date().toISOString().slice(0,10);
const todayN=DATA.filter(r=>(r.ts||'').startsWith(today)).length;
const stats=[
  ['总推送',DATA.length],['今日',todayN],
  ['类型数',Object.keys(modes).length],['接收人',Object.keys(recv).length],
];
document.getElementById('stats').innerHTML=stats.map(([l,n])=>
  '<div class="stat"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>').join('');

function recvChips(rs){
  return (rs||[]).map(x=>{
    const ch=CH_LABEL[x.channel]||x.channel||'';
    const star=x.personalized?' star':'';
    const tag=x.scope&&x.scope!=='all'?(' ·'+x.scope):'';
    return '<span class="chip r'+star+'">'+(x.name||'?')+' · '+ch+(x.personalized?' ⭐':'')+'</span>';
  }).join('');
}
function chanChips(cs){
  return (cs||[]).map(c=>'<span class="chip c">'+(CH_LABEL[c]||c)+'</span>').join('');
}

function render(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  const fm=document.getElementById('fMode').value;
  const fr=document.getElementById('fRecv').value;
  const list=document.getElementById('list');
  let html='';let shown=0;
  DATA.forEach((r,i)=>{
    const mode=r.mode||'';
    if(fm&&mode!==fm)return;
    const rs=r.recipients||[];
    if(fr&&!rs.some(x=>(x.name||'')===fr))return;
    const hay=((r.title||'')+' '+(r.text||'')+' '+(r.codes||[]).join(' ')).toLowerCase();
    if(q&&!hay.includes(q))return;
    shown++;
    const meta=MODE_COLOR[mode]||'#64748b';
    const label=MODE_LABEL[mode]||mode;
    const full=esc(r.text||'(无正文)');
    html+='<div class="card" data-i="'+i+'">'+
      '<div class="chead" onclick="this.parentNode.classList.toggle(\'open\')">'+
        '<span class="badge" style="background:'+meta+'">'+label+'</span>'+
        '<span class="ts">'+(r.ts||'')+'</span>'+
        '<span class="ttl">'+esc(r.title||'')+'</span>'+
        '<span class="arrow">▶</span>'+
      '</div>'+
      '<div class="chips">'+chanChips(r.channels)+recvChips(rs)+'</div>'+
      '<div class="body">'+mdToHtml(r.text||'')+'</div>'+
    '</div>';
  });
  if(!shown)html='<div class="empty">没有匹配的推送记录</div>';
  list.innerHTML=html;
}
document.getElementById('q').addEventListener('input',render);
document.getElementById('fMode').addEventListener('change',render);
document.getElementById('fRecv').addEventListener('change',render);
render();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    args = ap.parse_args()

    data = build()
    out = os.path.join(ROOT, "dist", "push_panel.html")

    mode_label = ",".join("%s:%s" % (json.dumps(m), json.dumps(l[0])) for m, l in MODE_META.items())
    mode_color = ",".join("%s:%s" % (json.dumps(m), json.dumps(l[1])) for m, l in MODE_META.items())
    html = HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    html = html.replace("__MODE_LABEL__", mode_label).replace("__MODE_COLOR__", mode_color)

    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)

    print("[gen_push_panel] 合并 %d 条推送记录 → %s" % (len(data), out))
    if args.open:
        webbrowser.open("file://" + os.path.abspath(out))


if __name__ == "__main__":
    main()
