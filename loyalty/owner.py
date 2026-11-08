"""
loyalty/owner.py: the Verzio Loyalty dashboard for owners (/owner/loyalty).

Its own layout and menu, separate from the Verzio Book portal:
  /owner/loyalty            Dashboard: give a reward, codes waiting at the counter,
                            add points, today's numbers, recent activity
  /owner/loyalty/rewards    the reward catalog
  /owner/loyalty/customers  regulars, points, visits, streaks (searchable)
  /owner/loyalty/settings   business name, WhatsApp number, points, streaks, counter QR,
                            outlets and counter-staff logins
  /owner/loyalty/grow       Loyalty Plus: tiers, double points, bill points, birthdays,
                            invites, Google review ask, weekly summary
  /owner/loyalty/results    what loyalty did this month (repeat visits, estimated revenue)

Counter staff (owners.role = 'staff') see only the dashboard's counter tools:
give a reward, bill points, add a visit. Phone numbers are masked for them.

A "loyalty only" business (a cafe without bookings) never sees Verzio Book pages:
loyalty_only_guard sends any booking page to the Loyalty dashboard. A business
with both products moves between them from each menu.

Switching loyalty on/off and choosing "loyalty only" is done by Verzio in
Admin -> Loyalty, not by the owner.
"""
import logging
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from activity_log import log_event
from database import get_db
from loyalty import engine as L
from loyalty.bot import local_clock, wallet_url
from loyalty.models import LoyaltyReward
from owner.owner_auth import is_mobile, resolve_owner_and_business
from whatsapp_client import canonical_phone, is_real_phone, mask_phone, send_whatsapp, window_open

logger = logging.getLogger("VERZIO_LOYALTY_OWNER")
router = APIRouter(prefix="/owner/loyalty", tags=["owner-loyalty"])
templates = Jinja2Templates(directory="templates")

ENTRY_LABELS = {"visit": "Visit (QR)", "welcome": "Welcome bonus", "manual": "Visit (staff)",
                "adjust": "Adjustment", "claim": "Reward given",
                "streak_daily": "Daily streak bonus", "streak_weekly": "Weekly streak bonus",
                "bill": "Bill points", "referral": "Invite bonus", "birthday": "Birthday gift"}
NOTE_KINDS = ("claim", "streak_daily", "streak_weekly", "bill", "referral", "visit")


PAGES = {"": "Dashboard", "rewards": "Rewards", "customers": "Customers", "grow": "Grow",
         "results": "Results", "settings": "Settings"}


def _back(ok: str | None = None, err: str | None = None, code: str | None = None, anchor: str = "",
          page: str = "") -> RedirectResponse:
    params = []
    if ok:
        params.append("ok=" + quote_plus(ok[:160]))
    if err:
        params.append("err=" + quote_plus(err[:160]))
    if code:
        params.append("code=" + quote_plus(code[:8]))
    url = "/owner/loyalty" + (f"/{page}" if page else "") + ("?" + "&".join(params) if params else "") + (f"#{anchor}" if anchor else "")
    return RedirectResponse(url=url, status_code=303)


def _auth(request: Request, db: Session):
    """(owner, business, settings, redirect). settings is None if loyalty is off.
    Counter staff get through too: use _auth_owner for owner-only actions."""
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        return None, None, None, RedirectResponse(url="/owner/login", status_code=303)
    settings = L.get_settings(db, business.id)
    return owner, business, (settings if settings and settings.enabled else None), None


def _is_staff(db: Session, owner) -> bool:
    from tables.engine import owner_role
    return owner is not None and owner_role(db, owner.id) == "staff"


def _auth_owner(request: Request, db: Session):
    """Like _auth, but counter staff are sent back to the dashboard."""
    owner, business, settings, redirect = _auth(request, db)
    if redirect is None and _is_staff(db, owner):
        return owner, business, settings, _back(err="Only the owner can do that.")
    return owner, business, settings, redirect


