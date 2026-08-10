/* 渲染层：把 window.__STOCK_DATA__ 铺成六个视图。零依赖。 */
(function () {
  'use strict';
  var D = window.__STOCK_DATA__;
  var E = CH.esc, C = CH.COLORS;

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
    var h = '<div class="tbl-wrap' + (opts.scroll ? ' scroll-y' : '') + '"><table><thead><tr>' +
      cols.map(function (c) { return '<th class="' + (c.a || '') + '">' + c.t + '</th>'; }).join('') +
      '</tr></thead><tbody>';
    if (!rows.length) return '<div class="empty">' + (opts.empty || '当日无符合条件的标的') + '</div>';
    h += rows.join('');
    return h + '</tbody></table></div>';
  }
  function seg(v, lo, hi) { return Math.max(0, Math.min(100, (v - lo) / (hi - lo) * 100)); }

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
    var _gen = m.generated_at ? new Date(m.generated_at.replace(/-/g, '/').replace(' ', 'T')) : null;
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
      if (days >= 2) fresh = '<span class="fresh stale">⚠ 数据已过期 ' + days + ' 天 · 请运行 update.bat 更新</span>';
      else fresh = '<span class="fresh ok">✓ 数据新鲜 · 收盘后已更新' +
        (_gen && !isNaN(_gen.getTime()) ? '（生成于 ' + _rel(_gen) + '）' : '') + '</span>';
    }
    document.getElementById('dateline').innerHTML = s + fresh;
    document.getElementById('foot').innerHTML =
      '数据源：' + E(m.source || '公开行情接口') + '（全部为当日收盘后数据，无盘中实时成分）<br>' +
      '生成于 ' + E(m.generated_at) + '，构建耗时 ' + n2(m.build_seconds) + ' 秒 · 行情库覆盖 ' +
      n2(m.universe) + ' 只个股 / ' + n2(m.trade_days) + ' 个交易日<br>' +
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
        (G.jp_pct !== null && G.jp_pct !== undefined ? '<span class="chip">日经 <b>' + (G.jp_pct >= 0 ? '+' : '') + f(G.jp_pct, 2) + '%</b></span>' : '') +
        (G.kr_pct !== null && G.kr_pct !== undefined ? '<span class="chip">韩国 <b>' + (G.kr_pct >= 0 ? '+' : '') + f(G.kr_pct, 2) + '%</b></span>' : '') +
        '</div>';
      if (G.indices && G.indices.length) {
        gbody += '<div class="tbl-wrap"><table><thead><tr><th>市场</th><th>指数</th><th class="r">涨跌幅</th></tr></thead><tbody>' +
          G.indices.map(function (x) {
            return '<tr><td class="muted">' + E(x.region || '') + '</td><td class="name">' + E(x.name || x.code || '') +
              '</td><td class="r num ' + (x.pct >= 0 ? 'up' : 'down') + '">' + (x.pct >= 0 ? '+' : '') + f(x.pct, 2) + '%</td></tr>';
          }).join('') + '</tbody></table></div>';
      }
      gbody += '<div class="note">' + E(gdetail || '外围数据缺失，按中性处理') + '</div>';
      h += card('🌐 外围市场定调（美股/日股/韩股 → A股次日）', gbody,
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
      auction: '⚡ 竞价强度确认', anomaly: '🚨 盘中异动提醒'
    };
    function blk(mode, label) {
      var p = LP[mode];
      if (!p) return '';
      return '<div style="margin-top:10px"><b>' + label + '</b> <span class="faint">' + E(p.ts || '') + '</span></div>' +
             '<div style="font-size:13px;line-height:1.65">' + md2html(p.text || '') + '</div>';
    }
    var modes = ['close', 'preauction', 'auction', 'anomaly'].filter(function (m) { return LP[m]; });
    var inner = modes.length
      ? modes.map(function (m) { return blk(m, LABELS[m] || m); }).join('')
      : '<div class="m">尚未生成推送。配置 config/notify.json 的微信/Telegram/邮件后，每日 <b>收盘后(16:10)</b> 与 <b>竞价前(9:00)</b> 自动推送；竞价强度确认与盘中异动可随时触发；即便未配置通道，此处也会留存最近一次推送内容。</div>';
    return card('📨 消息推送记录', '<div>' + inner +
      '</div><div class="m" style="margin-top:10px;color:var(--muted)">通道配置：config/notify.json（微信优先：企业微信群机器人 / ServerChan / PushPlus；亦支持 Telegram、SMTP 邮件）</div>',
      '每日推送：收盘后复盘 + 竞价前观察；竞价确认与盘中异动可随时触发');
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
          '<b>' + E(x.name) + '</b><span class="q">' + f(x.quality, 0) + '</span>' +
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
      return '<tr><td class="code">' + E(x.code) + '</td><td class="name">' + E(x.name) +
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
            return '<span class="chip">' + E(t.name) + ' <b>' + t.streak + '板</b></span>';
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
      return '<tr><td class="code">' + E(r.code) + '</td><td class="name">' + E(r.name) + '</td>' +
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
      return '<div class="card"><h3>' + E(d.name) + ' <span class="bd lb' + Math.min(6, d.streak) + '">' + d.streak +
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
      return '<tr><td class="code">' + E(d.code) + '</td><td class="name">' + E(d.name) + '</td>' +
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

  /* ============ 视图 6：当日推荐 ============ */
  function viewRec() {
    var R = D.recommend || {}, st = (D.market || {}).sentiment || {}, cy = (D.market || {}).cycle || {};
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
        var kvHtml = t
          ? '<div class="kv">'
            + '<span>收盘 <b>' + f(it.close) + '</b></span>'
            + '<span>行业 <b>' + E(it.industry || '—') + '</b></span>'
            + '<span>MA5 <b>' + f(t.ma5) + '</b></span>'
            + '<span>MA10 <b>' + f(t.ma10) + '</b></span>'
            + '<span>MA20 <b>' + f(t.ma20) + '</b></span>'
            + '<span>近5日 <b>' + (t.up_days) + '涨</b></span>'
            + '<span>偏离MA20 <b style="color:' + (t.momentum_pct >= 0 ? C.up : C.gold) + '">+' + f(t.momentum_pct, 1) + '%</b></span>'
            + '<span>量能 <b>' + f(t.vol_ratio, 1) + '倍</b></span>'
            + '<span>趋势分 <b style="color:' + scol + '">' + f(it.score, 1) + '</b></span>'
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
          '<span class="nm">' + E(it.name) + '</span><span class="code faint">' + E(it.code) + '</span>' +
          lbBadge(it.streak) + tierBadge(it.sector_tier) + vaBadge + hcBadge +
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
      '均线多头排列（MA5>MA10>MA20）且非涨停的趋势票，主升段低吸候选');

    /* 全量评分表 */
    var rows = (R.all || []).map(function (it, i) {
      var wcol = it.worth_score >= 60 ? C.up : it.worth_score >= 45 ? C.gold : C.gray;
      return '<tr><td class="faint">' + (i + 1) + '</td><td class="code">' + E(it.code) + '</td>' +
        '<td class="name">' + E(it.name) + qBadge(it) + '</td><td class="c">' + lbBadge(it.streak) + '</td>' +
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
      return '<tr><td class="code">' + E(a.code) + '</td><td class="name">' + E(a.name) + '</td>' +
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
        return '<div class="rec ' + cls + '"><div class="rh"><span class="nm">' + E(a.name) + '</span>' +
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
          return '<span class="chip" style="border-color:' + C.up + ';color:' + C.up + '">' + E(a.name) + ' <b>' + a.streak + '板</b></span>';
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
        return '<tr><td class="name">' + E(a.name) + '</td><td class="code">' + E(a.code) + '</td>' +
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

  /* ---------------- 用户管理（owner 专属入口，对接本机 serve 服务） ---------------- */
  var MU = { users: [], connected: false, overlay: null, noteEl: null };
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
      '.sa-mgmt .muted{color:#6b7a99;font-size:12px;}'
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
    var t = setTimeout(function () { ac.abort(); }, 3000);
    fetch('http://127.0.0.1:18789/api/users', { cache: 'no-store', signal: ac.signal })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) { clearTimeout(t); MU.users = (d && d.users) || []; MU.connected = true; muRender(); })
      .catch(function (e) { clearTimeout(t); MU.connected = false; muRenderOffline(e); });
  }
  function muRender() {
    var b = document.getElementById('saMgmtBody'); if (!b) return;
    var rows = MU.users.map(function (u, i) {
      var isOwner = u.id === 'owner';
      return '<tr>' +
        '<td class="id">' + saEsc(u.id) + '</td>' +
        '<td class="nm">' + saEsc(u.name) + (isOwner ? ' <span class="tag">管理员</span>' : '') + '</td>' +
        '<td class="pw">' + (isOwner ? '—' : '<code>' + saEsc(u.pass) + '</code>') + '</td>' +
        '<td class="ac">' +
        (isOwner ? '<span class="muted">不可删除</span>'
          : '<button class="mbtn mbtn-d" data-act="rm" data-i="' + i + '">删除</button>') +
        ' <button class="mbtn" data-act="cp" data-i="' + i + '">改口令</button>' +
        '</td></tr>';
    }).join('');
    b.innerHTML =
      '<div class="sa-mgmt-add"><input id="muNewName" placeholder="新用户名称，如：张三" maxlength="20">' +
      '<button class="mbtn mbtn-p" id="muAddBtn">+ 添加</button></div>' +
      (MU.users.length
        ? '<table class="sa-mgmt-t"><thead><tr><th>账户</th><th>名称</th><th>口令</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table>'
        : '<div class="sa-mgmt-empty">暂无其他用户。添加一个，把「账户名 + 口令」发给对方即可。</div>') +
      '<div class="sa-mgmt-actions">' +
      '<button class="mbtn mbtn-p" id="muDeployBtn">保存并部署</button>' +
      '<a class="mbtn mbtn-ghost" href="http://127.0.0.1:18789/" target="_blank" rel="noopener">在本地页面打开</a>' +
      '</div>' +
      '<div class="sa-mgmt-note" id="muNote"></div>' +
      '<div class="sa-mgmt-hint">修改后必须「保存并部署」才会生效（云端为每个用户重新生成加密数据）。</div>';
    MU.noteEl = document.getElementById('muNote');
    document.getElementById('muAddBtn').addEventListener('click', function () {
      var v = document.getElementById('muNewName').value.trim();
      if (!v) { muNote('请输入名称', 'err'); return; }
      muAdd(v);
    });
    document.getElementById('muDeployBtn').addEventListener('click', muSaveDeploy);
    b.querySelectorAll('button[data-act]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var i = parseInt(btn.dataset.i, 10), act = btn.dataset.act;
        if (act === 'rm') muRemove(i); else if (act === 'cp') muChgPass(i);
      });
    });
  }
  function muRenderOffline(e) {
    var b = document.getElementById('saMgmtBody'); if (!b) return;
    b.innerHTML = '<div class="sa-mgmt-off">' +
      '<p>未检测到本机管理服务。管理用户需在本机运行管理服务（它持有 GitHub 令牌，负责真正写入并部署）。</p>' +
      '<code class="cmd">python tools/manage_users.py serve</code>' +
      '<div class="sa-mgmt-actions">' +
      '<button class="mbtn mbtn-p" id="muCopy">复制命令</button>' +
      '<a class="mbtn mbtn-ghost" href="http://127.0.0.1:18789/" target="_blank" rel="noopener">打开本地管理页面</a>' +
      '</div>' +
      '<div class="sa-mgmt-note" id="muNote"></div>' +
      '<div class="sa-mgmt-hint">在项目目录运行上面命令后，刷新本页即可在此直接增删用户并一键部署。</div>' +
      '</div>';
    MU.noteEl = document.getElementById('muNote');
    document.getElementById('muCopy').addEventListener('click', function () {
      var cmd = 'python tools/manage_users.py serve';
      if (navigator.clipboard) navigator.clipboard.writeText(cmd).then(
        function () { muNote('已复制：' + cmd, 'ok'); },
        function () { muNote('复制失败，请手动复制：' + cmd, 'err'); });
      else muNote('请手动复制：' + cmd, 'info');
    });
  }
  function muAdd(name) {
    var base = (name.toLowerCase().replace(/[^a-z0-9一-龥]/g, '')).slice(0, 12) || ('user' + Date.now().toString(36));
    var id = base, c = 1;
    while (MU.users.some(function (u) { return u.id === id; })) id = base + (++c);
    var pass = muGenPass();
    MU.users.push({ id: id, name: name, pass: pass });
    muRender(); muNote('已添加「' + name + '」，口令：' + pass + '（保存并部署后生效）', 'ok');
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
    var views = { overview: viewOverview, ladder: viewLadder, sectors: viewSectors, risk: viewRisk, demon: viewDemon, rec: viewRec, auction: viewAuction };
    var done = {};
    function show(k) {
      if (!done[k]) {
        try {
          var el = document.getElementById('v-' + k);
          el.innerHTML = views[k]();
          initCountUp(el);
        }
        catch (e) { document.getElementById('v-' + k).innerHTML = '<div class="card"><div class="body"><div class="empty">渲染出错：' + E(e.message) + '</div></div></div>'; }
        done[k] = 1;
      }
      Object.keys(views).forEach(function (x) {
        document.getElementById('v-' + x).classList.toggle('on', x === k);
      });
      [].forEach.call(document.querySelectorAll('#tabs button'), function (b) {
        b.classList.toggle('on', b.dataset.v === k);
      });
      window.scrollTo(0, 0);
      if (location.hash.slice(1) !== k) history.replaceState(null, '', '#' + k);
    }
    document.getElementById('tabs').addEventListener('click', function (e) {
      if (e.target.dataset && e.target.dataset.v) show(e.target.dataset.v);
    });
    show(views[location.hash.slice(1)] ? location.hash.slice(1) : 'overview');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
