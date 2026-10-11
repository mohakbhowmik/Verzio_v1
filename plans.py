"""
plans.py
================================================================================
VERZIO STUDIO — MEMBERSHIP PLANS
================================================================================
One place that says what each plan includes, and whether a business's plan is
currently active. Everything that is limited by plan asks this module.

| Plan       | Bookings/month (soft) | Services on WhatsApp | At the same time | Morning summary | Excel export |
|------------|-----------------------|----------------------|------------------|-----------------|--------------|
| Basic      | 200                   | 5                    | 1                | no              | no           |
| Pro        | 1,000                 | unlimited            | 10               | yes             | yes          |
| Trial      | as Pro                | unlimited            | 10               | yes             | yes          |
| Suspended  | WhatsApp booking off; the owner portal still works                               |

The plan and "paid until" date come from Admin → Subscriptions (the
subscriptions table: plan, status, next_billing_date). A business with no
subscription row is on Trial with no end date, so nothing changes for it until
you set a plan.

Bookings per month is a SOFT limit: bookings are never refused for it. At 80%
and 100% the owner and you are told (scheduler.py), so you can upsell.

When "paid until" passes, there are GRACE_DAYS more days of service, then
WhatsApp booking stops (customers get the "not taking WhatsApp bookings right
now" reply) until you extend the date.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session, object_session

from database import Appointment, Business, SessionLocal, Subscription

logger = logging.getLogger("VERZIO_PLANS")

GRACE_DAYS = 3

PLANS = {
    "basic": {"label": "Basic", "bookings": 200, "services": 5, "parallel": 1,
              "morning_summary": False, "export": False},
    "pro": {"label": "Pro", "bookings": 1000, "services": None, "parallel": 10,
            "morning_summary": True, "export": True},
    "trial": {"label": "Trial", "bookings": 1000, "services": None, "parallel": 10,
              "morning_summary": True, "export": True},
}
PLAN_CHOICES = ["Trial", "Basic", "Pro", "Suspended"]     # what admin can pick


@dataclass
class PlanInfo:
    key: str                      # trial | basic | pro | suspended
    label: str
    bookings: int
    services: int | None          # None = unlimited
    parallel: int
    morning_summary: bool
    export: bool
    paid_until: date | None = None
    active: bool = True           # False: WhatsApp booking is switched off
    in_grace: bool = False        # past paid_until but still inside GRACE_DAYS
    reason: str = ""              # why it's inactive / in grace, for the owner
    extra: dict = field(default_factory=dict)


def _parse_date(raw) -> date | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return datetime.fromisoformat(str(raw).strip()).date()
    except ValueError:
        return None


def normalize(raw: str | None) -> str:
    key = (raw or "trial").strip().lower()
    return key if key in PLANS or key == "suspended" else "trial"


def _session_for(biz: Business, db: Session | None):
    """Use the caller's session, else the one `biz` is attached to, else a new one."""
    if db is not None:
        return db, False
    sess = object_session(biz)
    if sess is not None:
        return sess, False
    return SessionLocal(), True


def plan_for(biz: Business, db: Session | None = None, today: date | None = None) -> PlanInfo:
    sess, own = _session_for(biz, db)
    try:
        sub = sess.query(Subscription).filter(Subscription.business_id == biz.id).first()
    except Exception:
        logger.exception("Could not read the plan for business_id=%s; treating as Trial", biz.id)
        sub = None
    finally:
        if own:
            sess.close()

    key = normalize(sub.plan if sub else None)
    if sub and (sub.status or "").lower() == "suspended":
        key = "suspended"

    if key == "suspended":
        info = PlanInfo(key="suspended", label="Suspended", active=False,
                        reason="Your plan is suspended. Contact Verzio to switch WhatsApp booking back on.",
                        **{k: v for k, v in PLANS["basic"].items() if k != "label"})
        info.paid_until = _parse_date(sub.next_billing_date) if sub else None
        return info

    spec = PLANS[key]
    info = PlanInfo(key=key, **spec)
    info.paid_until = _parse_date(sub.next_billing_date) if sub else None
    today = today or date.today()
    if info.paid_until and today > info.paid_until:
        last_day = info.paid_until + timedelta(days=GRACE_DAYS)
        if today > last_day:
            info.active = False
            info.reason = (f"Your {info.label} plan ended on {info.paid_until:%d %b %Y}. "
                           f"WhatsApp booking is off until it's renewed.")
        else:
            info.in_grace = True
            info.reason = (f"Your {info.label} plan ended on {info.paid_until:%d %b %Y}. "
                           f"WhatsApp booking stops after {last_day:%d %b} unless it's renewed.")
    return info


def capacity(biz: Business, db: Session | None = None) -> int:
    """Customers at the same time: the business setting, capped by the plan."""
    wanted = max(int(biz.max_parallel_bookings or 1), 1)
    return min(wanted, plan_for(biz, db).parallel)


def month_start(today: date | None = None) -> datetime:
    today = today or date.today()
    return datetime(today.year, today.month, 1)


def bookings_this_month(biz: Business, db: Session | None = None) -> int:
    """Bookings created this calendar month that weren't cancelled."""
    sess, own = _session_for(biz, db)
    try:
        return (
            sess.query(Appointment)
            .filter(Appointment.business_id == biz.id,
                    Appointment.created_at >= month_start(),
                    Appointment.status != "cancelled")
            .count()
        )
    finally:
        if own:
            sess.close()


def usage(biz: Business, db: Session | None = None) -> dict:
    """Everything the owner portal shows about the plan."""
    info = plan_for(biz, db)
    used = bookings_this_month(biz, db)
    pct = int(used * 100 / info.bookings) if info.bookings else 0
    return {
        "plan": info,
        "bookings_used": used,
        "bookings_limit": info.bookings,
        "bookings_pct": min(pct, 100),
        "over_limit": used > info.bookings,
        "near_limit": pct >= 80,
    }
