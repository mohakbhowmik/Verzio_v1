"""
admin_businesses.py
================================================================================
VERZIO STUDIO — ADMIN: BUSINESS MANAGEMENT MODULE
================================================================================
CRUD screens for Business tenants, rendered with Jinja2 + Bootstrap 5.
Reuses the existing `Business` SQLAlchemy model as-is (no schema changes).
Fully self-contained: does not import from or alter booking_engine.py,
booking_runtime.py, or the /webhook route in server.py.
"""

import logging
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db, Business

logger = logging.getLogger("VERZIO_ADMIN_BUSINESSES")

router = APIRouter(prefix="/admin/businesses", tags=["admin-businesses"])
templates = Jinja2Templates(directory="templates")

# Canonical day order used to build/read the operational_hours JSON column
WEEK_DAYS = [
    ("mon", "Monday"),
    ("tue", "Tuesday"),
    ("wed", "Wednesday"),
    ("thu", "Thursday"),
    ("fri", "Friday"),
    ("sat", "Saturday"),
    ("sun", "Sunday"),
]


# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------

def _hours_rows_from_business(biz: Business | None) -> list[dict]:
    """Build the 7-day row structure the form template iterates over,
    pre-filled from an existing Business.operational_hours JSON dict
    (or sensible defaults for a brand new business)."""
    existing = (biz.operational_hours if biz else None) or {}
    rows = []
    for key, label in WEEK_DAYS:
        pair = existing.get(key)
        rows.append({
            "key": key,
            "label": label,
            "enabled": pair is not None,
            "open": pair[0] if pair else "09:00",
            "close": pair[1] if pair else "18:00",
        })
    return rows


def _build_operational_hours(form) -> dict:
    """Reconstruct the operational_hours JSON dict from submitted form fields
    named hours_<day>_enabled / hours_<day>_open / hours_<day>_close."""
    hours = {}
    for key, _label in WEEK_DAYS:
        if form.get(f"hours_{key}_enabled") == "on":
            open_t = form.get(f"hours_{key}_open") or "09:00"
            close_t = form.get(f"hours_{key}_close") or "18:00"
            hours[key] = [open_t, close_t]
    return hours


def _holidays_to_text(biz: Business | None) -> str:
    if not biz or not biz.holidays:
        return ""
    return "\n".join(biz.holidays)




def _parse_holidays(raw: str) -> list:
    if not raw:
        return []
    parts = [p.strip() for p in raw.replace(",", "\n").splitlines()]
    return [p for p in parts if p]


def _business_initials(name: str) -> str:
    words = name.split()

    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()

    return name[:2].upper()


def _form_context(biz: Business | None = None, error: str | None = None) -> dict:
    """Shared context builder for the create/edit form template."""
    return {
        "active_page": "businesses",
        "business": biz,
        "hours_rows": _hours_rows_from_business(biz),
        "holidays_text": _holidays_to_text(biz),
        "error": error,
    }


# ------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------

@router.get("")
async def list_businesses(request: Request, db: Session = Depends(get_db)):
    businesses = db.query(Business).order_by(Business.name.asc()).all()

    for b in businesses:
        # UI helpers
        b.initials = _business_initials(b.name)

        if b.accepting_bookings:
            b.status_class = "status-active"
            b.status_text = "Accepting"
        else:
            b.status_class = "status-inactive"
            b.status_text = "Paused"
        
    return templates.TemplateResponse(
        request=request,
        name="businesses.html",
        context={
            "active_page": "businesses",
            "businesses": businesses,
        },
    )


@router.get("/new")
async def new_business_form(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="business_form.html",
        context=_form_context(biz=None) | {"request": request, "form_action": "/admin/businesses"},
    )