async def _tell_customer(business, phone: str, text: str) -> None:
    """Message the customer only if they messaged in the last 24 h (free and allowed)."""
    if is_real_phone(phone) and window_open(business.id, phone):
        await send_whatsapp(phone, business.whatsapp_business_phone_number_id,
                            {"type": "text", "body": text}, business.id)


# ------------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------------

def _tables_on(db: Session, business) -> bool:
    from tables.engine import is_enabled
    return is_enabled(db, business)


def _render(request: Request, db: Session, page: str, template: str, extra=None):
    owner, business, settings, redirect = _auth(request, db) if page == "" else _auth_owner(request, db)
    if redirect:
        return redirect
    staff = _is_staff(db, owner)
    staff_outlet = L.staff_outlet(db, owner.id) if staff else None
    context = {
        "is_staff": staff,
        "staff_outlet_name": (L.outlet_names(db, settings).get(staff_outlet) if staff and settings and staff_outlet else None),
        "note_kinds": NOTE_KINDS,
        "layout": "loyalty/base_mobile.html" if is_mobile(request) else "loyalty/base.html",
        "active_page": page or "dashboard", "page_title": PAGES[page],
        "owner": owner, "business": business, "settings": settings,
        "has_book": not staff and not L.is_loyalty_only(db, business) and not _tables_on(db, business),
        "has_tables": _tables_on(db, business),
        "running": L.running(db, business),
        "plan": L.plan_info(settings) if settings is not None else None,
        "ok": (request.query_params.get("ok") or "")[:160] or None,
        "err": (request.query_params.get("err") or "")[:160] or None,
        "labels": ENTRY_LABELS,
        "clock": lambda dt: local_clock(business, dt),
    }
    if settings is not None and extra:
        context.update(extra(owner, business, settings))
    return templates.TemplateResponse(request=request, name="loyalty/off.html" if settings is None else template,
                                      context=context)


@router.get("")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    def extra(owner, business, settings):
        code = (request.query_params.get("code") or "").strip()
        found, problem = None, None
        if code:
            found = L.find_claim_by_code(db, business.id, code)
            problem = L.claim_problem(found) if found else "No claim with that code. Check the 4 digits on the customer's phone."
        visits = None
        if found:
            visits = found.member.visits
        from booking_engine import business_now
        return {"code": code, "found": found, "problem": problem, "found_visits": visits,
                "pending": L.pending_claims(db, business.id),
                "stats": L.stats(db, business.id),
                "recent": L.recent_entries(db, business.id, 12),
                "reward_count": len(L.rewards(db, business.id)),
                "qr_link": L.qr_link(settings),
                "boosts_now": L.boosts_today(db, business, business_now(business)),
                "boost_now": L.active_boost(db, business, business_now(business)),
                "describe_boost": L.describe_boost,
                "mask": mask_phone}
    return _render(request, db, "", "loyalty/dashboard.html", extra)


@router.get("/rewards")
async def rewards_page(request: Request, db: Session = Depends(get_db)):
    return _render(request, db, "rewards", "loyalty/rewards.html",
                   lambda o, b, s: {"rewards": L.rewards(db, b.id, include_inactive=True)})


@router.get("/customers")
async def customers_page(request: Request, db: Session = Depends(get_db)):
    def extra(owner, business, settings):
        q = (request.query_params.get("q") or "").strip()[:40]
        members = L.top_members(db, business.id, 300)
        if q:
            ql, digits = q.lower(), "".join(ch for ch in q if ch.isdigit())
            members = [m for m in members if ql in (m.name or "").lower() or (digits and digits in m.phone)]
        streaks = {m.id: L.streak_info(business, settings, m) for m in members}
        tiers = {m.id: L.tier_for(settings, m.visits) for m in members}
        return {"members": members, "q": q, "streaks": streaks, "stats": L.stats(db, business.id),
                "tiers": tiers, "birthday_label": L.birthday_label}
    return _render(request, db, "customers", "loyalty/customers.html", extra)


