#!/usr/bin/env node
/* 无头渲染体检：用真实 dist/data.js + Proxy DOM 垫片驱动全部视图函数，
   捕获运行时异常。用法：node tools/render_check.js [dist目录]
   退出码 0=全 PASS，2=有 FAIL/加载失败。 */
'use strict';
const vm = require('vm');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const DIST = process.argv[2] ? path.resolve(process.argv[2]) : path.join(ROOT, 'dist');
const read = (p) => fs.readFileSync(p, 'utf8');

const ctx2d = new Proxy({}, { get: (t, p) => (p in t ? t[p] : () => {}), set: (t, p, v) => { t[p] = v; return true; } });

function fakeEl(tag) {
  const store = { _html: '', _tag: tag || 'div', dataset: {} };
  const classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  const style = new Proxy({}, { get: () => '', set: () => true });
  return new Proxy(store, {
    get(t, p) {
      if (p === 'innerHTML') return store._html;
      if (p === 'textContent') return store._text || '';
      if (p === 'classList') return classList;
      if (p === 'style') return style;
      if (p === 'dataset') return store.dataset;
      if (['offsetWidth', 'offsetHeight', 'offsetLeft', 'clientWidth'].includes(p)) return 0;
      if (p === 'tagName') return (store._tag || 'DIV').toUpperCase();
      if (['appendChild', 'removeChild', 'insertBefore'].includes(p)) return () => fakeEl();
      if (['setAttribute', 'removeAttribute', 'addEventListener', 'removeEventListener',
        'focus', 'blur', 'click'].includes(p)) return () => {};
      if (p === 'querySelector') return () => fakeEl();
      if (p === 'querySelectorAll') return () => [];
      if (p === 'getAttribute') return () => null;
      if (p === 'getContext') return () => ctx2d;
      if (p === 'ownerDocument') return doc;
      return undefined;
    },
    set(t, p, v) {
      if (p === 'innerHTML') store._html = v; else if (p === 'textContent') store._text = v; else store[p] = v;
      return true;
    }
  });
}

const elCache = {};
const doc = {
  readyState: 'complete',
  documentElement: fakeEl('html'), body: fakeEl('body'), head: fakeEl('head'),
  getElementById(id) { return elCache[id] || (elCache[id] = fakeEl('section')); },
  createElement: (t) => fakeEl(t), createElementNS: (_n, t) => fakeEl(t),
  querySelector: () => fakeEl(), querySelectorAll: () => [],
  addEventListener: () => {}, removeEventListener: () => {},
};

const sandbox = {};
sandbox.window = sandbox; sandbox.document = doc;
sandbox.navigator = { userAgent: 'node' };
sandbox.location = { hash: '', protocol: 'https:', href: 'https://x/', replace() {} };
sandbox.history = { replaceState() {}, pushState() {} };
sandbox.console = console;
sandbox.setTimeout = () => 0; sandbox.clearTimeout = () => {};
sandbox.setInterval = () => 0; sandbox.clearInterval = () => {};
sandbox.requestAnimationFrame = () => 0; sandbox.cancelAnimationFrame = () => {};
sandbox.fetch = () => Promise.resolve({ ok: true, json: () => ({}) });
sandbox.alert = () => {};
sandbox.addEventListener = () => {}; sandbox.removeEventListener = () => {}; sandbox.scrollTo = () => {};
vm.createContext(sandbox);

function run(src, name) {
  try { vm.runInContext(src, sandbox, { filename: name }); }
  catch (e) { console.error('LOAD FAIL [' + name + ']: ' + e.message); process.exit(2); }
}

run(read(path.join(DIST, 'data.js')), 'data.js');
run(read(path.join(DIST, 'charts.js')), 'charts.js');
let app = read(path.join(DIST, 'app.js'));
if (!/function show\(k\) \{/.test(app)) { console.error('app.js 未找到 show()'); process.exit(2); }
app = app.replace(/function show\(k\) \{/, 'window.__APP__ = { views: views, show: show };\n    function show(k) {');
run(app, 'app.js');

const APP = sandbox.__APP__;
if (!APP || !APP.views) { console.error('views 未暴露'); process.exit(2); }

let pass = 0, fail = 0; const rep = [];
for (const k of Object.keys(APP.views)) {
  try {
    const html = APP.views[k]() || '';
    rep.push((html.length ? 'PASS' : 'EMPTY') + '  ' + k + '  (len=' + html.length + ')');
    if (html.length) pass++; else fail++;
  } catch (e) { fail++; rep.push('FAIL  ' + k + '  -> ' + e.message); }
}
console.log('=== Headless render of ' + Object.keys(APP.views).length + ' views (' + DIST + ') ===');
console.log(rep.join('\n'));
console.log('PASS=' + pass + ' FAIL=' + fail);
console.log(fail === 0 ? 'ALL_VIEWS_OK' : 'VIEWS_HAVE_ERRORS');
process.exit(fail === 0 ? 0 : 2);
