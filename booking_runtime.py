"""
booking_runtime.py
================================================================================
VERZIO STUDIO — WHATSAPP BOOKING CONVERSATION
================================================================================
Contract with server.py:
    runtime = BookingRuntime(db)
    payload = await runtime.process_interaction(phone, tenant_id, interaction_id,
                                                customer_name=...)
The runtime never sends messages itself. It RETURNS what to send, and server.py
sends it from the correct tenant's number (with retries) via whatsapp_client:
    - a single payload  {"type": "text" | "list" | "buttons", ...}
    - or {"customer": payload, "owner": {"recipient": phone, "payload": payload}}

Flow:  any text -> services -> dates -> times -> confirm -> booked
- Sessions are scoped per (phone, business) through runtime_state.py.
- W5: a tap that doesn't match the current step (old button, out of order,
  expired session) restarts politely instead of failing silently.
- W6: dates and times are rechecked when tapped; the engine revalidates
  everything again at confirmation.
- Only dates that actually have a free slot for the chosen service are offered.
- Manual-approval businesses: the owner gets Approve / Reject buttons.
"""
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from booking_engine import (
    OCCUPYING_STATUSES,
    BookingEngineException,
    VerzioSaaSEngine,
    business_now,
    business_today,
    owner_cannot_approve,
)
from database import Appointment
from database import Business, Service
from runtime_state import RuntimeStateManager
from whatsapp_client import build_owner_alert_template, build_owner_request_template, canonical_phone

logger = logging.getLogger("VERZIO_RUNTIME")

START_IDS = {"action_start", "restart_flow", "menu"}
CONFIRM_IDS = {"action_confirm", "confirm_booking"}      # second id: buttons sent by an older build
CANCEL_IDS = {"action_cancel"}
SESSION_TTL = timedelta(hours=2)
# Upcoming bookings one WhatsApp number may hold at a business (stops one person blocking a day).
MAX_UPCOMING_PER_CUSTOMER = int(os.getenv("MAX_UPCOMING_PER_CUSTOMER", "2"))
MAX_LIST_ROWS = 10           # WhatsApp list limit
SLOTS_PER_PAGE = 9           # leaves one row for "Later times"
SERVICES_PER_PAGE = 9        # leaves one row for "More services"

STALE_NOTICE = "That option has expired. Let's start again:"


# ------------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------------

def _clock(hhmm: str) -> str:
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def _day_label(day, today) -> str:
    base = day.strftime("%a, %d %b")
    if day == today:
        return f"Today · {base}"
    if day == today + timedelta(days=1):
        return f"Tomorrow · {base}"
    return base


def _price(service: Service) -> str:
    return f"₹{service.price:,.0f}" if service.price is not None else ""


def _service_line(service: Service) -> str:
    price = _price(service)
    return f"{price} · {service.duration} min" if price else f"{service.duration} min"


def _first_name(name: str | None) -> str:
    name = (name or "").strip()
    return name.split()[0][:30] if name else ""


