"""
loyalty/engine.py: the loyalty rules. No WhatsApp or HTML here.

- Customers earn points per visit by sending the counter QR's code (once per
  business-local day), or the owner adds them in the portal.
- A reward is unlocked when the balance reaches its points. The customer
  claims it and gets a 4-digit code, valid CLAIM_MINUTES.
- Points are deducted only when staff confirm the code in the portal
  ("Give reward"), never at claim time. A stray tap costs nothing.
- Every change to a balance is written to LoyaltyEntry with the balance after
  it, so the owner can always answer "where did my points go?".

Loyalty Plus (each part off until the owner switches it on):
- Outlets: one brand, one WhatsApp number, several counters with their own QR.
  Points, rewards and the once-a-day rule are shared across outlets.
- Tiers: Silver / Gold after a number of visits: +x% on visit and bill points.
- Double points: rules by weekday (and optional hours) multiply visit and bill points.
- Bill points: each scan also gives a 4-digit bill code; staff type it with the
  bill amount -> points per ₹100.
- Referrals: an invite link (JOIN <code>); on the friend's first visit both get points.
- Birthday: asked once (or set on the wallet page); points on the day.
- Google review ask: once, on the Nth visit, inside the free 24-hour window.
"""
import logging
import random
import secrets
import string
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from database import Business
from loyalty.models import (LoyaltyBillCode, LoyaltyBoost, LoyaltyClaim, LoyaltyEntry, LoyaltyMember,
                            LoyaltyOutlet, LoyaltyReferral, LoyaltyReward, LoyaltySettings, LoyaltyStaff)
from whatsapp_client import canonical_phone, is_real_phone, normalize_phone

logger = logging.getLogger("VERZIO_LOYALTY")

CLAIM_MINUTES = 15
BILL_CODE_MINUTES = 60
MAX_POINTS = 100000          # sanity cap for any single number the owner types
MAX_BILL = 200000            # ₹, sanity cap for one bill
REFERRAL_MONTHLY_CAP = 10    # rewarded invites per customer per month
MAX_OUTLETS = 30
BAR_CELLS = 10

_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1IL")


class LoyaltyError(Exception):
    """A rule was broken. `message` is safe to show the owner or customer."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------

def new_earn_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


def get_settings(db: Session, business_id: int) -> LoyaltySettings | None:
    return db.get(LoyaltySettings, business_id)


def is_enabled(db: Session, biz: Business) -> bool:
    row = get_settings(db, biz.id)
    return bool(row and row.enabled)


def is_loyalty_only(db: Session, biz: Business) -> bool:
    row = get_settings(db, biz.id)
    return bool(row and row.enabled and row.mode == "only")


def ensure_settings(db: Session, biz: Business) -> LoyaltySettings:
    """The business's settings row, created (switched off) if missing."""
    row = get_settings(db, biz.id)
    if row is None:
        row = LoyaltySettings(business_id=biz.id, enabled=False, mode="with_booking",
                              points_per_visit=10, welcome_bonus=0, earn_code=new_earn_code(),
                              whatsapp_number=_connected_number(biz))
        db.add(row)
        db.commit()
    return row


def _connected_number(biz: Business) -> str | None:
    """The business's WhatsApp number, if it was connected through Embedded Signup."""
    try:
        from whatsapp_accounts import account_credentials
        _, display = account_credentials(biz.id)
        return canonical_phone(display, biz.timezone) if display else None
    except Exception:
        return None


GRACE_DAYS = 3
PLAN_LABELS = {"trial": "Trial", "active": "Active", "suspended": "Suspended"}


@dataclass
class LoyaltyPlan:
    key: str                 # trial | active | suspended
    label: str
    active: bool             # False: rewards are paused on WhatsApp
    paid_until: date | None = None
    in_grace: bool = False   # past paid_until but within GRACE_DAYS
    reason: str = ""         # shown to the owner when paused or in grace
    amount: int = 0


def plan_info(settings: LoyaltySettings | None, today: date | None = None) -> LoyaltyPlan:
    """Verzio Loyalty's own plan. Trial has no end date; Active runs to paid_until
    plus GRACE_DAYS; Suspended is off at once. Independent of the Verzio Book plan."""
    if settings is None:
        return LoyaltyPlan("trial", "Trial", False)
    key = settings.plan if settings.plan in PLAN_LABELS else "trial"
    info = LoyaltyPlan(key, PLAN_LABELS[key], key != "suspended", amount=settings.monthly_amount or 0)
    if key == "suspended":
        info.reason = "Your Verzio Loyalty plan is suspended. Contact Verzio Solutions to switch rewards back on."
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
            info.reason = (f"Your Verzio Loyalty plan ended on {info.paid_until:%d %b %Y}. "
                           f"Rewards are paused on WhatsApp until it's renewed.")
        elif today > info.paid_until:
            info.in_grace = True
            info.reason = (f"Your Verzio Loyalty plan ended on {info.paid_until:%d %b %Y}. "
                           f"Rewards stop after {last_day:%d %b} unless it's renewed.")
    return info


def running(db: Session, biz: Business) -> bool:
    """Loyalty works for customers only while the business is active and its
    Verzio Loyalty plan is (the Verzio Book plan doesn't matter here)."""
    if not biz.is_active:
        return False
    return plan_info(get_settings(db, biz.id)).active


def qr_link(settings: LoyaltySettings) -> str | None:
    """wa.me link the counter QR opens: the cafe's chat with 'STAMP <code>' typed in."""
    number = normalize_phone(settings.whatsapp_number)
    if not is_real_phone(number):
        return None
    return f"https://wa.me/{number}?text=STAMP%20{settings.earn_code}"


# ------------------------------------------------------------------------
# Rewards
# ------------------------------------------------------------------------

def rewards(db: Session, business_id: int, include_inactive: bool = False) -> list[LoyaltyReward]:
    q = db.query(LoyaltyReward).filter(LoyaltyReward.business_id == business_id,
                                       LoyaltyReward.is_deleted == False)  # noqa: E712
    if not include_inactive:
        q = q.filter(LoyaltyReward.is_active == True)  # noqa: E712
    return q.order_by(LoyaltyReward.points.asc(), LoyaltyReward.name.asc()).all()


def clean_points(raw, allow_zero: bool = False, allow_negative: bool = False) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise LoyaltyError("Please enter a whole number of points.")
    if abs(value) > MAX_POINTS:
        raise LoyaltyError(f"That's too many points (max {MAX_POINTS:,}).")
    if value < 0 and not allow_negative:
        raise LoyaltyError("Points can't be negative.")
    if value == 0 and not allow_zero:
        raise LoyaltyError("Points must be more than 0.")
    return value


