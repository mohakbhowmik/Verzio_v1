"""
loyalty/v3.py: the v3 ("warm") owner screens for Verzio Loyalty.

Only businesses in VERZIO_PREVIEW see them (see preview.py); everyone else keeps
the current screens. Same pages, same forms and same endpoints as before: this
module only picks the v3 template and adds the extra numbers those screens show.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from loyalty import engine as L
from loyalty.models import LoyaltyClaim, LoyaltyMember
from preview import preview_on

TEMPLATES = {
    "loyalty/dashboard.html": "loyalty/v3/dashboard.html",
    "loyalty/rewards.html": "loyalty/v3/rewards.html",
    "loyalty/customers.html": "loyalty/v3/customers.html",
    "loyalty/grow.html": "loyalty/v3/grow.html",
    "loyalty/results.html": "loyalty/v3/results.html",
    "loyalty/settings.html": "loyalty/v3/settings.html",
}

WINBACK_VISITS = 5
WINBACK_DAYS = 14
FEED_KINDS = ("visit", "manual", "welcome", "streak_daily", "streak_weekly", "bill", "referral", "birthday", "claim")

SEGMENTS = [
    ("all", "Everyone", "All members", "#173b32"),
    ("new", "New", "Joined in 30 days", "#2f7d55"),
    ("reg", "Regulars", "4+ visits, seen lately", "#c79a5a"),
    ("risk", "At risk", "Not seen 15–30 days", "#c8643b"),
    ("lost", "Lost", "Not seen 30+ days", "#6b625a"),
]


def wants_v3(business, settings) -> bool:
    return preview_on(business, getattr(settings, "whatsapp_number", None))


def _local_now(business) -> datetime:
    from booking_engine import business_now
    return business_now(business)


def _days_since(last_visit: str | None, today: date) -> int | None:
    if not last_visit:
        return None
    try:
        return (today - date.fromisoformat(last_visit)).days
    except ValueError:
        return None


def ago_label(days: int | None) -> str:
    if days is None:
        return "–"
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return f"{days} days ago"


def segment_of(member: LoyaltyMember, today: date, now_utc: datetime) -> set[str]:
    out = {"all"}
    ago = _days_since(member.last_visit_date, today)
    if member.created_at and member.created_at >= now_utc - timedelta(days=30):
        out.add("new")
    if ago is not None:
        if member.visits >= 4 and ago <= 14:
            out.add("reg")
        elif member.visits >= 2 and 15 <= ago <= 30:
            out.add("risk")
        if ago > 30:
            out.add("lost")
    return out


def context(db: Session, request, page: str, owner, business, settings, base: dict) -> dict:
    """Extra values for the v3 template of `page` (keys go under `v3`)."""
    from whatsapp_client import is_real_phone
    now_local = _local_now(business)
    today = now_local.date()
    now_utc = datetime.utcnow()
    v3: dict = {"today_date": today, "ago_label": ago_label}
    extra = {"v3": v3, "is_real": is_real_phone}

    if page == "":
        midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        since_utc = now_utc - (now_local - midnight_local)
        v3["today"] = L.results(db, business, settings, days=1, now=since_utc + timedelta(days=1))
        v3["given_today"] = (db.query(func.count(LoyaltyClaim.id))
                             .filter(LoyaltyClaim.business_id == business.id, LoyaltyClaim.status == "given",
                                     LoyaltyClaim.given_at >= since_utc).scalar() or 0)
        hour = now_local.hour
        v3["greeting"] = "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")
        v3["today_label"] = now_local.strftime("%A, %d %B").upper()
        v3["scans"] = [(e, m) for e, m in L.recent_entries(db, business.id, 30) if e.kind in FEED_KINDS][:8]
        cutoff = (today - timedelta(days=WINBACK_DAYS)).isoformat()
        missing = (db.query(LoyaltyMember)
                   .filter(LoyaltyMember.business_id == business.id, LoyaltyMember.visits >= WINBACK_VISITS,
                           LoyaltyMember.last_visit_date.isnot(None), LoyaltyMember.last_visit_date <= cutoff)
                   .order_by(LoyaltyMember.visits.desc()).limit(4).all())
        v3["winback"] = [(m, _days_since(m.last_visit_date, today)) for m in missing]
        v3["winback_visits"], v3["winback_days"] = WINBACK_VISITS, WINBACK_DAYS

    elif page == "rewards":
        since = now_utc - timedelta(days=30)
        rows = (db.query(LoyaltyClaim.reward_id, func.count(LoyaltyClaim.id))
                .filter(LoyaltyClaim.business_id == business.id, LoyaltyClaim.status == "given",
                        LoyaltyClaim.given_at >= since).group_by(LoyaltyClaim.reward_id).all())
        v3["given_by_reward"] = dict(rows)

    elif page == "customers":
        seg = (request.query_params.get("seg") or "all").strip()
        seg = seg if seg in {s[0] for s in SEGMENTS} else "all"
        q = (request.query_params.get("q") or "").strip()[:40]
        everyone = (db.query(LoyaltyMember).filter(LoyaltyMember.business_id == business.id)
                    .order_by(LoyaltyMember.last_visit_date.desc(), LoyaltyMember.visits.desc()).limit(3000).all())
        groups = {m.id: segment_of(m, today, now_utc) for m in everyone}
        counts = {s[0]: 0 for s in SEGMENTS}
        for g in groups.values():
            for key in g:
                counts[key] += 1
        shown = [m for m in everyone if seg in groups[m.id]]
        if q:
            ql, digits = q.lower(), "".join(ch for ch in q if ch.isdigit())
            shown = [m for m in shown if ql in (m.name or "").lower() or (digits and digits in m.phone)]
        if seg == "reg":
            shown.sort(key=lambda m: -m.visits)
        capped = len(shown) > 300
        shown = shown[:300]
        v3.update(seg=seg, members=shown, capped=capped,
                  seg_label=next(s[1] for s in SEGMENTS if s[0] == seg),
                  segments=[{"id": k, "label": label, "hint": hint, "color": color, "count": counts[k]}
                            for k, label, hint, color in SEGMENTS],
                  days_ago={m.id: _days_since(m.last_visit_date, today) for m in shown})
        # The existing template keys are computed for the old member list; give v3 its own.
        extra["streaks"] = {m.id: L.streak_info(business, settings, m) for m in shown}
        extra["tiers"] = {m.id: L.tier_for(settings, m.visits) for m in shown}
        selected = None
        try:
            mid = int(request.query_params.get("m") or 0)
        except ValueError:
            mid = 0
        if mid:
            selected = db.get(LoyaltyMember, mid)
            if selected is not None and selected.business_id != business.id:
                selected = None
        if selected is None and shown:
            selected = shown[0]
        v3["selected"] = selected
        if selected is not None:
            v3["selected_history"] = L.history(db, selected, 8)
            v3["selected_given"] = (db.query(func.count(LoyaltyClaim.id))
                                    .filter(LoyaltyClaim.member_id == selected.id, LoyaltyClaim.status == "given")
                                    .scalar() or 0)

    elif page == "grow":
        v3["r"] = L.results(db, business, settings, 30)
        base_q = db.query(func.count(LoyaltyMember.id)).filter(LoyaltyMember.business_id == business.id)
        gold = base_q.filter(LoyaltyMember.visits >= settings.gold_visits).scalar() or 0
        silver = (db.query(func.count(LoyaltyMember.id))
                  .filter(LoyaltyMember.business_id == business.id, LoyaltyMember.visits >= settings.silver_visits,
                          LoyaltyMember.visits < settings.gold_visits).scalar() or 0)
        v3["gold"], v3["silver"] = (gold, silver) if settings.tiers_on else (0, 0)
        v3["reviews_asked"] = (db.query(func.count(LoyaltyMember.id))
                               .filter(LoyaltyMember.business_id == business.id, LoyaltyMember.review_asked.is_(True))
                               .scalar() or 0)

    elif page == "results":
        r = base.get("r") or {}
        v3["top"] = (db.query(LoyaltyMember).filter(LoyaltyMember.business_id == business.id, LoyaltyMember.visits >= 2)
                     .order_by(LoyaltyMember.visits.desc()).limit(5).all())
        month = settings.monthly_amount or 0
        v3["plan_month"] = month
        days = r.get("days") or 30
        money = r.get("extra_revenue")
        v3["roi"] = int(round(money / (month * days / 30))) if money and month else None

    elif page == "settings":
        try:
            from whatsapp_health import WhatsAppHealth
            v3["health"] = db.get(WhatsAppHealth, business.id)
        except Exception:
            v3["health"] = None
        v3["marketing_used"] = L.marketing_sent_this_month(db, business.id, today)

    return extra
