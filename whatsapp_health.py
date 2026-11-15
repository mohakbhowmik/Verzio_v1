"""
whatsapp_health.py
================================================================================
VERZIO STUDIO: IS EACH CLIENT'S WHATSAPP REALLY CONNECTED?
================================================================================
Meta can silently cut a client's connection (the owner logs out of WhatsApp
Business, reinstalls, changes phone, or disconnects in the app). Verzio's own
record then still says "connected" while nothing works. This module asks Meta
directly, every hour and right after a connect:

- can Verzio still read the number and the WhatsApp account (access)?
- the number's status at Meta (CONNECTED, PENDING, ...) and, for Coexistence,
  whether it is still on the WhatsApp Business app
- is Verzio's app subscribed to the account's webhooks (fixed automatically)
- Marketing Messages enrollment (permanent; blocks reconnecting an app number)
- payment problems reported by Meta (error 131042)

Two failed checks in a row = Offline. Then Verzio:
- records an incident and shows a red badge in Admin
- creates a fresh one-time reconnect link
- if ALERT_FROM_BUSINESS_ID is set, sends a WhatsApp alert (template
  "verzio_connection_alert") to ALERT_WHATSAPP_TO (you) and to the owner's
  manager number, with the reconnect link. Repeats at most once a day.

Only a new table (whatsapp_health); no existing table is altered.
"""
import logging
import os
from datetime import datetime, timedelta

import httpx
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from database import Base, Business, SessionLocal, engine

logger = logging.getLogger("VERZIO_WA_HEALTH")

CHECK_MINUTES = int(os.getenv("WHATSAPP_HEALTH_MINUTES", "60"))
REMIND_HOURS = 24
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://verzio.codes").rstrip("/")
ALERT_TEMPLATE = "verzio_connection_alert"

# Meta's answer when the object is gone or Verzio's access was removed.
_ACCESS_LOST_HINTS = ("does not exist", "missing permissions", "cannot be loaded", "not authorized",
                      "permission", "error validating access token", "session has been invalidated",
                      "has not authorized application")


class WhatsAppHealth(Base):
    __tablename__ = "whatsapp_health"
    business_id = Column(Integer, ForeignKey("businesses.id"), primary_key=True)
    # ok | warning | offline | unknown
    state = Column(String, default="unknown", nullable=False)
    reason = Column(Text, nullable=True)
    fails_in_row = Column(Integer, default=0, nullable=False)
    checked_at = Column(DateTime, nullable=True)
    last_ok_at = Column(DateTime, nullable=True)
    offline_since = Column(DateTime, nullable=True)
    phone_status = Column(String, nullable=True)
    platform_type = Column(String, nullable=True)
    on_biz_app = Column(String, nullable=True)          # "yes" | "no" | None (unknown)
    webhooks_ok = Column(Boolean, nullable=True)
    mm_status = Column(String, nullable=True)           # Marketing Messages: ELIGIBLE, ONBOARDED, ...
    mm_alerted = Column(Boolean, default=False, nullable=False)
    payment_issue_at = Column(DateTime, nullable=True)
    payment_alerted_at = Column(DateTime, nullable=True)
    alerted_at = Column(DateTime, nullable=True)
    reconnect_token = Column(String, nullable=True)


def ensure_tables() -> None:
    Base.metadata.create_all(bind=engine, tables=[WhatsAppHealth.__table__])


def _row(db, business_id: int) -> WhatsAppHealth:
    row = db.get(WhatsAppHealth, business_id)
    if row is None:
        row = WhatsAppHealth(business_id=business_id, state="unknown", fails_in_row=0, mm_alerted=False)
        db.add(row)
    return row


# ------------------------------------------------------------------------
# Asking Meta
# ------------------------------------------------------------------------

def _graph_base() -> str:
    from whatsapp_client import GRAPH_API_VERSION
    return f"https://graph.facebook.com/{GRAPH_API_VERSION}"


