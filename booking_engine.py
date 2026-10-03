"""
booking_engine.py
================================================================================
VERZIO STUDIO — CALENDAR & BOOKING RULES (single source of truth)
================================================================================
Used by the WhatsApp runtime (booking_runtime.py) and the owner portal
(owner/owner_appointments.py). Both call the same capacity rules, so what a
patient is offered and what the engine accepts can never disagree.

- C1  Duration-aware capacity: every appointment occupies
      [start, start + service.duration). A slot is bookable when the PEAK number
      of overlapping appointments inside its window is below
      max_parallel_bookings, and the service finishes by closing time.
- C2  Atomic booking: the capacity check and the insert run under a write lock
      (SQLite: BEGIN IMMEDIATE, Postgres: per-business advisory lock), so two
      patients can never both win the last seat.
- W6  Every booking is revalidated: tenant active, service bookable, not in the
      past, not a holiday, inside opening hours, capacity free.
- All "now" / "today" checks use the BUSINESS time zone, never the server's.

Errors are raised as BookingEngineException with an `error_code`.
"""
import logging
from datetime import date, datetime, time as dt_time, timedelta

import pytz
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import Appointment, Business, Service

logger = logging.getLogger("VERZIO_ENGINE")

# Appointment statuses that hold a seat on the calendar.
OCCUPYING_STATUSES = ("pending", "confirmed")

ERR_NOT_FOUND = "ERR_NOT_FOUND"
ERR_TENANT_LOCKED = "ERR_TENANT_LOCKED"
ERR_INVALID_SERVICE = "ERR_INVALID_SERVICE"
ERR_HOLIDAY = "ERR_HOLIDAY"
ERR_OUTSIDE_HOURS = "ERR_OUTSIDE_HOURS"
ERR_PAST_TIME = "ERR_PAST_TIME"
ERR_CAPACITY = "ERR_CAPACITY"
ERR_BUSY = "ERR_BUSY"


class BookingEngineException(Exception):
    def __init__(self, message: str, error_code: str):
        super().__init__(message)
        self.error_code = error_code

    @property
    def code(self) -> str:  # alias kept for older callers
        return self.error_code


# ------------------------------------------------------------------------
# Time helpers
# ------------------------------------------------------------------------

def business_tz(biz: Business):
    try:
        return pytz.timezone(biz.timezone or "UTC")
    except pytz.UnknownTimeZoneError:
        logger.warning("Unknown timezone %r for business_id=%s; using UTC", biz.timezone, biz.id)
        return pytz.UTC


def business_now(biz: Business) -> datetime:
    """Current wall-clock time at the business, as a naive datetime
    (appointment_time is stored as naive business-local time)."""
    return datetime.now(business_tz(biz)).replace(tzinfo=None, second=0, microsecond=0)


def business_today(biz: Business) -> date:
    return business_now(biz).date()


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


