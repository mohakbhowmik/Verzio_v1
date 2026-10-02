"""
server.py
================================================================================
VERZIO STUDIO — APPLICATION ENTRY POINT
================================================================================
- W1:  HMAC-SHA256 webhook signature verification (fail-closed in production).
- W2:  Fast ACK. Meta gets 200 immediately; work runs in BackgroundTasks.
- W2:  Idempotency on the WhatsApp message id (wamid) via processed_messages.
- W8:  Every entry / change / message in a webhook batch is processed.
- T5:  Manager approve/reject/no-show buttons are tenant-scoped, sender-checked
       and only allow valid status transitions.
- T1:  /admin guarded by Basic Auth middleware; API docs off in production.
"""
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from settings import IS_PRODUCTION
from admin_auth import admin_auth_middleware
from activity_log import log_event
from booking_engine import BookingEngineException
from booking_runtime import BookingRuntime
from database import ActivityEvent, Appointment, Business, SessionLocal, engine, get_db, init_db
from whatsapp_client import build_status_message, is_real_phone, mask_phone, normalize_phone, send_whatsapp

from routers.admin_businesses import router as business_router
from routers.admin_services import router as services_router
from routers.admin_system import router as system_router
from routers.admin_appointments import router as appointments_router
from routers.admin_subscriptions import router as subscriptions_router

from owner.owner_auth import router as owner_auth_router
from owner.owner_dashboard import router as owner_dashboard_router
from owner.owner_appointments import router as owner_appointments_router
from owner.owner_services import router as owner_services_router
from owner.owner_settings import router as owner_settings_router
from owner.owner_reports import router as owner_reports_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("VERZIO_SERVER")

META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()
VERIFY_TOKEN = os.getenv("VERZIO_VERIFY_TOKEN", "")

# Media and other message types we reply to with a short "please type" hint.
# Reactions and system events are ignored silently.
NON_TEXT_REPLY_TYPES = {"image", "audio", "video", "document", "sticker", "location", "contacts"}

MANAGER_ACTION_RE = re.compile(r"(confirm|cancel|noshow)_(\d+)")
MANAGER_TRANSITIONS = {
    #  action     allowed from              new status     event type             event status
    "confirm": ({"pending"},              "confirmed", "booking_confirmed",   "success"),
    "cancel":  ({"pending", "confirmed"}, "cancelled", "booking_cancelled",   "warning"),
    "noshow":  ({"confirmed"},            "no_show",   "appointment_no_show", "warning"),
}


# ------------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------------

app = FastAPI(
    title="Verzio Studio API",
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

# Zero-trust: every /admin path requires admin credentials, including future routes.
app.middleware("http")(admin_auth_middleware)

app.include_router(business_router)
app.include_router(services_router)
app.include_router(system_router)
app.include_router(appointments_router)
app.include_router(subscriptions_router)

app.include_router(owner_auth_router)
app.include_router(owner_dashboard_router)
app.include_router(owner_appointments_router)
app.include_router(owner_services_router)
app.include_router(owner_settings_router)
app.include_router(owner_reports_router)

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db()
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS processed_messages ("
            "  wamid TEXT PRIMARY KEY,"
            "  received_at TIMESTAMP NOT NULL"
            ")"
        ))
        conn.execute(
            text("DELETE FROM processed_messages WHERE received_at < :cutoff"),
            {"cutoff": datetime.utcnow() - timedelta(days=7)},
        )

    if not META_APP_SECRET:
        if IS_PRODUCTION:
            logger.critical("META_APP_SECRET is not set: every webhook will be rejected with 403.")
        else:
            logger.warning("META_APP_SECRET is not set: webhook signature checks are DISABLED (development only).")


# ------------------------------------------------------------------------
# W1 — Signature verification
# ------------------------------------------------------------------------

