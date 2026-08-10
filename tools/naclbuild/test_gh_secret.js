/* 真实验证：用浏览器同款 dist/nacl.js 的 SA_SEAL 加密名单并写入 GitHub Secret。
 * 写入内容与当前 config/allowed_users.json 完全一致，因此无副作用。
 * 若返回 204/201，说明浏览器端远程管理链路（密封加密 → 写密钥）完全可用。 */
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
const REPO_NAME = 'stock-analysis';

// 载入 nacl.js（同 realm，伪造 self，module 传 undefined 走浏览器分支）
const g = { crypto: require('node:crypto').webcrypto };
new Function('self', 'window', 'module', 'exports', 'require',
  fs.readFileSync(path.join(ROOT, 'dist', 'nacl.js'), 'utf8'))(
  g, g, undefined, undefined, undefined);

const tokenPath = path.join(ROOT, '..', '.ghtoken');
const token = fs.readFileSync(tokenPath, 'utf8').trim();
const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'config', 'allowed_users.json'), 'utf8'));

function gh(method, p, body) {
  return fetch('https://api.github.com' + p, {
    method,
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    body: body ? JSON.stringify(body) : undefined,
  });
}

(async () => {
  const me = await (await gh('GET', '/user')).json();
  const owner = me.login;
  console.log('仓库: ' + owner + '/' + REPO_NAME);

  const pkRes = await gh('GET', `/repos/${owner}/${REPO_NAME}/actions/secrets/public-key`);
  if (!pkRes.ok) { console.log('FAIL  取公钥失败 HTTP ' + pkRes.status); process.exit(1); }
  const pk = await pkRes.json();
  console.log('PASS  取到仓库公钥 key_id=' + pk.key_id);

  const sealed = g.SA_SEAL(JSON.stringify({ users: cfg.users }), pk.key);
  console.log('      浏览器端密封完成，密文长度 ' + sealed.length);

  const put = await gh('PUT', `/repos/${owner}/${REPO_NAME}/actions/secrets/ALLOWED_USERS_JSON`,
    { encrypted_value: sealed, key_id: pk.key_id });
  if (put.status === 204 || put.status === 201) {
    console.log('PASS  GitHub 接受了浏览器端生成的密文（HTTP ' + put.status + '）');
    console.log('      → 远程模式「保存并部署」链路已验证可用');
    process.exit(0);
  }
  console.log('FAIL  写入密钥失败 HTTP ' + put.status + ' ' + (await put.text()).slice(0, 300));
  process.exit(1);
})();
