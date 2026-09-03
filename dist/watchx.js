/* ==========================================================================
 * watchx.js — 本机自选池（免密钥）+ 智能输入纠错 + 三时相操作台 + 盘中告警
 *
 * 全局暴露 window.WLX，供 app.js 调用（接口面以 app.js 现有调用为准）：
 *   ensureStyle() inject CSS（含 .wlx-* / .wl-qk / 高亮类）
 *   bindDelegates() 全局点击委托：.wl-qk 一键加/删关注
 *   list() -> [{code,name,cost,at}]
 *   add(code,name) / del(code) -> {ok,msg}      （纯 localStorage，零密钥）
 *   resolve(input) -> {code,name,fixed,from} | null（同步，离线名录）
 *   search(v,n) -> [{code,name,score}]          （同步模糊搜索）
 *   onlineSearch(v,n) -> Promise<[{code,name}]> （腾讯 smartbox JSONP 兜底）
 *   setCost(code,cost)                          （成本价 → 止损/目标计算）
 *   marketOf(code) -> '上海'/'深圳'/'北交所'
 *   toast(msg,type)
 *   renderPanel(el) — 「我的自选 · 三时相操作台」整面板（含 30s 实时监控）
 *
 * 数据依赖：stock_names.js（window.SA_NAMES 全市场名录）、
 *          window.__STOCK_DATA__（zones/watch/recommend 提供止损止盈阈值）。
 * 行情：qt.gtimg.cn 实时接口（GBK，CORS *），浏览器直连、不落地。
 * ========================================================================== */
