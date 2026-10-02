"""
whatsapp_client.py
================================================================================
VERZIO STUDIO — OUTBOUND WHATSAPP (single source of truth)
================================================================================
Every outbound WhatsApp message goes through send_whatsapp(). It is shared by
the webhook (server.py) and the owner portal (owner/owner_appointments.py), so
neither module has to import the other.

- Retries network errors, 429 and 5xx responses with a short backoff.
- Logs success/failure with its OWN database session, so a logging failure can
  never roll back the caller's business transaction.
- Never logs full phone numbers.
"""
import asyncio
import logging
import os
import re

import httpx

from activity_log import log_event
from database import SessionLocal
from incident_service import create_incident

logger = logging.getLogger("VERZIO_WHATSAPP")

GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRY_DELAYS = (1, 3)  # seconds before attempt 2 and attempt 3


# ------------------------------------------------------------------------
# Phone helpers
# ------------------------------------------------------------------------

def normalize_phone(raw: str | None) -> str:
    """Digits only. '+91 98765-43210' -> '919876543210'."""
    return re.sub(r"\D", "", raw or "")


def is_real_phone(raw: str | None) -> bool:
    """True for a plausible international number (country code + number)."""
    return 11 <= len(normalize_phone(raw)) <= 15


def mask_phone(raw: str | None) -> str:
    digits = normalize_phone(raw)
    return f"***{digits[-4:]}" if digits else "unknown"


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


def build_status_message(appointment, business, previous_status: str, new_status: str) -> str | None:
    """Customer-facing text for a status change, or None if the customer
    should not be messaged for this transition."""
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


# ------------------------------------------------------------------------
# Sending
# ------------------------------------------------------------------------

def _log(business_id: int | None, event_type: str, status: str, message: str) -> None:
    db = SessionLocal()
    try:
        log_event(db=db, event_type=event_type, status=status, message=message, business_id=business_id)
    finally:
        db.close()


def _record_failure(business_id: int | None, to: str, reason: str) -> None:
    logger.error("WhatsApp send to %s failed: %s", mask_phone(to), reason)
    _log(business_id, "whatsapp_failed", "error", reason[:500])
    try:
        create_incident(
            severity="error",
            module="WhatsApp",
            title="Outbound message failed",
            message=reason,
            business_id=business_id,
            phone_number=to,
        )
    except Exception:
        logger.exception("Could not record WhatsApp incident")


async def send_whatsapp(
    to: str,
    phone_number_id: str,
    payload: dict,
    business_id: int | None = None,
) -> bool:
    """Send one message. Returns True on success. Never raises."""
    recipient = normalize_phone(to)
    if not recipient or not payload:
        return False

    access_token = os.getenv("META_ACCESS_TOKEN", "")
    if not access_token or not phone_number_id:
        _record_failure(business_id, recipient, "Missing META_ACCESS_TOKEN or phone_number_id")
        return False

    try:
        body = build_message_body(recipient, payload)
    except (KeyError, ValueError) as exc:
        _record_failure(business_id, recipient, f"Invalid payload: {exc}")
        return False

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
                _log(
                    business_id,
                    "whatsapp_sent",
                    "success",
                    f"Sent {payload.get('type')} message to {mask_phone(recipient)}",
                )
                return True
            last_error = f"Meta API {response.status_code}: {response.text[:300]}"
            if response.status_code not in RETRYABLE_STATUS:
                break  # 4xx such as 131047 (outside 24h window) will not succeed on retry

        if attempt < len(RETRY_DELAYS):
            await asyncio.sleep(RETRY_DELAYS[attempt])

    _record_failure(business_id, recipient, last_error)
    return False