@dataclass
class Progress:
    reward: LoyaltyReward
    have: int
    need: int

    @property
    def unlocked(self) -> bool:
        return self.have >= self.need

    @property
    def percent(self) -> int:
        return 100 if self.unlocked else int(self.have * 100 / self.need) if self.need else 100

    @property
    def missing(self) -> int:
        return max(0, self.need - self.have)

    @property
    def bar(self) -> str:
        filled = BAR_CELLS if self.unlocked else min(BAR_CELLS - 1, self.have * BAR_CELLS // self.need)
        return "🟩" * filled + "⬜" * (BAR_CELLS - filled)


def progress(db: Session, member: LoyaltyMember) -> list[Progress]:
    return [Progress(r, member.points, r.points) for r in rewards(db, member.business_id)]


# ------------------------------------------------------------------------
# Members and points
# ------------------------------------------------------------------------

def find_member(db: Session, business_id: int, phone: str) -> LoyaltyMember | None:
    return (db.query(LoyaltyMember)
            .filter(LoyaltyMember.business_id == business_id, LoyaltyMember.phone == normalize_phone(phone))
            .first())


def get_or_create_member(db: Session, biz: Business, phone: str, name: str | None = None) -> tuple[LoyaltyMember, bool]:
    digits = canonical_phone(phone, biz.timezone)
    if not is_real_phone(digits):
        raise LoyaltyError("Please enter a valid mobile number.")
    member = find_member(db, biz.id, digits)
    if member:
        if name and not member.name:
            member.name = name.strip()[:60]
        return member, False
    member = LoyaltyMember(business_id=biz.id, phone=digits, name=(name or "").strip()[:60] or None,
                           points=0, visits=0, wallet_token=secrets.token_urlsafe(18),
                           birthday_asked=False, review_asked=False, offers_off=False)
    db.add(member)
    db.flush()
    return member, True


def _add(db: Session, member: LoyaltyMember, delta: int, kind: str, note: str | None = None,
         outlet_id: int | None = None, bill_amount: int | None = None, boost: int | None = None) -> LoyaltyEntry:
    member.points = max(0, member.points + delta)
    entry = LoyaltyEntry(business_id=member.business_id, member_id=member.id, delta=delta,
                         kind=kind, note=(note or "")[:120] or None, balance_after=member.points,
                         outlet_id=outlet_id, bill_amount=bill_amount, boost=boost)
    db.add(entry)
    return entry


# ------------------------------------------------------------------------
# Streaks
# ------------------------------------------------------------------------
# Daily: visits on consecutive OPEN days. Days the business is closed (no
#   opening hours that weekday) are skipped, so Sunday -> Tuesday still counts
#   when Monday is the closed day.
# Weekly: at least one visit in each consecutive calendar week (Mon-Sun).
# Bonus = step x (streak - 1), never above the cap. The counters are kept even
# while a streak type is switched off, so switching it on later just works.

_DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _open_weekdays(biz: Business) -> set[int]:
    hours = biz.operational_hours or {}
    days = {i for i, key in enumerate(_DAY_KEYS) if hours.get(key)}
    return days or set(range(7))        # no hours set: treat every day as open


def previous_open_day(biz: Business, day: date) -> date:
    open_days = _open_weekdays(biz)
    d = day - timedelta(days=1)
    for _ in range(7):
        if d.weekday() in open_days:
            return d
        d -= timedelta(days=1)
    return day - timedelta(days=1)


def next_open_day(biz: Business, day: date) -> date:
    open_days = _open_weekdays(biz)
    d = day + timedelta(days=1)
    for _ in range(7):
        if d.weekday() in open_days:
            return d
        d += timedelta(days=1)
    return day + timedelta(days=1)


def _week(d: date) -> tuple[int, int]:
    year, week, _ = d.isocalendar()
    return year, week


def streak_bonus(step: int, cap: int, streak: int) -> int:
    return max(0, min(cap, step * (streak - 1))) if streak >= 2 else 0


@dataclass
class StreakInfo:
    daily: int = 0                  # current daily streak (0 = broken)
    weekly: int = 0                 # current weekly streak (0 = broken)
    visited_today: bool = False
    visited_this_week: bool = False
    next_visit_day: str = "tomorrow"   # when the daily streak continues: "tomorrow" / "on Tuesday" / "today"
    next_daily_bonus: int = 0       # bonus for coming on the next open day
    next_weekly_bonus: int = 0      # bonus for coming next week
    daily_on: bool = False
    weekly_on: bool = False


def streak_info(biz: Business, settings: LoyaltySettings | None, member: LoyaltyMember,
                today: date | None = None) -> StreakInfo:
    """Streaks as the customer should see them right now."""
    from booking_engine import business_today
    today = today or business_today(biz)
    info = StreakInfo(daily_on=bool(settings and settings.daily_streak_on),
                      weekly_on=bool(settings and settings.weekly_streak_on))
    last = date.fromisoformat(member.last_visit_date) if member.last_visit_date else None
    if last is None:
        return info
    info.visited_today = last == today
    info.visited_this_week = _week(last) == _week(today)
    if not info.visited_today:
        info.next_visit_day = "today"
    else:
        nxt = next_open_day(biz, today)
        info.next_visit_day = "tomorrow" if nxt == today + timedelta(days=1) else f"on {nxt:%A}"
    if last in (today, previous_open_day(biz, today)):
        info.daily = member.daily_streak
    if _week(last) in (_week(today), _week(today - timedelta(days=7))):
        info.weekly = member.weekly_streak
    if settings:
        # The next visit (today if not yet, else the next open day) makes each streak +1;
        # a broken streak (0) restarts at 1, which earns no bonus.
        info.next_daily_bonus = streak_bonus(settings.daily_streak_step, settings.daily_streak_max, info.daily + 1)
        info.next_weekly_bonus = streak_bonus(settings.weekly_streak_step, settings.weekly_streak_max, info.weekly + 1)
    return info


def _record_visit(db: Session, biz: Business, settings: LoyaltySettings, member: LoyaltyMember,
                  today: date) -> tuple[int, int]:
    """Update visit count, last visit and both streaks; add any streak bonuses.
    Returns (daily bonus, weekly bonus). Call once per visit day."""
    last = date.fromisoformat(member.last_visit_date) if member.last_visit_date else None
    member.daily_streak = member.daily_streak + 1 if last == previous_open_day(biz, today) else 1
    new_week = last is None or _week(last) != _week(today)
    if not new_week:
        member.weekly_streak = max(1, member.weekly_streak)
    elif last is not None and _week(last) == _week(today - timedelta(days=7)):
        member.weekly_streak += 1
    else:
        member.weekly_streak = 1
    member.visits += 1
    member.last_visit_date = today.isoformat()

    daily_bonus = weekly_bonus = 0
    if settings.daily_streak_on:
        daily_bonus = streak_bonus(settings.daily_streak_step, settings.daily_streak_max, member.daily_streak)
        if daily_bonus:
            _add(db, member, daily_bonus, "streak_daily", f"{member.daily_streak}-day streak bonus")
    if settings.weekly_streak_on and new_week:
        weekly_bonus = streak_bonus(settings.weekly_streak_step, settings.weekly_streak_max, member.weekly_streak)
        if weekly_bonus:
            _add(db, member, weekly_bonus, "streak_weekly", f"{member.weekly_streak}-week streak bonus")
    return daily_bonus, weekly_bonus


# ------------------------------------------------------------------------
# Earning
# ------------------------------------------------------------------------

@dataclass
class EarnResult:
    member: LoyaltyMember
    earned: int                  # points added now (0 if already earned today)
    already_today: bool = False
    welcome: int = 0             # welcome bonus included in `earned`
    newly_unlocked: list = None  # rewards that unlocked with this earn
    daily_bonus: int = 0         # streak bonuses included in `earned`
    weekly_bonus: int = 0
    streak: StreakInfo = None
    # Loyalty Plus
    outlet: "LoyaltyOutlet | None" = None
    visit_points: int = 0        # the visit part, after tier and double points
    tier: "Tier | None" = None   # tier used for this visit
    tier_up: "Tier | None" = None  # reached a new tier with this visit
    boost: int = 1               # 2 / 3 when a double-points rule applied
    bill_code: "LoyaltyBillCode | None" = None
    referral_bonus: int = 0      # included in `earned`
    referrer: "LoyaltyMember | None" = None   # to tell them their friend came
    referrer_bonus: int = 0
    ask_review: bool = False
    ask_birthday: bool = False


def match_code(db: Session, settings: LoyaltySettings, code: str) -> tuple[bool, LoyaltyOutlet | None]:
    """(True, None) for the main counter's code, (True, outlet) for an outlet's code."""
    code = (code or "").strip().upper()
    if not code:
        return False, None
    if code == settings.earn_code.upper():
        return True, None
    outlet = (db.query(LoyaltyOutlet)
              .filter(LoyaltyOutlet.business_id == settings.business_id, LoyaltyOutlet.is_active == True,  # noqa: E712
                      LoyaltyOutlet.earn_code == code).first())
    return (True, outlet) if outlet else (False, None)


def _visit_points(settings: LoyaltySettings, base: int, tier: "Tier", boost: int) -> int:
    return int(round(base * (100 + tier.bonus) / 100.0)) * max(1, boost)


def earn_visit(db: Session, biz: Business, phone: str, code: str, name: str | None = None,
               today: date | None = None, now: datetime | None = None) -> EarnResult:
    """Customer sent a counter QR's code. Raises LoyaltyError for a wrong code.
    `now` is business-local time (for double-points hours); defaults to now."""
    settings = get_settings(db, biz.id)
    if not settings or not settings.enabled:
        raise LoyaltyError("Rewards aren't switched on here.")
    ok, outlet = match_code(db, settings, code)
    if not ok:
        raise LoyaltyError("wrong_code")

    from booking_engine import business_now, business_today
    now = now or business_now(biz)
    today = today or business_today(biz)
    member, created = get_or_create_member(db, biz, phone, name)
    outlet_id = outlet.id if outlet else None
    if member.last_visit_date == today.isoformat():
        bill = issue_bill_code(db, biz, settings, member, outlet_id) if settings.bill_points_on else None
        db.commit()
        return EarnResult(member=member, earned=0, already_today=True, newly_unlocked=[],
                          streak=streak_info(biz, settings, member, today), outlet=outlet, bill_code=bill,
                          tier=tier_for(settings, member.visits))

    before, visits_before = member.points, member.visits
    welcome = 0
    if created and settings.welcome_bonus > 0:
        welcome = settings.welcome_bonus
        _add(db, member, welcome, "welcome", "Welcome bonus", outlet_id=outlet_id)
    tier = tier_for(settings, visits_before)
    boost = active_boost(db, biz, now)
    points = _visit_points(settings, settings.points_per_visit, tier, boost)
    note = "Visit (QR)" + (f" · {outlet.name}" if outlet else "") + (f" · {boost}x points" if boost > 1 else "")
    _add(db, member, points, "visit", note, outlet_id=outlet_id, boost=boost if boost > 1 else None)
    daily_bonus, weekly_bonus = _record_visit(db, biz, settings, member, today)
    result = EarnResult(member=member, earned=0, welcome=welcome, daily_bonus=daily_bonus,
                        weekly_bonus=weekly_bonus, outlet=outlet, visit_points=points, tier=tier, boost=boost)
    new_tier = tier_for(settings, member.visits)
    if new_tier.key and new_tier.key != tier.key:
        result.tier_up = new_tier
    if visits_before == 0:
        _apply_referral(db, biz, settings, member, result, outlet_id)
    if settings.bill_points_on:
        result.bill_code = issue_bill_code(db, biz, settings, member, outlet_id)
    # At most one question per reply: the review ask first, the birthday on a later visit.
    if (settings.review_on and settings.review_link and not member.review_asked
            and member.visits >= max(1, settings.review_after or 1)):
        member.review_asked = result.ask_review = True
    elif settings.birthday_on and not member.birthday and not member.birthday_asked and member.visits >= 2:
        member.birthday_asked = result.ask_birthday = True
    db.commit()
    result.earned = member.points - before
    result.newly_unlocked = _newly_unlocked(db, member, before)
    result.streak = streak_info(biz, settings, member, today)
    return result


def owner_adjust(db: Session, biz: Business, phone: str, points, note: str | None,
                 name: str | None = None, as_visit: bool = False,
                 today: date | None = None, outlet_id: int | None = None) -> tuple[LoyaltyMember, int, list]:
    """Owner adds (or removes, if negative) points. Returns (member, applied delta, newly unlocked).
    A staff-added visit counts towards streaks like a QR scan (once per day)."""
    delta = clean_points(points, allow_negative=not as_visit)
    if delta < 0:
        member = find_member(db, biz.id, canonical_phone(phone, biz.timezone))
        if member is None or member.points == 0:
            raise LoyaltyError("This customer has no points to remove.")
    else:
        member, _ = get_or_create_member(db, biz, phone, name)
    before = member.points
    delta = max(delta, -member.points)
    if as_visit:
        visits_before = member.visits
        _add(db, member, delta, "manual", note or "Visit (added by staff)", outlet_id=outlet_id)
        from booking_engine import business_today
        today = today or business_today(biz)
        settings = get_settings(db, biz.id)
        if settings and member.last_visit_date != today.isoformat():
            _record_visit(db, biz, settings, member, today)
        else:
            member.visits += 1
        if settings and visits_before == 0:
            _apply_referral(db, biz, settings, member, None, outlet_id)
    else:
        _add(db, member, delta, "adjust", note or ("Added by staff" if delta > 0 else "Removed by staff"),
             outlet_id=outlet_id)
    db.commit()
    return member, member.points - before, _newly_unlocked(db, member, before)


def _newly_unlocked(db: Session, member: LoyaltyMember, before: int) -> list[LoyaltyReward]:
    return [r for r in rewards(db, member.business_id) if before < r.points <= member.points]


def history(db: Session, member: LoyaltyMember, limit: int = 20) -> list[LoyaltyEntry]:
    return (db.query(LoyaltyEntry).filter(LoyaltyEntry.member_id == member.id)
            .order_by(LoyaltyEntry.id.desc()).limit(limit).all())


# ------------------------------------------------------------------------
# Claims
# ------------------------------------------------------------------------

def active_claim(db: Session, member: LoyaltyMember) -> LoyaltyClaim | None:
    return (db.query(LoyaltyClaim)
            .filter(LoyaltyClaim.member_id == member.id, LoyaltyClaim.status == "pending",
                    LoyaltyClaim.expires_at > datetime.utcnow())
            .order_by(LoyaltyClaim.id.desc()).first())


def _unique_code(db: Session, business_id: int) -> str:
    now = datetime.utcnow()
    for _ in range(50):
        code = f"{random.SystemRandom().randint(0, 9999):04d}"
        clash = (db.query(LoyaltyClaim.id)
                 .filter(LoyaltyClaim.business_id == business_id, LoyaltyClaim.code == code,
                         LoyaltyClaim.status == "pending", LoyaltyClaim.expires_at > now)
                 .first())
        if not clash:
            return code
    raise LoyaltyError("Too many claims right now. Please try again in a minute.")


def start_claim(db: Session, biz: Business, member: LoyaltyMember, reward_id: int) -> LoyaltyClaim:
    """Customer wants a reward: give them a code to show. No points are taken yet."""
    reward = db.get(LoyaltyReward, reward_id)
    if (reward is None or reward.business_id != biz.id or reward.is_deleted or not reward.is_active):
        raise LoyaltyError("That reward isn't available any more.")
    if member.points < reward.points:
        raise LoyaltyError(f"You need {reward.points - member.points} more points for {reward.name}.")
    # One live code per customer: a new claim replaces the old one.
    (db.query(LoyaltyClaim)
     .filter(LoyaltyClaim.member_id == member.id, LoyaltyClaim.status == "pending")
     .update({LoyaltyClaim.status: "replaced"}, synchronize_session=False))
    claim = LoyaltyClaim(business_id=biz.id, member_id=member.id, reward_id=reward.id,
                         reward_name=reward.name, points=reward.points, code=_unique_code(db, biz.id),
                         status="pending", expires_at=datetime.utcnow() + timedelta(minutes=CLAIM_MINUTES))
    db.add(claim)
    db.commit()
    return claim


def pending_claims(db: Session, business_id: int) -> list[LoyaltyClaim]:
    return (db.query(LoyaltyClaim)
            .filter(LoyaltyClaim.business_id == business_id, LoyaltyClaim.status == "pending",
                    LoyaltyClaim.expires_at > datetime.utcnow())
            .order_by(LoyaltyClaim.created_at.desc()).all())


def find_claim_by_code(db: Session, business_id: int, code: str) -> LoyaltyClaim | None:
    """The live claim with this code, else the most recent one (to explain why it can't be used)."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 4:
        return None
    rows = (db.query(LoyaltyClaim)
            .filter(LoyaltyClaim.business_id == business_id, LoyaltyClaim.code == code)
            .order_by(LoyaltyClaim.id.desc()).limit(20).all())
    for row in rows:
        if row.status == "pending" and row.expires_at > datetime.utcnow():
            return row
    return rows[0] if rows else None


def claim_problem(claim: LoyaltyClaim) -> str | None:
    """Why this claim can't be given right now, or None if it can."""
    if claim.status == "given":
        return "This reward was already given."
    if claim.status == "replaced":
        return "The customer claimed something else after this. Ask them for their newest code."
    if claim.expires_at <= datetime.utcnow():
        return "This code has expired. Ask the customer to tap Claim again."
    if claim.member.points < claim.points:
        return f"The customer only has {claim.member.points} points now ({claim.points} needed)."
    return None


def give_reward(db: Session, biz: Business, claim_id: int) -> LoyaltyClaim:
    """Staff handed over the reward: take the points and close the claim."""
    claim = db.get(LoyaltyClaim, claim_id)
    if claim is None or claim.business_id != biz.id:
        raise LoyaltyError("Claim not found.")
    problem = claim_problem(claim)
    if problem:
        raise LoyaltyError(problem)
    claim.status = "given"
    claim.given_at = datetime.utcnow()
    _add(db, claim.member, -claim.points, "claim", claim.reward_name)
    db.commit()
    return claim


# ------------------------------------------------------------------------
# Owner overview
# ------------------------------------------------------------------------

def top_members(db: Session, business_id: int, limit: int = 50) -> list[LoyaltyMember]:
    return (db.query(LoyaltyMember).filter(LoyaltyMember.business_id == business_id)
            .order_by(LoyaltyMember.points.desc(), LoyaltyMember.visits.desc()).limit(limit).all())


def recent_entries(db: Session, business_id: int, limit: int = 30) -> list[tuple[LoyaltyEntry, LoyaltyMember]]:
    return (db.query(LoyaltyEntry, LoyaltyMember)
            .join(LoyaltyMember, LoyaltyMember.id == LoyaltyEntry.member_id)
            .filter(LoyaltyEntry.business_id == business_id)
            .order_by(LoyaltyEntry.id.desc()).limit(limit).all())


def stats(db: Session, business_id: int) -> dict:
    from sqlalchemy import func
    since = datetime.utcnow() - timedelta(days=30)
    members = db.query(func.count(LoyaltyMember.id)).filter(LoyaltyMember.business_id == business_id).scalar() or 0
    visits = (db.query(func.count(LoyaltyEntry.id))
              .filter(LoyaltyEntry.business_id == business_id, LoyaltyEntry.kind.in_(("visit", "manual")),
                      LoyaltyEntry.created_at >= since).scalar() or 0)
    given = (db.query(func.count(LoyaltyClaim.id))
             .filter(LoyaltyClaim.business_id == business_id, LoyaltyClaim.status == "given",
                     LoyaltyClaim.given_at >= since).scalar() or 0)
    return {"members": members, "visits_30d": visits, "rewards_30d": given}


# ========================================================================
# Loyalty Plus
# ========================================================================

# ------------------------------------------------------------------------
# Tiers (Silver / Gold by number of visits)
# ------------------------------------------------------------------------

@dataclass
class Tier:
    key: str          # "" | silver | gold
    label: str        # "" | Silver | Gold
    bonus: int        # % extra on visit and bill points

    @property
    def emoji(self) -> str:
        return {"silver": "🥈", "gold": "🥇"}.get(self.key, "")


NO_TIER = Tier("", "", 0)


def tier_for(settings: LoyaltySettings | None, visits: int) -> Tier:
    if not settings or not settings.tiers_on:
        return NO_TIER
    if visits >= settings.gold_visits:
        return Tier("gold", "Gold", max(0, settings.gold_bonus))
    if visits >= settings.silver_visits:
        return Tier("silver", "Silver", max(0, settings.silver_bonus))
    return NO_TIER


def next_tier(settings: LoyaltySettings | None, visits: int) -> tuple[Tier, int] | None:
    """(the next tier, visits still needed), or None at the top / tiers off."""
    if not settings or not settings.tiers_on:
        return None
    if visits < settings.silver_visits:
        return Tier("silver", "Silver", settings.silver_bonus), settings.silver_visits - visits
    if visits < settings.gold_visits:
        return Tier("gold", "Gold", settings.gold_bonus), settings.gold_visits - visits
    return None


# ------------------------------------------------------------------------
# Double points
# ------------------------------------------------------------------------

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def boosts(db: Session, business_id: int, include_inactive: bool = False) -> list[LoyaltyBoost]:
    q = db.query(LoyaltyBoost).filter(LoyaltyBoost.business_id == business_id)
    if not include_inactive:
        q = q.filter(LoyaltyBoost.is_active == True)  # noqa: E712
    return q.order_by(LoyaltyBoost.id.asc()).all()


def boost_days(rule: LoyaltyBoost) -> list[int]:
    return sorted({int(x) for x in (rule.days or "").split(",") if x.strip().isdigit() and 0 <= int(x) <= 6})


def describe_boost(rule: LoyaltyBoost) -> str:
    days = boost_days(rule)
    when = ("Every day" if len(days) == 7 else
            "Weekdays" if days == [0, 1, 2, 3, 4] else
            "Weekends" if days == [5, 6] else ", ".join(DAY_NAMES[d] for d in days))
    if rule.start and rule.end:
        when += f", {_ampm(rule.start)}–{_ampm(rule.end)}"
    return f"{rule.multiplier}x points · {when}"


def _ampm(hhmm: str) -> str:
    try:
        return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0").replace(":00", "")
    except ValueError:
        return hhmm


def _boost_applies(rule: LoyaltyBoost, now: datetime) -> bool:
    if now.weekday() not in boost_days(rule):
        return False
    if rule.start and rule.end:
        return rule.start <= now.strftime("%H:%M") < rule.end
    return True


def active_boost(db: Session, biz: Business, now: datetime) -> int:
    """The biggest multiplier of the rules that apply right now (business-local time), else 1."""
    return max([r.multiplier for r in boosts(db, biz.id) if _boost_applies(r, now)] or [1])


def boosts_today(db: Session, biz: Business, now: datetime) -> list[LoyaltyBoost]:
    """Rules for today that haven't finished yet (for the wallet and replies)."""
    out = []
    for r in boosts(db, biz.id):
        if now.weekday() in boost_days(r) and not (r.end and now.strftime("%H:%M") >= r.end):
            out.append(r)
    return out


def add_boost(db: Session, biz: Business, days: list, start: str, end: str, multiplier) -> LoyaltyBoost:
    clean_days = sorted({int(d) for d in days if str(d).isdigit() and 0 <= int(d) <= 6})
    if not clean_days:
        raise LoyaltyError("Pick at least one day for double points.")
    start, end = (start or "").strip(), (end or "").strip()
    for t in (start, end):
        if t:
            try:
                datetime.strptime(t, "%H:%M")
            except ValueError:
                raise LoyaltyError("Times look like 15:00.")
    if bool(start) != bool(end):
        raise LoyaltyError("Set both a start and an end time, or leave both empty for the whole day.")
    if start and end <= start:
        raise LoyaltyError("The end time must be after the start time (same day).")
    try:
        mult = int(multiplier)
    except (TypeError, ValueError):
        mult = 2
    if mult not in (2, 3):
        raise LoyaltyError("Choose double (2x) or triple (3x) points.")
    if len(boosts(db, biz.id, include_inactive=True)) >= 10:
        raise LoyaltyError("You can have up to 10 double-points rules. Delete one first.")
    rule = LoyaltyBoost(business_id=biz.id, days=",".join(str(d) for d in clean_days),
                        start=start or None, end=end or None, multiplier=mult, is_active=True)
    db.add(rule)
    db.commit()
    return rule


# ------------------------------------------------------------------------
# Outlets (one brand, several counters, shared points)
# ------------------------------------------------------------------------

def outlets(db: Session, business_id: int, include_inactive: bool = False) -> list[LoyaltyOutlet]:
    q = db.query(LoyaltyOutlet).filter(LoyaltyOutlet.business_id == business_id)
    if not include_inactive:
        q = q.filter(LoyaltyOutlet.is_active == True)  # noqa: E712
    return q.order_by(LoyaltyOutlet.id.asc()).all()


def outlet_names(db: Session, settings: LoyaltySettings) -> dict:
    """{outlet_id or None: name}. None is the main counter."""
    names = {None: settings.main_outlet_name or "Main outlet"}
    for o in outlets(db, settings.business_id, include_inactive=True):
        names[o.id] = o.name
    return names


def _unused_earn_code(db: Session, settings: LoyaltySettings) -> str:
    for _ in range(50):
        code = new_earn_code()
        if code != settings.earn_code and not db.query(LoyaltyOutlet.id).filter(LoyaltyOutlet.earn_code == code).first():
            return code
    raise LoyaltyError("Couldn't make a new code. Please try again.")


def add_outlet(db: Session, biz: Business, settings: LoyaltySettings, name: str) -> LoyaltyOutlet:
    name = " ".join((name or "").split())[:60]
    if not name:
        raise LoyaltyError("Please give the outlet a name, e.g. Koregaon Park.")
    existing = outlets(db, biz.id, include_inactive=True)
    if len(existing) >= MAX_OUTLETS:
        raise LoyaltyError(f"You can have up to {MAX_OUTLETS} outlets.")
    if any(o.name.lower() == name.lower() for o in existing) or name.lower() == (settings.main_outlet_name or "main outlet").lower():
        raise LoyaltyError("There's already an outlet with that name.")
    outlet = LoyaltyOutlet(business_id=biz.id, name=name, earn_code=_unused_earn_code(db, settings), is_active=True)
    db.add(outlet)
    db.commit()
    return outlet


def outlet_qr_link(settings: LoyaltySettings, outlet: LoyaltyOutlet | None) -> str | None:
    number = normalize_phone(settings.whatsapp_number)
    if not is_real_phone(number):
        return None
    return f"https://wa.me/{number}?text=STAMP%20{outlet.earn_code if outlet else settings.earn_code}"


# ------------------------------------------------------------------------
# Bill points
# ------------------------------------------------------------------------

def _unique_bill_code(db: Session, business_id: int) -> str:
    now = datetime.utcnow()
    for _ in range(50):
        code = f"{random.SystemRandom().randint(0, 9999):04d}"
        clash = (db.query(LoyaltyBillCode.id)
                 .filter(LoyaltyBillCode.business_id == business_id, LoyaltyBillCode.code == code,
                         LoyaltyBillCode.status == "pending", LoyaltyBillCode.expires_at > now).first())
        if not clash:
            return code
    raise LoyaltyError("Too many bill codes right now. Please try again in a minute.")


def issue_bill_code(db: Session, biz: Business, settings: LoyaltySettings, member: LoyaltyMember,
                    outlet_id: int | None) -> LoyaltyBillCode:
    """One live bill code per customer: a new scan replaces the old one."""
    (db.query(LoyaltyBillCode)
     .filter(LoyaltyBillCode.member_id == member.id, LoyaltyBillCode.status == "pending")
     .update({LoyaltyBillCode.status: "replaced"}, synchronize_session=False))
    row = LoyaltyBillCode(business_id=biz.id, member_id=member.id, outlet_id=outlet_id,
                          code=_unique_bill_code(db, biz.id), status="pending",
                          expires_at=datetime.utcnow() + timedelta(minutes=BILL_CODE_MINUTES))
    db.add(row)
    db.flush()
    return row


def clean_bill(raw) -> int:
    text_ = str(raw or "").replace(",", "").replace("₹", "").strip()
    try:
        amount = int(round(float(text_)))
    except ValueError:
        raise LoyaltyError("Type the bill amount in rupees, e.g. 450.")
    if amount <= 0:
        raise LoyaltyError("The bill amount must be more than 0.")
    if amount > MAX_BILL:
        raise LoyaltyError(f"That bill looks too big (max ₹{MAX_BILL:,}). Check the amount.")
    return amount


def bill_points(db: Session, biz: Business, settings: LoyaltySettings, member: LoyaltyMember,
                amount: int, now: datetime | None = None) -> tuple[int, int]:
    """(points, boost) for a bill: points per full ₹100, with tier and double points."""
    from booking_engine import business_now
    boost = active_boost(db, biz, now or business_now(biz))
    base = (amount // 100) * max(0, settings.points_per_100)
    return _visit_points(settings, base, tier_for(settings, member.visits), boost), boost


def find_bill_code(db: Session, business_id: int, code: str) -> LoyaltyBillCode | None:
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 4:
        return None
    rows = (db.query(LoyaltyBillCode)
            .filter(LoyaltyBillCode.business_id == business_id, LoyaltyBillCode.code == code)
            .order_by(LoyaltyBillCode.id.desc()).limit(20).all())
    for row in rows:
        if row.status == "pending" and row.expires_at > datetime.utcnow():
            return row
    return rows[0] if rows else None


def add_bill(db: Session, biz: Business, code: str | None, amount_raw, phone: str | None = None,
             outlet_id: int | None = None, now: datetime | None = None) -> tuple[LoyaltyMember, int, int, list]:
    """Staff typed a bill: by bill code (from the customer's phone) or by phone number.
    Returns (member, points added, bill amount, newly unlocked rewards)."""
    settings = get_settings(db, biz.id)
    if not settings or not settings.enabled or not settings.bill_points_on:
        raise LoyaltyError("Points by bill amount aren't switched on. Turn them on in Grow.")
    amount = clean_bill(amount_raw)
    row = None
    if code:
        row = find_bill_code(db, biz.id, code)
        if row is None:
            raise LoyaltyError("No bill code like that. Check the 4 digits on the customer's phone.")
        if row.status == "used":
            raise LoyaltyError("This bill code was already used.")
        if row.status == "replaced":
            raise LoyaltyError("The customer scanned again after this. Ask for their newest bill code.")
        if row.expires_at <= datetime.utcnow():
            raise LoyaltyError("This bill code has expired. Ask the customer to scan the QR again.")
        member = row.member
        outlet_id = row.outlet_id if row.outlet_id is not None else outlet_id
    else:
        member, _ = get_or_create_member(db, biz, phone or "")
    points, boost = bill_points(db, biz, settings, member, amount, now)
    before = member.points
    note = f"Bill ₹{amount:,}" + (f" · {boost}x points" if boost > 1 else "")
    _add(db, member, points, "bill", note, outlet_id=outlet_id, bill_amount=amount, boost=boost if boost > 1 else None)
    if row is not None:
        row.status, row.used_at, row.bill_amount, row.points = "used", datetime.utcnow(), amount, points
    db.commit()
    return member, points, amount, _newly_unlocked(db, member, before)


# ------------------------------------------------------------------------
# Referrals
# ------------------------------------------------------------------------

def ref_code(db: Session, member: LoyaltyMember) -> str:
    """The member's personal invite code (made the first time it's needed)."""
    if member.ref_code:
        return member.ref_code
    for _ in range(50):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        if not db.query(LoyaltyMember.id).filter(LoyaltyMember.business_id == member.business_id,
                                                 LoyaltyMember.ref_code == code).first():
            member.ref_code = code
            db.commit()
            return code
    raise LoyaltyError("Couldn't make an invite code. Please try again.")


def invite_link(db: Session, settings: LoyaltySettings, member: LoyaltyMember) -> str | None:
    """wa.me link a friend taps: opens the business chat with 'JOIN <code>' typed in."""
    number = normalize_phone(settings.whatsapp_number)
    if not is_real_phone(number):
        return None
    return f"https://wa.me/{number}?text=JOIN%20{ref_code(db, member)}"


def register_referral(db: Session, biz: Business, friend_phone: str, code: str) -> tuple[str, LoyaltyMember | None]:
    """A friend sent 'JOIN <code>'. Returns (status, referrer):
    ok | already (invited before) | member (already a customer) | self | unknown | off."""
    settings = get_settings(db, biz.id)
    if not settings or not settings.referral_on:
        return "off", None
    referrer = (db.query(LoyaltyMember)
                .filter(LoyaltyMember.business_id == biz.id, LoyaltyMember.ref_code == (code or "").strip().upper())
                .first())
    if referrer is None:
        return "unknown", None
    phone = canonical_phone(friend_phone, biz.timezone)
    if phone == referrer.phone:
        return "self", referrer
    friend = find_member(db, biz.id, phone)
    if friend is not None and friend.visits > 0:
        return "member", referrer
    existing = (db.query(LoyaltyReferral)
                .filter(LoyaltyReferral.business_id == biz.id, LoyaltyReferral.friend_phone == phone).first())
    if existing:
        return "already", existing.referrer
    db.add(LoyaltyReferral(business_id=biz.id, referrer_id=referrer.id, friend_phone=phone, status="pending"))
    db.commit()
    return "ok", referrer


def _rewarded_this_month(db: Session, referrer_id: int) -> int:
    start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (db.query(LoyaltyReferral.id)
            .filter(LoyaltyReferral.referrer_id == referrer_id, LoyaltyReferral.status == "rewarded",
                    LoyaltyReferral.rewarded_at >= start).count())


def _apply_referral(db: Session, biz: Business, settings: LoyaltySettings, member: LoyaltyMember,
                    result: "EarnResult | None", outlet_id: int | None) -> None:
    """The friend's first visit: points for both (the inviter up to REFERRAL_MONTHLY_CAP a month)."""
    member._referral = None
    if not settings.referral_on or settings.referral_points <= 0:
        return
    row = (db.query(LoyaltyReferral)
           .filter(LoyaltyReferral.business_id == biz.id, LoyaltyReferral.friend_phone == member.phone,
                   LoyaltyReferral.status == "pending").first())
    if row is None or row.referrer_id == member.id:
        return
    bonus = settings.referral_points
    referrer = row.referrer
    first = (referrer.name or "your friend").split()[0]
    _add(db, member, bonus, "referral", f"Invited by {first}", outlet_id=outlet_id)
    row.friend_member_id, row.rewarded_at = member.id, datetime.utcnow()
    referrer_bonus = 0
    if _rewarded_this_month(db, referrer.id) < REFERRAL_MONTHLY_CAP:
        referrer_bonus = bonus
        friend_first = (member.name or "A friend").split()[0]
        _add(db, referrer, bonus, "referral", f"Invited {friend_first}", outlet_id=outlet_id)
        row.status = "rewarded"
    else:
        row.status = "over_cap"
    member._referral = (referrer, referrer_bonus)
    if result is not None:
        result.referral_bonus, result.referrer, result.referrer_bonus = bonus, referrer, referrer_bonus


# ------------------------------------------------------------------------
# Birthdays
# ------------------------------------------------------------------------

_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                           "nov", "dec"])}


