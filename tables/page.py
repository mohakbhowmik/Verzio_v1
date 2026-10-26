"""
tables/page.py: the guest's booking page (/t/<token>).

The link is sent on WhatsApp and belongs to one phone number: no login, no
cookies, no form to fill. It works for LINK_HOURS and books once. The page
shows the venue's floor plan; zones turn green / amber / grey for the chosen
date, time and party size, and the guest taps one to book. The name is
prefilled from their WhatsApp profile. The confirmation arrives on WhatsApp.
"""
import logging
from datetime import datetime
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from activity_log import log_event
from database import Business, get_db
from tables import bot as B
from tables import engine as T
from tables.models import TablesBooking
from whatsapp_client import mask_phone, send_whatsapp

logger = logging.getLogger("VERZIO_TABLES_PAGE")
router = APIRouter(tags=["tables-page"])
templates = Jinja2Templates(directory="templates")
NO_CACHE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"}


def _load(db: Session, token: str):
    """(link, business, settings, problem)."""
    link = T.find_link(db, token)
    if link is None:
        return None, None, None, "missing"
    biz = db.get(Business, link.business_id)
    settings = T.get_settings(db, link.business_id)
    if biz is None or settings is None or not settings.enabled:
        return link, None, None, "missing"
    problem = T.link_problem(link)
    if problem is None and not T.running(db, biz):
        problem = "paused"
    return link, biz, settings, problem


