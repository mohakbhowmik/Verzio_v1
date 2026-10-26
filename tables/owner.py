"""
tables/owner.py: the Verzio Tables dashboard (/owner/tables).

  /owner/tables            Tonight: bookings by time, Arrived / No-show / bill,
                           approvals, walk-ins and phone bookings, waitlist
  /owner/tables/plan       the floor plan: zones as blocks on a grid
  /owner/tables/guests     everyone who booked, visits, no-shows, offers on/off
  /owner/tables/results    this month: bookings, guests seated, commission saved
  /owner/tables/settings   booking rules, reminders/reviews/offers, staff logins

Door staff (owners.role = 'staff') see Tonight only. tables_guard keeps them
there, and sends a Tables business's owner away from Verzio Book pages.
"""
import json
import logging
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from activity_log import log_event
from database import get_db
from tables import bot as B
from tables import engine as T
from tables.models import TablesWaitlist, TablesZone
from owner.owner_auth import is_mobile, resolve_owner_and_business
from whatsapp_client import canonical_phone, is_real_phone, mask_phone, normalize_phone

logger = logging.getLogger("VERZIO_TABLES_OWNER")
router = APIRouter(prefix="/owner/tables", tags=["owner-tables"])
templates = Jinja2Templates(directory="templates")

PAGES = {"": "Tonight", "plan": "Floor plan", "guests": "Guests", "results": "Results", "settings": "Settings"}
STAFF_ACTIONS = {"arrived", "no_show", "undo"}


def _back(page: str = "", ok: str | None = None, err: str | None = None, anchor: str = "",
          day: str | None = None) -> RedirectResponse:
    params = []
    if day:
        params.append("day=" + quote_plus(day))
    if ok:
        params.append("ok=" + quote_plus(ok[:200]))
    if err:
        params.append("err=" + quote_plus(err[:200]))
    url = "/owner/tables" + (f"/{page}" if page else "") + ("?" + "&".join(params) if params else "") + (f"#{anchor}" if anchor else "")
    return RedirectResponse(url=url, status_code=303)


def _auth(request: Request, db: Session):
    """(owner, business, settings, role, redirect). settings is None if Tables is off."""
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        return None, None, None, None, RedirectResponse(url="/owner/login", status_code=303)
    settings = T.get_settings(db, business.id)
    return owner, business, (settings if settings and settings.enabled else None), T.owner_role(db, owner.id), None


def _owner_only(request: Request, db: Session):
    owner, business, settings, role, redirect = _auth(request, db)
    if redirect:
        return None, None, redirect
    if settings is None:
        return None, None, _back(err="Verzio Tables isn't switched on for your business.")
    if role != "owner":
        return None, None, _back(err="Only the owner can change that.")
    return business, settings, None


def _render(request: Request, db: Session, page: str, template: str, extra=None, owner_page: bool = True):
    owner, business, settings, role, redirect = _auth(request, db)
    if redirect:
        return redirect
    if owner_page and role != "owner":
        return _back()
    from loyalty import engine as L
    context = {
        "layout": "tables/base_mobile.html" if is_mobile(request) else "tables/base.html",
        "active_page": page or "tonight", "page_title": PAGES[page],
        "owner": owner, "business": business, "settings": settings, "role": role,
        "has_loyalty": role == "owner" and L.is_enabled(db, business),
        "running": T.running(db, business),
        "plan": T.plan_info(settings) if settings is not None else None,
        "ok": (request.query_params.get("ok") or "")[:200] or None,
        "err": (request.query_params.get("err") or "")[:200] or None,
        "clock": T.clock, "when": T.when_label, "inr": T.inr, "labels": T.STATUS_LABELS,
    }
    if settings is not None and extra:
        context.update(extra(owner, business, settings))
    return templates.TemplateResponse(request=request, name="tables/off.html" if settings is None else template,
                                      context=context)


def share_link(business) -> str | None:
    """wa.me link for Google Maps, Instagram and the venue's website."""
    from loyalty.engine import _connected_number
    number = normalize_phone(_connected_number(business) or "")
    if not is_real_phone(number):
        return None
    return f"https://wa.me/{number}?text=Hi%2C%20I%27d%20like%20to%20book%20a%20table"


# ------------------------------------------------------------------------
# Tonight (door view)
# ------------------------------------------------------------------------

def _day_param(request: Request, business) -> date:
    raw = request.query_params.get("day")
    try:
        return date.fromisoformat(raw) if raw else T.service_day(T.business_now(business))
    except ValueError:
        return T.service_day(T.business_now(business))


