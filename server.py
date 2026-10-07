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
- ES:  Embedded Signup onboarding (routers/whatsapp_onboarding.py).
- HT:  Human takeover. When the owner replies from the WhatsApp Business app
       (Coexistence "smb_message_echoes"), the bot stays quiet for that
       customer for BOT_PAUSE_HOURS.
- W3:  24-hour window. Every inbound message is recorded; owner requests and
       status updates fall back to approved templates when the window is
       closed. Template button taps (type "button") are handled like
       interactive taps. Failed deliveries reported by Meta are logged.
- HX:  Not every message is a booking. Greetings get the booking menu; any
       other text (or a photo, voice note...) gets "Book appointment / Talk
       to us". "Talk to us" (or a second non-booking message) pauses the bot
       and passes the messages to the manager's WhatsApp.
"""
import asyncio
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
from database import ActivityEvent, Appointment, Business, SessionLocal, UserSession, engine, get_db, init_db
import scheduler
from customer_inbox import MEDIA_LABELS, classify_text, save_message, take_unforwarded
from customer_inbox import ensure_tables as ensure_inbox_tables
from whatsapp_client import (
    build_status_message,
    build_customer_cancelled_template,
    build_template,
    canonical_phone,
    format_when,
    build_status_template,
    ensure_window_table,
    is_real_phone,
    mask_phone,
    normalize_phone,
    record_inbound,
    send_whatsapp,
)

from routers.admin_businesses import router as business_router
from routers.admin_services import router as services_router
from routers.admin_system import router as system_router
from routers.admin_appointments import router as appointments_router
from routers.admin_subscriptions import router as subscriptions_router
from routers.whatsapp_onboarding import admin_router as whatsapp_admin_router
from routers.whatsapp_onboarding import public_router as onboarding_router
from whatsapp_accounts import (
    BOT_PAUSE_HOURS,
    WhatsAppAccount,
    bot_paused,
    ensure_tables as ensure_account_tables,
    pause_bot,
    resume_bot,
)

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

# Media and other message types treated like a non-booking text.
# Reactions and system events are ignored silently.
NON_TEXT_REPLY_TYPES = set(MEDIA_LABELS)

# How long the bot stays quiet after a customer asks for a person.
HANDOFF_PAUSE_HOURS = float(os.getenv("HANDOFF_PAUSE_HOURS", "12"))
START_IDS = {"action_start", "restart_flow", "menu"}
HUMAN_ID = "action_human"
# Typed alone, these bring the booking menu back even while a person is handling the chat.
RESUME_WORDS = {"book", "booking", "menu", "book appointment"}

MANAGER_ACTION_RE = re.compile(r"(confirm|cancel|noshow)_(\d+)")
REMINDER_ACTION_RE = re.compile(r"remind_(ok|cancel)_(\d+)")
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
app.include_router(whatsapp_admin_router)
app.include_router(onboarding_router)

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
    ensure_window_table()
    ensure_account_tables()
    ensure_inbox_tables()
    _normalize_manager_numbers()
    if scheduler.enabled():
        app.state.scheduler_task = asyncio.create_task(scheduler.run_forever())

    if not META_APP_SECRET:
        if IS_PRODUCTION:
            logger.critical("META_APP_SECRET is not set: every webhook will be rejected with 403.")
        else:
            logger.warning("META_APP_SECRET is not set: webhook signature checks are DISABLED (development only).")


def _normalize_manager_numbers() -> None:
    """Older rows may hold '98765 43210' without a country code: messages to the
    owner then fail and their Approve taps are refused. Fix them once at startup."""
    db = SessionLocal()
    try:
        for biz in db.query(Business).all():
            fixed = canonical_phone(biz.manager_phone_number, biz.timezone)
            if is_real_phone(fixed) and fixed != biz.manager_phone_number:
                logger.info("Normalized manager number for business_id=%s", biz.id)
                biz.manager_phone_number = fixed
            elif not is_real_phone(fixed):
                logger.warning("business_id=%s has no valid manager number: owners won't get WhatsApp alerts", biz.id)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not normalize manager numbers")
    finally:
        db.close()


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
    if msg.get("type") == "button":
        # Quick-reply button on a template message.
        return (msg.get("button") or {}).get("payload")
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
            await send_whatsapp(owner["recipient"], tenant_id, owner["payload"], biz.id,
                                template=owner.get("template"))
    else:
        await send_whatsapp(phone, tenant_id, resp, biz.id)


def _owner_sees_chats(db: Session, biz: Business) -> bool:
    """True when the business number also runs the WhatsApp Business app
    (Coexistence): the owner already sees every customer message on their phone."""
    acct = db.query(WhatsAppAccount).filter(WhatsAppAccount.business_id == biz.id).first()
    return bool(acct and acct.coexistence)


def _choice_payload(biz: Business, name: str | None) -> dict:
    first = (name or "").strip().split()[0][:30] if (name or "").strip() else ""
    greeting = f"Hi {first}!" if first else "Hi!"
    return {
        "type": "buttons",
        "body": f"{greeting} Would you like to book an appointment at {biz.name}, or talk to someone there?",
        "buttons": [
            {"id": "action_start", "title": "Book appointment"},
            {"id": HUMAN_ID, "title": "Talk to us"},
        ],
    }


async def _handle_free_message(
    db: Session, biz: Business, tenant_id: str, sender: str, body: str, val: dict, is_media: bool
) -> None:
    """A typed message or a photo/voice note: booking menu, a choice, or a human."""
    name = _profile_name(val, sender)
    if bot_paused(biz.id, sender) and not is_media and body.strip(" .!").lower() in RESUME_WORDS:
        resume_bot(biz.id, sender)               # "reply Book to book instead"
        await _run_customer_flow(db, biz, tenant_id, sender, "action_start", val)
        return
    if bot_paused(biz.id, sender):
        # A person is handling this customer. With Coexistence the owner sees the
        # message in their app; otherwise pass it on so it isn't lost.
        if not _owner_sees_chats(db, biz):
            save_message(biz.id, normalize_phone(sender), name, body)
            await _forward_to_manager(db, biz, tenant_id, sender, name)
        else:
            logger.info("Bot paused (a person is chatting) for business_id=%s customer=%s", biz.id, mask_phone(sender))
        return

    intent = "other" if is_media else classify_text(body)
    if intent == "ack":
        logger.info("Acknowledgement from %s for business_id=%s; no reply needed", mask_phone(sender), biz.id)
        return
    if intent == "book":
        await _run_customer_flow(db, biz, tenant_id, sender, "action_start", val)
        return

    follow_up = save_message(biz.id, normalize_phone(sender), name, body)
    if follow_up:
        # Second non-booking message in a few minutes: they want a person.
        await _hand_to_human(db, biz, tenant_id, sender, val)
        return
    await send_whatsapp(sender, tenant_id, _choice_payload(biz, name), biz.id)


async def _hand_to_human(db: Session, biz: Business, tenant_id: str, sender: str, val: dict) -> None:
    name = _profile_name(val, sender)
    pause_bot(biz.id, sender, HANDOFF_PAUSE_HOURS)
    db.query(UserSession).filter(UserSession.business_id == biz.id,
                                 UserSession.phone_number == sender).delete()
    db.commit()
    await send_whatsapp(
        sender, tenant_id,
        {"type": "text",
         "body": f"Thanks! We've passed your message to {biz.name}. They'll get back to you soon.\n\n"
                 f"To book an appointment instead, just reply \"Book\"."},
        biz.id,
    )
    forwarded = await _forward_to_manager(db, biz, tenant_id, sender, name)
    log_event(db=db, event_type="customer_handoff", status="info" if forwarded else "warning",
              message=f"{mask_phone(sender)} asked to talk to the business"
                      + ("" if forwarded else " (owner not alerted on WhatsApp: check the manager number)"),
              business_id=biz.id)


async def _forward_to_manager(db: Session, biz: Business, tenant_id: str, sender: str, name: str | None) -> bool:
    """Send the customer's waiting messages to the manager's WhatsApp."""
    manager = canonical_phone(biz.manager_phone_number, biz.timezone)
    if not is_real_phone(manager) or manager == normalize_phone(sender):
        return False
    bodies = take_unforwarded(biz.id, normalize_phone(sender)) or ["(They tapped \"Talk to us\".)"]
    digits = normalize_phone(sender)
    who = f"{name} (+{digits})" if name else f"+{digits}"
    joined = " / ".join(bodies)
    link = f"https://wa.me/{digits}"
    text_body = (f"New message for {biz.name} from {who}:\n\n{joined[:1500]}\n\n"
                 f"Reply to them on WhatsApp: {link}\n\nThe booking bot has stepped back for this customer.")
    template = build_template("customer_message", [biz.name, who, joined, link])
    return await send_whatsapp(manager, tenant_id, {"type": "text", "body": text_body}, biz.id, template=template)


async def _handle_reminder_reply(
    db: Session, biz: Business, tenant_id: str, sender: str, action: str, appointment_id: int
) -> None:
    """Customer tapped "I'll be there" / "Cancel booking" on their reminder."""
    appt = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.business_id == biz.id)
        .first()
    )
    if appt is None or normalize_phone(appt.customer_phone) != normalize_phone(sender):
        logger.warning("Reminder reply for #%s from a different number %s; ignored", appointment_id, mask_phone(sender))
        return
    service = appt.service.name if appt.service else "your appointment"
    when = format_when(appt.appointment_time)
    from booking_engine import business_now
    if appt.status not in ("pending", "confirmed") or appt.appointment_time <= business_now(biz):
        await send_whatsapp(sender, tenant_id, {"type": "text", "body":
            f"This booking ({service}, {when}) is already {appt.status.replace('_', ' ')}. Send \"Hi\" to make a new booking."}, biz.id)
        return

    if action == "ok":
        log_event(db=db, event_type="reminder_confirmed", status="success",
                  message=f"Customer confirmed they're coming to #{appt.id}", business_id=biz.id)
        await send_whatsapp(sender, tenant_id, {"type": "text", "body":
            f"Thanks! See you on {when} at {biz.name}. 😊"}, biz.id)
        return

    previous = appt.status
    appt.status = "cancelled"
    db.commit()
    log_event(db=db, event_type="booking_cancelled", status="warning",
              message=f"Customer cancelled #{appt.id} from their reminder", business_id=biz.id)
    await send_whatsapp(sender, tenant_id, {"type": "text", "body":
        f"Your booking for {service} on {when} is cancelled. Send \"Hi\" any time to book again."}, biz.id)

    manager = canonical_phone(biz.manager_phone_number, biz.timezone)
    from booking_engine import owner_cannot_approve
    if is_real_phone(manager) and not owner_cannot_approve(biz):
        who = appt.customer_name or "Customer"
        await send_whatsapp(manager, tenant_id, {"type": "text", "body": (
            f"❌ Booking cancelled by the customer\n\nCustomer: {who} (+{normalize_phone(appt.customer_phone)})\n"
            f"Service: {service}\nWhen: {when}\nBooking #{appt.id} (was {previous})\n\nThe time is open for new bookings again."
        )}, biz.id, template=build_customer_cancelled_template(biz, appt, service))


async def _handle_manager_action(
    db: Session, biz: Business, tenant_id: str, sender: str, action: str, appointment_id: int
) -> None:
    # T5.2 — only this business's manager may act. Checked first so a stranger
    # learns nothing about which booking ids exist.
    if normalize_phone(sender) != canonical_phone(biz.manager_phone_number, biz.timezone):
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
            delivered = await send_whatsapp(
                appt.customer_phone, tenant_id, {"type": "text", "body": customer_text}, biz.id,
                template=build_status_template(appt, biz, previous_status, next_status),
            )
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
        record_inbound(business_id, sender)   # opens the 24h window for this person

        log_event(db=db, event_type="webhook_received", status="info",
                  message=f"Incoming {msg_type} message", business_id=business_id)

        if msg_type in ("interactive", "button"):
            iid = _interaction_id(msg)
            if not iid:
                logger.info("Unsupported interactive message for business_id=%s", business_id)
                return
            match = MANAGER_ACTION_RE.fullmatch(iid)
            reminder = REMINDER_ACTION_RE.fullmatch(iid)
            if reminder:
                await _handle_reminder_reply(db, biz, tenant_id, sender, reminder.group(1), int(reminder.group(2)))
            elif match:
                await _handle_manager_action(db, biz, tenant_id, sender, match.group(1), int(match.group(2)))
            elif iid == HUMAN_ID:
                await _hand_to_human(db, biz, tenant_id, sender, val)
            elif iid in START_IDS:
                resume_bot(business_id, sender)      # an explicit "Book" tap always wins
                await _run_customer_flow(db, biz, tenant_id, sender, iid, val)
            elif bot_paused(business_id, sender):
                logger.info("Bot paused (owner is chatting) for business_id=%s customer=%s", business_id, mask_phone(sender))
            else:
                await _run_customer_flow(db, biz, tenant_id, sender, iid, val)

        elif msg_type == "text":
            body = ((msg.get("text") or {}).get("body") or "").strip()
            await _handle_free_message(db, biz, tenant_id, sender, body, val, is_media=False)

        elif msg_type in NON_TEXT_REPLY_TYPES:
            caption = ((msg.get(msg_type) or {}).get("caption") or "").strip()
            body = MEDIA_LABELS[msg_type] + (f" {caption}" if caption else "")
            await _handle_free_message(db, biz, tenant_id, sender, body, val, is_media=True)

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


def process_echo(val: dict, echo: dict) -> None:
    """The owner messaged a customer from the WhatsApp Business app: pause the bot for that customer."""
    db = SessionLocal()
    try:
        tenant_id = (val.get("metadata") or {}).get("phone_number_id")
        biz = db.query(Business).filter(Business.whatsapp_business_phone_number_id == tenant_id).first()
        customer = echo.get("to")
        if not biz or not customer:
            return
        pause_bot(biz.id, customer)
        logger.info("Owner replied from the app: bot paused %sh for business_id=%s customer=%s",
                    BOT_PAUSE_HOURS, biz.id, mask_phone(customer))
        log_event(db=db, event_type="bot_paused", status="info",
                  message=f"Owner is chatting with {mask_phone(customer)}; bot paused for {BOT_PAUSE_HOURS:g}h",
                  business_id=biz.id)
    except Exception:
        logger.exception("Could not process message echo")
    finally:
        db.close()


def process_status(val: dict, status: dict) -> None:
    """Log a delivery failure Meta reported for one of our outbound messages."""
    db = SessionLocal()
    try:
        tenant_id = (val.get("metadata") or {}).get("phone_number_id")
        biz = db.query(Business).filter(Business.whatsapp_business_phone_number_id == tenant_id).first()
        error = (status.get("errors") or [{}])[0]
        code = error.get("code")
        title = error.get("title") or error.get("message") or "unknown error"
        message = f"Delivery to {mask_phone(status.get('recipient_id'))} failed: {code} {title}"
        if code == 131047:
            message += " (outside the 24-hour window: check this business's message templates are approved)"
        logger.warning(message)
        log_event(db=db, event_type="whatsapp_delivery_failed", status="error",
                  message=message[:500], business_id=biz.id if biz else None)
    except Exception:
        logger.exception("Could not process delivery status")
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
            for echo in val.get("message_echoes") or []:
                if claim_message(f"echo:{echo.get('id')}"):
                    background_tasks.add_task(process_echo, val, echo)
            for status in val.get("statuses") or []:
                if status.get("status") == "failed" and claim_message(f"status:{status.get('id')}:failed"):
                    background_tasks.add_task(process_status, val, status)

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
