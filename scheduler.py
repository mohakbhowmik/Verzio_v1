"""
scheduler.py
================================================================================
VERZIO STUDIO — TIMED JOBS
================================================================================
Runs inside the app (one worker, see Dockerfile) every SCHEDULER_TICK_SECONDS:

1. Reminders     Confirmed bookings get a WhatsApp reminder about a day ahead
                 (between REMINDER_MIN_LEAD_HOURS and REMINDER_HOURS before),
                 with "I'll be there" / "Cancel booking" buttons. Only sent
                 between 08:00 and 21:00 business time.
2. Nudge         A request still pending after PENDING_NUDGE_HOURS is sent to
                 the owner once more.
3. Expiry        A request still pending PENDING_EXPIRE_MINUTES before the
                 appointment is declined, and the customer is told, so nobody
                 turns up to an unconfirmed booking.
4. Morning       At SUMMARY_HOUR business time the owner gets today's schedule.
   summary

Every send is claimed in scheduled_sends first, so a restart or two overlapping
ticks can never send the same thing twice.

Set SCHEDULER=off to disable (the tests drive tick() directly).
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

from sqlalchemy import text

from activity_log import log_event
from booking_engine import business_now, owner_cannot_approve
from database import Appointment, Business, SessionLocal, engine
from whatsapp_client import (
    _first_name,
    build_customer_cancelled_template,
    build_owner_request_template,
    build_reminder_template,
    build_status_message,
    build_status_template,
    build_template,
    canonical_phone,
    format_when,
    is_real_phone,
    send_whatsapp,
)

logger = logging.getLogger("VERZIO_SCHEDULER")

TICK_SECONDS = int(os.getenv("SCHEDULER_TICK_SECONDS", "300"))
REMINDER_HOURS = float(os.getenv("REMINDER_HOURS", "24"))
REMINDER_MIN_LEAD_HOURS = float(os.getenv("REMINDER_MIN_LEAD_HOURS", "2"))
REMINDER_SKIP_IF_BOOKED_WITHIN_HOURS = float(os.getenv("REMINDER_SKIP_IF_BOOKED_WITHIN_HOURS", "3"))
QUIET_START, QUIET_END = 21, 8           # no customer reminders from 9 pm to 8 am
PENDING_NUDGE_HOURS = float(os.getenv("PENDING_NUDGE_HOURS", "2"))
PENDING_EXPIRE_MINUTES = int(os.getenv("PENDING_EXPIRE_MINUTES", "60"))
SUMMARY_HOUR = int(os.getenv("SUMMARY_HOUR", "8"))
SUMMARY_LATEST_HOUR = 12                 # after noon a "good morning" summary is pointless

_table_ready = False


# ------------------------------------------------------------------------
# Claims
# ------------------------------------------------------------------------

def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS scheduled_sends ("
            "  kind TEXT NOT NULL, ref TEXT NOT NULL, sent_at INTEGER NOT NULL,"
            "  PRIMARY KEY (kind, ref))"
        ))
    _table_ready = True


def claim(kind: str, ref: str) -> bool:
    """True the first time (kind, ref) is claimed, False afterwards."""
    _ensure_table()
    sql = ("INSERT OR IGNORE INTO scheduled_sends (kind, ref, sent_at) VALUES (:k, :r, :t)"
           if engine.dialect.name == "sqlite" else
           "INSERT INTO scheduled_sends (kind, ref, sent_at) VALUES (:k, :r, :t) ON CONFLICT DO NOTHING")
    with engine.begin() as conn:
        return conn.execute(text(sql), {"k": kind, "r": str(ref), "t": int(time.time())}).rowcount == 1


def already_claimed(kind: str, ref: str) -> bool:
    _ensure_table()
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM scheduled_sends WHERE kind = :k AND ref = :r"),
                            {"k": kind, "r": str(ref)}).first() is not None


# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------

def _service_name(appt) -> str:
    return appt.service.name if appt.service else "your appointment"


def _manager(biz) -> str | None:
    number = canonical_phone(biz.manager_phone_number, biz.timezone)
    return number if is_real_phone(number) else None


def _prefs(biz) -> dict:
    return biz.notification_preferences or {}


def _live_businesses(db):
    # Skip businesses that haven't connected WhatsApp yet (placeholder number id).
    return [b for b in db.query(Business).filter(Business.is_active == True).all()  # noqa: E712
            if b.whatsapp_business_phone_number_id and not b.whatsapp_business_phone_number_id.startswith("pending-")]


# ------------------------------------------------------------------------
# 1. Reminders
# ------------------------------------------------------------------------

async def send_reminders(db) -> int:
    sent = 0
    for biz in _live_businesses(db):
        if not _prefs(biz).get("reminders", True):
            continue
        now = business_now(biz)
        if not (QUIET_END <= now.hour < QUIET_START):
            continue
        rows = (
            db.query(Appointment)
            .filter(
                Appointment.business_id == biz.id,
                Appointment.status == "confirmed",
                Appointment.appointment_time > now + timedelta(hours=REMINDER_MIN_LEAD_HOURS),
                Appointment.appointment_time <= now + timedelta(hours=REMINDER_HOURS),
            )
            .all()
        )
        for appt in rows:
            booked_recently = (
                appt.created_at is not None
                and datetime.utcnow() - appt.created_at < timedelta(hours=REMINDER_SKIP_IF_BOOKED_WITHIN_HOURS)
            )
            if booked_recently or not is_real_phone(appt.customer_phone) or not claim("reminder", appt.id):
                continue
            service = _service_name(appt)
            body = (
                f"Hi {_first_name(appt.customer_name)}, a reminder of your appointment at {biz.name}:\n\n"
                f"{service}\n{format_when(appt.appointment_time)}\n\nAre you coming?"
            )
            payload = {
                "type": "buttons",
                "body": body,
                "buttons": [
                    {"id": f"remind_ok_{appt.id}", "title": "I'll be there"},
                    {"id": f"remind_cancel_{appt.id}", "title": "Cancel booking"},
                ],
            }
            ok = await send_whatsapp(appt.customer_phone, biz.whatsapp_business_phone_number_id, payload, biz.id,
                                     template=build_reminder_template(biz, appt, service))
            sent += 1 if ok else 0
            log_event(db=db, event_type="reminder_sent" if ok else "reminder_failed",
                      status="info" if ok else "warning",
                      message=f"Reminder for booking #{appt.id}" + ("" if ok else " could not be delivered"),
                      business_id=biz.id)
    return sent


# ------------------------------------------------------------------------
# 2 + 3. Pending requests the owner hasn't answered
# ------------------------------------------------------------------------

async def handle_pending(db) -> tuple[int, int]:
    nudged = expired = 0
    for biz in _live_businesses(db):
        now = business_now(biz)
        rows = (
            db.query(Appointment)
            .filter(Appointment.business_id == biz.id, Appointment.status == "pending")
            .all()
        )
        for appt in rows:
            pnid = biz.whatsapp_business_phone_number_id
            # Expiry first: too close to the time (or already past) to leave it open.
            if appt.appointment_time <= now + timedelta(minutes=PENDING_EXPIRE_MINUTES):
                if not claim("expire", appt.id):
                    continue
                appt.status = "cancelled"
                db.commit()
                expired += 1
                log_event(db=db, event_type="booking_expired", status="warning",
                          message=f"Booking #{appt.id} wasn't approved in time and was declined automatically",
                          business_id=biz.id)
                if appt.appointment_time > now and is_real_phone(appt.customer_phone):
                    await send_whatsapp(
                        appt.customer_phone, pnid,
                        {"type": "text", "body": build_status_message(appt, biz, "pending", "cancelled")},
                        biz.id, template=build_status_template(appt, biz, "pending", "cancelled"),
                    )
                manager = _manager(biz)
                if manager and appt.appointment_time > now and not owner_cannot_approve(biz):
                    # FYI only; free-form, so it's sent only if the owner's window is open.
                    await send_whatsapp(manager, pnid, {"type": "text", "body": (
                        f"Booking #{appt.id} ({_service_name(appt)}, {format_when(appt.appointment_time)}) "
                        f"wasn't approved in time, so the customer was told it couldn't be confirmed."
                    )}, biz.id)
                continue

            # Nudge once if it has been waiting a while.
            waiting = appt.created_at is not None and datetime.utcnow() - appt.created_at >= timedelta(hours=PENDING_NUDGE_HOURS)
            manager = _manager(biz)
            if not waiting or not manager or owner_cannot_approve(biz) or not claim("nudge", appt.id):
                continue
            service = _service_name(appt)
            customer = appt.customer_name or "Customer"
            payload = {
                "type": "buttons",
                "body": (
                    f"⏰ Still waiting for your answer\n\n"
                    f"Customer: {customer} (+{appt.customer_phone})\nService: {service}\n"
                    f"When: {format_when(appt.appointment_time)}\nBooking #{appt.id}\n\n"
                    f"If there's no answer {PENDING_EXPIRE_MINUTES} minutes before the time, "
                    f"we'll tell the customer it couldn't be confirmed."
                ),
                "buttons": [
                    {"id": f"confirm_{appt.id}", "title": "Approve ✅"},
                    {"id": f"cancel_{appt.id}", "title": "Reject ❌"},
                ],
            }
            if await send_whatsapp(manager, pnid, payload, biz.id,
                                   template=build_owner_request_template(biz, appt, service)):
                nudged += 1
    return nudged, expired


# ------------------------------------------------------------------------
# 4. Morning summary
# ------------------------------------------------------------------------

def _summary_lines(appts) -> list[str]:
    lines = []
    for a in appts:
        clock = a.appointment_time.strftime("%I:%M %p").lstrip("0")
        who = _first_name(a.customer_name, "customer")
        flag = " (pending)" if a.status == "pending" else ""
        lines.append(f"{clock} {_service_name(a)} ({who}){flag}")
    return lines


async def send_morning_summaries(db) -> int:
    sent = 0
    for biz in _live_businesses(db):
        if not _prefs(biz).get("morning_summary", True):
            continue
        now = business_now(biz)
        if not (SUMMARY_HOUR <= now.hour < SUMMARY_LATEST_HOUR):
            continue
        manager = _manager(biz)
        if not manager or owner_cannot_approve(biz):
            continue
        if not claim("summary", f"{biz.id}:{now.date().isoformat()}"):
            continue
        start = datetime.combine(now.date(), datetime.min.time())
        appts = (
            db.query(Appointment)
            .filter(
                Appointment.business_id == biz.id,
                Appointment.status.in_(("pending", "confirmed")),
                Appointment.appointment_time >= start,
                Appointment.appointment_time < start + timedelta(days=1),
            )
            .order_by(Appointment.appointment_time.asc())
            .all()
        )
        if not appts:
            continue                       # nothing to report: don't spend a message
        lines = _summary_lines(appts)
        pending = sum(1 for a in appts if a.status == "pending")
        body = (
            f"☀️ Good morning! Today at {biz.name}: {len(appts)} booking{'s' if len(appts) != 1 else ''}\n\n"
            + "\n".join(lines[:30])
            + (f"\n…and {len(lines) - 30} more" if len(lines) > 30 else "")
            + (f"\n\n⏳ {pending} waiting for your approval" if pending else "")
        )
        short = []
        for line in lines:
            if len(" · ".join(short + [line])) > 170:
                break
            short.append(line)
        schedule = " · ".join(short) + (f" · +{len(lines) - len(short)} more" if len(short) < len(lines) else "")
        template = build_template("daily_summary", [biz.name, len(appts), schedule, pending])
        if await send_whatsapp(manager, biz.whatsapp_business_phone_number_id,
                               {"type": "text", "body": body[:4000]}, biz.id, template=template):
            sent += 1
    return sent


# ------------------------------------------------------------------------
# Loop
# ------------------------------------------------------------------------

async def tick() -> dict:
    db = SessionLocal()
    result = {}
    try:
        for name, job in (("reminders", send_reminders), ("pending", handle_pending),
                          ("summaries", send_morning_summaries)):
            try:
                result[name] = await job(db)
            except Exception:
                db.rollback()
                logger.exception("Scheduled job %s failed", name)
    finally:
        db.close()
    return result


async def run_forever() -> None:
    logger.info("Scheduler started: every %ss", TICK_SECONDS)
    await asyncio.sleep(20)                  # let the app finish starting
    while True:
        try:
            result = await tick()
            if any(v for v in result.values() if v not in (0, (0, 0))):
                logger.info("Scheduler tick: %s", result)
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)


def enabled() -> bool:
    return os.getenv("SCHEDULER", "on").strip().lower() not in ("off", "false", "0", "no")
