/**
 * stock-analysis 管理代理 Worker（v2）
 * ------------------------------------------------------------------
 * 作用：把 GitHub 令牌留在服务端，浏览器只发「管理密钥(ADMIN_KEY)」+ 已加密的名单/持仓。
 *       Worker 用服务端令牌调用 GitHub API 写 Secrets + 触发构建。浏览器永不接触 GitHub 令牌。
 *
 * v2 安全加固：
 *   · Secret 白名单：只允许写 ALLOWED_USERS_JSON / HOLDINGS_JSON（防止 ADMIN_KEY 泄露后覆盖其他 Secret）
 *   · IP 限速：每分钟最多 20 次请求；管理密钥连续错 5 次锁定该 IP 10 分钟（防暴力破解）
 *   · 常量时间比较：防 timing attack
 *   · 审计日志：每次请求记一行，Cloudflare 仪表盘 → Workers → 日志 可查
 *
 * 部署（一次性）：
 *   1) npm i -g wrangler
 *   2) wrangler login
 *   3) cd tools/worker && wrangler deploy
 *   4) 配置私密变量：
 *        wrangler secret put GH_TOKEN      # 你的 GitHub PAT（repo + Secrets写 + Actions写）
 *        wrangler secret put ADMIN_KEY     # 你自定的管理密码（站点「管理密钥」里填这个）
 *   5) 记下分配的 *.workers.dev 地址，填进 dist/app.js 的 WORKER_URL（或站点面板里的代理设置）。
 *
 * 环境变量（非私密，可写在 wrangler.toml [vars]）：
 *   REPO         默认 fisk9r/stock-analysis
 *   ALLOW_ORIGIN 默认 https://fisk9r.github.io （限制只允许本站调用，防他人乱用）
 */

const GITHUB_API = 'https://api.github.com';

// 只允许写这三个 Secret——其余一概拒绝（仓库里还有 SC 密钥、GH_PAT 等，绝不能被覆盖）
// 2026-09-01 补 WATCH_JSON：站点「⭐ 管理关注股」云端同步写的就是它，
// 此前不在白名单 → 走 Worker 的用户一律 403 "secret not allowed"（表现为无法添加自选股）。
const SECRET_WHITELIST = new Set(['ALLOWED_USERS_JSON', 'HOLDINGS_JSON', 'WATCH_JSON']);

// —— 内存限速（实例重启即清零，但足够挡住脚本小子和误操作）——
const buckets = new Map(); // ip -> {n, t}     每 60s 一个窗口，窗口内 >20 次则拒
const fails = new Map();   // ip -> {n, lock}   连续错 5 次 → 锁 10 分钟

function tooFast(ip) {
  const now = Date.now();
  let b = buckets.get(ip);
  if (!b || now - b.t > 60000) { b = { n: 0, t: now }; buckets.set(ip, b); }
  b.n += 1;
  return b.n > 20;
}
function isLocked(ip) {
  const f = fails.get(ip);
  return !!(f && f.lock && Date.now() < f.lock);
}
function recordFail(ip) {
  const f = fails.get(ip) || { n: 0, lock: 0 };
  f.n += 1;
  if (f.n >= 5) { f.lock = Date.now() + 10 * 60 * 1000; f.n = 0; }
  fails.set(ip, f);
}

// 常量时间比较（长度也参与 diff，不提前返回）
function safeEqual(a, b) {
  const ab = new TextEncoder().encode(String(a));
  const bb = new TextEncoder().encode(String(b));
  const n = Math.max(ab.length, bb.length);
  let diff = ab.length ^ bb.length;
  for (let i = 0; i < n; i++) diff |= (ab[i] || 0) ^ (bb[i] || 0);
  return diff === 0;
}

function corsHeaders(origin) {
  return {
    'Access-Control-Allow-Origin': origin || '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
    'Cache-Control': 'no-store'
  };
}

function json(obj, status, headers) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: Object.assign({ 'Content-Type': 'application/json' }, headers || {})
  });
}

