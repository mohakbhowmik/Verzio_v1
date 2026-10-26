"""
tables/engine.py: the Verzio Tables rules. No WhatsApp or HTML here.

Capacity
- A zone is counted either in tables ("tables": N tables of S seats; a group
  takes ceil(party / S) tables, joined together) or in people ("people": a
  standing area or bar counter that holds N guests).
- A booking holds its space from start_at to end_at (seating_minutes later).
- A confirmed booking whose guests haven't arrived release_minutes after the
  start stops counting from then on: the table goes back on sale.
- Space is checked at the busiest moment of the new booking's window, so a
  9:30 pm table is never sold twice because an 8 pm group is still seated.

Bookings are created under one lock (the app runs one worker), so two guests
can never take the last table at the same moment.
"""
import logging
import math
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from database import Business, Owner
from tables.models import (TablesBooking, TablesLink, TablesOffer, TablesOptOut, TablesSettings,
                           TablesWaitlist, TablesZone)
from whatsapp_client import canonical_phone, is_real_phone, normalize_phone

logger = logging.getLogger("VERZIO_TABLES")

GRID_COLS, GRID_ROWS = 12, 8
LINK_HOURS = 6                     # a booking link works this long (well inside WhatsApp's 24 h window)
ACTIVE = ("pending", "confirmed", "arrived")
STATUS_LABELS = {"pending": "Waiting for approval", "confirmed": "Confirmed", "arrived": "Arrived",
                 "no_show": "No-show", "cancelled": "Cancelled", "declined": "Declined"}
DEMO_ZONES = [
    # name, note, kind, tables, seats, people, x, y, w, h
    ("Skyline edge", "Open air, city view", "tables", 6, 4, 0, 0, 0, 12, 2),
    ("Lounge", "Sofas and low tables", "tables", 5, 6, 0, 0, 2, 5, 4),
    ("Bar counter", "High stools, walk up", "people", 0, 0, 14, 5, 2, 4, 3),
    ("Private cabana", "One cabana, up to 12", "tables", 1, 12, 0, 9, 2, 3, 4),
]

_book_lock = threading.Lock()


