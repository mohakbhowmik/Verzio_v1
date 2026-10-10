"""
whatsapp_client.py
================================================================================
VERZIO STUDIO — OUTBOUND WHATSAPP (single source of truth)
================================================================================
Every outbound WhatsApp message goes through send_whatsapp(). It is shared by
the webhook (server.py), the booking runtime and the owner portal.

- 24-hour window: Meta only delivers free-form messages within 24 hours of the
  recipient's last message to that business number. We record every inbound
  message per (business, phone). When a send carries a `template` and the
  window is closed, the approved template is sent instead. If the template is
  not approved yet, we fall back to free-form and record an incident.
- Retries network errors, 429 and 5xx responses with a short backoff.
- Logs success/failure with its OWN database session, so a logging failure can
  never roll back the caller's business transaction.
- Never logs full phone numbers.
- Per-client tokens: messages for a business connected through Embedded
  Signup use that business's own token (whatsapp_accounts.py). Businesses
  without a stored connection fall back to META_ACCESS_TOKEN.
- Never messages the business's own WhatsApp number (a number can't message
  itself; this happens when the manager number is the business number).

Template definitions live in TEMPLATE_DEFINITIONS so the text submitted to Meta
(create_templates.py) and the parameters sent here can never drift apart.
"""
import asyncio
import logging
import os
import re
import time

import httpx
from sqlalchemy import text

from activity_log import log_event
from database import SessionLocal, engine
from incident_service import create_incident

logger = logging.getLogger("VERZIO_WHATSAPP")

GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRY_DELAYS = (1, 3)  # seconds before attempt 2 and attempt 3

# Meta's customer service window is 24 hours; keep a safety margin.
WINDOW_SECONDS = 23 * 3600
TEMPLATES_ENABLED = os.getenv("WHATSAPP_TEMPLATES", "on").strip().lower() not in ("off", "false", "0", "no")
TEMPLATE_LANG = os.getenv("WHATSAPP_TEMPLATE_LANG", "en")


# ------------------------------------------------------------------------
# Template definitions (submitted to Meta by create_templates.py)
# ------------------------------------------------------------------------
# Rules Meta enforces: a variable may not start or end the text, values may not
# contain newlines, and every template needs example values.