async def _get(client, path: str, token: str, fields: str | None = None) -> tuple[dict | None, str | None]:
    """(json, None) on success, (None, error message) otherwise."""
    params = {"fields": fields} if fields else None
    try:
        resp = await client.get(f"{_graph_base()}/{path}", params=params,
                                headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return None, f"network: {exc}"
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200:
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        return None, err.get("message") or f"HTTP {resp.status_code}"
    return data, None


def _access_lost(message: str | None) -> bool:
    text = (message or "").lower()
    return any(h in text for h in _ACCESS_LOST_HINTS)


async def probe(client, token: str, waba_id: str | None, phone_number_id: str | None,
                coexistence: bool, app_id: str | None = None, fix_webhooks: bool = True) -> dict:
    """Ask Meta about one connection. Never raises.

    Returns {"state": ok|warning|offline|unknown, "reason": str, "phone_status", "platform_type",
             "on_biz_app", "webhooks_ok", "mm_status"}."""
    out = {"state": "unknown", "reason": "", "phone_status": None, "platform_type": None,
           "on_biz_app": None, "webhooks_ok": None, "mm_status": None}
    if not token or not phone_number_id:
        out.update(state="offline", reason="Verzio has no access key or number for this business.")
        return out

    phone, err = await _get(client, phone_number_id, token, "display_phone_number,status,platform_type")
    if phone is None and "nonexisting field" in (err or "").lower():
        # Older Graph versions may not know a field: fall back to the basics.
        phone, err = await _get(client, phone_number_id, token, "display_phone_number")
    if phone is None:
        if _access_lost(err):
            out.update(state="offline",
                       reason="Verzio lost access to this WhatsApp number (the owner logged out, reinstalled, "
                              "changed phone or disconnected in the app). It needs a reconnect.")
        else:
            out.update(state="unknown", reason=f"Couldn't check with Meta: {err}")
        return out

    out["phone_status"] = (phone.get("status") or "").upper() or None
    out["platform_type"] = phone.get("platform_type")
    problems, warnings = [], []
    if out["phone_status"] and out["phone_status"] != "CONNECTED":
        problems.append(f"Meta reports the number as {out['phone_status']}.")

    if coexistence:
        extra, _ = await _get(client, phone_number_id, token, "is_on_biz_app")
        if extra and "is_on_biz_app" in extra:
            out["on_biz_app"] = "yes" if extra.get("is_on_biz_app") else "no"
            if out["on_biz_app"] == "no":
                problems.append("The number is no longer linked to the WhatsApp Business app.")

    if waba_id:
        subs, serr = await _get(client, f"{waba_id}/subscribed_apps", token)
        if subs is not None:
            apps = [((a.get("whatsapp_business_api_data") or {}).get("id") or a.get("id")) for a in subs.get("data", [])]
            out["webhooks_ok"] = bool(apps) if not app_id else (app_id in apps)
            if not out["webhooks_ok"] and fix_webhooks:
                try:
                    resp = await client.post(f"{_graph_base()}/{waba_id}/subscribed_apps",
                                             headers={"Authorization": f"Bearer {token}"})
                    out["webhooks_ok"] = resp.status_code == 200
                except httpx.HTTPError:
                    pass
                if out["webhooks_ok"]:
                    warnings.append("Webhooks were off; Verzio switched them back on.")
            if not out["webhooks_ok"]:
                problems.append("Customer messages aren't reaching Verzio (webhooks not subscribed).")
        elif _access_lost(serr):
            problems.append("Verzio can't read this WhatsApp account any more.")

        mm, _ = await _get(client, waba_id, token, "marketing_messages_onboarding_status")
        if mm:
            out["mm_status"] = mm.get("marketing_messages_onboarding_status")
            if out["mm_status"] == "ONBOARDED" and coexistence:
                warnings.append("Enrolled in Marketing Messages: never disconnect this number, "
                                "it couldn't be reconnected through the app.")

    if problems:
        out.update(state="offline", reason=" ".join(problems + warnings))
    elif warnings:
        out.update(state="warning", reason=" ".join(warnings))
    else:
        out.update(state="ok", reason="")
    return out


# ------------------------------------------------------------------------
# Saving results and alerting
# ------------------------------------------------------------------------

def _reconnect_url(db, business_id: int, row: WhatsAppHealth) -> str:
    from whatsapp_accounts import create_onboarding_link, valid_onboarding_link
    link = valid_onboarding_link(db, row.reconnect_token) if row.reconnect_token else None
    if link is None:
        link = create_onboarding_link(db, business_id)
        row.reconnect_token = link.token
    return f"{PUBLIC_BASE_URL}/onboard/{link.token}"


def alerts_configured() -> bool:
    return bool(os.getenv("ALERT_FROM_BUSINESS_ID", "").strip())


async def send_alert(db, biz, problem: str, link: str, to_owner: bool = True) -> int:
    """WhatsApp alert from Verzio's own number (ALERT_FROM_BUSINESS_ID) to you
    (ALERT_WHATSAPP_TO) and, optionally, the owner's manager number. Returns how many were sent."""
    from whatsapp_client import build_template, canonical_phone, normalize_phone, send_whatsapp
    sender_id = os.getenv("ALERT_FROM_BUSINESS_ID", "").strip()
    if not sender_id.isdigit():
        return 0
    sender = db.get(Business, int(sender_id))
    if not sender or not sender.whatsapp_business_phone_number_id or sender.id == biz.id:
        return 0
    targets = []
    founder = normalize_phone(os.getenv("ALERT_WHATSAPP_TO", ""))
    if founder:
        targets.append(founder)
    if to_owner:
        owner = canonical_phone(biz.manager_phone_number, biz.timezone)
        if owner and normalize_phone(owner) not in targets:
            targets.append(normalize_phone(owner))
    text = f"Verzio alert for {biz.name}: {problem}\n\nReconnect here (laptop, 5 minutes): {link}"
    sent = 0
    for to in targets:
        ok = await send_whatsapp(to, sender.whatsapp_business_phone_number_id, {"type": "text", "text": {"body": text}},
                                 business_id=sender.id,
                                 template=build_template(ALERT_TEMPLATE, [biz.name, problem[:900], link]))
        sent += 1 if ok else 0
    return sent


async def record(db, business_id: int, result: dict, alert: bool = True) -> WhatsAppHealth:
    """Store one probe result; handle transitions (offline, back online, Marketing Messages)."""
    from activity_log import log_event
    from incident_service import create_incident
    row = _row(db, business_id)
    now = datetime.utcnow()
    biz = db.get(Business, business_id)
    row.checked_at = now
    for key in ("phone_status", "platform_type", "on_biz_app", "webhooks_ok", "mm_status"):
        if result.get(key) is not None:
            setattr(row, key, result[key])

    if result["state"] in ("ok", "warning"):
        was_offline = row.state == "offline"
        row.state, row.reason, row.fails_in_row = result["state"], result["reason"] or None, 0
        row.last_ok_at, row.offline_since, row.alerted_at = now, None, None
        if was_offline:
            log_event(db=db, event_type="whatsapp_back_online", status="success",
                      message="WhatsApp connection is working again", business_id=business_id)
    elif result["state"] == "offline":
        row.fails_in_row = (row.fails_in_row or 0) + 1
        row.reason = result["reason"]
        # Two failed checks in a row, so a one-off Meta hiccup doesn't alarm anyone.
        if row.fails_in_row >= 2 and row.state != "offline":
            row.state, row.offline_since = "offline", now
            log_event(db=db, event_type="whatsapp_offline", status="error", message=result["reason"][:500],
                      business_id=business_id)
            try:
                create_incident(severity="critical", module="WhatsApp", title="WhatsApp disconnected",
                                message=result["reason"], business_id=business_id)
            except Exception:
                logger.exception("Could not record incident")
        if row.state == "offline" and biz and alert and alerts_configured():
            due = row.alerted_at is None or now - row.alerted_at >= timedelta(hours=REMIND_HOURS)
            if due:
                link = _reconnect_url(db, business_id, row)
                if await send_alert(db, biz, "your WhatsApp is no longer connected to Verzio, so the bot "
                                             "isn't replying to customers.", link):
                    row.alerted_at = now
    else:  # unknown: Meta unreachable or an odd error; don't change the state
        row.reason = result["reason"] if row.state != "offline" else row.reason

    if row.mm_status == "ONBOARDED" and not row.mm_alerted:
        row.mm_alerted = True
        log_event(db=db, event_type="whatsapp_marketing_messages", status="warning",
                  message="Enrolled in Marketing Messages: don't disconnect this number (it can't be "
                          "reconnected through the app).", business_id=business_id)
        try:
            create_incident(severity="warning", module="WhatsApp", title="Enrolled in Marketing Messages",
                            message="This account is enrolled in Marketing Messages. If the owner disconnects or "
                                    "logs out, the number can't be reconnected through the app.",
                            business_id=business_id)
        except Exception:
            logger.exception("Could not record incident")
    db.commit()
    return row


async def check_business(db, business_id: int, alert: bool = True) -> WhatsAppHealth | None:
    from whatsapp_accounts import WhatsAppAccount, decrypt
    acct = db.query(WhatsAppAccount).filter(WhatsAppAccount.business_id == business_id).first()
    if acct is None or acct.status != "connected":
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        result = await probe(client, decrypt(acct.access_token_enc), acct.waba_id, acct.phone_number_id,
                             acct.coexistence, os.getenv("META_APP_ID", "").strip() or None)
    return await record(db, business_id, result, alert=alert)


async def run(db) -> int:
    """Scheduler job: check every connected account at most once per CHECK_MINUTES."""
    from whatsapp_accounts import WhatsAppAccount
    cutoff = datetime.utcnow() - timedelta(minutes=CHECK_MINUTES)
    checked = 0
    for acct in db.query(WhatsAppAccount).filter(WhatsAppAccount.status == "connected").all():
        row = db.get(WhatsAppHealth, acct.business_id)
        biz = db.get(Business, acct.business_id)
        if biz is None or not biz.is_active:
            continue
        # Re-check sooner after a first failure, to confirm or clear it.
        recent = row and row.checked_at and row.checked_at > cutoff
        if recent and not (row.fails_in_row == 1 and row.checked_at < datetime.utcnow() - timedelta(minutes=10)):
            continue
        try:
            await check_business(db, acct.business_id)
            checked += 1
        except Exception:
            db.rollback()
            logger.exception("Health check failed for business_id=%s", acct.business_id)
    return checked


def request_check(db, waba_id: str | None) -> None:
    """A webhook said something changed on this account: re-check it at the next tick."""
    from whatsapp_accounts import WhatsAppAccount
    if not waba_id:
        return
    acct = db.query(WhatsAppAccount).filter(WhatsAppAccount.waba_id == str(waba_id)).first()
    if acct is None:
        return
    row = _row(db, acct.business_id)
    row.checked_at = None
    db.commit()


# ------------------------------------------------------------------------
# Payment problems (Meta error 131042)
# ------------------------------------------------------------------------

async def note_payment_issue(business_id: int | None) -> None:
    """Called when Meta rejects a message for payment reasons. Alerts at most once a day."""
    if not business_id:
        return
    from activity_log import log_event
    from incident_service import create_incident
    db = SessionLocal()
    try:
        row = _row(db, business_id)
        now = datetime.utcnow()
        row.payment_issue_at = now
        due = row.payment_alerted_at is None or now - row.payment_alerted_at >= timedelta(hours=REMIND_HOURS)
        if due:
            row.payment_alerted_at = now
            message = ("WhatsApp messages are failing because of a payment problem: no payment method, "
                       "or the prepaid balance ran out. Add UPI funds or a payment method in WhatsApp Manager "
                       "(Payment settings).")
            log_event(db=db, event_type="whatsapp_payment_issue", status="error", message=message,
                      business_id=business_id)
            try:
                create_incident(severity="critical", module="WhatsApp", title="WhatsApp payment problem",
                                message=message, business_id=business_id)
            except Exception:
                logger.exception("Could not record incident")
            biz = db.get(Business, business_id)
            if biz and alerts_configured():
                await send_alert(db, biz, "messages are failing because WhatsApp needs a payment method or "
                                          "more prepaid balance. Add it in WhatsApp Manager → Payment settings.",
                                 "https://business.facebook.com/wa/manage/home/")
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not record payment issue for business_id=%s", business_id)
    finally:
        db.close()


def is_payment_error(error_text: str | None) -> bool:
    return "131042" in (error_text or "")
