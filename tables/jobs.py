"""
tables/jobs.py: Verzio Tables' timed jobs, run by scheduler.tick() every few minutes.

1. Expire       A big-group request nobody accepted is declined EXPIRE_MINUTES
                before the time, and the guest is told.
2. Reminder     On the day, REMINDER_HOURS before the time: "Still coming?"
                with Yes / Cancel. A cancel frees the table for someone else.
3. Waitlist     When a full zone frees up (a cancel, a no-show), the first guest
                waiting for that time gets "A table opened up" with a Book it button.
4. Review ask   The day after a visit (from REVIEW_HOUR): "How was it?"; happy
                guests get the venue's Google review link.
5. Book again   Guests whose last visit was rebook_weeks ago get one offer
                (a Marketing template, ~₹1 each), up to the monthly cap.
                Never to anyone who replied STOP.

Every send is claimed in scheduled_sends first, so nothing is sent twice.
"""
import logging
from datetime import datetime, timedelta

from activity_log import log_event
from tables import bot as B
from tables import engine as T
from tables.models import TablesBooking, TablesOffer, TablesWaitlist
from whatsapp_client import _first_name, build_template, is_real_phone, send_whatsapp

logger = logging.getLogger("VERZIO_TABLES_JOBS")

EXPIRE_MINUTES = 60
REMINDER_HOURS = 3
REMINDER_MIN_MINUTES = 40
REMINDER_SKIP_IF_BOOKED_WITHIN = timedelta(hours=3)
REVIEW_HOUR, REVIEW_LATEST_HOUR = 11, 20
OFFER_HOURS = (12, 19)               # book-again offers only between 12 and 7 pm
OFFER_GAP_DAYS = 60                  # at most one offer per guest in this many days
OFFERS_PER_TICK = 30
WAITLIST_MIN_LEAD = timedelta(minutes=30)


def _businesses(db):
    from scheduler import _live_businesses
    out = []
    for biz in _live_businesses(db):
        settings = T.get_settings(db, biz.id)
        if settings and settings.enabled and T.running(db, biz):
            out.append((biz, settings))
    return out


async def run(db) -> dict:
    from scheduler import claim
    result = {"expired": 0, "reminders": 0, "waitlist": 0, "reviews": 0, "offers": 0}
    for biz, settings in _businesses(db):
        now = T.business_now(biz)
        for job, key in ((_expire, "expired"), (_reminders, "reminders"), (_waitlist, "waitlist"),
                         (_reviews, "reviews"), (_offers, "offers")):
            try:
                result[key] += await job(db, biz, settings, now, claim)
            except Exception:
                db.rollback()
                logger.exception("Tables job %s failed for business_id=%s", key, biz.id)
    return result


async def _expire(db, biz, settings, now, claim) -> int:
    rows = (db.query(TablesBooking)
            .filter(TablesBooking.business_id == biz.id, TablesBooking.status == "pending",
                    TablesBooking.start_at <= now + timedelta(minutes=EXPIRE_MINUTES)).all())
    count = 0
    for b in rows:
        if not claim("tbl_expire", b.id):
            continue
        b.status = "declined"
        db.commit()
        count += 1
        log_event(db=db, event_type="tables_expired", status="warning",
                  message=f"Table request #{b.id} wasn't accepted in time and was declined", business_id=biz.id)
        if b.start_at > now:
            await B.tell_guest_status(db, biz, b)
    return count


async def _reminders(db, biz, settings, now, claim) -> int:
    if not settings.reminder_on or not (9 <= now.hour < 23):
        return 0
    rows = (db.query(TablesBooking)
            .filter(TablesBooking.business_id == biz.id, TablesBooking.status == "confirmed",
                    TablesBooking.start_at > now + timedelta(minutes=REMINDER_MIN_MINUTES),
                    TablesBooking.start_at <= now + timedelta(hours=REMINDER_HOURS)).all())
    sent = 0
    for b in rows:
        if not is_real_phone(b.phone) or b.guest_reply == "coming" or b.source != "whatsapp":
            continue
        if b.created_at and datetime.utcnow() - b.created_at < REMINDER_SKIP_IF_BOOKED_WITHIN:
            continue                       # booked a moment ago: no reminder needed
        if not claim("tbl_reminder", b.id):
            continue
        first = _first_name(b.name)
        payload = {"type": "buttons",
                   "body": f"Hi {first}, see you soon at {biz.name}! 🥂\n\n{B._details(b)}\n\nStill coming?",
                   "buttons": [{"id": f"tbl_yes_{b.id}", "title": "Yes, coming"},
                               {"id": f"tbl_cancel_{b.id}", "title": "Cancel booking"}]}
        template = build_template("table_reminder", [first, biz.name, T.when_label(b.start_at), B._guests(b.party)],
                                  button_payloads=[f"tbl_yes_{b.id}", f"tbl_cancel_{b.id}"])
        if await send_whatsapp(b.phone, B._pnid(biz), payload, biz.id, template=template):
            sent += 1
    return sent