TEMPLATE_DEFINITIONS = {
    "booking_confirmed": {
        "text": (
            "Hi {{1}}, your booking at {{2}} is confirmed.\n\n"
            "Service: {{3}}\nWhen: {{4}}\n\n"
            "See you then!"
        ),
        "example": ["Priya", "Glow Salon", "Haircut", "Sat, 04 Oct at 3:00 PM"],
    },
    "booking_declined": {
        "text": (
            "Hi {{1}}, sorry, {{2}} couldn't confirm your booking for {{3}} on {{4}}.\n\n"
            "Reply to this message to choose another time."
        ),
        "example": ["Priya", "Glow Salon", "Haircut", "Sat, 04 Oct at 3:00 PM"],
    },
    "booking_cancelled": {
        "text": (
            "Hi {{1}}, your booking at {{2}} for {{3}} on {{4}} has been cancelled.\n\n"
            "Reply to this message to book a new time."
        ),
        "example": ["Priya", "Glow Salon", "Haircut", "Sat, 04 Oct at 3:00 PM"],
    },
    "new_booking_request": {
        "text": (
            "New booking request for {{1}}.\n\n"
            "Customer: {{2}}\nService: {{3}}\nWhen: {{4}}\nBooking ID: {{5}}\n\n"
            "Please approve or reject using the buttons below."
        ),
        "example": ["Glow Salon", "Priya Sharma (+919876543210)", "Haircut", "Sat, 04 Oct at 3:00 PM", "128"],
        "buttons": ["Approve", "Reject"],
    },
    "new_booking_alert": {
        "text": (
            "New booking for {{1}}.\n\n"
            "Customer: {{2}}\nService: {{3}}\nWhen: {{4}}\n\n"
            "This booking was confirmed automatically."
        ),
        "example": ["Glow Salon", "Priya Sharma (+919876543210)", "Haircut", "Sat, 04 Oct at 3:00 PM"],
    },
    "customer_message": {
        "text": (
            "New message for {{1}} from {{2}}:\n\n{{3}}\n\n"
            "Reply to them on WhatsApp: {{4}}\n\n"
            "The booking bot has stepped back for this customer."
        ),
        "example": ["Glow Salon", "Priya Sharma (+919876543210)",
                    "I had a facial on Monday and my skin is still red. Is that normal?",
                    "https://wa.me/919876543210"],
    },
    # v2: adds "Reschedule". (v1 with two buttons may exist on Meta; it's unused.)
    "appointment_reminder_v2": {
        "text": (
            "Hi {{1}}, this is a reminder of your appointment at {{2}}.\n\n"
            "Service: {{3}}\nWhen: {{4}}\n\n"
            "Please tap a button below to let us know if you are coming."
        ),
        "example": ["Priya", "Glow Salon", "Haircut", "Sat, 04 Oct at 3:00 PM"],
        "buttons": ["I'll be there", "Reschedule", "Cancel booking"],
    },
    "booking_rescheduled": {
        "text": (
            "Hi {{1}}, your booking at {{2}} has been moved.\n\n"
            "Service: {{3}}\nNew time: {{4}}\n\n"
            "Reply to this message if the new time doesn't work for you."
        ),
        "example": ["Priya", "Glow Salon", "Haircut", "Sat, 04 Oct at 5:00 PM"],
    },
    "customer_rescheduled": {
        "text": (
            "Booking moved at {{1}}.\n\n"
            "Customer: {{2}}\nService: {{3}}\nNew time: {{4}}\nWas: {{5}}\n\n"
            "The customer rescheduled on WhatsApp. The old time is open for new bookings again."
        ),
        "example": ["Glow Salon", "Priya Sharma (+919876543210)", "Haircut", "Sat, 04 Oct at 5:00 PM", "Sat, 04 Oct at 3:00 PM"],
    },
    "customer_cancelled": {
        "text": (
            "Booking cancelled at {{1}}.\n\n"
            "Customer: {{2}}\nService: {{3}}\nWhen: {{4}}\n\n"
            "The customer cancelled from their reminder. The time is open for new bookings again."
        ),
        "example": ["Glow Salon", "Priya Sharma (+919876543210)", "Haircut", "Sat, 04 Oct at 3:00 PM"],
    },
    "daily_summary": {
        "text": (
            "Good morning! Your day at {{1}}.\n\n"
            "Bookings today: {{2}}\nSchedule: {{3}}\nWaiting for your approval: {{4}}\n\n"
            "Open your Verzio owner portal for full details."
        ),
        "example": ["Glow Salon", "5", "10:00 AM Haircut (Priya) · 11:30 AM Facial (Anita) · 4:00 PM Manicure (Ritu)", "1"],
    },
}

# Names can be overridden per environment, e.g. WA_TEMPLATE_BOOKING_CONFIRMED=booking_confirmed_v2
TEMPLATE_NAMES = {key: os.getenv(f"WA_TEMPLATE_{key.upper()}", key) for key in TEMPLATE_DEFINITIONS}


# ------------------------------------------------------------------------
# Phone helpers
# ------------------------------------------------------------------------

def normalize_phone(raw: str | None) -> str:
    """Digits only. '+91 98765-43210' -> '919876543210'."""
    return re.sub(r"\D", "", raw or "")


# Country calling code per business timezone. India is the default market.
_TZ_COUNTRY_CODE = {
    "Asia/Kolkata": "91", "Asia/Calcutta": "91",
    "Europe/London": "44",
    "Asia/Dubai": "971",
}


def canonical_phone(raw: str | None, timezone: str | None = None) -> str:
    """Digits with country code, as WhatsApp uses them.
    '98765 43210' (India) -> '919876543210', '07700 900123' (UK) -> '447700900123',
    '050 123 4567' (UAE) -> '971501234567'. Numbers already in international
    form are left alone. Returns '' for empty input."""
    text_ = (raw or "").strip()
    digits = normalize_phone(text_)
    if not digits:
        return ""
    if text_.startswith("+"):
        return digits
    if digits.startswith("00"):
        return digits[2:]
    cc = _TZ_COUNTRY_CODE.get((timezone or "").strip(), "91")
    if digits.startswith("0"):
        national = digits.lstrip("0")
        return cc + national if national else digits
    if cc == "91" and len(digits) == 10 and digits[0] in "6789":
        return "91" + digits
    if cc == "44" and len(digits) == 10 and digits[0] == "7":
        return "44" + digits
    if cc == "971" and len(digits) == 9 and digits[0] == "5":
        return "971" + digits
    return digits


