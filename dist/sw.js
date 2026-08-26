/* 极简 PWA 离线缓存：仅缓存应用外壳，data.js 始终走网络（保证数据最新）。 */
const CACHE = "sa-shell-v1";
const SHELL = ["index.html", "styles.css", "charts.js", "app.js", "auth.js",
               "nacl.js", "users.json", "meta.json", "icon.svg", "manifest.webmanifest"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (ks) {
    return Promise.all(ks.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  // 动态数据（data.js / data/*.bin）永远走网络
  if (/\/(data(\.js)?|.*\.bin)(\?|$)/.test(url.pathname)) {
    return e.respondWith(fetch(req).catch(function () { return caches.match("index.html"); }));
  }
  // 外壳资源：缓存优先，离线可用
  e.respondWith(caches.match(req).then(function (hit) {
    if (hit) return hit;
    return fetch(req).then(function (res) {
      var copy = res.clone();
      caches.open(CACHE).then(function (c) { c.put(req, copy); });
      return res;
    });
  }));
});