def parse_birthday(text_: str) -> str | None:
    """'14 Aug', 'Aug 14', '14-08', '14/08/1995', '14.8' -> '08-14'. Day comes first for numbers (India)."""
    import re
    t = (text_ or "").strip().lower()
    if not t or len(t) > 40:
        return None
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    day = month = None
    m = re.fullmatch(r"(\d{1,2})\s*(?:of\s+)?([a-z]{3,9})\.?(?:,?\s*\d{2,4})?", t)
    if m and m.group(2)[:3] in _MONTHS:
        day, month = int(m.group(1)), _MONTHS[m.group(2)[:3]]
    m = m if day else re.fullmatch(r"([a-z]{3,9})\.?\s*(\d{1,2})(?:,?\s*\d{2,4})?", t)
    if not day and m and m.group(1)[:3] in _MONTHS:
        day, month = int(m.group(2)), _MONTHS[m.group(1)[:3]]
    if not day:
        m = re.fullmatch(r"(\d{1,2})\s*[-/. ]\s*(\d{1,2})(?:\s*[-/. ]\s*\d{2,4})?", t)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
    if not day:
        return None
    try:
        date(2000, month, day)          # 2000 is a leap year, so 29 Feb is allowed
    except ValueError:
        return None
    return f"{month:02d}-{day:02d}"


