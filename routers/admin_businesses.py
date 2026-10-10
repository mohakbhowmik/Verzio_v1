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
import secrets
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db, Business, Owner
from owner.owner_auth import create_session_token, hash_password, set_session_cookie
from activity_log import log_event
from whatsapp_client import canonical_phone, is_real_phone
from hours import build_hours, daily_break

PHONE_HELP = ("Enter the manager's WhatsApp number, e.g. +91 98765 43210. "
              "This is where booking requests are sent.")


def _placeholder_number_id() -> str:
    """Unique stand-in until the business connects WhatsApp (onboarding replaces it)."""
    return f"pending-{secrets.token_hex(4)}"

logger = logging.getLogger("VERZIO_ADMIN_BUSINESSES")

router = APIRouter(prefix="/admin/businesses", tags=["admin-businesses"])
templates = Jinja2Templates(directory="templates")
templates.env.globals["daily_break"] = daily_break

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
    """operational_hours from hours_<day>_enabled/_open/_close plus the optional
    daily break (break_start / break_end). See hours.py."""
    return build_hours(form)


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
        whatsapp_business_phone_number_id=form.get("whatsapp_business_phone_number_id", "").strip() or _placeholder_number_id(),
        manager_phone_number=canonical_phone(form.get("manager_phone_number", ""), form.get("timezone", "")),
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

    def fail(message: str):
        return templates.TemplateResponse(
            request=request,
            name="business_form.html",
            context=_form_context(biz=biz, error=message) | {"form_action": "/admin/businesses"},
            status_code=400,
        )

    if not biz.name:
        return fail("Enter the business name.")
    if not is_real_phone(biz.manager_phone_number):
        return fail(PHONE_HELP)

    owner_email = form.get("owner_email", "").strip().lower()
    owner_password = form.get("owner_password", "").strip()
    owner_name = form.get("owner_name", "").strip()

    # Check everything that must be unique BEFORE saving anything, so a clash never
    # leaves a half-created business behind.
    if submitted_number_id := form.get("whatsapp_business_phone_number_id", "").strip():
        if db.query(Business).filter(Business.whatsapp_business_phone_number_id == submitted_number_id).first():
            return fail("That WhatsApp Phone Number ID already belongs to another business. Leave it blank; it's filled in when WhatsApp connects.")
    if owner_email and db.query(Owner).filter(Owner.email.ilike(owner_email)).first():
        return fail(f"The login email {owner_email} is already used by another business's owner. "
                    f"Use a different email, e.g. name+{biz.name.split()[0].lower() if biz.name else 'shop'}@gmail.com.")
    if owner_email and len(owner_password) < 8:
        return fail("Give the owner a temporary password of at least 8 characters.")

    try:
        db.add(biz)
        db.flush()                       # gets biz.id without committing yet
        if owner_email and owner_password:
            # The owner's phone is optional for login and must be unique: the same
            # person (e.g. you) can be manager for several businesses.
            phone_taken = db.query(Owner).filter(Owner.phone_number == biz.manager_phone_number).first()
            db.add(Owner(
                business_id=biz.id,
                full_name=owner_name or biz.name,
                email=owner_email,
                phone_number=None if phone_taken else biz.manager_phone_number,
                password_hash=hash_password(owner_password),
                is_active=True,
            ))
        db.commit()                      # business and owner saved together, or neither
        db.refresh(biz)
    except IntegrityError as exc:
        db.rollback()
        logger.warning("Could not create business '%s': %s", biz.name, exc.orig)
        return fail("That business couldn't be saved because something in it is already in use "
                    "(Phone Number ID, owner email or owner phone). Check those fields and try again.")

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
    submitted_id = form.get("whatsapp_business_phone_number_id", "").strip()
    if submitted_id:                     # blank keeps the current one (set by onboarding)
        biz.whatsapp_business_phone_number_id = submitted_id
    biz.timezone = form.get("timezone", "UTC").strip() or "UTC"
    manager = canonical_phone(form.get("manager_phone_number", ""), biz.timezone)
    if not is_real_phone(manager):
        db.rollback()
        return templates.TemplateResponse(
            request=request,
            name="business_form.html",
            context=_form_context(biz=db.get(Business, business_id), error=PHONE_HELP)
                    | {"form_action": f"/admin/businesses/{business_id}"},
            status_code=400,
        )
    biz.manager_phone_number = manager
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
    db: Session = Depends(get_db)
):
    business = db.get(Business, business_id)

    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    # Toggle platform activation
    business.is_active = not business.is_active

    db.commit()

    logger.info(
        "Business '%s' platform activation changed -> %s",
        business.name,
        business.is_active
    )

    return RedirectResponse(
        url="/admin/businesses",
        status_code=303
    )


@router.post("/{business_id}/impersonate")
def impersonate_business_owner(
    business_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    owner = db.query(Owner).filter(Owner.business_id == business_id).first()
    if not owner:
        raise HTTPException(status_code=404, detail="Owner not found for this business.")

    log_event(
        db=db,
        event_type="admin_impersonation",
        status="warning",
        message=f"Admin opened owner session for owner #{owner.id}",
        business_id=business_id,
    )

    token, max_age = create_session_token(owner.id)
    response = RedirectResponse(url="/owner/dashboard", status_code=303)
    set_session_cookie(response, request, token, max_age)
    return response