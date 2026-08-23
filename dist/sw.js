/* stock-analysis Service Worker（PWA 离线壳）
 * 策略：
 *   · 静态壳（index/app/styles/charts/nacl/manifest/icon）→ 缓存优先，后台更新（stale-while-revalidate）
 *   · 数据（data.js / data/*.bin / ai_narrative.json / users.json / meta.json）→ 网络优先，失败回退缓存
 *     ——保证有网时永远看最新复盘，断网时仍能看最后一次数据。
 * 版本号随部署手动递增以清空旧静态缓存。
 */
const VER = 'sa-pwa-v1';
const SHELL = [
  './', './index.html', './app.js', './styles.css', './charts.js', './nacl.js',
  './manifest.webmanifest', './icon.svg'
];
const NET_FIRST = [/data\.js/, /\/data\//, /ai_narrative\.json/, /users\.json/, /meta\.json/];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VER).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== VER).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  const isData = NET_FIRST.some((re) => re.test(url.pathname));

  if (isData) {
    // 网络优先：拿到新数据就回填缓存；断网回退缓存
    e.respondWith(
      fetch(e.request).then((resp) => {
        const copy = resp.clone();
        caches.open(VER).then((c) => c.put(e.request, copy));
        return resp;
      }).catch(() => caches.match(e.request))
    );
  } else {
    // 静态壳：缓存优先 + 后台刷新
    e.respondWith(
      caches.match(e.request).then((hit) => {
        const net = fetch(e.request).then((resp) => {
          const copy = resp.clone();
          caches.open(VER).then((c) => c.put(e.request, copy));
          return resp;
        }).catch(() => hit);
        return hit || net;
      })
    );
  }
});