class TablesError(Exception):
    """A rule was broken. `message` is safe to show the guest or the owner."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------------------
# Settings and plan
# ------------------------------------------------------------------------

def get_settings(db: Session, business_id: int) -> TablesSettings | None:
    return db.get(TablesSettings, business_id)


def is_enabled(db: Session, biz: Business) -> bool:
    row = get_settings(db, biz.id)
    return bool(row and row.enabled)


def ensure_settings(db: Session, biz: Business) -> TablesSettings:
    row = get_settings(db, biz.id)
    if row is None:
        row = TablesSettings(business_id=biz.id, enabled=False)
        db.add(row)
        db.commit()
    return row


GRACE_DAYS = 3
PLAN_LABELS = {"trial": "Trial", "active": "Active", "suspended": "Suspended"}


@dataclass
class TablesPlan:
    key: str
    label: str
    active: bool
    paid_until: date | None = None
    in_grace: bool = False
    reason: str = ""
    amount: int = 0


def plan_info(settings: TablesSettings | None, today: date | None = None) -> TablesPlan:
    """Trial has no end date; Active runs to paid_until plus GRACE_DAYS; Suspended is off at once."""
    if settings is None:
        return TablesPlan("trial", "Trial", False)
    key = settings.plan if settings.plan in PLAN_LABELS else "trial"
    info = TablesPlan(key, PLAN_LABELS[key], key != "suspended", amount=settings.monthly_amount or 0)
    if key == "suspended":
        info.reason = "Your Verzio Tables plan is suspended. Contact Verzio Solutions to switch bookings back on."
        return info
    try:
        info.paid_until = date.fromisoformat(settings.paid_until) if settings.paid_until else None
    except ValueError:
        info.paid_until = None
    if key == "active" and info.paid_until:
        today = today or date.today()
        last_day = info.paid_until + timedelta(days=GRACE_DAYS)
        if today > last_day:
            info.active = False
            info.reason = (f"Your Verzio Tables plan ended on {info.paid_until:%d %b %Y}. "
                           f"WhatsApp bookings are paused until it's renewed.")
        elif today > info.paid_until:
            info.in_grace = True
            info.reason = (f"Your Verzio Tables plan ended on {info.paid_until:%d %b %Y}. "
                           f"WhatsApp bookings stop after {last_day:%d %b} unless it's renewed.")
    return info


def running(db: Session, biz: Business) -> bool:
    """Guests can book only while the business is active, Tables is on and its plan is."""
    settings = get_settings(db, biz.id)
    return bool(biz.is_active and settings and settings.enabled and plan_info(settings).active)


# ------------------------------------------------------------------------
# Time
# ------------------------------------------------------------------------

def business_now(biz: Business) -> datetime:
    from booking_engine import business_now as _now
    return _now(biz)


_DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _hhmm(value) -> dtime | None:
    try:
        return datetime.strptime((value or "").strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        return None


def opening_window(biz: Business, day: date) -> tuple[datetime, datetime] | None:
    """(open, close) for a business day. A close at or before the opening time
    means after midnight (a bar open 18:00-01:00 closes at 1 am the next day)."""
    if day.isoformat() in (biz.holidays or []):
        return None
    entry = (biz.operational_hours or {}).get(_DAY_KEYS[day.weekday()]) or []
    if len(entry) < 2:
        return None
    o, c = _hhmm(entry[0]), _hhmm(entry[1])
    if o is None or c is None:
        return None
    start = datetime.combine(day, o)
    end = datetime.combine(day, c)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def arrival_times(biz: Business, settings: TablesSettings, day: date, now: datetime | None = None) -> list[datetime]:
    """Arrival times guests can pick on `day`, after the minimum notice."""
    window = opening_window(biz, day)
    if window is None:
        return []
    now = now or business_now(biz)
    step = max(15, settings.slot_minutes or 30)
    last = window[1] - timedelta(minutes=max(0, settings.last_arrival_minutes or 0))
    earliest = now + timedelta(minutes=max(0, settings.min_notice_minutes or 0))
    out, t = [], window[0]
    while t <= last:
        if t >= earliest:
            out.append(t)
        t += timedelta(minutes=step)
    return out


def bookable_days(biz: Business, settings: TablesSettings, now: datetime | None = None) -> list[date]:
    now = now or business_now(biz)
    days = []
    # A bar open until 1 am still takes "tonight" bookings at 12:30 am: start from yesterday.
    for offset in range(-1, max(1, settings.book_ahead_days or 14) + 1):
        d = now.date() + timedelta(days=offset)
        if arrival_times(biz, settings, d, now):
            days.append(d)
    return days


def day_label(d: date, today: date) -> str:
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%a %d %b")


def when_label(dt: datetime) -> str:
    """'Fri 31 Oct, 8:30 PM'"""
    return f"{dt.strftime('%a %d %b')}, {dt.strftime('%I:%M %p').lstrip('0')}"


def inr(n) -> str:
    """Indian digit grouping: 518200 -> '5,18,200'."""
    digits = str(int(n or 0))
    neg, digits = digits.startswith("-"), digits.lstrip("-")
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        head = ",".join(re.findall(r"\d{1,2}", head[::-1]))[::-1]
        digits = f"{head},{tail}"
    return ("-" if neg else "") + digits


def clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


# ------------------------------------------------------------------------
# Zones and capacity
# ------------------------------------------------------------------------

def zones(db: Session, business_id: int, include_inactive: bool = True) -> list[TablesZone]:
    q = db.query(TablesZone).filter(TablesZone.business_id == business_id, TablesZone.is_deleted == False)  # noqa: E712
    if not include_inactive:
        q = q.filter(TablesZone.is_active == True)  # noqa: E712
    return q.order_by(TablesZone.y.asc(), TablesZone.x.asc(), TablesZone.id.asc()).all()


def capacity(zone: TablesZone) -> int:
    """Tables in a "tables" zone, guests in a "people" zone."""
    return max(0, zone.table_count or 0) if zone.kind == "tables" else max(0, zone.people_capacity or 0)


def seats(zone: TablesZone) -> int:
    return (zone.table_count or 0) * (zone.seats_per_table or 0) if zone.kind == "tables" else (zone.people_capacity or 0)


def need(zone: TablesZone, party: int) -> int:
    """How much of the zone's capacity a group uses."""
    if zone.kind == "tables":
        return math.ceil(party / max(1, zone.seats_per_table or 1))
    return party


def _counts(b: TablesBooking, now: datetime, release_minutes: int) -> bool:
    """Is this booking still holding its space? Late guests lose the table
    release_minutes after their time, unless they've been marked arrived."""
    if b.status not in ACTIVE:
        return False
    if b.status == "arrived":
        return True
    return now < b.start_at + timedelta(minutes=release_minutes)