(function () {
  'use strict';

  var LS_KEY = 'sa_watch_local_v1';

  /* ---------------- 存储层 ---------------- */
  function _num(x) {
    if (x === '' || x == null || isNaN(+x)) return null;
    var v = +x;
    return isFinite(v) ? v : null;
  }
  function load() {
    try {
      var v = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
      if (Array.isArray(v)) {
        return v.filter(function (x) { return x && /^\d{6}$/.test(x.code || ''); })
          .map(function (x) {
            return {
              code: x.code, name: x.name || '',
              cost: _num(x.cost), qty: _num(x.qty),
              stop: _num(x.stop), target: _num(x.target),
              at: x.at || ''
            };
          });
      }
    } catch (e) {}
    return [];
  }
  function save(arr) { try { localStorage.setItem(LS_KEY, JSON.stringify(arr)); } catch (e) {} }
  function nowStr() {
    var d = new Date();
    return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
  }
  function nowHM() {
    var d = new Date();
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2);
  }

  function list() { return load(); }
  function has(code) { return load().some(function (x) { return x.code === code; }); }
  function add(code, name, extra) {
    code = String(code || '').trim();
    if (!/^\d{6}$/.test(code)) return { ok: false, msg: '代码须为 6 位数字' };
    var arr = load();
    if (arr.some(function (x) { return x.code === code; })) return { ok: true, msg: code + ' 已在关注池中' };
    extra = extra || {};
    arr.push({
      code: code, name: name || resolveName(code) || '',
      cost: _num(extra.cost), qty: _num(extra.qty),
      stop: _num(extra.stop), target: _num(extra.target),
      at: nowStr()
    });
    save(arr);
    return { ok: true, msg: '已加入关注：' + code + ' ' + (name || resolveName(code) || '') + ' ✅（本机立即生效·零密钥）' };
  }
  function del(code) {
    var arr = load();
    var hit = arr.filter(function (x) { return x.code === code; })[0];
    save(arr.filter(function (x) { return x.code !== code; }));
    return hit ? { ok: true, msg: '已移出关注：' + code + ' ' + (hit.name || '') } : { ok: false, msg: code + ' 不在关注池中' };
  }
  /* 购入股票设置：数量 / 成本价 / 止损 / 目标，逐字段合并（仅更新传入项）。
     零密钥、本机存储；未关注会自动补一条关注。 */
  function setPlan(code, plan) {
    plan = plan || {};
    code = String(code || '').trim();
    if (!/^\d{6}$/.test(code)) return { ok: false, msg: '代码须为 6 位数字' };
    var arr = load(), hit = null;
    arr.forEach(function (x) { if (x.code === code) hit = x; });
    if (!hit) {
      arr.push({ code: code, name: resolveName(code) || '', cost: null, qty: null, stop: null, target: null, at: nowStr() });
      hit = arr[arr.length - 1];
    }
    ['cost', 'qty', 'stop', 'target'].forEach(function (k) {
      if (plan[k] === undefined) return;
      hit[k] = _num(plan[k]);
    });
    save(arr);
    return { ok: true, msg: code + ' 持仓设置已更新' };
  }
  function setCost(code, cost) { return setPlan(code, { cost: cost }); }

  /* ---------------- 名录索引（stock_names.js）---------------- */
  var IDX = null;
  function index() {
    if (IDX) return IDX;
    IDX = { byCode: {}, byName: {}, byPy: {} };
    var raw = (typeof window !== 'undefined' && window.SA_NAMES) || '';
    raw.split('|').forEach(function (tok) {
      var p = tok.split(':');
      if (p.length >= 2 && /^\d{6}$/.test(p[0])) {
        var code = p[0], name = p[1], py = (p[2] || '').replace(/#/g, '');
        if (!IDX.byCode[code]) IDX.byCode[code] = { name: name, py: py };
        if (name && !IDX.byName[name]) IDX.byName[name] = code;
        if (py && py.length >= 2 && !IDX.byPy[py]) IDX.byPy[py] = code;
      }
    });
    return IDX;
  }
  function resolveName(code) {
    var e = index().byCode[code];
    return e ? e.name : '';
  }
  function normalizeName(s) {
    return String(s || '').replace(/[\s\u3000]/g, '').replace(/[Ａ-Ｚａ-ｚ０-９]/g, function (ch) {
      return String.fromCharCode(ch.charCodeAt(0) - 0xFEE0);
    });
  }
  function marketOf(code) {
    if (/^(60|68|9)/.test(code)) return '上海';
    if (/^(0|2|3)/.test(code)) return '深圳';
    if (/^(4|8|92)/.test(code)) return '北交所';
    return '';
  }

  /* 同步解析输入：代码 / 名称 / 拼音首字母 / 错字模糊 */
  function resolve(v) {
    var s = normalizeName(v);
    if (!s) return null;
    var ix = index();
    if (/^\d{5,6}$/.test(s)) {
      if (/^\d{6}$/.test(s) && ix.byCode[s]) return { code: s, name: ix.byCode[s].name, fixed: false, from: '代码' };
      var cand = Object.keys(ix.byCode).filter(function (c) { return c.indexOf(s.slice(0, 5)) === 0; });
      if (cand.length >= 1 && cand.length <= 5) return { code: cand[0], name: ix.byCode[cand[0]].name, fixed: true, from: '代码补全' };
      return null;
    }
    var exact = ix.byName[s];
    if (exact) return { code: exact, name: ix.byCode[exact].name, fixed: false, from: '名称' };
    var lower = s.toLowerCase();
    if (/^[a-z]{2,8}$/.test(lower) && ix.byPy[lower]) return { code: ix.byPy[lower], name: ix.byCode[ix.byPy[lower]].name, fixed: false, from: '拼音' };
    var hits = search(v, 1);
    if (hits.length && hits[0].score >= 60) return { code: hits[0].code, name: hits[0].name, fixed: true, from: '模糊匹配' };
    return null;
  }

  /* 同步模糊搜索（供下拉建议） */
  function search(v, n) {
    var s = normalizeName(v), lower = s.toLowerCase(), out = [];
    if (!s) return out;
    var ix = index();
    if (/^\d+$/.test(s)) {
      Object.keys(ix.byCode).forEach(function (c) {
        if (c.indexOf(s) === 0) out.push({ code: c, name: ix.byCode[c].name, score: 95 });
      });
      return out.slice(0, n || 8);
    }
    Object.keys(ix.byName).forEach(function (nm) {
      var sc = 0;
      if (nm === s) sc = 100;
      else if (nm.indexOf(s) === 0) sc = 90;
      else if (nm.indexOf(s) > 0) sc = 70;
      else if (s.length === nm.length) {
        /* 错字容错：逐字比较，含相邻交换（如"矛台"→"茅台"这类单字错、"国际"字序颠倒） */
        var diff = 0, swapped = false;
        for (var i = 0; i < nm.length; i++) {
          if (nm[i] !== s[i]) {
            diff++;
            if (i + 1 < nm.length && nm[i] === s[i + 1] && nm[i + 1] === s[i]) { swapped = true; i++; }
          }
        }
        /* 错1字=60分；错1字且相邻交换=65分；错2字=50分（仍可候选） */
        if (diff === 1 && swapped) sc = 65;
        else if (diff === 1) sc = 60;
        else if (diff === 2 && nm.length >= 4) sc = 50;
      }
      /* 拼音模糊：输入 zhgj 之类前缀已在 byPy 命中，这里补「拼音含错字」不必要，跳过 */
      if (sc) out.push({ code: ix.byName[nm], name: nm, score: sc });
    });
    if (/^[a-z]{2,8}$/.test(lower)) {
      Object.keys(ix.byPy).forEach(function (py) {
        if (py === lower) out.push({ code: ix.byPy[py], name: ix.byCode[ix.byPy[py]].name, score: 98 });
        else if (py.indexOf(lower) === 0) out.push({ code: ix.byPy[py], name: ix.byCode[ix.byPy[py]].name, score: 80 });
      });
    }
    /* 同分时叠加读音权重：错字纠错按读音选最接近的（花/化同音 → 读音更近者排前） */
    function pyOf(code) { var e = ix.byCode[code]; return e ? e.py : ''; }
    /* 输入的读音近似串：汉字转拼音首字母不可得（名录里才有），反向——用候选票拼音与
       「正确名的拼音」对比没有参照，于是改用字符集重合度：错1字时，若错字与正确字
       在常见混淆集（同音/形近）中，优先。内置小混淆表足够覆盖高频错字。 */
    var CONFUSABLE = {
      '花':'化花华', '化':'化花华', '华':'化花华',
      '矛':'茅予', '茅':'茅予', '予':'茅予',
      '钢':'刚钢冈', '刚':'刚钢冈', '冈':'刚钢冈',
      '材':'才财材', '才':'才财材', '财':'才财材',
      '铝':'吕铝旅', '吕':'吕铝旅', '旅':'吕铝旅',
      '工':'公工功', '公':'公工功', '功':'公工功',
      '作':'做作', '做':'做作',
      '记':'纪计记', '纪':'纪计记', '计':'纪计记'
    };
    out.forEach(function (o) {
      if (o.score === 60 || o.score === 50) {
        var nm2 = o.name, s2 = s;
        if (nm2.length !== s2.length) return;
        for (var j = 0; j < nm2.length; j++) {
          if (nm2[j] !== s2[j]) {
            /* 只看那一个错字：若它在候选字与「常见正确字」的混淆集里则加权 */
            var set1 = CONFUSABLE[nm2[j]], set2 = CONFUSABLE[s2[j]];
            if ((set1 && set1.indexOf(s2[j]) >= 0) || (set2 && set2.indexOf(nm2[j]) >= 0)) o.score += 3;
            break; // 只按第一个错字判断
          }
        }
      }
    });
    out.sort(function (a, b) { return b.score - a.score || a.code.localeCompare(b.code); });
    return out.slice(0, n || 8);
  }

  /* 在线兜底：腾讯 smartbox JSONP（无 CORS 头，用 script 回调） */
  var sbSeq = 0;
  function smartbox(q) {
    return new Promise(function (res) {
      var cb = '__sa_sb_' + (++sbSeq) + '_' + Math.floor(Math.random() * 1e6);
      var s = document.createElement('script');
      var done = false;
      var timer = setTimeout(function () { cleanup(); res([]); }, 3500);
      function cleanup() {
        done = true; clearTimeout(timer);
        try { delete window[cb]; } catch (e) { try { window[cb] = undefined; } catch (e2) {} }
        if (s.parentNode) s.parentNode.removeChild(s);
      }
      window[cb] = function (txt) {
        if (done) return;
        cleanup();
        try {
          var m = /v_hint="(.*)"/.exec(txt);
          if (!m) return res([]);
          var out = [];
          m[1].split('^').forEach(function (item) {
            var f2 = item.split('~');
            if (f2.length >= 3 && /^(sh|sz|bj)$/.test(f2[0]) && /^\d{6}$/.test(f2[1].slice(-6))) {
              out.push({ code: f2[1].slice(-6), name: f2[2] });
            }
          });
          res(out);
        } catch (e) { res([]); }
      };
      s.src = 'https://smartbox.gtimg.cn/s3/?q=' + encodeURIComponent(q) + '&t=all&r=' + cb;
      s.onerror = function () { cleanup(); res([]); };
      document.head.appendChild(s);
    });
  }
  function onlineSearch(v, n) {
    var local = search(v, n || 8);
    if (local.length && local[0].score >= 90) return Promise.resolve(local);
    return smartbox(normalizeName(v)).then(function (on) {
      var seen = {}, merged = local.slice();
      on.forEach(function (r) { if (!seen[r.code]) { seen[r.code] = 1; merged.push({ code: r.code, name: r.name, score: 50 }); } });
      return merged.slice(0, n || 8);
    }).catch(function () { return local; });
  }

  /* ---------------- 实时行情（GBK 解码）---------------- */
  function symOf(code) { return /^(60|68|9|11|50|51|56|58)/.test(code) ? 'sh' + code : 'sz' + code; }
  var _dec = null;
  function gbkDecoder() {
    if (_dec) return _dec;
    try { _dec = new TextDecoder('gbk'); } catch (e) { _dec = new TextDecoder('utf-8'); }
    return _dec;
  }
  function realtime(codes) {
    if (!codes || !codes.length) return Promise.resolve({});
    return fetch('https://qt.gtimg.cn/q=' + codes.map(symOf).join(','), { mode: 'cors' })
      .then(function (r) { return r.arrayBuffer(); })
      .then(function (buf) {
        var txt = gbkDecoder().decode(buf);
        var out = {};
        txt.split(';').forEach(function (seg) {
          var m = /v_(?:sh|sz|bj)?(\d{6})="([^"]*)"/.exec(seg);
          if (!m) return;
          var f2 = m[2].split('~');
          if (f2.length < 50) return;
          out[m[1]] = {
            name: f2[1], code: m[1], price: +f2[3], preclose: +f2[4], open: +f2[5],
            high: +f2[33], low: +f2[34], pct: +f2[32], vol_ratio: +f2[49],
            zt: +(f2[47] || 0), dt: +(f2[48] || 0), amount_wan: +(f2[37] || 0), time: f2[30] || ''
          };
        });
        return out;
      })
      .catch(function () { return {}; });
  }

  /* ---------------- 阈值提取（止损/目标/买区）---------------- */
  function data() { return (typeof window !== 'undefined' && window.__STOCK_DATA__) || {}; }
  function thresholdsFor(code) {
    var t = { stop: null, tp: null, buy_lo: null, buy_hi: null, label: '' };
    function take(bd, label) {
      if (!bd) return false;
      var got = false;
      if (bd.stop != null && t.stop == null) { t.stop = +bd.stop; got = true; }
      if (bd.stop_band != null && t.stop == null) { t.stop = +bd.stop_band; got = true; }
      if (bd.sell_zone && bd.sell_zone[0] != null && t.tp == null) { t.tp = +bd.sell_zone[0]; got = true; }
      if (bd.buy_zone && bd.buy_zone[0] != null && t.buy_lo == null) { t.buy_lo = +bd.buy_zone[0]; t.buy_hi = +bd.buy_zone[1]; got = true; }
      if (got && !t.label) t.label = label;
      return got;
    }
    try {
      var D2 = data();
      ((D2.zones || {}).items || []).some(function (x) { return x.code === code && take(x, '买卖区间'); });
      ((D2.watch || {}).items || []).some(function (x) { return x.code === code && take(x.band_levels, '关注波段'); });
      ((D2.recommend || {}).trend || []).some(function (x) { return x.code === code && take(x, '趋势波段'); });
      ((D2.recommend || {}).ladder_plans || []).some(function (x) { return x.code === code && take(x, '连板计划'); });
    } catch (e) {}
    return t;
  }

  /* ---------------- 三时相建议 ---------------- */
  function phase() {
    var d = new Date(), day = d.getDay(), m = d.getHours() * 60 + d.getMinutes();
    if (day === 0 || day === 6) return '盘后';
    if (m < 9 * 60 + 30) return '盘前';
    if (m <= 15 * 60) return '盘中';
    return '盘后';
  }
  function tradingNow() { return phase() === '盘中'; }

  function adviceFor(it, q, th, ph) {
    var nm = it.name || (q && q.name) || resolveName(it.code) || it.code;
    var lines = [];
    if (ph === '盘前') {
      lines.push('盘前：竞价后核对是否低开 >1%，低开破 ' + (th.stop != null ? th.stop.toFixed(2) : '止损位') + ' 直接放弃/离场');
      if (th.buy_lo != null) lines.push('回踩 ' + th.buy_lo.toFixed(2) + '~' + th.buy_hi.toFixed(2) + ' 可分批建仓');
      if (th.tp != null) lines.push('冲高触及 ' + th.tp.toFixed(2) + ' 优先止盈');
    } else if (ph === '盘中') {
      if (!q || !q.price) { lines.push('行情获取中…'); return lines; }
      if (th.stop != null && q.price <= th.stop) lines.push('🚨 已跌破止损 ' + th.stop.toFixed(2) + '（现价 ' + q.price.toFixed(2) + '）→ 纪律止损');
      else if (th.stop != null && q.price <= th.stop * 1.003) lines.push('⚠ 逼近止损 ' + th.stop.toFixed(2) + '，收紧防守');
      if (th.tp != null && q.price >= th.tp) lines.push('🎯 已到目标 ' + th.tp.toFixed(2) + ' → 分批止盈');
      if (th.buy_lo != null && q.price >= th.buy_lo && q.price <= th.buy_hi) lines.push('✅ 处于买区 ' + th.buy_lo.toFixed(2) + '~' + th.buy_hi.toFixed(2));
      if (!lines.length) lines.push('现价 ' + q.price.toFixed(2) + '（' + (q.pct >= 0 ? '+' : '') + q.pct.toFixed(2) + '%）· 未触发阈值，持有观察');
    } else {
      lines.push('盘后复盘：收盘 ' + (q && q.price ? q.price.toFixed(2) : '—') +
        (q && q.pct != null ? '（' + (q.pct >= 0 ? '+' : '') + q.pct.toFixed(2) + '%）' : '') +
        '，明日按阈值执行：破 ' + (th.stop != null ? th.stop.toFixed(2) : '—') + ' 止损、到 ' + (th.tp != null ? th.tp.toFixed(2) : '—') + ' 止盈');
    }
    if (it.cost != null && q && q.price) {
      var pnl = (q.price - it.cost) / it.cost * 100;
      lines.unshift('成本 ' + it.cost.toFixed(2) + ' · 浮盈 ' + (pnl >= 0 ? '+' : '') + pnl.toFixed(1) + '%');
    }
    return lines;
  }

  /* ---------------- 告警评估（30s 轮询调用）---------------- */
  var MON = { timer: null, lastFire: {}, alerts: [] };
  function evaluate(codes, quotes) {
    var today = nowStr();
    var out = [];
    codes.forEach(function (code) {
      var q = quotes[code];
      if (!q || !q.price) return;
      var th = thresholdsFor(code);
      function fire(kind, text, level) {
        var k = code + ':' + kind + ':' + today;
        if (MON.lastFire[k]) return;
        MON.lastFire[k] = 1;
        out.push({ code: code, name: q.name || resolveName(code) || code, price: q.price, pct: q.pct, text: text, level: level, time: nowHM() });
      }
      if (th.stop != null) {
        if (q.price <= th.stop) fire('stop', '跌破止损位 ' + th.stop.toFixed(2) + '（现 ' + q.price.toFixed(2) + '）→ 建议止损', 'danger');
        else if (q.price <= th.stop * 1.003) fire('stop_near', '逼近止损位 ' + th.stop.toFixed(2) + '（现 ' + q.price.toFixed(2) + '）', 'warn');
      }
      if (th.tp != null && q.price >= th.tp) fire('tp', '触及目标位 ' + th.tp.toFixed(2) + '（现 ' + q.price.toFixed(2) + '）→ 可止盈', 'ok');
      if (th.buy_lo != null && q.price >= th.buy_lo && q.price <= th.buy_hi) fire('buy', '进入买区 ' + th.buy_lo.toFixed(2) + '~' + th.buy_hi.toFixed(2), 'ok');
    });
    return out;
  }

  /* ---------------- 样式注入 ---------------- */
  function ensureStyle() {
    if (document.getElementById('wlx-css')) return;
    var css = [
      '.wlx-sec{margin:14px 0 8px;font-weight:700;font-size:12.5px;color:var(--text-2);',
      'border-left:3px solid var(--accent);padding-left:8px;}',
      '.wlx-sug{position:absolute;top:100%;left:0;right:0;z-index:60;background:var(--card);',
      'border:1px solid var(--border);border-radius:8px;box-shadow:var(--shadow-lg);max-height:260px;overflow:auto;}',
      '.wlx-sug-it{display:flex;align-items:center;gap:10px;padding:7px 10px;cursor:pointer;font-size:13px;}',
      '.wlx-sug-it:hover,.wlx-sug-it.sel{background:var(--card-3);}',
      '.wlx-sug-it .n{font-weight:600;}',
      '.wlx-sug-it .c{color:var(--faint);font-size:11.5px;font-family:SF Mono,Menlo,monospace;}',
      '.wlx-sug-it .m{margin-left:auto;color:var(--muted);font-size:11px;}',
      '.wlx-sug-empty{padding:8px 10px;color:var(--muted);font-size:12px;}',
      '.wl-qk{display:inline-flex;align-items:center;gap:3px;margin-left:6px;padding:2px 8px;border-radius:10px;',
      'border:1px solid var(--border);background:transparent;color:var(--muted);font-size:11px;cursor:pointer;',
      'font-family:inherit;line-height:1.5;vertical-align:middle;white-space:nowrap;}',
      '.wl-qk:hover{border-color:var(--gold);color:var(--gold);}',
      '.wl-qk.on{border-color:var(--gold);color:var(--gold);background:var(--gold-bg);}',
      /* 需求5：趋势票买点/风险高亮 */
      '.wlx-hl-buy{border:1px solid var(--up)!important;box-shadow:0 0 0 1px var(--up-br),0 0 14px var(--up-bg);}',
      '.wlx-hl-sell{border:1px solid var(--down)!important;box-shadow:0 0 0 1px var(--down-br),0 0 14px var(--down-bg);}',
      /* 三时相操作台 */
      '.wlx-top{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px;}',
      '.wlx-alerts{margin-bottom:12px;}',
      '.wlx-alert{padding:7px 12px;border-radius:8px;margin-bottom:6px;font-size:13px;border:1px solid;}',
      '.wlx-alert.danger{border-color:var(--down);color:var(--down);background:var(--down-bg);}',
      '.wlx-alert.warn{border-color:var(--warn);color:var(--warn);background:var(--warn-bg);}',
      '.wlx-alert.ok{border-color:var(--up);color:var(--up);background:var(--up-bg);}',
      '.wlx-card{border:1px solid var(--border);border-radius:10px;background:var(--card-2);padding:10px 12px;margin-bottom:10px;}',
      '.wlx-card-h{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;margin-bottom:6px;}',
      '.wlx-card-h .nm{font-weight:700;font-size:14.5px;}',
      '.wlx-card-h .px{font-weight:800;font-variant-numeric:tabular-nums;font-size:15px;}',
      '.wlx-adv{font-size:12px;color:var(--text-2);line-height:1.7;margin-top:4px;}',
      '.wlx-adv div{padding-left:2px;}',
      '.wlx-th-chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:4px;}',
      /* toast */
      '#wlx-toast-box{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);z-index:10000;',
      'display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none;}',
      '.wlx-toast{padding:9px 18px;border-radius:10px;background:var(--card);border:1px solid var(--border);',
      'color:var(--text);font-size:13px;box-shadow:var(--shadow-lg);opacity:0;transition:opacity .25s;}',
      '.wlx-toast.show{opacity:1;}',
      '.wlx-toast.ok{border-color:var(--up);}',
      '.wlx-toast.err{border-color:var(--down);}'
    ].join('');
    var s = document.createElement('style');
    s.id = 'wlx-css'; s.textContent = css;
    document.head.appendChild(s);
  }

  /* ---------------- toast ---------------- */
  function toast(msg, type) {
    if (!document.body) return;
    ensureStyle();
    var box = document.getElementById('wlx-toast-box');
    if (!box) {
      box = document.createElement('div');
      box.id = 'wlx-toast-box';
      document.body.appendChild(box);
    }
    var t = document.createElement('div');
    t.className = 'wlx-toast ' + (type || '');
    t.textContent = msg;
    box.appendChild(t);
    requestAnimationFrame(function () { t.classList.add('show'); });
    setTimeout(function () {
      t.classList.remove('show');
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
    }, 2400);
  }

  /* ---------------- 一键关注按钮（推荐卡/趋势卡/梯队卡上的 ☆）---------------- */
  function quickBtnHtml(code, name) {
    var on = has(code);
    return '<button class="wl-qk' + (on ? ' on' : '') + '" data-wl-qk="' + code + '" data-wl-name="' + (name || '') + '" title="' + (on ? '点击移出本机关注' : '点击加入本机关注（零密钥）') + '">' + (on ? '★ 已关注' : '☆ 关注') + '</button>';
  }
  function bindDelegates() {
    if (bindDelegates._bound) return;
    bindDelegates._bound = true;
    document.addEventListener('click', function (e) {
      var t = e.target && e.target.closest ? e.target.closest('.wl-qk') : null;
      if (!t) return;
      e.preventDefault(); e.stopPropagation();
      var code = t.getAttribute('data-wl-qk');
      var name = t.getAttribute('data-wl-name') || '';
      var r;
      if (has(code)) { r = del(code); }
      else { r = add(code, name); }
      toast(r.msg, r.ok ? 'ok' : 'err');
      /* 同步页面上所有同票按钮状态 */
      document.querySelectorAll('.wl-qk[data-wl-qk="' + code + '"]').forEach(function (b) {
        var on = has(code);
        b.classList.toggle('on', on);
        b.textContent = on ? '★ 已关注' : '☆ 关注';
        b.title = on ? '点击移出本机关注' : '点击加入本机关注（零密钥）';
      });
    });
  }

  /* ---------------- 三时相操作台面板 ---------------- */
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

  function renderPanel(el) {
    if (!el) return;
    ensureStyle();
    var ph = phase();
    var items = load();
    /* 需求6：持仓（holdings.json 里 watch=false 且有 cost 的已买入票）自动并入监控，
       无需手动再加自选 —— 已买入 = 必须盯盘 */
    var held = [];
    try {
      var H = (window.__STOCK_DATA__ || {}).holdings;
      if (H && H.items && H.items.length) {
        H.items.forEach(function (h2) {
          if (h2.ok !== false && !h2.watch && !items.some(function (x) { return x.code === h2.code; })) {
            held.push({ code: h2.code, name: h2.name || '', cost: (h2.cost != null ? +h2.cost : null), at: '', held: true });
          }
        });
      }
    } catch (e) {}
    var all = items.concat(held);
    var html = '<div class="wlx-top">' +
      '<span class="chip">本机自选 <b>' + items.length + '</b> 只</span>' +
      (held.length ? '<span class="chip" style="border-color:var(--warn);color:var(--warn)">持仓并入 <b>' + held.length + '</b> 只（自动盯盘）</span>' : '') +
      '<span class="chip">当前时段 <b>' + (ph === '盘中' ? '🕐 盘中（实时监控中）' : ph === '盘前' ? '🌅 盘前' : '🌇 盘后') + '</b></span>' +
      '<span class="chip muted" id="wlx-upd">—</span>' +
      '<button class="mbtn mbtn-p" id="wlx-refresh" style="margin-left:auto">↻ 刷新行情</button>' +
      '<button class="mbtn" id="wlx-mgr">⚙ 管理自选池</button>' +
      '</div><div id="wlx-alerts" class="wlx-alerts"></div><div id="wlx-list"></div>';
    el.innerHTML = html;

    if (!all.length) {
      document.getElementById('wlx-list').innerHTML =
        '<div class="empty">本机自选池为空，也没有可并入的持仓。点右上「⚙ 管理自选池」添加，或在「当日推荐 / 涨停梯队」卡片上直接点 ☆ 一键关注（无需任何密钥）。</div>';
      return;
    }

    var codes = all.map(function (x) { return x.code; });
    function paint(quotes) {
      var listEl = document.getElementById('wlx-list');
      if (!listEl) return;
      var upd = document.getElementById('wlx-upd');
      if (upd) upd.textContent = '更新于 ' + nowHM() + (ph === '盘中' ? ' · 30 秒自动刷新' : '');
      listEl.innerHTML = all.map(function (it) {
        var q = quotes[it.code];
        var th = thresholdsFor(it.code);
        var pctCol = q && q.pct >= 0 ? 'var(--up)' : 'var(--down)';
        var thChips = '';
        if (th.stop != null) thChips += '<span class="chip" style="border-color:var(--down);color:var(--down)">止损 ' + th.stop.toFixed(2) + '</span>';
        if (th.tp != null) thChips += '<span class="chip" style="border-color:var(--warn);color:var(--warn)">目标 ' + th.tp.toFixed(2) + '</span>';
        if (th.buy_lo != null) thChips += '<span class="chip" style="border-color:var(--up);color:var(--up)">买区 ' + th.buy_lo.toFixed(2) + '~' + th.buy_hi.toFixed(2) + '</span>';
        if (!thChips) thChips += '<span class="chip muted">暂无波段阈值（等下一轮构建关联）</span>';
        var advs = adviceFor(it, q, th, ph).map(function (a) { return '<div>' + esc(a) + '</div>'; }).join('');
        var planChips = '';
        if (it.qty != null) planChips += '<span class="chip">数量 <b>' + it.qty + '</b></span>';
        if (it.stop != null) planChips += '<span class="chip" style="border-color:var(--down);color:var(--down)">止损 <b>' + it.stop.toFixed(2) + '</b></span>';
        if (it.target != null) planChips += '<span class="chip" style="border-color:var(--warn);color:var(--warn)">目标 <b>' + it.target.toFixed(2) + '</b></span>';
        if (planChips) planChips = '<div class="wlx-th-chips">' + planChips + '</div>';
        return '<div class="wlx-card">' +
          '<div class="wlx-card-h">' +
          (it.held ? '<span class="bd" style="border-color:var(--warn);color:var(--warn);font-size:10.5px;padding:0 4px">持仓</span>' : '') +
          '<span class="nm" style="cursor:pointer" data-kl-code="' + it.code + '" data-kl-name="' + esc(it.name) + '">' + esc(it.name || it.code) + '</span>' +
          '<span class="muted" style="font-size:11.5px">' + it.code + ' · ' + marketOf(it.code) + (th.label ? ' · ' + th.label : '') + '</span>' +
          (q && q.price ? '<span class="px" style="color:' + pctCol + '">' + q.price.toFixed(2) + '</span>' +
            '<span style="color:' + pctCol + ';font-weight:600">' + (q.pct >= 0 ? '+' : '') + q.pct.toFixed(2) + '%</span>' +
            '<span class="muted" style="font-size:11px">高 ' + q.high.toFixed(2) + ' / 低 ' + q.low.toFixed(2) + ' · 量比 ' + q.vol_ratio.toFixed(1) + '</span>' : '<span class="muted">行情获取中…</span>') +
          (it.held ? '' : '<button class="wl-qk on" data-wl-qk="' + it.code + '" data-wl-name="' + esc(it.name) + '" style="margin-left:auto">★ 移除</button>') +
          '</div>' +
          '<div class="wlx-th-chips">' + thChips + '</div>' +
          planChips +
          '<div class="wlx-adv">' + advs + '</div>' +
          '</div>';
      }).join('');
      /* 点股名弹K线（复用 app.js 的 openKline，通过事件委托由 app.js 处理 .stk；
         这里直接挂 .stk 类以复用既有通道） */
      listEl.querySelectorAll('[data-kl-code]').forEach(function (n) {
        n.classList.add('stk');
        n.setAttribute('data-code', n.getAttribute('data-kl-code'));
        n.setAttribute('data-name', n.getAttribute('data-kl-name'));
      });
    }
    realtime(codes).then(function (quotes) {
      paint(quotes);
      var alerts = evaluate(codes, quotes);
      if (alerts.length) {
        MON.alerts = alerts.concat(MON.alerts).slice(0, 12);
        var box = document.getElementById('wlx-alerts');
        if (box) box.innerHTML = MON.alerts.map(function (a) {
          return '<div class="wlx-alert ' + a.level + '">' + a.time + ' · <b>' + esc(a.name) + '</b> ' + esc(a.text) + '</div>';
        }).join('');
        alerts.forEach(function (a) { toast(a.name + ' ' + a.text, a.level === 'danger' ? 'err' : 'ok'); });
      }
      /* 启动/重置 30s 轮询（仅盘中） */
      if (MON.timer) { clearInterval(MON.timer); MON.timer = null; }
      if (tradingNow()) {
        MON.timer = setInterval(function () {
          if (!document.getElementById('wlx-list')) { clearInterval(MON.timer); MON.timer = null; return; }
          realtime(codes).then(function (qs) {
            paint(qs);
            var al = evaluate(codes, qs);
            if (al.length) {
              MON.alerts = al.concat(MON.alerts).slice(0, 12);
              var bx = document.getElementById('wlx-alerts');
              if (bx) bx.innerHTML = MON.alerts.map(function (a) {
                return '<div class="wlx-alert ' + a.level + '">' + a.time + ' · <b>' + esc(a.name) + '</b> ' + esc(a.text) + '</div>';
              }).join('');
              al.forEach(function (a) { toast(a.name + ' ' + a.text, a.level === 'danger' ? 'err' : 'ok'); });
            }
          });
        }, 30000);
      }
    });

    var rf = document.getElementById('wlx-refresh');
    if (rf) rf.addEventListener('click', function () { renderPanel(el); });
    var mg = document.getElementById('wlx-mgr');
    if (mg) mg.addEventListener('click', function () {
      if (typeof window.__SA_OPEN_WL__ === 'function') window.__SA_OPEN_WL__();
      else if (typeof wlOpenFallback === 'function') wlOpenFallback();
    });
  }
  function wlOpenFallback() { toast('请到「市场概览」页底部点「⭐ 管理关注股」', ''); }

  /* ---------------- 导出 ---------------- */
  window.WLX = {
    ensureStyle: ensureStyle,
    bindDelegates: bindDelegates,
    list: list, has: has, add: add, del: del, setCost: setCost, setPlan: setPlan,
    resolve: resolve, search: search, onlineSearch: onlineSearch,
    resolveName: resolveName, marketOf: marketOf,
    realtime: realtime, thresholdsFor: thresholdsFor,
    tradingNow: tradingNow, phase: phase,
    toast: toast, quickBtnHtml: quickBtnHtml,
    renderPanel: renderPanel, _MON: MON
  };
})();
