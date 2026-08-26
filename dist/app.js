/* 渲染层：把 window.__STOCK_DATA__ 铺成六个视图。零依赖。 */
(function () {
  'use strict';
  var D = window.__STOCK_DATA__;
  var E = CH.esc, C = CH.COLORS;
  /* 主线/龙头映射：模块级全局，供 overview/rec/bull 等所有视图的 mlBadge() 共用。
     （早期版本仅在某视图内部 var ML，导致 mlBadge 引用到 undefined，龙头/主线徽标不显示。） */
  var ML = (D && D.recommend && D.recommend.mainline_map) || {};

  /* ---------------- 工具 ---------------- */
  function n2(v, d) { return (v === null || v === undefined || v !== v) ? (d === undefined ? '—' : d) : v; }
  function f(v, p) { return (v === null || v === undefined || v !== v) ? '—' : Number(v).toFixed(p === undefined ? 2 : p); }
  function pct(v, p) { return (v === null || v === undefined || v !== v) ? '—' : Number(v).toFixed(p === undefined ? 1 : p) + '%'; }
  function yi(v) { return (v === null || v === undefined) ? '—' : (v / 1e8).toFixed(1) + '亿'; }
  function sign(v, p) {
    if (v === null || v === undefined || v !== v) return '<span class="faint">—</span>';
    var c = v > 0 ? 'up' : (v < 0 ? 'down' : 'muted');
    return '<span class="' + c + '">' + (v > 0 ? '+' : '') + Number(v).toFixed(p === undefined ? 2 : p) + '</span>';
  }
  function lbBadge(n) { return '<span class="bd lb' + Math.min(6, Math.max(1, n)) + '">' + n + '板</span>'; }
  function tierBadge(t) {
    var m = { '主线': 't-main', '支线': 't-sub', '零星': 't-min' };
    return '<span class="bd ' + (m[t] || 't-min') + '">' + E(t || '零星') + '</span>';
  }
  function trendBadge(t) {
    var c = t === '升温' ? C.up : t === '降温' ? C.down : C.gray;
    return '<span style="display:inline-block;padding:2px 9px;border-radius:10px;font-size:11px;font-weight:700;color:#fff;background:' + c + '">' + E(t) + '</span>';
  }
  /* 近 N 日涨停家数的迷你走势，用于板块轮动 */
  function spark(series, color) {
    if (!series || !series.length) return '';
    var w = 96, h = 26, max = Math.max.apply(null, series.concat([1]));
    var min = Math.min.apply(null, series.concat([0])), rng = (max - min) || 1;
    var pts = series.map(function (v, i) {
      var x = series.length === 1 ? w / 2 : (i / (series.length - 1)) * (w - 6) + 3;
      var y = h - 3 - ((v - min) / rng) * (h - 6);
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    var lx = series.length === 1 ? w / 2 : w - 3;
    var ly = h - 3 - ((series[series.length - 1] - min) / rng) * (h - 6);
    return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" style="vertical-align:middle">' +
      '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.6" stroke-linejoin="round"/>' +
      '<circle cx="' + lx.toFixed(1) + '" cy="' + ly.toFixed(1) + '" r="2.2" fill="' + color + '"/></svg>';
  }
  function qBar(q, color) {
    var c = color || (q >= 75 ? C.up : q >= 55 ? C.gold : C.gray);
    return '<span class="mbar"><i style="width:' + Math.max(2, Math.min(100, q)) + '%;background:' + c + '"></i></span>';
  }
  function card(title, body, hint, cls) {
    return '<div class="card' + (cls ? ' ' + cls : '') + '"><h3>' + title +
      (hint ? '<span class="hint">' + hint + '</span>' : '') + '</h3><div class="body">' + body + '</div></div>';
  }
  function kpi(k, v, d, cls) {
    var vcls = 'v', da = '';
    var s = String(v);
    if (/^-?\d+(\.\d+)?$/.test(s)) {
      var num = parseFloat(s);
      var dec = (s.split('.')[1] || '').length;
      vcls += ' count';
      da = ' data-count="' + num + '" data-dec="' + dec + '" data-suffix=""';
    }
    return '<div class="kpi' + (cls ? ' ' + cls : '') + '"><div class="k">' + E(k) + '</div><div class="' + vcls + '"' + da + '>' + v +
      '</div><div class="d">' + (d || '') + '</div></div>';
  }
  function table(cols, rows, opts) {
    opts = opts || {};
    /* rows 允许为「<tr> 数组」或「已拼接好的 HTML 字符串」，统一兼容 */
    var body = (rows == null) ? '' : (Array.isArray(rows) ? rows.join('') : String(rows));
    var h = '<div class="tbl-wrap' + (opts.scroll ? ' scroll-y' : '') + '"><table><thead><tr>' +
      cols.map(function (c) { return '<th class="' + (c.a || '') + '">' + c.t + '</th>'; }).join('') +
      '</tr></thead><tbody>';
    if (!body) return '<div class="empty">' + (opts.empty || '当日无符合条件的标的') + '</div>';
    h += body;
    return h + '</tbody></table></div>';
  }
  function seg(v, lo, hi) { return Math.max(0, Math.min(100, (v - lo) / (hi - lo) * 100)); }
  /* 股票代码归一化（去除 sh/sz 前缀与非数字字符），用于跨数据源 JOIN */
  function normCode(c) { return String(c == null ? '' : c).replace(/\D/g, ''); }

  /* ---------------- 数字滚动动画（仅在浏览器生效；无头环境安全跳过） ---------------- */
  function initCountUp(root) {
    if (typeof requestAnimationFrame !== 'function' || !root || !root.querySelectorAll) return;
    var els = root.querySelectorAll('.count[data-count]');
    for (var i = 0; i < els.length; i++) {
      (function (el) {
        var target = parseFloat(el.getAttribute('data-count'));
        var dec = parseInt(el.getAttribute('data-dec') || '0', 10);
        var suffix = el.getAttribute('data-suffix') || '';
        var dur = 750, t0 = null;
        function step(ts) {
          if (t0 === null) t0 = ts;
          var p = Math.min(1, (ts - t0) / dur);
          var e = 1 - Math.pow(1 - p, 3);
          el.textContent = (target * e).toFixed(dec) + suffix;
          if (p < 1) requestAnimationFrame(step);
          else el.textContent = target.toFixed(dec) + suffix;
        }
        requestAnimationFrame(step);
      })(els[i]);
    }
  }

  /* ---------------- 离线「类AI解读」卡片 ---------------- */
  function narrativeCard() {
    var n = D.narrative;
    if (!n) return '';
    var lis = (n.bullets || []).map(function (b) { return '<li>' + E(b) + '</li>'; }).join('');
    var by = n.ai_generated ? (n.generated_by || 'AI 引擎') : '规则引擎';
    var badge = n.ai_generated
      ? '<span class="tag" style="border-color:var(--accent);color:var(--accent)">⚡ ' + E(by) + '驱动</span>'
      : '<span class="tag">规则引擎生成</span>';
    var meta = (n.generated_at ? '<span class="faint" style="margin-left:8px;font-size:11px">撰写于 ' + E(n.generated_at) + '</span>' : '');
    var h2 = '<div class="narrative"><div class="nh"><span class="dot"></span><h3>' + E(n.headline || '智能复盘') +
      '</h3>' + badge + meta + '</div><ul>' + lis + '</ul>' +
      (n.outlook ? '<div class="out"><b>次日观察：</b>' + E(n.outlook) + '</div>' : '');
    /* 多模型共识（Hy3 + 可选 DeepSeek/Kimi/Qwen） */
    var AC = D.ai_consensus;
    if (AC && AC.n_models > 1) {
      var dirCol = AC.direction === '看多' ? C.up : (AC.direction === '看空' ? C.down : C.gold);
      h2 += '<div style="margin-top:12px;padding:11px 13px;border-left:3px solid var(--purple);' +
        'background:var(--purple-bg);border-radius:0 8px 8px 0;font-size:12.5px;color:var(--text-2);line-height:1.7">' +
        '<div style="font-weight:700;color:var(--purple);margin-bottom:5px">🤝 多模型综合共识（' +
        E((AC.models || []).join(' + ')) + '）</div>' +
        '<div>综合方向：<b style="color:' + dirCol + '">' + E(AC.direction) + '</b> · 置信度 <b>' +
        f(AC.confidence, 0) + '%</b></div>' +
        (AC.key_picks && AC.key_picks.length ? '<div style="margin-top:4px">重点标的：<b>' + E(AC.key_picks.join('、')) + '</b></div>' : '') +
        (AC.risks && AC.risks.length ? '<div style="margin-top:4px">共识风险：' + E(AC.risks.join('；')) + '</div>' : '') +
        (AC.comment ? '<div style="margin-top:4px;color:var(--muted)">' + E(AC.comment) + '</div>' : '') +
        '</div>';
    }
    return h2 + '</div>';
  }

  /* ---------------- 顶栏 ---------------- */
  function head() {
    var m = D.meta || {};
    var s = '<span class="pill">交易日 ' + E(m.date) + '</span>';
    s += '<span class="pill gray">上一交易日 ' + E(m.prev_date || '—') + '</span>';
    s += '<span class="pill ' + (m.snapshot_same_day ? 'ok' : 'warn') + '">' +
      (m.snapshot_same_day ? '盘后快照·当日' : '盘后快照·降级') + '</span>';
    s += '<span class="pill gray">' + n2(m.universe) + ' 只 / ' + n2(m.trade_days) + ' 日</span>';
    s += '<span class="pill purple">' + E(m.generated_at || '') + '</span>';
    // 数据新鲜度（实时计算距今时长，供轮询刷新时复用）
    var fresh = '';
    var _gen = m.generated_at ? new Date(m.generated_at.replace(' ', 'T')) : null;
    var dt = m.date ? new Date(m.date.replace(/-/g, '/')) : null;
    function _rel(ts) {
      if (!ts || isNaN(ts.getTime())) return '';
      var mins = Math.floor((Date.now() - ts.getTime()) / 60000);
      if (mins < 1) return '刚刚';
      if (mins < 60) return mins + ' 分钟前';
      var hrs = Math.floor(mins / 60);
      if (hrs < 24) return hrs + ' 小时前';
      return Math.floor(hrs / 24) + ' 天前';
    }
    if (dt && !isNaN(dt.getTime())) {
      var days = Math.floor((Date.now() - dt.getTime()) / 86400000);
      if (days >= 2) fresh = '<span class="fresh stale">⚠ 数据已过期 ' + days + ' 天 · 站点由定时任务自动更新，若长期未更新请检查推送状态或联系管理员</span>';
      else fresh = '<span class="fresh ok">✓ 数据新鲜 · 收盘后已更新' +
        (_gen && !isNaN(_gen.getTime()) ? '（生成于 ' + _rel(_gen) + '）' : '') + '</span>';
    }
    document.getElementById('dateline').innerHTML = s + fresh;
    document.getElementById('foot').innerHTML =
      '数据源：' + E(m.source || '公开行情接口') + '（全部为当日收盘后数据，无盘中实时成分）<br>' +
      '生成于 ' + E(m.generated_at) + '，构建耗时 ' + n2(m.build_seconds) + ' 秒 · 行情库覆盖 ' +
      n2(m.universe) + ' 只个股 / ' + n2(m.trade_days) + ' 个交易日<br>' +
      '<span class="muted">快捷键：1~9 / 0 / - / = 直接切换视图</span><br>' +
      '所有概率与评分均为历史统计外推，仅供研究复盘，不构成投资建议。';
  }

  /* ============ 视图 1：市场概览 ============ */
  function qBadge(it) {
    if (it && it.q && it.q.flag) {
      return ' <span class="bd" style="border-color:' + C.danger + ';color:' + C.danger +
        ';font-size:11px;padding:0 5px;margin-left:4px">⚠存疑</span>';
    }
    return '';
  }

  /* 主线 / 龙头 打标：是否属于主线板块、是否为主线龙头（来自 recommend.mainline_map） */
  function mlBadge(it) {
    var m = it && ML && ML[it.code];
    if (!m) return '';
    if (m.is_leader) return '<span class="bd lb4" title="所属【' + E(m.sector) + '】为主线板块，该股为板块龙头">👑龙头</span>';
    if (m.is_mainline) return '<span class="bd danger" title="所属【' + E(m.sector) + '】为主线板块">🔴主线</span>';
    return '';
  }

  /* 接力方向打标：个股所属板块正处于“旧主线退潮后的新抱团”接力方向 */
  function relayBadge(it) {
    if (it && it.relay_dir) return '<span class="bd" style="border-color:' + C.up + ';color:' + C.up + ';font-size:11px;padding:0 5px;margin-left:4px">🔗接力方向</span>';
    return '';
  }

  function qualityCard() {
    var q = D.data_quality;
    if (!q) return '';
    if (q.skipped) {
      return card('🔍 多源数据校验',
        '<div class="note">本次未执行多源交叉校验（' + E(q.reason || '已跳过') +
        '）。主数据仍来自东方财富。</div>',
        '东财 + 新浪 + 腾讯 三源比对，捕捉单一源报价异常');
    }
    var fl = q.flagged || [];
    var body = '<div class="chips" style="margin-bottom:8px">' +
      '<span class="chip">交叉校验 <b>' + n2(q.checked) + '</b> 只</span>' +
      '<span class="chip">可用数据源 <b>' + n2(q.with_data) + '</b></span>' +
      '<span class="chip">价差阈值 <b>' + f(q.threshold_pct, 1) + '%</b></span>' +
      '<span class="chip" style="border-color:' + (fl.length ? C.danger : C.ok) + ';color:' +
      (fl.length ? C.danger : C.ok) + '">存疑 <b>' + n2(q.flagged_count) + '</b> 只</span></div>';
    if (fl.length) {
      body += '<div class="tbl-wrap"><table><thead><tr><th>代码</th><th>东财</th><th>新浪</th>' +
        '<th>腾讯</th><th class="r">价差</th></tr></thead><tbody>' +
        fl.map(function (x) {
          var p = x.prices || {};
          return '<tr><td class="name">' + E(x.code) + '</td>' +
            '<td class="num">' + (p.em != null ? f(p.em, 2) : '—') + '</td>' +
            '<td class="num">' + (p.sina != null ? f(p.sina, 2) : '—') + '</td>' +
            '<td class="num">' + (p.tencent != null ? f(p.tencent, 2) : '—') + '</td>' +
            '<td class="r num down">+' + f(x.spread_pct != null ? x.spread_pct : 0, 2) + '%</td></tr>';
        }).join('') + '</tbody></table></div>' +
        '<div class="note">以上标的多源报价差异超过阈值，可能含单一源错误；展示以多源一致价（中位数）为准，请谨慎参考。</div>';
    } else {
      body += '<div class="note">三源报价一致，未发现明显异常；主分析数据已结合多源校验。</div>';
    }
    return card('🔍 多源数据校验（东财 · 新浪 · 腾讯）', body,
      '三源实时报价交叉比对，捕捉单一数据源的报价异常');
  }

  function viewOverview() {
    var mk = D.market || {}, em = mk.emotion || {}, st = mk.sentiment || {}, cy = mk.cycle || {};
    var ser = (mk.series || []);
    var h = '';

    h += narrativeCard();
    h += qualityCard();

    /* 数据完整性体检（构建期自检：覆盖度/量纲错乱/价格异常） */
    var IG = D.integrity;
    if (IG) {
      var igOk = IG.ok;
      var igCol = igOk ? C.up : C.down;
      var igWarn = (IG.warnings || []).map(function (w) {
        return '<div class="kv" style="margin:3px 0">⚠ ' + E(w) + '</div>';
      }).join('');
      var igBody = '<div style="margin-bottom:8px"><span class="bd" style="background:' + igCol + '22;color:' + igCol + ';border-color:' + igCol + '">' +
        (igOk ? '✅ 数据完整性体检通过' : '⚠️ 数据完整性告警') + '</span></div>' +
        '<div class="kv" style="margin-bottom:6px">交易日样本 <b>' + n2(IG.trade_days) + '</b> 天 ｜ 最新交易日 <b>' + E(IG.last_date || '—') + '</b> ｜ 覆盖 <b>' + n2(IG.last_day_rows) + '</b> 只' +
        (IG.scale_anomalies && IG.scale_anomalies.length ? ' ｜ 量纲异常 <b style="color:' + C.down + '">' + IG.scale_anomalies.length + '</b> 日' : '') + '</div>' +
        (igWarn || '<div class="note" style="color:var(--muted)">覆盖度、量纲、价格校验均正常</div>');
      h += card('🔍 数据完整性体检', igBody, '构建期自动自检：最新交易日覆盖度、量纲错乱(股/手·分/元混用放大百倍)、价格异常(收盘≤0/高<低)。异常会污染量比/热度/持仓缩量判断，发现即告警');
    }

    /* KPI */
    var lus = D.limit_ups || [];
    var maxlb = lus.reduce(function (a, b) { return Math.max(a, b.streak || 0); }, 0);
    h += '<div class="grid g4" style="margin-bottom:16px">' +
      kpi('涨停家数', n2(em.zt), '连板 ' + n2(em.lb) + ' 只 · 首板 ' + (n2(em.zt, 0) - n2(em.lb, 0)) + ' 只', 'up') +
      kpi('市场高度', n2(maxlb || em.max_lb) + ' <span style="font-size:14px">板</span>', '空间板决定接力意愿') +
      kpi('赚钱效应', (em.yest_perf >= 0 ? '+' : '') + f(em.yest_perf) + '%', '昨日涨停股今日平均涨幅',
        em.yest_perf >= 0 ? 'up' : 'down') +
      kpi('连板晋级率', pct(em.promote_rate), '昨涨停今日再涨停占比') +
      kpi('跌停家数', n2(em.dt), '亏钱效应观测', em.dt > 8 ? 'down' : '') +
      kpi('封板率', pct(st.seal_rate), '涨停 /（涨停+炸板）') +
      kpi('两市成交', yi(em.amount), '环比 ' + (st.amt_chg === null || st.amt_chg === undefined ? '—' : (st.amt_chg > 0 ? '+' : '') + f(st.amt_chg, 1) + '%')) +
      kpi('涨跌家数', n2(em.up) + ' / ' + n2(em.down),
        '上涨占比 ' + pct(em.up / Math.max(1, (em.up || 0) + (em.down || 0)) * 100)) +
      '</div>';

    /* 恐慌 / 崩盘扫描 */
    var PN = D.panic;
    if (PN && PN.level) {
      var pcol = PN.level === '恐慌' ? C.down : (PN.level === '升温' ? C.warn : (PN.level === '安全' ? C.up : C.blue));
      var bf = (PN.bigface || []).slice(0, 5).map(function (x) {
        return '<div class="kv" style="margin:4px 0"><b>' + E(x.name) + '</b> 收 ' + f(x.pct, 2) + '% · ' + E(x.kind) +
          ' · 较高点回落 ' + x.drop_from_high + '%</div>';
      }).join('');
      var pBody = '<div style="margin-bottom:8px"><span class="bd" style="background:' + pcol + '22;color:' + pcol + ';border-color:' + pcol + '">综合等级：' + E(PN.level) + '（评分 ' + PN.score + '）</span></div>' +
        '<div class="note" style="margin-bottom:8px">' + E(PN.hint) + '</div>' +
        '<div class="kv" style="margin-bottom:6px"><b>跌停</b> ' + n2(PN.dt_count) + ' 家（基线 ' + f(PN.dt_base, 0) + '，z=' + f(PN.dt_z, 1) + '）｜ <b>昨涨停收绿</b> ' + f(PN.yest_green, 0) + '% ｜ <b>炸板率</b> ' + f(PN.zb_rate, 0) + '% ｜ <b>涨跌比下跌</b> ' + f(PN.down_ratio, 0) + '%</div>' +
        (bf ? '<div style="margin-top:6px"><b style="color:var(--muted)">大面榜（冲高回落 / 天地板 / 墓碑线）</b>' + bf + '</div>' : '');
      h += card('⚠️ 盘面恐慌 / 崩盘扫描', pBody, '跌停潮 + 大面榜(天地板/墓碑线) + 亏钱效应 + 炸板率 + 广度，综合判定盘面恐慌等级');
    }

    /* 冷启修复节奏预判（coldwave）：热度位 / 修复预判 / 冷后领涨风格 / 方向轮动 */
    var CW = D.cold;
    if (CW && CW.today) {
      var tw = CW.today;
      var lvCol = (tw.level === '爆冷' || tw.level === '偏冷') ? C.down : (tw.level === '热' ? C.up : C.gold);
      var cb = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip" style="border-color:' + lvCol + ';color:' + lvCol + '">热度位 <b>' + f(tw.hp, 1) + '</b>（' + E(tw.level) + '）</span>' +
        '<span class="chip">涨停 <b>' + n2(tw.zt) + '</b> · 最高 <b>' + n2(tw.max_lb) + '板</b></span>' +
        '<span class="chip">红盘占比 <b>' + f(tw.up_ratio, 0) + '%</b></span>' +
        (CW.last_trough ? '<span class="chip">最近冷谷 <b>' + E(CW.last_trough) + '</b>（距今 T+' + n2(CW.since_trough) + '）</span>' : '') +
        '</div>';
      var FC = CW.forecast;
      if (FC) {
        cb += '<div class="bd" style="display:inline-block;background:' + lvCol + '22;color:' + lvCol + ';border-color:' + lvCol + '">' +
          E(FC.state) + ' · 预计修复 <b>' + E(FC.expect) + '</b></div>' +
          '<div class="note" style="margin-top:6px">' + E(FC.note || '') + '</div>';
      }
      var STY = CW.style;
      if (STY && STY.n) {
        cb += '<div style="margin-top:10px"><b style="color:var(--muted);font-size:12px">历史冷后领涨风格（' + n2(STY.n) + ' 只样本）</b>' +
          '<div class="chips" style="margin-top:4px">' +
          '<span class="chip">启动价中位 <b>' + f(STY.price_median, 1) + '元</b></span>' +
          '<span class="chip">流通盘中位 <b>' + f(STY.fmv_median, 0) + '亿</b></span>' +
          '<span class="chip">低价股占 <b>' + pct(STY.share_low_price * 100, 0) + '</b></span>' +
          '<span class="chip">超跌股占 <b>' + pct(STY.share_oversold * 100, 0) + '</b></span>' +
          ((STY.top_inds && STY.top_inds.length) ? '<span class="chip" style="border-color:' + C.purple + ';color:' + C.purple + '">高频方向 ' + STY.top_inds.slice(0, 4).map(function (x) { return E(x.name); }).join('/') + '</span>' : '') +
          '</div></div>';
      }
      var RO = CW.rotation || {};
      if (RO.pairs) {
        cb += '<div class="note" style="margin-top:8px">方向轮动：相邻两次冷后<b>换方向概率 ' + pct(RO.switch_rate * 100, 0) + '</b>' +
          ((RO.last_inds && RO.last_inds.length) ? '，最近领涨方向 ' + RO.last_inds.map(E).join('→') : '') + '</div>';
      }
      var cds = CW.candidates || [];
      if (cds.length) {
        cb += '<div style="margin-top:10px"><b style="color:var(--muted);font-size:12px">当下符合冷后风格的候选</b>' +
          table([{ t: '标的' }, { t: '行业' }, { t: '评分', a: 'r' }, { t: '现价', a: 'r' }, { t: '流通(亿)', a: 'r' }, { t: '距60日高', a: 'r' }, { t: '量比', a: 'r' }, { t: '入选逻辑' }],
            cds.slice(0, 6).map(function (c2) {
              return '<tr><td><b>' + E(c2.name) + '</b> <span class="muted">' + E(c2.code) + '</span></td>' +
                '<td>' + E(c2.ind || '—') + '</td>' +
                '<td class="r" style="color:' + C.gold + '"><b>' + f(c2.score, 1) + '</b></td>' +
                '<td class="r">' + f(c2.price, 2) + '</td>' +
                '<td class="r">' + f(c2.fmv, 1) + '</td>' +
                '<td class="r" style="color:' + C.down + '">' + f(c2.dd60, 0) + '%</td>' +
                '<td class="r">' + f(c2.vol_ratio, 2) + '</td>' +
                '<td class="muted" style="font-size:11px">' + E(c2.why || '') + '</td></tr>';
            })) + '</div>';
      }
      h += card('❄️ 冷启修复节奏预判', cb, '热度百分位判定冷暖；每次转冷后第几天修复、什么风格领涨、方向是否重复——全部由本地日K库实测统计');
    }

    /* 跳空缺口：回补规律分桶 + 当前未回补清单 */
    var GP = D.gaps;
    if (GP && GP.stats && GP.stats.n_total) {
      var GS = GP.stats, OV5 = (GS.overall || {}).t5 || {};
      var gb = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip">历史缺口 <b>' + n2(GS.n_total) + '</b> 个（≥1%）</span>' +
        (OV5.n ? '<span class="chip" style="border-color:' + C.blue + ';color:' + C.blue + '">5日回补 <b>' + pct((OV5.rate || 0) * 100, 0) + '</b></span>' : '') +
        (GS.up_t5 != null ? '<span class="chip">向上缺口回补 <b>' + pct(GS.up_t5 * 100, 0) + '</b></span>' : '') +
        (GS.down_t5 != null ? '<span class="chip">向下缺口回补 <b>' + pct(GS.down_t5 * 100, 0) + '</b></span>' : '') +
        '</div>';
      var BD = GS.by_depth || {}, dk = Object.keys(BD);
      if (dk.length) {
        gb += '<div style="margin-bottom:8px"><b style="color:var(--muted);font-size:12px">按深度 · 5 日回补率</b>' +
          '<div class="chips" style="margin-top:4px">' + dk.map(function (k2) {
            return '<span class="chip">' + E(k2) + ' <b>' + pct(BD[k2].fill_t5 * 100, 0) + '</b> <span class="muted">(' + n2(BD[k2].n) + ' 例)</span></span>';
          }).join('') + '</div></div>';
      }
      var ogs = GP.open_gaps || [];
      if (ogs.length) {
        gb += '<div><b style="color:var(--muted);font-size:12px">当前未回补缺口（近 20 个交易日形成）</b>' +
          table([{ t: '方向' }, { t: '标的' }, { t: '缺口日' }, { t: '幅度', a: 'r' }, { t: '已存', a: 'r' }, { t: '缺口区间' }],
            ogs.map(function (o2) {
              var isUp = o2.dir === 'up';
              return '<tr><td style="color:' + (isUp ? C.up : C.down) + ';font-weight:700">' + (isUp ? '▲ 支撑' : '▼ 压力') + '</td>' +
                '<td><b>' + E(o2.name) + '</b> <span class="muted">' + E(o2.code) + '</span></td>' +
                '<td class="muted">' + E(o2.gap_date) + '</td>' +
                '<td class="r">' + f(o2.gap_pct, 1) + '%</td>' +
                '<td class="r">' + n2(o2.days_alive) + ' 天</td>' +
                '<td class="muted">' + f(o2.gap_low, 2) + ' ~ ' + f(o2.gap_high, 2) + '</td></tr>';
            })) + '</div>';
      }
      h += card('🕳 跳空缺口扫描', gb, '「缺口必补」到底多真？历史回补率按深度/方向分桶定量；未回补向上缺口作支撑参考、向下缺口作压力参考');
    }

    /* 市场风格判定：小微盘题材 / 连板接力 / 权重抱团 / 双轨市 */
    var STY = D.stylereg;
    if (STY && STY.verdict) {
      var V = STY.verdict;
      var vb = '<div style="margin-bottom:8px"><span class="bd" style="background:' + C.purple + '22;color:' + C.purple + ';border-color:' + C.purple + ';font-size:14px;font-weight:700">' + E(V.label) + '</span>' +
        (STY.switch ? ' <span class="bd" style="border-color:' + C.warn + ';color:' + C.warn + '">⚠ 风格切换：' + E(STY.switch.from_style) + ' →</span>' : '') + '</div>';
      var ev = V.evidence || [];
      if (ev.length) {
        vb += '<div class="chips" style="margin-bottom:8px">' + ev.map(function (e2) {
          return '<span class="chip">' + E(e2) + '</span>';
        }).join('') + '</div>';
      }
      if (V.note) vb += '<div class="note">' + E(V.note) + '</div>';
      h += card('🧭 市场风格判定', vb, '涨停市值分布 × CR10 成交集中度 × 权重超额 × 强趋势占比，四维判定当下是谁在主导（小微盘题材轮动 / 连板接力 / 权重抱团趋势 / 双轨市），并给出对应打法');
    }

    /* 地量/缩量变盘窗口 */
    var DV = D.dryvol;
    if (DV && DV.today) {
      var T = DV.today;
      var stCol = T.in_dry ? C.down : ((T.shrink_days || 0) >= 3 ? C.warn : C.blue);
      var db = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip" style="border-color:' + stCol + ';color:' + stCol + '">' + E((DV.state || {}).state || '常态') + '</span>' +
        (T.ratio != null ? '<span class="chip">市场额比 <b>' + f(T.ratio, 2) + '</b></span>' : '') +
        (T.hp != null && !T.partial ? '<span class="chip">近一年分位 <b>' + f(T.hp, 0) + '%</b></span>' : '') +
        '<span class="chip">连续缩量 <b>' + n2(T.shrink_days || 0) + '</b> 日</span>' +
        '</div>';
      var SS = DV.stats;
      if (SS && SS.n) {
        db += '<div class="note">';
        db += '历史 ' + n2(SS.n) + ' 次地量段：' + (SS.hit_n
          ? '5 日内放量变盘率 ' + pct(SS.hit_rate * 100, 0) + '，向上占 ' + pct((SS.up_rate || 0) * 100, 0)
          : '尚无 5 日内放量先例（地量后常见继续磨底，勿抢跑）');
        if (SS.vol_expand != null) db += '；段末后5日波动为常态 <b>' + f(SS.vol_expand, 1) + ' 倍</b>、5日累计均 <b>' + f(SS.avg_dir5, 2) + '%</b>';
        db += '</div>';
      }
      if ((DV.state || {}).note) db += '<div class="note" style="margin-top:6px">' + E(DV.state.note) + '</div>';
      h += card('💧 地量 / 缩量变盘窗口', db, '全市场成交额分位识别地量区；地量之后是放量变盘还是继续磨底——由本地库历史实测统计给出答案');
    }

    /* 52周新高新低广度 */
    var NH = D.newhigh;
    if (NH && NH.today) {
      var NT = NH.today;
      var rCol = NT.ratio >= 0.3 ? C.up : (NT.ratio <= -0.3 ? C.down : C.gold);
      var hb = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">52周新高 <b>' + n2(NT.nh) + '</b> 只</span>' +
        '<span class="chip" style="border-color:' + C.down + ';color:' + C.down + '">52周新低 <b>' + n2(NT.nl) + '</b> 只</span>' +
        '<span class="chip" style="border-color:' + rCol + ';color:' + rCol + '">NH-NL 比 <b>' + f(NT.ratio, 2) + '</b></span>' +
        (NT.nh_rank != null ? '<span class="chip">新高占比处' + E(NH.span_note || '历史') + ' <b>' + f(NT.nh_rank, 0) + '%</b> 分位</span>' : '') +
        (NT.nl_rank != null ? '<span class="chip">新低占比 <b>' + f(NT.nl_rank, 0) + '%</b> 分位</span>' : '') +
        '</div>';
      var _nhl = function (rows, cls) {
        return rows.map(function (x2) { return '<span class="chip">' + E(x2.name) + '</span>'; }).join('');
      };
      if ((NH.new_highs || []).length) hb += '<div style="margin-bottom:6px"><b style="color:var(--muted);font-size:12px">新高前排</b><div class="chips" style="margin-top:4px">' + _nhl(NH.new_highs.slice(0, 8)) + '</div></div>';
      if ((NH.new_lows || []).length) hb += '<div><b style="color:var(--muted);font-size:12px">新低前排（回避）</b><div class="chips" style="margin-top:4px">' + _nhl(NH.new_lows.slice(0, 8)) + '</div></div>';
      h += card('🏔 52周新高新低广度', hb, '创新高/新低家数与 NH-NL 比是最诚实的市场广度指标；新低扩位常领先指数见底，新高收缩常领先情绪退潮');
    }

    /* 均线粘合待变盘池 */
    var MG = D.maglue;
    if (MG && MG.glue_n) {
      var mb = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip">粘合池 <b>' + n2(MG.glue_n) + '</b> 只（MA5/10/20/60 离散≤2.5%）</span>' +
        '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">已现放量启动 <b>' + n2(MG.launching_n) + '</b> 只</span>' +
        '</div>';
      var mrows = (MG.launching && MG.launching.length ? MG.launching : MG.items).slice(0, 6);
      if (mrows.length) {
        mb += table([{ t: '标的' }, { t: '粘合天数', a: 'r' }, { t: '离散度', a: 'r' }, { t: '20日振幅', a: 'r' }, { t: '量比', a: 'r' }, { t: '现价', a: 'r' }, { t: '今日', a: 'r' }],
          mrows.map(function (x3) {
            return '<tr>' + (x3.launch ? '<td><b>' + E(x3.name) + '</b> <span class="bd" style="border-color:' + C.up + ';color:' + C.up + ';font-size:10px;padding:0 4px">启动</span></td>'
              : '<td><b>' + E(x3.name) + '</b></td>') +
              '<td class="r">' + n2(x3.glue_days) + '</td>' +
              '<td class="r">' + f(x3.spread, 2) + '%</td>' +
              '<td class="r">' + f(x3.amp20, 1) + '%</td>' +
              '<td class="r">' + f(x3.vol_ratio, 2) + '</td>' +
              '<td class="r">' + f(x3.close, 2) + '</td>' +
              '<td class="r" style="color:' + (x3.pct >= 0 ? C.up : C.down) + '">' + f(x3.pct, 2) + '%</td></tr>';
          }));
      }
      h += card('🧲 均线粘合待变盘池', mb, '四线粘合 = 蓄势待变盘，粘合越久越紧能量越大；「启动」标记=当日放量阳线站上全部均线，优先关注');
    }

    /* 断头铡刀 / 出水芙蓉 */
    var SW = D.trendsword;
    if (SW && (SW.hits.length || Object.keys(SW.stats || {}).length)) {
      var SWS = SW.stats || {}, zd = SWS.zhadao, fr = SWS.furong;
      var wb = '<div class="chips" style="margin-bottom:8px">';
      if (zd) wb += '<span class="chip" style="border-color:' + C.down + ';color:' + C.down + '>铡刀历史 ' + n2(zd.n) + ' 例·次日均 ' + f(zd.avg_t1, 2) + '%（胜率 ' + f(zd.win_t1, 0) + '%）</span>';
      if (fr) wb += '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '>芙蓉历史 ' + n2(fr.n) + ' 例·次日均 +' + f(fr.avg_t1, 2) + '%（胜率 ' + f(fr.win_t1, 0) + '%）</span>';
      wb += '</div>';
      if (SW.hits.length) {
        wb += table([{ t: '形态' }, { t: '标的' }, { t: '今日涨幅', a: 'r' }, { t: '穿线数', a: 'r' }, { t: '量比', a: 'r' }, { t: '现价', a: 'r' }],
          SW.hits.map(function (x4) {
            var isZd = x4.kind === 'zhadao';
            return '<tr><td style="color:' + (isZd ? C.down : C.up) + ';font-weight:700">' + (isZd ? '⚔ 断头铡刀' : '✦ 出水芙蓉') + '</td>' +
              '<td><b>' + E(x4.name) + '</b> <span class="muted">' + E(x4.code) + '</span></td>' +
              '<td class="r" style="color:' + (x4.pct >= 0 ? C.up : C.down) + '">' + f(x4.pct, 2) + '%</td>' +
              '<td class="r">' + n2(x4.n_lines) + ' 线</td>' +
              '<td class="r">' + f(x4.vol_ratio, 1) + '</td>' +
              '<td class="r">' + f(x4.close, 2) + '</td></tr>';
          }));
      } else {
        wb += '<div class="empty">今日无铡刀/芙蓉触发</div>';
      }
      h += card('⚔️ 断头铡刀 / 出水芙蓉', wb, '一根K线切断或站上多条均线的趋势级信号；铡刀破位宜止损回避，芙蓉放量上穿可关注回踩确认');
    }

    /* 尾盘偷袭监测 */
    var TR = D.tailraid;
    if (TR && (TR.raid_n || TR.dump_n)) {
      var tb = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">尾盘偷袭拉升 <b>' + n2(TR.raid_n) + '</b> 只</span>' +
        '<span class="chip" style="border-color:' + C.down + ';color:' + C.down + '">尾盘跳水 <b>' + n2(TR.dump_n) + '</b> 只</span>' +
        '<span class="chip">焦点池 ' + n2(TR.scanned) + ' 只（涨停/炸板/连板）</span>' +
        '</div>';
      var trRows = (TR.raids || []).slice(0, 5).map(function (x5) {
        return '<tr><td style="color:' + C.up + ';font-weight:700">🌙 偷袭拉升</td>' +
          '<td><b>' + E(x5.name) + '</b>' + (x5.habit >= 2 ? ' <span class="bd" style="border-color:' + C.warn + ';color:' + C.warn + ';font-size:10px;padding:0 4px">惯犯×' + x5.habit + '</span>' : '') + '</td>' +
          '<td class="r">尾盘 +' + f(x5.last30, 2) + '%</td>' +
          '<td class="r">全天 ' + f(x5.day_pct, 2) + '%</td>' +
          '<td class="r">尾盘额占比 ' + pct(x5.tail_amt * 100, 0) + '</td></tr>';
      }).concat((TR.dumps || []).slice(0, 4).map(function (x6) {
        return '<tr><td style="color:' + C.down + ';font-weight:700">💧 尾盘跳水</td>' +
          '<td><b>' + E(x6.name) + '</b></td>' +
          '<td class="r">尾盘 ' + f(x6.last30, 2) + '%</td>' +
          '<td class="r">全天 ' + f(x6.day_pct, 2) + '%</td>' +
          '<td class="r">尾盘额占比 ' + pct(x6.tail_amt * 100, 0) + '</td></tr>';
      }));
      if (trRows.length) {
        tb += table([{ t: '类型' }, { t: '标的' }, { t: '尾盘(14:30后)', a: 'r' }, { t: '全天', a: 'r' }, { t: '量能分布', a: 'r' }], trRows);
      }
      h += card('🌙 尾盘偷袭监测', tb, '分钟级数据定向扫描焦点池：14:30 后急拉=次日兑现风险/资金抢筹，尾盘跳水=出货警示；「惯犯」=近日常尾盘异动');
    }

    /* 关注股雷达 */
    var WL = D.watch;
    if (WL && WL.items && WL.items.length) {
      var wch = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip">关注池 <b>' + n2(WL.n) + '</b> 只</span>' +
        '<span class="chip" style="border-color:' + C.warn + ';color:' + C.warn + '">急讯 <b>' + n2(WL.alert_n || 0) + '</b> 条</span>' +
        '</div>';
      var wRows = WL.items.map(function (x7) {
        if (x7.no_data) {
          return '<tr><td colspan="5"><b>' + E(x7.name || x7.code) + '</b> <span class="muted">' + E(x7.code) + '</span> <span class="muted">— ' + (x7.note || '暂无K线') + '</span></td></tr>';
        }
        var sigHtml = (x7.signals || []).map(function (s) {
          var col = (s === '涨停' || s === '放量突破20日高' || s === '多头排列') ? C.up
            : (s === '跌停' || s === '炸板' || s === '趋势破位' || s === '空头排列') ? C.down : '';
          return '<span class="bd" style="' + (col ? 'border-color:' + col + ';color:' + col + ';' : '') + 'font-size:10px;padding:0 4px;margin-right:4px;white-space:nowrap">' + E(s) + '</span>';
        }).join(' ') || '<span class="muted">—</span>';
        return '<tr><td><b>' + E(x7.name || '') + '</b> <span class="muted">' + E(x7.code) + '</span>' +
          (x7.urgent ? ' <span class="bd" style="border-color:' + C.warn + ';color:' + C.warn + ';font-size:10px;padding:0 4px">急</span>' : '') + '</td>' +
          '<td class="r">' + f(x7.close, 2) + '</td>' +
          '<td class="r" style="color:' + (x7.pct >= 0 ? C.up : C.down) + '">' + f(x7.pct, 2) + '%</td>' +
          '<td class="r">' + f(x7.vol_ratio, 1) + '</td>' +
          '<td>' + sigHtml + '</td></tr>';
      });
      wch += table([{ t: '标的' }, { t: '现价', a: 'r' }, { t: '涨幅', a: 'r' }, { t: '量比', a: 'r' }, { t: '信号' }], wRows);
      h += card('⭐ 关注股雷达', wch, '自选（notify.json watch）与持仓关注股（holdings.json watch=true）的每日信号：涨停/跌停/炸板/破位为急讯，置顶展示');
    }

    /* 买卖区间与操作提示 */
    var ZN = D.zones;
    if (ZN && ZN.items && ZN.items.length) {
      var AL = ZN.alerts || {};
      var zActCol = function (a) {
        return a === '破位卖出' ? C.down
          : (a === '加仓提示' || a === '回踩买入区') ? C.up
          : (a === '逼近卖出' || a === '突破持有') ? C.warn : '';
      };
      var zch = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip">覆盖 <b>' + n2(ZN.n) + '</b> 只</span>' +
        (AL.sell && AL.sell.length ? '<span class="chip" style="border-color:' + C.down + ';color:' + C.down + '">🛑 破位 <b>' + AL.sell.length + '</b></span>' : '') +
        (AL.time && AL.time.length ? '<span class="chip" style="border-color:' + C.warn + ';color:' + C.warn + '">⏰ 周期到期 <b>' + AL.time.length + '</b></span>' : '') +
        (AL.add && AL.add.length ? '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">➕ 加仓 <b>' + AL.add.length + '</b></span>' : '') +
        (AL.take_profit && AL.take_profit.length ? '<span class="chip" style="border-color:' + C.warn + ';color:' + C.warn + '">🎯 逼近卖点 <b>' + AL.take_profit.length + '</b></span>' : '') +
        '</div>';
      var HCOL = { '短线': C.warn, '中线': C.up, '长线': '#5b9bff' };
      var zRows = ZN.items.map(function (z1) {
        var ac = zActCol(z1.action);
        var hc = HCOL[z1.horizon] || '';
        var zoneCell = function (zz) {
          return '<td class="r" style="font-variant-numeric:tabular-nums">' + f(zz[0], 2) + ' ~ ' + f(zz[1], 2) + '</td>';
        };
        var t = z1.targets || {};
        var tgtLine = '<div class="muted" style="font-size:10px;margin-top:2px">目标 ' +
          (t['短线'] ? '短' + f(t['短线'].price, 2) + '(' + t['短线'].days + 'd) ' : '') +
          (t['中线'] ? '中' + f(t['中线'].price, 2) + '(' + t['中线'].days + 'd) ' : '') +
          (t['长线'] ? '长' + f(t['长线'].price, 2) + '(' + t['长线'].days + 'd)' : '') + '</div>';
        var tsColor = z1.time_alert ? C.warn : C.muted || '#9aa';
        var tsLine = z1.time_status ? '<div style="font-size:10px;margin-top:2px;color:' + tsColor + '">' + E(z1.time_status) + '</div>' : '';
        var reasonHtml = (z1.reasons || []).slice(0, 1).map(function (r9) { return E(r9); }).join('；');
        var costHtml = (z1.cost ? '<div style="font-size:10px;margin-top:2px">成本 ' + f(z1.cost, 2) +
          ' <span style="color:' + (z1.pnl_pct >= 0 ? C.up : C.down) + '">' + (z1.pnl_pct >= 0 ? '+' : '') + f(z1.pnl_pct, 1) + '%</span></div>' : '');
        return '<tr><td><b>' + E(z1.name || '') + '</b> <span class="muted">' + E(z1.code) + '</span>' +
          (z1.horizon ? ' <span class="bd" style="border-color:' + hc + ';color:' + hc + ';font-size:10px;padding:0 4px">' + E(z1.horizon) + '</span>' : '') +
          (z1.chanlun_buy ? ' <span class="bd" style="border-color:' + C.up + ';color:' + C.up + ';font-size:10px;padding:0 4px">缠论' + E(z1.chanlun_buy) + '</span>' : '') +
          costHtml + '</td>' +
          '<td class="r"><b>' + f(z1.close, 2) + '</b> <span class="muted" style="font-size:10px">' + (z1.pct >= 0 ? '+' : '') + f(z1.pct, 2) + '%</span></td>' +
          zoneCell(z1.buy_zone) +
          zoneCell(z1.sell_zone) +
          '<td class="r" style="color:' + C.down + '">' + f(z1.stop, 2) + '</td>' +
          '<td><span class="bd" style="' + (ac ? 'border-color:' + ac + ';color:' + ac + ';' : '') + 'font-size:10px;padding:0 4px;white-space:nowrap">' + E(z1.action) + '</span>' +
          tgtLine + tsLine +
          (reasonHtml ? '<div class="muted" style="font-size:10px;margin-top:2px">' + reasonHtml + '</div>' : '') + '</td></tr>';
      });
      zch += table([{ t: '标的' }, { t: '现价', a: 'r' }, { t: '买入区间', a: 'r' }, { t: '卖出区间', a: 'r' }, { t: '止损', a: 'r' }, { t: '操作/周期目标' }], zRows);
      h += card('🎯 买卖区间', zch, '每只标注短线/中线/长线周期；目标价：短线=卖出区上沿(~5交易日)、中线=量度涨幅(~15日)、长线=长期高位(~60日)。「时间状态」对已持仓且建仓锚点可溯的票生效：到期未达目标或破位会提示了结/减仓，达目标提示止盈。区间为技术参考，非投资建议');
    }

    /* 推荐池胜率回溯 */
    var RP = D.recperf;
    if (RP && RP.dates && RP.dates.length) {
      var _cum = RP.dates.map(function (d, i) { return { l: d, v: RP.cumulative[i] }; });
      var _wr = RP.dates.map(function (d, i) { return { l: d, v: RP.win_rate[i] }; });
      var rpBody = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip">回溯 <b>' + n2(RP.n_days) + '</b> 交易日</span>' +
        '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">累计净值 <b>' + f(RP.final_cum, 2) + '</b></span>' +
        '<span class="chip">近30日盈利占比 <b>' + (RP.recent30.win_rate != null ? RP.recent30.win_rate : '—') + '%</b></span>' +
        '<span class="chip">均收益 <b>' + (RP.recent30.avg_pct != null ? RP.recent30.avg_pct : '—') + '%</b></span></div>';
      if (RP.cumulative && RP.cumulative.length) {
        rpBody += CH.svgLine(_cum, { w: 460, h: 150, color: C.up, fill: true, xlabels: 4 });
      }
      h += card('📈 推荐池胜率回溯', rpBody, '每日推荐标的 T+1 等权次日了结的累计净值曲线——回答「这系统长期到底准不准」');
    }

    /* 风格切换回测 */
    var SB = D.style_switch;
    if (SB && SB.n) {
      var _SCN = { crowd_trend: '核心资产抱团趋势', big_weight: '大盘权重主导', dual_track: '双轨市', micro_theme: '小微盘题材轮动', mid_relay: '中小盘连板接力', balanced: '均衡混合' };
      function style_cn(k) { return _SCN[k] || k; }
      var sbBody = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip">历史切换 <b>' + n2(SB.n) + '</b> 次</span>' +
        '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">后' + SB.look + '日上涨占比 <b>' + SB.up_rate + '%</b></span>' +
        '<span class="chip">均收益 <b>' + (SB.avg_ret >= 0 ? '+' : '') + SB.avg_ret + '%</b></span></div>';
      var btl = [];
      for (var _k in (SB.by_target || {})) {
        var _v = SB.by_target[_k];
        btl.push('<tr><td>' + E(style_cn(_k)) + '</td><td class="r">' + n2(_v.n) + '</td><td class="r" style="color:' + C.up + '">' + _v.up_rate + '%</td><td class="r">' + ( _v.avg_ret >= 0 ? '+' : '') + _v.avg_ret + '%</td></tr>');
      }
      if (btl.length) {
        sbBody += table([{ t: '切换去向风格' }, { t: '次数', a: 'r' }, { t: '上涨占比', a: 'r' }, { t: '平均收益', a: 'r' }], btl);
      }
      h += card('🔁 风格切换回测', sbBody, '统计历史上每次风格切换（如小微盘→权重）后 ' + SB.look + ' 日市场表现，给「切换日该怎么做」提供实证依据');
    }

    /* 龙虎榜席位 */
    var LB = D.lhbseats;
    if (LB && LB.n) {
      var lbBody = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip">上榜 <b>' + n2(LB.n) + '</b> 只</span>' +
        ((LB.reasons || []).length ? '<span class="chip">主因 ' + E((LB.reasons[0][0] || '').slice(0, 12)) + '</span>' : '') +
        '</div>';
      var lbRows = (LB.top || []).slice(0, 6).map(function (t) {
        return '<tr><td><b>' + E(t.name) + '</b> <span class="muted">' + E(t.code) + '</span></td>' +
          '<td class="r" style="color:' + C.up + '">净买 ' + f(t.net_yi, 2) + '亿</td>' +
          '<td class="r" style="color:' + (t.chg >= 0 ? C.up : C.down) + '">' + f(t.chg, 2) + '%</td>' +
          '<td class="muted" style="font-size:11px">' + E(t.reason || '') + '</td></tr>';
      });
      if (lbRows.length) lbBody += table([{ t: '个股' }, { t: '净买入', a: 'r' }, { t: '涨幅', a: 'r' }, { t: '上榜原因' }], lbRows);
      h += card('🏦 龙虎榜席位', lbBody, '今日上榜股净买额 TOP + 上榜原因：判断资金合力与性质（机构接力/游资对倒/散户抢筹）');
    }

    /* 雷区日历 */
    var RC = D.riskcal;
    if (RC && (RC.unlock_top && RC.unlock_top.length || RC.fin_due && RC.fin_due.length)) {
      var rcBody = '';
      if (RC.unlock_top && RC.unlock_top.length) {
        rcBody += '<div class="kv" style="margin:4px 0"><b style="color:var(--muted);font-size:12px">未来 ' + RC.horizon + ' 日解禁 TOP</b></div>';
        rcBody += RC.unlock_top.slice(0, 6).map(function (u) {
          return '<span class="bd" style="border-color:' + C.danger + ';color:' + C.danger + ';margin:2px 4px 2px 0;display:inline-block">' +
            E(u.name) + ' ' + E(u.day) + ' · ' + f(u.mv_yi, 1) + '亿(' + f(u.ratio, 1) + '%)</span>';
        }).join('');
      }
      if (RC.fin_due && RC.fin_due.length) {
        rcBody += '<div class="kv" style="margin:8px 0 4px"><b style="color:var(--muted);font-size:12px">财报披露临近（' + RC.fin_due.length + ' 只）</b></div>';
        rcBody += RC.fin_due.slice(0, 6).map(function (u) {
          return '<span class="bd" style="border-color:' + C.warn + ';color:' + C.warn + ';margin:2px 4px 2px 0;display:inline-block">' +
            E(u.name) + ' ' + E(u.day) + '</span>';
        }).join('');
      }
      h += card('⚠️ 解禁/财报雷区', rcBody, '未来两周限售解禁金额 TOP + 财报披露日，提前规避「好票突然暴雷」');
    }

    /* 大宗交易折价 */
    var BT = D.blocktrade;
    if (BT && BT.top && BT.top.length) {
      var btRows = BT.top.slice(0, 6).map(function (d) {
        return '<tr><td><b>' + E(d.name) + '</b> <span class="muted">' + E(d.code) + '</span></td>' +
          '<td class="r" style="color:' + C.down + '">折价 ' + f(d.discount, 2) + '%</td>' +
          '<td class="r">' + f(d.amt_yi, 2) + '亿</td></tr>';
      });
      h += card('📜 大宗交易折价', table([{ t: '个股' }, { t: '折价率', a: 'r' }, { t: '金额', a: 'r' }], btRows),
        '当日大宗交易中折价≥5% 的标的——折价率越高越偏出货/减持信号');
    }

    /* 两融余额 */
    var MG = D.margin;
    if (MG && MG.latest_yi != null) {
      var dlt = MG.delta_yi || 0;
      var mgBody = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip">两融余额 <b>' + n2(MG.latest_yi) + '亿</b></span>' +
        '<span class="chip" style="border-color:' + (dlt >= 0 ? C.up : C.down) + ';color:' + (dlt >= 0 ? C.up : C.down) + '">较前日 ' + (dlt >= 0 ? '+' : '') + f(dlt, 1) + '亿</span>' +
        '<span class="chip">杠杆情绪' + (dlt >= 0 ? '回升' : '回落') + '</span></div>';
      if (MG.series && MG.series.length) {
        mgBody += CH.svgLine(MG.series.map(function (s) { return { l: s.date, v: s.total_yi }; }),
          { w: 460, h: 130, color: C.gold, fill: true, xlabels: 4 });
      }
      h += card('💳 两融余额', mgBody, '全市场融资融券余额趋势——杠杆资金的情绪温度计');
    }

    /* ETF 资金流 */
    var EF = D.etfflow;
    if (EF && EF.top && EF.top.length) {
      var efBody = '<div class="chips" style="margin-bottom:8px"><span class="chip">全市场净流入 <b>' + f(EF.total_net_yi, 1) + '亿</b></span></div>';
      var efRows = EF.top.slice(0, 6).map(function (t) {
        return '<tr><td><b>' + E(t.name) + '</b> <span class="muted">' + E(t.code) + '</span></td>' +
          '<td class="r" style="color:' + C.up + '">净流入 ' + f(t.net_yi, 2) + '亿</td>' +
          '<td class="r" style="color:' + (t.chg >= 0 ? C.up : C.down) + '">' + f(t.chg, 0) + '%</td></tr>';
      });
      efBody += table([{ t: 'ETF' }, { t: '净流入', a: 'r' }, { t: '涨跌', a: 'r' }], efRows);
      h += card('🧺 ETF 资金流', efBody, '宽基/行业 ETF 份额与成交额变化——增量资金借道指数进场的风向标，亦作风格判定的第五维');
    }

    /* 游资席位画像 */
    var ST = D.seats;
    if (ST && ST.n_hits) {
      var stBody = '<div class="chips" style="margin-bottom:8px"><span class="chip">知名席位动作 <b>' + n2(ST.n_hits) + '</b> 条</span></div>';
      var stRows = (ST.hits || []).slice(0, 8).map(function (t) {
        var wr = (ST.stats && ST.stats[t.label]) ? (' · 跟随胜率' + ST.stats[t.label].win_rate + '%(' + ST.stats[t.label].n + '次)') : '';
        return '<tr><td><b>' + E(t.name) + '</b> <span class="muted">' + E(t.code) + '</span></td>' +
          '<td class="muted" style="font-size:11px">' + E(t.label) + '</td>' +
          '<td class="r" style="color:' + (t.net_yi >= 0 ? C.up : C.down) + '">净买 ' + f(t.net_yi, 2) + '亿</td>' +
          '<td class="r muted" style="font-size:11px">' + E(wr) + '</td></tr>';
      });
      if (stRows.length) stBody += table([{ t: '个股' }, { t: '席位(坊间)' }, { t: '净买', a: 'r' }, { t: '跟随', a: 'r' }], stRows);
      h += card('🐉 游资席位画像', stBody, '识别当日龙虎榜上的知名游资营业部（坊间归因，仅供参考），并给出其历史 T+1 跟随胜率，辅助判断是否值得跟');
    }

    /* 题材主线 */
    var TH = D.theme;
    if (TH && TH.main_theme) {
      var thBody = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">主线 <b>' + E(TH.main_theme) + '</b></span>' +
        '<span class="chip">' + n2(TH.main_n) + ' 只涨停贡献</span>' +
        (TH.signal && TH.signal.streak >= 2 ? '<span class="chip" style="border-color:' + C.gold + ';color:' + C.gold + '">持续 ' + TH.signal.streak + ' 日</span>' : '') +
        (TH.signal && TH.signal.verdict && TH.signal.verdict !== '主线延续' ? '<span class="chip" style="border-color:' + C.warn + ';color:' + C.warn + '">' + E(TH.signal.verdict) + '</span>' : '') +
        '</div>';
      var thRows = (TH.sub_themes || []).map(function (s) {
        return '<tr><td><b>' + E(s.theme) + '</b></td><td class="r">' + n2(s.n) + '</td></tr>';
      });
      if (thRows.length) thBody += table([{ t: '支线题材' }, { t: '涨停数', a: 'r' }], thRows);
      h += card('🧭 题材主线', thBody, '对涨停股概念聚类，判定当日主线/支线、主线持续天数与退潮预警，接入情绪分');
    }

    /* 连续信号 */
    var SG = D.signals;
    if (SG) {
      var sgBody = '<div class="bullets">';
      if (SG.margin && SG.margin.verdict && SG.margin.verdict !== '中性') sgBody += '<div class="b">💳 ' + E(SG.margin.verdict) + '</div>';
      if (SG.etf && SG.etf.verdict && SG.etf.verdict !== '中性') sgBody += '<div class="b">🧺 ' + E(SG.etf.verdict) + '</div>';
      if (SG.lhb && SG.lhb.verdict && SG.lhb.verdict !== '龙虎榜活跃度平稳') sgBody += '<div class="b">🏦 ' + E(SG.lhb.verdict) + '</div>';
      (SG.seat_repeat || []).slice(0, 4).forEach(function (r) {
        sgBody += '<div class="b">🐉 ' + E(r.name) + ' 被知名席位反复净买 ' + r.times + ' 次（' + r.labels.slice(0, 2).map(E).join('、') + '）</div>';
      });
      sgBody += '</div>';
      var sgHas = (SG.margin && SG.margin.verdict && SG.margin.verdict !== '中性') ||
        (SG.etf && SG.etf.verdict && SG.etf.verdict !== '中性') ||
        (SG.lhb && SG.lhb.verdict && SG.lhb.verdict !== '龙虎榜活跃度平稳') ||
        (SG.seat_repeat && SG.seat_repeat.length);
      h += card('📡 连续信号', sgHas ? sgBody : '<div class="empty">样本积累中（连续信号需多日历史）</div>',
        '跨日硬信号：两融/ETF 连续净流入、龙虎榜活跃度、知名席位重复扫货——单日快照看不到，靠历史序列提炼');
    }

    /* 缠论结构 */
    var CL = D.chanlun;
    if (CL && CL.candidates && CL.candidates.length) {
      var buys = CL.buys || [];
      var clBody = '<div class="chips" style="margin-bottom:8px"><span class="chip">分析 <b>' + n2(CL.n_analyzed) + '</b> 只</span>' +
        '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">买点候选 <b>' + n2(buys.length) + '</b> 只</span></div>';
      var clRows = buys.slice(0, 8).map(function (c) {
        var cc = c.signal === '一买' ? C.up : c.signal === '二买' ? C.gold : c.signal === '三买' ? C.blue : C.muted;
        var extra = (c.zhongshu ? (' · 中枢[' + c.zhongshu[0] + ',' + c.zhongshu[1] + ']') : '') + (c.beichi ? (' · ' + (c.beichi === 'down' ? '底背驰' : '顶背驰')) : '');
        return '<tr><td><b>' + E(c.name) + '</b> <span class="muted">' + E(c.code) + '</span></td>' +
          '<td class="r" style="color:' + cc + ';font-weight:bold">' + E(c.signal) + '</td>' +
          '<td class="muted" style="font-size:11px">' + E(c.reason || '') + E(extra) + '</td></tr>';
      });
      if (!clRows.length) {
        clRows = CL.candidates.slice(0, 8).map(function (c) {
          return '<tr><td><b>' + E(c.name) + '</b> <span class="muted">' + E(c.code) + '</span></td>' +
            '<td class="r">' + E(c.last_dir) + '</td>' +
            '<td class="muted" style="font-size:11px">' + E(c.signal) + (c.beichi ? ' · 背驰' : '') + '</td></tr>';
        });
      }
      clBody += table([{ t: '个股' }, { t: '信号', a: 'r' }, { t: '结构' }], clRows);
      h += card('🌀 缠论结构', clBody, '笔-中枢-背驰框架：对推荐池/涨停股跑缠论，标出一/二/三买点与底背驰——结构化择时参照（算法遵循标准缠论定义）');
    }

    /* 相似形态检索 */
    var PS = D.patsim;
    if (PS && PS.items && PS.items.length) {
      var psRows = PS.items.slice(0, 8).map(function (it) {
        var top = (it.matches || [])[0];
        var sim = top ? ('≈ ' + E(top.name) + (top.fwd10 != null ? (' · 其后10日' + (top.fwd10 >= 0 ? '+' : '') + f(top.fwd10, 1) + '%') : '')) : '—';
        return '<tr><td><b>' + E(it.name) + '</b></td><td>' + sim + '</td>' +
          '<td class="r">' + it.matches_up + '/' + (it.matches ? it.matches.length : 0) + ' 涨</td></tr>';
      });
      h += card('🧬 相似形态检索', table([{ t: '焦点股' }, { t: '最相似标的/后续' }, { t: '10日上涨', a: 'r' }], psRows),
        '把焦点股近 20 日形态与全市场比对，找出历史上最像的标的并回看其后续表现（形态基因映射）');
    }

    /* 关注股网页化管理：仅 owner 显示入口 */
    if (window.__SA_USER__ === 'owner') {
      h += '<div class="sa-mgmt-actions" style="margin:6px 0 18px"><button class="mbtn mbtn-p" id="wlMgrBtn">⭐ 管理关注股</button>' +
        '<span class="muted">增删自选，保存后写回云端并重建（需管理密钥/令牌）</span></div>';
    }

    /* 市场可视化：温度走势(双线) + 板块涨停 TOP10 */
    var VZ = D.viz;
    if (VZ && VZ.temp && VZ.temp.length) {
      var _pts = VZ.temp.map(function (e) { return { l: e.d, v: e.zt, v2: e.lb }; });
      var _temp = CH.svgLine(_pts, {
        w: 460, h: 178, dual: true, color: C.up, color2: C.gold,
        legend: [{ l: '涨停家数', c: C.up }, { l: '连板高度(板)', c: C.gold }]
      });
      var _sec = (VZ.sector_zt || []).map(function (s) {
        var c = s.tier === '主线' ? C.up : s.tier === '支线' ? C.gold : C.blue;
        return { l: s.name, v: s.zt, c: c };
      });
      var _secChart = _sec.length ? CH.svgHBar(_sec, { w: 460, fmt: function (v) { return v + ' 只'; } })
                                  : '<div class="empty">无</div>';
      var _vizBody = '<div style="margin-bottom:10px"><b style="color:var(--muted);font-size:12px">市场温度走势（近 ' +
        VZ.temp.length + ' 日）</b>' + _temp + '</div>' +
        '<div><b style="color:var(--muted);font-size:12px">板块涨停家数 TOP10</b>' + _secChart + '</div>';
      h += card('📊 市场可视化 · 温度/主线', _vizBody,
        '涨停家数与连板高度双线刻画市场温度；板块涨停 TOP10 一眼看清新老主线');
    }

    /* 盘前策略：仓位 / 主线 / 接力 / 关注池 / 风险 一屏速览 */
    var PP = D.preopen_plan;
    if (PP && PP.position) {
      var _pp = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip" style="border-color:' + C.gold + ';color:' + C.gold + '">建议仓位 <b>' + E(PP.position) + '</b></span>' +
        (PP.main_line && PP.main_line.length ? '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">主线预判 ' + PP.main_line.map(E).join('/') + '</span>' : '') +
        (PP.relay_dir && PP.relay_dir.length ? '<span class="chip" style="border-color:' + C.purple + ';color:' + C.purple + '">接力方向 ' + PP.relay_dir.map(E).join('/') + '</span>' : '') +
        '</div>';
      if (PP.strategies && PP.strategies.length) {
        _pp += '<div class="bullets">' + PP.strategies.slice(0, 5).map(function (s) { return '<div class="b">· ' + E(s) + '</div>'; }).join('') + '</div>';
      }
      if (PP.watch && PP.watch.length) {
        _pp += '<div style="margin-top:8px"><b style="color:var(--muted);font-size:12px">关注池（前排）</b><div class="chips" style="margin-top:4px">' +
          PP.watch.map(function (w) {
            var _tag = w.relay_dir ? '<span class="bd" style="border-color:' + C.purple + ';color:' + C.purple + ';font-size:10px;padding:0 4px">接力</span>' : '';
            return '<span class="chip">' + E(w.name) + (w.streak ? ' <b>' + w.streak + '板</b>' : '') +
              ' <span class="muted">' + E(w.reason) + '</span>' + _tag + '</span>';
          }).join('') + '</div></div>';
      }
      if (PP.risks && PP.risks.length) {
        _pp += '<div style="margin-top:8px"><b style="color:var(--muted);font-size:12px">风险提醒</b><div class="note" style="margin-top:4px">' +
          PP.risks.map(E).join('；') + '</div></div>';
      }
      h += card('🎯 盘前策略', _pp, '聚合仓位/主线/接力/关注池/风险，开盘前一眼看清今日该怎么打');
    }

    /* 竞价定调 */
    var A0 = D.auction || {};
    if (A0 && A0.summary) {
      var s2 = A0.summary;
      var read = (s2.weak_strong >= s2.strong_weak)
        ? '资金竞价阶段整体偏积极，弱转强多于强转弱，低位承接有力'
        : '竞价阶段分歧加大，强转弱多于弱转强，高位需防兑现';
      h += card('⚡ 竞价定调', '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip">平均高开 <b>' + (s2.avg_open_pct >= 0 ? '+' : '') + f(s2.avg_open_pct, 2) + '%</b></span>' +
        '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">一字板 <b>' + n2(s2.yizi) + '</b></span>' +
        '<span class="chip" style="border-color:' + C.gold + ';color:' + C.gold + '">弱转强 <b>' + n2(s2.weak_strong) + '</b></span>' +
        '<span class="chip" style="border-color:' + C.danger + ';color:' + C.danger + '">强转弱 <b>' + n2(s2.strong_weak) + '</b></span>' +
        '<span class="chip">T字板 <b>' + n2(s2.t_board) + '</b></span>' +
        '<span class="chip" style="border-color:' + (s2.vol_warn > 0 ? C.danger : C.blue) + ';color:' +
        (s2.vol_warn > 0 ? C.danger : C.blue) + '">量能异动 <b>' + n2(s2.vol_anomaly || 0) + '</b></span></div>' +
        '<div class="note">' + E(read) + '</div>',
        '集合竞价阶段全市场定调（基于涨停股开盘/收盘行为离线重建）');
    }

    /* 外围市场定调（美股/日股/韩股 → 推断 A 股次日氛围） */
    var G = D.global_market || {};
    if (G && (G.available || (G.indices && G.indices.length))) {
      var gdetail = G.detail || '';
      var gbody = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip" style="border-color:' + (G.score >= 0 ? C.up : C.down) + ';color:' + (G.score >= 0 ? C.up : C.down) + '">外围信号 ' + E(G.signal || '中性') + '</span>' +
        '<span class="chip">A股次日上涨概率 <b>' + f(G.a_up_prob, 0) + '%</b></span>' +
        (G.us_pct !== null && G.us_pct !== undefined ? '<span class="chip">美股 <b>' + (G.us_pct >= 0 ? '+' : '') + f(G.us_pct, 2) + '%</b></span>' : '') +
        (G.hk_pct !== null && G.hk_pct !== undefined ? '<span class="chip">港股 <b>' + (G.hk_pct >= 0 ? '+' : '') + f(G.hk_pct, 2) + '%</b></span>' : '') +
        (G.jp_pct !== null && G.jp_pct !== undefined ? '<span class="chip">日经 <b>' + (G.jp_pct >= 0 ? '+' : '') + f(G.jp_pct, 2) + '%</b></span>' : '') +
        (G.kr_pct !== null && G.kr_pct !== undefined ? '<span class="chip">韩国 <b>' + (G.kr_pct >= 0 ? '+' : '') + f(G.kr_pct, 2) + '%</b></span>' : '') +
        (G.etfs && G.etfs.length ? '<span class="chip">ETF ' + G.etfs.map(function (e) { return E(e.name) + (e.pct >= 0 ? '+' : '') + f(e.pct, 1) + '%'; }).join(' ') + '</span>' : '') +
        '</div>';
      if (G.indices && G.indices.length) {
        gbody += '<div class="tbl-wrap"><table><thead><tr><th>市场</th><th>指数</th><th class="r">涨跌幅</th></tr></thead><tbody>' +
          G.indices.map(function (x) {
            return '<tr><td class="muted">' + E(x.region || '') + '</td><td class="name">' + E(x.name || x.code || '') +
              '</td><td class="r num ' + (x.pct >= 0 ? 'up' : 'down') + '">' + (x.pct >= 0 ? '+' : '') + f(x.pct, 2) + '%</td></tr>';
          }).join('') + '</tbody></table></div>';
      }
      gbody += '<div class="note">' + E(gdetail || '外围数据缺失，按中性处理') + '</div>';
      h += card('🌐 外围市场定调（美股/港股/日股/韩股/ETF → A股次日）', gbody,
        '外围涨跌通过情绪传导影响 A 股次日开盘方向与强度');
    }

    /* 历史连板热度研判（结合历史库校准推荐，记录状态为后期提供依据） */
    var RG = D.regime || {};
    if (RG && RG.level) {
      var rgbody = '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip" style="border-color:' + (RG.factor > 0.35 ? C.danger : RG.factor > 0 ? C.gold : C.ok) + ';color:' +
        (RG.factor > 0.35 ? C.danger : RG.factor > 0 ? C.gold : C.ok) + '">周期定位 ' + E(RG.level) + '</span>' +
        '<span class="chip">校准因子 <b>' + f(RG.factor, 2) + '</b></span>' +
        (RG.peak_max !== null && RG.peak_max !== undefined ? '<span class="chip">历史峰值 <b>' + n2(RG.peak_max) + ' 板</b></span>' : '') +
        (RG.cur_h !== null && RG.cur_h !== undefined ? '<span class="chip">当前高度 <b>' + n2(RG.cur_h) + ' 板</b></span>' : '') +
        (RG.hit_rate !== null && RG.hit_rate !== undefined ? '<span class="chip">历史推荐命中率 <b>' + f(RG.hit_rate, 1) + '%</b></span>' : '') +
        '</div>';
      rgbody += '<div class="note">' + E(RG.note || '') + '</div>';
      if (RG.hit_by_tag && Object.keys(RG.hit_by_tag).length) {
        rgbody += '<div class="note" style="margin-top:6px">各类标签历史续板命中：' +
          Object.keys(RG.hit_by_tag).map(function (k) { return E(k) + ' <b>' + f(RG.hit_by_tag[k], 1) + '%</b>'; }).join(' · ') + '</div>';
      }
      h += card('📚 历史连板热度研判', rgbody,
        '结合历史连板库（如华电辽能 8 板后衰减）校准次日断板/买入价值，记录每日状态供后期复盘');
    }

    /* 情绪温度计 + 周期 */
    var gaugeBody = '<div class="gauge-wrap">' +
      '<div style="flex:0 0 240px">' + CH.svgGauge(st.score) + '</div>' +
      '<div class="gauge-txt"><div class="lv" style="color:' +
      (st.score >= 60 ? C.up : st.score >= 45 ? C.gold : C.down) + '">' +
      f(st.score, 1) + ' <span style="font-size:17px">分 · ' + E(st.level || '') + '</span></div>' +
      '<div class="lb">' + E(st.label || '') + '</div></div></div>';
    var comps = (st.components || []);
    if (comps.length) {
      gaugeBody += '<div style="margin-top:14px">' + CH.svgRadar(comps.map(function (c) {
        return { l: c.k, v: c.score };
      }), { w: 320, h: 280 }) + '</div>';
      gaugeBody += table(
        [{ t: '维度' }, { t: '含义' }, { t: '实测', a: 'r' }, { t: '得分', a: 'r' }, { t: '权重', a: 'r' }],
        comps.map(function (c) {
          return '<tr><td class="name">' + E(c.k) + '</td><td class="muted" style="white-space:normal">' + E(c.desc) +
            '</td><td class="r num">' + (c.raw === null || c.raw === undefined ? '—' : c.raw + (c.unit || '')) +
            '</td><td class="r num">' + qBar(c.score) + ' ' + f(c.score, 0) +
            '</td><td class="r num faint">' + (c.w * 100).toFixed(0) + '%</td></tr>';
        }));
    }

    var cycBody = '<div style="font-size:22px;font-weight:750;color:' +
      (cy.phase === '高潮期' ? C.up : (cy.phase === '退潮期' || cy.phase === '冰点期') ? C.down : C.blue) + '">' +
      E(cy.phase || '—') + '</div>' +
      '<div class="note" style="margin:6px 0 12px">' + E(cy.desc || '') + '</div>' +
      '<ul class="list-tight">' + (cy.evidence || []).map(function (x) { return '<li>' + E(x) + '</li>'; }).join('') + '</ul>';
    /* 次日推演 */
    cycBody += '<div style="margin-top:14px;padding-top:12px;border-top:1px dashed var(--border)">' +
      '<div style="font-size:12px;font-weight:700;margin-bottom:6px">次日操作基调</div>' +
      '<div class="note">建议仓位 <b>' + E((D.recommend || {}).position || '—') + '</b>。' +
      E(((D.recommend || {}).strategies || [])[0] || '') + '</div></div>';

    h += '<div class="split">' +
      card('🌡️ 场内情绪温度计', gaugeBody, '7 维加权 · 0–100 分') +
      card('🔄 情绪周期定位', cycBody, '近 6 个交易日趋势判定') + '</div>';

    /* 短线情绪微观结构（首板/断层/晋级率分档/炸板率/赚钱效应细分） */
    var MIC = D.micro || {};
    if (MIC && MIC.zt !== undefined) {
      var pf = MIC.profit || {}, pt = MIC.promote_tiered || {}, fb = MIC.first_board || {};
      var mbody = '<div class="grid g3" style="gap:10px;margin-bottom:10px">' +
        kpi('首板', (fb.count || 0) + ' 只', '新题材试错入口') +
        kpi('最高连板', MIC.max_lb + ' 板', (MIC.gap && MIC.gap.length) ? ('断层缺 ' + MIC.gap.join('/') + ' 板') : '梯队完整') +
        kpi('炸板率', (MIC.zhaban_rate == null ? '—' : MIC.zhaban_rate + '%'), '分歧/派发信号') + '</div>';
      mbody += '<table class="tbl"><tr><th>赚钱效应</th><th>翻红率</th><th>再涨停率</th><th>平均涨幅</th><th>亏钱(翻绿)</th></tr><tr>' +
        '<td class="r num">昨涨停今均</td><td class="r num">' + f(pf.red_rate, 1) + '%</td><td class="r num">' + f(pf.again_rate, 1) + '%</td>' +
        '<td class="r num ' + (pf.avg_pct >= 0 ? '' : '') + '">' + f(pf.avg_pct, 1) + '%</td><td class="r num">' + f(pf.green_rate, 1) + '%</td></tr></table>';
      mbody += '<table class="tbl" style="margin-top:8px"><tr><th>晋级率分档</th><th>1进2</th><th>2进3</th><th>3板及以上</th></tr><tr>' +
        '<td>今日各档</td><td class="r num">' + f(pt['1进2'], 1) + '%</td><td class="r num">' + f(pt['2进3'], 1) + '%</td><td class="r num">' + f(pt['3板及以上'], 1) + '%</td></tr></table>';
      h += card('🔬 短线情绪微观结构', mbody, '首板/梯队断层/晋级率分档/炸板率/赚钱效应细分 · 纯计算');
    }

    /* 近5日板块热度趋势 + 龙头谱系（题材持续性/退潮追踪） */
    function spark(ser) {
      var w = 150, h = 26, vs = ser.map(function (d) { return d.v; });
      var mn = Math.min.apply(null, vs), mx = Math.max.apply(null, vs), rg = (mx - mn) || 1;
      var pts = ser.map(function (d, i) {
        var x = ser.length > 1 ? (i / (ser.length - 1)) * w : 0;
        var y = h - ((d.v - mn) / rg) * (h - 4) - 2;
        return x.toFixed(1) + ',' + y.toFixed(1);
      }).join(' ');
      return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">' +
        '<polyline fill="none" stroke="' + C.blue + '" stroke-width="1.6" points="' + pts + '"/></svg>';
    }
    var STH = D.sector_trend_hist || {};
    if (STH.trend && STH.trend.length) {
      var trows = STH.trend.map(function (s) {
        var ser = (s.strength || []).map(function (v, i) { return { l: (STH.dates[i] || '').slice(5), v: v }; });
        return '<tr><td class="name">' + E(s.name) + '</td><td>' + spark(ser) + '</td>' +
          '<td class="r num">' + f(s.strength[s.strength.length - 1], 0) + '</td>' +
          '<td class="r num ' + (s.drift === '升温' ? '' : (s.drift === '降温' ? '' : '')) + '">' +
          '<span style="color:' + (s.drift === '升温' ? C.up : s.drift === '降温' ? C.down : C.gray) + '">' + s.drift +
          ' ' + (s.delta >= 0 ? '+' : '') + f(s.delta, 0) + '</span></td></tr>';
      }).join('');
      var lbody = '<table class="tbl"><tr><th>板块</th><th>5日强度</th><th>最新</th><th>趋势</th></tr>' + trows + '</table>';
      if (STH.lineage && STH.lineage.length) {
        var lrows = STH.lineage.map(function (x) {
          var now = x.lead_now ? (x.lead_now.name + '(' + x.lead_now.streak + '板)') : '—';
          var old = x.lead_5d_ago ? (x.lead_5d_ago.name + '(' + x.lead_5d_ago.streak + '板)') : '—';
          var op = x.lead_old_today_pct;
          return '<tr><td class="name">' + E(x.sector) + '</td><td>' + E(now) + '</td><td>' + E(old) + '</td>' +
            '<td class="r num ' + (op >= 0 ? '' : '') + '"><span style="color:' + (op == null ? C.gray : op >= 0 ? C.up : C.down) + '">' +
            (op == null ? '—' : f(op, 1) + '%') + '</span></td></tr>';
        }).join('');
        lbody += '<div style="margin-top:10px;font-size:12px;font-weight:700">龙头谱系（5日前领涨股现状）</div>' +
          '<table class="tbl"><tr><th>主线板块</th><th>今日领涨</th><th>5日前领涨</th><th>现状</th></tr>' + lrows + '</table>';
      }
      h += card('🧭 近5日板块热度趋势 · 龙头谱系', lbody, '题材持续性/退潮追踪：升温板块可跟随，降温+龙头跌=退潮');
    }

    /* 竞价强度定调（涨停梯队集合竞价强弱 · 离线重建） */
    var AUC = D.auction || {};
    var MV = AUC.market_view || {};
    if (MV && MV.avg_score !== undefined) {
      var aqBody = '<div class="grid g3" style="gap:10px;margin-bottom:10px">' +
        kpi('竞价强度', MV.avg_score + ' 分', '涨停股竞价定调 · ' + MV.strength) +
        kpi('平均高开', (AUC.summary ? f(AUC.summary.avg_open_pct, 2) : '—') + '%', '一字板 ' + (AUC.summary ? AUC.summary.yizi : 0) + ' 只') +
        kpi('弱转强/强转弱', (AUC.summary ? AUC.summary.weak_strong : 0) + ' / ' + (AUC.summary ? AUC.summary.strong_weak : 0), '竞价分歧转向') + '</div>';
      var gd = MV.gap_dist || {}, gdTotal = 0;
      Object.keys(gd).forEach(function (k) { gdTotal += gd[k]; }); gdTotal = gdTotal || 1;
      var gdColors = { '一字': C.gold, '大幅高开': C.up, '高开': C.up, '平开': C.gray, '低开': C.down };
      var gdBar = '<div style="display:flex;height:18px;border-radius:4px;overflow:hidden;margin:4px 0 2px">';
      Object.keys(gd).forEach(function (k) {
        var w = gd[k] / gdTotal * 100;
        if (w > 0) gdBar += '<div title="' + k + ' ' + gd[k] + '" style="width:' + w.toFixed(1) + '%;background:' + (gdColors[k] || C.gray) + '"></div>';
      });
      gdBar += '</div><div style="font-size:11px;color:' + C.faint + '">高开分布：' +
        Object.keys(gd).map(function (k) { return k + ' ' + gd[k]; }).join(' · ') + '</div>';
      aqBody += gdBar;
      var qc = (MV.qiangchou || []).map(function (x) { return E(x.name) + '(' + x.streak + '板 +' + f(x.open_pct, 1) + '%)'; }).join('、') || '—';
      var pf = (MV.paifa || []).map(function (x) { return E(x.name) + '(' + x.streak + '板 +' + f(x.open_pct, 1) + '%)'; }).join('、') || '—';
      aqBody += '<div style="margin-top:8px;font-size:12px"><span style="color:' + C.up + '">抢筹放量</span>：' + qc + '</div>' +
        '<div style="font-size:12px;margin-top:2px"><span style="color:' + C.down + '">派发预警</span>：' + pf + '</div>';
      h += card('🔥 竞价强度定调', aqBody, '涨停股集合竞价强弱（离线重建 · 不需盘中逐笔）');
    }

    /* 主力/北向资金流向 */
    var MONEY = D.money || {};
    if (MONEY && MONEY.boards_in) {
      var mi = MONEY.boards_in || [], mo = MONEY.boards_out || [];
      var north = MONEY.north;
      var mbody = '<div class="grid g3" style="gap:10px;margin-bottom:10px">' +
        kpi('全市场主力净流入', (MONEY.total_main_net == null ? '—' : (MONEY.total_main_net >= 0 ? '+' : '') + MONEY.total_main_net + ' 亿'), '净流入板块 ' + (MONEY.net_in_boards || 0) + ' / 流出 ' + (MONEY.net_out_boards || 0)) +
        kpi('北向资金', north ? ('+' + north.total + ' 亿') : '数据源停更', north ? ('沪 ' + north.sh + ' / 深 ' + north.sz) : '东财口径调整') +
        kpi('净流入行业Top', (mi[0] ? E(mi[0].name) : '—'), mi[0] ? (E(mi[0].net) + ' 亿') : '') + '</div>';
      var inRows = mi.map(function (b) {
        return '<tr><td class="name">' + E(b.name) + '</td><td class="r num" style="color:' + C.up + '">+' + f(b.net, 1) + '亿</td><td class="r num">' + f(b.rate, 1) + '%</td></tr>';
      }).join('');
      var outRows = mo.map(function (b) {
        return '<tr><td class="name">' + E(b.name) + '</td><td class="r num" style="color:' + C.down + '">' + f(b.net, 1) + '亿</td><td class="r num">' + f(b.rate, 1) + '%</td></tr>';
      }).join('');
      mbody += '<div style="display:flex;gap:14px;flex-wrap:wrap">' +
        '<div style="flex:1;min-width:240px"><div style="font-size:12px;font-weight:700;color:' + C.up + ';margin-bottom:4px">主力净流入行业 Top10</div>' +
        '<table class="tbl"><tr><th>行业</th><th>净流入</th><th>净率</th></tr>' + inRows + '</table></div>' +
        '<div style="flex:1;min-width:240px"><div style="font-size:12px;font-weight:700;color:' + C.down + ';margin-bottom:4px">主力净流出行业 Top5</div>' +
        '<table class="tbl"><tr><th>行业</th><th>净流入</th><th>净率</th></tr>' + outRows + '</table></div></div>';
      h += card('💰 主力/北向资金流向', mbody, '行业板块主力净流入排行（东财实时）· 北向若空白为数据源停更');
    }

    /* 选股回测（历史真实推荐 + K线前向收益） */
    var BT = D.backtest || {};
    if (BT && BT.total) {
      function btCell(s) {
        if (!s) return '<td class="r num">—</td>';
        var col = s.win >= 55 ? C.up : (s.win < 45 ? C.down : C.gold);
        return '<td class="r num"><span style="color:' + col + '">' + s.win + '%</span><br><span style="font-size:10px;color:' + C.faint + '">均' + (s.avg >= 0 ? '+' : '') + s.avg + '%</span></td>';
      }
      var bbody = '<table class="tbl"><tr><th>持有周期</th><th>样本</th><th>胜率/均收益</th></tr>' +
        '<tr><td>次日 +1</td><td class="r num">' + (BT.h1 ? BT.h1.n : '—') + '</td>' + btCell(BT.h1) + '</tr>' +
        '<tr><td>持有 +3</td><td class="r num">' + (BT.h3 ? BT.h3.n : '—') + '</td>' + btCell(BT.h3) + '</tr>' +
        '<tr><td>持有 +5</td><td class="r num">' + (BT.h5 ? BT.h5.n : '—') + '</td>' + btCell(BT.h5) + '</tr></table>';
      var btg = BT.by_tag || {};
      var tk = Object.keys(btg);
      if (tk.length) {
        var trows = tk.map(function (t) {
          var v = btg[t];
          var col = v.win >= 55 ? C.up : (v.win < 45 ? C.down : C.gold);
          return '<tr><td class="name">' + E(t) + '</td><td class="r num">' + v.n + '</td>' +
            '<td class="r num"><span style="color:' + col + '">' + v.win + '%</span></td>' +
            '<td class="r num ' + (v.avg >= 0 ? '' : '') + '"><span style="color:' + (v.avg >= 0 ? C.up : C.down) + '">' + (v.avg >= 0 ? '+' : '') + v.avg + '%</span></td></tr>';
        }).join('');
        bbody += '<div style="margin-top:8px;font-size:12px;font-weight:700">分类型胜率（样本≥3）</div>' +
          '<table class="tbl"><tr><th>类型</th><th>样本</th><th>胜率</th><th>均收益</th></tr>' + trows + '</table>';
      }
      h += card('📊 选股回测', bbody, '基于每日真实推荐 + K线前向收益 · 自证策略有效性');
    }

    /* 时间序列 */
    if (ser.length >= 3) {
      var sub = ser.slice(-30);
      var l1 = sub.map(function (x) { return { l: x.date.slice(5), v: x.zt, v2: x.dt }; });
      var l2 = sub.map(function (x) { return { l: x.date.slice(5), v: x.yest_perf === null ? 0 : x.yest_perf }; });
      var l3 = sub.map(function (x) { return { l: x.date.slice(5), v: x.promote_rate === null ? 0 : x.promote_rate, v2: x.max_lb * 10 }; });
      h += '<div class="grid g2">' +
        card('📈 涨停 / 跌停家数（近30日）', CH.svgLine(l1, {
          dual: true, color: C.up, color2: C.down, w: 470,
          legend: [{ l: '涨停家数', c: C.up }, { l: '跌停家数', c: C.down }]
        }), '情绪总量') +
        card('💰 赚钱效应曲线（近30日）', CH.svgLine(l2, {
          color: C.blue, w: 470, zeroBase: true,
          legend: [{ l: '昨日涨停股今日平均涨幅 %', c: C.blue }]
        }), '正值=接力有肉，负值=亏钱') +
        card('🪜 晋级率与市场高度（近30日）', CH.svgLine(l3, {
          dual: true, color: C.gold, color2: C.purple, w: 470,
          legend: [{ l: '连板晋级率 %', c: C.gold }, { l: '最高连板 ×10', c: C.purple }]
        }), '晋级率是连板可持续性的核心指标') +
        card('🏛️ 场外指数环境', indexTable(), '大盘环境决定题材容错空间') +
        '</div>' +
        '<div style="margin-top:16px">' + pushCard() + '</div>';
    }

    /* 市场热度（标杆趋势股交易额度） */
    var bh = mk.bench_heat || {};
    if (bh && bh.stocks && bh.stocks.length) {
      var heatColor = bh.level === '热' ? C.up : bh.level === '冷' ? C.down : C.gold;
      var bhRows = bh.stocks.map(function (s) {
        var ratioColor = s.amt_ratio >= 1.15 ? C.up : s.amt_ratio <= 0.82 ? C.down : C.gray;
        var tColor = s.trending ? C.up : C.down;
        return '<tr><td class="name">' + E(s.name) + '</td>' +
          '<td class="r num">' + f(s.amt, 1) + '亿</td>' +
          '<td class="r num" style="color:' + ratioColor + '">' + f(s.amt_ratio, 2) + '×</td>' +
          '<td class="r num" style="color:' + (s.avg_daily >= 0 ? C.up : C.down) + '">' + (s.avg_daily >= 0 ? '+' : '') + f(s.avg_daily, 2) + '%</td>' +
          '<td class="c"><span style="color:' + tColor + ';font-weight:700">' + (s.trending ? '多头' : '破位') + '</span></td></tr>';
      });
      var zb = mk.zhaban_stats || {};
      var zbTxt = zb && zb.samples ? ('炸板股次日：平均' + (zb.avg_next_close >= 0 ? '+' : '') + f(zb.avg_next_close, 2) + '%、收绿率' + f(zb.green_rate, 0) + '%、反包涨停率' + f(zb.limitup_rate, 1) + '%') : '';
      var bhBody = '<div style="display:flex;align-items:baseline;gap:14px;margin-bottom:10px">' +
        '<span style="font-size:26px;font-weight:800;color:' + heatColor + '">' + E(bh.level) + '</span>' +
        '<span class="muted">整体热度</span>' +
        '<span style="margin-left:auto;font-size:12px" class="muted">标杆股合计成交额 <b style="color:var(--text)">' + f(bh.total_amt, 0) + '亿</b> · 相对20日均量 <b style="color:' + heatColor + '">' + f(bh.avg_amt_ratio, 2) + '×</b></span>' +
        '</div>' +
        table([{ t: '标杆趋势股' }, { t: '成交额', a: 'r' }, { t: '额/20日均', a: 'r' }, { t: '近5日日均', a: 'r' }, { t: '结构', a: 'c' }], bhRows) +
        (zbTxt ? '<div class="note" style="margin-top:10px">💡 ' + zbTxt + '</div>' : '') +
        '<div class="note" style="margin-top:6px">市场热度以标杆趋势股【交易额度（成交额）】为核心判据：放量+多头=抱团可参与，缩量+破位=退潮需严格止损。</div>';
      h += '<div style="margin-top:16px">' + card('🔥 市场热度 · 标杆趋势股参照系', bhBody,
        '华电辽能 / 圣阳股份 / 正丹股份 / 沃尔核材 / 寒武纪 / 拓维信息 / 光启技术') + '</div>';
    }

    /* 涨停形态图谱 · 次日规律 */
    var ps = mk.pattern_stats || {};
    var pt = mk.pattern_today || {};
    if (ps && Object.keys(ps).length) {
      var SHAPE_COLOR = { "一字板": "#2f80ed", "地天板": "#9b51e0", "T字板": "#d4a017", "烂板": "#eb5757", "换手板": "#27ae60" };
      var SHAPE_ORDER = ["一字板", "地天板", "T字板", "烂板", "换手板"];
      function shpChip(s) {
        var col = SHAPE_COLOR[s] || C.gray;
        return '<span style="display:inline-block;padding:1px 8px;border-radius:9px;font-size:12px;font-weight:700;color:' + col + ';border:1px solid ' + col + '">' + E(s) + '</span>';
      }
      var pRows = SHAPE_ORDER.filter(function (s) { return ps[s]; }).map(function (shp) {
        var p = ps[shp];
        var nc = p.avg_next_close, no = p.avg_next_open;
        var ncCol = nc >= 0 ? C.up : C.down;
        var noCol = no >= 0 ? C.up : C.down;
        return '<tr>' +
          '<td class="name">' + shpChip(shp) + '</td>' +
          '<td class="r num">' + p.samples + '</td>' +
          '<td class="r num" style="color:' + noCol + '">' + (no >= 0 ? '+' : '') + f(no, 2) + '%</td>' +
          '<td class="r num" style="color:' + ncCol + '">' + (nc >= 0 ? '+' : '') + f(nc, 2) + '%</td>' +
          '<td class="r num" style="color:' + C.up + '">' + f(p.limitup_rate, 1) + '%</td>' +
          '<td class="r num" style="color:' + C.down + '">' + f(p.green_rate, 0) + '%</td>' +
          '<td class="r num" style="color:' + C.up + '">' + f(p.strong_rate, 1) + '%</td>' +
          '</tr>';
      });
      var ptChips = SHAPE_ORDER.filter(function (s) { return pt[s]; }).map(function (s) {
        return shpChip(s) + ' <b style="color:var(--text)">' + pt[s] + '</b>';
      }).join(' &nbsp; ');
      var pBody = (ptChips ? '<div style="margin-bottom:10px">今日形态分布：' + ptChips + '</div>' : '') +
        table([
          { t: '形态' }, { t: '样本', a: 'r' }, { t: '次日均开', a: 'r' }, { t: '次日均收', a: 'r' },
          { t: '次日涨停率', a: 'r' }, { t: '次日收绿率', a: 'r' }, { t: '次日≥3%强延续', a: 'r' }
        ], pRows) +
        '<div class="note" style="margin-top:8px">形态规律基于近 120 日历史涨停次日表现重建：<b style="color:' + C.up + '">次日均收为正</b>=易连板延续，<b style="color:' + C.down + '">收绿率高</b>=分歧/派发需谨慎。一字/地天最强，烂板分歧最大。</div>';
      h += '<div style="margin-top:16px">' + card('🧬 涨停形态图谱 · 次日规律', pBody,
        '基于日K重建的涨停封板形态分类与各自历史次日胜率') + '</div>';
    }

    /* 财经要闻 */
    var NW = D.news || {};
    var nwItems = NW.items || [];
    if (nwItems.length) {
      var nwRows = nwItems.slice(0, 14).map(function (it) {
        var sc = it.score || 0;
        var scColor = sc >= 6 ? C.up : sc >= 3 ? C.gold : C.gray;
        var title = '<div style="font-weight:600;color:var(--text)">' + E(it.title) + '</div>' +
          (it.summary ? '<div style="font-size:12px;color:var(--muted);margin-top:2px;white-space:normal;max-width:640px">' + E(it.summary) + '</div>' : '');
        return '<tr><td class="muted" style="white-space:nowrap;width:118px">' + E((it.date || '').slice(5, 16)) + '</td>' +
          '<td class="name">' + title + '</td>' +
          '<td class="muted" style="white-space:nowrap;width:74px">' + E(it.source || '') + '</td>' +
          '<td class="c" style="width:66px"><span style="display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;font-weight:700;color:#fff;background:' + scColor + '">相关 ' + sc + '</span></td></tr>';
      });
      h += '<div style="margin-top:16px">' + card('📰 财经要闻（近期，按对 A 股相关性排序）',
        '<div style="font-size:12px;color:var(--muted);margin-bottom:8px">' + E(NW.summary || '') + '</div>' +
        table([{ t: '时间', a: 'c' }, { t: '标题' }, { t: '来源', a: 'c' }, { t: '相关性', a: 'c' }], nwRows, { scroll: true }),
        '来源：东方财富 / 同花顺 7×24 快讯，经相关性打分过滤噪音') + '</div>';
    }
    return h;
  }

  function md2html(md) {
    var lines = (md || '').split('\n'), out = [], sec = [];
    // 收集当前分节的内容，遇到 ### / ## 时 flush 上一节
    function flush() {
      if (!sec.length) return;
      out.push('<div class="push-section">' + sec.join('') + '</div>');
      sec = [];
    }
    for (var i = 0; i < lines.length; i++) {
      var t = lines[i], raw = t.replace(/^\s+/, '');
      if (/^### /.test(t)) { flush(); out.push('<div class="push-section"><b>' + E(t.slice(4)) + '</b><div>'); sec = []; }
      else if (/^## /.test(t)) { flush(); out.push('<div class="push-section"><b style="font-size:15px">' + E(t.slice(3)) + '</b><div>'); sec = []; }
      else if (/^---+$/.test(t)) { flush(); out.push('<hr style="border:none;border-top:1px solid var(--border);margin:7px 0">'); }
      else if (/^>\s?/.test(t)) { sec.push('<div style="margin:2px 0;padding-left:8px;border-left:3px solid var(--accent);color:var(--muted);white-space:pre-wrap;font-size:12px">' + E(t.replace(/^>\s?/, '')) + '</div>'); }
      else if (/^[-*]\s/.test(raw)) {
        // 推荐标的行 → 渲染为标签
        var m = raw.match(/^\d+\.\s+(.+)$/);
        if (m) { sec.push('<div class="push-rec-item">' + parseRecLine(m[1]) + '</div>'); }
        else { sec.push('<div style="margin:1px 0 1px 16px;white-space:pre-wrap;font-size:12.5px;color:var(--text-2)">• ' + E(t.trim().slice(2)) + '</div>'); }
      }
      else if (/^\d+\.\s/.test(raw)) { sec.push('<div style="margin:1px 0 1px 16px;white-space:pre-wrap">' + E(t.trim()) + '</div>'); }
      else if (t.trim() === '') { sec.push('<div style="height:3px"></div>'); }
      else { sec.push('<div style="margin:1px 0;white-space:pre-wrap;font-size:12.5px;color:var(--text-2)">' + E(t) + '</div>'); }
    }
    flush();
    return out.map(function (h) {
      return h.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
              .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener" style="color:var(--accent)">$1</a>');
    }).join('');
  }

  // 解析推荐行 "名称(板) · 分数 · 晋级% | 简因" 为内联标签
  function parseRecLine(line) {
    var html = line
      .replace(/\*\*(.+?)\*\*/g, '<b style="color:var(--text)">$1</b>')
      .replace(/·/g, '<span style="color:var(--muted);margin:0 3px">·</span>')
      .replace(/(买入价值\*\*\d+分\*\*)/g, '<span style="color:' + C.up + '">$1</span>')
      .replace(/(晋级\*\*[\d.]+%\*\*)/g, '<span style="color:' + C.gold + '">$1</span>');
    return html;
  }

  function pushCard() {
    var LP = D.last_push || {};
    var LABELS = {
      close: '📊 收盘后复盘', preauction: '🔔 竞价前观察',
      auction: '⚡ 竞价强度确认', close_again: '🌙 复盘补发',
      weekend: '🗓️ 周末发酵', anomaly: '🚨 盘中异动提醒'
    };
    function blk(mode, label) {
      var p = LP[mode];
      if (!p) return '';
      return '<div style="margin-top:10px"><b>' + label + '</b> <span class="faint">' + E(p.ts || '') + '</span></div>' +
             '<div style="font-size:13px;line-height:1.65">' + md2html(p.text || '') + '</div>';
    }
    var modes = ['preauction', 'auction', 'close', 'close_again', 'weekend', 'anomaly']
      .filter(function (m) { return LP[m]; });
    var inner = modes.length
      ? modes.map(function (m) { return blk(m, LABELS[m] || m); }).join('')
      : '<div class="m">尚未生成推送。配置 config/notify.json 的微信/Telegram/邮件后，每日 <b>盘前(08:50)</b>、<b>收盘(15:20)</b> 与 <b>复盘补发(20:00)</b> 自动推送；竞价确认与盘中异动可随时触发；即便未配置通道，此处也会留存最近一次推送内容。</div>';
    return card('📨 消息推送记录', '<div>' + inner +
      '</div><div class="m" style="margin-top:10px;color:var(--muted)">通道配置：config/notify.json（微信优先：企业微信群机器人 / ServerChan / PushPlus；亦支持 Telegram、SMTP 邮件）</div>',
      '每日推送：盘前观察 + 竞价确认 + 收盘复盘 + 复盘补发(20:00)；盘中异动可随时触发');
  }

  function indexTable() {
    var idx = (D.market || {}).indexes || [];
    if (!idx.length) return '<div class="empty">指数快照抓取失败（不影响其余分析）</div>';
    return table([{ t: '指数' }, { t: '点位', a: 'r' }, { t: '涨跌幅', a: 'r' }, { t: '成交额', a: 'r' }, { t: '涨/跌家数', a: 'r' }],
      idx.map(function (x) {
        return '<tr><td class="name">' + E(x.name) + '</td><td class="r num">' + f(x.price) +
          '</td><td class="r num">' + sign(x.pct) + '%</td><td class="r num">' + yi(x.amount) +
          '</td><td class="r num faint">' + n2(x.up) + ' / ' + n2(x.down) + '</td></tr>';
      }));
  }

  /* ============ 视图 2：涨停梯队 ============ */
  function viewLadder() {
    var lad = D.ladder || {}, lus = D.limit_ups || [];
    var lvs = Object.keys(lad).map(Number).sort(function (a, b) { return b - a; });
    var h = '';

    /* 金字塔 + 梯队 */
    var pyr = lvs.map(function (lv) { return { lv: lv, n: lad[lv].length }; });
    var ladHtml = '<div class="ladder">';
    lvs.forEach(function (lv) {
      var arr = lad[lv].slice().sort(function (a, b) { return (b.quality || 0) - (a.quality || 0); });
      var t = Math.min(6, lv);
      /* 主题感知：连板色阶走 CSS 变量，浅色/科技深色两套主题下都保证对比度 */
      var bg = 'var(--lb' + t + '-bg)';
      var fg = 'var(--lb' + t + '-fg)';
      ladHtml += '<div class="lrow"><div class="lv" style="background:' + bg + ';color:' + fg + '">' +
        lv + ' 板 · ' + arr.length + '</div><div class="items">';
      ladHtml += arr.map(function (x) {
        var pc = x.p_continue;
        var col = pc === null || pc === undefined ? 'var(--faint)' : (pc >= 35 ? C.up : pc >= 20 ? C.gold : C.gray);
        return '<span class="lchip" title="' + E(x.industry || '') + ' · 质量分 ' + f(x.quality, 0) +
          ' · 换手 ' + f(x.turn, 1) + '% · 流通 ' + yi(x.float_mv) + '">' +
          (x.yizi ? '<span style="color:' + C.up + ';font-size:10px">一</span>' : '') +
          stk(x.code, x.name) + '<span class="q">' + f(x.quality, 0) + '</span>' +
          '<span style="color:' + col + ';font-size:10.5px;font-weight:700">' +
          (pc === null || pc === undefined ? '' : f(pc, 0) + '%') + '</span></span>';
      }).join('');
      ladHtml += '</div></div>';
    });
    ladHtml += '</div><div class="legend"><span>chip 内数字依次为：<b>封板质量分</b> / <b>模型测算次日续板概率</b></span>' +
      '<span><i style="background:' + C.up + '"></i>「一」= 一字板</span></div>';

    h += '<div class="split">' +
      card('🏔️ 连板高度分布', CH.svgPyramid(pyr, { w: 320 }) +
        '<div class="note" style="margin-top:10px">共 <b>' + lus.length + '</b> 只涨停，其中连板 <b>' +
        lus.filter(function (x) { return x.streak >= 2; }).length + '</b> 只，最高 <b>' +
        (lvs[0] || 0) + '</b> 板。梯队完整（各高度均有承接）说明情绪结构健康；出现断层则次日空间容易坍塌。</div>',
        '每层为该连板高度的个股数量') +
      card('🪜 涨停梯队一览', ladHtml, '按连板高度分层，层内按封板质量排序') +
      '</div>';

    /* 明细表 */
    var rows = lus.slice().sort(function (a, b) {
      return (b.streak - a.streak) || (b.quality - a.quality);
    }).map(function (x) {
      var pc = (D.break_risk || []).filter(function (r) { return r.code === x.code; })[0];
      return '<tr><td class="code">' + E(x.code) + '</td><td class="name">' + stk(x.code, x.name) +
        (x.yizi ? ' <span class="bd lb3">一字</span>' : '') + '</td>' +
        '<td class="c">' + lbBadge(x.streak) + '</td>' +
        '<td class="muted">' + E(x.industry || '—') + '</td>' +
        '<td class="r num up">+' + f(x.pct) + '%</td>' +
        '<td class="r num">' + f(x.turn, 1) + '%</td>' +
        '<td class="r num">' + yi(x.float_mv) + '</td>' +
        '<td class="c faint">' + E(x.seal_time || '—') + '</td>' +
        '<td class="c">' + (x.zb_count ? '<span class="bd warn">' + x.zb_count + '</span>' : '<span class="faint">0</span>') + '</td>' +
        '<td class="r num">' + qBar(x.quality) + ' <b>' + f(x.quality, 0) + '</b></td>' +
        '<td class="r num">' + sign(x.gain20, 0) + '%</td>' +
        '<td class="r num">' + (pc ? '<b style="color:' + (pc.p_continue >= 35 ? C.up : pc.p_continue >= 20 ? C.gold : C.gray) + '">' +
          f(pc.p_continue, 0) + '%</b>' : '—') + '</td></tr>';
    });
    h += card('📋 当日涨停明细（' + lus.length + ' 只）', table([
      { t: '代码' }, { t: '名称' }, { t: '连板', a: 'c' }, { t: '行业' }, { t: '涨幅', a: 'r' },
      { t: '换手', a: 'r' }, { t: '流通市值', a: 'r' }, { t: '首封', a: 'c' }, { t: '炸板', a: 'c' },
      { t: '封板质量', a: 'r' }, { t: '20日涨幅', a: 'r' }, { t: '续板概率', a: 'r' }
    ], rows, { scroll: true }),
      '封板质量 = 首封时间 + 炸板次数 + 封单强度 + 换手 综合评分');
    return h;
  }

  /* ============ 视图 3：板块热力 ============ */
  function viewSectors() {
    var sec = D.sectors || {}, inds = sec.industry || [], cons = sec.concept || [];
    var h = '';
    function bars(list, n) {
      return CH.svgHBar(list.slice(0, n).map(function (s) {
        return {
          l: s.name.length > 6 ? s.name.slice(0, 6) : s.name, v: Math.round(s.strength),
          sub: s.zt + '涨停/最高' + s.max_lb + '板',
          c: s.tier === '主线' ? C.up : s.tier === '支线' ? C.gold : C.gray
        };
      }), { w: 470, padL: 74 });
    }
    h += '<div class="grid g2">' +
      card('🔥 行业板块强度 Top' + Math.min(14, inds.length), bars(inds, 14) +
        '<div class="legend"><span><i style="background:' + C.up + '"></i>主线</span>' +
        '<span><i style="background:' + C.gold + '"></i>支线</span>' +
        '<span><i style="background:' + C.gray + '"></i>零星</span></div>',
        '强度 = 涨停家数30% + 连板家数22% + 高度20% + 板块涨幅16% + 主力净额12%') +
      card('💡 概念题材强度 Top' + Math.min(14, cons.length),
        cons.length ? (bars(cons, 14) +
          '<div class="legend"><span><i style="background:' + C.up + '"></i>主线</span>' +
          '<span><i style="background:' + C.gold + '"></i>支线</span>' +
          '<span><i style="background:' + C.gray + '"></i>零星</span></div>')
        : '<div class="empty">概念题材成分未抓取（盘后快照未含板块成分；重跑 <code>python pipeline/fetch.py</code> 补全板块库后，题材维度会自动出现）</div>',
        '已剔除「昨日涨停/融资融券/沪深股通」等非题材噪声概念') +
      '</div>';

    /* 板块卡片 */
    function secCards(list, title, hint) {
      var pick = list.filter(function (s) { return s.tier !== '零星'; }).slice(0, 12);
      if (!pick.length) pick = list.slice(0, 6);
      if (!pick.length) return '';
      var body = '<div class="grid g3">' + pick.map(function (s) {
        return '<div class="sec"><div class="hd"><b>' + E(s.name) + '</b>' + tierBadge(s.tier) +
          '<span style="margin-left:auto;font-size:16px;font-weight:750;color:' +
          (s.strength >= 60 ? C.up : s.strength >= 40 ? C.gold : C.gray) + '">' + f(s.strength, 0) + '</span></div>' +
          '<div class="metrics"><span>涨停 <b style="color:' + C.up + '">' + s.zt + '</b></span>' +
          '<span>连板 <b>' + s.lb + '</b></span><span>最高 <b>' + s.max_lb + '</b> 板</span>' +
          '<span>均质 <b>' + f(s.avg_quality, 0) + '</b></span>' +
          (s.pct === null || s.pct === undefined ? '' : '<span>板块 ' + sign(s.pct) + '%</span>') +
          (s.main_net ? '<span>主力 ' + sign(s.main_net / 1e8, 1) + '亿</span>' : '') + '</div>' +
          '<div class="chips">' + (s.top || []).map(function (t) {
            return '<span class="chip">' + stk(t.code || '', t.name) + ' <b>' + t.streak + '板</b></span>';
          }).join('') + '</div></div>';
      }).join('') + '</div>';
      return card(title, body, hint);
    }
    h += secCards(inds, '🏭 行业板块画像（主线 / 支线）', '主线 = 涨停≥5 且有连板；支线 = 涨停≥3 或（涨停≥2 且高度≥3）');
    h += secCards(cons, '🧬 概念题材画像', '同一只票可同属多个题材，交叉出现的题材是资金真正的合力方向');

    /* 连板梯队持续性 */
    var L = D.ladder_history || {};
    if (L && L.matrix) {
      var lrows = Object.keys(L.matrix).map(function (k) { return { l: k + '板', vals: L.matrix[k] }; });
      h += card('🪜 连板梯队持续性（近 ' + L.dates.length + ' 日）',
        CH.svgHeat({ rows: lrows, cols: L.dates, max: L.max }),
        '各高度涨停家数。较高连板(3-5板)持续有承接=情绪结构健康；高位行突然归零=退潮信号');
    }

    /* 板块轮动 · 主线持续性 */
    var ROT = D.rotation || [];
    if (ROT.length) {
      var nd = (ROT[0].zt_days || []).length || 5;
      var rotRows = ROT.map(function (r) {
        var tColor = r.trend === '升温' ? C.up : r.trend === '降温' ? C.down : C.gray;
        var tags = '';
        if (r.is_new) tags += ' <span class="bd ok">新题材</span>';
        if (r.persistent) tags += ' <span class="bd mid">持续主线</span>';
        return '<tr><td class="name">' + E(r.name) + tags + '</td>' +
          '<td class="r num"><b>' + (r.today || 0) + '</b> 家涨停</td>' +
          '<td class="c">' + trendBadge(r.trend) + '</td>' +
          '<td style="min-width:104px">' + spark(r.zt_days, tColor) + '</td></tr>';
      });
      h += card('🔄 板块轮动 · 主线持续性（近 ' + nd + ' 日）',
        table([{ t: '行业' }, { t: '今日涨停', a: 'r' }, { t: '趋势', a: 'c' }, { t: '近 ' + nd + ' 日涨停家数' }], rotRows, { scroll: true }) +
        '<div class="note" style="margin-top:8px">升温=板块涨停家数较 ' + nd + ' 日前增加；降温=减少；' +
        '持续主线=近 ' + nd + ' 日有 ≥2 只涨停的天数 ≥3；新题材=此前无涨停、今日首次爆发。决定题材的容错与仓位。</div>',
        '判断主线是升温接力 / 降温兑现 / 一日游');
    }

    /* 板块接力 · 主副线切换（断板→接力检测）+ 主副线分类 */
    var RL = D.sector_relay;
    if (RL && RL.available) {
      var phColor = RL.phase.indexOf('接力切换') >= 0 ? C.up : (RL.phase.indexOf('退潮') >= 0 ? C.down : C.gold);
      var brk = (RL.broken_list || []).map(function (b) {
        return '<span class="bd danger" title="峰值 ' + b.peak_zt + ' 只@' + b.peak_date + ' → 现 ' + b.latest_zt + ' 只涨停">' + E(b.name) + ' 退潮</span>';
      }).join(' ');
      var rly = (RL.relay || []).map(function (x) {
        var kc = x.kind === '新崛起' ? C.up : C.gold;
        var cer = x.certainty != null ? x.certainty : 0;
        var tag = (x.toward_main ? ' <span class="bd" style="border-color:' + C.purple + ';color:' + C.purple + ';font-size:10px;padding:0 4px">⬆晋级主线</span>' : '') +
                  (x.persistent ? ' <span class="bd" style="border-color:' + C.gold + ';color:' + C.gold + ';font-size:10px;padding:0 4px">持续</span>' : '');
        return '<div class="kv" style="margin:5px 0"><span class="bd" style="border-color:' + kc + ';color:' + kc + ';font-size:11px;padding:0 6px">' + E(x.kind) + '</span> <b>' + E(x.name) + '</b> · 今日 ' + x.latest_zt + ' 只涨停（7日前 ' + x.prev7_zt + '，+' + x.delta + '）' + tag +
          '<span class="note" style="margin-left:6px">接力确定性 ' + cer + '%</span></div>';
      }).join('');
      var ml = (RL.mainline || []).slice(0, 4).map(function (x) {
        return '<span class="bd" style="border-color:' + C.up + ';color:' + C.up + ';font-size:11px;padding:0 6px">主线</span> <b>' + E(x.name) + '</b>（' + x.zt + '板·' + x.max_lb + '连板·' + x.trend + '）';
      }).join(' ｜ ');
      var sl = (RL.sublines || []).slice(0, 4).map(function (x) {
        return '<span class="bd" style="border-color:' + C.gold + ';color:' + C.gold + ';font-size:11px;padding:0 6px">支线</span> <b>' + E(x.name) + '</b>（' + x.zt + '板·' + x.trend + '）';
      }).join(' ｜ ');
      var rlBody = '<div style="margin-bottom:8px"><span class="bd" style="background:' + phColor + '22;color:' + phColor + ';border-color:' + phColor + '">' + E(RL.phase) + (RL.relay_cer ? ' · 接力确定性 ' + RL.relay_cer + '%' : '') + '</span></div>' +
        (brk ? '<div style="margin:6px 0"><b style="color:var(--muted)">退潮旧主线：</b>' + brk + '</div>' : '') +
        (rly ? '<div style="margin:6px 0"><b style="color:var(--muted)">接力方向：</b>' + rly + '</div>' : '<div class="note">暂无明确接力方向，市场处于混沌轮动</div>') +
        (ml ? '<div style="margin:6px 0"><b style="color:var(--muted)">当前主线：</b>' + ml + '</div>' : '') +
        (sl ? '<div style="margin:6px 0"><b style="color:var(--muted)">支线轮动：</b>' + sl + '</div>' : '') +
        (RL.leader ? '<div class="note" style="margin-top:8px">当前领涨板块：<b>' + E(RL.leader.name) + '</b>（' + RL.leader.zt + ' 只涨停）</div>' : '');
      h += card('🔗 板块接力 · 主副线切换（近 ' + RL.window_days + ' 日）', rlBody,
        '旧主线涨停家数崩塌（断板退潮）后，往往有另一条板块从低位崛起承接——经典“断板→接力”规律（例：2026-03 电力断板后医药接力）；接力确定性越高，新方向越可信');
    }
    return h;
  }

  /* ============ 视图 4：断板风险 ============ */
  function viewRisk() {
    var risks = D.break_risk || [], stats = D.streak_stats || {};
    var h = '';
    var lvs = Object.keys(stats).map(Number).sort(function (a, b) { return a - b; })
      .filter(function (k) { return (stats[k].samples || 0) >= 5 && k <= 8; });
    var statBar = CH.svgBar(lvs.map(function (k) {
      return { l: k + '板', v: Math.round(stats[k].promote_rate), c: k >= 4 ? C.danger : k >= 2 ? C.warn : C.blue };
    }), { w: 440, fmt: function (v) { return v + '%'; } });
    var statTbl = table([{ t: '当前高度' }, { t: '样本', a: 'r' }, { t: '次日晋级率', a: 'r' },
      { t: '次日平均开盘', a: 'r' }, { t: '次日平均收盘', a: 'r' }, { t: '收绿概率', a: 'r' }, { t: '跌停概率', a: 'r' }],
      lvs.map(function (k) {
        var s = stats[k];
        return '<tr><td class="name">' + k + ' 连板</td><td class="r num faint">' + s.samples +
          '</td><td class="r num"><b style="color:' + (s.promote_rate >= 30 ? C.up : s.promote_rate >= 15 ? C.gold : C.down) + '">' +
          f(s.promote_rate, 1) + '%</b></td><td class="r num">' + sign(s.avg_open) + '%</td>' +
          '<td class="r num">' + sign(s.avg_close) + '%</td><td class="r num">' + f(s.green_rate, 0) + '%</td>' +
          '<td class="r num">' + f(s.limitdown_rate, 0) + '%</td></tr>';
      }));

    h += '<div class="split">' +
      card('📊 历史连板晋级率（本地行情库实证）', statBar +
        '<div class="note" style="margin-top:8px">用近 ' + n2((D.meta || {}).trade_days) +
        ' 个交易日的全市场日 K 自建涨停/连板库统计，<b>非估算值</b>。板数越高，晋级率呈指数衰减 —— 这正是「3–5 板后迎调整」的统计根源。</div>',
        '样本 ≥5 才纳入') +
      card('🎯 当日断板概率分布', CH.svgProbDist(risks, { w: 440 }) +
        '<div class="note" style="margin-top:8px">断板概率 ≥80% 的个股共 <b>' +
        risks.filter(function (r) { return r.p_break >= 80; }).length + '</b> 只，' +
        '其中 3 板以上高位股 <b>' + risks.filter(function (r) { return r.p_break >= 80 && r.streak >= 3; }).length +
        '</b> 只，是次日最主要的调整来源。</div>', '按 p_break 分桶') +
      '</div>';

    h += card('📐 各高度晋级基准表', statTbl, '模型的先验概率来自此表，再叠加个股因子修正');

    /* 风险明细 */
    var rows = risks.map(function (r, i) {
      var fac = (r.factors || []).map(function (x) {
        var w = Math.min(50, Math.abs(x.impact) * 55);
        var pos = x.impact >= 0;
        return '<div class="factor"><span class="fk">' + E(x.k) + '</span><span class="fv">' + E(x.v) + '</span>' +
          '<span class="fbar"><i style="' + (pos ? 'left:50%' : 'right:50%') + ';width:' + w.toFixed(0) + '%;background:' +
          (pos ? C.up : C.down) + '"></i></span><span class="fn">' + E(x.note) + '</span></div>';
      }).join('');
      return '<tr><td class="code">' + E(r.code) + '</td><td class="name">' + stk(r.code, r.name) + '</td>' +
        '<td class="c">' + lbBadge(r.streak) + '</td>' +
        '<td class="muted">' + E(r.industry || '—') + '</td>' +
        '<td class="r num">' + f(r.quality, 0) + '</td>' +
        '<td class="r num faint">' + f(r.base_rate, 1) + '%</td>' +
        '<td class="r num"><b style="color:' + (r.p_continue >= 35 ? C.up : r.p_continue >= 20 ? C.gold : C.gray) + '">' +
        f(r.p_continue, 1) + '%</b></td>' +
        '<td class="r num"><b style="color:' + (r.p_break >= 86 ? C.danger : r.p_break >= 78 ? C.warn : C.down) + '">' +
        f(r.p_break, 1) + '%</b></td>' +
        '<td class="c"><span class="bd ' + (r.cls === 'danger' ? 'danger' : r.cls === 'warn' ? 'warn' : r.cls === 'mid' ? 'mid' : 'ok') +
        '">' + E(r.risk) + '</span></td>' +
        '<td style="white-space:normal;min-width:330px"><details class="more"><summary>因子分解</summary>' + fac + '</details></td></tr>';
    });
    h += card('⚠️ 个股断板概率与因子分解（' + risks.length + ' 只）', table([
      { t: '代码' }, { t: '名称' }, { t: '连板', a: 'c' }, { t: '行业' }, { t: '质量', a: 'r' },
      { t: '基准晋级', a: 'r' }, { t: '续板概率', a: 'r' }, { t: '断板概率', a: 'r' }, { t: '风险', a: 'c' }, { t: '归因' }
    ], rows, { scroll: true }),
      'p = sigmoid( logit(同高度基准晋级率) + Σ 因子权重 )，红条=利多续板，绿条=利空');
    return h;
  }

  /* ============ 视图 5：妖股基因 ============ */
  function viewDemon() {
    var dem = D.demons || [], tpl = D.demon_templates || [];
    var h = '';
    if (!dem.length) return card('🧬 妖股形态相似度', '<div class="empty">当日无可扫描标的</div>');

    /* Top3 雷达 */
    var top3 = dem.slice(0, 3);
    h += '<div class="grid g3">' + top3.map(function (d) {
      var sim = (d.similar || [])[0] || {};
      return '<div class="card"><h3>' + stk(d.code, d.name) + ' <span class="bd lb' + Math.min(6, d.streak) + '">' + d.streak +
        '板</span><span class="hint">妖股基因 ' + f(d.score, 1) + '</span></h3><div class="body">' +
        CH.svgRadar((d.traits || []).map(function (t) { return { l: t.k, v: t.v }; }),
          { w: 290, h: 262, color: d.score >= 65 ? C.up : C.blue }) +
        '<div class="note" style="margin-top:6px">形态分 <b>' + f(d.pattern, 1) + '</b> · 特质分 <b>' + f(d.trait, 1) +
        '</b> · 流通 <b>' + yi(d.float_mv) + '</b> · 换手 <b>' + f(d.turn, 1) + '%</b></div>' +
        (sim.name ? '<div class="note" style="margin-top:6px;padding-top:8px;border-top:1px dashed var(--border)">' +
          '形态最像 <b>' + E(sim.name) + '</b>（' + E(sim.start) + ' 启动，相似度 <b style="color:' + C.up + '">' +
          f(sim.sim, 1) + '%</b>，当时启动后最高 <b style="color:' + C.up + '">+' + f(sim.gain, 0) + '%</b>，' +
          sim.max_streak + ' 连板）</div>' : '') +
        '</div></div>';
    }).join('') + '</div>';

    /* 榜单 */
    var rows = dem.map(function (d) {
      var s = (d.similar || []).map(function (x) {
        return '<span class="chip">' + E(x.name) + ' <b>' + f(x.sim, 0) + '%</b> <span class="faint">+' +
          f(x.gain, 0) + '%</span></span>';
      }).join(' ');
      return '<tr><td class="code">' + E(d.code) + '</td><td class="name">' + stk(d.code, d.name) + '</td>' +
        '<td class="c">' + lbBadge(d.streak) + '</td><td class="muted">' + E(d.industry || '—') + '</td>' +
        '<td class="r num">' + qBar(d.score, d.score >= 65 ? C.purple : C.gray) + ' <b>' + f(d.score, 1) + '</b></td>' +
        '<td class="r num faint">' + f(d.pattern, 1) + '</td><td class="r num faint">' + f(d.trait, 1) + '</td>' +
        '<td class="r num">' + yi(d.float_mv) + '</td><td class="r num">' + f(d.turn, 1) + '%</td>' +
        '<td style="white-space:normal;min-width:280px">' + s + '</td></tr>';
    });
    h += card('🧬 妖股基因榜（' + dem.length + ' 只涨停股全扫描）', table([
      { t: '代码' }, { t: '名称' }, { t: '连板', a: 'c' }, { t: '行业' }, { t: '基因总分', a: 'r' },
      { t: '形态分', a: 'r' }, { t: '特质分', a: 'r' }, { t: '流通市值', a: 'r' }, { t: '换手', a: 'r' },
      { t: '历史形态最相似的妖股（相似度 / 当时涨幅）' }
    ], rows, { scroll: true }),
      '总分 = 形态相似度 55% + 妖股特质 45%；形态用 28 日「价格 Z 序列 + 量能 Z 序列 + 结构特征」做 Pearson 匹配');

    /* 模板库 */
    if (tpl.length) {
      var trs = tpl.map(function (t) {
        return '<tr><td class="code">' + E(t.code) + '</td><td class="name">' + E(t.name) + '</td>' +
          '<td class="c faint">' + E(t.start) + '</td><td class="c">' + lbBadge(t.max_streak) + '</td>' +
          '<td class="r num up">+' + f(t.gain, 0) + '%</td>' +
          '<td class="muted" style="white-space:normal">' + E(t.trigger || '') + '</td></tr>';
      });
      h += card('📚 历史妖股模板库（' + tpl.length + ' 个）', table([
        { t: '代码' }, { t: '名称' }, { t: '启动日', a: 'c' }, { t: '最高连板', a: 'c' },
        { t: '启动后最大涨幅', a: 'r' }, { t: '启动特征' }
      ], trs, { scroll: true }),
        '筛选条件：本地行情库内 最高连板 ≥5 且 启动后累计涨幅 ≥85%');
    }
    return h;
  }

  /* ============ 视图 5.5：妖股潜力 ============ */
  function viewYaogu() {
    var y = D.yaogu;
    if (!y || !y.ranked || !y.ranked.length)
      return card('⚡ 妖股潜力', '<div class="empty">当日实时涨停池数据缺失（可能休市或非交易时段）</div>');

    var ranked = y.ranked, h = '';
    var genAt = y.generated_at || '';

    /* 顶部概览卡 */
    var ladderS = (y.ladder && Object.keys(y.ladder).length)
      ? Object.keys(y.ladder).map(function (k) { return k + '板×' + y.ladder[k].length; }).join('  ')
      : '—';
    var conceptS = (y.concept_top && y.concept_top.length)
      ? y.concept_top.slice(0, 6).map(function (c) { return c.name + '(' + c.up + ')'; }).join('、')
      : '—';
    h += card('⚡ 妖股潜力榜（涨停池 · 按潜力分降序）',
      '<div class="note">数据日期：<b>' + E(y.date || '—') + '</b>（上一交易日 / 最近有完整涨停数据的交易日）｜ 当日涨停 <b>' + y.count + '</b> 只 ｜ 连板梯隊：' + E(ladderS) + '</div>' +
      '<div class="note">今日最强题材：' + E(conceptS) + '</div>' +
      '<div class="note faint">生成于 ' + E(genAt) +
      ' ｜ 评分 = 板块联动20 + 连板位置18 + 封单强度18 + 流通盘10 + 换手8 + 封板质量16 + 题材启动10（0~100）｜ 与「妖股基因」(K线形态)互补</div>');

    /* Top3 卡片：潜力分 + 核心指标 + 因子理由 */
    h += '<div class="grid g3">' + ranked.slice(0, 3).map(function (it, idx) {
      var m = it.meta || {};
      var col = it.score >= 75 ? C.up : (it.score >= 60 ? C.blue : C.gray);
      var reasons = (it.reasons || []).slice().sort(function (a, b) { return b[2] - a[2]; })
        .slice(0, 4).map(function (r) { return '<div class="note">· ' + E(r[0]) + '：' + E(r[1]) + '</div>'; }).join('');
      return '<div class="card"><h3>#' + (idx + 1) + ' ' + stk(it.code, it.name) +
        ' <span class="hint">潜力 ' + f(it.score, 0) + '</span></h3><div class="body">' +
        qBar(it.score, col) +
        '<div class="note">连板 <b>' + (m.lbc || 1) + '</b>板 ｜ 板块 <b>' + E(it.sector) + '</b>（同板块 ' + (m.sector_count || 1) + ' 只涨停）</div>' +
        '<div class="note">流通市值 <b>' + f(m.ltsz_yi || 0, 0) + '</b> 亿 ｜ 封单 <b>' + f(m.fund_yi || 0, 2) + '</b> 亿（流通盘 ' + f(m.ratio || 0, 2) + '%）</div>' +
        '<div class="note">换手 <b>' + f(m.hs || 0, 1) + '%</b> ｜ 封板 <b>' + E(m.fbt || '—') + '</b>' + ((m.zbc || 0) ? ' ｜ ⚠ 炸板 ' + m.zbc + ' 次' : '') + '</div>' +
        ((it.lhb) ? '<div class="note">🐉 龙虎榜：' + ((it.lhb.net_amt > 0) ? '净买' : '净卖') + f(Math.abs(it.lhb.net_amt || 0) / 1e8, 2) + '亿 ｜ 买方 ' + (it.lhb.buy_seat || 0) + ' 席' + (((it.lhb.explanation || '').indexOf('连续三个交易日') >= 0) ? ' ｜ 连板妖股特征' : '') + '</div>' : '') +
        '<div class="note" style="margin-top:6px;padding-top:6px;border-top:1px dashed var(--border)"><b>核心因子</b></div>' +
        reasons + '</div></div>';
    }).join('') + '</div>';

    /* 潜力榜表格（严格按评分降序） */
    var rows = ranked.map(function (it, idx) {
      var m = it.meta || {};
      var col = it.score >= 75 ? C.up : (it.score >= 60 ? C.blue : C.gray);
      var topReason = (it.reasons || []).slice().sort(function (a, b) { return b[2] - a[2]; })[0];
      return '<tr><td class="c rank">' + (idx + 1) + '</td>' +
        '<td class="code">' + E(it.code) + '</td>' +
        '<td class="name">' + stk(it.code, it.name) + '</td>' +
        '<td class="r num">' + qBar(it.score, col) + ' <b>' + f(it.score, 0) + '</b></td>' +
        '<td class="c">' + lbBadge(m.lbc || 1) + '</td>' +
        '<td class="muted">板块 <b>' + E(it.sector) + '</b><span class="faint">（同板块 ' + (m.sector_count || 1) + ' 只）</span></td>' +
        '<td class="r num">' + f(m.ltsz_yi || 0, 0) + '</td>' +
        '<td class="r num">' + f(m.fund_yi || 0, 2) + '<span class="faint">/' + f(m.ratio || 0, 2) + '%</span></td>' +
        '<td class="c">' + E(m.fbt || '—') + ((m.zbc || 0) ? '<span class="faint"> ⚠' + m.zbc + '</span>' : '') + '</td>' +
        '<td class="c muted">' + ((it.lhb) ? ((it.lhb.net_amt > 0 ? '净买' : '净卖') + f(Math.abs(it.lhb.net_amt || 0) / 1e8, 2) + '亿·买' + (it.lhb.buy_seat || 0) + '席') : '<span class="faint">—</span>') + '</td>' +
        '<td class="muted" style="white-space:normal;min-width:230px">' + E(topReason ? topReason[1] : '—') + '</td></tr>';
    });
    h += card('🏆 妖股潜力榜 Top ' + ranked.length + '（按潜力分降序）', table([
      { t: '#', a: 'c' }, { t: '代码' }, { t: '名称' }, { t: '潜力分', a: 'r' },
      { t: '连板', a: 'c' }, { t: '板块（同板块涨停数）', a: 'l' }, { t: '流通亿', a: 'r' },
      { t: '封单亿(/流通)', a: 'r' }, { t: '封板', a: 'c' }, { t: '龙虎榜', a: 'c' }, { t: '核心因子' }
    ], rows, { scroll: true }),
      '潜力分 = 板块联动20 + 连板位置18 + 封单强度18 + 流通盘10 + 换手8 + 封板质量16 + 题材启动10，+ 龙虎榜·游资合力（最高+14），0~100。⚡ 高分=站风口+早板强封单+适中流通盘+题材启动+游资合力，妖股早期特征最明显');

    /* 新晋首板 */
    if (y.fresh_boards && y.fresh_boards.length) {
      var fb = y.fresh_boards.slice(0, 24).map(function (x) {
        return '<span class="chip">' + stk(x.code, x.name) + '<span class="faint"> ' + E(x.sector || '') + '</span></span>';
      }).join(' ');
      h += card('🆕 新晋首板（' + y.fresh_boards.length + ' 只 · 明日观察能否晋级二板）',
        '<div style="line-height:2">' + fb + '</div>',
        '首板是妖股起点。明日若能晋级二板且仍站风口，潜力分将跳升——重点关注同板块有≥2只涨停者');
    }

    /* 涨停行业强度热力 */
    if (y.sector_top && y.sector_top.length) {
      var sr = y.sector_top.slice(0, 12).map(function (s) {
        return '<tr><td class="name">' + E(s.sector) + '</td><td class="r num">' +
          qBar(s.count, C.blue) + ' <b>' + s.count + '</b></td></tr>';
      }).join('');
      h += card('📊 涨停行业强度 Top ' + Math.min(12, y.sector_top.length), table([
        { t: '行业' }, { t: '涨停数', a: 'r' }
      ], sr, { scroll: true }), '行业涨停越多=题材风口越强，妖股多诞生于最强风口');
    }

    h += '<div class="card"><div class="body"><div class="note faint">⚠️ 妖股波动极大、风险极高，本榜仅作「规律量化 + 线索挖掘」参考，非投资建议。评分维度来自涨停池单接口，与「妖股基因」(K线形态相似度)互补。</div></div></div>';
    return h;
  }

  /* ============ 视图 6：当日推荐 ============ */
  /* ============ 视图 5.8：妖股双确认（妖股潜力 ∩ 妖股基因 交集筛选） ============ */
  var OVERLAP_DATA = null;
  var OVERLAP_STATE = { f: 'all', s: 'confirm' };
  function viewOverlap() {
    var dem = (D.demons || []).slice();
    var y = D.yaogu;
    var yRank = (y && y.ranked) || [];
    var ymap = {};
    yRank.forEach(function (it) { ymap[normCode(it.code)] = it; });
    var dmap = {};
    dem.forEach(function (d) { dmap[normCode(d.code)] = d; });

    /* 交集：同时出现在「妖股基因」与「妖股潜力」两榜的标的 */
    var inter = [];
    Object.keys(dmap).forEach(function (code) {
      var d = dmap[code];
      var yit = ymap[code];
      if (!yit) return;
      var ds = d.score || 0, ys = yit.score || 0;
      var confirm = Math.sqrt(Math.max(0, ds) * Math.max(0, ys)); /* 几何平均，奖励两者皆高 */
      inter.push({
        code: code,
        name: d.name || yit.name,
        streak: d.streak || (yit.meta && yit.meta.lbc) || 1,
        industry: d.industry || yit.sector || '—',
        demon: ds, yaogu: ys, confirm: confirm,
        dbl: (ds >= 65 && ys >= 60),
        ditem: d, yitem: yit,
        similar: d.similar || [],
        lhb: yit.lhb,
        reasons: yit.reasons || [],
        meta: yit.meta || {}
      });
    });
    OVERLAP_DATA = inter;

    if (!inter.length)
      return card('⚡🧬 妖股双确认', '<div class="empty">当日无同时登上「妖股基因」与「妖股潜力」两榜的标的（可能休市、非交易时段或两榜无重叠）</div>');

    var dblN = inter.filter(function (x) { return x.dbl; }).length;
    var h = '';
    h += card('⚡🧬 妖股双确认（交集筛选）',
      '<div class="note">🧬 妖股基因榜 <b>' + dem.length + '</b> 只 ｜ ⚡ 妖股潜力榜 <b>' + yRank.length +
      '</b> 只 ｜ 同时登上两榜的「双确认」标的 <b style="color:var(--up)">' + inter.length + '</b> 只（其中 ⭐双高 ' + dblN + ' 只）</div>' +
      '<div class="note faint">⚡ 妖股潜力=实时资金+题材（涨停池单接口） ｜ 🧬 妖股基因=历史K线形态相似度（本地K线库）。两者【同时高分】= 资金与形态共振，早期妖股确认度最高。</div>',
      '确认度 = √(基因分 × 潜力分) 的几何平均：任一维度塌方则确认度骤降，只有「资金+形态」共振才给高分');

    /* 筛选 / 排序工具栏 */
    h += '<div class="toolbar" id="ov-bar">' +
      '<span class="muted" style="font-weight:600">筛选：</span>' +
      ovChip('all', '全部交集', 'f') +
      ovChip('dbl', '⭐ 双高(基因≥65 & 潜力≥60)', 'f') +
      ovChip('yhi', '潜力≥75', 'f') +
      ovChip('dhi', '基因≥70', 'f') +
      '<span class="muted" style="font-weight:600;margin-left:10px">排序：</span>' +
      ovChip('confirm', '确认度', 's') +
      ovChip('y', '潜力分', 's') +
      ovChip('d', '基因分', 's') +
      '</div>';

    h += '<div id="ov-body">' + renderOverlapBody(inter, OVERLAP_STATE) + '</div>';
    return h;
  }

  function ovChip(v, label, kind) {
    var active = (kind === 'f' && OVERLAP_STATE.f === v) || (kind === 's' && OVERLAP_STATE.s === v);
    var st = active
      ? 'border-color:var(--accent);color:var(--accent);box-shadow:var(--glow-soft);background:var(--accent-bg);font-weight:700'
      : '';
    return '<button type="button" class="pat-chip ov-chip" data-kind="' + kind + '" data-v="' + E(v) +
      '" style="' + st + '">' + E(label) + '</button>';
  }

  function renderOverlapBody(inter, state) {
    var rows = inter.slice();
    if (state.f === 'dbl') rows = rows.filter(function (x) { return x.dbl; });
    else if (state.f === 'yhi') rows = rows.filter(function (x) { return x.yaogu >= 75; });
    else if (state.f === 'dhi') rows = rows.filter(function (x) { return x.demon >= 70; });
    rows.sort(function (a, b) {
      if (state.s === 'y') return b.yaogu - a.yaogu;
      if (state.s === 'd') return b.demon - a.demon;
      return b.confirm - a.confirm;
    });
    if (!rows.length) return '<div class="empty">当前筛选条件下无符合条件的双确认标的</div>';

    var flabel = state.f === 'dbl' ? '⭐ 双高' : state.f === 'yhi' ? '潜力≥75' : state.f === 'dhi' ? '基因≥70' : '全部交集';
    var sobj = state.s === 'y' ? '潜力分' : state.s === 'd' ? '基因分' : '确认度';

    /* Top3 双确认卡片 */
    var top = rows.slice(0, 3);
    var cards = '<div class="grid g3">' + top.map(function (x, idx) {
      var ccol = x.confirm >= 70 ? C.up : x.confirm >= 55 ? C.gold : C.gray;
      var dcol = x.demon >= 65 ? C.up : C.blue;
      var ycol = x.yaogu >= 75 ? C.up : (x.yaogu >= 60 ? C.blue : C.gray);
      var sim = (x.similar || [])[0] || {};
      var badge = x.dbl ? '<span class="bd lb4" style="margin-left:4px">⭐双高</span>' : '';
      var reasons = (x.reasons || []).slice().sort(function (a, b) { return b[2] - a[2]; }).slice(0, 3)
        .map(function (r) { return '<div class="note">· ' + E(r[0]) + '：' + E(r[1]) + '</div>'; }).join('');
      return '<div class="card"><h3>#' + (idx + 1) + ' ' + stk(x.code, x.name) + badge +
        ' <span class="hint">确认 ' + f(x.confirm, 0) + '</span></h3><div class="body">' +
        '<div class="kv" style="margin-bottom:6px">' +
        '<span>🧬 基因 <b style="color:' + dcol + '">' + f(x.demon, 0) + '</b></span>' +
        '<span>⚡ 潜力 <b style="color:' + ycol + '">' + f(x.yaogu, 0) + '</b></span>' +
        '<span>连板 <b>' + (x.streak || 1) + '</b>板</span>' +
        '</div>' +
        qBar(x.confirm, ccol) +
        '<div class="note">行业 <b>' + E(x.industry) + '</b></div>' +
        (sim.name ? '<div class="note" style="margin-top:6px;padding-top:6px;border-top:1px dashed var(--border)">形态最像 <b>' + E(sim.name) + '</b>（相似 ' + f(sim.sim, 0) + '% · 当时 +' + f(sim.gain, 0) + '%）</div>' : '') +
        (x.lhb ? '<div class="note">🐉 龙虎榜：' + ((x.lhb.net_amt > 0) ? '净买' : '净卖') + f(Math.abs(x.lhb.net_amt || 0) / 1e8, 2) + '亿·买' + (x.lhb.buy_seat || 0) + '席</div>' : '') +
        (reasons ? '<div class="note" style="margin-top:6px;padding-top:6px;border-top:1px dashed var(--border)"><b>核心因子</b></div>' + reasons : '') +
        '</div></div>';
    }).join('') + '</div>';

    /* 全量双确认表 */
    var trs = rows.map(function (x, i) {
      var ccol = x.confirm >= 70 ? C.up : x.confirm >= 55 ? C.gold : C.gray;
      var sim = (x.similar || [])[0] || {};
      var topReason = (x.reasons || []).slice().sort(function (a, b) { return b[2] - a[2]; })[0];
      return '<tr><td class="c rank">' + (i + 1) + '</td>' +
        '<td class="code">' + E(x.code) + '</td>' +
        '<td class="name">' + stk(x.code, x.name) + (x.dbl ? ' <span class="bd lb4" style="font-size:10px">⭐双高</span>' : '') + '</td>' +
        '<td class="c">' + lbBadge(x.streak || 1) + '</td>' +
        '<td class="muted">' + E(x.industry) + '</td>' +
        '<td class="r num">' + qBar(x.demon, x.demon >= 65 ? C.up : C.blue) + ' <b>' + f(x.demon, 0) + '</b></td>' +
        '<td class="r num">' + qBar(x.yaogu, x.yaogu >= 75 ? C.up : (x.yaogu >= 60 ? C.blue : C.gray)) + ' <b>' + f(x.yaogu, 0) + '</b></td>' +
        '<td class="r num">' + qBar(x.confirm, ccol) + ' <b style="color:' + ccol + '">' + f(x.confirm, 0) + '</b></td>' +
        '<td class="c">' + (x.lhb ? ((x.lhb.net_amt > 0 ? '净买' : '净卖') + f(Math.abs(x.lhb.net_amt || 0) / 1e8, 2) + '亿·买' + (x.lhb.buy_seat || 0) + '席') : '<span class="faint">—</span>') + '</td>' +
        '<td class="muted" style="white-space:normal;min-width:200px">' + (sim.name ? ('形态最像 <b>' + E(sim.name) + '</b> ' + f(sim.sim, 0) + '%') : '—') + '</td>' +
        '<td class="muted" style="white-space:normal;min-width:200px">' + E(topReason ? topReason[1] : '—') + '</td></tr>';
    });
    return cards + card('⚡🧬 双确认榜（' + rows.length + ' 只 · ' + flabel + ' · 按' + sobj + '降序）',
      table([
        { t: '#', a: 'c' }, { t: '代码' }, { t: '名称' }, { t: '连板', a: 'c' }, { t: '行业' },
        { t: '🧬 基因', a: 'r' }, { t: '⚡ 潜力', a: 'r' }, { t: '确认度', a: 'r' },
        { t: '龙虎榜', a: 'c' }, { t: '形态最像', a: 'l' }, { t: '核心因子', a: 'l' }
      ], trs, { scroll: true }),
      '确认度 = √(基因分 × 潜力分)。⭐双高 = 基因≥65 且 潜力≥60，是「资金+形态」共振的最强早期妖股信号');
  }

  function applyOverlapFilter(kind, v) {
    if (kind === 'f') OVERLAP_STATE.f = v; else if (kind === 's') OVERLAP_STATE.s = v;
    var body = document.getElementById('ov-body');
    if (body && OVERLAP_DATA) body.innerHTML = renderOverlapBody(OVERLAP_DATA, OVERLAP_STATE);
    var bar = document.getElementById('ov-bar');
    if (bar) [].forEach.call(bar.querySelectorAll('.ov-chip'), function (b) {
      var on = (b.dataset.kind === kind && b.dataset.v === v);
      b.style.borderColor = on ? 'var(--accent)' : '';
      b.style.color = on ? 'var(--accent)' : '';
      b.style.boxShadow = on ? 'var(--glow-soft)' : '';
      b.style.background = on ? 'var(--accent-bg)' : '';
      b.style.fontWeight = on ? '700' : '';
    });
    if (body) initCountUp(body);
  }

  function viewRec() {
    var R = D.recommend || {}, st = (D.market || {}).sentiment || {}, cy = (D.market || {}).cycle || {};
    var ML = R.mainline_map || {};
    var h = '';
    h += card('🧭 次日操作总纲', '<div class="grid g4" style="margin-bottom:14px">' +
      kpi('建议仓位', E(R.position || '—'), '基于情绪分 ' + f(st.score, 1)) +
      kpi('情绪状态', E(st.level || '—'), E(st.label || '')) +
      kpi('周期阶段', E(cy.phase || '—'), E((cy.desc || '').slice(0, 22))) +
      kpi('环境系数', f(R.env_k, 2), '推荐评分的市场折价因子') +
      '</div><ul class="list-tight">' +
      (R.strategies || []).map(function (s) { return '<li>' + E(s) + '</li>'; }).join('') + '</ul>',
      '仓位为组合层面建议，非单票');

    function group(title, arr, cls, hint) {
      if (!arr || !arr.length) return card(title, '<div class="empty">当日无符合条件的标的</div>', hint);
      var body = '<div class="grid g2">' + arr.map(function (it) {
        var scol = it.score >= 65 ? C.up : it.score >= 50 ? C.gold : C.gray;
        var sim = (it.similar || [])[0];
        var va = it.vol_anomaly || {};
        var vaBadge = (va.flag && va.flag !== '正常') ?
          '<span class="bd ' + (va.warn ? 'danger' : va.flag === '放量异动' ? 'warn' : 'gray') +
          '" title="' + E(va.note || '') + '">' + E(va.flag) + (va.ratio ? ' ×' + f(va.ratio, 1) : '') + '</span>' : '';
        var hc = it.hist_calib || {};
        var hcBadge = hc.level && hc.level !== '—' ?
          '<span class="bd ' + (hc.factor > 0.35 ? 'danger' : hc.factor > 0 ? 'mid' : 'ok') +
          '" title="历史研判：' + E(hc.note || hc.level) + '">' + E(hc.level.length > 4 ? hc.level.slice(0, 4) : hc.level) + '</span>' : '';
        var wcol = it.worth_score >= 60 ? C.up : it.worth_score >= 45 ? C.gold : C.gray;
        var t = it.trend_meta || null;
        var m = it.momentum_meta || null;
        var kvHtml = t
          ? '<div class="kv">'
            + '<span>收盘 <b>' + f(it.close) + '</b></span>'
            + '<span>行业 <b>' + E(it.industry || '—') + '</b></span>'
            + '<span>MA5 <b>' + f(t.ma5) + '</b></span>'
            + '<span>MA10 <b>' + f(t.ma10) + '</b></span>'
            + '<span>MA20 <b>' + f(t.ma20) + '</b></span>'
            + '<span>近5日 <b>' + (t.up_days) + '涨</b></span>'
            + '<span>近5日均涨 <b style="color:' + (t.avg_daily >= 3 ? C.up : C.gold) + '">' + f(t.avg_daily, 1) + '%</b></span>'
            + '<span>趋势带 <b style="color:' + (t.band === '主升强趋势' ? C.up : C.gold) + '">' + E(t.band) + '</b></span>'
            + '<span>偏离MA20 <b style="color:' + (t.momentum_pct >= 0 ? C.up : C.gold) + '">+' + f(t.momentum_pct, 1) + '%</b></span>'
            + '<span>量能 <b>' + f(t.vol_ratio, 1) + '倍</b></span>'
            + '<span>趋势分 <b style="color:' + scol + '">' + f(it.score, 1) + '</b></span>'
            + '<span>买入价值 <b style="color:' + wcol + '">' + f(it.worth_score, 0) + '</b></span>'
            + '</div>'
          : m
          ? '<div class="kv">'
            + '<span>收盘 <b>' + f(it.close) + '</b></span>'
            + '<span>行业 <b>' + E(it.industry || '—') + '</b></span>'
            + '<span>近12日涨停 <b style="color:' + C.up + '">' + m.lu_count + '</b></span>'
            + '<span>最高连板 <b>' + m.max_streak + '</b></span>'
            + '<span>余波(距涨停) <b>' + m.recency + '日</b></span>'
            + '<span>近10日涨幅 <b style="color:' + C.up + '">+' + f(m.gain10, 1) + '%</b></span>'
            + '<span>距高点回撤 <b style="color:' + (m.drawdown <= 10 ? C.up : C.gold) + '">' + f(m.drawdown, 1) + '%</b></span>'
            + '<span>MA20斜率 <b>+' + f(m.slope20, 1) + '%</b></span>'
            + '<span>趋势带 <b style="color:' + C.up + '">' + E(m.band) + '</b></span>'
            + '<span>量能 <b>' + f(m.vol_ratio, 1) + '倍</b></span>'
            + '<span>动量分 <b style="color:' + scol + '">' + f(it.score, 1) + '</b></span>'
            + '<span>买入价值 <b style="color:' + wcol + '">' + f(it.worth_score, 0) + '</b></span>'
            + '</div>'
          : '<div class="kv">'
            + '<span>收盘 <b>' + f(it.close) + '</b></span>'
            + '<span>行业 <b>' + E(it.industry || '—') + '</b></span>'
            + '<span>换手 <b>' + f(it.turn, 1) + '%</b></span>'
            + '<span>流通 <b>' + yi(it.float_mv) + '</b></span>'
            + '<span>质量 <b>' + f(it.quality, 0) + '</b></span>'
            + '<span>续板 <b style="color:' + (it.p_continue >= 35 ? C.up : C.gold) + '">' + f(it.p_continue, 0) + '%</b></span>'
            + '<span>买入价值 <b style="color:' + wcol + '">' + f(it.worth_score, 0) + '</b></span>'
            + '<span>妖股基因 <b>' + f(it.demon, 0) + '</b></span>'
            + '</div>';
        return '<div class="rec ' + cls + '"><div class="rh">' +
          '<span class="nm">' + stk(it.code, it.name) + '</span><span class="code faint">' + E(it.code) + '</span>' +
          lbBadge(it.streak) + tierBadge(it.sector_tier) + mlBadge(it) + relayBadge(it) + vaBadge + hcBadge +
          '<span class="sc" style="color:' + scol + '">' + f(it.score, 1) + '</span></div>' +
          '<div class="rb">' + kvHtml +
          '<ul>' + (it.reasons || []).map(function (r) { return '<li>' + E(r) + '</li>'; }).join('') + '</ul>' +
          ((it.concepts || []).length ? '<div class="chips" style="margin-top:8px;display:flex;flex-wrap:wrap;gap:5px">' +
            it.concepts.map(function (c) { return '<span class="chip">' + E(c) + '</span>'; }).join('') + '</div>' : '') +
          (sim ? '<div class="note" style="margin-top:8px">形态参照：<b>' + E(sim.name) + '</b> · 相似 ' +
            f(sim.sim, 0) + '% · 当时 +' + f(sim.gain, 0) + '%</div>' : '') +
          (hc.note ? '<div class="note" style="margin-top:8px;color:var(--muted)">历史研判：' + E(hc.note) + '</div>' : '') +
          '</div>' +
          '<div class="rf"><div class="t">风险提示</div><ul class="risk" style="list-style:none">' +
          (it.risks || []).map(function (r) { return '<li style="padding-left:0;color:var(--warn)">· ' + E(r) + '</li>'; }).join('') +
          '</ul></div></div>';
      }).join('') + '</div>';
      return card(title, body, hint);
    }

    h += group('⭐ 核心龙头（' + (R.core || []).length + '）', R.core, 'core',
      '连板≥2 且综合分≥60 且处于主线/支线板块');
    h += group('🔁 主线接力（' + (R.relay || []).length + '）', R.relay, '',
      '有板块合力的接力候选，性价比通常优于孤票');
    h += group('🌱 低位潜伏（' + (R.ambush || []).length + '）', R.ambush, '',
      '主线内首板，位置低、容错高，是退潮/启动期的主力打法');
    h += group('🚫 高位风险回避（' + (R.avoid || []).length + '）', R.avoid, 'avoid',
      '连板≥3 且断板概率≥86%，次日冲高回落概率大，列出仅为提示回避');
    h += group('📈 趋势向上 · 主升候选（' + (R.trend || []).length + '）', R.trend, 'trend',
      '均线多头 + 近5日日均涨幅≥2% + 至少4天收涨 + 横盘日≤1（剔除“技术多头实则横盘”的票）');
    h += group('⚡ 强动量 · 连板余波（' + (R.momentum || []).length + '）', R.momentum, 'momentum',
      '近期≥2次涨停/≥2连板基因 + 多头未破位 + 距高点回撤≤18%（接住“连板妖股型、今日非涨停”掉缝里的票，如风范股份）');

    /* 板块趋势推荐：把趋势向上的个股按行业聚类，找出趋势抱团最强的板块，并标注主线/龙头 */
    (function () {
      var ST = R.sector_trend || [];
      if (!ST.length) return;
      var body = '<div class="grid g2">' + ST.map(function (s) {
        var scol = s.strength >= 65 ? C.up : s.strength >= 50 ? C.gold : C.gray;
        var tierTag = s.tier === '主线'
          ? '<span class="bd lb4" title="' + (s.resonance ? '同时被涨停主线确认（双主线共振），主线信号最强' : '趋势强度居前，判定为主线板块') + '">' + (s.resonance ? '🔥双主线' : '主线') + '</span>'
          : '<span class="bd gray" title="趋势抱团但强度未达主线门槛">支线</span>';
        var leads = (s.leads || []).map(function (x) {
          var tag = x.is_leader
            ? '<span class="bd lb4" style="margin-left:4px">👑龙头</span>'
            : '<span class="faint" style="margin-left:4px">领涨</span>';
          return '<span class="chip">' + stk(x.code, x.name) +
            (x.band === '主升强趋势' ? ' · 主升' : '') + tag + '</span>';
        }).join('');
        return '<div class="rec sector"><div class="rh">' +
          '<span class="nm">' + E(s.sector) + '</span>' + tierTag +
          '<span class="sc" style="color:' + scol + '">' + f(s.strength, 1) + '</span></div>' +
          '<div class="rb"><div class="kv">' +
            '<span>趋势票 <b>' + s.trend_count + '只</b></span>' +
            '<span>均分 <b style="color:' + scol + '">' + f(s.avg_score, 1) + '</b></span>' +
            '<span>日均 <b style="color:' + C.up + '">' + f(s.avg_daily, 1) + '%</b></span>' +
            '<span>主升 <b>' + s.strong_count + '只</b></span>' +
          '</div><div class="chips" style="margin-top:8px;display:flex;flex-wrap:wrap;gap:5px">' + leads + '</div>' +
          '</div></div>';
      }).join('') + '</div>';
      h += card('🔥 板块趋势推荐（' + ST.length + '）', body,
        '把趋势向上的个股按行业聚类，找出「多只票悄悄走主升、却没几只涨停」的趋势抱团板块；🔥双主线=同时被涨停主线确认的强主线，👑=该主线板块的龙头');
    })();

    /* 全量评分表 */
    var rows = (R.all || []).map(function (it, i) {
      var wcol = it.worth_score >= 60 ? C.up : it.worth_score >= 45 ? C.gold : C.gray;
      return '<tr><td class="faint">' + (i + 1) + '</td><td class="code">' + E(it.code) + '</td>' +
        '<td class="name">' + stk(it.code, it.name) + qBadge(it) + mlBadge(it) + relayBadge(it) + '</td><td class="c">' + lbBadge(it.streak) + '</td>' +
        '<td class="muted">' + E(it.industry || '—') + '</td><td class="c">' + tierBadge(it.sector_tier) + '</td>' +
        '<td class="r num">' + f(it.quality, 0) + '</td><td class="r num">' + f(it.p_continue, 0) + '%</td>' +
        '<td class="r num">' + f(it.demon, 0) + '</td>' +
        '<td class="r num" style="color:' + wcol + '"><b>' + f(it.worth_score, 0) + '</b></td>' +
        '<td class="r num">' + qBar(it.score, it.score >= 60 ? C.up : C.gray) + ' <b>' + f(it.score, 1) + '</b></td>' +
        '<td class="c">' + (it.tag ? '<span class="bd ' + (it.tag === '高位风险' ? 'danger' : it.tag === '核心龙头' ? 'lb4' : 'ok') +
          '">' + E(it.tag) + '</span>' : '<span class="faint">—</span>') + '</td></tr>';
    });
    h += card('🗂️ 全量综合评分（前 ' + (R.all || []).length + ' 只）', table([
      { t: '#' }, { t: '代码' }, { t: '名称' }, { t: '连板', a: 'c' }, { t: '行业' }, { t: '板块层级', a: 'c' },
      { t: '封板质量', a: 'r' }, { t: '续板概率', a: 'r' }, { t: '妖股基因', a: 'r' }, { t: '买入价值', a: 'r' }, { t: '综合分', a: 'r' }, { t: '归类', a: 'c' }
    ], rows, { scroll: true }),
      '综合分 = 封板质量28% + 续板概率×1.6 26% + 板块强度20% + 妖股基因16% + 高度10%，再乘市场环境系数');
    return h;
  }

  /* ============ 视图 7：竞价判断 ============ */
  function viewAuction() {
    var A = D.auction || {}, sum = A.summary || {}, items = A.items || {};
    var lus = D.limit_ups || [];
    var arr = lus.filter(function (r) { return items[r.code]; }).map(function (r) {
      var a = items[r.code], o = {};
      for (var k in r) o[k] = r[k];
      for (var k2 in a) o[k2] = a[k2];
      return o;
    });
    var rmap = {}; (D.break_risk || []).forEach(function (r) { rmap[r.code] = r; });
    function patChip(p) {
      var m = { '一字板': 'lb4', 'T字板': 'lb2', '弱转强': 'gold', '强转弱': 'danger', '高开高走': 'blue', '换手板': 'gray' };
      return '<span class="bd ' + (m[p] || 'gray') + '">' + E(p) + '</span>';
    }
    var h = '';
    var pcount = {};
    arr.forEach(function (a) { if (a.pattern) pcount[a.pattern] = (pcount[a.pattern] || 0) + 1; });
    var patOrder = ['弱转强', '强转弱', 'T字板', '一字板', '换手板', '高开高走'];
    h += '<div class="toolbar" style="margin-bottom:14px"><span class="muted" style="font-weight:600">竞价形态筛选：</span>' +
      patOrder.filter(function (p) { return pcount[p]; }).map(function (p) {
        return '<button class="pat-chip" data-p="' + E(p) + '">' + E(p) + ' <b>' + pcount[p] + '</b></button>';
      }).join('') + '</div>';
    h += '<div class="grid g4" style="margin-bottom:14px">' +
      kpi('一字板', n2(sum.yizi), '开盘即封死，最强一致', sum.yizi > 0 ? 'up' : '') +
      kpi('弱转强', n2(sum.weak_strong), '低开/平开却涨停，次日乐观', sum.weak_strong > 0 ? 'gold' : '') +
      kpi('强转弱', n2(sum.strong_weak), '高开炸板/高换手，分歧风险', sum.strong_weak > 0 ? 'down' : '') +
      kpi('平均高开', (sum.avg_open_pct >= 0 ? '+' : '') + f(sum.avg_open_pct, 2) + '%', '涨停股竞价平均高开幅度') +
      '</div>';

    var bk = [['<55', 0, 55], ['55-65', 55, 65], ['65-75', 65, 75], ['75-85', 75, 85], ['≥85', 85, 101]];
    var bdata = bk.map(function (b) {
      return { l: b[0], v: arr.filter(function (a) { return (a.auction_score || 0) >= b[1] && (a.auction_score || 0) < b[2]; }).length,
        c: b[1] >= 75 ? C.up : (b[1] >= 65 ? C.gold : C.gray) };
    });
    var ddata = [['一字板', sum.yizi || 0, C.up], ['T字板', sum.t_board || 0, C.blue],
      ['弱转强', sum.weak_strong || 0, C.gold], ['强转弱', sum.strong_weak || 0, C.danger],
      ['高开高走', sum.high_open || 0, C.purple]].filter(function (d) { return d[1] > 0; });
    h += '<div class="split">' +
      card('🎚️ 竞价强度分布', CH.svgBar(bdata, { w: 440 }) +
        '<div class="note" style="margin-top:8px">竞价强度分综合「开盘定调 + 日内强弱 + 封板质量 + 弱转强/分歧消化」四维；≥75 为强势定调，次日承接更有保障。</div>',
        '按竞价强度分桶') +
      card('🧩 涨停竞价形态分布', ddata.length ? CH.svgDonut(ddata.map(function (d) {
        return { l: d[0], v: d[1], c: d[2] }; }), { w: 240 }) : '<div class="empty">无形态数据</div>',
        '一字 / T字 / 弱转强 / 强转弱 / 高开高走') + '</div>';

    var top = arr.slice().sort(function (a, b) { return (b.auction_score || 0) - (a.auction_score || 0); }).slice(0, 14);
    var rows = top.map(function (a) {
      var pc = rmap[a.code];
      return '<tr><td class="code">' + E(a.code) + '</td><td class="name">' + stk(a.code, a.name) + '</td>' +
        '<td class="c">' + lbBadge(a.streak) + '</td>' +
        '<td class="r num ' + (a.open_pct >= 0 ? 'up' : 'down') + '">' + (a.open_pct >= 0 ? '+' : '') + f(a.open_pct, 2) + '%</td>' +
        '<td class="r num ' + (a.intraday >= 0 ? 'up' : 'down') + '">' + (a.intraday >= 0 ? '+' : '') + f(a.intraday, 1) + '%</td>' +
        '<td class="c">' + patChip(a.pattern) + '</td>' +
        '<td class="r num">' + qBar(a.auction_score, a.auction_score >= 75 ? C.up : (a.auction_score >= 60 ? C.gold : C.gray)) +
        ' <b>' + f(a.auction_score, 0) + '</b></td>' +
        '<td class="r num">' + (pc ? f(pc.p_continue, 0) + '%' : '—') + '</td></tr>';
    });
    h += card('🏆 竞价强度榜 Top ' + top.length, table([
      { t: '代码' }, { t: '名称' }, { t: '连板', a: 'c' }, { t: '高开%', a: 'r' }, { t: '日内%', a: 'r' },
      { t: '形态', a: 'c' }, { t: '竞价强度', a: 'r' }, { t: '续板概率', a: 'r' }
    ], rows, { scroll: true }),
      '竞价强度高 = 开盘即被资金认可，次日断板概率更低');

    function grp(title, list, cls, hint) {
      if (!list.length) return card(title, '<div class="empty">当日无此类标的</div>', hint);
      var body = '<div class="grid g2">' + list.map(function (a) {
        var pc = rmap[a.code];
        return '<div class="rec ' + cls + '"><div class="rh"><span class="nm">' + stk(a.code, a.name) + '</span>' +
          '<span class="code faint">' + E(a.code) + '</span>' + lbBadge(a.streak) + patChip(a.pattern) +
          '<span class="sc" style="color:' + (a.auction_score >= 75 ? C.up : C.gold) + '">' + f(a.auction_score, 0) + '</span></div>' +
          '<div class="note">高开 ' + (a.open_pct >= 0 ? '+' : '') + f(a.open_pct, 2) + '% · 日内 ' +
          (a.intraday >= 0 ? '+' : '') + f(a.intraday, 1) + '% · 续板 ' + (pc ? f(pc.p_continue, 0) + '%' : '—') + '</div></div>';
      }).join('') + '</div>';
      return card(title, body, hint);
    }
    var weak = arr.filter(function (a) { return a.pattern === '弱转强'; }).sort(function (a, b) { return (b.auction_score || 0) - (a.auction_score || 0); });
    var strong = arr.filter(function (a) { return a.pattern === '强转弱'; }).sort(function (a, b) { return (b.auction_score || 0) - (a.auction_score || 0); });
    h += grp('🟢 弱转强 · 次日乐观信号（' + weak.length + '）', weak, 'core',
      '开盘被低估、盘中强势封板，资金从分歧转一致，次日溢价概率高');
    h += grp('🔴 强转弱 · 分歧风险（' + strong.length + '）', strong, 'avoid',
      '大幅高开后炸板/高换手，典型诱多回落，次日冲高回落概率大');

    var yz = arr.filter(function (a) { return a.yizi; });
    if (yz.length) {
      h += card('⚡ 一字板（' + yz.length + '）', '<div class="chips" style="display:flex;flex-wrap:wrap;gap:6px">' +
        yz.map(function (a) {
          return '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">' + stk(a.code, a.name) + ' <b>' + a.streak + '板</b></span>';
        }).join('') + '</div>', '一字无量封板 = 最强一致预期，但次日易一字加速或爆量开板');
    }

    /* 竞价量能异动预警 */
    var vaArr = arr.filter(function (a) { return a.vol_anomaly && a.vol_anomaly.flag && a.vol_anomaly.flag !== '正常'; });
    if (vaArr.length) {
      var vaRows = vaArr.slice().sort(function (a, b) {
        var order = { '放量异动': 0, '缩量': 1, '一字锁仓': 2 };
        return (order[a.vol_anomaly.flag] || 9) - (order[b.vol_anomaly.flag] || 9);
      }).map(function (a) {
        var va = a.vol_anomaly;
        var cls = va.warn ? 'danger' : va.flag === '放量异动' ? 'warn' : 'gray';
        return '<tr><td class="name">' + stk(a.code, a.name) + '</td><td class="code">' + E(a.code) + '</td>' +
          '<td class="c">' + lbBadge(a.streak) + '</td>' +
          '<td class="c"><span class="bd ' + cls + '">' + E(va.flag) + '</span></td>' +
          '<td class="r num">' + (va.ratio ? '×' + f(va.ratio, 1) : '—') + '</td>' +
          '<td class="muted" style="white-space:normal">' + E(va.note || '') + '</td></tr>';
      });
      h += card('📡 竞价量能异动预警（' + vaArr.length + '）', table([
        { t: '名称' }, { t: '代码' }, { t: '连板', a: 'c' }, { t: '信号', a: 'c' }, { t: '放量倍数', a: 'r' }, { t: '解读' }
      ], vaRows, { scroll: true }),
        '竞价成交额≈当日成交额×开盘参与度，并与该股自身 20 日中位数比较；放量≈资金主动进攻或派发，缩量≈分歧小/承接不足');
    }
    return h;
  }
  function initBackdrop() {
    var root = document.documentElement;
    var theme = (root && root.getAttribute('data-theme')) || 'tech';
    if (theme !== 'tech') return;
    if (typeof document.createElement !== 'function') return;
    var cv;
    try { cv = document.createElement('canvas'); if (!cv.getContext) return; }
    catch (e) { return; }
    cv.id = 'bgfx';
    cv.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:-2;pointer-events:none;opacity:.55';
    if (document.body) document.body.appendChild(cv); else return;
    var ctx = cv.getContext('2d');
    var W, H, pts = [], N = 64;
    function resize() { W = cv.width = (window.innerWidth || 1280); H = cv.height = (window.innerHeight || 800); }
    if (typeof window.addEventListener === 'function') window.addEventListener('resize', resize);
    resize();
    for (var i = 0; i < N; i++) pts.push({ x: Math.random() * W, y: Math.random() * H, vx: (Math.random() - .5) * .25, vy: (Math.random() - .5) * .25 });
    if (typeof requestAnimationFrame !== 'function') return;
    function tick() {
      ctx.clearRect(0, 0, W, H);
      ctx.strokeStyle = 'rgba(64,160,255,0.05)'; ctx.lineWidth = 1;
      for (var gx = 0; gx < W; gx += 46) { ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, H); ctx.stroke(); }
      for (var gy = 0; gy < H; gy += 46) { ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke(); }
      for (var i = 0; i < N; i++) {
        var p = pts[i]; p.x += p.vx; p.y += p.vy;
        if (p.x < 0 || p.x > W) p.vx *= -1; if (p.y < 0 || p.y > H) p.vy *= -1;
        ctx.fillStyle = 'rgba(80,180,255,0.55)'; ctx.beginPath(); ctx.arc(p.x, p.y, 1.6, 0, 7); ctx.fill();
        for (var j = i + 1; j < N; j++) {
          var q = pts[j], dx = p.x - q.x, dy = p.y - q.y, d = Math.sqrt(dx * dx + dy * dy);
          if (d < 120) { ctx.strokeStyle = 'rgba(80,180,255,' + (0.18 * (1 - d / 120)) + ')'; ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke(); }
        }
      }
      requestAnimationFrame(tick);
    }
    tick();
  }

  /* ---------------- 数据新鲜度轮询：检测后台是否已重新构建 ---------------- */
  function startFreshnessWatch() {
    var curGen = (D.meta && D.meta.generated_at) || '';
    var banner = document.getElementById('refreshBanner');
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'refreshBanner';
      banner.className = 'refresh-banner';
      banner.style.display = 'none';
      document.body.insertBefore(banner, document.body.firstChild);
    }
    function check() {
      try {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', 'meta.json?_=' + Date.now(), true);
        xhr.onreadystatechange = function () {
          if (xhr.readyState !== 4) return;
          if (xhr.status !== 200) return;
          try {
            var m = JSON.parse(xhr.responseText);
            head();  // 刷新顶部新鲜度文案
            if (m.generated_at && curGen && m.generated_at !== curGen) {
              banner.textContent = '📡 已更新至 ' + (m.generated_at || '') + ' · 点击刷新查看最新分析';
              banner.onclick = function () { location.reload(); };
              banner.style.display = 'block';
            }
          } catch (e) {}
        };
        xhr.send();
      } catch (e) {}
    }
    check();
    setInterval(check, 180000);  // 每 3 分钟探测一次
  }

  /* ---------------- 用户管理（owner 专属）----------------
   * 两种模式，自动择优：
   *   local  —— 本机在跑 manage_users.py serve：走本机 API（它持令牌，最省事）
   *   remote —— 不在本机（手机 / 别人的电脑）：浏览器直接对 GitHub API 操作
   *             · 现有名单从 data/_admin.bin 解出（GitHub 密钥只能写不能读，
   *               所以云端额外存了一份用 owner 口令加密的名单快照）
   *             · 改完用 NaCl 密封加密写回 ALLOWED_USERS_JSON 密钥，再触发构建
   *             · 令牌只留在本次会话，明文口令从不上传
   */
  var MU = {
    users: [], connected: false, overlay: null, noteEl: null,
    mode: null,          // 'local' | 'remote'
    ownerPass: '',       // 解开名单快照用，仅存内存
    token: '',           // GitHub 令牌（旧模式直连用），仅存内存 / sessionStorage
    adminKey: '',        // 代理 Worker 的管理密钥（新模式用），仅存内存 / sessionStorage
    repoOwner: '', repoName: 'stock-analysis'
  };
  var MU_TOKEN_KEY = 'sa_gh_token';
  var MU_ADMIN_KEY = 'sa_worker_admin_key';
  // 管理代理 Worker：留空 = 沿用旧模式（浏览器直连 GitHub，需粘贴令牌）
  // 填入你的 Worker URL 后 = 浏览器只发「管理密钥(ADMIN_KEY)」，GitHub 令牌仅存于 Worker 服务端
  var WORKER_URL = 'https://stock-admin.37204360.workers.dev';

  // 站点部署地址：<owner>.github.io/<repo>/ 或 CF Pages（stock-analysis-8zm.pages.dev，仓库固定）；据此推断仓库
  function muRepo() {
    if (!MU.repoOwner) {
      var m = /^([^.]+)\.github\.io$/i.exec(location.hostname || '');
      if (m) {
        MU.repoOwner = m[1];
        var seg = (location.pathname || '').split('/').filter(Boolean);
        if (seg.length) MU.repoName = seg[0];
      } else {
        // CF Pages / 本地预览 / 其他托管：仓库固定（站点已迁移至 stock-analysis-8zm.pages.dev）
        MU.repoOwner = 'fisk9r';
        MU.repoName = 'stock-analysis';
      }
    }
    return MU.repoOwner + '/' + MU.repoName;
  }
  function muRecallToken() {
    if (MU.token) return MU.token;
    try { MU.token = sessionStorage.getItem(MU_TOKEN_KEY) || localStorage.getItem(MU_TOKEN_KEY) || ''; } catch (e) {}
    return MU.token;
  }
  function muRecallAdminKey() {
    if (MU.adminKey) return MU.adminKey;
    try { MU.adminKey = sessionStorage.getItem(MU_ADMIN_KEY) || localStorage.getItem(MU_ADMIN_KEY) || ''; } catch (e) {}
    return MU.adminKey;
  }
  // Worker 地址：编译期常量优先；否则允许在面板「代理设置」里运行时配置（存 localStorage）
  function muWorkerUrl() {
    if (WORKER_URL) return WORKER_URL;
    try { return (localStorage.getItem('sa_worker_url') || '').trim(); } catch (e) { return ''; }
  }
  // 登录时若勾了「记住口令」，这里就能直接拿到 owner 口令，免得再输一次
  function muRecallPass() {
    try {
      var v = JSON.parse(localStorage.getItem('sa_auth_v1') || 'null');
      if (v && v.id === 'owner' && v.pass) return v.pass;
    } catch (e) {}
    return '';
  }
  function muLoadNacl() {
    if (window.SA_SEAL) return Promise.resolve();
    return new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = 'nacl.js?t=' + Math.floor(Date.now() / 3600000);
      s.onload = function () { window.SA_SEAL ? res() : rej(new Error('加密库加载异常')); };
      s.onerror = function () { rej(new Error('加密库 nacl.js 加载失败')); };
      document.head.appendChild(s);
    });
  }
  function saEsc(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
  function muGenPass() {
    var a = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    var arr = new Uint8Array(14); crypto.getRandomValues(arr);
    return Array.from(arr, function (v) { return a[v % a.length]; }).join('');
  }
  function ensureMgmtStyle() {
    if (document.getElementById('sa-mgmt-css')) return;
    var css = [
      '.sa-mgmt-ov{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;',
      'background:rgba(8,12,20,.85);backdrop-filter:blur(6px);',
      'font-family:-apple-system,Segoe UI,Roboto,"Microsoft YaHei",sans-serif;}',
      '.sa-mgmt{width:540px;max-width:92vw;max-height:88vh;overflow:auto;background:#0f1626;border:1px solid #1e2a44;',
      'border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.5);color:#e8eefc;}',
      '.sa-mgmt-h{display:flex;align-items:center;justify-content:space-between;padding:16px 18px;',
      'border-bottom:1px solid #1e2a44;font-size:15px;font-weight:700;color:#e8eefc;}',
      '.sa-mgmt-x{cursor:pointer;color:#8aa0c8;font-size:16px;line-height:1;padding:2px 6px;border-radius:6px;}',
      '.sa-mgmt-x:hover{background:#1e2a44;color:#e8eefc;}',
      '.sa-mgmt-b{padding:16px 18px;}',
      '.sa-mgmt-loading,.sa-mgmt-empty,.sa-mgmt-off{font-size:13px;color:#8aa0c8;line-height:1.7;}',
      '.sa-mgmt-add{display:flex;gap:8px;margin-bottom:14px;}',
      '.sa-mgmt-add input{flex:1;padding:9px 11px;border-radius:8px;border:1px solid #243453;background:#0a1120;',
      'color:#e8eefc;font-size:14px;outline:none;font-family:inherit;}',
      '.sa-mgmt-add input:focus{border-color:#3b82f6;}',
      '.sa-mgmt-t{width:100%;border-collapse:collapse;font-size:13px;}',
      '.sa-mgmt-t th{background:#0a1120;color:#8aa0c8;font-weight:600;font-size:11.5px;text-align:left;padding:8px 10px;border-bottom:1px solid #1e2a44;}',
      '.sa-mgmt-t td{padding:8px 10px;border-bottom:1px solid #16203a;vertical-align:middle;}',
      '.sa-mgmt-t .pw code{color:#34d399;font-family:"SF Mono",Menlo,monospace;font-size:12px;user-select:all;word-break:break-all;}',
      '.sa-mgmt-t .tag{font-size:10.5px;color:#60a5fa;border:1px solid #2451a3;border-radius:4px;padding:0 5px;margin-left:4px;}',
      '.mbtn{display:inline-flex;align-items:center;gap:4px;padding:7px 14px;border:0;border-radius:8px;font-size:12.5px;',
      'font-weight:600;cursor:pointer;font-family:inherit;background:#1e2a44;color:#cfe0ff;white-space:nowrap;text-decoration:none;}',
      '.mbtn:hover{background:#27375a;}',
      '.mbtn-p{background:linear-gradient(135deg,#2563eb,#06b6d4);color:#fff;}',
      '.mbtn-p:hover{filter:brightness(1.08);}',
      '.mbtn-d{background:#3b1d1d;color:#fca5a5;}',
      '.mbtn-d:hover{background:#4d2424;}',
      '.mbtn-ghost{background:#0a1120;border:1px solid #243453;color:#9fb3d8;}',
      '.mbtn-ghost:hover{background:#16203a;}',
      '.sa-mgmt-actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;}',
      '.sa-mgmt-note{margin-top:10px;font-size:12.5px;min-height:16px;color:#8aa0c8;}',
      '.sa-mgmt-note.ok{color:#34d399;}.sa-mgmt-note.err{color:#f87171;}.sa-mgmt-note.info{color:#60a5fa;}',
      '.sa-mgmt-hint{margin-top:10px;font-size:11.5px;color:#6b7a99;line-height:1.6;}',
      '.sa-mgmt-off .cmd{display:block;margin:10px 0;padding:10px 12px;background:#0a1120;border:1px solid #243453;',
      'border-radius:8px;color:#7dd3fc;font-family:"SF Mono",Menlo,monospace;font-size:13px;user-select:all;}',
      '.sa-mgmt .muted{color:#6b7a99;font-size:12px;}',
      // 远程模式的口令 / 令牌输入
      '.mu-lb{display:block;margin:14px 0 6px;font-size:12.5px;color:#9fb3d8;}',
      '.mu-lb .muted{margin-left:6px;}',
      '.mu-in{width:100%;box-sizing:border-box;padding:9px 11px;border-radius:8px;border:1px solid #243453;',
      'background:#0a1120;color:#e8eefc;font-size:14px;outline:none;font-family:inherit;}',
      '.mu-in:focus{border-color:#3b82f6;}',
      '.mu-ck{display:flex;align-items:center;gap:7px;margin-top:12px;font-size:12.5px;color:#9fb3d8;cursor:pointer;}',
      '.mu-ck input{margin:0;}',
      '.mu-badge{display:inline-flex;align-items:center;padding:7px 12px;border-radius:8px;font-size:11.5px;',
      'background:#0a1120;border:1px solid #243453;color:#7dd3fc;white-space:nowrap;}',
      '.sa-mgmt-hint code{color:#7dd3fc;font-family:"SF Mono",Menlo,monospace;}'
    ].join('');
    var s = document.createElement('style'); s.id = 'sa-mgmt-css'; s.textContent = css;
    document.head.appendChild(s);
  }
  function muNote(msg, type) {
    if (!MU.noteEl) return;
    MU.noteEl.textContent = msg;
    MU.noteEl.className = 'sa-mgmt-note ' + (type || '');
  }
  function muConnect() {
    var ac = new AbortController();
    var t = setTimeout(function () { ac.abort(); }, 2500);
    fetch('http://127.0.0.1:18789/api/users', { cache: 'no-store', signal: ac.signal })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) {
        clearTimeout(t);
        MU.users = (d && d.users) || []; MU.connected = true; MU.mode = 'local';
        muRender();
      })
      .catch(function () {
        // 本机没开服务 —— 不是死路，转为浏览器直连 GitHub 的远程模式
        clearTimeout(t); MU.connected = false; MU.mode = 'remote';
        muStartRemote();
      });
  }

  /* —— 远程模式：先把「owner 口令 + GitHub 令牌」凑齐，再拉名单 —— */
  function muStartRemote() {
    MU.ownerPass = MU.ownerPass || muRecallPass();
    if (muWorkerUrl()) muRecallAdminKey(); else muRecallToken();
    var haveKey = muWorkerUrl() ? MU.adminKey : MU.token;
    if (MU.ownerPass && haveKey) { muLoadRemote(); return; }
    muRenderRemoteGate();
  }

  function muRenderRemoteGate(errMsg) {
    var b = document.getElementById('saMgmtBody'); if (!b) return;
    var needPass = !MU.ownerPass;
    var useWorker = !!muWorkerUrl();
    var secretLabel = useWorker
      ? '管理密钥<span class="muted">（你在 Cloudflare Worker 里设的 ADMIN_KEY，仅本机会话保存，不是 GitHub 令牌）</span>'
      : 'GitHub 令牌<span class="muted">（需要 repo 权限，仅本次会话保存，不上传任何地方）</span>';
    var secretInput = useWorker
      ? '<input class="mu-in" id="muTokIn" type="password" placeholder="ADMIN_KEY（Worker 管理密钥）" autocomplete="off">'
      : '<input class="mu-in" id="muTokIn" type="password" placeholder="ghp_… 或 github_pat_…" autocomplete="off">';
    var keepBox = useWorker
      ? '<label class="mu-ck"><input type="checkbox" id="muTokKeep">在这台设备上记住管理密钥（下次免输）</label>'
      : '<label class="mu-ck"><input type="checkbox" id="muTokKeep">在这台设备上记住令牌（下次免输）</label>';
    var createLink = useWorker ? '' :
      '<a class="mbtn mbtn-ghost" href="https://github.com/settings/tokens/new?scopes=repo&description=stock-analysis%20admin" target="_blank" rel="noopener">去创建令牌</a>';
    /* 免令牌模式入口：还没配 Worker 时可从这里一键切换（填入 workers.dev 地址即可，无需改代码重发） */
    var workerSetupLink = useWorker ? '' :
      '<button class="mbtn mbtn-ghost" id="muWorkerSetupBtn">⚙ 免令牌模式（Cloudflare 代理）</button>';
    var testBtn = useWorker ? '<button class="mbtn" id="muTestBtn">测试连接</button>' : '';
    var hint = useWorker
      ? '<div class="sa-mgmt-hint">管理密钥是你在 Worker 环境变量里自定的密码，不等于 GitHub 令牌。GitHub 令牌只存在 Worker 服务端，浏览器永不接触——这就是选 ① 的意义。</div>'
      : '<div class="sa-mgmt-hint">创建令牌时勾选 <code>repo</code> 即可（fine-grained 令牌需要 Secrets 写 + Actions 写 + Contents 读）。令牌等于仓库钥匙，别发给别人；不想留痕就别勾「记住」。' +
        '不想用令牌？点上方「免令牌模式」，部署一个 Cloudflare Worker 后只输自定密码。</div>';
    b.innerHTML = '<div class="sa-mgmt-off">' +
      '<p>本机管理服务未运行，已切换到<b style="color:#7dd3fc">远程模式</b>——直接在这台设备上改，不需要你家里的电脑开机。</p>' +
      (needPass
        ? '<label class="mu-lb">管理员口令<span class="muted">（你登录本站用的那个，用于解开云端名单）</span></label>' +
          '<input class="mu-in" id="muPassIn" type="password" placeholder="请输入管理员口令" autocomplete="off">'
        : '') +
      '<label class="mu-lb">' + secretLabel + '</label>' +
      secretInput +
      keepBox +
      '<div class="sa-mgmt-actions">' +
      '<button class="mbtn mbtn-p" id="muGoBtn">进入管理</button>' +
      testBtn +
      createLink +
      workerSetupLink +
      '</div>' +
      '<div class="sa-mgmt-note' + (errMsg ? ' err' : '') + '" id="muNote">' + (errMsg ? saEsc(errMsg) : '') + '</div>' +
      hint +
      '</div>';
    MU.noteEl = document.getElementById('muNote');
    var go = document.getElementById('muGoBtn');
    function submit() {
      var key = (document.getElementById('muTokIn').value || '').trim();
      if (needPass) {
        var p = (document.getElementById('muPassIn').value || '').trim();
        if (!p) { muNote('请输入管理员口令', 'err'); return; }
        MU.ownerPass = p;
      }
      if (!key) { muNote(useWorker ? '请填写管理密钥' : '请粘贴 GitHub 令牌', 'err'); return; }
      var keep = document.getElementById('muTokKeep') && document.getElementById('muTokKeep').checked;
      if (useWorker) {
        MU.adminKey = key;
        try { if (keep) localStorage.setItem(MU_ADMIN_KEY, key); else sessionStorage.setItem(MU_ADMIN_KEY, key); } catch (e) {}
      } else {
        MU.token = key;
        try { if (keep) localStorage.setItem(MU_TOKEN_KEY, key); else sessionStorage.setItem(MU_TOKEN_KEY, key); } catch (e) {}
      }
      muLoadRemote();
    }
    go.addEventListener('click', submit);
    var tst = document.getElementById('muTestBtn');
    if (tst) tst.addEventListener('click', function () {
      var k = (document.getElementById('muTokIn').value || '').trim();
      if (!k) { muNote('先填写管理密钥再测试', 'err'); return; }
      MU.adminKey = k;
      muTestWorker();
    });
    var ws = document.getElementById('muWorkerSetupBtn');
    if (ws) ws.addEventListener('click', function () {
      var u = prompt('粘贴你的 Cloudflare Worker 地址（形如 https://stock-admin.xxx.workers.dev）：\n' +
        '还没有？按 tools/worker/index.js 顶部的部署说明创建一个（约 5 分钟）。');
      if (u === null) return;
      u = (u || '').trim();
      if (!u) { muNote('未填写，保持当前模式', 'info'); return; }
      if (!/^https:\/\/.+\.workers\.dev\/?$/i.test(u) && !/^https:\/\/.+$/.test(u)) { muNote('地址格式不对（要 https:// 开头）', 'err'); return; }
      try { localStorage.setItem('sa_worker_url', u.replace(/\/+$/, '')); } catch (e2) {}
      muRenderRemoteGate('已切到免令牌模式，请输入你设的管理密钥(ADMIN_KEY)。');
    });
    b.querySelectorAll('.mu-in').forEach(function (inp) {
      inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') submit(); });
    });
  }

  /* 从云端取回名单：data/_admin.bin 是用 owner 口令加密的完整名单快照 */
  function muLoadRemote() {
    var b = document.getElementById('saMgmtBody');
    if (b) b.innerHTML = '<div class="sa-mgmt-loading">正在解密云端名单…（约 1 秒）</div>';
    MU.noteEl = null;
    fetch('data/_admin.bin?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('云端还没有名单快照（需要先部署一次新版流水线）');
        return r.arrayBuffer();
      })
      .then(function (buf) { return muDecrypt(new Uint8Array(buf), MU.ownerPass); })
      .then(function (txt) {
        var d;
        try { d = JSON.parse(txt); } catch (e) { throw new Error('管理员口令不正确'); }
        MU.users = (d && d.users) || [];
        MU.connected = true;
        muRender();
        muNote('已连接 GitHub（' + muRepo() + '），当前为远程模式', 'info');
      })
      .catch(function (e) {
        MU.ownerPass = '';   // 口令可能错了，回到入口重来
        muRenderRemoteGate((e && e.message) || String(e));
      });
  }

  /* 与 auth.js / encrypt_data.py 同款：PBKDF2-HMAC-SHA256 + HMAC 密钥流 XOR */
  function muDecrypt(bytes, pass) {
    var SALT_LEN = 16, ITER = 200000;
    var salt = bytes.slice(0, SALT_LEN), ct = bytes.slice(SALT_LEN);
    return crypto.subtle.importKey('raw', new TextEncoder().encode(pass), 'PBKDF2', false, ['deriveKey'])
      .then(function (mat) {
        return crypto.subtle.deriveKey(
          { name: 'PBKDF2', salt: salt, iterations: ITER, hash: 'SHA-256' },
          mat, { name: 'HMAC', hash: 'SHA-256', length: 256 }, false, ['sign']);
      })
      .then(function (key) {
        var out = new Uint8Array(ct.length), p = 0, i = 0;
        function block() {
          if (p >= ct.length) return Promise.resolve();
          var ctr = new Uint8Array(4);
          new DataView(ctr.buffer).setUint32(0, i, false);
          return crypto.subtle.sign('HMAC', key, ctr).then(function (mac) {
            mac = new Uint8Array(mac);
            for (var k = 0; k < mac.length && p < ct.length; k++) { out[p] = ct[p] ^ mac[k]; p++; }
            i++;
          }).then(block);
        }
        return block().then(function () { return new TextDecoder().decode(out); });
      });
  }

  function muGh(method, path, body) {
    return fetch('https://api.github.com' + path, {
      method: method,
      headers: {
        'Authorization': 'Bearer ' + MU.token,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
      },
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) {
      if (r.status === 204) return {};
      return r.text().then(function (t) {
        var j = null;
        try { j = t ? JSON.parse(t) : {}; } catch (e) {}
        if (!r.ok) {
          var msg = (j && j.message) || ('HTTP ' + r.status);
          if (r.status === 401) msg = '令牌无效或已过期';
          else if (r.status === 403) msg = '令牌权限不足（需要 repo / Secrets 写权限）';
          else if (r.status === 404) msg = '找不到仓库 ' + muRepo() + '（或令牌无权访问）';
          throw new Error(msg);
        }
        return j || {};
      });
    });
  }

  /* 代理 Worker 中转：GitHub 令牌只存于 Worker 服务端，浏览器只发「管理密钥(ADMIN_KEY)」 */
  function muProxyCall(action, payload) {
    return fetch(muWorkerUrl(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ admin_key: MU.adminKey, action: action }, payload || {}))
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) {
          var msg = (j && j.error) || ('HTTP ' + r.status);
          if (r.status === 403) msg = '管理密钥不正确或无权限';
          else if (r.status === 429) msg = '请求过于频繁，请稍后再试';
          throw new Error(msg);
        }
        return j || {};
      });
    });
  }
  /* 一键测试 Worker 连通性 + 管理密钥是否正确 */
  function muTestWorker() {
    var key = MU.adminKey;
    if (!key) { muNote('先填写管理密钥再测试', 'err'); return; }
    muNote('正在测试 Worker 连接…', 'info');
    fetch(muWorkerUrl(), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ admin_key: key, action: 'ping' })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      if (res.ok && res.j && res.j.ok) muNote('Worker 连接正常 ✅（仓库：' + saEsc(res.j.repo || '') + '）', 'ok');
      else muNote('连接异常：' + ((res.j && res.j.error) || '未知响应'), 'err');
    }).catch(function (e) {
      muNote('连不上 Worker：' + ((e && e.message) || e), 'err');
    });
  }
  function muGetPubKey() {
    if (muWorkerUrl()) return muProxyCall('public-key', {});
    return muGh('GET', '/repos/' + muRepo() + '/actions/secrets/public-key');
  }
  function muSecretWrite(name, sealed, keyId) {
    if (muWorkerUrl()) return muProxyCall('put-secret', { secret_name: name, encrypted_value: sealed, key_id: keyId });
    return muGh('PUT', '/repos/' + muRepo() + '/actions/secrets/' + name, { encrypted_value: sealed, key_id: keyId });
  }
  function muDispatchBuild() {
    if (muWorkerUrl()) return muProxyCall('dispatch', {});
    return muGh('POST', '/repos/' + muRepo() + '/actions/workflows/stock.yml/dispatches', { ref: 'main', inputs: { task: 'build' } });
  }

  /* 远程保存：密封加密写回 Secret → 触发构建。明文口令只在浏览器里，不经第三方 */
  function muSaveDeployRemote() {
    if (!MU.users.length) { muNote('至少要保留一个用户', 'err'); return; }
    var btn = document.getElementById('muDeployBtn');
    if (btn) btn.disabled = true;
    muNote('正在加载加密库…', 'info');
    muLoadNacl()
      .then(function () {
        muNote('正在写入 GitHub 密钥…', 'info');
        return muGetPubKey();
      })
      .then(function (pk) {
        if (!pk || !pk.key || !pk.key_id) throw new Error('没能取到仓库公钥');
        var sealed = window.SA_SEAL(JSON.stringify({ users: MU.users }), pk.key);
        return muSecretWrite('ALLOWED_USERS_JSON', sealed, pk.key_id);
      })
      .then(function () {
        muNote('密钥已更新，正在触发重建…', 'info');
        return muDispatchBuild();
      })
      .then(function () {
        if (btn) btn.disabled = false;
        muNote('已提交 ✅ 云端正在重建，约 2 分钟后生效（期间旧口令仍可用）', 'ok');
      })
      .catch(function (e) {
        if (btn) btn.disabled = false;
        muNote('失败：' + ((e && e.message) || e), 'err');
      });
  }
  function muRender() {
    var b = document.getElementById('saMgmtBody'); if (!b) return;
    var isRemote = MU.mode === 'remote';
    var rows = MU.users.map(function (u, i) {
      var isOwner = u.id === 'owner';
      var sc = (u.sc || '').toString();
      var pp = (u.pp || '').toString();
      var hd = (Array.isArray(u.holdings) ? u.holdings : []).join(',');
      return '<tr>' +
        '<td class="id">' + saEsc(u.id) + '</td>' +
        '<td class="nm">' + saEsc(u.name) + (isOwner ? ' <span class="tag">管理员</span>' : '') + '</td>' +
        '<td class="pw">' + (isOwner ? '—' : '<code>' + saEsc(u.pass) + '</code> <button class="mbtn" data-act="cp" data-i="' + i + '">改口令</button>') + '</td>' +
        '<td class="keys">' +
          '<input class="mu-in" data-f="sc" data-i="' + i + '" placeholder="ServerChan 密钥(留空=共用广播)" value="' + saEsc(sc) + '">' +
          '<input class="mu-in" data-f="pp" data-i="' + i + '" placeholder="PushPlus 令牌(留空=共用广播)" value="' + saEsc(pp) + '">' +
        '</td>' +
        '<td class="hold"><input class="mu-in" data-f="holdings" data-i="' + i + '" placeholder="本人持仓代码，逗号分隔(如 600519,000001)" value="' + saEsc(hd) + '"></td>' +
        '<td class="ac">' +
        (isOwner ? '<span class="muted">不可删除</span>'
          : '<button class="mbtn mbtn-d" data-act="rm" data-i="' + i + '">删除</button>') +
        '</td></tr>';
    }).join('');
    b.innerHTML =
      '<div class="sa-mgmt-add">' +
      '<input id="muNewId" placeholder="用户名（登录账户，如 lily）" maxlength="20">' +
      '<input id="muNewName" placeholder="显示名称（可选，如 莉莉）" maxlength="20">' +
      '<button class="mbtn mbtn-p" id="muAddBtn">+ 添加</button></div>' +
      (MU.users.length
        ? '<table class="sa-mgmt-t"><thead><tr><th>账户</th><th>名称</th><th>口令</th><th>推送密钥(SC/PP)</th><th>本人持仓(个性化推送用)</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table>'
        : '<div class="sa-mgmt-empty">暂无其他用户。添加一个，把「账户名 + 口令」发给对方即可。</div>') +
      '<div class="sa-mgmt-actions">' +
      '<button class="mbtn mbtn-p" id="muDeployBtn">保存并部署</button>' +
      (isRemote
        ? '<span class="mu-badge">远程模式 · ' + saEsc(muRepo()) + '</span>' +
          '<button class="mbtn mbtn-ghost" id="muForgetBtn">清除令牌</button>'
        : '<a class="mbtn mbtn-ghost" href="http://127.0.0.1:18789/" target="_blank" rel="noopener">在本地页面打开</a>' +
          '<span class="mu-badge">本机模式</span>') +
      '</div>' +
      '<div class="sa-mgmt-note" id="muNote"></div>' +
      '<div class="sa-mgmt-hint">修改后必须「保存并部署」才会生效（云端为每个用户重新生成加密数据，约 2 分钟）。' +
      (isRemote ? '口令明文只在你这台设备上，写到 GitHub 前会先加密。' : '') +
      '｜ 想让某用户收『本人持仓专属复盘』：在其行填 ServerChan/PushPlus 密钥 + 本人持仓代码（逗号分隔）即可，未填则自动并入共用广播。' + '</div>';
    MU.noteEl = document.getElementById('muNote');
    document.getElementById('muAddBtn').addEventListener('click', function () {
      var uid = document.getElementById('muNewId').value.trim();
      var unm = document.getElementById('muNewName').value.trim();
      if (!uid) { muNote('请输入用户名', 'err'); return; }
      muAdd(uid, unm);
    });
    document.getElementById('muDeployBtn').addEventListener('click',
      isRemote ? muSaveDeployRemote : muSaveDeploy);
    if (isRemote) {
      document.getElementById('muForgetBtn').addEventListener('click', function () {
        if (muWorkerUrl()) {
          MU.adminKey = '';
          try { sessionStorage.removeItem(MU_ADMIN_KEY); localStorage.removeItem(MU_ADMIN_KEY); } catch (e) {}
          muRenderRemoteGate('管理密钥已清除，需要重新输入。');
        } else {
          MU.token = '';
          try { sessionStorage.removeItem(MU_TOKEN_KEY); localStorage.removeItem(MU_TOKEN_KEY); } catch (e) {}
          muRenderRemoteGate('令牌已清除，需要重新输入。');
        }
      });
    }
    b.querySelectorAll('button[data-act]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var i = parseInt(btn.dataset.i, 10), act = btn.dataset.act;
        if (act === 'rm') muRemove(i); else if (act === 'cp') muChgPass(i);
      });
    });
    // 每用户推送密钥(SC/PP) + 本人持仓：输入即时回写 MU.users（无需手动保存，点"保存并部署"即密封上传）
    b.querySelectorAll('input[data-f]').forEach(function (inp) {
      inp.addEventListener('input', function () {
        var i = parseInt(inp.dataset.i, 10);
        var f = inp.dataset.f;
        var u = MU.users[i];
        if (!u) return;
        if (f === 'holdings') {
          u.holdings = inp.value.split(/[,，\s]+/).map(function (s) { return s.trim(); }).filter(Boolean);
          if (!u.holdings.length) u.holdings = [];
        } else {
          u[f] = inp.value.trim();
        }
      });
    });
  }
  function muAdd(uname, dname) {
    var id = (uname || '').trim().toLowerCase().replace(/[^a-z0-9一-龥]/g, '').slice(0, 20);
    if (!id) { muNote('用户名含非法字符或为空', 'err'); return; }
    if (MU.users.some(function (u) { return u.id === id; })) { muNote('用户名「' + id + '」已存在', 'err'); return; }
    var name = (dname || '').trim() || id;
    var pass = muGenPass();
    MU.users.push({ id: id, name: name, pass: pass, sc: '', pp: '', holdings: [] });
    muRender(); muNote('已添加「' + name + '」（' + id + '），口令：' + pass + '（保存并部署后生效）', 'ok');
  }
  function muRemove(i) {
    var u = MU.users[i]; if (!u || u.id === 'owner') return;
    if (!confirm('删除「' + u.name + '」？删除后该用户将无法解密数据。')) return;
    MU.users.splice(i, 1); muRender(); muNote('已删除「' + u.name + '」', 'info');
  }
  function muChgPass(i) {
    var u = MU.users[i]; if (!u) return;
    var p = prompt('为「' + u.name + '」设置新口令（留空则自动生成）：');
    if (p === null) return;
    u.pass = (p.trim()) || muGenPass();
    muRender(); muNote('已为「' + u.name + '」更新口令：' + u.pass, 'ok');
  }
  function muSaveDeploy() {
    muNote('正在保存并部署…', 'info');
    fetch('http://127.0.0.1:18789/api/save-users', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ users: MU.users })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) throw new Error(d.error || '保存失败');
        return fetch('http://127.0.0.1:18789/api/deploy', { method: 'POST' }).then(function (r2) { return r2.json(); });
      })
      .then(function (d2) {
        if (d2 && d2.ok) muNote('部署已触发，约 2 分钟后生效 ✅', 'ok');
        else muNote('保存成功，但部署失败：' + ((d2 && d2.error) || '未知错误'), 'err');
      })
      .catch(function (e) {
        muNote('无法连接本机服务：' + ((e && e.message) || e) + '。请确认 manage_users.py serve 正在运行。', 'err');
      });
  }
  function openUserMgr() {
    ensureMgmtStyle();
    if (MU.overlay && MU.overlay.parentNode) MU.overlay.parentNode.removeChild(MU.overlay);
    var ov = document.createElement('div'); ov.className = 'sa-mgmt-ov';
    var md = document.createElement('div'); md.className = 'sa-mgmt';
    ov.appendChild(md);
    ov.addEventListener('click', function (e) { if (e.target === ov) ov.parentNode.removeChild(ov); });
    document.body.appendChild(ov);
    MU.overlay = ov;
    md.innerHTML = '<div class="sa-mgmt-h">⚙ 访问人员管理' +
      '<span class="sa-mgmt-x" onclick="var o=this.closest(\'.sa-mgmt-ov\');if(o)o.remove()">✕</span></div>' +
      '<div class="sa-mgmt-b" id="saMgmtBody"><div class="sa-mgmt-loading">正在连接本机管理服务…</div></div>';
    MU.noteEl = null;
    muConnect();
  }

  /* ---------------- K线（浏览器直连腾讯财经公开接口，无需本机、不落盘） ---------------- */
  var KL = { overlay: null };
  function ensureKlineStyle() {
    if (document.getElementById('kl-css')) return;
    var css = [
      '.kl-ov{position:fixed;inset:0;z-index:9998;display:flex;align-items:center;justify-content:center;',
      'background:rgba(6,10,18,.82);backdrop-filter:blur(6px);',
      'font-family:-apple-system,Segoe UI,Roboto,"Microsoft YaHei",sans-serif;}',
      '.kl{width:780px;max-width:94vw;max-height:92vh;display:flex;flex-direction:column;',
      'background:var(--card);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow-lg);overflow:hidden;',
      'animation:klIn .22s ease}',
      '@keyframes klIn{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:none}}',
      '@media (prefers-reduced-motion:reduce){.kl{animation:none}}',
      '.kl-h{display:flex;align-items:center;justify-content:space-between;padding:13px 16px;border-bottom:1px solid var(--border);}',
      '.kl-t{font-size:15px;font-weight:700;color:var(--text);display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;}',
      '.kl-t .kl-code{font-size:12px;color:var(--muted);font-family:"SF Mono",Menlo,monospace;}',
      '.kl-t .kl-last{font-size:12.5px;font-family:"SF Mono",Menlo,monospace;}',
      '.kl-x{cursor:pointer;color:var(--muted);font-size:16px;line-height:1;padding:2px 7px;border-radius:6px;}',
      '.kl-x:hover{background:var(--card-2);color:var(--text);}',
      '.kl-b{padding:14px 16px;overflow:auto;}',
      '.kl-cv{width:100%;height:380px;display:block;}',
      '.kl-legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin:8px 0 2px;}',
      '.kl-msg{font-size:13px;color:var(--muted);line-height:1.7;}',
      '.kl-off{color:var(--warn);background:var(--warn-bg);border:1px solid var(--warn-br);border-radius:8px;padding:12px 14px;}',
      '.kl-off code{user-select:all;}',
      '.kl-hint{margin-top:10px;font-size:11.5px;color:var(--faint);line-height:1.6;}',
      '.kl-loading,.kl-empty{padding:26px;text-align:center;color:var(--muted);font-size:13px;}',
      '.plist{display:flex;flex-direction:column;gap:6px;max-height:62vh;overflow:auto;}',
      '.pitem{display:flex;align-items:center;gap:10px;padding:8px 10px;border:1px solid var(--border);',
      'border-radius:8px;background:var(--card-2);font-size:13px;}',
      '.pitem .code{color:var(--faint);font-size:11.5px;}',
      '.pitem .muted{color:var(--muted);font-size:11.5px;}'
    ].join('');
    var s = document.createElement('style'); s.id = 'kl-css'; s.textContent = css;
    document.head.appendChild(s);
  }
  function klTheme() {
    var tech = (document.documentElement && document.documentElement.getAttribute('data-theme')) === 'tech';
    return tech ? {
      up: '#ff6b6b', down: '#3ddc84', grid: 'rgba(34,211,238,.12)', axis: 'rgba(147,165,196,.5)',
      text: '#93a5c4', ma5: '#fbbf24', ma10: '#22d3ee', ma20: '#a78bfa', cross: 'rgba(34,211,238,.55)', bg: '#0b1424'
    } : {
      up: '#d12626', down: '#217a33', grid: '#eceff3', axis: 'rgba(91,103,121,.5)',
      text: '#5b6779', ma5: '#d97706', ma10: '#1971c2', ma20: '#7c3aed', cross: 'rgba(24,100,171,.5)', bg: '#ffffff'
    };
  }
  /* 可点击的个股名（点击弹出日K线） */
  function stk(code, name) {
    return '<span class="stk" data-code="' + E(code || '') + '" data-name="' + E(name || code || '') + '">' + E(name || code || '') + '</span>';
  }
  // 浏览器直连腾讯财经公开行情接口（Access-Control-Allow-Origin: *，可跨域 fetch）。
  // 不依赖本机服务、不落地存储；腾讯不可达时经公开 CORS 代理兜底新浪。
  function fetchKline(code) {
    code = (code || '').trim();
    if (!/^\d{6}$/.test(code)) return Promise.resolve({ ok: false, error: '代码格式应为6位数字', klines: [] });
    var mkt = /^(60|68|90|11|50|51|56|58|110|113|118|132|204)/.test(code) ? 'sh' : 'sz';
    var sym = mkt + code;
    var tUrl = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=' + sym + ',day,,,130,qfq';
    function normalizeTencent(j) {
      var node = j && j.data && j.data[sym];
      var rows = node && (node.qfqday || node.day);
      if (!rows || !rows.length) throw new Error('腾讯无K线数据');
      var name = (node.qt && node.qt[sym] && node.qt[sym][1]) || code;
      var klines = rows.map(function (r) {
        return { date: r[0], open: +r[1], close: +r[2], high: +r[3], low: +r[4], vol: +r[5], amount: +r[5] * 100 * (+r[2]) };
      });
      return { ok: true, code: code, name: name, source: '腾讯财经', klines: klines };
    }
    function sinaFallback() {
      var sUrl = 'https://api.allorigins.win/raw?url=' + encodeURIComponent(
        'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=' + sym + '&scale=240&ma=no&datalen=130');
      return fetch(sUrl, { mode: 'cors' }).then(function (r2) {
        if (!r2.ok) throw new Error('HTTP ' + r2.status);
        return r2.json();
      }).then(function (arr) {
        if (!arr || !arr.length) throw new Error('新浪也无数据');
        var klines = arr.map(function (k) {
          return { date: k.day, open: +k.open, close: +k.close, high: +k.high, low: +k.low, vol: +k.volume, amount: 0 };
        });
        return { ok: true, code: code, name: code, source: '新浪财经', klines: klines };
      });
    }
    return fetch(tUrl, { mode: 'cors' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(normalizeTencent)
      .catch(function () { return sinaFallback(); })
      .catch(function (e2) { return { ok: false, error: (e2 && e2.message) || '行情接口不可用', klines: [] }; });
  }
  /* canvas 蜡烛图：阳线红/阴线绿（A股习惯），叠加 MA5/10/20 + 量能 + 十字光标 */
  function drawKline(canvas, kl, theme) {
    var dpr = window.devicePixelRatio || 1;
    var W = canvas.clientWidth || 740, H = canvas.clientHeight || 380;
    canvas.width = Math.max(1, W * dpr); canvas.height = Math.max(1, H * dpr);
    var ctx = canvas.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var n = kl.length; if (!n) return;
    var padL = 10, padR = 60, padT = 14, padB = 22;
    var priceH = Math.round(H * 0.72), volH = Math.round(H * 0.20);
    var gap = H - padT - priceH - volH - padB; if (gap < 2) gap = 2;
    var volTop = padT + priceH + gap;
    var lo = Infinity, hi = -Infinity, vmax = 0;
    for (var i = 0; i < n; i++) { var k = kl[i]; if (k.low < lo) lo = k.low; if (k.high > hi) hi = k.high; if (k.vol > vmax) vmax = k.vol; }
    var padY = (hi - lo) * 0.08 || 1; hi += padY; lo -= padY;
    var plotW = W - padL - padR;
    var cw = plotW / n;
    function x(i) { return padL + cw * (i + 0.5); }
    function y(p) { return padT + (hi - p) / (hi - lo) * priceH; }
    function ma(len) { var out = [], s = 0; for (var i = 0; i < n; i++) { if (i < len - 1) { out.push(null); continue; } s += kl[i].close; if (i >= len) s -= kl[i - len].close; out.push(s / len); } return out; }
    var ma5 = ma(5), ma10 = ma(10), ma20 = ma(20);
    function render(hv) {
      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = theme.bg; ctx.fillRect(0, 0, W, H);
      ctx.font = '10px SF Mono,Menlo,Consolas,monospace'; ctx.textBaseline = 'middle';
      ctx.strokeStyle = theme.grid; ctx.lineWidth = 1;
      for (var g = 0; g <= 4; g++) {
        var py = padT + priceH * g / 4;
        ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(padL + plotW, py); ctx.stroke();
        ctx.fillStyle = theme.text; ctx.textAlign = 'left';
        ctx.fillText((hi - (hi - lo) * g / 4).toFixed(2), padL + plotW + 5, py);
      }
      function drawMA(arr, color) {
        ctx.strokeStyle = color; ctx.lineWidth = 1.3; ctx.beginPath(); var started = false;
        for (var i = 0; i < n; i++) { if (arr[i] == null) continue; var xx = x(i), yy = y(arr[i]); if (!started) { ctx.moveTo(xx, yy); started = true; } else ctx.lineTo(xx, yy); }
        ctx.stroke();
      }
      drawMA(ma5, theme.ma5); drawMA(ma10, theme.ma10); drawMA(ma20, theme.ma20);
      var bw = Math.max(1.6, cw * 0.62);
      for (var i = 0; i < n; i++) {
        var k = kl[i], up = k.close >= k.open, col = up ? theme.up : theme.down;
        ctx.strokeStyle = col; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x(i), y(k.high)); ctx.lineTo(x(i), y(k.low)); ctx.stroke();
        var ytop = y(Math.max(k.open, k.close)), ybot = y(Math.min(k.open, k.close));
        ctx.fillStyle = col; ctx.fillRect(x(i) - bw / 2, ytop, bw, Math.max(1, ybot - ytop));
      }
      for (var i = 0; i < n; i++) {
        var k = kl[i], up = k.close >= k.open;
        ctx.fillStyle = up ? theme.up : theme.down; ctx.globalAlpha = 0.5;
        var vh = (k.vol / (vmax || 1)) * volH;
        ctx.fillRect(x(i) - bw / 2, volTop + volH - vh, bw, vh);
      }
      ctx.globalAlpha = 1;
      var yl = y(kl[n - 1].close);
      ctx.strokeStyle = theme.axis; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(padL, yl); ctx.lineTo(padL + plotW, yl); ctx.stroke(); ctx.setLineDash([]);
      if (hv >= 0 && hv < n) {
        var cx = x(hv); ctx.strokeStyle = theme.cross; ctx.setLineDash([2, 2]);
        ctx.beginPath(); ctx.moveTo(cx, padT); ctx.lineTo(cx, volTop + volH); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(padL, y(kl[hv].close)); ctx.lineTo(padL + plotW, y(kl[hv].close)); ctx.stroke(); ctx.setLineDash([]);
        var k = kl[hv], chg = hv > 0 ? (k.close - kl[hv - 1].close) / kl[hv - 1].close * 100 : 0;
        var tx = padL + 6, tw = 196, th = 64, ty = padT + 4;
        ctx.fillStyle = 'rgba(0,0,0,.55)'; ctx.fillRect(tx, ty, tw, th);
        ctx.fillStyle = theme.text; ctx.textAlign = 'left';
        ctx.fillText('日期 ' + k.date, tx + 8, ty + 11);
        ctx.fillText('开 ' + k.open.toFixed(2) + '  收 ' + k.close.toFixed(2), tx + 8, ty + 25);
        ctx.fillText('高 ' + k.high.toFixed(2) + '  低 ' + k.low.toFixed(2), tx + 8, ty + 39);
        ctx.fillStyle = chg >= 0 ? theme.up : theme.down;
        ctx.fillText('涨跌幅 ' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%', tx + 8, ty + 53);
      }
    }
    render(-1);
    canvas.onmousemove = function (e) {
      var r = canvas.getBoundingClientRect(), mx = e.clientX - r.left;
      var idx = Math.floor((mx - padL) / cw); if (idx < 0 || idx >= n) { render(-1); return; } render(idx);
    };
    canvas.onmouseleave = function () { render(-1); };
  }
  function openKline(code, name) {
    ensureKlineStyle();
    if (KL.overlay && KL.overlay.parentNode) KL.overlay.parentNode.removeChild(KL.overlay);
    var ov = document.createElement('div'); ov.className = 'kl-ov';
    var md = document.createElement('div'); md.className = 'kl';
    ov.appendChild(md);
    ov.addEventListener('click', function (e) { if (e.target === ov) ov.parentNode.removeChild(ov); });
    document.body.appendChild(ov); KL.overlay = ov;
    md.innerHTML =
      '<div class="kl-h"><div class="kl-t"><b class="kl-name"></b> <span class="kl-code"></span> <span class="kl-last"></span></div>' +
      '<span class="kl-x" onclick="var o=this.closest(\'.kl-ov\');if(o)o.remove()">✕</span></div>' +
      '<div class="kl-b">' +
      '<div class="kl-loading">正在从腾讯财经实时抓取日K线…</div>' +
      '<canvas class="kl-cv" style="display:none"></canvas>' +
      '<div class="kl-legend" style="display:none">' +
      '<span style="color:var(--up)">● 阳线(涨)</span> <span style="color:var(--down)">● 阴线(跌)</span> ' +
      '<span style="color:#fbbf24">— MA5</span> <span style="color:#22d3ee">— MA10</span> <span style="color:#a78bfa">— MA20</span></div>' +
      '<div class="kl-msg"></div>' +
      '<div class="kl-hint">数据实时取自腾讯财经公开接口，浏览器直连、不落地存储。鼠标移到 K 线上可看每日开 / 收 / 高 / 低。A股惯例：红涨 / 绿跌。</div>' +
      '</div>';
    md.querySelector('.kl-name').textContent = name || code;
    md.querySelector('.kl-code').textContent = code;
    var cv = md.querySelector('.kl-cv');
    fetchKline(code).then(function (d) {
      if (!d.ok || !d.klines || !d.klines.length) {
        md.querySelector('.kl-loading').style.display = 'none';
        md.querySelector('.kl-msg').innerHTML = '<div class="kl-off">未能获取 K 线：' + E(d.error || '无数据') +
          '<br><span style="color:var(--muted)">本功能由浏览器直连腾讯财经公开接口，请检查网络后重试。</span></div>';
        return;
      }
      md.querySelector('.kl-loading').style.display = 'none';
      cv.style.display = 'block'; md.querySelector('.kl-legend').style.display = 'flex';
      md.querySelector('.kl-name').textContent = d.name || name || code;
      var kl = d.klines.map(function (k) {
        return { date: k.date, open: +k.open, close: +k.close, high: +k.high, low: +k.low, vol: +k.vol, amount: +k.amount };
      });
      var last = kl[kl.length - 1], prev = kl[kl.length - 2] || last;
      var chg = (last.close - prev.close) / prev.close * 100;
      md.querySelector('.kl-last').innerHTML = '收盘 <b style="color:' + (chg >= 0 ? 'var(--up)' : 'var(--down)') + '">' +
        last.close.toFixed(2) + '</b> <span style="color:' + (chg >= 0 ? 'var(--up)' : 'var(--down)') + '">' +
        (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%</span> <span class="muted">(' + last.date + ')</span>';
      requestAnimationFrame(function () { drawKline(cv, kl, klTheme()); });
    }).catch(function (e) {
      md.querySelector('.kl-loading').style.display = 'none';
      md.querySelector('.kl-msg').innerHTML = '<div class="kl-off">无法获取 K 线：' + E((e && e.message) || e) +
        '<br><span style="color:var(--muted)">本功能由浏览器直连腾讯财经公开接口，请检查网络后重试。</span></div>';
    });
  }
  function openPatternList(pattern) {
    ensureKlineStyle();
    if (KL.overlay && KL.overlay.parentNode) KL.overlay.parentNode.removeChild(KL.overlay);
    var ov = document.createElement('div'); ov.className = 'kl-ov';
    var md = document.createElement('div'); md.className = 'kl';
    ov.appendChild(md);
    ov.addEventListener('click', function (e) { if (e.target === ov) ov.parentNode.removeChild(ov); });
    document.body.appendChild(ov); KL.overlay = ov;
    var A = D.auction || {}, items = A.items || {};
    var lus = D.limit_ups || [];
    var arr = lus.filter(function (r) { return items[r.code]; }).map(function (r) {
      var a = items[r.code], o = {}; for (var k in r) o[k] = r[k]; for (var k2 in a) o[k2] = a[k2]; return o;
    });
    var list = arr.filter(function (a) { return a.pattern === pattern; })
      .sort(function (a, b) { return (b.auction_score || 0) - (a.auction_score || 0); });
    var rows = list.map(function (a) {
      return '<div class="pitem">' + stk(a.code, a.name) +
        '<span class="code">' + E(a.code) + '</span>' + lbBadge(a.streak) +
        '<span class="muted">高开 ' + (a.open_pct >= 0 ? '+' : '') + f(a.open_pct, 2) + '%</span>' +
        '<span class="muted">日内 ' + (a.intraday >= 0 ? '+' : '') + f(a.intraday, 1) + '%</span>' +
        '<span class="muted">竞价强度 ' + f(a.auction_score, 0) + '</span></div>';
    }).join('');
    md.innerHTML = '<div class="kl-h"><div class="kl-t"><b>' + E(pattern) + '</b> · 共 ' + list.length + ' 只</div>' +
      '<span class="kl-x" onclick="var o=this.closest(\'.kl-ov\');if(o)o.remove()">✕</span></div>' +
      '<div class="kl-b">' + (list.length ? '<div class="plist">' + rows + '</div>' : '<div class="kl-empty">当日无该形态标的</div>') +
      '<div class="kl-hint">点击任意个股名称，查看其日 K 线。</div></div>';
  }

  /* ---------------- 牛股雷达 ---------------- */
  function sigBadge(s) {
    var m = {
      '阶段新高突破': 't-main', '平台突破': 't-main', '二波启动': 't-main', '趋势加速': 't-main',
      '反包': 't-sub', '均线发散': 't-sub', '深水拉板': 't-sub', '低位首板': 't-min',
      'N字回调': 't-min', '缺口不补': 't-min',
      '放量上涨': 't-main', '均线多头': 't-main', '海龟突破': 't-main',
      '停机坪': 't-sub', '高窄旗形': 't-sub', '稳健上行': 't-sub',
      '回踩长线': 't-min', '低ATR慢牛': 't-min', '放量跌停': 't-min'
    };
    return '<span class="bd ' + (m[s] || 't-min') + '">' + E(s) + '</span>';
  }
  function viewBull() {
    var rep = D.bull || [];
    var h = '';
    var multi = rep.filter(function (x) { return x.multi >= 2; }).length;
    var best = rep[0] || {};
    h += '<div class="grid g4" style="margin-bottom:16px">' +
      kpi('雷达命中', n2(rep.length), '多维度独立探测器共振', rep.length ? 'up' : '') +
      kpi('多信号共振', n2(multi), '≥2 维信号同时命中', multi ? 'up' : '') +
      kpi('最强标的', best.name ? E(best.name) : '—', best.signals ? best.signals.join('+') : '暂无') +
      kpi('最高评分', f(best.score), '信号强度加权', '') +
      '</div>';
    var desc = '10 类独立牛股探测器：阶段新高突破 · 平台突破 · 二波启动 · 反包 · 均线发散 · 深水拉板 · 低位首板 · N字回调 · 缺口不补 · 趋势加速。多维度共振越多，确定性越高。';
    if (!rep.length) {
      h += card('🐂 牛股雷达', '<div class="empty">今日无明确牛股信号（市场偏冷或风格混沌，建议控仓等待）</div>', desc);
      return h;
    }
    var rows = rep.map(function (it) {
      var sig = (it.signals || []).map(function (s) { return sigBadge(s); }).join(' ');
      return '<tr>' +
        '<td>' + stk(it.code, it.name) + '</td>' +
        '<td>' + sig + '</td>' +
        '<td class="num">' + f(it.score) + '</td>' +
        '<td class="num">' + (it.price != null ? f(it.price) : '—') + '</td>' +
        '<td class="num ' + ((it.pct || 0) >= 0 ? 'up' : 'down') + '">' + (it.pct != null ? sign(it.pct) : '—') + '</td>' +
        '<td class="num">' + (it.vol_ratio ? f(it.vol_ratio, 1) : '—') + '</td>' +
        '<td>' + E(it.ind || '') + '</td>' +
        '<td class="muted">' + E(it.tags || '') + '</td>' +
        '</tr>';
    }).join('');
    var cols = [
      { t: '个股' }, { t: '信号' }, { t: '评分', a: 'num' }, { t: '现价', a: 'num' },
      { t: '涨跌幅', a: 'num' }, { t: '量比', a: 'num' }, { t: '行业' }, { t: '说明' }
    ];
    h += card('🐂 牛股雷达 · Top' + rep.length, table(cols, rows), desc);
    return h;
  }

  /* ---------------- 经典策略库（开源策略移植） ---------------- */
  function viewStrategies() {
    var rep = D.strategies || [];
    var h = '';
    var multi = rep.filter(function (x) { return x.multi >= 2; }).length;
    var best = rep[0] || {};
    h += '<div class="grid g4" style="margin-bottom:16px">' +
      kpi('策略命中', n2(rep.length), '9 类经典选股策略', rep.length ? 'up' : '') +
      kpi('多策略共振', n2(multi), '≥2 个策略同时命中', multi ? 'up' : '') +
      kpi('最强标的', best.name ? E(best.name) : '—', best.signals ? best.signals.join('+') : '暂无') +
      kpi('最高评分', f(best.score), '多策略加权', '') +
      '</div>';
    var desc = '移植自 GitHub 开源项目（InStock 系）的经典选股策略：放量上涨 · 均线多头 · 停机坪 · 回踩长线(年线/半年线) · 海龟突破(唐奇安60日) · 高窄旗形 · 稳健上行(无大幅回撤) · 低ATR慢牛 · 放量跌停观察。与牛股雷达互补共振。';
    if (!rep.length) {
      h += card('🎯 经典策略库', '<div class="empty">今日无经典策略信号（市场偏冷或风格不匹配，建议等待）</div>', desc);
    } else {
    var rows = rep.map(function (it) {
      var sig = (it.signals || []).map(function (s) { return sigBadge(s); }).join(' ');
      return '<tr>' +
        '<td>' + stk(it.code, it.name) + '</td>' +
        '<td>' + sig + '</td>' +
        '<td class="num">' + f(it.score) + '</td>' +
        '<td class="num">' + (it.price != null ? f(it.price) : '—') + '</td>' +
        '<td class="num ' + ((it.pct || 0) >= 0 ? 'up' : 'down') + '">' + (it.pct != null ? sign(it.pct) : '—') + '</td>' +
        '<td class="num">' + (it.vol_ratio ? f(it.vol_ratio, 1) : '—') + '</td>' +
        '<td>' + E(it.ind || '') + '</td>' +
        '<td class="muted">' + E(it.tags || '') + '</td>' +
        '</tr>';
    }).join('');
    var cols = [
      { t: '个股' }, { t: '命中策略' }, { t: '评分', a: 'num' }, { t: '现价', a: 'num' },
      { t: '涨跌幅', a: 'num' }, { t: '量比', a: 'num' }, { t: '行业' }, { t: '说明' }
    ];
    h += card('🎯 经典策略库 · Top' + rep.length, table(cols, rows), desc);
    }
    /* -- 策略历史回测（近25交易日逐日重放） -- */
    var bt = D.strategy_bt || [];
    if (bt.length) {
      var btDesc = '对 9 类策略在近 25 个交易日做逐日重放：信号触发后次日 / 3日的胜率与平均收益（真实前向价计算）。样本<5 标记为「低」，仅供参考。';
      var btRows = bt.map(function (b) {
        var wr1 = b.n ? Math.round(b.win1 * 100 / b.n) : 0;
        var wr3 = b.n ? Math.round(b.win3 * 100 / b.n) : 0;
        return '<tr>' +
          '<td>' + sigBadge(b.signal) + '</td>' +
          '<td class="num">' + n2(b.n) + (b.low ? ' <span style="color:#94a3b8;font-size:12px">低</span>' : '') + '</td>' +
          '<td class="num ' + (wr1 >= 50 ? 'up' : 'down') + '">' + wr1 + '%</td>' +
          '<td class="num ' + ((b.avg1 || 0) >= 0 ? 'up' : 'down') + '">' + sign(b.avg1 * 100, 2) + '%</td>' +
          '<td class="num ' + (wr3 >= 50 ? 'up' : 'down') + '">' + wr3 + '%</td>' +
          '<td class="num ' + ((b.avg3 || 0) >= 0 ? 'up' : 'down') + '">' + sign(b.avg3 * 100, 2) + '%</td>' +
          '</tr>';
      }).join('');
      h += card('📊 策略历史回测 · 近25交易日', table(
        [{ t: '策略' }, { t: '样本', a: 'num' }, { t: '次日胜率', a: 'num' }, { t: '次日均收', a: 'num' }, { t: '3日胜率', a: 'num' }, { t: '3日均收', a: 'num' }],
        btRows), btDesc);
    }
    /* -- K线组合形态（今日识别） -- */
    var cd = D.candles || {};
    var cstats = cd.stats || [];
    var chits = cd.hits || [];
    if (cstats.length) {
      var dirC = function (d) { return d === 'bull' ? '#ef4444' : (d === 'bear' ? '#22c55e' : '#64748b'); };
      var dirL = { bull: '看涨', bear: '看跌', neutral: '中性' };
      var chips = cstats.map(function (s) {
        return '<span style="display:inline-flex;align-items:center;gap:5px;margin:2px;padding:4px 10px;border-radius:14px;border:1px solid ' + dirC(s.direction) + '55;color:' + dirC(s.direction) + ';font-weight:600">' +
          E(s.pattern) + '<b>' + n2(s.n) + '</b><span style="opacity:.7;font-size:11px">' + dirL[s.direction] + '</span></span>';
      }).join('');
      var cdDesc = '基于日K识别 12 种经典蜡烛图形态（锤头/上吊/吞没/晨星/暮星/红三兵/三乌鸦/刺透/乌云盖顶/十字星系）。A股惯例：红=看涨、绿=看跌。';
      var chRows = chits.slice(0, 20).map(function (x) {
        return '<tr>' +
          '<td>' + stk(x.code, x.name) + '</td>' +
          '<td style="color:' + dirC(x.direction) + ';font-weight:600">' + E(x.pattern) + '</td>' +
          '<td>' + (dirL[x.direction] || '—') + '</td>' +
          '<td class="num">' + f(x.close) + '</td>' +
          '<td class="num ' + ((x.pct || 0) >= 0 ? 'up' : 'down') + '">' + sign(x.pct) + '</td>' +
          '</tr>';
      }).join('');
      var cdBody = '<div style="margin-bottom:10px;line-height:2.2">' + chips + '</div>';
      if (chRows) {
        cdBody += table([{ t: '个股' }, { t: '形态' }, { t: '方向' }, { t: '收盘', a: 'num' }, { t: '涨跌幅', a: 'num' }], chRows) +
          (chits.length > 20 ? '<div class="muted" style="margin-top:6px;font-size:12px">仅展示前 20 条，共 ' + chits.length + ' 条命中</div>' : '');
      }
      h += card('🕯 K线形态 · 今日识别', cdBody, cdDesc);
    }
    /* -- 筹码获利盘（近似估计） -- */
    var cp = D.chips || {};
    if (cp.n) {
      var avgPct = Math.round((cp.avg || 0) * 100);
      var cpDesc = '近120日成交量按换手半衰期加权分布到价格区间估算获利盘比例（一阶近似，仅供参考）。获利盘低=套牢沉重/超跌，高=注意兑现风险。';
      var cpRow = function (x, hot) {
        return '<tr>' +
          '<td>' + stk(x.code, x.name) + '</td>' +
          '<td class="num" style="color:' + (hot ? '#ef4444' : '#22c55e') + ';font-weight:700">' + Math.round(x.ratio * 100) + '%</td>' +
          '<td class="num">' + f(x.close) + '</td>' +
          '<td class="num ' + ((x.pct || 0) >= 0 ? 'up' : 'down') + '">' + sign(x.pct) + '</td>' +
          '</tr>';
      };
      var lo = (cp.top_low || []).map(function (x) { return cpRow(x, false); }).join('');
      var hi2 = (cp.top_high || []).map(function (x) { return cpRow(x, true); }).join('');
      var colsCp = [{ t: '个股' }, { t: '获利盘', a: 'num' }, { t: '收盘', a: 'num' }, { t: '涨跌幅', a: 'num' }];
      var cpBody = kpi('全市场平均获利盘', avgPct + '%', '基于 ' + n2(cp.n) + ' 只有效样本', '') +
        '<div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:12px"><div style="flex:1;min-width:260px">' +
        '<div style="font-weight:700;margin-bottom:4px;color:#22c55e">🔻 获利盘最低（超跌/套牢区）</div>' + table(colsCp, lo) + '</div>' +
        '<div style="flex:1;min-width:260px">' +
        '<div style="font-weight:700;margin-bottom:4px;color:#ef4444">🔺 获利盘最高（兑现风险区）</div>' + table(colsCp, hi2) + '</div></div>';
      h += card('🪙 筹码分布 · 获利盘', cpBody, cpDesc);
    }
    return h;
  }

  /* ---------------- 持股监测 ---------------- */
  function gradeBadge(g) {
    var m = { A: '#22c55e', B: '#3b82f6', C: '#f59e0b', D: '#ef4444' };
    var lbl = { A: 'A 继续持有', B: 'B 持有观察', C: 'C 减仓/止盈', D: 'D 止损离场' };
    var c = m[g] || '#64748b';
    return '<span style="display:inline-flex;align-items:center;padding:3px 10px;border-radius:10px;font-weight:700;color:#fff;background:' + c + '">' + (lbl[g] || E(g)) + '</span>';
  }
  function viewHoldings() {
    var H = D.holdings;
    var h = '';
    var desc = '预测未来涨跌概率 + 持续跟踪：每日自动给出评级（A继续/B观察/C减仓/D止损）、目标位/止损位、次日上涨概率，并在评级恶化时预警。';
    if (!H || !H.enabled) {
      h += card('📡 持股监测', '<div class="empty">未配置持仓。点击「✎ 编辑持仓」添加你关注的股票（可纯观察、可不填成本）。</div>', desc);
      h += '<div class="sa-mgmt-actions" style="margin-top:12px"><button class="mbtn mbtn-p he-edit-btn">✎ 编辑持仓</button>' +
        '<span class="muted">次日上涨概率来自历史同状态实测，非预测承诺</span></div>';
      return h;
    }
    h += '<div class="grid g4" style="margin-bottom:14px">' +
      kpi('持仓', n2(H.n_held) + ' 只', (H.n_watch ? ('+关注 ' + H.n_watch) : '纯持仓'), '') +
      kpi('加权浮动', (H.pnl_pct_weighted != null ? sign(H.pnl_pct_weighted) : '—'), '组合整体浮盈', '') +
      kpi('浮动盈亏', (H.total_pnl != null ? ((H.total_pnl >= 0 ? '+' : '') + Math.round(H.total_pnl)) : '—'), '按持股金额加权', '') +
      kpi('需关注', n2((H.need_action || []).length), '评级 C/D 的标的', ((H.need_action || []).length ? 'down' : '')) +
      '</div>';
    if (H.alerts && H.alerts.length) {
      h += card('🔔 评级异动预警', '<div class="note" style="color:' + C.danger + '">' + H.alerts.map(E).join('<br>') + '</div>', '相较上一交易日评级下降即触发');
    }
    var items = H.items || [];
    var cards = items.map(function (d) {
      if (!d.ok) return '<div class="card"><div class="body"><b>' + stk(d.code, d.name) + '</b>：' + E(d.msg || '无数据') + '</div></div>';
      var pred = (d.p_up1 != null) ? ('次日上涨概率 <b style="color:' + (d.p_up1 >= 0.5 ? C.up : C.down) + '">' + Math.round(d.p_up1 * 100) + '%</b>（平均' + (d.r1 >= 0 ? '+' : '') + f(d.r1) + '%）') : '';
      var tgt = (d.target != null) ? ('目标 ' + f(d.target) + '（' + (d.target_pct >= 0 ? '+' : '') + f(d.target_pct) + '%）') : '';
      var stp = (d.stop != null) ? ('止损 ' + f(d.stop) + '（' + (d.stop_pct >= 0 ? '+' : '') + f(d.stop_pct) + '%）') : '';
      var risks = (d.risks && d.risks.length) ? d.risks.map(function (r) { return '<span class="bd" style="border-color:' + C.danger + ';color:' + C.danger + '">' + E(r) + '</span>'; }).join(' ') : '<span class="muted">无</span>';
      var plus = (d.plus && d.plus.length) ? d.plus.map(function (r) { return '<span class="bd" style="border-color:' + C.up + ';color:' + C.up + '">' + E(r) + '</span>'; }).join(' ') : '';
      var pnl = (d.pnl_pct != null) ? ('<span class="' + (d.pnl_pct >= 0 ? 'up' : 'down') + '">' + (d.pnl_pct >= 0 ? '+' : '') + f(d.pnl_pct) + '%</span>') : '<span class="muted">观察仓</span>';
      return '<div class="card"><div class="body">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
        '<div>' + stk(d.code, d.name) + (d.watch ? ' <span class="tag">观察</span>' : '') + '</div>' + gradeBadge(d.grade) +
        '</div>' +
        '<div class="chips" style="margin-bottom:8px">' +
        '<span class="chip">现价 ' + f(d.price) + '</span>' +
        '<span class="chip" style="border-color:' + (d.pct >= 0 ? C.up : C.down) + ';color:' + (d.pct >= 0 ? C.up : C.down) + '">今 ' + (d.pct >= 0 ? '+' : '') + f(d.pct) + '%</span>' +
        '<span class="chip">浮盈 ' + pnl + '</span>' +
        '<span class="chip">趋势 ' + E(d.trend) + '</span>' +
        '</div>' +
        (pred ? '<div class="kv" style="margin:4px 0">' + pred + '</div>' : '') +
        '<div class="kv" style="margin:4px 0">' + tgt + ' ｜ ' + stp + '</div>' +
        '<div style="margin:6px 0"><b style="color:var(--muted);font-size:12px">风险</b> ' + risks + '</div>' +
        (plus ? '<div style="margin:6px 0"><b style="color:var(--muted);font-size:12px">亮点</b> ' + plus + '</div>' : '') +
        ((d.signals && d.signals.length) ? '<div style="margin:6px 0"><b style="color:var(--muted);font-size:12px">信号</b> ' + d.signals.map(sigBadge).join(' ') + '</div>' : '') +
        '<div class="note" style="margin-top:6px">动作：<b>' + E(d.action) + '</b> · ' + E(d.why || '') + '</div>' +
        '</div></div>';
    }).join('');
    h += cards;
    h += '<div class="sa-mgmt-actions" style="margin-top:12px"><button class="mbtn mbtn-p he-edit-btn">✎ 编辑持仓</button>' +
      '<span class="muted">次日上涨概率来自历史同状态实测，非预测承诺</span></div>';
    return h;
  }

  /* ---------------- 持仓编辑器：写回 HOLDINGS_JSON 密钥 ---------------- */
  var HE_POS = [];
  function heNote(m, t) { var n = document.getElementById('heNote'); if (n) { n.textContent = m; n.className = 'sa-mgmt-note ' + (t || ''); } }
  function openHoldingsEditor() {
    ensureMgmtStyle();
    var cur = (D.holdings && D.holdings.items) ? D.holdings.items : [];
    HE_POS = cur.map(function (d) {
      return { code: d.code, name: d.name, cost: (d.cost != null ? d.cost : ''),
        shares: (d.shares != null ? d.shares : ''), watch: !!d.watch, note: (d.note || '') };
    });
    if (!HE_POS.length) {
      HE_POS.push({ code: '', name: '', cost: '', shares: '', watch: true, note: '' });
    }
    var ov = document.createElement('div'); ov.className = 'sa-mgmt-ov'; ov.id = 'heOv';
    ov.innerHTML = '<div class="sa-mgmt"><div class="sa-mgmt-h"><span>✎ 编辑持仓（预测 + 持续监测）</span>' +
      '<span class="sa-mgmt-x" onclick="var o=this.closest(\'.sa-mgmt-ov\');if(o)o.remove()">✕</span></div>' +
      '<div class="sa-mgmt-b" id="heBody"></div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function (e) { if (e.target === ov) ov.parentNode.removeChild(ov); });
    heRender();
  }
  function heRender() {
    var b = document.getElementById('heBody'); if (!b) return;
    var rows = HE_POS.map(function (p, i) {
      return '<tr>' +
        '<td><input class="mu-in" style="width:80px" data-i="' + i + '" data-k="code" value="' + E(p.code) + '"></td>' +
        '<td><input class="mu-in" style="width:90px" data-i="' + i + '" data-k="name" value="' + E(p.name) + '"></td>' +
        '<td><input class="mu-in" style="width:76px" data-i="' + i + '" data-k="cost" value="' + E(p.cost) + '" placeholder="观察留空"></td>' +
        '<td><input class="mu-in" style="width:70px" data-i="' + i + '" data-k="shares" value="' + E(p.shares) + '"></td>' +
        '<td style="text-align:center"><input type="checkbox" data-i="' + i + '" data-k="watch" ' + (p.watch ? 'checked' : '') + '></td>' +
        '<td><input class="mu-in" style="width:90px" data-i="' + i + '" data-k="note" value="' + E(p.note) + '"></td>' +
        '<td><button class="mbtn mbtn-d" data-rm="' + i + '">删</button></td>' +
        '</tr>';
    }).join('');
    b.innerHTML = '<div class="sa-mgmt-note info" style="margin-bottom:10px">此处添加的票会进入「持股监测」：每日自动给出评级、目标/止损位、次日上涨概率，并在评级恶化时预警。纯观察可不填成本。改动需 GitHub 令牌（仅本机会话保存），保存后写回密钥并触发重建。</div>' +
      '<div class="sa-mgmt-add">' +
      '<input id="heNewCode" placeholder="代码 如 600396" style="width:120px">' +
      '<input id="heNewName" placeholder="名称(可选)" style="width:120px">' +
      '<button class="mbtn mbtn-p" id="heAddBtn">+ 添加</button></div>' +
      (HE_POS.length ? '<table class="sa-mgmt-t"><thead><tr><th>代码</th><th>名称</th><th>成本</th><th>股数</th><th>观察</th><th>备注</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>'
        : '<div class="sa-mgmt-empty">还没有持仓，添加一只试试。</div>') +
      '<div class="sa-mgmt-actions">' +
      '<button class="mbtn mbtn-p" id="heSaveBtn">保存并部署</button>' +
      '<button class="mbtn mbtn-ghost" id="heCancelBtn">取消</button></div>' +
      '<div class="sa-mgmt-note" id="heNote"></div>';
    b.querySelectorAll('input[data-i]').forEach(function (inp) {
      inp.addEventListener('input', function () {
        var i = +inp.dataset.i, k = inp.dataset.k;
        if (k === 'watch') { HE_POS[i].watch = inp.checked; return; }
        HE_POS[i][k] = inp.value.trim();
      });
      inp.addEventListener('change', function () {
        var i = +inp.dataset.i, k = inp.dataset.k;
        if (k === 'watch') HE_POS[i].watch = inp.checked;
      });
    });
    b.querySelectorAll('button[data-rm]').forEach(function (btn) {
      btn.addEventListener('click', function () { HE_POS.splice(+btn.dataset.rm, 1); heRender(); });
    });
    var add = document.getElementById('heAddBtn');
    if (add) add.addEventListener('click', function () {
      var c = (document.getElementById('heNewCode').value || '').trim();
      var nm = (document.getElementById('heNewName').value || '').trim();
      if (!/^\d{6}$/.test(c)) { heNote('代码须为 6 位数字', 'err'); return; }
      HE_POS.push({ code: c, name: nm, cost: '', shares: '', watch: true, note: '' });
      heRender();
    });
    var save = document.getElementById('heSaveBtn');
    if (save) save.addEventListener('click', heSave);
    var cancel = document.getElementById('heCancelBtn');
    if (cancel) cancel.addEventListener('click', function () { var o = document.getElementById('heOv'); if (o) o.remove(); });
  }
  /* 持仓编辑器提交的规范载荷（本地/远程两条通道共用） */
  function hePayload() {
    return { positions: HE_POS.filter(function (p) { return /^\d{6}$/.test(p.code || ''); }).map(function (p) {
      return { code: p.code, name: p.name, cost: (p.cost === '' ? null : parseFloat(p.cost) || null),
        shares: (p.shares === '' ? null : parseFloat(p.shares) || null), watch: !!p.watch, note: p.note };
    }) };
  }
  function heSave() {
    var btn = document.getElementById('heSaveBtn');
    /* 本机模式：走本机 serve 的 API（它持令牌），浏览器零令牌 */
    if (MU.mode === 'local') {
      if (btn) btn.disabled = true;
      heNote('正在通过本机服务保存并部署持仓…', 'info');
      fetch('http://127.0.0.1:18789/api/save-holdings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(hePayload())
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok) throw new Error(d.error || '保存失败');
          heNote('已保存，正在写回云端密钥并触发重建…', 'info');
          return fetch('http://127.0.0.1:18789/api/deploy-holdings', { method: 'POST' }).then(function (r2) { return r2.json(); });
        })
        .then(function (d2) {
          if (btn) btn.disabled = false;
          if (d2 && d2.ok) heNote('已提交 ✅ 云端重建约 2 分钟，刷新即可看到监测结果', 'ok');
          else heNote('保存成功，但部署失败：' + ((d2 && d2.error) || '未知错误'), 'err');
        })
        .catch(function (e) {
          if (btn) btn.disabled = false;
          heNote('无法连接本机服务：' + ((e && e.message) || e) + '。请确认 manage_users.py serve 正在运行。', 'err');
        });
      return;
    }
    var haveKey = muWorkerUrl() ? MU.adminKey : muRecallToken();
    if (!haveKey) {
      var b = document.getElementById('heBody'); if (!b) return;
      if (document.getElementById('heTokIn')) { heNote(muWorkerUrl() ? '请先输入管理密钥' : '请先输入令牌', 'err'); return; }
      var gate = document.createElement('div');
      if (muWorkerUrl()) {
        gate.innerHTML = '<label class="mu-lb">管理密钥<span class="muted">（Worker 的 ADMIN_KEY，仅本机会话保存）</span></label>' +
          '<input class="mu-in" id="heTokIn" type="password" placeholder="ADMIN_KEY">' +
          '<label class="mu-ck"><input type="checkbox" id="heTokKeep">记住管理密钥</label>' +
          '<div class="sa-mgmt-hint">管理密钥是 Worker 环境变量里自定的密码，不是 GitHub 令牌。</div>';
      } else {
        gate.innerHTML = '<label class="mu-lb">GitHub 令牌<span class="muted">（repo 权限，仅本机会话保存）</span></label>' +
          '<input class="mu-in" id="heTokIn" type="password" placeholder="ghp_… / github_pat_…">' +
          '<label class="mu-ck"><input type="checkbox" id="heTokKeep">记住令牌</label>' +
          '<div class="sa-mgmt-hint">创建：github.com/settings/tokens/new?scopes=repo。令牌等于仓库钥匙，别外泄。</div>';
      }
      b.insertBefore(gate, b.firstChild);
      heNote(muWorkerUrl() ? '需要管理密钥才能写回持仓密钥（仅本机会话保存）' : '需要 GitHub 令牌才能写回持仓密钥（仅本机会话保存）', 'info');
      var go = document.getElementById('heSaveBtn');
      if (go) go.textContent = '确认保存';
      go.onclick = function () {
        var t = (document.getElementById('heTokIn').value || '').trim();
        if (!t) { heNote(muWorkerUrl() ? '请填写管理密钥' : '请粘贴令牌', 'err'); return; }
        if (muWorkerUrl()) {
          MU.adminKey = t;
          try { if (document.getElementById('heTokKeep').checked) localStorage.setItem(MU_ADMIN_KEY, t); else sessionStorage.setItem(MU_ADMIN_KEY, t); } catch (e2) {}
        } else {
          MU.token = t;
          try { if (document.getElementById('heTokKeep').checked) localStorage.setItem(MU_TOKEN_KEY, t); else sessionStorage.setItem(MU_TOKEN_KEY, t); } catch (e2) {}
        }
        gate.remove(); go.textContent = '保存并部署'; go.onclick = null;
        heSave();
      };
      return;
    }
    heNote('正在加密写回 GitHub 密钥…', 'info');
    var btn = document.getElementById('heSaveBtn'); if (btn) btn.disabled = true;
    muLoadNacl().then(function () {
      return muGetPubKey();
    }).then(function (pk) {
      if (!pk || !pk.key || !pk.key_id) throw new Error('未取得仓库公钥');
      var sealed = window.SA_SEAL(JSON.stringify(hePayload()), pk.key);
      return muSecretWrite('HOLDINGS_JSON', sealed, pk.key_id);
    }).then(function () {
      heNote('持仓已写入密钥，正在触发重建…', 'info');
      return muDispatchBuild();
    }).then(function () {
      if (btn) btn.disabled = false;
      heNote('已提交 ✅ 云端重建约 2 分钟，刷新即可看到监测结果', 'ok');
    }).catch(function (e) {
      if (btn) btn.disabled = false;
      heNote('失败：' + ((e && e.message) || e), 'err');
    });
  }

  /* ---------------- 关注股网页管理：写回 WATCH_JSON 密钥 ---------------- */
  var WL_POS = [];
  function wlOpen() {
    ensureMgmtStyle();
    var cur = (D.watch_meta && D.watch_meta.length) ? D.watch_meta : [];
    WL_POS = cur.map(function (d) { return { code: d.code, name: d.name || '' }; });
    if (!WL_POS.length) WL_POS.push({ code: '', name: '' });
    var ov = document.createElement('div'); ov.className = 'sa-mgmt-ov'; ov.id = 'wlOv';
    ov.innerHTML = '<div class="sa-mgmt"><div class="sa-mgmt-h"><span>⭐ 管理关注股（网页可编辑自选池）</span>' +
      '<span class="sa-mgmt-x" onclick="var o=this.closest(\'.sa-mgmt-ov\');if(o)o.remove()">✕</span></div>' +
      '<div class="sa-mgmt-b" id="wlBody"></div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function (e) { if (e.target === ov) ov.parentNode.removeChild(ov); });
    wlRender();
  }
  function wlRender() {
    var b = document.getElementById('wlBody'); if (!b) return;
    var rows = WL_POS.map(function (p, i) {
      return '<tr>' +
        '<td><input class="mu-in" style="width:90px" data-i="' + i + '" data-k="code" value="' + E(p.code) + '"></td>' +
        '<td><input class="mu-in" style="width:110px" data-i="' + i + '" data-k="name" value="' + E(p.name) + '"></td>' +
        '<td><button class="mbtn mbtn-d" data-rm="' + i + '">删</button></td>' +
        '</tr>';
    }).join('');
    b.innerHTML = '<div class="sa-mgmt-note info" style="margin-bottom:10px">这里的自选池会进入「关注股雷达」每日推送提醒。保存后加密写回云端密钥并触发重建，手机上也能随时管理。改动需管理密钥/令牌（仅本机会话保存）。</div>' +
      '<div class="sa-mgmt-add">' +
      '<input id="wlNewCode" placeholder="代码 如 600396" style="width:130px">' +
      '<input id="wlNewName" placeholder="名称(可选)" style="width:120px">' +
      '<button class="mbtn mbtn-p" id="wlAddBtn">+ 添加</button></div>' +
      (WL_POS.length ? '<table class="sa-mgmt-t"><thead><tr><th>代码</th><th>名称</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>'
        : '<div class="sa-mgmt-empty">还没有关注股，添加一只试试。</div>') +
      '<div class="sa-mgmt-actions">' +
      '<button class="mbtn mbtn-p" id="wlSaveBtn">保存并部署</button>' +
      '<button class="mbtn mbtn-ghost" id="wlCancelBtn">取消</button></div>' +
      '<div class="sa-mgmt-note" id="wlNote"></div>';
    b.querySelectorAll('input[data-i]').forEach(function (inp) {
      inp.addEventListener('input', function () { WL_POS[+inp.dataset.i][inp.dataset.k] = inp.value.trim(); });
    });
    b.querySelectorAll('button[data-rm]').forEach(function (btn) {
      btn.addEventListener('click', function () { WL_POS.splice(+btn.dataset.rm, 1); if (!WL_POS.length) WL_POS.push({ code: '', name: '' }); wlRender(); });
    });
    var add = document.getElementById('wlAddBtn');
    if (add) add.addEventListener('click', function () {
      var c = (document.getElementById('wlNewCode').value || '').trim();
      var nm = (document.getElementById('wlNewName').value || '').trim();
      if (!/^\d{6}$/.test(c)) { wlNote('代码须为 6 位数字', 'err'); return; }
      WL_POS.push({ code: c, name: nm }); wlRender();
    });
    var save = document.getElementById('wlSaveBtn'); if (save) save.addEventListener('click', wlSave);
    var cancel = document.getElementById('wlCancelBtn');
    if (cancel) cancel.addEventListener('click', function () { var o = document.getElementById('wlOv'); if (o) o.remove(); });
  }
  function wlNote(m, t) { var n = document.getElementById('wlNote'); if (n) { n.textContent = m; n.className = 'sa-mgmt-note ' + (t || ''); } }
  function wlPayload() {
    var out = [];
    WL_POS.forEach(function (p) { if (/^\d{6}$/.test(p.code || '')) out.push({ code: p.code, name: p.name || '' }); });
    return { watch: out };
  }
  function wlSave() {
    var haveKey = muWorkerUrl() ? MU.adminKey : muRecallToken();
    if (!haveKey) {
      var b = document.getElementById('wlBody'); if (!b) return;
      if (document.getElementById('wlTokIn')) { wlNote(muWorkerUrl() ? '请先输入管理密钥' : '请先输入令牌', 'err'); return; }
      var gate = document.createElement('div');
      if (muWorkerUrl()) {
        gate.innerHTML = '<label class="mu-lb">管理密钥<span class="muted">（Worker 的 ADMIN_KEY，仅本机会话保存）</span></label>' +
          '<input class="mu-in" id="wlTokIn" type="password" placeholder="ADMIN_KEY">' +
          '<label class="mu-ck"><input type="checkbox" id="wlTokKeep">记住管理密钥</label>';
      } else {
        gate.innerHTML = '<label class="mu-lb">GitHub 令牌<span class="muted">（repo 权限，仅本机会话保存）</span></label>' +
          '<input class="mu-in" id="wlTokIn" type="password" placeholder="ghp_… / github_pat_…">' +
          '<label class="mu-ck"><input type="checkbox" id="wlTokKeep">记住令牌</label>';
      }
      b.insertBefore(gate, b.firstChild);
      wlNote(muWorkerUrl() ? '需要管理密钥才能写回关注池密钥' : '需要 GitHub 令牌才能写回关注池密钥', 'info');
      var go = document.getElementById('wlSaveBtn');
      if (go) go.textContent = '确认保存';
      go.onclick = function () {
        var t = (document.getElementById('wlTokIn').value || '').trim();
        if (!t) { wlNote('请填写密钥', 'err'); return; }
        if (muWorkerUrl()) { MU.adminKey = t; try { if (document.getElementById('wlTokKeep').checked) localStorage.setItem(MU_ADMIN_KEY, t); else sessionStorage.setItem(MU_ADMIN_KEY, t); } catch (e2) {} }
        else { MU.token = t; try { if (document.getElementById('wlTokKeep').checked) localStorage.setItem(MU_TOKEN_KEY, t); else sessionStorage.setItem(MU_TOKEN_KEY, t); } catch (e2) {} }
        gate.remove(); go.textContent = '保存并部署'; go.onclick = null; wlSave();
      };
      return;
    }
    wlNote('正在加密写回 GitHub 密钥…', 'info');
    var btn = document.getElementById('wlSaveBtn'); if (btn) btn.disabled = true;
    muLoadNacl().then(function () { return muGetPubKey(); }).then(function (pk) {
      if (!pk || !pk.key || !pk.key_id) throw new Error('未取得仓库公钥');
      var sealed = window.SA_SEAL(JSON.stringify(wlPayload()), pk.key);
      return muSecretWrite('WATCH_JSON', sealed, pk.key_id);
    }).then(function () { wlNote('关注池已写入密钥，正在触发重建…', 'info'); return muDispatchBuild(); }).then(function () {
      if (btn) btn.disabled = false; wlNote('已提交 ✅ 云端重建约 2 分钟，刷新即可看到更新', 'ok');
    }).catch(function (e) { if (btn) btn.disabled = false; wlNote('失败：' + ((e && e.message) || e), 'err'); });
  }

  /* ---------------- 启动 ---------------- */
  function boot() {
    if (!D) {
      document.querySelector('.wrap').innerHTML =
        '<div class="card"><div class="body"><div class="empty">未找到 data.js —— 请先运行 <code>python pipeline/fetch.py</code> 与 <code>python pipeline/build.py</code></div></div></div>';
      return;
    }
    /* 主题初始化（默认科技深色，可切换浅色并持久化） */
    var savedTheme = 'tech';
    try { savedTheme = localStorage.getItem('sa_theme') || 'tech'; } catch (e) {}
    if (document.documentElement) document.documentElement.setAttribute('data-theme', savedTheme);
    var tb = document.getElementById('themeBtn');
    if (tb) tb.addEventListener('click', function () {
      var cur = (document.documentElement && document.documentElement.getAttribute('data-theme')) || 'tech';
      var nx = cur === 'tech' ? 'light' : 'tech';
      if (document.documentElement) document.documentElement.setAttribute('data-theme', nx);
      try { localStorage.setItem('sa_theme', nx); } catch (e) {}
      /* 重新渲染当前视图，使 SVG 图表按新主题解析真实配色 */
      var curView = 'overview';
      try {
        [].forEach.call(document.querySelectorAll('#tabs button'), function (b) {
          if (b.classList.contains('on')) curView = b.dataset.v;
        });
      } catch (e2) {}
      done = {};
      show(curView);
    });
    head();
    /* owner 专属：站点顶栏「管理用户」入口，对接本机 serve 服务 */
    if (window.__SA_USER__ === 'owner') {
      var _tb = document.getElementById('themeBtn');
      if (_tb) {
        var _mb = document.createElement('button');
        _mb.type = 'button'; _mb.className = 'theme-btn'; _mb.textContent = '⚙ 管理用户';
        _mb.addEventListener('click', openUserMgr);
        _tb.parentNode.insertBefore(_mb, _tb);
      }
    }
    initBackdrop();
    startFreshnessWatch();
    /* owner 专属：总览页「管理关注股」入口（动态渲染的按钮用委托） */
    document.addEventListener('click', function (e) {
      if (e.target && e.target.id === 'wlMgrBtn') { e.preventDefault(); wlOpen(); }
    });
    var views = { overview: viewOverview, ladder: viewLadder, sectors: viewSectors, risk: viewRisk, demon: viewDemon, yaogu: viewYaogu, overlap: viewOverlap, rec: viewRec, auction: viewAuction, bull: viewBull, strategies: viewStrategies, holdings: viewHoldings };
    var done = {};
    function show(k) {
      var el = document.getElementById('v-' + k);
      if (!el) { console.warn('[show] 缺少视图容器 #v-' + k); return; }
      if (!done[k]) {
        try {
          el.innerHTML = views[k]();
          initCountUp(el);
        }
        catch (e) {
          el.innerHTML = '<div class="card"><div class="body"><div class="empty">渲染出错：' + E(e.message) + '</div></div></div>';
        }
        done[k] = 1;
      }
      /* 切换视图：仅对存在的容器切换 .on，避免缺节点时整段崩溃 */
      Object.keys(views).forEach(function (x) {
        var vx = document.getElementById('v-' + x);
        if (vx) vx.classList.toggle('on', x === k);
      });
      [].forEach.call(document.querySelectorAll('#tabs button'), function (b) {
        b.classList.toggle('on', b.dataset.v === k);
      });
      /* 重新触发淡入动画，使每次切换都有顺滑过渡 */
      el.classList.remove('view-in'); void el.offsetWidth; el.classList.add('view-in');
      moveTabInd();
      window.scrollTo(0, 0);
      if (location.hash.slice(1) !== k) history.replaceState(null, '', '#' + k);
    }
    /* 滑动指示条：跟随激活 tab 平滑移动 */
    var tabsNav = document.getElementById('tabs');
    var tabInd = document.createElement('span'); tabInd.className = 'tab-ind';
    if (tabsNav) tabsNav.appendChild(tabInd);
    function moveTabInd() {
      var b = tabsNav && tabsNav.querySelector('button.on');
      if (b && tabInd) { tabInd.style.width = b.offsetWidth + 'px'; tabInd.style.transform = 'translateX(' + b.offsetLeft + 'px)'; }
    }
    /* 全局点击：个股名 → 日K线；竞价形态 chip → 该形态个股清单 */
    document.addEventListener('click', function (e) {
      var t = e.target; if (!t || !t.closest) return;
      var st = t.closest('.stk');
      if (st && st.dataset && st.dataset.code) { e.preventDefault(); openKline(st.dataset.code, st.dataset.name); return; }
      var oc = t.closest('.ov-chip');
      if (oc && oc.dataset && oc.dataset.kind) { applyOverlapFilter(oc.dataset.kind, oc.dataset.v); return; }
      var pc = t.closest('.pat-chip');
      if (pc && pc.dataset && pc.dataset.p) { openPatternList(pc.dataset.p); return; }
      var ed = t.closest('.he-edit-btn');
      if (ed) { openHoldingsEditor(); return; }
    });
    document.getElementById('tabs').addEventListener('click', function (e) {
      if (e.target.dataset && e.target.dataset.v) show(e.target.dataset.v);
    });
    window.addEventListener('resize', moveTabInd);
    window.addEventListener('load', moveTabInd);
    /* 回到顶部按钮：滚过一屏浮现，点击平滑回顶 */
    var toTop = document.getElementById('toTop');
    if (toTop) {
      window.addEventListener('scroll', function () {
        toTop.classList.toggle('show', (window.scrollY || document.documentElement.scrollTop || 0) > 420);
      }, { passive: true });
      toTop.addEventListener('click', function () {
        try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch (e) { window.scrollTo(0, 0); }
      });
    }
    /* 键盘快捷切换视图：1-9 / 0 / - / = 对应 12 个 tab（输入框聚焦时忽略） */
    document.addEventListener('keydown', function (e) {
      if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
      var tg = e.target;
      if (tg && (tg.tagName === 'INPUT' || tg.tagName === 'TEXTAREA' || tg.isContentEditable)) return;
      var order = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0', '-', '='];
      var keys = Object.keys(views);
      var i = order.indexOf(e.key);
      if (i >= 0 && keys[i]) show(keys[i]);
    });
    show(views[location.hash.slice(1)] ? location.hash.slice(1) : 'overview');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
  /* PWA：注册 Service Worker（离线壳 + 数据网络优先） */
  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    try { navigator.serviceWorker.register('sw.js'); } catch (e) {}
  }
})();
