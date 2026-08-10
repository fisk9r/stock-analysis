/* 验证 dist/nacl.js：
 *  1) blake2b 官方测试向量
 *  2) SA_SEAL 产出的密文能被 PyNaCl SealedBox 解开（由 verify.py 对拍）
 *
 * 说明：不用 vm 沙箱——跨 realm 的 instanceof Uint8Array 会失败，
 * tweetnacl 内部有类型检查，会误报。改为在同一 realm 里用 new Function 注入伪 self，
 * 并把 module 传成 undefined，让 tweetnacl 走浏览器分支（self.nacl）。 */
const fs = require('fs');
const path = require('path');

const HERE = __dirname;
const ROOT = path.join(HERE, '..', '..');
const code = fs.readFileSync(path.join(ROOT, 'dist', 'nacl.js'), 'utf8');

// 真实浏览器里 self.crypto 天然存在；这里补上，tweetnacl 靠它取随机数
const g = { crypto: require('node:crypto').webcrypto };
// eslint-disable-next-line no-new-func
new Function('self', 'window', 'module', 'exports', 'require', code)(
  g, g, undefined, undefined, undefined
);

if (typeof g.SA_SEAL !== 'function') {
  console.log('FAIL  nacl.js 未暴露 SA_SEAL');
  process.exit(1);
}

let fail = 0;
function check(name, got, want) {
  const ok = got === want;
  if (!ok) fail++;
  console.log((ok ? 'PASS  ' : 'FAIL  ') + name);
  if (!ok) { console.log('   got : ' + got); console.log('   want: ' + want); }
}

const toHex = (u8) => Buffer.from(u8).toString('hex');
const enc = (s) => new TextEncoder().encode(s);
const b2 = g.SA_SEAL.blake2b;

// ---- 1. blake2b 官方向量（RFC 7693 / libsodium） ----
check(
  'blake2b("abc") 64B',
  toHex(b2(enc('abc'), null, 64)),
  'ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d1' +
  '7d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923'
);
check(
  'blake2b("") 64B',
  toHex(b2(new Uint8Array(0), null, 64)),
  '786a02f742015903c6c6fd852552d272912f4740e15847618a86e217f71f5419' +
  'd25e1031afee585313896444934eb04b903a685b1448b755d56f701afe9be2ce'
);
// 注：sealed box 的 nonce 用 24 字节 blake2b（长度参数化，非截断 512）。
// 其正确性由与 PyNaCl 的解密对拍覆盖——nonce 不对必然解不开。
check('blake2b 24B 输出长度', b2(enc('abc'), null, 24).length, 24);

// ---- 2. 生成 sealed box 密文，交给 PyNaCl 解 ----
const key = JSON.parse(fs.readFileSync(path.join(HERE, 'testkey.json'), 'utf8'));
const plaintext = JSON.stringify({
  users: [
    { id: 'owner', name: '我', pass: 'I8Tc9nTyooBxwcmA' },
    { id: 'friend1', name: '张三·测试', pass: 'aB3dEfGhJkMn' },
  ],
});
const sealedB64 = g.SA_SEAL(plaintext, key.pk);
fs.writeFileSync(
  path.join(HERE, 'sealed.json'),
  JSON.stringify({ sealed: sealedB64, plaintext })
);
console.log('      sealed(b64) 长度: ' + sealedB64.length);

// 密文 = 32 字节临时公钥 + 16 字节 MAC + 明文长度
const rawLen = g.SA_SEAL.b64decode(sealedB64).length;
check('密文长度 = 32 + 16 + 明文字节数', rawLen, 32 + 16 + Buffer.byteLength(plaintext, 'utf8'));
check('每次密文不同（临时密钥随机）', g.SA_SEAL(plaintext, key.pk) !== sealedB64, true);

process.exit(fail ? 1 : 0);