class BookingRuntime:
    def __init__(self, db: Session):
        self.db = db
        self.state = RuntimeStateManager(db)
        self.engine = VerzioSaaSEngine()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def process_interaction(
        self,
        phone: str,
        tenant_id: str,
        interaction_id: str,
        customer_name: str | None = None,
    ):
        # Raises BookingEngineException(ERR_TENANT_LOCKED / ERR_NOT_FOUND);
        # server.py turns that into a polite reply.
        biz = self.engine.get_tenant_config(self.db, tenant_id)

        try:
            return self._dispatch(biz, phone, tenant_id, interaction_id or "", customer_name)
        except BookingEngineException:
            raise
        except Exception:
            # Never leave a patient without a reply.
            logger.exception("Runtime error for business_id=%s interaction=%r", biz.id, interaction_id)
            self.db.rollback()
            self.state.clear_session(phone, business_id=biz.id)
            return self._render_services(phone, biz, notice="Sorry, something went wrong on our side. Let's try again:")

    def _dispatch(self, biz: Business, phone: str, tenant_id: str, iid: str, customer_name: str | None):
        session = self.state.get_or_create_session(phone, business_id=biz.id)

        if iid in START_IDS:
            limit_reply = self._upcoming_limit_reply(phone, biz)
            if limit_reply:
                return limit_reply
            return self._render_services(phone, biz, name=customer_name)

        expired = (
            session.step not in (None, "START")
            and session.updated_at is not None
            and datetime.utcnow() - session.updated_at > SESSION_TTL
        )
        if expired:
            return self._render_services(phone, biz, notice=STALE_NOTICE)

        step = session.step
        if iid.startswith("svcpage_") and step == "SELECT_SERVICE":
            try:
                page = int(iid[len("svcpage_"):])
            except ValueError:
                page = 0
            return self._render_services(phone, biz, name=customer_name, page=page)
        if iid.startswith("svc_") and step == "SELECT_SERVICE":
            return self._on_service(phone, biz, iid)
        if iid.startswith("date_") and step == "SELECT_DATE":
            return self._on_date(phone, biz, session, iid)
        if (iid.startswith("time_") or iid.startswith("page_")) and step == "SELECT_TIME":
            return self._on_time(phone, biz, session, iid)
        if iid in CONFIRM_IDS and step == "CONFIRM":
            return self._finalize(phone, biz, session, tenant_id, customer_name)
        if iid in CANCEL_IDS and step == "CONFIRM":
            self.state.clear_session(phone, business_id=biz.id)
            return {"type": "text", "body": "No problem, nothing was booked. Send \"Hi\" whenever you'd like to book."}

        # W5: old button, out-of-order tap, or unknown id.
        logger.info("Stale tap %r at step %s for business_id=%s", iid, step, biz.id)
        return self._render_services(phone, biz, notice=STALE_NOTICE)

    # ------------------------------------------------------------------
    # Per-customer limit
    # ------------------------------------------------------------------

    def _upcoming(self, phone: str, biz: Business) -> list:
        return (
            self.db.query(Appointment)
            .filter(
                Appointment.business_id == biz.id,
                Appointment.customer_phone == phone,
                Appointment.status.in_(OCCUPYING_STATUSES),
                Appointment.appointment_time > business_now(biz),
            )
            .order_by(Appointment.appointment_time.asc())
            .all()
        )

    def _upcoming_limit_reply(self, phone: str, biz: Business):
        if MAX_UPCOMING_PER_CUSTOMER <= 0:
            return None
        upcoming = self._upcoming(phone, biz)
        if len(upcoming) < MAX_UPCOMING_PER_CUSTOMER:
            return None
        self.state.clear_session(phone, business_id=biz.id)
        today = business_today(biz)
        lines = []
        for appt in upcoming[:5]:
            service = appt.service.name if appt.service else "Appointment"
            when = f"{_day_label(appt.appointment_time.date(), today)} at {_clock(appt.appointment_time.strftime('%H:%M'))}"
            pending = " (waiting for confirmation)" if appt.status == "pending" else ""
            lines.append(f"• {service}, {when}{pending}")
        return {
            "type": "buttons",
            "body": (
                f"You already have {len(upcoming)} upcoming bookings at {biz.name}:\n\n"
                + "\n".join(lines)
                + "\n\nTo change or cancel one, or to book more, tap below and the team will help."
            )[:1024],
            "buttons": [{"id": "action_human", "title": "Talk to us"}],
        }

    # ------------------------------------------------------------------
    # Step 1: services
    # ------------------------------------------------------------------

    def _active_services(self, biz: Business) -> list[Service]:
        return (
            self.db.query(Service)
            .filter(
                Service.business_id == biz.id,
                Service.is_active == True,
                Service.is_deleted == False,
            )
            .order_by(Service.name.asc())
            .all()
        )

    def _render_services(self, phone: str, biz: Business, notice: str | None = None, name: str | None = None,
                         page: int = 0):
        services = self._active_services(biz)
        if not services:
            self.state.clear_session(phone, business_id=biz.id)
            return {"type": "text", "body": f"Sorry, {biz.name} has no services available for online booking right now."}

        self.state.update_session(
            phone, business_id=biz.id, step="SELECT_SERVICE",
            selected_service_id=None, selected_date=None, selected_time=None,
        )

        # Businesses that turned off service selection skip straight to dates.
        if not biz.enable_service_selection:
            return self._on_service(phone, biz, f"svc_{services[0].id}", notice=notice)

        # More than 10 services: 9 per page plus "More services" / "Back".
        pages = [services] if len(services) <= MAX_LIST_ROWS else [
            services[i:i + SERVICES_PER_PAGE] for i in range(0, len(services), SERVICES_PER_PAGE)
        ]
        page = page if 0 <= page < len(pages) else 0
        chunk = pages[page]
        rows = [{"id": f"svc_{s.id}", "title": s.name[:24], "description": _service_line(s)[:72]} for s in chunk]
        if page + 1 < len(pages):
            rows.append({"id": f"svcpage_{page + 1}", "title": "More services →",
                         "description": f"Page {page + 2} of {len(pages)}"})
        elif page > 0:
            rows.append({"id": "svcpage_0", "title": "← Back to start", "description": "First services"})

        first = _first_name(name)
        greeting = f"Hi {first}! 👋 " if first else "Hi! 👋 "
        if page == 0:
            body = f"{greeting}Welcome to {biz.name}.\n\nWhich service would you like to book?"
        else:
            body = f"More services at {biz.name} (page {page + 1} of {len(pages)}):"
        if notice:
            body = f"{notice}\n\n{body}"

        return {
            "type": "list",
            "header": biz.name[:60],
            "body": body,
            "button": "View services",
            "sections": [{"title": "Services", "rows": rows[:MAX_LIST_ROWS]}],
        }

    def _on_service(self, phone: str, biz: Business, iid: str, notice: str | None = None):
        try:
            service_id = int(iid[len("svc_"):])
        except ValueError:
            return self._render_services(phone, biz, notice=STALE_NOTICE)

        service = self.engine.get_service_for_business(self.db, biz.id, service_id)
        if service is None:
            return self._render_services(phone, biz, notice="That service isn't available any more. Please choose another:")

        self.state.update_session(phone, business_id=biz.id, step="SELECT_DATE", selected_service_id=service.id)
        return self._render_dates(phone, biz, service, notice=notice)

    # ------------------------------------------------------------------
    # Step 2: dates
    # ------------------------------------------------------------------

    def _bookable_dates(self, biz: Business, service: Service) -> list[tuple]:
        """(date, free slot count) for open dates in the window that have room."""
        result = []
        for day in self.engine.get_available_dates(self.db, biz, limit=31):
            slots = self.engine.get_available_slots(self.db, biz, day, service)
            if slots:
                result.append((day, len(slots)))
            if len(result) >= MAX_LIST_ROWS:
                break
        return result

    def _render_dates(self, phone: str, biz: Business, service: Service, notice: str | None = None):
        dates = self._bookable_dates(biz, service)
        if not dates:
            self.state.clear_session(phone, business_id=biz.id)
            return {
                "type": "text",
                "body": f"Sorry, {service.name} is fully booked for the next few days. "
                        f"Send \"Hi\" to choose another service, or contact {biz.name} directly.",
            }

        today = business_today(biz)
        body = f"Great choice: {service.name} ({_service_line(service)}).\n\nWhich day suits you?"
        if notice:
            body = f"{notice}\n\n{body}"

        return {
            "type": "list",
            "header": "Choose a date",
            "body": body,
            "button": "Pick a date",
            "sections": [{
                "title": "Available days",
                "rows": [
                    {
                        "id": f"date_{day.isoformat()}",
                        "title": _day_label(day, today)[:24],
                        "description": f"{count} time{'s' if count != 1 else ''} free",
                    }
                    for day, count in dates
                ],
            }],
        }

    def _current_service(self, phone: str, biz: Business, session):
        service = self.engine.get_service_for_business(self.db, biz.id, session.selected_service_id)
        if service is None:
            return None, self._render_services(phone, biz, notice="That service isn't available any more. Please choose another:")
        return service, None

    def _on_date(self, phone: str, biz: Business, session, iid: str):
        service, fallback = self._current_service(phone, biz, session)
        if fallback:
            return fallback

        try:
            day = datetime.strptime(iid[len("date_"):], "%Y-%m-%d").date()
        except ValueError:
            return self._render_dates(phone, biz, service, notice=STALE_NOTICE)

        if day not in self.engine.get_available_dates(self.db, biz, limit=31):
            return self._render_dates(phone, biz, service, notice="That day isn't available. Please pick another:")

        slots = self.engine.get_available_slots(self.db, biz, day, service)
        if not slots:
            return self._render_dates(phone, biz, service, notice="That day just filled up. Please pick another:")

        self.state.update_session(phone, business_id=biz.id, step="SELECT_TIME", selected_date=day.isoformat())
        return self._render_times(biz, service, day, slots, page=0)

    # ------------------------------------------------------------------
    # Step 3: times
    # ------------------------------------------------------------------

    def _render_times(self, biz: Business, service: Service, day, slots: list[str], page: int, notice: str | None = None):
        start = page * SLOTS_PER_PAGE
        chunk = slots[start:start + SLOTS_PER_PAGE]
        if not chunk:                       # page out of range: show the first page
            page, chunk = 0, slots[:SLOTS_PER_PAGE]

        rows = [{"id": f"time_{s}", "title": _clock(s), "description": f"{service.duration} min"} for s in chunk]
        if len(slots) > (page + 1) * SLOTS_PER_PAGE:
            rows.append({"id": f"page_{page + 1}", "title": "Later times →", "description": "See more times"})
        elif page > 0:
            rows.append({"id": "page_0", "title": "← Earlier times", "description": "Back to the first times"})

        today = business_today(biz)
        body = f"{service.name} on {_day_label(day, today)}.\n\nPick a time:"
        if notice:
            body = f"{notice}\n\n{body}"

        return {
            "type": "list",
            "header": "Choose a time",
            "body": body,
            "button": "Pick a time",
            "sections": [{"title": "Free times", "rows": rows[:MAX_LIST_ROWS]}],
        }

    def _on_time(self, phone: str, biz: Business, session, iid: str):
        service, fallback = self._current_service(phone, biz, session)
        if fallback:
            return fallback

        try:
            day = datetime.strptime(session.selected_date or "", "%Y-%m-%d").date()
        except ValueError:
            return self._render_dates(phone, biz, service, notice=STALE_NOTICE)

        slots = self.engine.get_available_slots(self.db, biz, day, service)
        if not slots:
            self.state.update_session(phone, business_id=biz.id, step="SELECT_DATE", selected_date=None)
            return self._render_dates(phone, biz, service, notice="That day just filled up. Please pick another:")

        if iid.startswith("page_"):
            try:
                page = int(iid[len("page_"):])
            except ValueError:
                page = 0
            self.state.update_session(phone, business_id=biz.id, step="SELECT_TIME")
            return self._render_times(biz, service, day, slots, page=page)

        hhmm = iid[len("time_"):]
        if hhmm not in slots:
            return self._render_times(biz, service, day, slots, page=0,
                                      notice="Sorry, that time was just taken. Please pick another:")

        self.state.update_session(phone, business_id=biz.id, step="CONFIRM", selected_time=hhmm)

        price = _price(service)
        body = (
            f"Please confirm your booking at {biz.name}:\n\n"
            f"• {service.name}{f' ({price})' if price else ''}\n"
            f"• {day.strftime('%A, %d %B')}\n"
            f"• {_clock(hhmm)} · {service.duration} min"
        )
        return {
            "type": "buttons",
            "body": body,
            "buttons": [
                {"id": "action_confirm", "title": "Confirm ✅"},
                {"id": "action_cancel", "title": "Cancel"},
            ],
        }

    # ------------------------------------------------------------------
    # Step 4: book
    # ------------------------------------------------------------------

    def _finalize(self, phone: str, biz: Business, session, tenant_id: str, customer_name: str | None):
        service, fallback = self._current_service(phone, biz, session)
        if fallback:
            return fallback

        try:
            start = datetime.strptime(f"{session.selected_date} {session.selected_time}", "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return self._render_services(phone, biz, notice=STALE_NOTICE)

        # Customer policy: stay inside the advance booking window (checked again
        # here because the confirm button may be tapped long after it was sent).
        window_days = max(int(biz.advance_booking_days or 14), 1)
        if (start.date() - business_today(biz)).days >= window_days:
            self.state.update_session(phone, business_id=biz.id, step="SELECT_DATE", selected_date=None, selected_time=None)
            return self._render_dates(phone, biz, service, notice="That date is too far ahead. Please pick another:")

        limit_reply = self._upcoming_limit_reply(phone, biz)
        if limit_reply:
            return limit_reply

        try:
            appt = self.engine.validate_and_book(self.db, tenant_id, start, phone, service.id, customer_name)
        except BookingEngineException as exc:
            return self._booking_failed(phone, biz, service, start, exc)

        self.state.clear_session(phone, business_id=biz.id)

        when = f"{start.strftime('%A, %d %B')} at {_clock(start.strftime('%H:%M'))}"
        if appt.status == "confirmed":
            customer_text = (
                f"✅ You're booked!\n\n{service.name}\n{when}\n\n"
                f"See you at {biz.name}."
            )
        else:
            customer_text = (
                f"⏳ Request received!\n\n{service.name}\n{when}\n\n"
                f"{biz.name} will confirm shortly. We'll message you here."
            )
        response = {"customer": {"type": "text", "body": customer_text}}

        owner_payload, owner_template = self._owner_notification(biz, appt, service, when)
        if owner_payload:
            # The owner may not have messaged this number in 24h, so the
            # template is sent instead whenever their window is closed.
            response["owner"] = {
                "recipient": canonical_phone(biz.manager_phone_number, biz.timezone),
                "payload": owner_payload,
                "template": owner_template,
            }
        return response

    def _booking_failed(self, phone: str, biz: Business, service: Service, start: datetime, exc: BookingEngineException):
        code = exc.error_code
        logger.info("Booking rejected (%s) for business_id=%s", code, biz.id)

        if code in ("ERR_CAPACITY", "ERR_PAST_TIME", "ERR_OUTSIDE_HOURS", "ERR_BUSY"):
            notice = {
                "ERR_CAPACITY": "Sorry, that time was just taken.",
                "ERR_PAST_TIME": "Sorry, that time has already passed.",
                "ERR_OUTSIDE_HOURS": "Sorry, that time is no longer available.",
                "ERR_BUSY": "Sorry, we couldn't save that just now.",
            }[code]
            slots = self.engine.get_available_slots(self.db, biz, start.date(), service)
            if slots:
                self.state.update_session(phone, business_id=biz.id, step="SELECT_TIME", selected_time=None)
                return self._render_times(biz, service, start.date(), slots, page=0,
                                          notice=f"{notice} Please pick another time:")
            self.state.update_session(phone, business_id=biz.id, step="SELECT_DATE", selected_date=None, selected_time=None)
            return self._render_dates(phone, biz, service, notice=f"{notice} Please pick another day:")

        if code == "ERR_HOLIDAY":
            self.state.update_session(phone, business_id=biz.id, step="SELECT_DATE", selected_date=None, selected_time=None)
            return self._render_dates(phone, biz, service, notice=f"{biz.name} is closed that day. Please pick another:")

        if code == "ERR_INVALID_SERVICE":
            return self._render_services(phone, biz, notice="That service isn't available any more. Please choose another:")

        if code == "ERR_TENANT_LOCKED":
            raise exc  # server.py sends the "not taking bookings" reply

        self.state.clear_session(phone, business_id=biz.id)
        return {"type": "text", "body": "Sorry, we couldn't complete your booking. Send \"Hi\" to try again."}

    @staticmethod
    def _owner_notification(biz: Business, appt, service: Service, when: str):
        customer = appt.customer_name or "Customer"
        details = (
            f"Customer: {customer} (+{appt.customer_phone})\n"
            f"Service: {service.name}\n"
            f"When: {when}\n"
            f"Booking #{appt.id}"
        )
        if appt.status == "pending":
            return (
                {
                    "type": "buttons",
                    "body": f"📅 New booking request\n\n{details}",
                    "buttons": [
                        {"id": f"confirm_{appt.id}", "title": "Approve ✅"},
                        {"id": f"cancel_{appt.id}", "title": "Reject ❌"},
                    ],
                },
                build_owner_request_template(biz, appt, service.name),
            )
        prefs = biz.notification_preferences or {}
        if owner_cannot_approve(biz):
            return None, None      # can't message itself; owner sees the booking in the app + portal
        if prefs.get("whatsapp_owner", True):
            return (
                {"type": "text", "body": f"📅 New booking (auto-confirmed)\n\n{details}"},
                build_owner_alert_template(biz, appt, service.name),
            )
        return None, None
