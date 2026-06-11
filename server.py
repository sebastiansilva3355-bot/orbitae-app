# Orbitae — Servidor de producción (solo astrocartografía)
# Este archivo es INDEPENDIENTE del server.py de trading.
# Se usa para desplegar en Render.com / Railway / cualquier hosting.

import os
import sys
import logging
import uuid
import hashlib
import datetime
import sqlite3
import threading
from contextlib import contextmanager
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
    version="2.0.0"
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
    if path.endswith(".html") or "sw.js" in path or path in ["/", "/astro", "/privacidad"]:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    elif path.endswith((".js", ".css", ".png", ".jpg", ".svg", ".woff2", ".json")):
        if "icon" not in path:
            response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response

# ── GEOCODING PROXY CON CACHE ─────────────────────────────────────────────────
import urllib.request
import urllib.parse
import json

# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────
MP_ACCESS_TOKEN = os.environ.get(
    "MP_ACCESS_TOKEN",
    "APP_USR-2055215287777718-061112-2e1047c21eeff9813b1474c0d8090923-172306186"
)
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "BAA0Jivw-zoOWaAqsFIMsjyFsCQvBGme7gmQX1fdr_JKoUI_emgoDiqlZ2kn_-0GSC69ngLKCDTiFV6lBc")
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET", "")  # Configurar en variables de entorno de Render
BASE_URL = os.environ.get("BASE_URL", "https://orbitae-app.onrender.com")
ADMIN_SECRET = os.environ.get("ORBITAE_ADMIN_SECRET", "orbitae-admin-2025")

# ── BASE DE DATOS SQLite ──────────────────────────────────────────────────────
DB_FILE = "orbitae_premium.db"
db_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Inicializa las tablas de la base de datos."""
    with db_lock:
        conn = get_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS premium_sessions (
                    session_token TEXT PRIMARY KEY,
                    payment_method TEXT NOT NULL,
                    payment_id TEXT,
                    email TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    active INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS mp_payments (
                    payment_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    preference_id TEXT,
                    session_token TEXT,
                    amount REAL,
                    currency TEXT,
                    created_at TEXT NOT NULL,
                    raw_data TEXT
                );

                CREATE TABLE IF NOT EXISTS paypal_subscriptions (
                    subscription_id TEXT PRIMARY KEY,
                    plan_id TEXT,
                    status TEXT NOT NULL,
                    session_token TEXT,
                    email TEXT,
                    created_at TEXT NOT NULL,
                    raw_data TEXT
                );

                CREATE TABLE IF NOT EXISTS activation_tokens (
                    token TEXT PRIMARY KEY,
                    email TEXT,
                    payment_ref TEXT,
                    used INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    used_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_payment ON premium_sessions(payment_id);
                CREATE INDEX IF NOT EXISTS idx_mp_session ON mp_payments(session_token);
            """)
            conn.commit()
            logger.info("Base de datos inicializada correctamente.")
        except Exception as e:
            logger.error(f"Error inicializando DB: {e}")
        finally:
            conn.close()

# Inicializar la DB al arrancar
init_db()

# ── MIGRACIÓN: cargar tokens JSON legacy a la DB ─────────────────────────────
TOKENS_FILE = "premium_tokens.json"
tokens_lock = threading.Lock()

def load_tokens():
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading tokens: {e}")
    return {}

def save_tokens(tokens: dict):
    try:
        with tokens_lock:
            with open(TOKENS_FILE, "w", encoding="utf-8") as f:
                json.dump(tokens, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving tokens: {e}")

# ── HELPERS PREMIUM ───────────────────────────────────────────────────────────

def create_premium_session(payment_method: str, payment_id: str = None,
                           email: str = None, months: int = 1) -> str:
    """Crea una sesión premium en la DB y devuelve el session_token."""
    session_token = str(uuid.uuid4())
    now = datetime.datetime.utcnow()
    expires = now + datetime.timedelta(days=30 * months)
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO premium_sessions
                   (session_token, payment_method, payment_id, email, created_at, expires_at, active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (session_token, payment_method, payment_id, email,
                 now.isoformat(), expires.isoformat())
            )
            conn.commit()
            logger.info(f"Sesión premium creada: {session_token} método={payment_method} pago={payment_id}")
        finally:
            conn.close()
    return session_token

def is_valid_premium_session(session_token: str) -> bool:
    """Verifica si un session_token es válido y no expiró."""
    if not session_token:
        return False
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT expires_at, active FROM premium_sessions WHERE session_token = ?",
                (session_token,)
            ).fetchone()
        finally:
            conn.close()

    if not row or not row["active"]:
        return False

    expires_at = row["expires_at"]
    if expires_at:
        try:
            exp = datetime.datetime.fromisoformat(expires_at)
            if datetime.datetime.utcnow() > exp:
                return False
        except Exception:
            pass
    return True

