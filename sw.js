// 핏프라이스 서비스워커 — 앱 셸 캐시 + 가격데이터는 항상 최신
const CACHE = "fitprice-v17";
const SHELL = [
  "./", "./index.html",
  "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {})));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const url = e.request.url;
  // app_data.js 는 네트워크 우선(최신 가격), 실패 시 캐시
  if (url.includes("app_data.js")) {
    e.respondWith(
      fetch(e.request).then(r => {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return r;
      }).catch(() => caches.match(e.request))
    );
  } else {
    // 그 외는 캐시 우선
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});

// 알림 클릭 시 앱 열기/포커스
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({ type: "window" }).then(cs => {
      for (const c of cs) { if ("focus" in c) return c.focus(); }
      if (clients.openWindow) return clients.openWindow("./");
    })
  );
});