def birthday_label(mmdd: str | None) -> str:
    if not mmdd:
        return ""
    try:
        return date(2000, int(mmdd[:2]), int(mmdd[3:])).strftime("%d %B").lstrip("0")
    except ValueError:
        return ""


def set_birthday(db: Session, member: LoyaltyMember, mmdd: str, overwrite: bool = False) -> bool:
    """Save once. Customers can't change it later (so it can't be moved for extra gifts);
    the owner can (overwrite=True)."""
    if member.birthday and not overwrite:
        return False
    member.birthday = mmdd
    member.birthday_asked = True
    db.commit()
    return True


def birthday_today(member: LoyaltyMember, today: date) -> bool:
    if not member.birthday:
        return False
    if member.birthday == today.strftime("%m-%d"):
        return True
    # 29 Feb birthdays are celebrated on 28 Feb in other years.
    import calendar
    return member.birthday == "02-29" and today.strftime("%m-%d") == "02-28" and not calendar.isleap(today.year)


def give_birthday(db: Session, member: LoyaltyMember, points: int) -> int:
    before = member.points
    _add(db, member, points, "birthday", "Birthday gift")
    db.commit()
    return before


def marketing_sent_this_month(db: Session, business_id: int, today: date) -> int:
    start = datetime(today.year, today.month, 1)
    return (db.query(LoyaltyEntry.id)
            .filter(LoyaltyEntry.business_id == business_id, LoyaltyEntry.kind == "birthday",
                    LoyaltyEntry.created_at >= start).count())


