// Service Worker v2 — 鹿児島防災ナビ
const CACHE = 'kagoshima-bousai-v2';
const ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icon.svg',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'
];

// インストール：コアアセットをキャッシュ
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(ASSETS).catch(() => {}))
  );
  self.skipWaiting();
});

// アクティベート：古いキャッシュを削除
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// フェッチ戦略
self.addEventListener('fetch', e => {
  const url = e.request.url;

  // 地図タイル → Network First（キャッシュ）
  if (url.includes('tile.openstreetmap.org') || url.includes('arcgisonline.com')) {
    e.respondWith(
      caches.open(CACHE + '-tiles').then(tileCache =>
        tileCache.match(e.request).then(cached => {
          if (cached) return cached;
          return fetch(e.request).then(res => {
            if (res && res.status === 200) tileCache.put(e.request, res.clone());
            return res;
          }).catch(() => cached);
        })
      )
    );
    return;
  }

  // 気象庁iframe → ネットワーク優先（オフライン時はキャッシュ）
  if (url.includes('jma.go.jp')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }

  // OSRM ルーティング → ネットワークのみ（オフライン時はエラー）
  if (url.includes('project-osrm.org')) {
    e.respondWith(fetch(e.request).catch(() =>
      new Response(JSON.stringify({ code: 'Error', message: 'offline' }), {
        headers: { 'Content-Type': 'application/json' }
      })
    ));
    return;
  }

  // アプリ本体 (HTML/JS/CSS) → キャッシュ優先
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(res => {
        if (!res || res.status !== 200 || res.type === 'opaque') return res;
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      });
    })
  );
});

// メッセージ受信（強制更新）
self.addEventListener('message', e => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});
