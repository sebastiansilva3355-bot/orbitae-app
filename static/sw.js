// Orbitae Service Worker — Offline Cache v34
const CACHE_NAME = 'orbitae-v34';

// Archivos a cachear al instalar (App Shell)
// NOTA: index.html NO está aquí — siempre se sirve directo del servidor (no-cache)
const PRECACHE_ASSETS = [
    '/static/leaflet.css',
    '/static/leaflet.js',
    '/static/astronomy.min.js',
    '/static/astro_lines.json',
    '/static/manifest.json',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    'https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;800&family=Inter:wght@300;400;500;600;700&display=swap',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css'
];

// Al instalar: cachear el app shell
self.addEventListener('install', (event) => {
    self.skipWaiting();
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            // Cachear archivos críticos (si falla alguno, continuar igual)
            return Promise.allSettled(
                PRECACHE_ASSETS.map(url => cache.add(url).catch(e => console.warn('No se pudo cachear:', url, e)))
            );
        })
    );
});

// Al activar: limpiar caches viejos
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(
                keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
            )
        ).then(() => self.clients.claim())
    );
});

// Estrategia: Cache First para assets estáticos, Network First para datos
self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Solo manejar GET
    if (event.request.method !== 'GET') return;

    // Estrategia Network First para rutas de API del servidor
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/alerts')) {
        event.respondWith(
            fetch(event.request)
                .catch(() => new Response(JSON.stringify({ error: 'Sin conexión' }), {
                    headers: { 'Content-Type': 'application/json' }
                }))
        );
        return;
    }

    // Nunca cachear index.html — siempre red para tener PDF generator actualizado
    if (url.pathname === '/static/index.html' || url.pathname === '/astro' || url.pathname === '/') {
        event.respondWith(fetch(event.request));
        return;
    }

    // Estrategia Cache First para assets estáticos (leaflet, astronomy, fonts, íconos)
    const isStaticAsset = url.pathname.match(/\.(js|css|png|jpg|jpeg|svg|woff2?|ttf)$/) ||
                          url.hostname.includes('cdnjs.cloudflare.com') ||
                          url.hostname.includes('fonts.gstatic.com') ||
                          url.hostname.includes('fonts.googleapis.com');

    if (isStaticAsset) {
        event.respondWith(
            caches.match(event.request).then(cached => {
                return cached || fetch(event.request).then(response => {
                    // Guardar en cache para próxima vez
                    if (response.ok) {
                        const copy = response.clone();
                        caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
                    }
                    return response;
                }).catch(() => cached); // Si falla la red, usar cache
            })
        );
        return;
    }

    // Estrategia Network First con fallback a cache para el resto
    event.respondWith(
        fetch(event.request)
            .then(response => {
                if (response.ok) {
                    const copy = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
                }
                return response;
            })
            .catch(() => caches.match(event.request))
    );
});