@router.post("")
async def create_business(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    biz = Business(
        name=form.get("name", "").strip(),
        whatsapp_business_phone_number_id=form.get("whatsapp_business_phone_number_id", "").strip(),
        manager_phone_number=form.get("manager_phone_number", "").strip(),
        timezone=form.get("timezone", "UTC").strip() or "UTC",
        is_active=form.get("is_active") == "on",
        accepting_bookings=form.get("accepting_bookings") == "on",
        max_parallel_bookings=int(form.get("max_parallel_bookings") or 1),
        operational_hours=_build_operational_hours(form),
        holidays=_parse_holidays(form.get("holidays", "")),
        slot_interval=int(form.get("slot_interval") or 30),
        advance_booking_days=int(form.get("advance_booking_days") or 14),
        enable_service_selection=form.get("enable_service_selection") == "on",
        approval_mode=form.get("approval_mode", "manual"),
        notification_preferences={
            "whatsapp_owner": form.get("notify_owner") == "on",
            "whatsapp_customer": form.get("notify_customer") == "on",
        },
    )

    try:
        db.add(biz)
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.warning("Duplicate whatsapp_business_phone_number_id on create: %s",
                        form.get("whatsapp_business_phone_number_id"))
        return templates.TemplateResponse(
            request=request,
            name="business_form.html",
            context=_form_context(
                biz=None,
                error="That WhatsApp business phone number ID is already in use by another business.",
            ) | {"form_action": "/admin/businesses"},
            status_code=400,
        )

    logger.info("Created business '%s' (id=%s)", biz.name, biz.id)
    return RedirectResponse(url="/admin/businesses", status_code=303)


@router.get("/{business_id}/edit")
async def edit_business_form(business_id: int, request: Request, db: Session = Depends(get_db)):
    biz = db.get(Business, business_id)
    if not biz:
        raise HTTPException(status_code=404, detail="Business not found.")

    return templates.TemplateResponse(
        request=request,
        name="business_form.html",
        context=_form_context(biz=biz) | {"form_action": f"/admin/businesses/{biz.id}"},
    )


@router.post("/{business_id}")
async def update_business(business_id: int, request: Request, db: Session = Depends(get_db)):
    biz = db.get(Business, business_id)
    if not biz:
        raise HTTPException(status_code=404, detail="Business not found.")

    form = await request.form()

    biz.name = form.get("name", "").strip()
    biz.whatsapp_business_phone_number_id = form.get("whatsapp_business_phone_number_id", "").strip()
    biz.manager_phone_number = form.get("manager_phone_number", "").strip()
    biz.timezone = form.get("timezone", "UTC").strip() or "UTC"
    biz.is_active = form.get("is_active") == "on"
    biz.accepting_bookings = form.get("accepting_bookings") == "on"
    biz.max_parallel_bookings = int(form.get("max_parallel_bookings") or 1)
    biz.operational_hours = _build_operational_hours(form)
    biz.holidays = _parse_holidays(form.get("holidays", ""))
    biz.slot_interval = int(form.get("slot_interval") or 30)
    biz.advance_booking_days = int(form.get("advance_booking_days") or 14)
    biz.enable_service_selection = form.get("enable_service_selection") == "on"
    biz.approval_mode = form.get("approval_mode", "manual")
    biz.notification_preferences = {
        "whatsapp_owner": form.get("notify_owner") == "on",
        "whatsapp_customer": form.get("notify_customer") == "on",
    }



    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.warning("Duplicate whatsapp_business_phone_number_id on update: %s",
                        form.get("whatsapp_business_phone_number_id"))
        return templates.TemplateResponse(
            request=request,
            name="business_form.html",
            context=_form_context(
                biz=biz,
                error="That WhatsApp business phone number ID is already in use by another business.",
            ) | {"form_action": f"/admin/businesses/{biz.id}"},
            status_code=400,
        )

    logger.info("Updated business '%s' (id=%s)", biz.name, biz.id)
    return RedirectResponse(url="/admin/businesses", status_code=303)


@router.post("/{business_id}/toggle-active")
def toggle_business_active(
    business_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    business = db.query(Business).filter(Business.id == business_id).first()

    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    business.is_active = not business.is_active

    db.commit()

    return RedirectResponse(
        url="/admin/businesses",
        status_code=303
    )