class VerzioSaaSEngine:
    # ------------------------------------------------------------------
    # Tenant & service lookups
    # ------------------------------------------------------------------

    def get_tenant_config(self, db: Session, tenant_id: str) -> Business:
        """Resolve a business by its WhatsApp phone_number_id. Raises if the
        business is unknown, deactivated, or has paused bookings."""
        biz = (
            db.query(Business)
            .filter(Business.whatsapp_business_phone_number_id == tenant_id)
            .first()
        )
        if not biz:
            raise BookingEngineException("Business not found.", ERR_NOT_FOUND)
        if not biz.is_active or not biz.accepting_bookings:
            raise BookingEngineException("Business is not currently accepting bookings.", ERR_TENANT_LOCKED)
        return biz

    def get_service_for_business(self, db: Session, business_id: int, service_id) -> Service | None:
        """Return the service only if it belongs to this business and is bookable."""
        if not isinstance(service_id, int) or isinstance(service_id, bool):
            return None
        return (
            db.query(Service)
            .filter(
                Service.id == service_id,
                Service.business_id == business_id,
                Service.is_active == True,
                Service.is_deleted == False,
            )
            .first()
        )

    # ------------------------------------------------------------------
    # Calendar rules
    # ------------------------------------------------------------------

    @staticmethod
    def interval_minutes(biz: Business) -> int:
        return max(int(biz.slot_interval or 30), 5)

    def service_minutes(self, biz: Business, service: Service | None) -> int:
        if service is not None and service.duration and int(service.duration) > 0:
            return int(service.duration)
        return self.interval_minutes(biz)

    @staticmethod
    def is_holiday(biz: Business, day) -> bool:
        return _as_date(day).isoformat() in (biz.holidays or [])

    @staticmethod
    def day_window(biz: Business, day) -> tuple[datetime, datetime] | None:
        """(open, close) as naive local datetimes, or None if closed that day."""
        day = _as_date(day)
        hours = (biz.operational_hours or {}).get(day.strftime("%a").lower()[:3])
        if not hours or len(hours) < 2:
            return None
        try:
            open_t = datetime.strptime(hours[0], "%H:%M").time()
            close_t = datetime.strptime(hours[1], "%H:%M").time()
        except (TypeError, ValueError):
            logger.warning("Bad operational_hours %r for business_id=%s", hours, biz.id)
            return None
        if close_t <= open_t:
            return None  # overnight hours are not supported
        return datetime.combine(day, open_t), datetime.combine(day, close_t)

    def is_open_day(self, biz: Business, day) -> bool:
        return not self.is_holiday(biz, day) and self.day_window(biz, day) is not None

    def get_available_dates(self, db: Session, biz: Business, limit: int = 10) -> list[date]:
        """Open, non-holiday dates within the advance booking window."""
        today = business_today(biz)
        window_days = max(int(biz.advance_booking_days or 14), 1)
        dates = []
        for offset in range(window_days):
            day = today + timedelta(days=offset)
            if self.is_open_day(biz, day):
                dates.append(day)
            if len(dates) >= limit:
                break
        return dates

    def _day_occupancy(self, db: Session, biz: Business, day) -> list[tuple[datetime, datetime]]:
        """All seat-holding appointments for this business on this day, as (start, end)."""
        day = _as_date(day)
        rows = (
            db.query(Appointment.appointment_time, Service.duration)
            .outerjoin(Service, Appointment.service_id == Service.id)
            .filter(
                Appointment.business_id == biz.id,
                Appointment.status.in_(OCCUPYING_STATUSES),
                Appointment.appointment_time >= datetime.combine(day, dt_time.min),
                Appointment.appointment_time <= datetime.combine(day, dt_time.max),
            )
            .all()
        )
        fallback = self.interval_minutes(biz)
        intervals = []
        for start, duration in rows:
            minutes = int(duration) if duration and int(duration) > 0 else fallback
            intervals.append((start, start + timedelta(minutes=minutes)))
        return intervals

    @staticmethod
    def _peak_concurrency(intervals, start: datetime, end: datetime) -> int:
        """Max number of intervals active at any instant inside [start, end).
        Concurrency only rises at an interval's start, so checking the window
        start plus every interval start inside the window is sufficient."""
        overlapping = [(s, e) for s, e in intervals if s < end and start < e]
        if not overlapping:
            return 0
        checkpoints = {start} | {s for s, _ in overlapping if start <= s < end}
        return max(sum(1 for s, e in overlapping if s <= p < e) for p in checkpoints)

    def _fits(self, biz: Business, intervals, start: datetime, end: datetime) -> bool:
        capacity = max(int(biz.max_parallel_bookings or 1), 1)
        return self._peak_concurrency(intervals, start, end) < capacity

    def get_available_slots(
        self,
        db: Session,
        biz: Business,
        target_date,
        service: Service | None = None,
    ) -> list[str]:
        """Start times ("HH:MM") where `service` fits, in business-local time."""
        day = _as_date(target_date)
        if self.is_holiday(biz, day):
            return []
        window = self.day_window(biz, day)
        if window is None:
            return []
        open_dt, close_dt = window

        now_local = business_now(biz)
        step = timedelta(minutes=self.interval_minutes(biz))
        length = timedelta(minutes=self.service_minutes(biz, service))
        intervals = self._day_occupancy(db, biz, day)

        slots = []
        curr = open_dt
        while curr + length <= close_dt:          # the service must FINISH by closing
            if curr > now_local and self._fits(biz, intervals, curr, curr + length):
                slots.append(curr.strftime("%H:%M"))
            curr += step
        return slots

    # ------------------------------------------------------------------
    # Atomic booking
    # ------------------------------------------------------------------

    @staticmethod
    def _acquire_write_lock(db: Session, business_id: int) -> None:
        """Serialise check-then-insert for this business.
        SQLite: BEGIN IMMEDIATE takes the database write lock up front, so a
        second booking waits until the first has committed and then sees it.
        Postgres: a transaction-scoped advisory lock per business."""
        dialect = db.get_bind().dialect.name
        if db.in_transaction():
            db.commit()  # start from a clean transaction so the lock covers the whole check
        if dialect == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        elif dialect == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": business_id})

    def validate_and_book(
        self,
        db: Session,
        tenant_id: str,
        target_dt: datetime,
        customer_phone: str,
        service_id: int,
        customer_name: str | None = None,
    ) -> Appointment:
        """Revalidate everything and insert the appointment atomically.
        Returns the committed Appointment or raises BookingEngineException."""
        biz = self.get_tenant_config(db, tenant_id)
        business_id = biz.id

        try:
            self._acquire_write_lock(db, business_id)
        except Exception as exc:
            db.rollback()
            logger.error("Could not lock calendar for business_id=%s: %s", business_id, exc)
            raise BookingEngineException("The calendar is busy, please try again.", ERR_BUSY) from exc

        try:
            # Re-read inside the lock: settings may have changed since the patient started.
            biz = db.get(Business, business_id)
            if not biz or not biz.is_active or not biz.accepting_bookings:
                raise BookingEngineException("Business is not currently accepting bookings.", ERR_TENANT_LOCKED)

            service = self.get_service_for_business(db, biz.id, service_id)
            if service is None:
                logger.warning("Rejected booking: service_id=%s not bookable for business_id=%s", service_id, biz.id)
                raise BookingEngineException("Selected service is not available for this business.", ERR_INVALID_SERVICE)

            start = target_dt.replace(second=0, microsecond=0)
            end = start + timedelta(minutes=self.service_minutes(biz, service))

            if start <= business_now(biz):
                raise BookingEngineException("This appointment time is in the past.", ERR_PAST_TIME)

            if self.is_holiday(biz, start):
                raise BookingEngineException("Business is closed on this date.", ERR_HOLIDAY)

            window = self.day_window(biz, start)
            if window is None or start < window[0] or end > window[1]:
                raise BookingEngineException("Selected time is outside business hours.", ERR_OUTSIDE_HOURS)

            if not self._fits(biz, self._day_occupancy(db, biz, start), start, end):
                raise BookingEngineException("This slot just reached full capacity.", ERR_CAPACITY)

            status = "confirmed" if biz.approval_mode == "automatic" else "pending"
            appt = Appointment(
                business_id=biz.id,
                service_id=service.id,
                customer_phone=customer_phone,
                customer_name=(customer_name or "").strip()[:120] or None,
                appointment_time=start,
                status=status,
            )
            db.add(appt)
            db.commit()
        except BookingEngineException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise

        db.refresh(appt)
        logger.info("Booked appointment #%s for business_id=%s (%s)", appt.id, appt.business_id, appt.status)
        return appt


# ------------------------------------------------------------------------
# Module-level helpers (kept for scripts and tests)
# ------------------------------------------------------------------------

engine = VerzioSaaSEngine()


def get_available_slots_for_day(db, business, target_date, service=None):
    return engine.get_available_slots(db, business, target_date, service)


def validate_and_book(db, tenant_id, target_dt, customer_phone, service_id, customer_name=None):
    return engine.validate_and_book(db, tenant_id, target_dt, customer_phone, service_id, customer_name)
