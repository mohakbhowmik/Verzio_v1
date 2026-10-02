"""
owner_appointments.py
================================================================================
VERZIO STUDIO — OWNER PORTAL: APPOINTMENTS
================================================================================
- List / filter / sort the tenant's appointments.
- Status actions (approve, reject, complete, cancel, no-show) with valid
  transitions only. Approve / reject / cancel send the customer a WhatsApp
  update in the background, so the owner's page responds instantly.
- Manual booking (walk-ins and phone calls) at /owner/appointments/new.
  Goes through the booking engine so the slot is blocked on WhatsApp in real
  time. An explicit override exists for customers who are already in the chair.
"""
import logging
from datetime import datetime, timedelta

import pytz
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from activity_log import log_event
from booking_engine import BookingEngineException, VerzioSaaSEngine
from database import Appointment, Business, Service, get_db
from owner.owner_auth import is_mobile, resolve_owner_and_business
from whatsapp_client import build_status_message, is_real_phone, normalize_phone, send_whatsapp

logger = logging.getLogger("VERZIO_OWNER_APPOINTMENTS")

router = APIRouter(prefix="/owner/appointments", tags=["owner-appointments"])
templates = Jinja2Templates(directory="templates")

ALLOWED_STATUSES = {"pending", "confirmed", "completed", "cancelled", "no_show"}
ACTION_TARGETS = {
    "approve": ("pending", "confirmed"),
    "reject": ("pending", "cancelled"),
    "complete": ("confirmed", "completed"),
    "cancel": ("confirmed", "cancelled"),
    "no_show": ("confirmed", "no_show"),
}
STATUS_EVENTS = {
    "confirmed": ("booking_confirmed", "success"),
    "cancelled": ("booking_cancelled", "warning"),
    "completed": ("appointment_completed", "success"),
    "no_show": ("appointment_no_show", "warning"),
}

# Stored when the owner logs a walk-in without a phone number (column is NOT NULL).
WALK_IN_PHONE = "walk-in"

MANUAL_BOOKING_ERRORS = {
    "ERR_CAPACITY": "That time is already fully booked. Pick another time, or tick the override if the customer is already here.",
    "ERR_OUTSIDE_HOURS": "That time is outside your opening hours (or the service runs past closing). Tick the override to save it anyway.",
    "ERR_HOLIDAY": "You're marked as closed on that date. Tick the override to save it anyway.",
    "ERR_TENANT_LOCKED": "WhatsApp bookings are paused for your business. Tick the override to save this booking anyway, or turn bookings back on in Settings.",
    "ERR_INVALID_SERVICE": "That service isn't active any more. Choose another service.",
}


# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------

def _now_local(business: Business) -> datetime:
    try:
        tz = pytz.timezone(business.timezone or "UTC")
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC
    return datetime.now(tz).replace(tzinfo=None)


def _next_slot_time(business: Business) -> str:
    interval = max(int(business.slot_interval or 30), 5)
    now = _now_local(business).replace(second=0, microsecond=0)
    minutes_to_add = interval - (now.minute % interval)
    return (now + timedelta(minutes=minutes_to_add)).strftime("%H:%M")


def _clean_customer_phone(raw: str, business: Business) -> str | None:
    """Blank -> walk-in marker. 10-digit Indian numbers get +91. Otherwise the
    number must include a country code. Returns None if invalid."""
    if not raw:
        return WALK_IN_PHONE
    digits = normalize_phone(raw)
    if len(digits) == 10 and (business.timezone or "") == "Asia/Kolkata":
        digits = "91" + digits
    if not 11 <= len(digits) <= 15:
        return None
    return digits


def _active_services(db: Session, business: Business) -> list[Service]:
    return (
        db.query(Service)
        .filter(
            Service.business_id == business.id,
            Service.is_active == True,
            Service.is_deleted == False,
        )
        .order_by(Service.name.asc())
        .all()
    )


def _booking_form_response(
    request: Request,
    db: Session,
    owner,
    business: Business,
    values: dict | None = None,
    allow_overbook: bool = False,
    error: str | None = None,
    status_code: int = 200,
):
    form_values = {
        "customer_name": "",
        "customer_phone": "",
        "service_id": "",
        "date": _now_local(business).strftime("%Y-%m-%d"),
        "time": _next_slot_time(business),
    }
    if values:
        form_values.update(values)

    return templates.TemplateResponse(
        request=request,
        name="owner/appointment_form.html",
        context={
            "active_page": "appointments",
            "owner": owner,
            "business": business,
            "services": _active_services(db, business),
            "values": form_values,
            "allow_overbook": allow_overbook,
            "error": error,
            "mobile": is_mobile(request),
        },
        status_code=status_code,
    )


def _queue_customer_update(
    background_tasks: BackgroundTasks,
    appointment: Appointment,
    business: Business,
    previous_status: str,
    new_status: str,
) -> None:
    if not is_real_phone(appointment.customer_phone):
        return
    message = build_status_message(appointment, business, previous_status, new_status)
    if not message:
        return
    background_tasks.add_task(
        send_whatsapp,
        appointment.customer_phone,
        business.whatsapp_business_phone_number_id,
        {"type": "text", "body": message},
        business.id,
    )


# ------------------------------------------------------------------------
# List
# ------------------------------------------------------------------------