def peak_load(db: Session, zone: TablesZone, start: datetime, end: datetime, now: datetime,
              release_minutes: int, exclude_id: int | None = None) -> int:
    """The most capacity in use at any moment between start and end."""
    q = db.query(TablesBooking).filter(TablesBooking.zone_id == zone.id,
                                       TablesBooking.status.in_(ACTIVE),
                                       TablesBooking.start_at < end, TablesBooking.end_at > start)
    rows = [b for b in q.all() if b.id != exclude_id]
    events = []
    for b in rows:
        if not _counts(b, now, release_minutes):
            # Guests are late past the grace time and weren't marked arrived: table is free again.
            continue
        use = b.tables_used if zone.kind == "tables" else b.party
        events.append((max(b.start_at, start), use))
        events.append((min(b.end_at, end), -use))
    events.sort(key=lambda e: (e[0], e[1]))     # frees before takes at the same instant
    load = peak = 0
    for _, delta in events:
        load += delta
        peak = max(peak, load)
    return peak


@dataclass
class ZoneState:
    zone: TablesZone
    state: str              # free | few | full | off | small (party below the zone minimum)
    left: int = 0           # tables or guests still free at the busiest moment
    note: str = ""


def zone_state(db: Session, settings: TablesSettings, zone: TablesZone, start: datetime, party: int,
               now: datetime, exclude_id: int | None = None) -> ZoneState:
    if not zone.is_active:
        return ZoneState(zone, "off", note="Closed today")
    cap = capacity(zone)
    want = need(zone, party)
    if party < (zone.min_party or 1):
        return ZoneState(zone, "small", note=f"For {zone.min_party}+ guests")
    if cap <= 0 or want > cap:
        return ZoneState(zone, "full", note=f"Fits up to {seats(zone)}")
    end = start + timedelta(minutes=settings.seating_minutes or 150)
    used = peak_load(db, zone, start, end, now, settings.release_minutes or 20, exclude_id)
    left = cap - used
    if want > left:
        return ZoneState(zone, "full", left=max(0, left), note="Full at this time")
    after = left - want
    if after <= max(0, math.floor(cap * 0.25)):
        return ZoneState(zone, "few", left=left, note="Almost full")
    return ZoneState(zone, "free", left=left, note="Available")


def availability(db: Session, biz: Business, settings: TablesSettings, start: datetime, party: int,
                 now: datetime | None = None) -> list[ZoneState]:
    now = now or business_now(biz)
    return [zone_state(db, settings, z, start, party, now) for z in zones(db, biz.id)]


def check_time(biz: Business, settings: TablesSettings, start: datetime, now: datetime | None = None) -> None:
    now = now or business_now(biz)
    last_day = now.date() + timedelta(days=max(1, settings.book_ahead_days or 14))
    for d in (start.date(), start.date() - timedelta(days=1)):     # after midnight belongs to the night before
        if now.date() - timedelta(days=1) <= d <= last_day and start in arrival_times(biz, settings, d, now):
            return
    raise TablesError("That time isn't available any more. Please pick another time.")


def clean_party(raw, settings: TablesSettings) -> int:
    try:
        party = int(str(raw).strip())
    except (TypeError, ValueError):
        raise TablesError("Please choose how many people are coming.")
    if party < 1:
        raise TablesError("Please choose how many people are coming.")
    if party > (settings.max_party or 20):
        raise TablesError(f"For more than {settings.max_party} people, please message us and we'll arrange it.")
    return party


# ------------------------------------------------------------------------
# Bookings
# ------------------------------------------------------------------------

def _clean_name(raw: str | None) -> str | None:
    name = " ".join((raw or "").split())[:60]
    return name or None


def needs_approval(biz: Business, settings: TablesSettings, party: int) -> bool:
    from booking_engine import owner_cannot_approve
    limit = settings.approval_party or 0
    return bool(limit and party >= limit and not owner_cannot_approve(biz))