export default {
  async fetch(req, env) {
    const origin = req.headers.get('Origin') || '';
    const allow = env.ALLOW_ORIGIN || '*';
    const allowOrigin = allow === '*' ? (origin || '*') : (origin === allow ? allow : 'null');
    const ip = req.headers.get('CF-Connecting-IP') || 'unknown';

    if (req.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(allowOrigin) });
    }
    if (req.method !== 'POST') {
      return json({ error: 'method not allowed' }, 405, corsHeaders(allowOrigin));
    }
    if (tooFast(ip)) {
      console.log(JSON.stringify({ w: 'sa-admin', ev: 'rate_limited', ip }));
      return json({ error: 'too many requests' }, 429, corsHeaders(allowOrigin));
    }
    if (isLocked(ip)) {
      console.log(JSON.stringify({ w: 'sa-admin', ev: 'locked', ip }));
      return json({ error: 'temporarily locked (too many wrong keys), try in 10 min' }, 403, corsHeaders(allowOrigin));
    }

    let body;
    try {
      body = await req.json();
    } catch (e) {
      return json({ error: 'bad json' }, 400, corsHeaders(allowOrigin));
    }

    // 第一道闸：管理密钥（常量时间比较；连续错 5 次锁 IP 10 分钟）
    if (!body.admin_key || !safeEqual(body.admin_key, env.ADMIN_KEY || '')) {
      recordFail(ip);
      console.log(JSON.stringify({ w: 'sa-admin', ev: 'bad_key', ip, action: body.action || '' }));
      return json({ error: 'unauthorized' }, 403, corsHeaders(allowOrigin));
    }
    fails.delete(ip);

    const repo = env.REPO || 'fisk9r/stock-analysis';
    const ghHeaders = {
      'Authorization': 'Bearer ' + env.GH_TOKEN,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'Content-Type': 'application/json'
    };

    try {
      // 连通性测试：验证 Worker 在线 + 密钥正确 + 令牌可用
      if (body.action === 'ping') {
        console.log(JSON.stringify({ w: 'sa-admin', ev: 'ping', ip }));
        return json({ ok: true, repo: repo }, 200, corsHeaders(allowOrigin));
      }

      if (body.action === 'public-key') {
        const r = await fetch(GITHUB_API + '/repos/' + repo + '/actions/secrets/public-key', { headers: ghHeaders });
        console.log(JSON.stringify({ w: 'sa-admin', ev: 'public-key', ip, status: r.status }));
        return json(await r.json(), r.status, corsHeaders(allowOrigin));
      }

      if (body.action === 'put-secret') {
        // 第二道闸：Secret 名白名单
        if (!body.secret_name || !SECRET_WHITELIST.has(body.secret_name)) {
          console.log(JSON.stringify({ w: 'sa-admin', ev: 'secret_blocked', ip, name: body.secret_name || '' }));
          return json({ error: 'secret not allowed' }, 403, corsHeaders(allowOrigin));
        }
        if (!body.encrypted_value || !body.key_id) {
          return json({ error: 'missing fields' }, 400, corsHeaders(allowOrigin));
        }
        const r = await fetch(GITHUB_API + '/repos/' + repo + '/actions/secrets/' + encodeURIComponent(body.secret_name), {
          method: 'PUT',
          headers: ghHeaders,
          body: JSON.stringify({ encrypted_value: body.encrypted_value, key_id: body.key_id })
        });
        console.log(JSON.stringify({ w: 'sa-admin', ev: 'put-secret', ip, name: body.secret_name, status: r.status }));
        if (!r.ok) return json({ error: 'put-secret failed', detail: await r.text() }, r.status, corsHeaders(allowOrigin));
        return json({ ok: true }, 200, corsHeaders(allowOrigin));
      }

      if (body.action === 'dispatch') {
        const r = await fetch(GITHUB_API + '/repos/' + repo + '/actions/workflows/stock.yml/dispatches', {
          method: 'POST',
          headers: ghHeaders,
          body: JSON.stringify({ ref: 'main', inputs: { task: 'build' } })
        });
        console.log(JSON.stringify({ w: 'sa-admin', ev: 'dispatch', ip, status: r.status }));
        if (!r.ok) return json({ error: 'dispatch failed', detail: await r.text() }, r.status, corsHeaders(allowOrigin));
        return json({ ok: true }, 200, corsHeaders(allowOrigin));
      }

      return json({ error: 'unknown action' }, 400, corsHeaders(allowOrigin));
    } catch (e) {
      console.log(JSON.stringify({ w: 'sa-admin', ev: 'error', ip, detail: String(e) }));
      return json({ error: 'worker error', detail: String(e) }, 500, corsHeaders(allowOrigin));
    }
  }
};
