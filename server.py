# AstroMap — Servidor de producción (solo astrocartografía)
# Este archivo es INDEPENDIENTE del server.py de trading.
# Se usa para desplegar en Render.com / Railway / cualquier hosting.

import os
import sys
import logging
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ── UTF-8 en cualquier SO ────────────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ── APP ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Orbitae — Astrocartografía Interactiva",
    description="Calculadora de líneas de nacimiento planetarias.",
    version="1.0.0"
)

# CORS (necesario para que la PWA funcione desde cualquier origen)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── MIDDLEWARE: No-cache para HTML, cache para assets estáticos ───────────────
@app.middleware("http")
async def cache_control(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    # HTML y Service Worker siempre frescos
    if path.endswith(".html") or "sw.js" in path or path in ["/", "/astro", "/privacidad"]:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    # Assets estáticos: cachear 7 días
    elif path.endswith((".js", ".css", ".png", ".jpg", ".svg", ".woff2", ".json")):
        if "icon" not in path:  # Los íconos pueden cambiar, cachear menos
            response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response

# ── GEOCODING PROXY CON CACHE ─────────────────────────────────────────────────
import urllib.request
import urllib.parse
import json
import threading

CACHE_FILE = "geocode_cache.json"
geocode_cache = {}
cache_lock = threading.Lock()

if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            geocode_cache = json.load(f)
    except Exception as e:
        logger.error(f"Error loading geocode cache: {e}")

def save_cache():
    try:
        with cache_lock:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(geocode_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving geocode cache: {e}")

@app.get("/api/geocode")
async def api_geocode(q: str):
    query = q.strip().lower()
    if not query:
        return []
    
    # Intentar obtener de cache
    if query in geocode_cache:
        logger.info(f"Geocode cache hit for query: '{query}'")
        return geocode_cache[query]
    
    # Consultar Nominatim con User-Agent custom para evitar bloqueos
    url = f"https://nominatim.openstreetmap.org/search?format=json&limit=10&q={urllib.parse.quote(q)}"
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Orbitae/1.0 (https://orbitae.app; contact@orbitae.app)'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            # Guardar en cache
            geocode_cache[query] = data
            save_cache()
            logger.info(f"Geocode fetched and cached for: '{query}'")
            return data
    except Exception as e:
        logger.error(f"Geocoding error for '{q}': {e}")
        return []

@app.get("/api/reverse")
async def api_reverse(lat: float, lon: float):
    cache_key = f"rev_{lat:.4f}_{lon:.4f}"
    if cache_key in geocode_cache:
        logger.info(f"Reverse geocode cache hit for key: {cache_key}")
        return geocode_cache[cache_key]
    
    url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10"
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Orbitae/1.0 (https://orbitae.app; contact@orbitae.app)'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            geocode_cache[cache_key] = data
            save_cache()
            logger.info(f"Reverse geocode fetched and cached for key: {cache_key}")
            return data
    except Exception as e:
        logger.error(f"Reverse geocoding error for ({lat}, {lon}): {e}")
        return {}

# ── ARCHIVOS ESTÁTICOS ────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── RUTAS ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """Redirige la raíz directamente a la app de astro."""
    return FileResponse("static/index.html")


@app.get("/astro")
async def astro_app():
    """Ruta limpia para la PWA — requerida para TWA (Play Store)."""
    return FileResponse("static/index.html")


@app.get("/privacidad")
async def privacy_policy():
    """Política de privacidad — requerida por Google Play y Apple App Store."""
    html = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Política de Privacidad — Orbitae</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Segoe UI', sans-serif; background: #0a0a10; color: #e0e0f0;
               padding: 32px 20px; max-width: 720px; margin: 0 auto; line-height: 1.7; }
        h1 { font-size: 26px; color: #ba55d3; margin-bottom: 8px; }
        h2 { font-size: 18px; color: #9c7cd6; margin: 28px 0 8px; }
        p, li { font-size: 15px; color: #a0a0b8; margin-bottom: 10px; }
        ul { padding-left: 20px; }
        .badge { display: inline-block; background: rgba(156,39,176,0.15);
                 border: 1px solid rgba(156,39,176,0.3); color: #ba55d3;
                 padding: 4px 12px; border-radius: 20px; font-size: 12px; margin-bottom: 24px; }
        a { color: #ba55d3; }
        code { background: rgba(255,255,255,0.05); padding: 2px 6px; border-radius: 4px; font-size: 13px; }
    </style>
</head>
<body>
    <h1>🌟 Política de Privacidad</h1>
    <span class="badge">Orbitae — Astrocartografía Interactiva</span>
    <p><strong>Última actualización:</strong> Junio 2025</p>

    <h2>1. Información que recopilamos</h2>
    <p>Orbitae <strong>no recopila, almacena ni transmite</strong> ningún dato personal a
    servidores externos. Todos los cálculos astrológicos se realizan localmente en tu dispositivo.</p>
    <ul>
        <li><strong>Datos de nacimiento</strong> (fecha, hora, lugar): se guardan únicamente en el
        almacenamiento local de tu dispositivo (<code>localStorage</code>) y nunca se envían a ningún servidor.</li>
        <li><strong>Ubicación GPS</strong>: solo se usa si vos elegís "Usar mi ubicación actual".
        No se almacena ni se envía a ningún servidor.</li>
        <li><strong>Datos de uso</strong>: no usamos analíticas, cookies de seguimiento ni publicidad.</li>
    </ul>

    <h2>2. Servicios de terceros</h2>
    <ul>
        <li><strong>OpenStreetMap / Leaflet</strong>: para mostrar el mapa interactivo.</li>
        <li><strong>Nominatim (OSM)</strong>: para buscar ciudades por nombre. Solo se envía el texto escrito.</li>
        <li><strong>Google Fonts</strong>: para las tipografías. Se descarga la fuente la primera vez.</li>
        <li><strong>cdnjs (Cloudflare)</strong>: para Font Awesome y jsPDF. Solo se descargan los archivos.</li>
    </ul>

    <h2>3. Seguridad y datos locales</h2>
    <p>Los datos guardados en tu dispositivo son accesibles solo por esta aplicación.
    No realizamos copias de seguridad en la nube. Podés borrar todos tus datos en cualquier
    momento desde la pestaña <strong>Origen → Borrar mis datos</strong>.</p>

    <h2>4. Menores de edad</h2>
    <p>Esta aplicación no está dirigida a menores de 13 años.</p>

    <h2>5. Cambios a esta política</h2>
    <p>Cualquier cambio se publicará en esta misma página con la fecha actualizada.</p>

    <h2>6. Contacto</h2>
    <p>Para consultas sobre privacidad, contactanos a través de la página de la aplicación
    en Google Play Store.</p>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/health")
async def health():
    """Endpoint de salud para Render — evita que el servicio duerma por inactividad."""
    logger.info("❤️ Health check ping received")
    return {"status": "ok", "app": "Orbitae"}


# ── INICIO ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🌟 Orbitae iniciando en puerto {port}")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
