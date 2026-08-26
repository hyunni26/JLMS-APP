const CACHE_NAME = "jmo-lms-viewer-v2";
const OFFLINE_ASSETS = [
  "/static/manifest.json",
  "/static/icons/icon-192.png?v=2",
  "/static/icons/icon-512.png?v=2",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(OFFLINE_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// 데이터 화면은 항상 최신이어야 하니 네트워크 우선, 정적 아이콘류만 캐시로 보완
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