def is_real_phone(raw: str | None) -> bool:
    """True for a plausible international number (country code + number)."""
    return 11 <= len(normalize_phone(raw)) <= 15


def mask_phone(raw: str | None) -> str:
    digits = normalize_phone(raw)
    return f"***{digits[-4:]}" if digits else "unknown"


# ------------------------------------------------------------------------
# 24-hour customer service window
# ------------------------------------------------------------------------

_window_table_ready = False


def ensure_window_table() -> None:
    global _window_table_ready
    if _window_table_ready:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS conversation_windows ("
            "  business_id INTEGER NOT NULL,"
            "  phone TEXT NOT NULL,"
            "  last_inbound_at INTEGER NOT NULL,"
            "  PRIMARY KEY (business_id, phone)"
            ")"
        ))
    _window_table_ready = True


def record_inbound(business_id: int, phone: str, at: float | None = None) -> None:
    """Note that `phone` just messaged this business. Never raises."""
    digits = normalize_phone(phone)
    if not business_id or not digits:
        return
    try:
        ensure_window_table()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversation_windows (business_id, phone, last_inbound_at) "
                    "VALUES (:b, :p, :t) "
                    "ON CONFLICT (business_id, phone) DO UPDATE SET last_inbound_at = excluded.last_inbound_at"
                ),
                {"b": business_id, "p": digits, "t": int(at if at is not None else time.time())},
            )
    except Exception:
        logger.exception("Could not record inbound message for business_id=%s", business_id)


def window_open(business_id: int | None, phone: str) -> bool:
    """True if free-form messages to `phone` will be delivered right now.
    Unknown or on error -> False, so the caller prefers a template."""
    digits = normalize_phone(phone)
    if not business_id or not digits:
        return False
    try:
        ensure_window_table()
        with engine.connect() as conn:
            last = conn.execute(
                text("SELECT last_inbound_at FROM conversation_windows WHERE business_id = :b AND phone = :p"),
                {"b": business_id, "p": digits},
            ).scalar()
    except Exception:
        logger.exception("Could not read conversation window for business_id=%s", business_id)
        return False
    return last is not None and time.time() - int(last) < WINDOW_SECONDS


# ------------------------------------------------------------------------
# Message builders
# ------------------------------------------------------------------------

def build_message_body(to: str, payload: dict) -> dict:
    """Translate a runtime payload into a Meta Cloud API message body."""
    msg_type = payload.get("type")
    body = {"messaging_product": "whatsapp", "to": to}

    if msg_type == "text":
        body["type"] = "text"
        body["text"] = {"body": payload["body"]}

    elif msg_type == "list":
        interactive = {
            "type": "list",
            "body": {"text": payload.get("body", "")},
            "action": {
                "button": payload.get("button", "Select"),
                "sections": payload.get("sections", []),
            },
        }
        if payload.get("header"):
            interactive["header"] = {"type": "text", "text": payload["header"]}
        body["type"] = "interactive"
        body["interactive"] = interactive

    elif msg_type == "buttons":
        body["type"] = "interactive"
        body["interactive"] = {
            "type": "button",
            "body": {"text": payload.get("body", "")},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in payload.get("buttons", [])
                ]
            },
        }

    elif msg_type == "template":
        # payload["template"] = {"name": ..., "language": {"code": ...}, "components": [...]}
        body["type"] = "template"
        body["template"] = payload["template"]

    else:
        raise ValueError(f"Unsupported WhatsApp payload type: {msg_type!r}")

    return body


def format_when(dt) -> str:
    """'Sat, 04 Oct at 3:00 PM'"""
    return f"{dt.strftime('%a, %d %b')} at {dt.strftime('%I:%M %p').lstrip('0')}"


def _first_name(name: str | None, fallback: str = "there") -> str:
    name = (name or "").strip()
    return name.split()[0][:30] if name else fallback


def _param(value) -> str:
    """Template parameter: no newlines/tabs, no long runs of spaces, never empty."""
    cleaned = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return cleaned[:200] or "-"