@router.get("")
async def tonight(request: Request, db: Session = Depends(get_db)):
    def extra(owner, business, settings):
        day = _day_param(request, business)
        now = T.business_now(business)
        today = T.service_day(now)
        rows = T.bookings_for_day(db, business, day)
        zones = T.zones(db, business.id)
        live = []
        if day == today:
            for z in zones:
                used = T.peak_load(db, z, now, now + timedelta(minutes=1), now, settings.release_minutes)
                live.append({"zone": z, "used": used, "cap": T.capacity(z)})
        active = [b for b in rows if b.status in ("pending", "confirmed", "arrived")]
        late_after = timedelta(minutes=settings.release_minutes or 20)
        times = []
        for b in rows:
            key = T.clock(b.start_at)
            if not times or times[-1][0] != key:
                times.append((key, []))
            times[-1][1].append(b)
        slot_choices = [(t.strftime("%Y-%m-%dT%H:%M"), T.clock(t))
                        for t in T.arrival_times(business, settings, day, now - timedelta(days=1))]
        return {
            "day": day, "day_label": T.day_label(day, today), "prev": (day - timedelta(days=1)).isoformat(),
            "next": (day + timedelta(days=1)).isoformat(), "is_today": day == today,
            "groups": times, "zones": zones, "live": live,
            "pending": [b for b in rows if b.status == "pending"],
            "counts": {"bookings": len(active), "guests": sum(b.party for b in active),
                       "arrived": sum(1 for b in rows if b.status == "arrived"),
                       "waiting": sum(1 for b in rows if b.status == "confirmed")},
            "late": {b.id for b in rows if b.status == "confirmed" and now >= b.start_at + late_after},
            "waitlist": T.waitlist_for_day(db, business, day),
            "slot_choices": slot_choices, "now_value": now.strftime("%Y-%m-%dT%H:%M"),
            "share": share_link(business),
        }
    return _render(request, db, "", "tables/tonight.html", extra, owner_page=False)


@router.post("/bookings/{booking_id}/bill")
async def booking_bill(booking_id: int, request: Request, db: Session = Depends(get_db),
                       bill: str = Form(""), day: str = Form("")):
    owner, business, settings, role, redirect = _auth(request, db)
    if redirect:
        return redirect
    try:
        booking = T.set_bill(db, business, booking_id, bill)
    except T.TablesError as err:
        return _back(err=err.message, day=day)
    return _back(ok=f"Bill saved for {booking.name or 'the table'}.", day=day, anchor=f"b{booking.id}")