@router.get("/settings")
async def settings_page(request: Request, db: Session = Depends(get_db)):
    return _render(request, db, "settings", "loyalty/settings.html",
                   lambda o, b, s: {"qr_link": L.qr_link(s), "outlets": L.outlets(db, b.id, include_inactive=True),
                                    "outlet_names": L.outlet_names(db, s), "staff": L.staff_list(db, b)})


@router.get("/grow")
async def grow_page(request: Request, db: Session = Depends(get_db)):
    def extra(owner, business, settings):
        return {"boosts": L.boosts(db, business.id, include_inactive=True), "describe_boost": L.describe_boost,
                "day_names": L.DAY_NAMES,
                "marketing_used": L.marketing_sent_this_month(db, business.id, __import__("datetime").date.today())}
    return _render(request, db, "grow", "loyalty/grow.html", extra)


@router.get("/results")
async def results_page(request: Request, db: Session = Depends(get_db)):
    def extra(owner, business, settings):
        try:
            days = int(request.query_params.get("days") or 30)
        except ValueError:
            days = 30
        days = days if days in (7, 30, 90) else 30
        return {"r": L.results(db, business, settings, days)}
    return _render(request, db, "results", "loyalty/results.html", extra)


# ------------------------------------------------------------------------
# Claims
# ------------------------------------------------------------------------

@router.post("/lookup")
async def lookup(request: Request, db: Session = Depends(get_db), code: str = Form("")):
    _, business, settings, redirect = _auth(request, db)
    if redirect:
        return redirect
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) != 4:
        return _back(err="Claim codes have 4 digits.", anchor="give")
    return _back(code=digits, anchor="give")