def create_booking(db: Session, biz: Business, settings: TablesSettings, zone_id: int, start: datetime,
                   party: int, phone: str | None, name: str | None, source: str = "whatsapp",
                   now: datetime | None = None, check_rules: bool = True) -> TablesBooking:
    """Book a zone. Raises TablesError if it doesn't fit. Walk-ins (source
    "walkin") skip the time rules and are marked arrived straight away."""
    now = now or business_now(biz)
    zone = db.get(TablesZone, zone_id)
    if zone is None or zone.business_id != biz.id or zone.is_deleted:
        raise TablesError("Please pick an area on the map.")
    digits = canonical_phone(phone, biz.timezone) if phone else ""
    if phone and not is_real_phone(digits):
        raise TablesError("That phone number doesn't look right.")
    with _book_lock:
        if check_rules:
            check_time(biz, settings, start, now)
            if digits:
                clash = (db.query(TablesBooking)
                         .filter(TablesBooking.business_id == biz.id, TablesBooking.phone == digits,
                                 TablesBooking.status.in_(("pending", "confirmed")),
                                 TablesBooking.start_at < start + timedelta(minutes=settings.seating_minutes),
                                 TablesBooking.end_at > start)
                         .first())
                if clash:
                    raise TablesError(f"You already have a table booked for {when_label(clash.start_at)}. "
                                      f"Reply on WhatsApp if you'd like to change it.")
        state = zone_state(db, settings, zone, start, party, now)
        if source != "walkin" and state.state != "free" and state.state != "few":
            raise TablesError(f"Sorry, {zone.name} is full for {party} at {clock(start)}. "
                              f"Try another area or time, or join the waitlist.")
        status = "arrived" if source == "walkin" else (
            "pending" if source == "whatsapp" and needs_approval(biz, settings, party) else "confirmed")
        booking = TablesBooking(
            business_id=biz.id, zone_id=zone.id, phone=digits or None, name=_clean_name(name), party=party,
            tables_used=need(zone, party) if zone.kind == "tables" else 0,
            start_at=start, end_at=start + timedelta(minutes=settings.seating_minutes or 150),
            status=status, source=source, arrived_at=datetime.utcnow() if status == "arrived" else None)
        db.add(booking)
        db.commit()
    return booking


# Allowed status changes. Undo is allowed for door-staff mistakes.
TRANSITIONS = {
    "approve": ({"pending"}, "confirmed"),
    "decline": ({"pending"}, "declined"),
    "cancel": ({"pending", "confirmed"}, "cancelled"),
    "arrived": ({"confirmed", "no_show", "pending"}, "arrived"),
    "no_show": ({"confirmed"}, "no_show"),
    "undo": ({"arrived", "no_show"}, "confirmed"),
}


def change_status(db: Session, biz: Business, booking_id: int, action: str) -> tuple[TablesBooking, str]:
    """(booking, previous status). Raises TablesError for an invalid change."""
    booking = own_booking(db, biz, booking_id)
    if booking is None:
        raise TablesError("Booking not found.")
    if action not in TRANSITIONS:
        raise TablesError("Unknown action.")
    allowed, new = TRANSITIONS[action]
    if booking.status not in allowed:
        raise TablesError(f"This booking is already {STATUS_LABELS.get(booking.status, booking.status).lower()}.")
    previous = booking.status
    booking.status = new
    if new == "arrived":
        booking.arrived_at = datetime.utcnow()
    elif action == "undo":
        booking.arrived_at = None
    db.commit()
    return booking, previous


def own_booking(db: Session, biz: Business, booking_id: int) -> TablesBooking | None:
    b = db.get(TablesBooking, booking_id)
    return b if b is not None and b.business_id == biz.id else None


def set_bill(db: Session, biz: Business, booking_id: int, raw: str) -> TablesBooking:
    booking = own_booking(db, biz, booking_id)
    if booking is None:
        raise TablesError("Booking not found.")
    digits = re.sub(r"[^\d]", "", raw or "")
    booking.bill_amount = min(int(digits), 10_000_000) if digits else None
    db.commit()
    return booking


def bookings_for_day(db: Session, biz: Business, day: date) -> list[TablesBooking]:
    """Every booking that starts on `day`'s service (until 6 am the next morning)."""
    start = datetime.combine(day, dtime(6, 0))
    return (db.query(TablesBooking)
            .filter(TablesBooking.business_id == biz.id,
                    TablesBooking.start_at >= start, TablesBooking.start_at < start + timedelta(days=1))
            .order_by(TablesBooking.start_at.asc(), TablesBooking.id.asc()).all())


