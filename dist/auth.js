/* 登录门禁：从 dist/data/<id>.bin 解密分析数据，再渲染。
 * 与 pipeline/encrypt_data.py 的加密算法对应（PBKDF2-HMAC-SHA256 + HMAC 密钥流 XOR）。
 * 本地若有明文 data.js（双击打开），则跳过登录直接渲染（开发模式）。 */
(function () {
  'use strict';
  var SALT_LEN = 16, ITER = 200000, LABEL = 'stock-analysis-v1';
  var STORE_KEY = 'sa_auth_v1';

  // 口令记忆：这是给自己和朋友用的看板，威胁模型是"链接被转发"，不是"设备被攻破"。
  // 存本机可免去每天重输；换设备/改口令自动失效。
  function remember(id, pass) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ id: id, pass: pass })); } catch (e) {}
  }
  function recall() {
    try {
      var v = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
      return (v && v.id && v.pass) ? v : null;
    } catch (e) { return null; }
  }
  function forget() {
    try { localStorage.removeItem(STORE_KEY); } catch (e) {}
  }

  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function cacheBust(u) { return u + (u.indexOf('?') >= 0 ? '&' : '?') + 't=' + Math.floor(Date.now() / 60000); }

  function injectStyle() {
    var css = [
      '.sa-lock{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;',
      'background:rgba(8,12,20,.92);backdrop-filter:blur(6px);font-family:-apple-system,Segoe UI,Roboto,"Microsoft YaHei",sans-serif;}',
      '.sa-card{width:340px;max-width:90vw;background:#0f1626;border:1px solid #1e2a44;border-radius:14px;padding:26px 24px;box-shadow:0 20px 60px rgba(0,0,0,.5);}',
      '.sa-card h2{margin:0 0 4px;font-size:18px;color:#e8eefc;}',
      '.sa-card p.sub{margin:0 0 18px;font-size:12px;color:#8aa0c8;line-height:1.5;}',
      '.sa-card label{display:block;font-size:12px;color:#9fb3d8;margin:12px 0 6px;}',
      '.sa-card select,.sa-card input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:9px;',
      'border:1px solid #243453;background:#0a1120;color:#e8eefc;font-size:14px;outline:none;}',
      '.sa-card select:focus,.sa-card input:focus{border-color:#3b82f6;}',
      '.sa-card button{margin-top:18px;width:100%;padding:11px;border:0;border-radius:9px;cursor:pointer;',
      'background:linear-gradient(135deg,#2563eb,#06b6d4);color:#fff;font-size:15px;font-weight:600;}',
      '.sa-card button:disabled{opacity:.6;cursor:default;}',
      '.sa-err{margin-top:12px;font-size:12px;color:#f87171;min-height:16px;}',
      '.sa-loading{margin-top:14px;font-size:12px;color:#8aa0c8;}',
      '.sa-remember{display:flex;align-items:center;gap:7px;margin-top:14px;font-size:12px;color:#9fb3d8;cursor:pointer;}',
      '.sa-remember input{width:auto;margin:0;}',
      '.sa-switch{position:fixed;right:14px;bottom:14px;z-index:9998;padding:7px 12px;border:1px solid #243453;',
      'border-radius:8px;background:rgba(15,22,38,.86);color:#8aa0c8;font-size:12px;cursor:pointer;',
      'backdrop-filter:blur(4px);font-family:inherit;}',
      '.sa-switch:hover{color:#e8eefc;border-color:#3b82f6;}'
    ].join('');
    var s = document.createElement('style');
    s.textContent = css;
    document.head.appendChild(s);
  }

  function loadScript(src) {
    return new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = cacheBust(src);
      s.onload = function () { res(); };
      s.onerror = function () { rej(new Error('加载失败: ' + src)); };
      document.head.appendChild(s);
    });
  }

  function renderApp() {
    // charts.js 定义全局 CH，app.js 渲染；顺序加载
    return loadScript('charts.js').then(function () { return loadScript('app.js'); });
  }

  function addSwitchButton() {
    if (document.querySelector('.sa-switch')) return;
    var b = el('button', 'sa-switch', '切换账户');
    b.type = 'button';
    b.addEventListener('click', function () {
      forget();
      location.reload();
    });
    document.body.appendChild(b);
  }

  function showError(box, msg) { box.textContent = msg || ''; }

  function buildLogin(meta) {
    injectStyle();
    var overlay = el('div', 'sa-lock');
    var card = el('div', 'sa-card');
    card.appendChild(el('h2', null, 'A股盘后分析 · 访问验证'));
    card.appendChild(el('p', 'sub', '数据已加密。请输入你被授权的口令；链接本身不含任何可读内容。'));

    card.appendChild(el('label', null, '选择账户'));
    var sel = el('select');
    meta.forEach(function (m) {
      var o = document.createElement('option');
      o.value = m.id; o.textContent = m.name; sel.appendChild(o);
    });
    card.appendChild(sel);

    card.appendChild(el('label', null, '口令'));
    var pwd = el('input');
    pwd.type = 'password'; pwd.placeholder = '请输入口令'; pwd.autocomplete = 'off';
    card.appendChild(pwd);

    var remLabel = el('label', 'sa-remember');
    var remBox = document.createElement('input');
    remBox.type = 'checkbox'; remBox.checked = true;
    remLabel.appendChild(remBox);
    remLabel.appendChild(document.createTextNode('在本机记住口令（下次免输）'));
    card.appendChild(remLabel);

    var btn = el('button', null, '解密并进入');
    card.appendChild(btn);
    var err = el('div', 'sa-err');
    card.appendChild(err);
    var loading = el('div', 'sa-loading');
    card.appendChild(loading);

    overlay.appendChild(card);
    document.body.appendChild(overlay);

    function attempt() {
      showError(err, '');
      var id = sel.value;
      var pass = pwd.value;
      if (!pass) { showError(err, '请输入口令'); return; }
      btn.disabled = true; loading.textContent = '正在解密…（首次约 1 秒）';
      fetchBlob(id, pass).then(function () {
        if (remBox.checked) remember(id, pass); else forget();
        overlay.parentNode.removeChild(overlay);
        addSwitchButton();
        window.__SA_USER__ = id;   // 供站点顶栏判断是否显示「管理用户」入口
        return renderApp();
      }).catch(function (e) {
        btn.disabled = false; loading.textContent = '';
        showError(err, (e && e.message) ? e.message : '解密失败');
      });
    }
    btn.addEventListener('click', attempt);
    pwd.addEventListener('keydown', function (e) { if (e.key === 'Enter') attempt(); });
    setTimeout(function () { pwd.focus(); }, 50);
  }

  function fetchBlob(id, pass) {
    var url = cacheBust('data/' + id + '.bin');
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error('找不到该账户的加密数据');
      return r.arrayBuffer();
    }).then(function (buf) {
      return decrypt(new Uint8Array(buf), pass);
    }).then(function (txt) {
      var data;
      try { data = JSON.parse(txt); } catch (e) { throw new Error('口令错误（无法解密）'); }
      if (!data || !data.meta) throw new Error('口令错误（数据损坏）');
      window.__STOCK_DATA__ = data;
      return data;
    });
  }

  function decrypt(bytes, pass) {
    var salt = bytes.slice(0, SALT_LEN);
    var ct = bytes.slice(SALT_LEN);
    var enc = new TextEncoder();
    return crypto.subtle.importKey('raw', enc.encode(pass), 'PBKDF2', false, ['deriveKey'])
      .then(function (mat) {
        return crypto.subtle.deriveKey(
          { name: 'PBKDF2', salt: salt, iterations: ITER, hash: 'SHA-256' },
          mat, { name: 'HMAC', hash: 'SHA-256', length: 256 }, false, ['sign']);
      }).then(function (key) {
        var ks = new Uint8Array(ct.length);
        var p = 0, i = 0;
        var chain = Promise.resolve();
        // 逐块生成 HMAC 密钥流并异或（保持与 Python 端一致）
        function block() {
          if (p >= ct.length) return;
          var ctr = new Uint8Array(4);
          new DataView(ctr.buffer).setUint32(0, i, false);
          return crypto.subtle.sign('HMAC', key, ctr).then(function (mac) {
            mac = new Uint8Array(mac);
            for (var k = 0; k < mac.length && p < ct.length; k++) {
              ks[p] = ct[p] ^ mac[k]; p++;
            }
            i++;
          }).then(block);
        }
        return block().then(function () {
          return new TextDecoder().decode(ks);
        });
      });
  }

  function splash(text) {
    injectStyle();
    var o = el('div', 'sa-lock');
    var c = el('div', 'sa-card');
    c.appendChild(el('h2', null, 'A股盘后分析'));
    c.appendChild(el('p', 'sub', text));
    o.appendChild(c);
    document.body.appendChild(o);
    return o;
  }

  function boot() {
    // 开发模式：明文 data.js 已被 index.html 加载
    if (window.__STOCK_DATA__) { return renderApp(); }
    fetch(cacheBust('users.json')).then(function (r) {
      if (!r.ok) throw new Error('nousers');
      return r.json();
    }).then(function (meta) {
      if (!meta || !meta.length) throw new Error('nousers');

      // 本机记住过口令就直接进；口令被管理员改过则自动清掉并回到登录框
      var saved = recall();
      var known = saved && meta.some(function (m) { return m.id === saved.id; });
      if (known) {
        var sp = splash('正在解密数据…');
        return fetchBlob(saved.id, saved.pass).then(function () {
          sp.parentNode.removeChild(sp);
          addSwitchButton();
          window.__SA_USER__ = saved.id;   // 供站点顶栏判断是否显示「管理用户」入口
          return renderApp();
        }).catch(function () {
          forget();
          sp.parentNode.removeChild(sp);
          buildLogin(meta);
        });
      }
      buildLogin(meta);
    }).catch(function () {
      injectStyle();
      var d = el('div', 'sa-lock');
      var c = el('div', 'sa-card');
      c.appendChild(el('h2', null, '暂无可用数据'));
      c.appendChild(el('p', 'sub', '本站尚未配置访问凭据，或数据正在首次生成中。请稍后重试，或联系管理员。'));
      d.appendChild(c); document.body.appendChild(d);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