def stop_offers(db: Session, member: LoyaltyMember, off: bool = True) -> None:
    member.offers_off = off
    db.commit()


# ------------------------------------------------------------------------
# Counter staff
# ------------------------------------------------------------------------

def staff_outlet(db: Session, owner_id: int | None) -> int | None:
    row = db.get(LoyaltyStaff, owner_id) if owner_id else None
    return row.outlet_id if row else None


def staff_list(db: Session, biz: Business) -> list[tuple]:
    """[(owner row, outlet id or None)] for every staff login of the business."""
    from tables.engine import staff_logins
    out = []
    for s in staff_logins(db, biz):
        row = db.get(LoyaltyStaff, s.id)
        out.append((s, row.outlet_id if row else None))
    return out


def create_staff(db: Session, biz: Business, name: str, username: str, password: str,
                 outlet_id: int | None) -> object:
    from tables.engine import TablesError, create_staff as _create
    if outlet_id is not None:
        o = db.get(LoyaltyOutlet, outlet_id)
        if o is None or o.business_id != biz.id:
            raise LoyaltyError("Outlet not found.")
    try:
        staff = _create(db, biz, name, username, password)
    except TablesError as err:
        raise LoyaltyError(err.message)
    db.add(LoyaltyStaff(owner_id=staff.id, business_id=biz.id, outlet_id=outlet_id))
    db.commit()
    return staff