# ── ENDPOINTS: SESIÓN PREMIUM ─────────────────────────────────────────────────

@app.post("/api/premium/check")
async def check_premium(request: Request):
    """
    Verifica si un session_token tiene premium activo.
    Body JSON: { "session_token": "uuid" }
    Devuelve: { "premium": true/false }
    """
    try:
        data = await request.json()
        session_token = str(data.get("session_token", "")).strip()
    except Exception:
        return JSONResponse({"premium": False, "error": "Formato inválido"}, status_code=400)

    valid = is_valid_premium_session(session_token)
    return {"premium": valid}

# ── ENDPOINTS: TOKENS DE ACTIVACIÓN ──────────────────────────────────────────

@app.get("/api/admin/create-token")
async def admin_create_token(secret: str, email: str = "", ref: str = ""):
    """
    Endpoint de administrador para crear un token de activación único.
    Uso: GET /api/admin/create-token?secret=TU_CLAVE&email=cliente@email.com&ref=PAGO123
    """
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acceso denegado")

    token = str(uuid.uuid4()).replace("-", "").upper()[:16]
    now = datetime.datetime.utcnow().isoformat()
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO activation_tokens (token, email, payment_ref, used, created_at) VALUES (?, ?, ?, 0, ?)",
                (token, email, ref, now)
            )
            conn.commit()
        finally:
            conn.close()

    # También guardar en JSON legacy por compatibilidad
    tokens = load_tokens()
    tokens[token] = {"created_at": now, "email": email, "payment_ref": ref, "used": False, "used_at": None}
    save_tokens(tokens)

    logger.info(f"Token creado: {token} para email={email} ref={ref}")
    return {"token": token, "email": email, "ref": ref, "status": "created"}

@app.post("/api/activate")
async def activate_premium(request: Request):
    """
    Valida un token de activación manual (de un solo uso).
    Body JSON: { "token": "XXXXXXXXXXXXXXXX" }
    Devuelve: { "valid": true, "session_token": "uuid" }
    """
    try:
        data = await request.json()
        token = str(data.get("token", "")).strip().upper()
    except Exception:
        return JSONResponse({"valid": False, "error": "Formato inválido"}, status_code=400)

    if not token:
        return JSONResponse({"valid": False, "error": "Token vacío"}, status_code=400)

    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM activation_tokens WHERE token = ?", (token,)
            ).fetchone()

            if not row:
                # Fallback: buscar en JSON legacy
                tokens = load_tokens()
                if token not in tokens:
                    logger.warning(f"Token inválido intentado: {token}")
                    return JSONResponse({"valid": False, "error": "Token no reconocido"})
                token_data = tokens[token]
                if token_data.get("used"):
                    return JSONResponse({"valid": False, "error": "Este token ya fue utilizado"})
                # Migrar a DB
                now = datetime.datetime.utcnow().isoformat()
                conn.execute(
                    "INSERT OR IGNORE INTO activation_tokens (token, email, payment_ref, used, created_at) VALUES (?, ?, ?, 0, ?)",
                    (token, token_data.get("email", ""), token_data.get("payment_ref", ""), now)
                )
                conn.commit()
                row = conn.execute("SELECT * FROM activation_tokens WHERE token = ?", (token,)).fetchone()

            if row["used"]:
                logger.warning(f"Token ya usado: {token}")
                return JSONResponse({"valid": False, "error": "Este token ya fue utilizado"})

            # Marcar como usado
            conn.execute(
                "UPDATE activation_tokens SET used = 1, used_at = ? WHERE token = ?",
                (datetime.datetime.utcnow().isoformat(), token)
            )
            conn.commit()

            # Actualizar JSON legacy
            tokens = load_tokens()
            if token in tokens:
                tokens[token]["used"] = True
                tokens[token]["used_at"] = datetime.datetime.utcnow().isoformat()
                save_tokens(tokens)
        finally:
            conn.close()

    # Crear sesión premium server-side
    session_token = create_premium_session(
        payment_method="manual_token",
        payment_id=token,
        email=row["email"] if row else ""
    )

    logger.info(f"Token activado: {token} → sesión {session_token}")
    return JSONResponse({"valid": True, "message": "Premium activado correctamente", "session_token": session_token})

