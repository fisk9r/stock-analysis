/* 用浏览器同款 WebCrypto 算法（PBKDF2-HMAC-SHA256 + HMAC 密钥流 XOR）
 * 解开 dist/data/_admin.bin，确认 owner 在任意设备上都能取回完整名单。 */
const fs = require('fs');
const path = require('path');
const { webcrypto } = require('node:crypto');
const crypto = webcrypto;

const ROOT = path.join(__dirname, '..', '..');
const SALT_LEN = 16, ITER = 200000;

async function decrypt(bytes, pass) {
  const salt = bytes.slice(0, SALT_LEN);
  const ct = bytes.slice(SALT_LEN);
  const mat = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(pass), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations: ITER, hash: 'SHA-256' },
    mat, { name: 'HMAC', hash: 'SHA-256', length: 256 }, false, ['sign']);
  const out = new Uint8Array(ct.length);
  let p = 0, i = 0;
  while (p < ct.length) {
    const ctr = new Uint8Array(4);
    new DataView(ctr.buffer).setUint32(0, i, false);
    const mac = new Uint8Array(await crypto.subtle.sign('HMAC', key, ctr));
    for (let k = 0; k < mac.length && p < ct.length; k++) { out[p] = ct[p] ^ mac[k]; p++; }
    i++;
  }
  return new TextDecoder().decode(out);
}

(async () => {
  const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'config', 'allowed_users.json'), 'utf8'));
  const owner = cfg.users.find((u) => u.id === 'owner') || cfg.users[0];
  const blob = new Uint8Array(fs.readFileSync(path.join(ROOT, 'dist', 'data', '_admin.bin')));

  let txt;
  try {
    txt = await decrypt(blob, owner.pass);
  } catch (e) {
    console.log('FAIL  解密抛错: ' + e.message); process.exit(1);
  }

  let got;
  try { got = JSON.parse(txt); } catch (e) {
    console.log('FAIL  解密结果不是合法 JSON（口令不符？）'); process.exit(1);
  }

  const ok = JSON.stringify(got.users) === JSON.stringify(cfg.users);
  console.log((ok ? 'PASS  ' : 'FAIL  ') + 'owner 口令可解开 _admin.bin，名单与本机配置一致');
  console.log('      成员: ' + got.users.map((u) => u.id + '(' + u.name + ')').join(', '));
  console.log('      含口令字段: ' + got.users.every((u) => !!u.pass));

  // 反例：错误口令必须解不出合法 JSON
  let wrongOk = false;
  try { JSON.parse(await decrypt(blob, owner.pass + 'x')); wrongOk = true; } catch (e) {}
  console.log((wrongOk ? 'FAIL  ' : 'PASS  ') + '错误口令无法解开（门禁有效）');

  process.exit(ok && !wrongOk ? 0 : 1);
})();