def upcoming_for_phone(db: Session, biz: Business, phone: str, now: datetime) -> list[TablesBooking]:
    return (db.query(TablesBooking)
            .filter(TablesBooking.business_id == biz.id, TablesBooking.phone == normalize_phone(phone),
                    TablesBooking.status.in_(("pending", "confirmed")), TablesBooking.start_at > now)
            .order_by(TablesBooking.start_at.asc()).limit(5).all())


def service_day(dt: datetime) -> date:
    """The evening a time belongs to: 1 am Saturday is Friday night."""
    return (dt - timedelta(hours=6)).date()


# ------------------------------------------------------------------------
# Booking links (/t/<token>)
# ------------------------------------------------------------------------

def new_link(db: Session, biz: Business, phone: str, name: str | None = None, party: int | None = None,
             start: datetime | None = None) -> TablesLink:
    link = TablesLink(token=secrets.token_urlsafe(16), business_id=biz.id, phone=normalize_phone(phone),
                      name=_clean_name(name), party=party,
                      day=service_day(start).isoformat() if start else None,
                      time=start.strftime("%H:%M") if start else None,
                      expires_at=datetime.utcnow() + timedelta(hours=LINK_HOURS))
    db.add(link)
    db.commit()
    return link


def find_link(db: Session, token: str) -> TablesLink | None:
    if not token or len(token) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return None
    return db.get(TablesLink, token)


def link_problem(link: TablesLink | None) -> str | None:
    if link is None:
        return "missing"
    if link.used_at is not None:
        return "used"
    if datetime.utcnow() >= link.expires_at:
        return "expired"
    return None


# ------------------------------------------------------------------------
# Waitlist
# ------------------------------------------------------------------------

def join_waitlist(db: Session, biz: Business, phone: str, name: str | None, party: int, start: datetime,
                  zone_id: int | None) -> TablesWaitlist:
    digits = normalize_phone(phone)
    existing = (db.query(TablesWaitlist)
                .filter(TablesWaitlist.business_id == biz.id, TablesWaitlist.phone == digits,
                        TablesWaitlist.start_at == start, TablesWaitlist.status == "waiting").first())
    if existing:
        existing.party, existing.zone_id = party, zone_id
        db.commit()
        return existing
    row = TablesWaitlist(business_id=biz.id, phone=digits, name=_clean_name(name), party=party,
                         start_at=start, zone_id=zone_id)
    db.add(row)
    db.commit()
    return row


def waitlist_for_day(db: Session, biz: Business, day: date) -> list[TablesWaitlist]:
    start = datetime.combine(day, dtime(6, 0))
    return (db.query(TablesWaitlist)
            .filter(TablesWaitlist.business_id == biz.id, TablesWaitlist.status.in_(("waiting", "offered")),
                    TablesWaitlist.start_at >= start, TablesWaitlist.start_at < start + timedelta(days=1))
            .order_by(TablesWaitlist.start_at.asc(), TablesWaitlist.created_at.asc()).all())


def first_fitting_zone(db: Session, biz: Business, settings: TablesSettings, entry: TablesWaitlist,
                       now: datetime) -> TablesZone | None:
    for z in zones(db, biz.id, include_inactive=False):
        if entry.zone_id and z.id != entry.zone_id:
            continue
        if zone_state(db, settings, z, entry.start_at, entry.party, now).state in ("free", "few"):
            return z
    return None


# ------------------------------------------------------------------------
# Book-again offers (marketing messages): opt-outs and the monthly cap
# ------------------------------------------------------------------------

def opted_out(db: Session, business_id: int, phone: str) -> bool:
    return db.query(TablesOptOut).filter(TablesOptOut.business_id == business_id,
                                         TablesOptOut.phone == normalize_phone(phone)).first() is not None


def opt_out(db: Session, business_id: int, phone: str) -> None:
    if not opted_out(db, business_id, phone):
        db.add(TablesOptOut(business_id=business_id, phone=normalize_phone(phone)))
        db.commit()


def opt_in(db: Session, business_id: int, phone: str) -> None:
    db.query(TablesOptOut).filter(TablesOptOut.business_id == business_id,
                                  TablesOptOut.phone == normalize_phone(phone)).delete()
    db.commit()


def offers_this_month(db: Session, business_id: int, month: str | None = None) -> int:
    month = month or datetime.utcnow().strftime("%Y-%m")
    return db.query(func.count(TablesOffer.id)).filter(TablesOffer.business_id == business_id,
                                                       TablesOffer.month == month).scalar() or 0