def verify_meta_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Validate X-Hub-Signature-256 against META_APP_SECRET.
    Development without a secret: allowed (with a startup warning).
    Production without a secret: always rejected."""
    if not META_APP_SECRET:
        return not IS_PRODUCTION
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    received = signature_header.split("=", 1)[1].strip()
    expected = hmac.new(META_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


# ------------------------------------------------------------------------
# W2 — Idempotency
# ------------------------------------------------------------------------

def claim_message(wamid: str | None) -> bool:
    """Atomically record a wamid. True = first delivery, False = duplicate."""
    if not wamid:
        logger.warning("Webhook message has no id; processing without deduplication.")
        return True

    if engine.dialect.name == "sqlite":
        sql = "INSERT OR IGNORE INTO processed_messages (wamid, received_at) VALUES (:wamid, :ts)"
    else:
        sql = ("INSERT INTO processed_messages (wamid, received_at) VALUES (:wamid, :ts) "
               "ON CONFLICT (wamid) DO NOTHING")

    with engine.begin() as conn:
        result = conn.execute(text(sql), {"wamid": wamid, "ts": datetime.utcnow()})
    return result.rowcount == 1


# ------------------------------------------------------------------------
# Runtime compatibility: pass the WhatsApp profile name if the runtime accepts it
# ------------------------------------------------------------------------

def _detect_runtime_name_kwarg() -> str | None:
    params = inspect.signature(BookingRuntime.process_interaction).parameters
    for candidate in ("customer_name", "profile_name"):
        if candidate in params:
            return candidate
    return None


RUNTIME_NAME_KWARG = _detect_runtime_name_kwarg()


def _profile_name(val: dict, wa_id: str) -> str | None:
    contacts = val.get("contacts") or []
    for contact in contacts:
        if contact.get("wa_id") == wa_id:
            return (contact.get("profile") or {}).get("name")
    if len(contacts) == 1:
        return (contacts[0].get("profile") or {}).get("name")
    return None


def _interaction_id(msg: dict) -> str | None:
    interactive = msg.get("interactive") or {}
    itype = interactive.get("type")
    if itype == "button_reply":
        return (interactive.get("button_reply") or {}).get("id")
    if itype == "list_reply":
        return (interactive.get("list_reply") or {}).get("id")
    return None


# ------------------------------------------------------------------------
# Background processing
# ------------------------------------------------------------------------

async def _run_customer_flow(
    db: Session, biz: Business, tenant_id: str, phone: str, interaction_id: str, val: dict
) -> None:
    runtime = BookingRuntime(db)

    kwargs = {}
    name = _profile_name(val, phone)
    if RUNTIME_NAME_KWARG and name:
        kwargs[RUNTIME_NAME_KWARG] = name

    try:
        resp = await runtime.process_interaction(phone, tenant_id, interaction_id, **kwargs)
    except BookingEngineException as exc:
        db.rollback()
        if exc.error_code == "ERR_TENANT_LOCKED":
            resp = {
                "type": "text",
                "body": f"Sorry, {biz.name} isn't taking WhatsApp bookings right now. "
                        f"Please contact the business directly.",
            }
        else:
            logger.warning("Booking engine error %s for business_id=%s", exc.error_code, biz.id)
            resp = {"type": "text", "body": "Sorry, something went wrong. Send \"Hi\" to start again."}

    if not resp:
        logger.warning("Runtime returned no response for business_id=%s phone=%s", biz.id, mask_phone(phone))
        resp = {"type": "text", "body": "Sorry, I didn't catch that. Send \"Hi\" to start a new booking."}

    if isinstance(resp, dict) and "customer" in resp:
        await send_whatsapp(phone, tenant_id, resp["customer"], biz.id)
        owner = resp.get("owner") or {}
        if owner.get("recipient") and owner.get("payload"):
            await send_whatsapp(owner["recipient"], tenant_id, owner["payload"], biz.id)
    else:
        await send_whatsapp(phone, tenant_id, resp, biz.id)


async def _handle_manager_action(
    db: Session, biz: Business, tenant_id: str, sender: str, action: str, appointment_id: int
) -> None:
    # T5.2 — only this business's manager may act. Checked first so a stranger
    # learns nothing about which booking ids exist.
    if normalize_phone(sender) != normalize_phone(biz.manager_phone_number):
        logger.warning(
            "Blocked manager action %s_%s from non-manager %s for business_id=%s",
            action, appointment_id, mask_phone(sender), biz.id,
        )
        log_event(
            db=db,
            event_type="manager_action_blocked",
            status="warning",
            message=f"Action '{action}' on #{appointment_id} from unauthorised number {mask_phone(sender)}",
            business_id=biz.id,
        )
        return

    # T5.1 — the appointment must exist AND belong to the tenant that received the webhook.
    appt = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.business_id == biz.id)
        .first()
    )
    if appt is None:
        await send_whatsapp(sender, tenant_id, {"type": "text", "body": f"Booking #{appointment_id} wasn't found."}, biz.id)
        return

    # T5.3 — valid transitions only.
    allowed_from, next_status, event_type, event_status = MANAGER_TRANSITIONS[action]
    previous_status = appt.status
    if previous_status not in allowed_from:
        await send_whatsapp(
            sender, tenant_id,
            {"type": "text", "body": f"Booking #{appt.id} is already {previous_status.replace('_', ' ')}. No change made."},
            biz.id,
        )
        return

    appt.status = next_status
    db.commit()
    log_event(
        db=db,
        event_type=event_type,
        status=event_status,
        message=f"Appointment #{appt.id} {previous_status} -> {next_status} (via WhatsApp)",
        business_id=biz.id,
    )

    # T5.4 — tell the customer immediately.
    customer_text = build_status_message(appt, biz, previous_status, next_status)
    note = ""
    if customer_text:
        delivered = False
        if is_real_phone(appt.customer_phone):
            delivered = await send_whatsapp(appt.customer_phone, tenant_id, {"type": "text", "body": customer_text}, biz.id)
        note = " Customer notified." if delivered else " We couldn't message the customer, please contact them directly."

    await send_whatsapp(
        sender, tenant_id,
        {"type": "text", "body": f"Booking #{appt.id} marked {next_status.replace('_', ' ')}.{note}"},
        biz.id,
    )


async def process_message(val: dict, msg: dict) -> None:
    """Handle one inbound message. Owns its own DB session; never raises."""
    db = SessionLocal()
    business_id = None
    try:
        tenant_id = (val.get("metadata") or {}).get("phone_number_id")
        sender = msg.get("from")
        msg_type = msg.get("type")

        if not tenant_id or not sender:
            logger.warning("Webhook message missing tenant or sender; ignored.")
            return

        biz = db.query(Business).filter(Business.whatsapp_business_phone_number_id == tenant_id).first()
        if biz is None:
            logger.warning("Webhook for unknown phone_number_id %s; ignored.", tenant_id)
            return
        business_id = biz.id

        log_event(db=db, event_type="webhook_received", status="info",
                  message=f"Incoming {msg_type} message", business_id=business_id)

        if msg_type == "interactive":
            iid = _interaction_id(msg)
            if not iid:
                logger.info("Unsupported interactive message for business_id=%s", business_id)
                return
            match = MANAGER_ACTION_RE.fullmatch(iid)
            if match:
                await _handle_manager_action(db, biz, tenant_id, sender, match.group(1), int(match.group(2)))
            else:
                await _run_customer_flow(db, biz, tenant_id, sender, iid, val)

        elif msg_type == "text":
            await _run_customer_flow(db, biz, tenant_id, sender, "action_start", val)

        elif msg_type in NON_TEXT_REPLY_TYPES:
            await send_whatsapp(
                sender, tenant_id,
                {"type": "text", "body": f"Hi! To book at {biz.name}, just send us a text message like \"Hi\"."},
                business_id,
            )

    except Exception as exc:
        logger.exception("Webhook processing error for business_id=%s", business_id)
        try:
            db.rollback()
            log_event(db=db, event_type="webhook_failed", status="error",
                      message=str(exc)[:500], business_id=business_id)
        except Exception:
            pass
    finally:
        db.close()


# ------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()

    if not verify_meta_signature(raw_body, request.headers.get("x-hub-signature-256")):
        logger.warning("Rejected webhook: invalid or missing signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        data = json.loads(raw_body)
    except ValueError:
        return {"status": "received"}
    if not isinstance(data, dict):
        return {"status": "received"}

    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            val = change.get("value") or {}
            for msg in val.get("messages") or []:
                if claim_message(msg.get("id")):
                    background_tasks.add_task(process_message, val, msg)
                else:
                    logger.info("Duplicate webhook message ignored: %s", msg.get("id"))

    return {"status": "received"}


@app.get("/webhook")
async def verify(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    """Meta's webhook verification handshake."""
    if mode == "subscribe" and VERIFY_TOKEN and hmac.compare_digest(token or "", VERIFY_TOKEN):
        return PlainTextResponse(challenge or "")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "healthy", "database": "ok"}
    except Exception:
        logger.exception("Health check: database unreachable")
        return JSONResponse({"status": "degraded", "database": "error"}, status_code=503)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    active_business_count = db.query(Business).filter(Business.is_active == True).count()
    total_business_count = db.query(Business).count()

    needing_attention = (
        db.query(Business)
        .filter(or_(Business.is_active == False, Business.accepting_bookings == False))
        .order_by(Business.name.asc())
        .all()
    )

    since = datetime.utcnow() - timedelta(hours=24)
    failed_events = (
        db.query(ActivityEvent)
        .filter(ActivityEvent.status == "error", ActivityEvent.created_at >= since)
        .all()
    )

    recent_activity = (
        db.query(ActivityEvent)
        .filter(ActivityEvent.message.isnot(None))
        .order_by(ActivityEvent.created_at.desc())
        .limit(20)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "active_page": "dashboard",
            "active_business_count": active_business_count,
            "total_business_count": total_business_count,
            "needing_attention": needing_attention,
            "failed_events": failed_events,
            "recent_activity": recent_activity,
        },
    )