def build_template(key: str, params: list, button_payloads: list[str] | None = None) -> dict:
    components = [{
        "type": "body",
        "parameters": [{"type": "text", "text": _param(p)} for p in params],
    }]
    for index, payload in enumerate(button_payloads or []):
        components.append({
            "type": "button",
            "sub_type": "quick_reply",
            "index": str(index),
            "parameters": [{"type": "payload", "payload": payload}],
        })
    return {
        "type": "template",
        "template": {
            "name": TEMPLATE_NAMES[key],
            "language": {"code": TEMPLATE_LANG},
            "components": components,
        },
    }


def build_status_message(appointment, business, previous_status: str, new_status: str) -> str | None:
    """Customer-facing free-form text for a status change, or None if the
    customer should not be messaged for this transition."""
    service_name = appointment.service.name if appointment.service else "your appointment"
    when = appointment.appointment_time
    day = when.strftime("%a, %d %b")
    clock = when.strftime("%I:%M %p").lstrip("0")

    name = (appointment.customer_name or "").strip()
    greeting = f"Hi {name.split()[0]}, " if name else "Hi, "

    if new_status == "confirmed":
        return (
            f"{greeting}your booking at {business.name} is confirmed ✅\n\n"
            f"{service_name}\n{day} at {clock}\n\n"
            f"See you then!"
        )

    if new_status == "cancelled" and previous_status == "pending":
        return (
            f"{greeting}sorry, {business.name} couldn't confirm your booking for "
            f"{service_name} on {day} at {clock}.\n\n"
            f"Send us a message any time to pick another slot."
        )

    if new_status == "cancelled":
        return (
            f"{greeting}your booking at {business.name} for {service_name} on "
            f"{day} at {clock} has been cancelled.\n\n"
            f"Send us a message any time to book a new slot."
        )

    return None


def build_status_template(appointment, business, previous_status: str, new_status: str) -> dict | None:
    """Template equivalent of build_status_message, for a closed 24h window."""
    if new_status == "confirmed":
        key = "booking_confirmed"
    elif new_status == "cancelled" and previous_status == "pending":
        key = "booking_declined"
    elif new_status == "cancelled":
        key = "booking_cancelled"
    else:
        return None
    service_name = appointment.service.name if appointment.service else "your appointment"
    return build_template(key, [
        _first_name(appointment.customer_name),
        business.name,
        service_name,
        format_when(appointment.appointment_time),
    ])


def _customer_label(appointment) -> str:
    phone = normalize_phone(appointment.customer_phone)
    phone_part = f"+{phone}" if phone else ""
    name = (appointment.customer_name or "").strip()
    if name and phone_part:
        return f"{name} ({phone_part})"
    return name or phone_part or "Customer"


def build_owner_request_template(business, appointment, service_name: str) -> dict:
    """Approve / Reject request for the owner. The quick-reply payloads are the
    same ids as the interactive buttons, so server.py handles both the same way."""
    return build_template(
        "new_booking_request",
        [business.name, _customer_label(appointment), service_name,
         format_when(appointment.appointment_time), appointment.id],
        button_payloads=[f"confirm_{appointment.id}", f"cancel_{appointment.id}"],
    )


def build_reminder_template(business, appointment, service_name: str) -> dict:
    return build_template(
        "appointment_reminder_v2",
        [_first_name(appointment.customer_name), business.name, service_name, format_when(appointment.appointment_time)],
        button_payloads=[f"remind_ok_{appointment.id}", f"remind_move_{appointment.id}", f"remind_cancel_{appointment.id}"],
    )


def build_rescheduled_template(business, appointment, service_name: str) -> dict:
    """To the customer: the owner moved their booking."""
    return build_template(
        "booking_rescheduled",
        [_first_name(appointment.customer_name), business.name, service_name, format_when(appointment.appointment_time)],
    )


def build_customer_rescheduled_template(business, appointment, service_name: str, old_time) -> dict:
    """To the owner: the customer moved their booking."""
    return build_template(
        "customer_rescheduled",
        [business.name, _customer_label(appointment), service_name,
         format_when(appointment.appointment_time), format_when(old_time)],
    )


def build_customer_cancelled_template(business, appointment, service_name: str) -> dict:
    return build_template(
        "customer_cancelled",
        [business.name, _customer_label(appointment), service_name, format_when(appointment.appointment_time)],
    )