@router.post("/claims/{claim_id}/give")
async def give(claim_id: int, request: Request, db: Session = Depends(get_db)):
    _, business, settings, redirect = _auth(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    try:
        claim = L.give_reward(db, business, claim_id)
    except L.LoyaltyError as err:
        db.rollback()
        return _back(err=err.message, anchor="give")
    member = claim.member
    log_event(db=db, event_type="loyalty_reward_given", status="success",
              message=f"Gave {claim.reward_name} to {mask_phone(member.phone)} (-{claim.points} points, "
                      f"balance {member.points})", business_id=business.id)
    await _tell_customer(business, member.phone,
                         f"🎉 Enjoy your {claim.reward_name}!\n\nWallet: {member.points} points left at "
                         f"{business.name}.\n\nSee your progress: {wallet_url(member)}")
    who = member.name or mask_phone(member.phone)
    return _back(ok=f"{claim.reward_name} given to {who}. {claim.points} points used, {member.points} left.",
                 anchor="give")


# ------------------------------------------------------------------------
# Points
# ------------------------------------------------------------------------

@router.post("/points")
async def add_points(
    request: Request,
    db: Session = Depends(get_db),
    phone: str = Form(""),
    name: str = Form(""),
    points: str = Form(""),
    kind: str = Form("visit"),
    note: str = Form(""),
):
    owner, business, settings, redirect = _auth(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    staff = _is_staff(db, owner)
    if staff and kind != "visit":
        return _back(err="Only the owner can add or remove points. You can add a visit.", anchor="points")
    outlet_id = L.staff_outlet(db, owner.id) if staff else None
    raw = (points or "").strip() or str(settings.points_per_visit)
    try:
        # visit: a visit without the QR (counts for streaks) · add / remove: corrections.
        # Staff always type a positive number; "remove" takes it away. ("adjust" = older form: signed number.)
        if kind == "visit":
            value, as_visit = raw, True
        elif kind == "add":
            value, as_visit = L.clean_points(raw), False
        elif kind == "remove":
            value, as_visit = -L.clean_points(raw.lstrip("-")), False
        else:
            value, as_visit = raw, False
        member, delta, unlocked = L.owner_adjust(db, business, phone, value,
                                                 note.strip() or None, name=name.strip() or None,
                                                 as_visit=as_visit, outlet_id=outlet_id)
    except L.LoyaltyError as err:
        db.rollback()
        return _back(err=err.message, anchor="points")
    log_event(db=db, event_type="loyalty_adjusted", status="info",
              message=f"Staff {'added' if delta > 0 else 'removed'} {abs(delta)} points for {mask_phone(member.phone)} "
                      f"(balance {member.points})", business_id=business.id)
    if delta > 0:
        extra = "".join(f"\n\n🎉 You unlocked {r.name}! Reply CLAIM when you'd like it." for r in unlocked)
        await _tell_customer(business, member.phone,
                             f"☕ +{delta} points at {business.name}!\n\nWallet: {member.points} points.{extra}")
    referral = getattr(member, "_referral", None)
    if referral and referral[1]:
        from loyalty.bot import tell_referrer
        await tell_referrer(db, business, referral[0], member, referral[1])
    who = member.name or (mask_phone(member.phone) if staff else f"+{member.phone}")
    verb = f"Added {delta}" if delta > 0 else f"Removed {abs(delta)}"
    return _back(ok=f"{verb} points for {who}. Balance: {member.points}.", anchor="points")


# ------------------------------------------------------------------------
# Rewards
# ------------------------------------------------------------------------

def _clean_name(raw: str) -> str:
    name = " ".join((raw or "").split())[:60]
    if not name:
        raise L.LoyaltyError("Please give the reward a name.")
    return name


def _own_reward(db: Session, business, reward_id: int) -> LoyaltyReward | None:
    reward = db.get(LoyaltyReward, reward_id)
    if reward is None or reward.business_id != business.id or reward.is_deleted:
        return None
    return reward


@router.post("/rewards")
async def add_reward(request: Request, db: Session = Depends(get_db), name: str = Form(""), points: str = Form("")):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    try:
        reward = LoyaltyReward(business_id=business.id, name=_clean_name(name), points=L.clean_points(points))
    except L.LoyaltyError as err:
        return _back(err=err.message, page="rewards")
    if len(L.rewards(db, business.id, include_inactive=True)) >= 20:
        return _back(err="You can have up to 20 rewards. Delete one first.", page="rewards")
    db.add(reward)
    db.commit()
    return _back(ok=f"Added {reward.name} ({reward.points} points).", page="rewards")


@router.post("/rewards/{reward_id}")
async def edit_reward(reward_id: int, request: Request, db: Session = Depends(get_db),
                      name: str = Form(""), points: str = Form("")):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    reward = _own_reward(db, business, reward_id) if settings else None
    if reward is None:
        return _back(err="Reward not found.", page="rewards")
    try:
        reward.name, reward.points = _clean_name(name), L.clean_points(points)
    except L.LoyaltyError as err:
        db.rollback()
        return _back(err=err.message, page="rewards")
    db.commit()
    return _back(ok=f"Saved {reward.name}.", page="rewards")


@router.post("/rewards/{reward_id}/toggle")
async def toggle_reward(reward_id: int, request: Request, db: Session = Depends(get_db)):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    reward = _own_reward(db, business, reward_id) if settings else None
    if reward is None:
        return _back(err="Reward not found.", page="rewards")
    reward.is_active = not reward.is_active
    db.commit()
    return _back(ok=f"{reward.name} is now {'shown to customers' if reward.is_active else 'hidden'}.", page="rewards")


@router.post("/rewards/{reward_id}/delete")
async def delete_reward(reward_id: int, request: Request, db: Session = Depends(get_db)):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    reward = _own_reward(db, business, reward_id) if settings else None
    if reward is None:
        return _back(err="Reward not found.", page="rewards")
    reward.is_deleted = True
    reward.is_active = False
    db.commit()
    return _back(ok=f"Deleted {reward.name}.", page="rewards")


# ------------------------------------------------------------------------
# Settings + QR
# ------------------------------------------------------------------------

@router.post("/settings")
async def save_settings(
    request: Request,
    db: Session = Depends(get_db),
    business_name: str = Form(""),
    whatsapp_number: str = Form(""),
    points_per_visit: str = Form(""),
    welcome_bonus: str = Form("0"),
    daily_streak_on: str = Form(""),
    daily_streak_step: str = Form("5"),
    daily_streak_max: str = Form("20"),
    weekly_streak_on: str = Form(""),
    weekly_streak_step: str = Form("10"),
    weekly_streak_max: str = Form("30"),
):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    try:
        per_visit = L.clean_points(points_per_visit)
        welcome = L.clean_points(welcome_bonus or "0", allow_zero=True)
        d_step = L.clean_points(daily_streak_step or "5")
        d_max = L.clean_points(daily_streak_max or "20")
        w_step = L.clean_points(weekly_streak_step or "10")
        w_max = L.clean_points(weekly_streak_max or "30")
    except L.LoyaltyError as err:
        return _back(err=err.message, page="settings")
    if d_max < d_step or w_max < w_step:
        return _back(err="The biggest streak bonus can't be smaller than the bonus per day or week.", page="settings")
    name = " ".join(business_name.split())[:80] or business.name     # left empty: keep the current name
    number = canonical_phone(whatsapp_number, business.timezone) if whatsapp_number.strip() else ""
    if number and not is_real_phone(number):
        return _back(err="That WhatsApp number doesn't look right. Use the full number, e.g. 98765 43210.",
                     page="settings")
    business.name = name
    settings.whatsapp_number = number or None
    settings.points_per_visit, settings.welcome_bonus = per_visit, welcome
    settings.daily_streak_on = daily_streak_on in ("on", "true", "1")
    settings.daily_streak_step, settings.daily_streak_max = d_step, d_max
    settings.weekly_streak_on = weekly_streak_on in ("on", "true", "1")
    settings.weekly_streak_step, settings.weekly_streak_max = w_step, w_max
    db.commit()
    return _back(ok="Settings saved.", page="settings")


@router.post("/rotate-code")
async def rotate_code(request: Request, db: Session = Depends(get_db)):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    settings.earn_code = L.new_earn_code()
    db.commit()
    log_event(db=db, event_type="loyalty_code_changed", status="warning",
              message="Counter QR code changed; old printed QR codes no longer work", business_id=business.id)
    return _back(ok="New QR code made. Print it and replace the old one: the old QR no longer gives points.",
                 page="settings", anchor="qr")


@router.get("/qr")
async def qr_poster(request: Request, db: Session = Depends(get_db)):
    owner, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    outlet = None
    oid = request.query_params.get("outlet")
    if oid and oid.isdigit():
        outlet = next((o for o in L.outlets(db, business.id) if o.id == int(oid)), None)
        if outlet is None:
            return _back(err="Outlet not found.", page="settings", anchor="outlets")
    link = L.outlet_qr_link(settings, outlet)
    qr_svg = None
    if link:
        try:
            import segno                       # pure Python, in requirements.txt
            qr_svg = segno.make(link, error="m").svg_inline(border=3, dark="#2b1d14", light="#ffffff", omitsize=True)
        except ImportError:
            logger.error("segno is not installed: run  pip install -r requirements.txt")
    return templates.TemplateResponse(
        request=request, name="loyalty/qr.html",
        context={"business": business, "settings": settings, "qr_link": link, "qr_svg": qr_svg,
                 "rewards": L.rewards(db, business.id)[:3], "outlet": outlet},
    )


# ------------------------------------------------------------------------
# Loyalty Plus: bill points (staff too)
# ------------------------------------------------------------------------

@router.post("/bill")
async def add_bill(request: Request, db: Session = Depends(get_db), code: str = Form(""),
                   phone: str = Form(""), amount: str = Form("")):
    owner, business, settings, redirect = _auth(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    staff = _is_staff(db, owner)
    digits = "".join(ch for ch in code if ch.isdigit())
    if not digits and not phone.strip():
        return _back(err="Type the customer's 4-digit bill code (or their WhatsApp number).", anchor="bill")
    try:
        member, points, amount_, unlocked = L.add_bill(
            db, business, digits or None, amount, phone=None if digits else phone,
            outlet_id=L.staff_outlet(db, owner.id) if staff else None)
    except L.LoyaltyError as err:
        db.rollback()
        return _back(err=err.message, anchor="bill")
    log_event(db=db, event_type="loyalty_bill", status="info",
              message=f"Bill ₹{amount_:,} for {mask_phone(member.phone)}: +{points} points (balance {member.points})",
              business_id=business.id)
    if points > 0:
        extra = "".join(f"\n\n🎉 You unlocked {r.name}! Reply CLAIM when you'd like it." for r in unlocked)
        await _tell_customer(business, member.phone,
                             f"🧾 +{points} points for your ₹{amount_:,} bill at {business.name}!\n\n"
                             f"Wallet: {member.points} points.{extra}")
    who = member.name or mask_phone(member.phone)
    if points == 0:
        return _back(ok=f"Bill ₹{amount_:,} saved for {who}. Bills under ₹100 earn no points.", anchor="bill")
    return _back(ok=f"+{points} points for {who} (bill ₹{amount_:,}). Balance: {member.points}.", anchor="bill")


# ------------------------------------------------------------------------
# Loyalty Plus: Grow settings (owner only)
# ------------------------------------------------------------------------

def _on(v: str) -> bool:
    return v in ("on", "true", "1")


@router.post("/grow")
async def save_grow(
    request: Request, db: Session = Depends(get_db),
    tiers_on: str = Form(""), silver_visits: str = Form("5"), silver_bonus: str = Form("25"),
    gold_visits: str = Form("15"), gold_bonus: str = Form("50"),
    bill_points_on: str = Form(""), points_per_100: str = Form("5"), avg_bill: str = Form("0"),
    birthday_on: str = Form(""), birthday_points: str = Form("50"),
    referral_on: str = Form(""), referral_points: str = Form("30"),
    review_on: str = Form(""), review_link: str = Form(""), review_after: str = Form("3"),
    summary_on: str = Form(""),
):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    try:
        sv, gv = L.clean_points(silver_visits or "5"), L.clean_points(gold_visits or "15")
        sb = L.clean_points(silver_bonus or "0", allow_zero=True)
        gb = L.clean_points(gold_bonus or "0", allow_zero=True)
        p100 = L.clean_points(points_per_100 or "0", allow_zero=True)
        avg = L.clean_points(avg_bill or "0", allow_zero=True)
        bday = L.clean_points(birthday_points or "0", allow_zero=True)
        ref = L.clean_points(referral_points or "0", allow_zero=True)
        rafter = L.clean_points(review_after or "3")
    except L.LoyaltyError as err:
        return _back(err=err.message, page="grow")
    if gv <= sv:
        return _back(err="Gold needs more visits than Silver.", page="grow", anchor="tiers")
    if sb > 300 or gb > 300:
        return _back(err="Tier bonus is a percentage: keep it at 300% or less.", page="grow", anchor="tiers")
    link = (review_link or "").strip()[:300]
    if link and not link.startswith(("https://", "http://")):
        return _back(err="The review link should start with https://", page="grow", anchor="review")
    if _on(review_on) and not link:
        return _back(err="Paste your Google review link to switch on the review ask.", page="grow", anchor="review")
    if _on(bill_points_on) and p100 == 0:
        return _back(err="Set how many points each ₹100 of bill gives.", page="grow", anchor="bill")
    settings.tiers_on, settings.silver_visits, settings.silver_bonus = _on(tiers_on), sv, sb
    settings.gold_visits, settings.gold_bonus = gv, gb
    settings.bill_points_on, settings.points_per_100, settings.avg_bill = _on(bill_points_on), p100, avg
    settings.birthday_on, settings.birthday_points = _on(birthday_on), bday
    settings.referral_on, settings.referral_points = _on(referral_on), ref
    settings.review_on, settings.review_link, settings.review_after = _on(review_on), link or None, rafter
    settings.summary_on = _on(summary_on)
    db.commit()
    log_event(db=db, event_type="loyalty_grow_saved", status="info", message="Loyalty Plus settings saved",
              business_id=business.id)
    return _back(ok="Saved.", page="grow")


@router.post("/boosts")
async def add_boost(request: Request, db: Session = Depends(get_db), start: str = Form(""),
                    end: str = Form(""), multiplier: str = Form("2")):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    form = await request.form()
    try:
        rule = L.add_boost(db, business, form.getlist("days"), start, end, multiplier)
    except L.LoyaltyError as err:
        db.rollback()
        return _back(err=err.message, page="grow", anchor="boosts")
    return _back(ok=f"Added: {L.describe_boost(rule)}.", page="grow", anchor="boosts")


def _own(db, model, business, row_id):
    row = db.get(model, row_id)
    return row if row is not None and row.business_id == business.id else None


@router.post("/boosts/{rule_id}/toggle")
async def toggle_boost(rule_id: int, request: Request, db: Session = Depends(get_db)):
    from loyalty.models import LoyaltyBoost
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    rule = _own(db, LoyaltyBoost, business, rule_id) if settings else None
    if rule is None:
        return _back(err="Rule not found.", page="grow", anchor="boosts")
    rule.is_active = not rule.is_active
    db.commit()
    return _back(ok=f"{L.describe_boost(rule)} is now {'on' if rule.is_active else 'paused'}.", page="grow", anchor="boosts")


@router.post("/boosts/{rule_id}/delete")
async def delete_boost(rule_id: int, request: Request, db: Session = Depends(get_db)):
    from loyalty.models import LoyaltyBoost
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    rule = _own(db, LoyaltyBoost, business, rule_id) if settings else None
    if rule is None:
        return _back(err="Rule not found.", page="grow", anchor="boosts")
    db.delete(rule)
    db.commit()
    return _back(ok="Rule deleted.", page="grow", anchor="boosts")


# ------------------------------------------------------------------------
# Loyalty Plus: outlets and counter staff (owner only)
# ------------------------------------------------------------------------

@router.post("/outlets")
async def add_outlet(request: Request, db: Session = Depends(get_db), name: str = Form(""),
                     main_name: str = Form("")):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    if main_name.strip() and not settings.main_outlet_name:
        settings.main_outlet_name = " ".join(main_name.split())[:60]
        db.commit()
    try:
        outlet = L.add_outlet(db, business, settings, name)
    except L.LoyaltyError as err:
        db.rollback()
        return _back(err=err.message, page="settings", anchor="outlets")
    log_event(db=db, event_type="loyalty_outlet_added", status="info", message=f"Outlet added: {outlet.name}",
              business_id=business.id)
    return _back(ok=f"Added {outlet.name}. Print its own counter QR below.", page="settings", anchor="outlets")


@router.post("/outlets/main")
async def rename_main(request: Request, db: Session = Depends(get_db), name: str = Form("")):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    clean = " ".join(name.split())[:60]
    if not clean:
        return _back(err="Please type a name.", page="settings", anchor="outlets")
    settings.main_outlet_name = clean
    db.commit()
    return _back(ok="Saved.", page="settings", anchor="outlets")


@router.post("/outlets/{outlet_id}/toggle")
async def toggle_outlet(outlet_id: int, request: Request, db: Session = Depends(get_db)):
    from loyalty.models import LoyaltyOutlet
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    outlet = _own(db, LoyaltyOutlet, business, outlet_id) if settings else None
    if outlet is None:
        return _back(err="Outlet not found.", page="settings", anchor="outlets")
    outlet.is_active = not outlet.is_active
    db.commit()
    state = "on: its QR gives points" if outlet.is_active else "paused: its QR no longer gives points"
    return _back(ok=f"{outlet.name} is {state}.", page="settings", anchor="outlets")


@router.post("/outlets/{outlet_id}/rotate")
async def rotate_outlet(outlet_id: int, request: Request, db: Session = Depends(get_db)):
    from loyalty.models import LoyaltyOutlet
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    outlet = _own(db, LoyaltyOutlet, business, outlet_id) if settings else None
    if outlet is None:
        return _back(err="Outlet not found.", page="settings", anchor="outlets")
    outlet.earn_code = L._unused_earn_code(db, settings)
    db.commit()
    log_event(db=db, event_type="loyalty_code_changed", status="warning",
              message=f"Counter QR code changed for {outlet.name}", business_id=business.id)
    return _back(ok=f"New QR for {outlet.name}. Print it: the old one no longer gives points.",
                 page="settings", anchor="outlets")


@router.post("/staff")
async def add_staff(request: Request, db: Session = Depends(get_db), name: str = Form(""),
                    username: str = Form(""), password: str = Form(""), outlet_id: str = Form("")):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    oid = int(outlet_id) if outlet_id.strip().isdigit() else None
    try:
        staff = L.create_staff(db, business, name, username, password, oid)
    except L.LoyaltyError as err:
        db.rollback()
        return _back(err=err.message, page="settings", anchor="staff")
    log_event(db=db, event_type="loyalty_staff_added", status="info",
              message=f"Counter staff login added for {staff.full_name}", business_id=business.id)
    return _back(ok=f"{staff.full_name} can now sign in at verzio.codes/owner/login with '{staff.email}' "
                    f"and the password you set.", page="settings", anchor="staff")


@router.post("/staff/{staff_id}/delete")
async def delete_staff(staff_id: int, request: Request, db: Session = Depends(get_db)):
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    try:
        staff = L.remove_staff(db, business, staff_id)
    except L.LoyaltyError as err:
        db.rollback()
        return _back(err=err.message, page="settings", anchor="staff")
    log_event(db=db, event_type="loyalty_staff_removed", status="warning",
              message=f"Counter staff login removed for {staff.full_name}", business_id=business.id)
    return _back(ok=f"{staff.full_name}'s login was removed.", page="settings", anchor="staff")


@router.post("/customers/{member_id}/birthday")
async def owner_birthday(member_id: int, request: Request, db: Session = Depends(get_db), birthday: str = Form("")):
    from loyalty.models import LoyaltyMember
    _, business, settings, redirect = _auth_owner(request, db)
    if redirect:
        return redirect
    member = _own(db, LoyaltyMember, business, member_id) if settings else None
    if member is None:
        return _back(err="Customer not found.", page="customers")
    if not birthday.strip():
        member.birthday = None
        db.commit()
        return _back(ok="Birthday cleared.", page="customers")
    mmdd = L.parse_birthday(birthday)
    if not mmdd:
        return _back(err="Type the birthday like 14 Aug.", page="customers")
    L.set_birthday(db, member, mmdd, overwrite=True)
    return _back(ok=f"Birthday saved: {L.birthday_label(mmdd)}.", page="customers")


# ------------------------------------------------------------------------
# Loyalty-only businesses never see Verzio Book pages
# ------------------------------------------------------------------------

_BOOK_PAGES = {"dashboard", "appointments", "services", "reports", "settings"}


async def loyalty_only_guard(request: Request, call_next):
    """Middleware: for an owner whose business is "loyalty only", any Verzio Book
    page (/owner, /owner/dashboard, appointments, services, reports, settings)
    goes to the Loyalty dashboard instead. Everyone else is untouched."""
    path = request.url.path.rstrip("/")
    parts = path.split("/")
    if path == "/owner" or (len(parts) >= 3 and parts[1] == "owner" and parts[2] in _BOOK_PAGES):
        from database import SessionLocal
        db = SessionLocal()
        try:
            owner, business = resolve_owner_and_business(request, db)
            if business is not None and L.is_loyalty_only(db, business):
                return RedirectResponse(url="/owner/loyalty", status_code=303)
        except Exception:
            logger.exception("Loyalty-only check failed; showing the page as usual")
        finally:
            db.close()
    return await call_next(request)
