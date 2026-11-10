/**
 * Cloudflare Worker —— 信号看板「更新」按钮的触发代理
 *
 * 作用：浏览器点「更新」→ 调这个 Worker → Worker 用私密 token 触发 GitHub Actions
 * 好处：token 只存在 Worker 的环境变量里，页面源码中完全看不到
 *
 * 部署步骤（网页操作，3 分钟）：
 *   1. 打开 https://dash.cloudflare.com → Workers & Pages → Create → Worker
 *   2. 粘贴本文件内容，Deploy
 *   3. Settings → Variables → Add variable
 *        名称: GH_TOKEN   值: <你的 classic token>   类型: Secret
 *        名称: REPO       值: fujianxin1408/signal-dashboard
 *   4. 复制 Worker 地址（形如 https://xxx.workers.dev）
 *   5. 把地址填到 signal-site/page.js 里的 WORKER_URL
 */

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS });
    }

    // 限流：同一 IP 60 秒内只能触发一次
    const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
    const now = Date.now();
    const last = await env.KV?.get('t:' + ip);
    if (last && now - Number(last) < 60000) {
      return json({ ok: false, msg: '太频繁，请 1 分钟后再试' }, 429);
    }
    await env.KV?.put('t:' + ip, String(now), { expirationTtl: 120 });

    const slot = 'live';
    const url = `https://api.github.com/repos/${env.REPO}/actions/workflows/signal.yml/dispatches`;

    const r = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `token ${env.GH_TOKEN}`,
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'signal-refresh',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref: 'main', inputs: { slot } }),
    });

    if (r.status === 204) return json({ ok: true, msg: '已触发，约 2 分钟后刷新' });
    const txt = await r.text();
    return json({ ok: false, msg: 'GitHub 返回 ' + r.status, detail: txt.slice(0, 200) }, 502);
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...CORS, 'Content-Type': 'application/json' },
  });
}
