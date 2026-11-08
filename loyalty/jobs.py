"""
loyalty/jobs.py: Verzio Loyalty Plus timed jobs, run by scheduler.tick() every few minutes.

1. Birthdays       On a member's birthday (from BIRTHDAY_HOURS), add the owner's
                   birthday points and say happy birthday. Inside the 24-hour
                   window it's a free message; otherwise the loyalty_birthday
                   Marketing template (~₹1), up to offers_per_month a month.
                   Only for members who visited in the last ACTIVE_DAYS days and
                   never for anyone who replied STOP (here or in Verzio Tables).
2. Weekly summary  Monday from SUMMARY_HOURS: last week's numbers to the
                   manager number (loyalty_weekly_summary, Utility).

Every send is claimed in scheduled_sends first, so nothing is sent twice.
"""
import logging
from datetime import date, datetime, timedelta

from activity_log import log_event
from loyalty import engine as L
from loyalty.bot import PUBLIC_BASE_URL
from loyalty.models import LoyaltyClaim, LoyaltyEntry, LoyaltyMember
from whatsapp_client import _first_name, build_template, canonical_phone, is_real_phone, send_whatsapp

logger = logging.getLogger("VERZIO_LOYALTY_JOBS")

BIRTHDAY_HOURS = (10, 20)          # business-local
ACTIVE_DAYS = 180
BIRTHDAYS_PER_TICK = 40
SUMMARY_HOURS = (9, 13)            # Monday, business-local


def _businesses(db):
    from scheduler import _live_businesses
    out = []
    for biz in _live_businesses(db):
        settings = L.get_settings(db, biz.id)
        if settings and settings.enabled and L.running(db, biz):
            out.append((biz, settings))
    return out


async def run(db) -> dict:
    from booking_engine import business_now
    from scheduler import claim
    result = {"birthdays": 0, "summaries": 0}
    for biz, settings in _businesses(db):
        now = business_now(biz)
        for job, key in ((_birthdays, "birthdays"), (_weekly_summary, "summaries")):
            try:
                result[key] += await job(db, biz, settings, now, claim)
            except Exception:
                db.rollback()
                logger.exception("Loyalty job %s failed for business_id=%s", key, biz.id)
    return result


def _tables_opted_out(db, biz, phone) -> bool:
    try:
        from tables.engine import opted_out
        return opted_out(db, biz.id, phone)
    except Exception:
        return False


async def _birthdays(db, biz, settings, now: datetime, claim) -> int:
    if not settings.birthday_on or settings.birthday_points <= 0:
        return 0
    if not (BIRTHDAY_HOURS[0] <= now.hour < BIRTHDAY_HOURS[1]):
        return 0
    today = now.date()
    active_since = (today - timedelta(days=ACTIVE_DAYS)).isoformat()
    members = (db.query(LoyaltyMember)
               .filter(LoyaltyMember.business_id == biz.id, LoyaltyMember.birthday.isnot(None),
                       LoyaltyMember.last_visit_date >= active_since).all())
    due = [m for m in members if L.birthday_today(m, today)]
    if not due:
        return 0
    from whatsapp_client import window_open
    cap = settings.offers_per_month or 0
    used = L.marketing_sent_this_month(db, biz.id, today)
    sent = 0
    for m in due:
        if sent >= BIRTHDAYS_PER_TICK:
            break
        if m.offers_off or _tables_opted_out(db, biz, m.phone) or not is_real_phone(m.phone):
            continue
        free = window_open(biz.id, m.phone)
        if not free and used >= cap:
            if cap and claim("loy_cap", f"{biz.id}:{today:%Y-%m}"):
                log_event(db=db, event_type="loyalty_offer_cap", status="warning",
                          message=f"All {cap} birthday messages for this month are used", business_id=biz.id)
            continue
        if not claim("loy_bday", f"{m.id}:{today.year}"):
            continue
        L.give_birthday(db, m, settings.birthday_points)
        first = _first_name(m.name)
        body = (f"Happy birthday, {first}! 🎂 {biz.name} has added {settings.birthday_points} birthday points "
                f"to your wallet.\n\nWallet: {m.points} points. Come in this week and treat yourself!")
        payload = {"type": "buttons", "body": body,
                   "buttons": [{"id": "loy_wallet", "title": "My wallet"}, {"id": "loy_stop", "title": "Stop offers"}]}
        template = build_template("loyalty_birthday", [first, biz.name, settings.birthday_points],
                                  button_payloads=["loy_wallet", "loy_stop"])
        if await send_whatsapp(m.phone, biz.whatsapp_business_phone_number_id, payload, biz.id, template=template):
            sent += 1
            if not free:
                used += 1
    if sent:
        log_event(db=db, event_type="loyalty_birthdays", status="success",
                  message=f"Sent {sent} birthday gift{'s' if sent != 1 else ''}", business_id=biz.id)
    return sent


def week_numbers(db, biz, start_utc: datetime, end_utc: datetime) -> dict:
    """New members, visits, repeat visits and rewards given between two UTC times."""
    from sqlalchemy import func
    first_visit = dict(db.query(LoyaltyEntry.member_id, func.min(LoyaltyEntry.id))
                       .filter(LoyaltyEntry.business_id == biz.id, LoyaltyEntry.kind.in_(L.VISIT_KINDS))
                       .group_by(LoyaltyEntry.member_id).all())
    visits = (db.query(LoyaltyEntry)
              .filter(LoyaltyEntry.business_id == biz.id, LoyaltyEntry.kind.in_(L.VISIT_KINDS),
                      LoyaltyEntry.created_at >= start_utc, LoyaltyEntry.created_at < end_utc).all())
    new = len({e.member_id for e in visits if first_visit.get(e.member_id) == e.id})
    given = (db.query(func.count(LoyaltyClaim.id))
             .filter(LoyaltyClaim.business_id == biz.id, LoyaltyClaim.status == "given",
                     LoyaltyClaim.given_at >= start_utc, LoyaltyClaim.given_at < end_utc).scalar() or 0)
    return {"new": new, "visits": len(visits), "repeat": len(visits) - new, "given": given}


async def _weekly_summary(db, biz, settings, now: datetime, claim) -> int:
    if not settings.summary_on or now.weekday() != 0 or not (SUMMARY_HOURS[0] <= now.hour < SUMMARY_HOURS[1]):
        return 0
    manager = canonical_phone(biz.manager_phone_number, biz.timezone)
    from booking_engine import owner_cannot_approve
    if not is_real_phone(manager) or owner_cannot_approve(biz):
        return 0                      # no separate number to send to
    year, week, _ = (now.date() - timedelta(days=7)).isocalendar()
    if not claim("loy_week", f"{biz.id}:{year}-{week:02d}"):
        return 0
    end_utc = datetime.utcnow() - timedelta(hours=now.hour, minutes=now.minute)   # about local midnight
    n = week_numbers(db, biz, end_utc - timedelta(days=7), end_utc)
    body = (f"📊 Your week at {biz.name} with Verzio Loyalty\n\n"
            f"New members: {n['new']}\nVisits: {n['visits']}\nRepeat visits: {n['repeat']}\n"
            f"Rewards given: {n['given']}\n\nSee more: {PUBLIC_BASE_URL}/owner/loyalty/results")
    template = build_template("loyalty_weekly_summary",
                              [biz.name, n["new"], n["visits"], n["repeat"], n["given"]])
    ok = await send_whatsapp(manager, biz.whatsapp_business_phone_number_id, {"type": "text", "body": body},
                             biz.id, template=template)
    return 1 if ok else 0
