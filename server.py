# Orbitae — Servidor de producción (solo astrocartografía)
# Este archivo es INDEPENDIENTE del server.py de trading.
# Se usa para desplegar en Render.com / Railway / cualquier hosting.

import os
import sys
import logging
import uuid
import hashlib
import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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
    allow_methods=["GET", "POST", "OPTIONS"],
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

# ── SISTEMA DE TOKENS PREMIUM ───────────────────────────────────────────────
TOKENS_FILE = "premium_tokens.json"
tokens_lock = threading.Lock()

# Contraseña de administrador (cambiala por una segura en producción)
ADMIN_SECRET = os.environ.get("ORBITAE_ADMIN_SECRET", "orbitae-admin-2025")

def load_tokens():
    """Carga el archivo de tokens desde disco."""
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading tokens: {e}")
    return {}

def save_tokens(tokens: dict):
    """Guarda el archivo de tokens en disco."""
    try:
        with tokens_lock:
            with open(TOKENS_FILE, "w", encoding="utf-8") as f:
                json.dump(tokens, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving tokens: {e}")

@app.get("/api/admin/create-token")
async def admin_create_token(secret: str, email: str = "", ref: str = ""):
    """
    Endpoint de administrador para crear un token de activación único.
    Uso: GET /api/admin/create-token?secret=TU_CLAVE&email=cliente@email.com&ref=PAGO123
    """
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    
    token = str(uuid.uuid4()).replace("-", "").upper()[:16]
    tokens = load_tokens()
    tokens[token] = {
        "created_at": datetime.datetime.utcnow().isoformat(),
        "email": email,
        "payment_ref": ref,
        "used": False,
        "used_at": None
    }
    save_tokens(tokens)
    logger.info(f"Token creado: {token} para email={email} ref={ref}")
    return {"token": token, "email": email, "ref": ref, "status": "created"}

@app.post("/api/activate")
async def activate_premium(request: Request):
    """
    Valida un token de activación. El token es de un solo uso.
    Body JSON: { "token": "XXXXXXXXXXXXXXXX" }
    """
    try:
        data = await request.json()
        token = str(data.get("token", "")).strip().upper()
    except Exception:
        return JSONResponse({"valid": False, "error": "Formato inválido"}, status_code=400)
    
    if not token:
        return JSONResponse({"valid": False, "error": "Token vacío"}, status_code=400)
    
    tokens = load_tokens()
    
    if token not in tokens:
        logger.warning(f"Token inválido intentado: {token}")
        return JSONResponse({"valid": False, "error": "Token no reconocido"})
    
    token_data = tokens[token]
    
    if token_data.get("used"):
        logger.warning(f"Token ya usado: {token}")
        return JSONResponse({"valid": False, "error": "Este token ya fue utilizado"})
    
    # Marcar como usado
    tokens[token]["used"] = True
    tokens[token]["used_at"] = datetime.datetime.utcnow().isoformat()
    save_tokens(tokens)
    
    logger.info(f"Token activado exitosamente: {token} (email={token_data.get('email')})")
    return JSONResponse({"valid": True, "message": "Premium activado correctamente"})

@app.post("/api/mp/create-preference")
async def create_mp_preference(request: Request):
    import requests
    
    # Access Token de Producción para Orbitae
    MP_ACCESS_TOKEN = "APP_USR-2055215287777718-061112-2e1047c21eeff9813b1474c0d8090923-172306186"
    
    base_url = "https://orbitae-app.onrender.com"
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {
        "items": [
            {
                "title": "Orbitae Premium - Acceso Ilimitado",
                "quantity": 1,
                "unit_price": 4500,
                "currency_id": "ARS"
            }
        ],
        "back_urls": {
            "success": base_url,
            "failure": base_url,
            "pending": base_url
        },
        "auto_return": "approved"
    }
    
    try:
        res = requests.post(url, headers=headers, json=body, timeout=5)
        res.raise_for_status()
        data = res.json()
        return {"init_point": data.get("init_point")}
    except Exception as e:
        logger.error(f"Error al crear preferencia de Mercado Pago: {e}")
        return JSONResponse({"error": "No se pudo iniciar el pago con Mercado Pago"}, status_code=500)

@app.get("/api/admin/tokens")
async def admin_list_tokens(secret: str):
    """Lista todos los tokens (solo admin)."""
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    tokens = load_tokens()
    return {"total": len(tokens), "tokens": tokens}

# ────────────────────────────────────────────────────────────────────────────

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
        .highlight { background: rgba(156,39,176,0.1); border: 1px solid rgba(156,39,176,0.25);
                     border-radius: 10px; padding: 14px 18px; margin: 16px 0; }
        .back-btn { display: inline-block; margin-top: 32px; padding: 10px 24px;
                    background: rgba(156,39,176,0.2); border: 1px solid rgba(156,39,176,0.4);
                    color: #ba55d3; border-radius: 8px; text-decoration: none; font-size: 14px; }
    </style>
</head>
<body>
    <h1>&#127775; Política de Privacidad</h1>
    <span class="badge">Orbitae — Astrocartografía Interactiva</span>
    <p><strong>Última actualización:</strong> Junio 2026</p>

    <h2>1. Información que recopilamos</h2>
    <p>Orbitae <strong>no recopila, almacena ni transmite</strong> ningún dato personal a
    servidores externos. Todos los cálculos astrológicos se realizan localmente en tu dispositivo.</p>
    <ul>
        <li><strong>Datos de nacimiento</strong> (fecha, hora, lugar): se guardan únicamente en el
        almacenamiento local de tu dispositivo (<code>localStorage</code>) y nunca se envían a ningún servidor.</li>
        <li><strong>Ubicación GPS</strong>: solo se usa si elegís "Usar mi ubicación actual".
        No se almacena ni se envía a ningún servidor.</li>
        <li><strong>Nombre de usuario</strong>: se guarda localmente en tu dispositivo solo para personalizar el saludo.</li>
        <li><strong>Datos de uso</strong>: no usamos analíticas, cookies de seguimiento ni publicidad.</li>
    </ul>

    <h2>2. Activación Premium</h2>
    <div class="highlight">
        <p>Al adquirir el plan Premium, enviás tu código de compra a nuestro servidor únicamente
        para validar que el pago es legítimo. El servidor <strong>no guarda</strong> datos personales,
        solo el código de transacción de pago para evitar su reutilización.</p>
        <p style="margin-top:8px;">Una vez activado, el estado Premium se guarda en tu dispositivo.
        Si borrás los datos de la app, deberás reactivar con tu código original.</p>
    </div>

    <h2>3. Servicios de terceros</h2>
    <ul>
        <li><strong>OpenStreetMap / Leaflet</strong>: para mostrar el mapa interactivo.</li>
        <li><strong>Nominatim (OSM)</strong>: para buscar ciudades por nombre. Solo se envía el texto buscado.</li>
        <li><strong>Mercado Pago / PayPal</strong>: procesamiento de pagos. Consultar sus políticas de privacidad.</li>
        <li><strong>Google Fonts / cdnjs</strong>: para tipografías e íconos. Solo se descargan los archivos.</li>
    </ul>

    <h2>4. Seguridad y datos locales</h2>
    <p>Los datos guardados en tu dispositivo son accesibles solo por esta aplicación.
    No realizamos copias de seguridad en la nube. Podés borrar todos tus datos en cualquier
    momento desde la pestaña <strong>Mi Carta → Borrar mis datos</strong>.</p>

    <h2>5. Menores de edad</h2>
    <p>Esta aplicación no está dirigida a menores de 13 años y no recopilamos intencionalmente
    información de menores de esa edad.</p>

    <h2>6. Cambios a esta política</h2>
    <p>Cualquier cambio material se publicará en esta misma página con la fecha actualizada.
    El uso continuado de la aplicación implica la aceptación de la política vigente.</p>

    <h2>7. Contacto</h2>
    <p>Para consultas sobre privacidad o solicitudes de eliminación de datos:</p>
    <ul>
        <li>Email: <a href="mailto:orbitae.app@gmail.com">orbitae.app@gmail.com</a></li>
        <li>Asunto: "Privacidad — [tu solicitud]"</li>
    </ul>

    <a href="/" class="back-btn">&#8592; Volver a la App</a>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/api/log-error")
async def log_error(request: Request):
    try:
        data = await request.json()
        logger.error(f"❌ CLIENT ERROR: {data.get('message')} at {data.get('filename')}:{data.get('lineno')}:{data.get('colno')}\nStack: {data.get('error')}")
    except Exception as e:
        logger.error(f"Error parsing client error: {e}")
    return {"status": "ok"}


@app.middleware("http")
async def add_post_cors(request: Request, call_next):
    """Permitir POST desde la PWA para el endpoint /api/activate."""
    response = await call_next(request)
    if request.url.path == "/api/activate":
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request):
    """Endpoint de salud para Render — evita que el servicio duerma por inactividad."""
    logger.info(f"❤️ Health check ping received ({request.method})")
    return {"status": "ok", "app": "Orbitae"}


# ── INICIO ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🌟 Orbitae iniciando en puerto {port}")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
