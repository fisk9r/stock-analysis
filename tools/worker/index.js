/**
 * stock-analysis 管理代理 Worker
 * ------------------------------------------------------------------
 * 作用：把 GitHub 令牌留在服务端，浏览器只发「管理密钥(ADMIN_KEY)」+ 已加密的名单/持仓。
 *       Worker 用服务端令牌调用 GitHub API 写 Secrets + 触发构建。浏览器永不接触 GitHub 令牌。
 *
 * 部署（一次性）：
 *   1) npm i -g wrangler
 *   2) wrangler login
 *   3) cd tools/worker && wrangler deploy
 *   4) 配置私密变量（见下方）：
 *        wrangler secret put GH_TOKEN      # 你的 GitHub PAT（repo + Secrets写 + Actions写）
 *        wrangler secret put ADMIN_KEY     # 你自定的管理密码（站点「管理密钥」里填这个）
 *   5) 记下分配的 *.workers.dev 地址，填进 dist/app.js 的 WORKER_URL。
 *
 * 环境变量（非私密，可直接写 wrangler.toml）：
 *   REPO         默认 fisk9r/stock-analysis
 *   ALLOW_ORIGIN 默认 https://fisk9r.github.io （限制只允许本站调用，防他人乱用）
 */

const GITHUB_API = 'https://api.github.com';

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
    const url = new URL(req.url);
    const origin = req.headers.get('Origin') || '';
    const allow = env.ALLOW_ORIGIN || '*';
    const allowOrigin = allow === '*' ? (origin || '*') : (origin === allow ? allow : 'null');

    if (req.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(allowOrigin) });
    }
    if (req.method !== 'POST') {
      return json({ error: 'method not allowed' }, 405, corsHeaders(allowOrigin));
    }

    let body;
    try {
      body = await req.json();
    } catch (e) {
      return json({ error: 'bad json' }, 400, corsHeaders(allowOrigin));
    }

    // 第一道闸：管理密钥。错的直接 403，不碰 GitHub。
    if (!body.admin_key || body.admin_key !== env.ADMIN_KEY) {
      return json({ error: 'unauthorized' }, 403, corsHeaders(allowOrigin));
    }

    const repo = env.REPO || 'fisk9r/stock-analysis';
    const ghHeaders = {
      'Authorization': 'Bearer ' + env.GH_TOKEN,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'Content-Type': 'application/json'
    };

    try {
      if (body.action === 'public-key') {
        const r = await fetch(GITHUB_API + '/repos/' + repo + '/actions/secrets/public-key', { headers: ghHeaders });
        return json(await r.json(), r.status, corsHeaders(allowOrigin));
      }

      if (body.action === 'put-secret') {
        if (!body.secret_name || !body.encrypted_value || !body.key_id) {
          return json({ error: 'missing fields' }, 400, corsHeaders(allowOrigin));
        }
        const r = await fetch(GITHUB_API + '/repos/' + repo + '/actions/secrets/' + encodeURIComponent(body.secret_name), {
          method: 'PUT',
          headers: ghHeaders,
          body: JSON.stringify({ encrypted_value: body.encrypted_value, key_id: body.key_id })
        });
        if (!r.ok) return json({ error: 'put-secret failed', detail: await r.text() }, r.status, corsHeaders(allowOrigin));
        return json({ ok: true }, 200, corsHeaders(allowOrigin));
      }

      if (body.action === 'dispatch') {
        const r = await fetch(GITHUB_API + '/repos/' + repo + '/actions/workflows/stock.yml/dispatches', {
          method: 'POST',
          headers: ghHeaders,
          body: JSON.stringify({ ref: 'main', inputs: { task: 'build' } })
        });
        if (!r.ok) return json({ error: 'dispatch failed', detail: await r.text() }, r.status, corsHeaders(allowOrigin));
        return json({ ok: true }, 200, corsHeaders(allowOrigin));
      }

      return json({ error: 'unknown action' }, 400, corsHeaders(allowOrigin));
    } catch (e) {
      return json({ error: 'worker error', detail: String(e) }, 500, corsHeaders(allowOrigin));
    }
  }
};