# ------------------------------------------------------------------------
# Results: what the owner is paying for
# ------------------------------------------------------------------------

@dataclass
class Results:
    month_label: str
    bookings: int = 0              # WhatsApp bookings made for this month (not cancelled/declined)
    seated: int = 0                # of those, guests marked arrived
    guests: int = 0
    no_shows: int = 0
    bills: int = 0                 # ₹ (typed bills, or the average bill)
    typed_bills: int = 0           # how many bills were typed at the door
    saved: int = 0                 # ₹ commission not paid to a platform
    cancelled_early: int = 0       # cancelled from the reminder: table resold
    walkins: int = 0
    loved: int = 0
    reviews_asked: int = 0
    offers_sent: int = 0
    offers_booked: int = 0         # bookings made within 14 days of a book-again message
    offer_cap: int = 0
    daily: list = field(default_factory=list)   # (day label, seated) for a small chart


def results(db: Session, biz: Business, settings: TablesSettings, today: date | None = None) -> Results:
    today = today or business_now(biz).date()
    first = today.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    start, end = datetime.combine(first, dtime(6, 0)), datetime.combine(nxt, dtime(6, 0))
    rows = (db.query(TablesBooking)
            .filter(TablesBooking.business_id == biz.id, TablesBooking.start_at >= start,
                    TablesBooking.start_at < end).all())
    r = Results(month_label=first.strftime("%B %Y"), offer_cap=settings.offers_per_month or 0)
    per_day: dict[date, int] = {}
    for b in rows:
        if b.source == "walkin":
            r.walkins += 1
            continue
        if b.status in ("cancelled", "declined"):
            if b.guest_reply == "cancelled":
                r.cancelled_early += 1
            continue
        r.bookings += 1
        if b.status == "no_show":
            r.no_shows += 1
        if b.status == "arrived":
            r.seated += 1
            r.guests += b.party
            bill = b.bill_amount if b.bill_amount else (settings.avg_bill or 0)
            r.typed_bills += 1 if b.bill_amount else 0
            r.bills += bill
            d = service_day(b.start_at)
            per_day[d] = per_day.get(d, 0) + 1
        if b.rating:
            r.reviews_asked += 1
            r.loved += 1 if b.rating == "loved" else 0
    r.saved = round(r.bills * (settings.commission_pct or 0) / 100)
    month = first.strftime("%Y-%m")
    offers = (db.query(TablesOffer).filter(TablesOffer.business_id == biz.id, TablesOffer.month == month).all())
    r.offers_sent = len(offers)
    for o in offers:
        hit = (db.query(TablesBooking.id)
               .filter(TablesBooking.business_id == biz.id, TablesBooking.phone == o.phone,
                       TablesBooking.source == "whatsapp",
                       TablesBooking.created_at > o.sent_at,
                       TablesBooking.created_at < o.sent_at + timedelta(days=14)).first())
        r.offers_booked += 1 if hit else 0
    d = first
    while d <= min(today, nxt - timedelta(days=1)):
        r.daily.append((d.strftime("%d"), per_day.get(d, 0)))
        d += timedelta(days=1)
    return r


@dataclass
class Guest:
    phone: str
    name: str | None
    visits: int
    bookings: int
    no_shows: int
    last_visit: datetime | None
    opted_out: bool


def guests(db: Session, biz: Business, q: str = "", limit: int = 300) -> list[Guest]:
    rows = (db.query(TablesBooking)
            .filter(TablesBooking.business_id == biz.id, TablesBooking.phone.isnot(None))
            .order_by(TablesBooking.start_at.desc()).limit(5000).all())
    out: dict[str, Guest] = {}
    for b in rows:
        g = out.get(b.phone)
        if g is None:
            g = out[b.phone] = Guest(b.phone, b.name, 0, 0, 0, None, False)
        g.name = g.name or b.name
        if b.status in ("cancelled", "declined"):
            continue
        g.bookings += 1
        if b.status == "arrived":
            g.visits += 1
            g.last_visit = max(g.last_visit, b.start_at) if g.last_visit else b.start_at
        elif b.status == "no_show":
            g.no_shows += 1
    opted = {o.phone for o in db.query(TablesOptOut).filter(TablesOptOut.business_id == biz.id).all()}
    result = list(out.values())
    for g in result:
        g.opted_out = g.phone in opted
    if q:
        ql, digits = q.lower(), "".join(ch for ch in q if ch.isdigit())
        result = [g for g in result if ql in (g.name or "").lower() or (digits and digits in g.phone)]
    result.sort(key=lambda g: (g.visits, g.last_visit or datetime.min), reverse=True)
    return result[:limit]


