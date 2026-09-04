// オフラインで使えるようにアプリ本体をキャッシュする
//
// HTML はネットワーク優先。更新したのに古い画面が出続ける事故を防ぐため、
// 通信できるときは必ず最新を取りに行き、失敗したときだけキャッシュを返す。
// それ以外(アイコン等)はキャッシュ優先で速さを取る。
const C = 'kosu-v3';
const FILES = ['./', './index.html', './manifest.webmanifest', './icon.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(C).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks =>
    Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

const isHtml = req =>
  req.mode === 'navigate' ||
  (req.headers.get('accept') || '').includes('text/html');

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;              // 同期の POST は素通しする
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;     // GAS など外部への通信は触らない

  if (isHtml(e.request)) {
    e.respondWith(
      fetch(e.request).then(res => {
        const copy = res.clone();
        caches.open(C).then(c => c.put(e.request, copy)).catch(() => {});
        return res;
      }).catch(() => caches.match(e.request, {ignoreSearch: true})
                       .then(r => r || caches.match('./index.html')))
    );
    return;
  }

  e.respondWith(
    caches.match(e.request, {ignoreSearch: true}).then(r => r || fetch(e.request).then(res => {
      const copy = res.clone();
      caches.open(C).then(c => c.put(e.request, copy)).catch(() => {});
      return res;
    }).catch(() => caches.match('./index.html')))
  );
});
