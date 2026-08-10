/* 把 tweetnacl(nacl-fast) + blakejs(blake2b) + crypto_box_seal 封装打成单个浏览器文件。
 * 产出：dist/nacl.js，暴露 SA_SEAL(明文, 公钥base64) -> 密文base64
 * 用途：让浏览器能按 GitHub 要求密封（sealed box）加密仓库 Secret，
 *       从而在任意设备远程管理用户，不必依赖本机跑 manage_users.py serve。
 *
 * 重建：node tools/naclbuild/bundle_nacl.js       （上游源码缺失时自动下载）
 * 自检：node tools/naclbuild/test_seal.js
 *       python tools/naclbuild/genkey.py && node tools/naclbuild/test_seal.js && python tools/naclbuild/verify.py
 */
const fs = require('fs');

const path = require('path');
const HERE = __dirname;

const SOURCES = {
  'tweetnacl.min.js': 'https://cdn.jsdelivr.net/npm/tweetnacl@1.0.3/nacl-fast.min.js',
  'b2b.js': 'https://cdn.jsdelivr.net/npm/blakejs@1.2.1/blake2b.js',
};

async function ensureSources() {
  for (const name of Object.keys(SOURCES)) {
    const p = path.join(HERE, name);
    if (fs.existsSync(p)) continue;
    process.stdout.write('下载 ' + name + ' … ');
    const r = await fetch(SOURCES[name]);
    if (!r.ok) throw new Error('下载失败 ' + SOURCES[name] + ' HTTP ' + r.status);
    fs.writeFileSync(p, Buffer.from(await r.arrayBuffer()));
    console.log('ok');
  }
}

async function build() {
await ensureSources();
const tweetnacl = fs.readFileSync(path.join(HERE, 'tweetnacl.min.js'), 'utf8');
let b2b = fs.readFileSync(path.join(HERE, 'b2b.js'), 'utf8');

// 去掉 CommonJS 接线，改为本文件内联
b2b = b2b.replace("const util = require('./util')", `const util = {
  normalizeInput: function (input) {
    if (input instanceof Uint8Array) return input;
    if (typeof input === 'string') return new TextEncoder().encode(input);
    throw new Error('Input must be a string or Uint8Array');
  },
  toHex: function (bytes) {
    return Array.prototype.map.call(bytes, function (n) {
      return (n < 16 ? '0' : '') + n.toString(16);
    }).join('');
  }
}`);
b2b = b2b.replace(/module\.exports\s*=\s*\{[\s\S]*?\}\s*$/, '');

const out = `/* 自带的加密库（vendored，无外部 CDN 依赖）
 * - tweetnacl 1.0.3 (nacl-fast) —— X25519 + XSalsa20-Poly1305
 * - blakejs 1.2.1 blake2b     —— sealed box 的 nonce 派生
 * - SA_SEAL()                  —— libsodium crypto_box_seal 等价实现
 * 用途：让浏览器能按 GitHub 要求加密仓库 Secret，从而在任意设备远程管理用户，
 *       不再依赖本机运行 python tools/manage_users.py serve。
 * 许可：tweetnacl(Unlicense) / blakejs(MIT) */
(function () {
'use strict';

/* ============ tweetnacl 1.0.3 (nacl-fast.min.js) ============ */
${tweetnacl}

/* ============ blakejs 1.2.1 blake2b ============ */
${b2b}

/* ============ crypto_box_seal（与 libsodium / PyNaCl SealedBox 兼容） ============
 * seal(m, pk):
 *   ephemeral = box_keypair()
 *   nonce     = blake2b(ephemeral.pk || pk, outlen=24)
 *   c         = box(m, nonce, pk, ephemeral.sk)
 *   result    = ephemeral.pk (32B) || c
 */
var _nacl = (typeof self !== 'undefined' && self.nacl) ? self.nacl : nacl;

function sealedBox(message, recipientPk) {
  if (!(message instanceof Uint8Array)) message = new TextEncoder().encode(String(message));
  if (recipientPk.length !== 32) throw new Error('公钥长度必须是 32 字节');
  var eph = _nacl.box.keyPair();
  var nonceInput = new Uint8Array(64);
  nonceInput.set(eph.publicKey, 0);
  nonceInput.set(recipientPk, 32);
  var nonce = blake2b(nonceInput, null, 24);
  var boxed = _nacl.box(message, nonce, recipientPk, eph.secretKey);
  var out = new Uint8Array(32 + boxed.length);
  out.set(eph.publicKey, 0);
  out.set(boxed, 32);
  return out;
}

function b64encode(bytes) {
  var s = '';
  for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}
function b64decode(str) {
  var bin = atob(str);
  var out = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/* 对外：把明文按 GitHub 公钥密封，直接返回可提交的 base64 */
self.SA_SEAL = function (plaintext, publicKeyB64) {
  var pk = b64decode(publicKeyB64);
  return b64encode(sealedBox(plaintext, pk));
};
self.SA_SEAL.raw = sealedBox;
self.SA_SEAL.blake2b = blake2b;
self.SA_SEAL.b64encode = b64encode;
self.SA_SEAL.b64decode = b64decode;

})();
`;

fs.writeFileSync(path.join(HERE, '..', '..', 'dist', 'nacl.js'), out);
console.log('已生成 dist/nacl.js（' + out.length + ' 字节）');
}

build().catch(function (e) { console.error(e.message); process.exit(1); });
