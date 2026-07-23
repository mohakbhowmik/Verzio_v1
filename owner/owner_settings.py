from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import Business, get_db

router = APIRouter(prefix="/owner/settings", tags=["owner-settings"])
templates = Jinja2Templates(directory="templates")

WEEK_DAYS = [
    ("mon", "Monday"),
    ("tue", "Tuesday"),
    ("wed", "Wednesday"),
    ("thu", "Thursday"),
    ("fri", "Friday"),
    ("sat", "Saturday"),
    ("sun", "Sunday"),
]


def _resolve_business(request: Request, db: Session) -> Business | None:
    owner_phone = (
        request.cookies.get("verzio_owner_phone")
        or request.headers.get("x-verzio-owner-phone")
        or ""
    ).strip()

    if not owner_phone:
        return None

    return (
        db.query(Business)
        .filter(Business.manager_phone_number == owner_phone)
        .first()
    )


def _business_hours_rows(business: Business | None) -> list[dict]:
    existing = (business.operational_hours if business and business.operational_hours else {}) or {}
    rows = []

    for key, label in WEEK_DAYS:
        pair = existing.get(key)
        rows.append(
            {
                "key": key,
                "label": label,
                "open": pair[0] if pair else "09:00",
                "close": pair[1] if pair else "18:00",
                "closed": pair is None,
            }
        )

    return rows


def _build_operational_hours(form) -> dict:
    hours = {}
    for key, _label in WEEK_DAYS:
        if form.get(f"hours_{key}_enabled") == "on":
            open_time = form.get(f"hours_{key}_open") or "09:00"
            close_time = form.get(f"hours_{key}_close") or "18:00"
            hours[key] = [open_time, close_time]
    return hours


@router.get("")
async def owner_settings_page(
    request: Request,
    db: Session = Depends(get_db),
):
    business = _resolve_business(request, db)

    return templates.TemplateResponse(
        request=request,
        name="owner/settings.html",
        context={
            "active_page": "settings",
            "business": business,
            "hours_rows": _business_hours_rows(business),
            "message": None,
            "error": None,
        },
    )


@router.post("")
async def owner_settings_save(
    request: Request,
    db: Session = Depends(get_db),
):
    business = _resolve_business(request, db)
    if not business:
        return templates.TemplateResponse(
            request=request,
            name="owner/settings.html",
            context={
                "active_page": "settings",
                "business": None,
                "hours_rows": [],
                "message": None,
                "error": "Unable to resolve the authenticated owner tenant.",
            },
        )

    form = await request.form()

    business.name = (form.get("business_name") or "").strip() or business.name
    business.manager_phone_number = (form.get("phone_number") or "").strip() or business.manager_phone_number
    business.timezone = (form.get("timezone") or "UTC").strip() or "UTC"
    business.accepting_bookings = form.get("accept_online_bookings") == "on"
    business.enable_service_selection = form.get("show_services_during_booking") == "on"
    business.max_parallel_bookings = int(form.get("max_parallel_bookings") or business.max_parallel_bookings or 1)
    business.slot_interval = int(form.get("slot_duration") or business.slot_interval or 30)
    business.advance_booking_days = int(form.get("advance_booking_days") or business.advance_booking_days or 14)
    business.approval_mode = "manual" if form.get("require_owner_approval") == "on" else "automatic"
    business.operational_hours = _build_operational_hours(form)

    notification_preferences = business.notification_preferences or {}
    notification_preferences["whatsapp_owner"] = bool(notification_preferences.get("whatsapp_owner"))
    notification_preferences["whatsapp_customer"] = form.get("send_booking_confirmation") == "on"
    business.notification_preferences = notification_preferences

    db.commit()

    return templates.TemplateResponse(
        request=request,
        name="owner/settings.html",
        context={
            "active_page": "settings",
            "business": business,
            "hours_rows": _business_hours_rows(business),
            "message": "Settings saved successfully.",
            "error": None,
        },
    )