async def _waitlist(db, biz, settings, now, claim) -> int:
    if not settings.waitlist_on:
        return 0
    rows = (db.query(TablesWaitlist)
            .filter(TablesWaitlist.business_id == biz.id, TablesWaitlist.status.in_(("waiting", "offered")))
            .order_by(TablesWaitlist.created_at.asc()).all())
    sent, offered_starts = 0, set()
    for entry in rows:
        if entry.start_at <= now:
            entry.status = "expired"
            db.commit()
            continue
        if entry.status != "waiting" or entry.start_at < now + WAITLIST_MIN_LEAD or entry.start_at in offered_starts:
            continue
        zone = T.first_fitting_zone(db, biz, settings, entry, now)
        if zone is None or not claim("tbl_waitlist", entry.id):
            continue
        offered_starts.add(entry.start_at)
        entry.status, entry.offered_at = "offered", datetime.utcnow()
        db.commit()
        first, when = _first_name(entry.name), T.when_label(entry.start_at)
        payload = {"type": "buttons",
                   "body": f"Good news, {first}! A table for {B._guests(entry.party)} opened up at {biz.name} "
                           f"for {when}. Tap below to book it before someone else does.",
                   "buttons": [{"id": f"tbl_wl_{entry.id}", "title": "Book it"}]}
        template = build_template("table_waitlist", [first, biz.name, when], button_payloads=[f"tbl_wl_{entry.id}"])
        if await send_whatsapp(entry.phone, B._pnid(biz), payload, biz.id, template=template):
            sent += 1
            log_event(db=db, event_type="tables_waitlist_offer", status="info",
                      message=f"Waitlist: offered {when} to {entry.name or 'a guest'} ({entry.party} people)",
                      business_id=biz.id)
    return sent


async def _reviews(db, biz, settings, now, claim) -> int:
    if not settings.review_on or not (REVIEW_HOUR <= now.hour < REVIEW_LATEST_HOUR):
        return 0
    yesterday = now.date() - timedelta(days=1)
    sent, seen = 0, set()
    for b in T.bookings_for_day(db, biz, yesterday):
        if b.status != "arrived" or not is_real_phone(b.phone) or b.rating or b.phone in seen:
            continue
        seen.add(b.phone)
        if not claim("tbl_review_guest", f"{biz.id}:{b.phone}:{now:%Y-%m}"):
            continue                       # one review ask per guest per month
        if not claim("tbl_review", b.id):
            continue
        first, day = _first_name(b.name), b.start_at.strftime("%a %d %b")
        payload = {"type": "buttons",
                   "body": f"Hi {first}, thanks for coming to {biz.name} last night! How was it?",
                   "buttons": [{"id": f"tbl_rate_loved_{b.id}", "title": "Loved it"},
                               {"id": f"tbl_rate_ok_{b.id}", "title": "It was OK"},
                               {"id": f"tbl_rate_bad_{b.id}", "title": "Not great"}]}
        template = build_template("table_feedback", [first, biz.name, day],
                                  button_payloads=[f"tbl_rate_loved_{b.id}", f"tbl_rate_ok_{b.id}",
                                                   f"tbl_rate_bad_{b.id}"])
        if await send_whatsapp(b.phone, B._pnid(biz), payload, biz.id, template=template):
            sent += 1
    return sent


async def _offers(db, biz, settings, now, claim) -> int:
    if not settings.rebook_on or not (OFFER_HOURS[0] <= now.hour < OFFER_HOURS[1]):
        return 0
    cap = settings.offers_per_month or 0
    used = T.offers_this_month(db, biz.id)
    if used >= cap:
        if cap and claim("tbl_cap", f"{biz.id}:{now:%Y-%m}"):
            log_event(db=db, event_type="tables_offer_cap", status="warning",
                      message=f"All {cap} book-again messages for this month are used", business_id=biz.id)
        return 0
    weeks = max(1, settings.rebook_weeks or 4)
    window_end = now - timedelta(weeks=weeks)
    window_start = window_end - timedelta(days=7)
    visits = (db.query(TablesBooking)
              .filter(TablesBooking.business_id == biz.id, TablesBooking.status == "arrived",
                      TablesBooking.phone.isnot(None), TablesBooking.start_at >= window_start,
                      TablesBooking.start_at < window_end).all())
    sent = 0
    for b in visits:
        if sent >= OFFERS_PER_TICK or used + sent >= cap:
            break
        phone = b.phone
        later = (db.query(TablesBooking.id)
                 .filter(TablesBooking.business_id == biz.id, TablesBooking.phone == phone,
                         TablesBooking.id != b.id, TablesBooking.start_at >= b.start_at,
                         TablesBooking.status.in_(("pending", "confirmed", "arrived"))).first())
        if later or T.opted_out(db, biz.id, phone):
            continue                       # came back already, or booked: no offer needed
        recent = (db.query(TablesOffer.id)
                  .filter(TablesOffer.business_id == biz.id, TablesOffer.phone == phone,
                          TablesOffer.sent_at > datetime.utcnow() - timedelta(days=OFFER_GAP_DAYS)).first())
        if recent or not claim("tbl_offer", f"{biz.id}:{phone}:{b.id}"):
            continue
        first = _first_name(b.name)
        payload = {"type": "buttons",
                   "body": f"Hi {first}, we miss you at {biz.name}! Tables for this week are open. "
                           f"Book in a few taps:",
                   "buttons": [{"id": "tbl_book", "title": "Book a table"},
                               {"id": "tbl_stop", "title": "Stop offers"}]}
        template = build_template("table_book_again", [first, biz.name], button_payloads=["tbl_book", "tbl_stop"])
        if await send_whatsapp(phone, B._pnid(biz), payload, biz.id, template=template):
            db.add(TablesOffer(business_id=biz.id, phone=phone, month=datetime.utcnow().strftime("%Y-%m")))
            db.commit()
            sent += 1
    return sent
