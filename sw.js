/* 防災トラベルナビ  Service Worker
   - アプリ本体: cache first（更新は新バージョンのSWで差し替え）
   - data/*.json（地域・火山の全国マスターデータ）: network first → 失敗時キャッシュ
   - 気象庁・国土地理院への直接リクエストはSWを素通りさせる（アプリ側のIndexedDB/localStorageが担当）
   - 地図タイル: ここでは扱わない（アプリ側の IndexedDB が担当）
*/
const VERSION = 'v1.0.0';
const SHELL = `shell-${VERSION}`;
const DATA  = `data-${VERSION}`;

const SHELL_FILES = [
  './',
  './index.html',
  './manifest.json',
  './data/regions.json',
  './data/volcanoes.json',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon.png',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon-2x.png',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png'
];

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    const c = await caches.open(SHELL);
    await Promise.allSettled(SHELL_FILES.map(u => c.add(new Request(u, { cache: 'reload' }))));
    self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => !k.endsWith(VERSION)).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('message', e => {
  if (e.data === 'skipWaiting') self.skipWaiting();
  if (e.data === 'clearAll') {
    caches.keys().then(ks => Promise.all(ks.map(k => caches.delete(k))));
  }
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // 気象庁・国土地理院のタイルはSWを通さない（IndexedDB側で管理）
  if (/(^|\.)jma\.go\.jp$/.test(url.hostname) || /gsi\.go\.jp$/.test(url.hostname)) return;
  // Google Maps の埋め込みはキャッシュしない（規約上オフライン保存不可）
  if (/google\.com$/.test(url.hostname)) return;

  if (url.pathname.includes('/data/') && url.pathname.endsWith('.json')) {
    e.respondWith((async () => {
      try {
        const res = await fetch(req, { cache: 'no-store' });
        const c = await caches.open(DATA);
        c.put(req, res.clone());
        return res;
      } catch (err) {
        const hit = await caches.match(req);
        if (hit) return hit;
        return new Response('{}', { headers: { 'Content-Type': 'application/json' } });
      }
    })());
    return;
  }

  e.respondWith((async () => {
    const hit = await caches.match(req);
    if (hit) return hit;
    try {
      const res = await fetch(req);
      if (res.ok && (url.origin === location.origin || url.hostname === 'cdnjs.cloudflare.com')) {
        const c = await caches.open(SHELL);
        c.put(req, res.clone());
      }
      return res;
    } catch (err) {
      if (req.mode === 'navigate') return caches.match('./index.html');
      throw err;
    }
  })());
});
