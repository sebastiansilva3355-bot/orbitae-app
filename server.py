# Orbitae — Servidor de producción
# Soporta PostgreSQL (producción en Render) y SQLite (desarrollo local).

import os
import sys
import logging
import uuid
import datetime
import threading
import json
import urllib.request
import urllib.parse

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ── UTF-8 ─────────────────────────────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ── BASE DE DATOS: PostgreSQL en producción, SQLite en local ──────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Render usa "postgres://" pero psycopg2 necesita "postgresql://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

USE_POSTGRES = bool(DATABASE_URL)
DB_FILE = "orbitae_premium.db"
PH = "%s" if USE_POSTGRES else "?"   # placeholder según el motor
db_lock = threading.Lock()

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    logger.info("Base de datos: PostgreSQL")
else:
    import sqlite3
    logger.info("Base de datos: SQLite (desarrollo local)")

# ── HELPERS DE DB ─────────────────────────────────────────────────────────────

def get_db():
    """Devuelve una conexión a la base de datos activa."""
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    else:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

def db_fetchone(conn, query, params=()):
    """Ejecuta una query y devuelve una fila como dict (o None)."""
    if USE_POSTGRES:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        cur = conn.cursor()
    cur.execute(query, params)
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None

def db_fetchall(conn, query, params=()):
    """Ejecuta una query y devuelve todas las filas como lista de dicts."""
    if USE_POSTGRES:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        cur = conn.cursor()
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]

def db_execute(conn, query, params=()):
    """Ejecuta una query de escritura (INSERT/UPDATE/DELETE)."""
    cur = conn.cursor()
    cur.execute(query, params)
    cur.close()

def db_upsert_session(conn, session_token, payment_method, payment_id, email, created_at, expires_at):
    if USE_POSTGRES:
        db_execute(conn, f"""
            INSERT INTO premium_sessions
                (session_token, payment_method, payment_id, email, created_at, expires_at, active)
            VALUES ({PH},{PH},{PH},{PH},{PH},{PH},1)
            ON CONFLICT (session_token) DO UPDATE SET
                payment_method = EXCLUDED.payment_method,
                payment_id     = EXCLUDED.payment_id,
                email          = EXCLUDED.email,
                expires_at     = EXCLUDED.expires_at,
                active         = 1
        """, (session_token, payment_method, payment_id, email, created_at, expires_at))
    else:
        db_execute(conn, f"""
            INSERT OR REPLACE INTO premium_sessions
                (session_token, payment_method, payment_id, email, created_at, expires_at, active)
            VALUES ({PH},{PH},{PH},{PH},{PH},{PH},1)
        """, (session_token, payment_method, payment_id, email, created_at, expires_at))

def db_upsert_mp_payment(conn, payment_id, status, preference_id, session_token,
                         amount, currency, created_at, raw_data):
    if USE_POSTGRES:
        db_execute(conn, f"""
            INSERT INTO mp_payments
                (payment_id, status, preference_id, session_token, amount, currency, created_at, raw_data)
            VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})
            ON CONFLICT (payment_id) DO UPDATE SET
                status         = EXCLUDED.status,
                session_token  = EXCLUDED.session_token,
                raw_data       = EXCLUDED.raw_data
        """, (payment_id, status, preference_id, session_token, amount, currency, created_at, raw_data))
    else:
        db_execute(conn, f"""
            INSERT OR REPLACE INTO mp_payments
                (payment_id, status, preference_id, session_token, amount, currency, created_at, raw_data)
            VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})
        """, (payment_id, status, preference_id, session_token, amount, currency, created_at, raw_data))

