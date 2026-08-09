/* 端到端无头校验：用 mock window/document 驱动 app.js 渲染六个视图 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = 'C:/Users/Basshunter-j/WorkBuddy/2026-08-04-11-06-17/stock-analysis/dist';

const store = {};
function mkEl(id) {
  return {
    id, _html: '',
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    classList: { toggle() {}, add() {}, remove() {} },
    dataset: {},
    _h: {},
    addEventListener(t, fn) { this._h[t] = fn; },
    querySelectorAll() { return []; },
  };
}
function getEl(id) { if (!store[id]) store[id] = mkEl(id); return store[id]; }

global.window = global;
global.document = {
  readyState: 'complete',
  getElementById: (id) => getEl(id),
  querySelector: (sel) => mkEl(sel),
  querySelectorAll: () => [],
  addEventListener: (type, fn) => { if (type === 'DOMContentLoaded') fn(); },
  createElement: () => mkEl('x'),
};
global.location = { hash: '' };
global.history = { replaceState() {} };
global.scrollTo = () => {};
global.navigator = { userAgent: 'node' };

function load(f) {
  const code = fs.readFileSync(path.join(ROOT, f), 'utf8');
  vm.runInThisContext(code, { filename: f });
}

let fail = 0;
try {
  load('charts.js');
  console.log('charts.js loaded; CH keys:', Object.keys(global.CH || {}).slice(0, 12).join(','));
  load('data.js');
  const D = global.__STOCK_DATA__;
  console.log('data.js loaded; date=', D.meta.date, 'limit_ups=', D.limit_ups.length,
              'sectors.industry=', D.sectors.industry.length, 'sectors.concept=', D.sectors.concept.length,
              'break_risk=', D.break_risk.length, 'demons=', D.demons.length,
              'indexes=', (D.market.indexes || []).length);
  load('app.js'); // IIFE runs boot() -> renders overview
} catch (e) {
  console.error('LOAD ERROR:', e.stack);
  process.exit(1);
}

const VIEWS = ['overview', 'ladder', 'sectors', 'risk', 'demon', 'auction', 'rec'];
const click = getEl('tabs')._h.click;
if (!click) { console.error('tabs click handler not captured!'); process.exit(1); }

console.log('\n--- per-view render ---');
for (const k of VIEWS) {
  if (k !== 'overview') {
    try { click({ target: { dataset: { v: k } } }); }
    catch (e) { console.error('  [' + k + '] click threw:', e.message); fail++; continue; }
  }
  const html = getEl('v-' + k)._html || '';
  const err = html.includes('渲染出错');
  const svg = (html.match(/<svg/g) || []).length;
  const len = html.length;
  const ok = !err && len > 200;
  if (!ok) fail++;
  console.log('  ' + (ok ? 'OK ' : 'FAIL') + ' ' + k.padEnd(9) +
              ' len=' + String(len).padStart(6) + ' svg=' + String(svg).padStart(2) +
              (err ? '  <<< 渲染出错' : ''));
}

// 抽查关键字段渲染
const D = global.__STOCK_DATA__;
console.log('\n--- spot checks ---');
console.log('  最高连板:', Math.max.apply(null, D.limit_ups.map(r => r.streak)), '板');
console.log('  情绪分:', D.market.sentiment && D.market.sentiment.score,
            '级别:', D.market.sentiment && D.market.sentiment.label);
console.log('  周期:', D.market.cycle && D.market.cycle.phase);
console.log('  推荐仓位:', D.recommend && D.recommend.position);
console.log('  今日推荐条数(all):', (D.recommend && D.recommend.all || []).length);

console.log('\nRESULT:', fail === 0 ? 'PASS' : ('FAIL (' + fail + ')'));
process.exit(fail === 0 ? 0 : 1);