def build_owner_alert_template(business, appointment, service_name: str) -> dict:
    return build_template(
        "new_booking_alert",
        [business.name, _customer_label(appointment), service_name, format_when(appointment.appointment_time)],
    )


# ------------------------------------------------------------------------
# Sending
# ------------------------------------------------------------------------

def _log(business_id: int | None, event_type: str, status: str, message: str) -> None:
    db = SessionLocal()
    try:
        log_event(db=db, event_type=event_type, status=status, message=message, business_id=business_id)
    except Exception:
        logger.exception("Could not write activity log")
    finally:
        db.close()


def _record_incident(business_id: int | None, to: str, title: str, reason: str, severity: str = "error") -> None:
    try:
        create_incident(
            severity=severity,
            module="WhatsApp",
            title=title,
            message=reason,
            business_id=business_id,
            phone_number=to,
        )
    except Exception:
        logger.exception("Could not record WhatsApp incident")


async def _post(recipient: str, phone_number_id: str, payload: dict, token: str | None = None) -> str | None:
    """POST one message to Meta. Returns None on success, else an error string."""
    access_token = token or os.getenv("META_ACCESS_TOKEN", "")
    if not access_token or not phone_number_id:
        return "Missing access token or phone_number_id"

    try:
        body = build_message_body(recipient, payload)
    except (KeyError, ValueError) as exc:
        return f"Invalid payload: {exc}"

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    last_error = "unknown error"
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            last_error = f"Network error: {exc}"
        else:
            if response.status_code == 200:
                return None
            last_error = f"Meta API {response.status_code}: {response.text[:300]}"
            if response.status_code not in RETRYABLE_STATUS:
                break  # 4xx (e.g. template not approved) will not succeed on retry

        if attempt < len(RETRY_DELAYS):
            await asyncio.sleep(RETRY_DELAYS[attempt])
    return last_error


def _describe(payload: dict) -> str:
    if payload.get("type") == "template":
        return f"template '{payload['template'].get('name')}'"
    return f"{payload.get('type')} message"


async def send_whatsapp(
    to: str,
    phone_number_id: str,
    payload: dict,
    business_id: int | None = None,
    template: dict | None = None,
) -> bool:
    """Send one message. Returns True when Meta accepted it. Never raises.

    `template` is the approved-template equivalent of `payload`. Pass it for
    anything that may be sent more than 24h after the recipient's last message
    (owner alerts, approval updates). It is used only when the window is closed."""
    recipient = normalize_phone(to)
    if not recipient or not payload:
        return False

    from whatsapp_accounts import account_credentials  # local import avoids a cycle
    client_token, business_number = account_credentials(business_id)
    if business_number and normalize_phone(business_number) == recipient:
        reason = ("Not sent: the recipient is the business's own WhatsApp number. Set a different "
                  "manager number, or approve bookings in the owner portal.")
        logger.warning("WhatsApp to %s skipped: %s", mask_phone(recipient), reason)
        _log(business_id, "whatsapp_skipped", "warning", reason)
        _record_incident(business_id, recipient, "Manager number is the business number", reason, severity="warning")
        return False

    if template and TEMPLATES_ENABLED and not window_open(business_id, recipient):
        error = await _post(recipient, phone_number_id, template, token=client_token)
        if error is None:
            _log(business_id, "whatsapp_sent", "success", f"Sent {_describe(template)} to {mask_phone(recipient)}")
            return True
        reason = f"{_describe(template)} not delivered ({error}). Is it approved on this business's WhatsApp account?"
        logger.warning("WhatsApp to %s: %s Falling back to a normal message.", mask_phone(recipient), reason)
        _log(business_id, "whatsapp_template_failed", "warning", reason[:500])
        _record_incident(business_id, recipient, "Message template not delivered", reason, severity="warning")
        # Fall through: the normal message still reaches anyone inside the window.

    error = await _post(recipient, phone_number_id, payload, token=client_token)
    if error is None:
        _log(business_id, "whatsapp_sent", "success", f"Sent {_describe(payload)} to {mask_phone(recipient)}")
        return True

    logger.error("WhatsApp send to %s failed: %s", mask_phone(recipient), error)
    _log(business_id, "whatsapp_failed", "error", error[:500])
    _record_incident(business_id, recipient, "Outbound message failed", error)
    return False
