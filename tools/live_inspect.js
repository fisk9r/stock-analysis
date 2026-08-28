// 线上体检：直连 Cloudflare Pages 拉 users.json + data/<uid>.bin，本地解密后 dump 关键字段。
// 用法：node tools/live_inspect.js [uid] [pass] [--deep]
const https = require('https');

const uid = process.argv[2] || 'owner';
const pass = process.argv[3] || 'I8Tc9nTyooBxwcmA';
const DEEP = process.argv.includes('--deep');
const BASE = 'https://stock-analysis-8zm.pages.dev';
const ITER = 200000;

function get(url) {
  return new Promise((resolve, reject) => {
    https.get(url, { family: 4, headers: { 'Accept-Encoding': 'identity' } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        return get(new URL(res.headers.location, url).href).then(resolve, reject);
      }
      if (res.statusCode !== 200) return reject(new Error('HTTP ' + res.statusCode + ' ' + url));
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve(Buffer.concat(chunks)));
    }).on('error', reject);
  });
}

async function decrypt(bytes, password) {
  const salt = bytes.slice(0, 16);
  const ct = bytes.slice(16);
  const baseKey = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey({ name: 'PBKDF2', salt, iterations: ITER, hash: 'SHA-256' },
    baseKey, { name: 'HMAC', hash: 'SHA-256', length: 256 }, false, ['sign']);
  const out = new Uint8Array(ct.length);
  const blocks = Math.ceil(ct.length / 32);
  for (let i = 0; i < blocks; i++) {
    const counter = new Uint8Array(4);
    new DataView(counter.buffer).setUint32(0, i, false);
    const ks = new Uint8Array(await crypto.subtle.sign('HMAC', key, counter));
    for (let j = 0; j < 32 && i * 32 + j < ct.length; j++) out[i * 32 + j] = ct[i * 32 + j] ^ ks[j];
  }
  return new TextDecoder().decode(out);
}

(async () => {
  const users = JSON.parse((await get(BASE + '/users.json')).toString('utf8'));
  console.log('users:', users.map((u) => u.id).join(','));
  const bin = await get(BASE + '/data/' + uid + '.bin');
  const txt = await decrypt(new Uint8Array(bin), pass);
  const data = JSON.parse(txt);
  console.log('bin bytes:', bin.length, '| date:', data.meta && data.meta.date,
    '| version:', data.meta && data.meta.version, '| built:', data.meta && data.meta.built_at);
  console.log('top keys:', Object.keys(data).join(','));
  const rec = data.recommend || data.rec || {};
  console.log('rec keys:', Object.keys(rec).join(','));
  if (rec.trend) {
    console.log('--- trend n=' + rec.trend.length);
    rec.trend.slice(0, 6).forEach((t, i) => {
      const vd = t.verdict || {};
      var inst = t.institution || {};
      console.log('  [' + i + ']', t.name, t.code, 'is_new=' + t.is_new,
        'first_seen=' + t.first_seen,
        'band=' + ((t.trend_meta || {}).band),
        'state=' + ((t.trend_meta || {}).trend_state),
        '| verdict=' + (t.verdict ? (vd.action + '/hold' + vd.suggested_hold_days + '/early' + vd.early) : 'NONE'),
        '| inst=' + (inst.level || '无') + ':' + (inst.tags || []).join(','));
    });
  }
  if (rec.watch_reco) {
    const w = rec.watch_reco;
    console.log('--- watch_reco keys=' + Object.keys(w).join(','));
    console.log('   items n=' + ((w.items || []).length) + ' sell=' + (w.sell_n || 0) + ' buy=' + (w.buy_n || 0));
    (w.items || []).slice(0, 5).forEach((x) => console.log('   *', x.name, x.action, x.reason));
  }
  ['fused', 'core', 'relay'].forEach((k) => {
    if (rec[k]) console.log('--- ' + k + ' n=' + rec[k].length + ' keys=' + Object.keys(rec[k][0] || {}).join(','));
  });
  if (data.zones) console.log('--- zones items=' + ((data.zones.items || []).length));
  if (data.seats) console.log('--- seats hits=' + ((data.seats.hits || []).length));
  if (DEEP) {
    const fs = require('fs');
    fs.writeFileSync('tmp_verify/live_dump.json', JSON.stringify(data));
    console.log('deep dump -> tmp_verify/live_dump.json');
  }
})().catch((e) => { console.error('FAIL:', e.message); process.exit(1); });
