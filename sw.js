// LangLearn Service Worker v2 — Offline PWA + Push Notifications
const CACHE_NAME    = 'langlearn-v2';
const OFFLINE_URL   = '/';

// Offline uchun cache qilinadigan asosiy fayllar
const PRECACHE = [
  '/',
  '/manifest.json',
];

// ─── Install: asosiy fayllarni cache qilish ──────────────────────────────────
self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(PRECACHE);
    }).then(() => self.skipWaiting())
  );
});

// ─── Activate: eski cache larni tozalash ─────────────────────────────────────
self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      );
    }).then(() => clients.claim())
  );
});

// ─── Fetch: network-first, offline fallback ───────────────────────────────────
self.addEventListener('fetch', function(event) {
  const req = event.request;

  // POST/API so'rovlarni cache qilmaymiz
  if (req.method !== 'GET') return;
  if (req.url.includes('/api/')) return;
  if (req.url.includes('socket.io')) return;

  // Static fayllar: cache-first
  if (req.url.match(/\.(js|css|png|jpg|jpeg|gif|webp|svg|woff|woff2|ico)$/)) {
    event.respondWith(
      caches.match(req).then(function(cached) {
        if (cached) return cached;
        return fetch(req).then(function(res) {
          if (!res || res.status !== 200) return res;
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, clone));
          return res;
        }).catch(() => cached);
      })
    );
    return;
  }

  // HTML sahifalar: network-first, fallback to cache
  event.respondWith(
    fetch(req)
      .then(function(res) {
        if (!res || res.status !== 200 || res.type !== 'basic') return res;
        const clone = res.clone();
        caches.open(CACHE_NAME).then(c => c.put(req, clone));
        return res;
      })
      .catch(function() {
        return caches.match(req).then(cached => {
          if (cached) return cached;
          // Offline sahifa
          return caches.match(OFFLINE_URL);
        });
      })
  );
});

// ─── Push Notifications ───────────────────────────────────────────────────────
self.addEventListener('push', function(event) {
  if (!event.data) return;
  let data = {};
  try { data = event.data.json(); } catch(e) {
    data = { title: 'LangLearn', body: event.data.text() };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || 'LangLearn', {
      body:    data.body  || '',
      icon:    '/static/icons/icon-192.png',
      badge:   '/static/icons/icon-192.png',
      tag:     data.tag || 'langlearn',
      vibrate: [200, 100, 200],
      data:    { url: data.url || '/' },
    })
  );
});

// ─── Notification click ───────────────────────────────────────────────────────
self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(cs) {
      const target = (event.notification.data && event.notification.data.url) || '/';
      for (const c of cs) {
        if (c.url === target && 'focus' in c) return c.focus();
      }
      if (clients.openWindow) return clients.openWindow(target);
    })
  );
});

// ─── Background sync (offline amallar) ───────────────────────────────────────
self.addEventListener('sync', function(event) {
  if (event.tag === 'sync-messages') {
    // Offline paytida yozilgan xabarlarni yuborish
    event.waitUntil(syncOfflineMessages());
  }
});

async function syncOfflineMessages() {
  const db = await openOfflineDB();
  const tx  = db.transaction('pending_msgs', 'readonly');
  const all = await getAllFromStore(tx.objectStore('pending_msgs'));
  for (const msg of all) {
    try {
      await fetch('/api/chat/global', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(msg)
      });
    } catch(e) { /* network error, try next time */ }
  }
}

function openOfflineDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('langlearn-offline', 1);
    req.onupgradeneeded = e => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('pending_msgs')) {
        db.createObjectStore('pending_msgs', { autoIncrement: true });
      }
    };
    req.onsuccess  = e => resolve(e.target.result);
    req.onerror    = e => reject(e.target.error);
  });
}

function getAllFromStore(store) {
  return new Promise((resolve, reject) => {
    const req = store.getAll();
    req.onsuccess = e => resolve(e.target.result || []);
    req.onerror   = e => reject(e.target.error);
  });
}