def remove_staff(db: Session, biz: Business, staff_id: int):
    from tables.engine import TablesError, remove_staff as _remove
    row = db.get(LoyaltyStaff, staff_id)
    try:
        staff = _remove(db, biz, staff_id)
    except TablesError as err:
        raise LoyaltyError(err.message)
    if row is not None:
        db.delete(row)
        db.commit()
    return staff


# ------------------------------------------------------------------------
# Results
# ------------------------------------------------------------------------

VISIT_KINDS = ("visit", "manual")


def results(db: Session, biz: Business, settings: LoyaltySettings, days: int = 30,
            now: datetime | None = None) -> dict:
    """Numbers that show the owner what Verzio Loyalty did for them in the last `days` days.

    A *repeat visit* is any visit by someone who had visited before. The extra-revenue
    estimate is repeat visits x average bill (typed bills if there are any, else the
    average the owner set)."""
    from sqlalchemy import func
    now = now or datetime.utcnow()
    since, prev_since = now - timedelta(days=days), now - timedelta(days=2 * days)
    first_visit = dict(db.query(LoyaltyEntry.member_id, func.min(LoyaltyEntry.id))
                       .filter(LoyaltyEntry.business_id == biz.id, LoyaltyEntry.kind.in_(VISIT_KINDS))
                       .group_by(LoyaltyEntry.member_id).all())
    rows = (db.query(LoyaltyEntry)
            .filter(LoyaltyEntry.business_id == biz.id, LoyaltyEntry.created_at >= prev_since).all())
    names = outlet_names(db, settings)

    def window(start, end):
        visits = [e for e in rows if e.kind in VISIT_KINDS and start <= e.created_at < end]
        repeat = [e for e in visits if first_visit.get(e.member_id) != e.id]
        bills = [e for e in rows if e.kind == "bill" and start <= e.created_at < end and e.bill_amount]
        return visits, repeat, bills

    visits, repeat, bills = window(since, now + timedelta(seconds=1))
    p_visits, p_repeat, _ = window(prev_since, since)
    bill_total = sum(e.bill_amount for e in bills)
    avg_bill = int(bill_total / len(bills)) if bills else (settings.avg_bill or 0)
    members_all = db.query(func.count(LoyaltyMember.id)).filter(LoyaltyMember.business_id == biz.id).scalar() or 0
    with_visit = (db.query(func.count(LoyaltyMember.id))
                  .filter(LoyaltyMember.business_id == biz.id, LoyaltyMember.visits >= 1).scalar() or 0)
    regulars = (db.query(func.count(LoyaltyMember.id))
                .filter(LoyaltyMember.business_id == biz.id, LoyaltyMember.visits >= 2).scalar() or 0)
    new_members = len({e.member_id for e in visits if first_visit.get(e.member_id) == e.id})
    given = (db.query(func.count(LoyaltyClaim.id))
             .filter(LoyaltyClaim.business_id == biz.id, LoyaltyClaim.status == "given",
                     LoyaltyClaim.given_at >= since).scalar() or 0)
    referrals = (db.query(func.count(LoyaltyReferral.id))
                 .filter(LoyaltyReferral.business_id == biz.id, LoyaltyReferral.rewarded_at.isnot(None),
                         LoyaltyReferral.rewarded_at >= since).scalar() or 0)
    birthdays = len([e for e in rows if e.kind == "birthday" and e.created_at >= since])
    boosted = len([e for e in visits if e.boost])

    by_outlet = {}
    if len(names) > 1:
        for key, label in names.items():
            v = [e for e in visits if e.outlet_id == key]
            b = [e for e in bills if e.outlet_id == key]
            by_outlet[label] = {"visits": len(v), "repeat": len([e for e in v if first_visit.get(e.member_id) != e.id]),
                                "new": len({e.member_id for e in v if first_visit.get(e.member_id) == e.id}),
                                "bills": sum(e.bill_amount for e in b)}

    # Visits per day for the last 14 days (business-local dates are close enough for a chart).
    daily = []
    for i in range(13, -1, -1):
        day = (now - timedelta(days=i)).date()
        n = len([e for e in rows if e.kind in VISIT_KINDS and e.created_at.date() == day])
        daily.append((day, n))
    peak = max([n for _, n in daily] or [0]) or 1

    def change(cur, prev):
        if prev == 0:
            return None
        return int(round((cur - prev) * 100 / prev))

    return {
        "days": days, "members": members_all, "new_members": new_members,
        "visits": len(visits), "visits_change": change(len(visits), len(p_visits)),
        "repeat": len(repeat), "repeat_change": change(len(repeat), len(p_repeat)),
        "repeat_share": int(round(len(repeat) * 100 / len(visits))) if visits else 0,
        "regular_rate": int(round(regulars * 100 / with_visit)) if with_visit else 0, "regulars": regulars,
        "rewards_given": given, "referrals": referrals, "birthdays": birthdays, "boosted": boosted,
        "bill_count": len(bills), "bill_total": bill_total, "avg_bill": avg_bill,
        "avg_from_bills": bool(bills),
        "extra_revenue": len(repeat) * avg_bill if avg_bill else None,
        "by_outlet": by_outlet, "daily": daily, "peak": peak,
    }
