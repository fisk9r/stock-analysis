// 用 Node 的 WebCrypto 复现 dist/auth.js 的解密流程，验证 Python 加密的 .bin 在浏览器里确实能打开。
// 用法：node tools/verify_decrypt.js <site目录> <用户id> <口令>
const fs = require('fs');
const path = require('path');

const [dir, uid, pass] = process.argv.slice(2);
if (!dir || !uid || !pass) {
  console.error('用法: node tools/verify_decrypt.js <site目录> <用户id> <口令>');
  process.exit(2);
}

const ITER = 200000;

async function decrypt(bytes, password) {
  const salt = bytes.slice(0, 16);
  const ct = bytes.slice(16);
  const baseKey = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations: ITER, hash: 'SHA-256' },
    baseKey, { name: 'HMAC', hash: 'SHA-256', length: 256 }, false, ['sign']);

  const out = new Uint8Array(ct.length);
  const blocks = Math.ceil(ct.length / 32);
  for (let i = 0; i < blocks; i++) {
    const counter = new Uint8Array(4);
    new DataView(counter.buffer).setUint32(0, i, false);   // big-endian，与 Python 一致
    const ks = new Uint8Array(await crypto.subtle.sign('HMAC', key, counter));
    for (let j = 0; j < 32 && i * 32 + j < ct.length; j++) {
      out[i * 32 + j] = ct[i * 32 + j] ^ ks[j];
    }
  }
  return new TextDecoder().decode(out);
}

(async () => {
  const users = JSON.parse(fs.readFileSync(path.join(dir, 'users.json'), 'utf8'));
  console.log('users.json:', JSON.stringify(users));
  if (JSON.stringify(users).includes('pass')) {
    console.error('❌ users.json 里出现了 pass 字段，口令泄露！');
    process.exit(1);
  }

  const bin = new Uint8Array(fs.readFileSync(path.join(dir, 'data', uid + '.bin')));
  console.log('密文大小:', bin.length, '字节');

  const t0 = Date.now();
  const txt = await decrypt(bin, pass);
  const ms = Date.now() - t0;

  let data;
  try {
    data = JSON.parse(txt);
  } catch (e) {
    console.error('❌ 解密结果不是合法 JSON（口令错或算法不匹配）：' + String(txt).slice(0, 80));
    process.exit(1);
  }
  console.log('✅ 解密成功，耗时 ' + ms + ' ms');
  console.log('   日期:', data.meta && data.meta.date);
  console.log('   数据源:', data.meta && data.meta.source);
  console.log('   涨停家数:', (data.limit_ups || []).length);
  console.log('   顶层字段:', Object.keys(data).join(', '));
  if (data.data_quality) {
    const q = data.data_quality;
    console.log('   多源校验: 抽检 ' + q.checked + ' 只，存疑 ' + q.flagged_count + ' 只');
  }

  // 错误口令必须失败
  const bad = await decrypt(bin, pass + 'x');
  let ok = false;
  try { JSON.parse(bad); } catch (e) { ok = true; }
  console.log(ok ? '✅ 错误口令无法解密（符合预期）' : '❌ 错误口令竟然也能解出 JSON！');
  process.exit(ok ? 0 : 1);
})();
