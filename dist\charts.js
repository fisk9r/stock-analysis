/* 纯 SVG 图表函数族 —— 零依赖、返回字符串、不碰 DOM
   配色遵循 A 股习惯：涨红 / 跌绿
   关键修复：SVG 表现属性（fill/stroke）不支持 var()，故在绘制时通过 getComputedStyle
   解析出主题真实色值并内联，保证浅色与科技深色两套主题下都清晰可读。 */
(function (g) {
  'use strict';

  /* 解析当前主题下的真实颜色；无 DOM（如 node 校验）时回退到通用可读色 */
  function gv(k) {
    try {
      if (typeof getComputedStyle === 'function' && document && document.documentElement) {
        var v = getComputedStyle(document.documentElement).getPropertyValue(k);
        if (v) return v.trim();
      }
    } catch (e) {}
    return '';
  }
  function pal() {
    return {
      up: gv('--up') || '#e03131', down: gv('--down') || '#2f9e44',
      accent: gv('--accent') || '#1864ab', accent2: gv('--accent-2') || '#1971c2',
      gold: gv('--gold') || '#a67c00', purple: gv('--purple') || '#5f3dc4',
      ok: gv('--ok') || '#2b8a3e', warn: gv('--warn') || '#e8590c',
      danger: gv('--danger') || '#c92a2a', teal: gv('--ok') || '#2b8a3e',
      muted: gv('--muted') || '#6b7280', text: gv('--text') || '#111827',
      text2: gv('--text-2') || '#374151', faint: gv('--faint') || '#9ca3af',
      border: gv('--border') || '#e5e7eb', border2: gv('--border-2') || '#eef1f5',
      card: gv('--card') || '#ffffff', grid: gv('--border-2') || '#eef1f5'
    };
  }
  /* 供 app.js 内联 style 使用：var() 在内联样式中可正常解析，且随主题切换自动更新 */
  var C = {
    up: 'var(--up)', down: 'var(--down)', blue: 'var(--accent)', blue2: 'var(--accent-2)',
    gold: 'var(--gold)', purple: 'var(--purple)', teal: 'var(--ok)', gray: 'var(--faint)',
    warn: 'var(--warn)', danger: 'var(--danger)',
    ok: 'var(--ok)', faint: 'var(--faint)', text: 'var(--text)', muted: 'var(--muted)',
    border: 'var(--border)', border2: 'var(--border-2)', card: 'var(--card)'
  };
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function nz(v, d) { return (v === null || v === undefined || isNaN(v)) ? (d || 0) : v; }

  /* 竖向柱状图 data=[{l,v,c}] */
  function svgBar(data, o) {
    o = o || {}; var P = pal();
    var W = o.w || 440, H = o.h || 190, pad = 28, padB = 30;
    if (!data.length) return '<div class="empty">无数据</div>';
    var max = Math.max(1, Math.max.apply(null, data.map(function (d) { return nz(d.v); })));
    var bw = (W - pad * 2) / data.length;
    var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:' + W + 'px;">';
    [0.5, 1].forEach(function (f) {
      var y = H - padB - f * (H - pad - padB);
      s += '<line x1="' + pad + '" y1="' + y.toFixed(1) + '" x2="' + (W - pad) + '" y2="' + y.toFixed(1) +
        '" stroke="' + P.grid + '" stroke-dasharray="3 3"/>';
    });
    data.forEach(function (d, i) {
      var v = nz(d.v), bh = (v / max) * (H - pad - padB), x = pad + i * bw, y = H - padB - bh;
      s += '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + Math.max(2, bw - 6).toFixed(1) +
        '" height="' + Math.max(0, bh).toFixed(1) + '" rx="3" fill="' + (d.c || P.accent) + '">' +
        '<title>' + esc(d.l) + ': ' + (o.fmt ? o.fmt(v) : v) + '</title></rect>';
      if (o.showVal !== false) {
        s += '<text x="' + (x + (bw - 6) / 2).toFixed(1) + '" y="' + (y - 4).toFixed(1) +
          '" font-size="10" text-anchor="middle" fill="' + P.muted + '">' + (o.fmt ? o.fmt(v) : v) + '</text>';
      }
      s += '<text x="' + (x + (bw - 6) / 2).toFixed(1) + '" y="' + (H - padB + 14) +
        '" font-size="10" text-anchor="middle" fill="' + P.muted + '">' + esc(d.l) + '</text>';
    });
    return s + '</svg>';
  }

  /* 横向条 data=[{l,v,c,sub}] —— Top N 排行 */
  function svgHBar(data, o) {
    o = o || {}; var P = pal();
    var W = o.w || 460, rowH = o.rowH || 24, padL = o.padL || 92, padR = 46;
    if (!data.length) return '<div class="empty">无数据</div>';
    var H = Math.max(40, data.length * rowH + 8);
    var max = Math.max(1, Math.max.apply(null, data.map(function (d) { return nz(d.v); })));
    var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:' + W + 'px;">';
    data.forEach(function (d, i) {
      var y = i * rowH + 5, bw = (nz(d.v) / max) * (W - padL - padR);
      s += '<text x="' + (padL - 7) + '" y="' + (y + rowH / 2 + 2) + '" font-size="11" text-anchor="end" fill="' + P.text2 + '">' + esc(d.l) + '</text>';
      s += '<rect x="' + padL + '" y="' + y + '" width="' + Math.max(1, bw).toFixed(1) + '" height="' + (rowH - 10) +
        '" rx="3" fill="' + (d.c || P.accent) + '"><title>' + esc(d.l) + ': ' + nz(d.v) + (d.sub ? ' · ' + esc(d.sub) : '') + '</title></rect>';
      s += '<text x="' + (padL + bw + 6).toFixed(1) + '" y="' + (y + rowH / 2 + 2) +
        '" font-size="10.5" fill="' + P.text + '">' + (o.fmt ? o.fmt(d.v) : d.v) + '</text>';
    });
    return s + '</svg>';
  }

  /* 折线（可双序列） points=[{l,v,v2}] */
  function svgLine(points, o) {
    o = o || {}; var P = pal();
    var W = o.w || 460, H = o.h || 190, pad = 34, padB = 26;
    if (points.length < 2) return '<div class="empty">数据不足</div>';
    var vals = points.map(function (p) { return nz(p.v); });
    if (o.dual) vals = vals.concat(points.map(function (p) { return nz(p.v2); }));
    var max = Math.max.apply(null, vals), min = Math.min.apply(null, vals);
    if (o.zeroBase) min = Math.min(0, min);
    var span = (max - min) || 1;
    max += span * 0.12; min -= span * 0.12; span = max - min;
    var step = (W - pad - 10) / (points.length - 1);
    function Y(v) { return H - padB - (nz(v) - min) / span * (H - pad - padB); }
    var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:' + W + 'px;">';
    [0, 0.5, 1].forEach(function (f) {
      var y = padB + f * (H - pad - padB), val = max - f * span;
      s += '<line x1="' + pad + '" y1="' + y.toFixed(1) + '" x2="' + (W - 10) + '" y2="' + y.toFixed(1) +
        '" stroke="' + P.grid + '"/>' +
        '<text x="' + (pad - 5) + '" y="' + (y + 3).toFixed(1) + '" font-size="9" text-anchor="end" fill="' + P.faint + '">' + val.toFixed(0) + '</text>';
    });
    if (min < 0 && max > 0) {
      s += '<line x1="' + pad + '" y1="' + Y(0).toFixed(1) + '" x2="' + (W - 10) + '" y2="' + Y(0).toFixed(1) + '" stroke="' + P.border + '" stroke-dasharray="4 3"/>';
    }
    function draw(key, color, fill) {
      var d = '', a = '';
      points.forEach(function (p, i) {
        var x = pad + i * step, y = Y(p[key]);
        d += (i === 0 ? 'M' : 'L') + x.toFixed(1) + ' ' + y.toFixed(1) + ' ';
      });
      if (fill) {
        a = d + 'L' + (pad + (points.length - 1) * step).toFixed(1) + ' ' + (H - padB) + ' L' + pad + ' ' + (H - padB) + ' Z';
        s += '<path d="' + a + '" fill="' + color + '" fill-opacity="0.10"/>';
      }
      s += '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round"/>';
      points.forEach(function (p, i) {
        if (points.length > 40 && i % 2) return;
        var x = pad + i * step, y = Y(p[key]);
        s += '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="2.6" fill="' + color + '">' +
          '<title>' + esc(p.l) + ': ' + nz(p[key]) + '</title></circle>';
      });
    }
    draw('v', o.color || P.up, o.fill !== false);
    if (o.dual) draw('v2', o.color2 || P.accent, false);
    var gap = Math.max(1, Math.ceil(points.length / 8));
    points.forEach(function (p, i) {
      if (i % gap === 0 || i === points.length - 1) {
        s += '<text x="' + (pad + i * step).toFixed(1) + '" y="' + (H - padB + 15) +
          '" font-size="9" text-anchor="middle" fill="' + P.faint + '">' + esc(p.l) + '</text>';
      }
    });
    s += '</svg>';
    if (o.legend) {
      s += '<div class="legend">' + o.legend.map(function (lg) {
        return '<span><i style="background:' + lg.c + '"></i>' + esc(lg.l) + '</span>';
      }).join('') + '</div>';
    }
    return s;
  }

  /* 环形图 data=[{l,v,c}] */
  function svgDonut(data, o) {
    o = o || {}; var P = pal();
    var W = o.w || 230, H = o.h || 230, cx = W / 2, cy = H / 2, R = Math.min(cx, cy) - 16;
    var sw = Math.max(14, Math.min(24, R * 0.5));
    var raw = data.reduce(function (s, d) { return s + nz(d.v); }, 0);
    var total = Math.max(1, raw), CIR = 2 * Math.PI * R, off = 0;
    var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:' + W + 'px;">';
    s += '<circle cx="' + cx + '" cy="' + cy + '" r="' + R + '" fill="none" stroke="' + P.grid + '" stroke-width="' + sw + '"/>';
    data.forEach(function (d) {
      var frac = nz(d.v) / total; if (frac <= 0) return;
      var len = frac * CIR;
      s += '<circle cx="' + cx + '" cy="' + cy + '" r="' + R + '" fill="none" stroke="' + (d.c || P.accent) +
        '" stroke-width="' + sw + '" stroke-dasharray="' + len.toFixed(2) + ' ' + (CIR - len).toFixed(2) +
        '" stroke-dashoffset="' + (-off).toFixed(2) + '" transform="rotate(-90 ' + cx + ' ' + cy + ')">' +
        '<title>' + esc(d.l) + ': ' + nz(d.v) + ' (' + Math.round(frac * 100) + '%)</title></circle>';
      off += len;
    });
    s += '<text x="' + cx + '" y="' + (cy - 4) + '" font-size="11" text-anchor="middle" fill="' + P.muted + '">' + esc(o.label || '合计') + '</text>' +
      '<text x="' + cx + '" y="' + (cy + 15) + '" font-size="17" font-weight="700" text-anchor="middle" fill="' + P.text + '">' + raw + '</text></svg>';
    s += '<div class="legend" style="justify-content:center">' + data.map(function (d) {
      return '<span><i style="background:' + (d.c || P.accent) + '"></i>' + esc(d.l) + ' ' + nz(d.v) + '</span>';
    }).join('') + '</div>';
    return s;
  }

  /* 雷达 axes=[{l,v}] v:0~100 */
  function svgRadar(axes, o) {
    o = o || {}; var P = pal();
    var W = o.w || 300, H = o.h || 268, cx = W / 2, cy = H / 2 + 4, R = Math.min(cx, cy) - 46;
    var n = axes.length; if (!n) return '<div class="empty">无数据</div>';
    var col = o.color || P.accent;
    var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:' + W + 'px;">';
    [0.25, 0.5, 0.75, 1].forEach(function (f) {
      var pts = '';
      for (var i = 0; i < n; i++) {
        var a = -Math.PI / 2 + i * 2 * Math.PI / n;
        pts += (cx + R * f * Math.cos(a)).toFixed(1) + ',' + (cy + R * f * Math.sin(a)).toFixed(1) + ' ';
      }
      s += '<polygon points="' + pts.trim() + '" fill="none" stroke="' + P.grid + '" stroke-width="1"/>';
    });
    axes.forEach(function (ax, i) {
      var a = -Math.PI / 2 + i * 2 * Math.PI / n;
      s += '<line x1="' + cx + '" y1="' + cy + '" x2="' + (cx + R * Math.cos(a)).toFixed(1) + '" y2="' +
        (cy + R * Math.sin(a)).toFixed(1) + '" stroke="' + P.grid + '"/>';
      var lx = cx + (R + 18) * Math.cos(a), ly = cy + (R + 18) * Math.sin(a);
      var an = Math.abs(Math.cos(a)) < 0.3 ? 'middle' : (Math.cos(a) > 0 ? 'start' : 'end');
      s += '<text x="' + lx.toFixed(1) + '" y="' + (ly + 3).toFixed(1) + '" font-size="10" text-anchor="' + an +
        '" fill="' + P.muted + '">' + esc(ax.l) + '</text>';
    });
    var dp = '';
    axes.forEach(function (ax, i) {
      var a = -Math.PI / 2 + i * 2 * Math.PI / n, r = R * Math.min(1, Math.max(0, nz(ax.v)) / 100);
      dp += (cx + r * Math.cos(a)).toFixed(1) + ',' + (cy + r * Math.sin(a)).toFixed(1) + ' ';
    });
    s += '<polygon points="' + dp.trim() + '" fill="' + col + '" fill-opacity="0.22" stroke="' + col + '" stroke-width="2"/>';
    axes.forEach(function (ax, i) {
      var a = -Math.PI / 2 + i * 2 * Math.PI / n, r = R * Math.min(1, Math.max(0, nz(ax.v)) / 100);
      s += '<circle cx="' + (cx + r * Math.cos(a)).toFixed(1) + '" cy="' + (cy + r * Math.sin(a)).toFixed(1) +
        '" r="3" fill="' + col + '"><title>' + esc(ax.l) + ': ' + Math.round(nz(ax.v)) + '</title></circle>';
    });
    return s + '</svg>';
  }

  /* 半圆仪表盘 v:0~100 */
  function svgGauge(v, o) {
    o = o || {}; var P = pal();
    var W = o.w || 240, H = o.h || 140, cx = W / 2, cy = H - 18, R = 88, sw = 17;
    v = Math.max(0, Math.min(100, nz(v)));
    var segs = [[0, 30, '#4dabf7'], [30, 45, '#3aa884'], [45, 60, '#8ac926'], [60, 76, '#f4a52a'], [76, 100, P.up]];
    function pt(p) { var a = Math.PI * (1 - p / 100); return [cx + R * Math.cos(a), cy - R * Math.sin(a)]; }
    var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:' + W + 'px;">';
    segs.forEach(function (sg) {
      var p0 = pt(sg[0]), p1 = pt(sg[1]);
      s += '<path d="M' + p0[0].toFixed(1) + ' ' + p0[1].toFixed(1) + ' A' + R + ' ' + R + ' 0 0 1 ' +
        p1[0].toFixed(1) + ' ' + p1[1].toFixed(1) + '" fill="none" stroke="' + sg[2] +
        '" stroke-width="' + sw + '" stroke-linecap="butt" opacity="0.9"/>';
    });
    var a = Math.PI * (1 - v / 100);
    var nx = cx + (R - 3) * Math.cos(a), ny = cy - (R - 3) * Math.sin(a);
    s += '<line x1="' + cx + '" y1="' + cy + '" x2="' + nx.toFixed(1) + '" y2="' + ny.toFixed(1) +
      '" stroke="' + P.text + '" stroke-width="2.5" stroke-linecap="round"/>';
    s += '<circle cx="' + cx + '" cy="' + cy + '" r="5" fill="' + P.text + '"/>';
    s += '<text x="18" y="' + (cy + 14) + '" font-size="9" fill="' + P.faint + '">冰点</text>';
    s += '<text x="' + (W - 18) + '" y="' + (cy + 14) + '" font-size="9" text-anchor="end" fill="' + P.faint + '">亢奋</text>';
    return s + '</svg>';
  }

  /* 连板梯队金字塔 rows=[{lv,n}] */
  function svgPyramid(rows, o) {
    o = o || {}; var P = pal();
    var W = o.w || 300, rowH = 26, H = Math.max(40, rows.length * rowH + 10);
    if (!rows.length) return '<div class="empty">无数据</div>';
    var max = Math.max.apply(null, rows.map(function (r) { return r.n; }));
    var cx = W / 2;
    var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:' + W + 'px;">';
    rows.forEach(function (r, i) {
      var w = Math.max(16, (r.n / max) * (W - 96)), y = i * rowH + 5;
      var t = Math.min(6, r.lv);
      /* SVG 表现属性不支持 var()，故解析当前主题的连板色阶 token 后内联 */
      var col = gv('--lb' + t + '-bg') ||
        ['#e9ecef', '#ffe3e3', '#ffc9c9', '#ffa8a8', '#fa5252', '#c92a2a'][t - 1] || '#e9ecef';
      var tc = gv('--lb' + t + '-fg') || (t >= 5 ? '#ffffff' : '#a51111');
      s += '<rect x="' + (cx - w / 2).toFixed(1) + '" y="' + y + '" width="' + w.toFixed(1) + '" height="' + (rowH - 7) +
        '" rx="4" fill="' + col + '"><title>' + r.lv + ' 连板: ' + r.n + ' 只</title></rect>';
      s += '<text x="' + cx + '" y="' + (y + rowH / 2 + 1) + '" font-size="11" font-weight="700" text-anchor="middle" fill="' +
        tc + '">' + r.n + '</text>';
      s += '<text x="' + (cx - w / 2 - 8).toFixed(1) + '" y="' + (y + rowH / 2 + 1) +
        '" font-size="10.5" text-anchor="end" fill="' + P.muted + '">' + r.lv + '板</text>';
    });
    return s + '</svg>';
  }

  /* 概率分布条：把个股按断板概率分桶 */
  function svgProbDist(risks, o) {
    var buckets = [
      { l: '<50%', lo: 0, hi: 50, c: '#2b8a3e' }, { l: '50-66', lo: 50, hi: 66, c: '#8ac926' },
      { l: '66-80', lo: 66, hi: 80, c: '#f4a52a' }, { l: '80-90', lo: 80, hi: 90, c: '#e8590c' },
      { l: '≥90%', lo: 90, hi: 101, c: '#c92a2a' }
    ];
    var data = buckets.map(function (b) {
      return { l: b.l, c: b.c, v: risks.filter(function (r) { return r.p_break >= b.lo && r.p_break < b.hi; }).length };
    });
    return svgBar(data, o);
  }

  /* 热力网格：矩阵强度图（连板持续性 / 板块轮动）rows=[{l,vals:[v...]}], cols=[label...] */
  function svgHeat(o) {
    o = o || {}; var P = pal();
    var rows = o.rows || [], cols = o.cols || [], max = o.max || 1;
    if (!rows.length || !cols.length) return '<div class="empty">无数据</div>';
    var cw = o.cw || 34, ch = o.ch || 24, padL = o.padL || 40, padT = o.padT || 16;
    var W = padL + cols.length * cw + 8, H = padT + rows.length * ch + 8;
    function heat(t) {
      if (!t || t <= 0) return 'rgba(120,130,150,0.12)';
      if (t < 0.2) return '#2b6cb0';
      if (t < 0.4) return '#1c7ed6';
      if (t < 0.6) return '#e8a90c';
      if (t < 0.8) return '#f08c00';
      return '#ff5a5a';
    }
    var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:' + W + 'px;">';
    cols.forEach(function (c, j) {
      s += '<text x="' + (padL + j * cw + cw / 2) + '" y="' + (padT - 4) +
        '" font-size="9.5" text-anchor="middle" fill="' + P.faint + '">' + esc(c) + '</text>';
    });
    rows.forEach(function (r, i) {
      s += '<text x="' + (padL - 6) + '" y="' + (padT + i * ch + ch / 2 + 3) +
        '" font-size="10" text-anchor="end" fill="' + P.muted + '">' + esc(r.l) + '</text>';
      (r.vals || []).forEach(function (v, j) {
        var t = max > 0 ? v / max : 0;
        var x = padL + j * cw, y = padT + i * ch;
        s += '<rect x="' + x + '" y="' + y + '" width="' + (cw - 3) + '" height="' + (ch - 3) +
          '" rx="3" fill="' + heat(t) + '"><title>' + esc(r.l) + ' · ' + esc(cols[j]) + '：' + v + ' 只</title></rect>';
        if (v > 0) s += '<text x="' + (x + (cw - 3) / 2) + '" y="' + (y + (ch - 3) / 2 + 3) +
          '" font-size="9.5" text-anchor="middle" fill="#fff" opacity="' + (t > 0.35 ? 1 : 0) + '">' + v + '</text>';
      });
    });
    return s + '</svg>';
  }

  g.CH = { svgBar: svgBar, svgHBar: svgHBar, svgLine: svgLine, svgDonut: svgDonut, svgRadar: svgRadar,
           svgGauge: svgGauge, svgPyramid: svgPyramid, svgProbDist: svgProbDist, svgHeat: svgHeat,
           COLORS: C, esc: esc };
})(window);
