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
"""
import logging
import random
import secrets
import string
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from database import Business
from loyalty.models import LoyaltyClaim, LoyaltyEntry, LoyaltyMember, LoyaltyReward, LoyaltySettings
from whatsapp_client import canonical_phone, is_real_phone, normalize_phone

logger = logging.getLogger("VERZIO_LOYALTY")

CLAIM_MINUTES = 15
MAX_POINTS = 100000          # sanity cap for any single number the owner types
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


def running(db: Session, biz: Business) -> bool:
    """Loyalty works for customers only while the business and its plan are active."""
    if not biz.is_active:
        return False
    try:
        from plans import plan_for
        return plan_for(biz, db).active
    except Exception:
        logger.exception("Could not read plan for business_id=%s", biz.id)
        return True


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
                           points=0, visits=0, wallet_token=secrets.token_urlsafe(18))
    db.add(member)
    db.flush()
    return member, True


def _add(db: Session, member: LoyaltyMember, delta: int, kind: str, note: str | None = None) -> LoyaltyEntry:
    member.points = max(0, member.points + delta)
    entry = LoyaltyEntry(business_id=member.business_id, member_id=member.id, delta=delta,
                         kind=kind, note=(note or "")[:120] or None, balance_after=member.points)
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


def earn_visit(db: Session, biz: Business, phone: str, code: str, name: str | None = None,
               today: date | None = None) -> EarnResult:
    """Customer sent the counter QR's code. Raises LoyaltyError for a wrong code."""
    settings = get_settings(db, biz.id)
    if not settings or not settings.enabled:
        raise LoyaltyError("Rewards aren't switched on here.")
    if (code or "").strip().upper() != settings.earn_code.upper():
        raise LoyaltyError("wrong_code")

    from booking_engine import business_today
    today = today or business_today(biz)
    member, created = get_or_create_member(db, biz, phone, name)
    if member.last_visit_date == today.isoformat():
        db.commit()
        return EarnResult(member=member, earned=0, already_today=True, newly_unlocked=[],
                          streak=streak_info(biz, settings, member, today))

    before = member.points
    welcome = 0
    if created and settings.welcome_bonus > 0:
        welcome = settings.welcome_bonus
        _add(db, member, welcome, "welcome", "Welcome bonus")
    _add(db, member, settings.points_per_visit, "visit", "Visit (QR)")
    daily_bonus, weekly_bonus = _record_visit(db, biz, settings, member, today)
    db.commit()
    return EarnResult(member=member, earned=member.points - before, welcome=welcome,
                      newly_unlocked=_newly_unlocked(db, member, before),
                      daily_bonus=daily_bonus, weekly_bonus=weekly_bonus,
                      streak=streak_info(biz, settings, member, today))


def owner_adjust(db: Session, biz: Business, phone: str, points, note: str | None,
                 name: str | None = None, as_visit: bool = False,
                 today: date | None = None) -> tuple[LoyaltyMember, int, list]:
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
        _add(db, member, delta, "manual", note or "Visit (added by staff)")
        from booking_engine import business_today
        today = today or business_today(biz)
        settings = get_settings(db, biz.id)
        if settings and member.last_visit_date != today.isoformat():
            _record_visit(db, biz, settings, member, today)
        else:
            member.visits += 1
    else:
        _add(db, member, delta, "adjust", note or ("Added by staff" if delta > 0 else "Removed by staff"))
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