# ── ENDPOINTS: MERCADO PAGO ───────────────────────────────────────────────────

@app.post("/api/mp/create-preference")
async def create_mp_preference(request: Request):
    """Crea una preferencia de pago en Mercado Pago y devuelve el init_point."""
    import requests as req_lib

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
            "success": f"{BASE_URL}/?mp=success",
            "failure": f"{BASE_URL}/?mp=failure",
            "pending": f"{BASE_URL}/?mp=pending"
        },
        "auto_return": "approved",
        "notification_url": f"{BASE_URL}/api/mp/webhook"
    }

    try:
        res = req_lib.post(url, headers=headers, json=body, timeout=10)
        res.raise_for_status()
        data = res.json()
        preference_id = data.get("id", "")
        init_point = data.get("init_point", "")
        logger.info(f"Preferencia MP creada: {preference_id}")
        return {"init_point": init_point, "preference_id": preference_id}
    except Exception as e:
        logger.error(f"Error al crear preferencia de Mercado Pago: {e}")
        return JSONResponse({"error": "No se pudo iniciar el pago con Mercado Pago"}, status_code=500)

@app.post("/api/mp/webhook")
async def mp_webhook(request: Request):
    """
    Recibe notificaciones IPN de Mercado Pago y verifica el pago contra la API de MP.
    Este endpoint es llamado directamente por Mercado Pago (no por el usuario).
    """
    import requests as req_lib

    try:
        body = await request.json()
    except Exception:
        body = {}

    logger.info(f"Webhook MP recibido: {json.dumps(body)[:500]}")

    topic = body.get("type") or request.query_params.get("topic", "")
    resource_id = body.get("data", {}).get("id") or request.query_params.get("id", "")

    if not resource_id or topic not in ("payment", "merchant_order"):
        return {"status": "ignored"}

    # Verificar el pago contra la API oficial de MP
    try:
        verify_url = f"https://api.mercadopago.com/v1/payments/{resource_id}"
        headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
        r = req_lib.get(verify_url, headers=headers, timeout=10)
        r.raise_for_status()
        payment = r.json()
    except Exception as e:
        logger.error(f"Error verificando pago MP {resource_id}: {e}")
        return {"status": "error"}

    payment_id = str(payment.get("id", resource_id))
    status = payment.get("status", "")
    email = payment.get("payer", {}).get("email", "")
    amount = payment.get("transaction_amount", 0)
    currency = payment.get("currency_id", "ARS")

    logger.info(f"Pago MP {payment_id}: status={status} email={email} amount={amount}")

    # Guardar en DB
    with db_lock:
        conn = get_db()
        try:
            existing = conn.execute(
                "SELECT session_token FROM mp_payments WHERE payment_id = ?", (payment_id,)
            ).fetchone()

            conn.execute(
                """INSERT OR REPLACE INTO mp_payments
                   (payment_id, status, preference_id, session_token, amount, currency, created_at, raw_data)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (payment_id, status,
                 str(payment.get("preference_id", "")),
                 existing["session_token"] if existing else None,
                 amount, currency,
                 datetime.datetime.utcnow().isoformat(),
                 json.dumps(payment)[:2000])
            )
            conn.commit()
        finally:
            conn.close()

    # Si el pago fue aprobado, crear sesión premium
    if status == "approved":
        session_token = create_premium_session(
            payment_method="mercadopago",
            payment_id=payment_id,
            email=email
        )
        # Asociar el payment_id con la sesión
        with db_lock:
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE mp_payments SET session_token = ? WHERE payment_id = ?",
                    (session_token, payment_id)
                )
                conn.commit()
            finally:
                conn.close()
        logger.info(f"Premium activado via webhook MP: payment={payment_id} session={session_token}")

    return {"status": "ok"}

@app.get("/api/mp/verify-payment")
async def mp_verify_payment(payment_id: str):
    """
    El frontend llama a este endpoint después del redirect de MP para verificar el pago
    y obtener el session_token si fue aprobado.
    """
    import requests as req_lib

    if not payment_id or not payment_id.isdigit():
        return JSONResponse({"approved": False, "error": "payment_id inválido"}, status_code=400)

    # Primero revisar si ya tenemos el pago en DB
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT status, session_token FROM mp_payments WHERE payment_id = ?",
                (payment_id,)
            ).fetchone()
        finally:
            conn.close()

    if row and row["status"] == "approved" and row["session_token"]:
        return {"approved": True, "session_token": row["session_token"]}

    # Si no está en DB aún, consultamos directamente a MP
    try:
        verify_url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
        headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
        r = req_lib.get(verify_url, headers=headers, timeout=10)
        r.raise_for_status()
        payment = r.json()
    except Exception as e:
        logger.error(f"Error verificando pago MP {payment_id}: {e}")
        return JSONResponse({"approved": False, "error": "No se pudo verificar el pago"}, status_code=500)

    status = payment.get("status", "")
    email = payment.get("payer", {}).get("email", "")
    amount = payment.get("transaction_amount", 0)
    currency = payment.get("currency_id", "ARS")

    if status == "approved":
        session_token = create_premium_session(
            payment_method="mercadopago",
            payment_id=payment_id,
            email=email
        )
        with db_lock:
            conn = get_db()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO mp_payments
                       (payment_id, status, preference_id, session_token, amount, currency, created_at, raw_data)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (payment_id, status,
                     str(payment.get("preference_id", "")),
                     session_token, amount, currency,
                     datetime.datetime.utcnow().isoformat(),
                     json.dumps(payment)[:2000])
                )
                conn.commit()
            finally:
                conn.close()
        logger.info(f"Pago MP verificado y aprobado: {payment_id} → sesión {session_token}")
        return {"approved": True, "session_token": session_token}

    return {"approved": False, "status": status}

# ── ENDPOINTS: PAYPAL ─────────────────────────────────────────────────────────

def get_paypal_access_token() -> str:
    """Obtiene un access token de PayPal usando Client ID + Secret."""
    import requests as req_lib
    import base64

    if not PAYPAL_SECRET:
        raise Exception("PAYPAL_SECRET no configurado")

    credentials = base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_SECRET}".encode()).decode()
    r = req_lib.post(
        "https://api-m.paypal.com/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data="grant_type=client_credentials",
        timeout=10
    )
    r.raise_for_status()
    return r.json().get("access_token", "")

@app.post("/api/paypal/validate-subscription")
async def validate_paypal_subscription(request: Request):
    """
    Valida una suscripción de PayPal contra la API oficial.
    Body JSON: { "subscription_id": "I-XXXX" }
    Devuelve: { "valid": true, "session_token": "uuid" }
    """
    import requests as req_lib

    try:
        data = await request.json()
        subscription_id = str(data.get("subscription_id", "")).strip()
    except Exception:
        return JSONResponse({"valid": False, "error": "Formato inválido"}, status_code=400)

    if not subscription_id or not subscription_id.startswith("I-"):
        return JSONResponse({"valid": False, "error": "subscription_id inválido"}, status_code=400)

    # Verificar que no esté ya registrada (para evitar reutilización)
    with db_lock:
        conn = get_db()
        try:
            existing = conn.execute(
                "SELECT session_token, status FROM paypal_subscriptions WHERE subscription_id = ?",
                (subscription_id,)
            ).fetchone()
        finally:
            conn.close()

    if existing and existing["status"] == "ACTIVE" and existing["session_token"]:
        # Ya está registrada, devolver la misma sesión
        return {"valid": True, "session_token": existing["session_token"]}

    # Si no hay PAYPAL_SECRET configurado, logueamos la suscripción sin verificar
    # (menos seguro pero funcional para pruebas)
    if not PAYPAL_SECRET:
        logger.warning(f"PAYPAL_SECRET no configurado. Aceptando sub {subscription_id} sin verificar API.")
        session_token = create_premium_session(
            payment_method="paypal_subscription",
            payment_id=subscription_id,
            months=1
        )
        with db_lock:
            conn = get_db()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO paypal_subscriptions
                       (subscription_id, plan_id, status, session_token, email, created_at, raw_data)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (subscription_id, "P-55W80757HF211482RNIVJ6VQ", "ACTIVE",
                     session_token, "",
                     datetime.datetime.utcnow().isoformat(),
                     json.dumps({"note": "PAYPAL_SECRET not set, unverified"}))
                )
                conn.commit()
            finally:
                conn.close()
        logger.info(f"Suscripción PayPal registrada (sin verificación): {subscription_id} → {session_token}")
        return {"valid": True, "session_token": session_token}

    # Verificar contra la API de PayPal
    try:
        access_token = get_paypal_access_token()
        r = req_lib.get(
            f"https://api-m.paypal.com/v1/billing/subscriptions/{subscription_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        )
        r.raise_for_status()
        sub_data = r.json()
    except Exception as e:
        logger.error(f"Error verificando suscripción PayPal {subscription_id}: {e}")
        return JSONResponse({"valid": False, "error": "No se pudo verificar la suscripción"}, status_code=500)

    pp_status = sub_data.get("status", "")
    email = sub_data.get("subscriber", {}).get("email_address", "")
    plan_id = sub_data.get("plan_id", "")

    logger.info(f"PayPal sub {subscription_id}: status={pp_status} email={email} plan={plan_id}")

    if pp_status in ("ACTIVE", "APPROVED"):
        session_token = create_premium_session(
            payment_method="paypal_subscription",
            payment_id=subscription_id,
            email=email,
            months=1
        )
        with db_lock:
            conn = get_db()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO paypal_subscriptions
                       (subscription_id, plan_id, status, session_token, email, created_at, raw_data)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (subscription_id, plan_id, pp_status,
                     session_token, email,
                     datetime.datetime.utcnow().isoformat(),
                     json.dumps(sub_data)[:2000])
                )
                conn.commit()
            finally:
                conn.close()
        logger.info(f"Premium PayPal activado: {subscription_id} → sesión {session_token}")
        return {"valid": True, "session_token": session_token}

    return {"valid": False, "status": pp_status}

# ── ENDPOINTS: ADMIN ──────────────────────────────────────────────────────────

@app.get("/api/admin/tokens")
async def admin_list_tokens(secret: str):
    """Lista todos los tokens de activación."""
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    tokens = load_tokens()
    return {"total": len(tokens), "tokens": tokens}

@app.get("/api/admin/sessions")
async def admin_list_sessions(secret: str):
    """Lista todas las sesiones premium activas."""
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT session_token, payment_method, payment_id, email, created_at, expires_at, active FROM premium_sessions ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
            sessions = [dict(r) for r in rows]
        finally:
            conn.close()
    return {"total": len(sessions), "sessions": sessions}

@app.get("/api/admin/payments")
async def admin_list_payments(secret: str):
    """Lista todos los pagos de Mercado Pago registrados."""
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT payment_id, status, session_token, amount, currency, created_at FROM mp_payments ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
            payments = [dict(r) for r in rows]
        finally:
            conn.close()
    return {"total": len(payments), "payments": payments}

# ── GEOCODING ─────────────────────────────────────────────────────────────────
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
    if query in geocode_cache:
        logger.info(f"Geocode cache hit for query: '{query}'")
        return geocode_cache[query]
    url = f"https://nominatim.openstreetmap.org/search?format=json&limit=10&q={urllib.parse.quote(q)}"
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Orbitae/1.0 (https://orbitae.app; contact@orbitae.app)'}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
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
        headers={'User-Agent': 'Orbitae/1.0 (https://orbitae.app; contact@orbitae.app)'}
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
        <p>Al adquirir el plan Premium, el pago es procesado de forma segura por Mercado Pago o PayPal.
        Nuestro servidor verifica el pago directamente con el procesador de pagos y genera una
        <strong>sesión premium cifrada</strong> vinculada a tu dispositivo.
        No almacenamos datos de tarjetas de crédito ni información de pago sensible.</p>
        <p style="margin-top:8px;">La sesión premium se guarda en tu dispositivo y se valida contra
        nuestro servidor. Si cambiás de dispositivo, podés reactivar con tu código de transacción original
        escribiendo a <a href="mailto:orbitae.app@gmail.com">orbitae.app@gmail.com</a>.</p>
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
        logger.error(f"CLIENT ERROR: {data.get('message')} at {data.get('filename')}:{data.get('lineno')}:{data.get('colno')}\nStack: {data.get('error')}")
    except Exception as e:
        logger.error(f"Error parsing client error: {e}")
    return {"status": "ok"}

@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request):
    """Endpoint de salud para Render — evita que el servicio duerma por inactividad."""
    logger.info(f"Health check ping received ({request.method})")
    return {"status": "ok", "app": "Orbitae", "version": "2.0.0"}

# ── INICIO ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Orbitae iniciando en puerto {port}")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