@router.post("/bookings/{booking_id}/{action}")
async def booking_action(booking_id: int, action: str, request: Request, db: Session = Depends(get_db),
                         day: str = Form("")):
    owner, business, settings, role, redirect = _auth(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Verzio Tables isn't switched on for your business.")
    if role != "owner" and action not in STAFF_ACTIONS:
        return _back(err="Only the owner can do that.", day=day)
    try:
        booking, previous = T.change_status(db, business, booking_id, action)
    except T.TablesError as err:
        db.rollback()
        return _back(err=err.message, day=day)
    who = booking.name or (mask_phone(booking.phone) if booking.phone else "walk-in")
    log_event(db=db, event_type=f"tables_{action}", status="info",
              message=f"Table #{booking.id} ({who}, {booking.party} people) {previous} -> {booking.status} "
                      f"by {'staff' if role == 'staff' else 'owner'}", business_id=business.id)
    note = ""
    if action in ("approve", "decline", "cancel") and booking.phone:
        told = await B.tell_guest_status(db, business, booking)
        note = " Guest told on WhatsApp." if told else " We couldn't message the guest: please call them."
    labels = {"approve": "accepted", "decline": "declined", "cancel": "cancelled", "arrived": "marked arrived",
              "no_show": "marked no-show", "undo": "set back to booked"}
    return _back(ok=f"{who}: {labels[action]}.{note}", day=day, anchor=f"b{booking.id}")


@router.post("/add")
async def add_booking(request: Request, db: Session = Depends(get_db), kind: str = Form("walkin"),
                      at: str = Form(""), party: str = Form("2"), zone_id: str = Form(""), name: str = Form(""),
                      phone: str = Form(""), notify: str = Form(""), day: str = Form("")):
    """A walk-in (seated now) or a booking taken on the phone."""
    owner, business, settings, role, redirect = _auth(request, db)
    if redirect:
        return redirect
    if settings is None:
        return _back(err="Verzio Tables isn't switched on for your business.")
    now = T.business_now(business)
    try:
        size = T.clean_party(party, settings)
        if not zone_id.isdigit():
            raise T.TablesError("Pick an area.")
        if kind == "walkin":
            start = now
        else:
            try:
                start = datetime.strptime(at.strip()[:16], "%Y-%m-%dT%H:%M")
            except ValueError:
                raise T.TablesError("Pick a time for the booking.")
        number = canonical_phone(phone, business.timezone) if phone.strip() else ""
        if kind != "walkin" and not number:
            raise T.TablesError("Type the guest's phone number so they get the confirmation and reminder.")
        booking = T.create_booking(db, business, settings, int(zone_id), start, size, number or None, name,
                                   source="walkin" if kind == "walkin" else "staff", now=now, check_rules=False)
    except T.TablesError as err:
        db.rollback()
        return _back(err=err.message, day=day, anchor="add")
    log_event(db=db, event_type="tables_walkin" if kind == "walkin" else "tables_phone_booking", status="info",
              message=f"{'Walk-in' if kind == 'walkin' else 'Phone booking'}: {size} people, "
                      f"{booking.zone.name}, {T.when_label(booking.start_at)}", business_id=business.id)
    note = ""
    if kind != "walkin" and notify and booking.phone:
        told = await B.tell_guest_status(db, business, booking)
        note = " Confirmation sent on WhatsApp." if told else " The WhatsApp confirmation couldn't be sent."
    what = "Walk-in added" if kind == "walkin" else f"Booked for {T.when_label(booking.start_at)}"
    return _back(ok=f"{what}: {size} people, {booking.zone.name}.{note}",
                 day=T.service_day(booking.start_at).isoformat(), anchor=f"b{booking.id}")


@router.post("/waitlist/{entry_id}/remove")
async def waitlist_remove(entry_id: int, request: Request, db: Session = Depends(get_db), day: str = Form("")):
    owner, business, settings, role, redirect = _auth(request, db)
    if redirect:
        return redirect
    entry = db.get(TablesWaitlist, entry_id)
    if entry is None or entry.business_id != business.id:
        return _back(err="Waitlist entry not found.", day=day)
    entry.status = "cancelled"
    db.commit()
    return _back(ok="Removed from the waitlist.", day=day, anchor="waitlist")


# ------------------------------------------------------------------------
# Floor plan
# ------------------------------------------------------------------------

@router.get("/plan")
async def plan_page(request: Request, db: Session = Depends(get_db)):
    def extra(owner, business, settings):
        zones = T.zones(db, business.id)
        return {"zones": zones, "cols": T.GRID_COLS, "rows": T.GRID_ROWS,
                "zones_json": [{"id": z.id, "name": z.name, "x": z.x, "y": z.y, "w": z.w, "h": z.h,
                                "active": z.is_active} for z in zones],
                "seats": {z.id: T.seats(z) for z in zones}}
    return _render(request, db, "plan", "tables/plan.html", extra)


def _own_zone(db: Session, business, zone_id: int) -> TablesZone | None:
    z = db.get(TablesZone, zone_id)
    return z if z is not None and z.business_id == business.id and not z.is_deleted else None


def _free_spot(db: Session, business, w: int, h: int) -> tuple[int, int]:
    taken = set()
    for z in T.zones(db, business.id):
        for cx in range(z.x, z.x + z.w):
            for cy in range(z.y, z.y + z.h):
                taken.add((cx, cy))
    for y in range(0, T.GRID_ROWS - h + 1):
        for x in range(0, T.GRID_COLS - w + 1):
            if all((cx, cy) not in taken for cx in range(x, x + w) for cy in range(y, y + h)):
                return x, y
    return 0, 0


@router.post("/zones")
async def add_zone(request: Request, db: Session = Depends(get_db)):
    business, settings, redirect = _owner_only(request, db)
    if redirect:
        return redirect
    form = dict(await request.form())
    if len(T.zones(db, business.id)) >= 20:
        return _back("plan", err="You can have up to 20 areas.")
    try:
        fields = T.clean_zone_fields({**form, "x": 0, "y": 0})
    except T.TablesError as err:
        return _back("plan", err=err.message, anchor="add")
    fields["x"], fields["y"] = _free_spot(db, business, fields["w"], fields["h"])
    zone = TablesZone(business_id=business.id, **fields)
    db.add(zone)
    db.commit()
    return _back("plan", ok=f"Added {zone.name}. Drag it into place on the plan.")


@router.post("/zones/demo")
async def demo_zones(request: Request, db: Session = Depends(get_db)):
    business, settings, redirect = _owner_only(request, db)
    if redirect:
        return redirect
    added = T.add_demo_zones(db, business)
    return _back("plan", ok=f"Added {added} example areas. Rename and resize them to match your venue." if added
                 else None, err=None if added else "The example areas can only be added to an empty plan.")


@router.post("/zones/layout")
async def save_layout(request: Request, db: Session = Depends(get_db)):
    business, settings, redirect = _owner_only(request, db)
    if redirect:
        return JSONResponse({"ok": False, "error": "Please sign in again."}, status_code=401)
    try:
        items = json.loads((await request.body()) or b"[]")
        assert isinstance(items, list)
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad layout."}, status_code=400)
    for item in items[:40]:
        try:
            zone = _own_zone(db, business, int(item.get("id")))
        except (TypeError, ValueError, AttributeError):
            continue
        if zone is None:
            continue
        w = max(1, min(T.GRID_COLS, int(item.get("w", zone.w))))
        h = max(1, min(T.GRID_ROWS, int(item.get("h", zone.h))))
        zone.w, zone.h = w, h
        zone.x = max(0, min(T.GRID_COLS - w, int(item.get("x", zone.x))))
        zone.y = max(0, min(T.GRID_ROWS - h, int(item.get("y", zone.y))))
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/zones/{zone_id}")
async def edit_zone(zone_id: int, request: Request, db: Session = Depends(get_db)):
    business, settings, redirect = _owner_only(request, db)
    if redirect:
        return redirect
    zone = _own_zone(db, business, zone_id)
    if zone is None:
        return _back("plan", err="Area not found.")
    form = dict(await request.form())
    try:
        fields = T.clean_zone_fields({**form, "x": zone.x, "y": zone.y, "w": zone.w, "h": zone.h})
    except T.TablesError as err:
        return _back("plan", err=err.message, anchor=f"z{zone.id}")
    for k in ("name", "note", "kind", "table_count", "seats_per_table", "people_capacity", "min_party"):
        setattr(zone, k, fields[k])
    db.commit()
    return _back("plan", ok=f"Saved {zone.name}.", anchor=f"z{zone.id}")


@router.post("/zones/{zone_id}/toggle")
async def toggle_zone(zone_id: int, request: Request, db: Session = Depends(get_db)):
    business, settings, redirect = _owner_only(request, db)
    if redirect:
        return redirect
    zone = _own_zone(db, business, zone_id)
    if zone is None:
        return _back("plan", err="Area not found.")
    zone.is_active = not zone.is_active
    db.commit()
    return _back("plan", ok=f"{zone.name} is {'open for bookings' if zone.is_active else 'closed: guests see it grey'}.",
                 anchor=f"z{zone.id}")


@router.post("/zones/{zone_id}/delete")
async def delete_zone(zone_id: int, request: Request, db: Session = Depends(get_db)):
    business, settings, redirect = _owner_only(request, db)
    if redirect:
        return redirect
    zone = _own_zone(db, business, zone_id)
    if zone is None:
        return _back("plan", err="Area not found.")
    zone.is_deleted, zone.is_active = True, False
    db.commit()
    return _back("plan", ok=f"Deleted {zone.name}. Bookings already made for it stay on your list.")


# ------------------------------------------------------------------------
# Guests and results
# ------------------------------------------------------------------------

@router.get("/guests")
async def guests_page(request: Request, db: Session = Depends(get_db)):
    def extra(owner, business, settings):
        q = (request.query_params.get("q") or "").strip()[:40]
        return {"guests": T.guests(db, business, q), "q": q}
    return _render(request, db, "guests", "tables/guests.html", extra)


@router.post("/guests/{phone}/offers")
async def guest_offers(phone: str, request: Request, db: Session = Depends(get_db), on: str = Form("")):
    business, settings, redirect = _owner_only(request, db)
    if redirect:
        return redirect
    if on == "1":
        T.opt_in(db, business.id, phone)
    else:
        T.opt_out(db, business.id, phone)
    return _back("guests", ok="Saved.")


@router.get("/results")
async def results_page(request: Request, db: Session = Depends(get_db)):
    def extra(owner, business, settings):
        r = T.results(db, business, settings)
        top = max([n for _, n in r.daily] + [1])
        return {"r": r, "top": top, "plan_price": settings.monthly_amount or 0}
    return _render(request, db, "results", "tables/results.html", extra)


# ------------------------------------------------------------------------
# Settings and staff logins
# ------------------------------------------------------------------------

@router.get("/settings")
async def settings_page(request: Request, db: Session = Depends(get_db)):
    def extra(owner, business, settings):
        return {"staff": T.staff_logins(db, business), "share": share_link(business),
                "used": T.offers_this_month(db, business.id)}
    return _render(request, db, "settings", "tables/settings.html", extra)


@router.post("/settings")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    business, settings, redirect = _owner_only(request, db)
    if redirect:
        return redirect
    form = await request.form()

    def num(key, lo, hi, current):
        try:
            return max(lo, min(hi, int(str(form.get(key, current)).strip())))
        except (TypeError, ValueError):
            return current

    settings.slot_minutes = num("slot_minutes", 15, 120, settings.slot_minutes)
    settings.seating_minutes = num("seating_minutes", 30, 720, settings.seating_minutes)
    settings.last_arrival_minutes = num("last_arrival_minutes", 0, 360, settings.last_arrival_minutes)
    settings.book_ahead_days = num("book_ahead_days", 1, 90, settings.book_ahead_days)
    settings.min_notice_minutes = num("min_notice_minutes", 0, 1440, settings.min_notice_minutes)
    settings.max_party = num("max_party", 1, 200, settings.max_party)
    settings.approval_party = num("approval_party", 0, 200, settings.approval_party)
    settings.release_minutes = num("release_minutes", 5, 180, settings.release_minutes)
    settings.avg_bill = num("avg_bill", 0, 1_000_000, settings.avg_bill)
    settings.commission_pct = num("commission_pct", 0, 50, settings.commission_pct)
    settings.rebook_weeks = num("rebook_weeks", 1, 26, settings.rebook_weeks)
    for flag in ("reminder_on", "review_on", "rebook_on", "waitlist_on"):
        setattr(settings, flag, form.get(flag) in ("on", "1", "true"))
    url = (form.get("google_review_url") or "").strip()[:300]
    if url and not url.startswith("https://"):
        return _back("settings", err="The Google review link must start with https://", anchor="growth")
    settings.google_review_url = url or None
    db.commit()
    return _back("settings", ok="Settings saved.")


@router.post("/staff")
async def add_staff(request: Request, db: Session = Depends(get_db), name: str = Form(""),
                    username: str = Form(""), password: str = Form("")):
    business, settings, redirect = _owner_only(request, db)
    if redirect:
        return redirect
    try:
        staff = T.create_staff(db, business, name, username, password)
    except T.TablesError as err:
        db.rollback()
        return _back("settings", err=err.message, anchor="staff")
    log_event(db=db, event_type="tables_staff_added", status="info",
              message=f"Door staff login added for {staff.full_name}", business_id=business.id)
    return _back("settings", ok=f"{staff.full_name} can now sign in at verzio.codes/owner/login with "
                                f"'{staff.email}' and the password you set.", anchor="staff")


@router.post("/staff/{staff_id}/delete")
async def delete_staff(staff_id: int, request: Request, db: Session = Depends(get_db)):
    business, settings, redirect = _owner_only(request, db)
    if redirect:
        return redirect
    try:
        staff = T.remove_staff(db, business, staff_id)
    except T.TablesError as err:
        return _back("settings", err=err.message, anchor="staff")
    log_event(db=db, event_type="tables_staff_removed", status="warning",
              message=f"Door staff login removed for {staff.full_name}", business_id=business.id)
    return _back("settings", ok=f"{staff.full_name}'s login was removed.", anchor="staff")


# ------------------------------------------------------------------------
# Guard: door staff stay on Tonight; Tables businesses never see Verzio Book
# ------------------------------------------------------------------------

_BOOK_PAGES = {"dashboard", "appointments", "services", "reports", "settings"}
_STAFF_OK = ("/owner/tables", "/owner/login", "/owner/logout", "/owner/password", "/owner/forgot", "/owner/reset")


async def tables_guard(request: Request, call_next):
    path = request.url.path.rstrip("/")
    if path == "/owner" or path.startswith("/owner/"):
        from database import SessionLocal
        db = SessionLocal()
        try:
            owner, business = resolve_owner_and_business(request, db)
            if owner is not None and business is not None:
                if T.owner_role(db, owner.id) == "staff":
                    allowed = path == "/owner/tables" or any(path.startswith(p + "/") or path == p for p in _STAFF_OK)
                    if not allowed or path.startswith(("/owner/tables/plan", "/owner/tables/guests",
                                                       "/owner/tables/results", "/owner/tables/settings",
                                                       "/owner/tables/zones", "/owner/tables/staff")):
                        return RedirectResponse(url="/owner/tables", status_code=303)
                else:
                    parts = path.split("/")
                    if (path == "/owner" or (len(parts) >= 3 and parts[2] in _BOOK_PAGES)) and T.is_enabled(db, business):
                        return RedirectResponse(url="/owner/tables", status_code=303)
        except Exception:
            logger.exception("Tables guard check failed; showing the page as usual")
        finally:
            db.close()
    return await call_next(request)