@router.get("")
async def owner_appointments_page(request: Request, db: Session = Depends(get_db)):
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        return RedirectResponse(url="/owner/login", status_code=303)

    search = request.query_params.get("search", "").strip()
    status = request.query_params.get("status", "all").strip().lower()
    sort = request.query_params.get("sort", "newest").strip().lower()

    query = (
        db.query(Appointment)
        .filter(Appointment.business_id == business.id)
        .join(Service, Appointment.service_id == Service.id, isouter=True)
    )

    if search:
        like = f"%{search}%"
        query = query.filter(
            (Appointment.customer_name.ilike(like)) | (Appointment.customer_phone.ilike(like))
        )

    if status in ALLOWED_STATUSES:
        query = query.filter(Appointment.status == status)
    else:
        status = "all"

    appointments = query.order_by(
        Appointment.appointment_time.asc() if sort == "oldest" else Appointment.appointment_time.desc()
    ).all()

    template_name = "owner/appointments_mobile.html" if is_mobile(request) else "owner/appointments.html"
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_page": "appointments",
            "owner": owner,
            "business": business,
            "appointments": appointments,
            "search": search,
            "status": status,
            "sort": sort if sort in {"oldest", "newest"} else "newest",
        },
    )


# ------------------------------------------------------------------------
# Manual booking (walk-ins and phone calls)
# ------------------------------------------------------------------------

@router.get("/new")
async def new_manual_appointment_form(request: Request, db: Session = Depends(get_db)):
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        return RedirectResponse(url="/owner/login", status_code=303)
    return _booking_form_response(request, db, owner, business)


@router.post("/manual")
async def create_manual_appointment(request: Request, db: Session = Depends(get_db)):
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        return RedirectResponse(url="/owner/login", status_code=303)

    form = await request.form()
    values = {
        key: (form.get(key) or "").strip()
        for key in ("customer_name", "customer_phone", "service_id", "date", "time")
    }
    allow_overbook = form.get("allow_overbook") == "on"

    def fail(message: str):
        return _booking_form_response(
            request, db, owner, business, values=values,
            allow_overbook=allow_overbook, error=message, status_code=400,
        )

    engine = VerzioSaaSEngine()

    try:
        service_id = int(values["service_id"])
    except ValueError:
        return fail("Please choose a service.")
    service = engine.get_service_for_business(db, business.id, service_id)
    if service is None:
        return fail(MANUAL_BOOKING_ERRORS["ERR_INVALID_SERVICE"])

    try:
        start = datetime.strptime(f"{values['date']} {values['time']}", "%Y-%m-%d %H:%M")
    except ValueError:
        return fail("Please enter a valid date and time.")

    phone = _clean_customer_phone(values["customer_phone"], business)
    if phone is None:
        return fail("Enter a mobile number with country code (e.g. 919876543210), or leave it blank for a walk-in.")

    name = values["customer_name"][:120] or None
    if not name and phone == WALK_IN_PHONE:
        return fail("Enter the customer's name or phone number.")

    try:
        if allow_overbook:
            # Explicit owner override: the customer is physically here, so record
            # reality even if it exceeds capacity or falls outside hours.
            appt = Appointment(
                business_id=business.id,
                service_id=service.id,
                customer_phone=phone,
                customer_name=name,
                appointment_time=start,
                status="confirmed",
            )
            db.add(appt)
            db.commit()
            db.refresh(appt)
        else:
            # Same engine and lock as WhatsApp bookings: the slot is blocked atomically.
            appt = engine.validate_and_book(
                db,
                business.whatsapp_business_phone_number_id,
                start,
                phone,
                service.id,
            )
            appt.customer_name = name
            appt.status = "confirmed"  # the owner entered it, so no approval step
            db.commit()
    except BookingEngineException as exc:
        db.rollback()
        return fail(MANUAL_BOOKING_ERRORS.get(
            exc.error_code,
            "That booking couldn't be saved. Try another time, or tick the override if the customer is already here.",
        ))

    log_event(
        db=db,
        event_type="manual_booking",
        status="success",
        message=(
            f"Owner added booking #{appt.id} for {start.strftime('%d %b %H:%M')}"
            f"{' (override)' if allow_overbook else ''}"
        ),
        business_id=business.id,
    )
    logger.info("Manual booking #%s created for business_id=%s", appt.id, business.id)
    return RedirectResponse(url="/owner/appointments?status=confirmed", status_code=303)


# ------------------------------------------------------------------------
# Status actions
# ------------------------------------------------------------------------

@router.post("/{appointment_id}/{action}")
async def update_owner_appointment(
    appointment_id: int,
    action: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        return RedirectResponse(url="/owner/login", status_code=303)

    transition = ACTION_TARGETS.get(action)
    if not transition:
        raise HTTPException(status_code=404, detail="Appointment action not found.")

    appointment = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.business_id == business.id)
        .first()
    )
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found.")

    expected_status, next_status = transition
    if appointment.status != expected_status:
        raise HTTPException(status_code=409, detail="This appointment cannot be changed from its current status.")

    previous_status = appointment.status
    appointment.status = next_status
    db.commit()

    event_type, event_status = STATUS_EVENTS[next_status]
    log_event(
        db=db,
        event_type=event_type,
        status=event_status,
        message=f"Appointment #{appointment.id} {previous_status} -> {next_status} (via portal)",
        business_id=business.id,
    )

    _queue_customer_update(background_tasks, appointment, business, previous_status, next_status)

    redirect_url = "/owner/appointments"
    if request.query_params:
        redirect_url = f"{redirect_url}?{request.query_params}"
    return RedirectResponse(url=redirect_url, status_code=303)