# ------------------------------------------------------------------------
# Zones: setup helpers
# ------------------------------------------------------------------------

def clean_zone_fields(form: dict) -> dict:
    """Validated zone fields from the floor plan form."""
    def num(key, lo, hi, default):
        try:
            v = int(str(form.get(key, default)).strip())
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))
    name = " ".join((form.get("name") or "").split())[:40]
    if not name:
        raise TablesError("Please give the area a name, e.g. Rooftop or Bar counter.")
    kind = "people" if form.get("kind") == "people" else "tables"
    fields = {
        "name": name, "note": " ".join((form.get("note") or "").split())[:60] or None, "kind": kind,
        "table_count": num("table_count", 0, 200, 4), "seats_per_table": num("seats_per_table", 1, 40, 4),
        "people_capacity": num("people_capacity", 0, 2000, 20), "min_party": num("min_party", 1, 40, 1),
        "w": num("w", 1, GRID_COLS, 4), "h": num("h", 1, GRID_ROWS, 3),
    }
    fields["x"] = num("x", 0, GRID_COLS - fields["w"], 0)
    fields["y"] = num("y", 0, GRID_ROWS - fields["h"], 0)
    if kind == "tables" and fields["table_count"] < 1:
        raise TablesError("A tables area needs at least 1 table.")
    if kind == "people" and fields["people_capacity"] < 1:
        raise TablesError("Please say how many guests the area holds.")
    return fields


def add_demo_zones(db: Session, biz: Business) -> int:
    if zones(db, biz.id):
        return 0
    for name, note, kind, tables, seats_, people, x, y, w, h in DEMO_ZONES:
        db.add(TablesZone(business_id=biz.id, name=name, note=note, kind=kind, table_count=tables or 1,
                          seats_per_table=seats_ or 4, people_capacity=people or 20, x=x, y=y, w=w, h=h,
                          min_party=6 if name == "Private cabana" else 1))
    db.commit()
    return len(DEMO_ZONES)


# ------------------------------------------------------------------------
# Door-staff logins (owners.role = 'staff')
# ------------------------------------------------------------------------

def owner_role(db: Session, owner_id: int | None) -> str:
    if not owner_id:
        return "owner"
    try:
        role = db.execute(text("SELECT role FROM owners WHERE id = :i"), {"i": owner_id}).scalar()
    except Exception:
        return "owner"
    return "staff" if role == "staff" else "owner"


def staff_logins(db: Session, biz: Business) -> list[Owner]:
    ids = [r[0] for r in db.execute(text("SELECT id FROM owners WHERE business_id = :b AND role = 'staff'"),
                                    {"b": biz.id}).fetchall()]
    return db.query(Owner).filter(Owner.id.in_(ids)).order_by(Owner.full_name.asc()).all() if ids else []


def create_staff(db: Session, biz: Business, name: str, username: str, password: str) -> Owner:
    from owner.owner_auth import hash_password
    name = " ".join((name or "").split())[:60]
    username = (username or "").strip().lower()
    if not name:
        raise TablesError("Please type the staff member's name.")
    if not re.fullmatch(r"[a-z0-9._@-]{4,60}", username):
        raise TablesError("Login name: 4 or more letters or numbers, no spaces (e.g. door.skyline).")
    if len(password or "") < 8:
        raise TablesError("The password needs at least 8 characters.")
    if db.query(Owner).filter(Owner.email == username).first():
        raise TablesError("That login name is taken. Try another.")
    staff = Owner(business_id=biz.id, full_name=name, email=username, phone_number=None,
                  password_hash=hash_password(password), is_active=True)
    db.add(staff)
    db.flush()
    db.execute(text("UPDATE owners SET role = 'staff' WHERE id = :i"), {"i": staff.id})
    db.commit()
    return staff


def remove_staff(db: Session, biz: Business, staff_id: int) -> Owner:
    staff = db.get(Owner, staff_id)
    if staff is None or staff.business_id != biz.id or owner_role(db, staff.id) != "staff":
        raise TablesError("Staff login not found.")
    db.delete(staff)
    db.commit()
    return staff