def db_upsert_paypal_sub(conn, subscription_id, plan_id, status, session_token,
                         email, created_at, raw_data):
    if USE_POSTGRES:
        db_execute(conn, f"""
            INSERT INTO paypal_subscriptions
                (subscription_id, plan_id, status, session_token, email, created_at, raw_data)
            VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH})
            ON CONFLICT (subscription_id) DO UPDATE SET
                status        = EXCLUDED.status,
                session_token = EXCLUDED.session_token,
                raw_data      = EXCLUDED.raw_data
        """, (subscription_id, plan_id, status, session_token, email, created_at, raw_data))
    else:
        db_execute(conn, f"""
            INSERT OR REPLACE INTO paypal_subscriptions
                (subscription_id, plan_id, status, session_token, email, created_at, raw_data)
            VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH})
        """, (subscription_id, plan_id, status, session_token, email, created_at, raw_data))

def db_insert_token(conn, token, email, payment_ref, created_at):
    if USE_POSTGRES:
        db_execute(conn, f"""
            INSERT INTO activation_tokens (token, email, payment_ref, used, created_at)
            VALUES ({PH},{PH},{PH},0,{PH})
            ON CONFLICT DO NOTHING
        """, (token, email, payment_ref, created_at))
    else:
        db_execute(conn, f"""
            INSERT OR IGNORE INTO activation_tokens (token, email, payment_ref, used, created_at)
            VALUES ({PH},{PH},{PH},0,{PH})
        """, (token, email, payment_ref, created_at))