def _parse_at(raw: str) -> datetime | None:
    try:
        return datetime.strptime((raw or "").strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def _back(token: str, err: str, at: str = "", party: str = "", zone: str = "") -> RedirectResponse:
    q = f"?err={quote_plus(err[:200])}&at={quote_plus(at)}&party={quote_plus(party)}&zone={quote_plus(zone)}"
    return RedirectResponse(url=f"/t/{token}{q}", status_code=303)


@router.get("/t/{token}", response_class=HTMLResponse)
def booking_page(token: str, request: Request, db: Session = Depends(get_db)):
    link, biz, settings, problem = _load(db, token)
    if problem:
        booking = db.get(TablesBooking, link.booking_id) if (link and link.booking_id) else None
        return templates.TemplateResponse(
            request=request, name="tables/link_problem.html", headers=NO_CACHE,
            status_code=200 if booking else 404,
            context={"biz": biz, "problem": problem, "booking": booking,
                     "when": T.when_label(booking.start_at) if booking else None})
    now = T.business_now(biz)
    days = []
    for d in T.bookable_days(biz, settings, now):
        times = [{"v": t.strftime("%Y-%m-%dT%H:%M"), "l": T.clock(t)} for t in T.arrival_times(biz, settings, d, now)]
        label = T.day_label(d, now.date())
        days.append({"v": d.isoformat(), "l": label if label in ("Today", "Tomorrow") else d.strftime("%A"),
                     "sub": d.strftime("%d %b"), "times": times})
    zones = [{"id": z.id, "name": z.name, "note": z.note or "", "x": z.x, "y": z.y, "w": z.w, "h": z.h,
              "kind": z.kind, "seats": T.seats(z)} for z in T.zones(db, biz.id)]
    pre_at = request.query_params.get("at") or ""
    if not pre_at and link.day and link.time:
        for d in days:
            for t in d["times"]:
                if d["v"] == link.day and t["v"].endswith(link.time):
                    pre_at = t["v"]
    return templates.TemplateResponse(
        request=request, name="tables/book.html", headers=NO_CACHE,
        context={"biz": biz, "settings": settings, "token": token, "days": days, "zones": zones,
                 "cols": T.GRID_COLS, "rows": T.GRID_ROWS, "masked": mask_phone(link.phone),
                 "name": link.name or "", "pre_at": pre_at,
                 "pre_party": request.query_params.get("party") or link.party or 2,
                 "pre_zone": request.query_params.get("zone") or "",
                 "err": (request.query_params.get("err") or "")[:200] or None,
                 "approval_party": settings.approval_party if T.needs_approval(biz, settings, 999) else 0,
                 "max_party": settings.max_party, "waitlist_on": settings.waitlist_on,
                 "hours": settings.seating_minutes})


@router.get("/t/{token}/zones")
def zone_states(token: str, at: str = "", party: str = "2", db: Session = Depends(get_db)):
    link, biz, settings, problem = _load(db, token)
    if problem:
        return JSONResponse({"error": problem}, status_code=410, headers=NO_CACHE)
    start = _parse_at(at)
    try:
        size = T.clean_party(party, settings)
        if start is None:
            raise T.TablesError("Pick a time.")
        T.check_time(biz, settings, start)
    except T.TablesError as err:
        return JSONResponse({"error": err.message}, status_code=400, headers=NO_CACHE)
    states = T.availability(db, biz, settings, start, size)
    return JSONResponse({"zones": [{"id": s.zone.id, "state": s.state, "note": s.note} for s in states],
                         "approval": T.needs_approval(biz, settings, size)}, headers=NO_CACHE)


@router.post("/t/{token}/book")
async def book(token: str, db: Session = Depends(get_db), at: str = Form(""), party: str = Form(""),
               zone_id: str = Form(""), name: str = Form("")):
    link, biz, settings, problem = _load(db, token)
    if problem:
        return RedirectResponse(url=f"/t/{token}", status_code=303)
    start = _parse_at(at)
    try:
        size = T.clean_party(party, settings)
        if start is None or not zone_id.isdigit():
            raise T.TablesError("Please choose a time and tap an area on the map.")
        booking = T.create_booking(db, biz, settings, int(zone_id), start, size, link.phone, name or link.name)
    except T.TablesError as err:
        db.rollback()
        return _back(token, err.message, at, party, zone_id)
    link.used_at, link.booking_id = datetime.utcnow(), booking.id
    db.commit()
    log_event(db=db, event_type="tables_booked", status="success",
              message=f"Table #{booking.id}: {booking.party} people, {T.when_label(booking.start_at)}, "
                      f"{booking.zone.name}{' (waiting for approval)' if booking.status == 'pending' else ''}",
              business_id=biz.id)
    await B.notify_booked(db, biz, booking)
    return RedirectResponse(url=f"/t/{token}/done", status_code=303)


@router.post("/t/{token}/waitlist")
async def waitlist(token: str, db: Session = Depends(get_db), at: str = Form(""), party: str = Form(""),
                   zone_id: str = Form(""), name: str = Form("")):
    link, biz, settings, problem = _load(db, token)
    if problem:
        return RedirectResponse(url=f"/t/{token}", status_code=303)
    if not settings.waitlist_on:
        return _back(token, "The waitlist isn't open. Please try another time.", at, party, zone_id)
    start = _parse_at(at)
    try:
        size = T.clean_party(party, settings)
        if start is None:
            raise T.TablesError("Please choose a time.")
        T.check_time(biz, settings, start)
    except T.TablesError as err:
        return _back(token, err.message, at, party, zone_id)
    zone = int(zone_id) if zone_id.isdigit() else None
    entry = T.join_waitlist(db, biz, link.phone, name or link.name, size, start, zone)
    log_event(db=db, event_type="tables_waitlist", status="info",
              message=f"Waitlist: {size} people for {T.when_label(start)}", business_id=biz.id)
    await send_whatsapp(link.phone, biz.whatsapp_business_phone_number_id, {"type": "text", "body":
        f"You're on the waitlist at {biz.name} for {T.when_label(start)} ({B._guests(size)}). "
        f"We'll message you here the moment a table opens up."}, biz.id)
    return RedirectResponse(url=f"/t/{token}/done?waitlist={entry.id}", status_code=303)


@router.get("/t/{token}/done", response_class=HTMLResponse)
def done(token: str, request: Request, db: Session = Depends(get_db)):
    link = T.find_link(db, token)
    if link is None:
        return RedirectResponse(url=f"/t/{token}", status_code=303)
    biz = db.get(Business, link.business_id)
    booking = db.get(TablesBooking, link.booking_id) if link.booking_id else None
    waiting = request.query_params.get("waitlist")
    if booking is None and not waiting:
        return RedirectResponse(url=f"/t/{token}", status_code=303)
    return templates.TemplateResponse(
        request=request, name="tables/done.html", headers=NO_CACHE,
        context={"biz": biz, "booking": booking, "waitlist": bool(waiting),
                 "when": T.when_label(booking.start_at) if booking else None})
