"""
loyalty/owner.py: the Verzio Loyalty dashboard for owners (/owner/loyalty).

Its own layout and menu, separate from the Verzio Book portal:
  /owner/loyalty            Dashboard: give a reward, codes waiting at the counter,
                            add points, today's numbers, recent activity
  /owner/loyalty/rewards    the reward catalog
  /owner/loyalty/customers  regulars, points, visits, streaks (searchable)
  /owner/loyalty/settings   business name, WhatsApp number, points, streaks, counter QR

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
                "streak_daily": "Daily streak bonus", "streak_weekly": "Weekly streak bonus"}


PAGES = {"": "Dashboard", "rewards": "Rewards", "customers": "Customers", "settings": "Settings"}


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
    """(owner, business, settings, redirect). settings is None if loyalty is off."""
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        return None, None, None, RedirectResponse(url="/owner/login", status_code=303)
    settings = L.get_settings(db, business.id)
    return owner, business, (settings if settings and settings.enabled else None), None


async def _tell_customer(business, phone: str, text: str) -> None:
    """Message the customer only if they messaged in the last 24 h (free and allowed)."""
    if is_real_phone(phone) and window_open(business.id, phone):
        await send_whatsapp(phone, business.whatsapp_business_phone_number_id,
                            {"type": "text", "body": text}, business.id)


# ------------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------------

def _render(request: Request, db: Session, page: str, template: str, extra=None):
    owner, business, settings, redirect = _auth(request, db)
    if redirect:
        return redirect
    context = {
        "layout": "loyalty/base_mobile.html" if is_mobile(request) else "loyalty/base.html",
        "active_page": page or "dashboard", "page_title": PAGES[page],
        "owner": owner, "business": business, "settings": settings,
        "has_book": not L.is_loyalty_only(db, business),
        "running": L.running(db, business),
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
        return {"code": code, "found": found, "problem": problem, "found_visits": visits,
                "pending": L.pending_claims(db, business.id),
                "stats": L.stats(db, business.id),
                "recent": L.recent_entries(db, business.id, 12),
                "reward_count": len(L.rewards(db, business.id)),
                "qr_link": L.qr_link(settings)}
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
        return {"members": members, "q": q, "streaks": streaks, "stats": L.stats(db, business.id)}
    return _render(request, db, "customers", "loyalty/customers.html", extra)


@router.get("/settings")
async def settings_page(request: Request, db: Session = Depends(get_db)):
    return _render(request, db, "settings", "loyalty/settings.html",
                   lambda o, b, s: {"qr_link": L.qr_link(s)})


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
    who = member.name or f"+{member.phone}"
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
    _, business, settings, redirect = _auth(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    try:
        member, delta, unlocked = L.owner_adjust(db, business, phone, points or settings.points_per_visit,
                                                 note.strip() or None, name=name.strip() or None,
                                                 as_visit=(kind == "visit"))
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
    who = member.name or f"+{member.phone}"
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
    _, business, settings, redirect = _auth(request, db)
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
    _, business, settings, redirect = _auth(request, db)
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
    _, business, settings, redirect = _auth(request, db)
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
    _, business, settings, redirect = _auth(request, db)
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
    _, business, settings, redirect = _auth(request, db)
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
    _, business, settings, redirect = _auth(request, db)
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
    owner, business, settings, redirect = _auth(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Loyalty isn't switched on for your business.")
    link = L.qr_link(settings)
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
                 "rewards": L.rewards(db, business.id)[:3]},
    )


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