def init_db():
    """Crea las tablas si no existen."""
    statements = [
        """CREATE TABLE IF NOT EXISTS premium_sessions (
            session_token TEXT PRIMARY KEY,
            payment_method TEXT NOT NULL,
            payment_id TEXT,
            email TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            active INTEGER DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS mp_payments (
            payment_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            preference_id TEXT,
            session_token TEXT,
            amount REAL,
            currency TEXT,
            created_at TEXT NOT NULL,
            raw_data TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS paypal_subscriptions (
            subscription_id TEXT PRIMARY KEY,
            plan_id TEXT,
            status TEXT NOT NULL,
            session_token TEXT,
            email TEXT,
            created_at TEXT NOT NULL,
            raw_data TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS activation_tokens (
            token TEXT PRIMARY KEY,
            email TEXT,
            payment_ref TEXT,
            used INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            used_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_sessions_payment ON premium_sessions(payment_id)",
        "CREATE INDEX IF NOT EXISTS idx_mp_session ON mp_payments(session_token)",
    ]
    with db_lock:
        conn = get_db()
        try:
            for sql in statements:
                db_execute(conn, sql)
            conn.commit()
            logger.info("Tablas de DB inicializadas correctamente.")
        except Exception as e:
            logger.error(f"Error inicializando DB: {e}")
        finally:
            conn.close()

init_db()

# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────
MP_ACCESS_TOKEN = os.environ.get(
    "MP_ACCESS_TOKEN",
    "APP_USR-2055215287777718-061112-2e1047c21eeff9813b1474c0d8090923-172306186"
)
PAYPAL_CLIENT_ID = os.environ.get(
    "PAYPAL_CLIENT_ID",
    "BAA0Jivw-zoOWaAqsFIMsjyFsCQvBGme7gmQX1fdr_JKoUI_emgoDiqlZ2kn_-0GSC69ngLKCDTiFV6lBc"
)
PAYPAL_SECRET   = os.environ.get("PAYPAL_SECRET", "")
BASE_URL        = os.environ.get("BASE_URL", "https://orbitae-app.onrender.com")
ADMIN_SECRET    = os.environ.get("ORBITAE_ADMIN_SECRET", "orbitae-admin-2025")

# ── TOKENS LEGACY (JSON) ──────────────────────────────────────────────────────
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
    """Crea una sesión premium en la DB y devuelve el session_token (UUID)."""
    session_token = str(uuid.uuid4())
    now = datetime.datetime.utcnow()
    expires = now + datetime.timedelta(days=30 * months)
    with db_lock:
        conn = get_db()
        try:
            db_upsert_session(conn, session_token, payment_method, payment_id,
                              email or "", now.isoformat(), expires.isoformat())
            conn.commit()
            logger.info(f"Sesión premium creada: {session_token[:8]}… método={payment_method}")
        finally:
            conn.close()
    return session_token

def is_valid_premium_session(session_token: str) -> bool:
    """Devuelve True si el session_token existe en DB, está activo y no expiró."""
    if not session_token:
        return False
    with db_lock:
        conn = get_db()
        try:
            row = db_fetchone(
                conn,
                f"SELECT expires_at, active FROM premium_sessions WHERE session_token = {PH}",
                (session_token,)
            )
        finally:
            conn.close()
    if not row or not row.get("active"):
        return False
    expires_at = row.get("expires_at")
    if expires_at:
        try:
            if datetime.datetime.utcnow() > datetime.datetime.fromisoformat(expires_at):
                return False
        except Exception:
            pass
    return True

# ── APP ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Orbitae — Astrocartografía Interactiva",
    description="Calculadora de líneas de nacimiento planetarias.",
    version="2.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

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

# ── ENDPOINTS: SESIÓN PREMIUM ─────────────────────────────────────────────────

@app.post("/api/premium/check")
async def check_premium(request: Request):
    """Verifica si un session_token tiene premium activo. Body: { session_token }"""
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
    """Crea un token de activación manual. Solo admin."""
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    token = str(uuid.uuid4()).replace("-", "").upper()[:16]
    now = datetime.datetime.utcnow().isoformat()
    with db_lock:
        conn = get_db()
        try:
            db_insert_token(conn, token, email, ref, now)
            conn.commit()
        finally:
            conn.close()
    # Guardar también en JSON legacy
    tokens = load_tokens()
    tokens[token] = {"created_at": now, "email": email, "payment_ref": ref, "used": False, "used_at": None}
    save_tokens(tokens)
    logger.info(f"Token creado: {token} email={email} ref={ref}")
    return {"token": token, "email": email, "ref": ref, "status": "created"}

@app.post("/api/activate")
async def activate_premium(request: Request):
    """
    Valida un token de activación manual (un solo uso).
    Body: { token } → Devuelve { valid, session_token }
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
            row = db_fetchone(conn,
                f"SELECT * FROM activation_tokens WHERE token = {PH}", (token,))

            if not row:
                # Fallback: buscar en JSON legacy y migrar
                tokens = load_tokens()
                if token not in tokens:
                    logger.warning(f"Token inválido: {token}")
                    return JSONResponse({"valid": False, "error": "Token no reconocido"})
                token_data = tokens[token]
                if token_data.get("used"):
                    return JSONResponse({"valid": False, "error": "Este token ya fue utilizado"})
                db_insert_token(conn, token, token_data.get("email", ""),
                                token_data.get("payment_ref", ""),
                                datetime.datetime.utcnow().isoformat())
                conn.commit()
                row = db_fetchone(conn,
                    f"SELECT * FROM activation_tokens WHERE token = {PH}", (token,))

            if row.get("used"):
                logger.warning(f"Token ya usado: {token}")
                return JSONResponse({"valid": False, "error": "Este token ya fue utilizado"})

            # Marcar como usado
            db_execute(conn,
                f"UPDATE activation_tokens SET used = 1, used_at = {PH} WHERE token = {PH}",
                (datetime.datetime.utcnow().isoformat(), token))
            conn.commit()

            # Actualizar JSON legacy
            tokens = load_tokens()
            if token in tokens:
                tokens[token]["used"] = True
                tokens[token]["used_at"] = datetime.datetime.utcnow().isoformat()
                save_tokens(tokens)
        finally:
            conn.close()

    session_token = create_premium_session(
        payment_method="manual_token",
        payment_id=token,
        email=row.get("email", "") if row else ""
    )
    logger.info(f"Token activado: {token} → sesión {session_token[:8]}…")
    return JSONResponse({"valid": True, "message": "Premium activado correctamente",
                         "session_token": session_token})

# ── ENDPOINTS: MERCADO PAGO ───────────────────────────────────────────────────

@app.post("/api/mp/create-preference")
async def create_mp_preference(request: Request):
    """Crea una preferencia en Mercado Pago y devuelve el init_point."""
    import requests as req_lib
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    body = {
        "items": [{
            "title": "Orbitae Premium - Acceso Ilimitado",
            "quantity": 1,
            "unit_price": 4500,
            "currency_id": "ARS"
        }],
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
        logger.info(f"Preferencia MP creada: {data.get('id')}")
        return {"init_point": data.get("init_point"), "preference_id": data.get("id")}
    except Exception as e:
        logger.error(f"Error creando preferencia MP: {e}")
        return JSONResponse({"error": "No se pudo iniciar el pago con Mercado Pago"}, status_code=500)

@app.post("/api/mp/webhook")
async def mp_webhook(request: Request):
    """
    Recibe notificaciones IPN de Mercado Pago.
    MP llama a este endpoint directamente (no el usuario).
    Verifica el pago contra la API oficial y activa premium si está aprobado.
    """
    import requests as req_lib
    try:
        body = await request.json()
    except Exception:
        body = {}

    logger.info(f"Webhook MP: {json.dumps(body)[:300]}")
    topic = body.get("type") or request.query_params.get("topic", "")
    resource_id = (body.get("data", {}).get("id") or
                   request.query_params.get("id", ""))

    if not resource_id or topic not in ("payment", "merchant_order"):
        return {"status": "ignored"}

    try:
        r = req_lib.get(
            f"https://api.mercadopago.com/v1/payments/{resource_id}",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            timeout=10
        )
        r.raise_for_status()
        payment = r.json()
    except Exception as e:
        logger.error(f"Error verificando pago MP {resource_id}: {e}")
        return {"status": "error"}

    payment_id  = str(payment.get("id", resource_id))
    status      = payment.get("status", "")
    email       = payment.get("payer", {}).get("email", "")
    amount      = payment.get("transaction_amount", 0)
    currency    = payment.get("currency_id", "ARS")
    pref_id     = str(payment.get("preference_id", ""))
    now         = datetime.datetime.utcnow().isoformat()

    logger.info(f"Pago MP {payment_id}: status={status} email={email}")

    session_token = None
    if status == "approved":
        session_token = create_premium_session("mercadopago", payment_id, email)

    with db_lock:
        conn = get_db()
        try:
            db_upsert_mp_payment(conn, payment_id, status, pref_id,
                                 session_token, amount, currency, now,
                                 json.dumps(payment)[:2000])
            conn.commit()
        finally:
            conn.close()

    if status == "approved":
        logger.info(f"Premium MP activado: {payment_id} → sesión {session_token[:8]}…")

    return {"status": "ok"}

@app.get("/api/mp/verify-payment")
async def mp_verify_payment(payment_id: str):
    """
    El frontend llama aquí tras el redirect de MP.
    Verifica el payment_id con la API de MP y devuelve session_token si fue aprobado.
    """
    import requests as req_lib

    if not payment_id or not payment_id.isdigit():
        return JSONResponse({"approved": False, "error": "payment_id inválido"}, status_code=400)

    # Buscar en DB primero
    with db_lock:
        conn = get_db()
        try:
            row = db_fetchone(conn,
                f"SELECT status, session_token FROM mp_payments WHERE payment_id = {PH}",
                (payment_id,))
        finally:
            conn.close()

    if row and row.get("status") == "approved" and row.get("session_token"):
        return {"approved": True, "session_token": row["session_token"]}

    # Si no está en DB, consultar directamente a MP
    try:
        r = req_lib.get(
            f"https://api.mercadopago.com/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            timeout=10
        )
        r.raise_for_status()
        payment = r.json()
    except Exception as e:
        logger.error(f"Error verificando pago MP {payment_id}: {e}")
        return JSONResponse({"approved": False, "error": "No se pudo verificar"}, status_code=500)

    status   = payment.get("status", "")
    email    = payment.get("payer", {}).get("email", "")
    amount   = payment.get("transaction_amount", 0)
    currency = payment.get("currency_id", "ARS")
    pref_id  = str(payment.get("preference_id", ""))
    now      = datetime.datetime.utcnow().isoformat()

    if status == "approved":
        session_token = create_premium_session("mercadopago", payment_id, email)
        with db_lock:
            conn = get_db()
            try:
                db_upsert_mp_payment(conn, payment_id, status, pref_id,
                                     session_token, amount, currency, now,
                                     json.dumps(payment)[:2000])
                conn.commit()
            finally:
                conn.close()
        logger.info(f"Pago MP verificado y aprobado: {payment_id}")
        return {"approved": True, "session_token": session_token}

    return {"approved": False, "status": status}

# ── ENDPOINTS: PAYPAL ─────────────────────────────────────────────────────────

def get_paypal_access_token() -> str:
    import requests as req_lib
    import base64
    if not PAYPAL_SECRET:
        raise Exception("PAYPAL_SECRET no configurado")
    creds = base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_SECRET}".encode()).decode()
    r = req_lib.post(
        "https://api-m.paypal.com/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data="grant_type=client_credentials",
        timeout=10
    )
    r.raise_for_status()
    return r.json().get("access_token", "")

@app.post("/api/paypal/validate-subscription")
async def validate_paypal_subscription(request: Request):
    """
    Valida una suscripción de PayPal contra la API oficial.
    Body: { subscription_id } → Devuelve { valid, session_token }
    """
    import requests as req_lib
    try:
        data = await request.json()
        subscription_id = str(data.get("subscription_id", "")).strip()
    except Exception:
        return JSONResponse({"valid": False, "error": "Formato inválido"}, status_code=400)

    if not subscription_id or not subscription_id.startswith("I-"):
        return JSONResponse({"valid": False, "error": "subscription_id inválido"}, status_code=400)

    # Si ya existe en DB con sesión activa, reutilizarla
    with db_lock:
        conn = get_db()
        try:
            existing = db_fetchone(conn,
                f"SELECT session_token, status FROM paypal_subscriptions WHERE subscription_id = {PH}",
                (subscription_id,))
        finally:
            conn.close()

    if existing and existing.get("status") == "ACTIVE" and existing.get("session_token"):
        return {"valid": True, "session_token": existing["session_token"]}

    now = datetime.datetime.utcnow().isoformat()

    # Sin PAYPAL_SECRET: aceptar sin verificar (registrar igualmente)
    if not PAYPAL_SECRET:
        logger.warning(f"PAYPAL_SECRET no configurado. Aceptando {subscription_id} sin verificar.")
        session_token = create_premium_session("paypal_subscription", subscription_id)
        with db_lock:
            conn = get_db()
            try:
                db_upsert_paypal_sub(conn, subscription_id, "P-55W80757HF211482RNIVJ6VQ",
                                     "ACTIVE", session_token, "", now,
                                     json.dumps({"note": "PAYPAL_SECRET not set"}))
                conn.commit()
            finally:
                conn.close()
        return {"valid": True, "session_token": session_token}

    # Verificar con la API de PayPal
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
    email     = sub_data.get("subscriber", {}).get("email_address", "")
    plan_id   = sub_data.get("plan_id", "")

    logger.info(f"PayPal sub {subscription_id}: status={pp_status} email={email}")

    if pp_status in ("ACTIVE", "APPROVED"):
        session_token = create_premium_session("paypal_subscription", subscription_id, email)
        with db_lock:
            conn = get_db()
            try:
                db_upsert_paypal_sub(conn, subscription_id, plan_id, pp_status,
                                     session_token, email, now, json.dumps(sub_data)[:2000])
                conn.commit()
            finally:
                conn.close()
        logger.info(f"Premium PayPal activado: {subscription_id}")
        return {"valid": True, "session_token": session_token}

    return {"valid": False, "status": pp_status}

# ── ENDPOINTS: ADMIN ──────────────────────────────────────────────────────────

@app.get("/api/admin/tokens")
async def admin_list_tokens(secret: str):
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    return {"tokens": load_tokens()}

@app.get("/api/admin/sessions")
async def admin_list_sessions(secret: str):
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    with db_lock:
        conn = get_db()
        try:
            rows = db_fetchall(conn,
                "SELECT session_token, payment_method, payment_id, email, created_at, expires_at, active "
                "FROM premium_sessions ORDER BY created_at DESC LIMIT 200")
        finally:
            conn.close()
    return {"total": len(rows), "sessions": rows}

@app.get("/api/admin/payments")
async def admin_list_payments(secret: str):
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    with db_lock:
        conn = get_db()
        try:
            rows = db_fetchall(conn,
                "SELECT payment_id, status, session_token, amount, currency, created_at "
                "FROM mp_payments ORDER BY created_at DESC LIMIT 200")
        finally:
            conn.close()
    return {"total": len(rows), "payments": rows}

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

def save_geocache():
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
        return geocode_cache[query]
    url = f"https://nominatim.openstreetmap.org/search?format=json&limit=10&q={urllib.parse.quote(q)}"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Orbitae/1.0 (https://orbitae.app; contact@orbitae.app)'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            geocode_cache[query] = data
            save_geocache()
            return data
    except Exception as e:
        logger.error(f"Geocoding error for '{q}': {e}")
        return []

@app.get("/api/reverse")
async def api_reverse(lat: float, lon: float):
    cache_key = f"rev_{lat:.4f}_{lon:.4f}"
    if cache_key in geocode_cache:
        return geocode_cache[cache_key]
    url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Orbitae/1.0 (https://orbitae.app; contact@orbitae.app)'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            geocode_cache[cache_key] = data
            save_geocache()
            return data
    except Exception as e:
        logger.error(f"Reverse geocoding error for ({lat}, {lon}): {e}")
        return {}

# ── ARCHIVOS ESTÁTICOS ────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── RUTAS ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/astro")
async def astro_app():
    return FileResponse("static/index.html")

@app.get("/privacidad")
async def privacy_policy():
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
    <p>Orbitae <strong>no recopila</strong> datos personales. Los cálculos se realizan localmente en tu dispositivo.</p>
    <h2>2. Activación Premium</h2>
    <div class="highlight">
        <p>Al adquirir Premium, el pago es procesado por Mercado Pago o PayPal. Nuestro servidor verifica el pago
        directamente con el procesador y genera una <strong>sesión cifrada</strong>. No almacenamos datos de tarjetas.</p>
    </div>
    <h2>3. Servicios de terceros</h2>
    <ul>
        <li><strong>OpenStreetMap / Leaflet</strong>: mapa interactivo.</li>
        <li><strong>Nominatim</strong>: búsqueda de ciudades.</li>
        <li><strong>Mercado Pago / PayPal</strong>: pagos. Ver sus políticas.</li>
        <li><strong>Google Fonts</strong>: tipografías.</li>
    </ul>
    <h2>4. Contacto</h2>
    <p>Email: <a href="mailto:orbitae.app@gmail.com">orbitae.app@gmail.com</a></p>
    <a href="/" class="back-btn">&#8592; Volver a la App</a>
</body>
</html>"""
    return HTMLResponse(content=html)

@app.post("/api/log-error")
async def log_error(request: Request):
    try:
        data = await request.json()
        logger.error(f"CLIENT ERROR: {data.get('message')} at {data.get('filename')}:{data.get('lineno')}")
    except Exception:
        pass
    return {"status": "ok"}

@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request):
    db_type = "postgresql" if USE_POSTGRES else "sqlite"
    return {"status": "ok", "app": "Orbitae", "version": "2.1.0", "db": db_type}

# ── INICIO ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Orbitae iniciando en puerto {port}")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
