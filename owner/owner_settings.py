from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from owner.owner_auth import is_mobile, resolve_owner_and_business
from fastapi.responses import RedirectResponse

from whatsapp_client import canonical_phone, is_real_phone
from hours import build_hours, daily_break
from database import Business, get_db

router = APIRouter(prefix="/owner/settings", tags=["owner-settings"])
templates = Jinja2Templates(directory="templates")
templates.env.globals["daily_break"] = daily_break

WEEK_DAYS = [
    ("mon", "Monday"),
    ("tue", "Tuesday"),
    ("wed", "Wednesday"),
    ("thu", "Thursday"),
    ("fri", "Friday"),
    ("sat", "Saturday"),
    ("sun", "Sunday"),
]





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
    return build_hours(form)    # includes the optional daily break (hours.py)


@router.get("")
async def owner_settings_page(
    request: Request,
    db: Session = Depends(get_db),
):
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        return RedirectResponse(url="/owner/login", status_code=303)

    template_name = "owner/settings_mobile.html" if is_mobile(request) else "owner/settings.html"

    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_page": "settings",
            "owner": owner,
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
    owner, business = resolve_owner_and_business(request, db)
    if not business:
        template_name = "owner/settings_mobile.html" if is_mobile(request) else "owner/settings.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context={
                "active_page": "settings",
                "owner": owner,
                "business": None,
                "hours_rows": [],
                "message": None,
                "error": "Unable to resolve the authenticated owner tenant.",
            },
        )

    form = await request.form()

    business.name = (form.get("business_name") or "").strip() or business.name
    business.timezone = (form.get("timezone") or "UTC").strip() or "UTC"
    phone_error = None
    submitted_phone = (form.get("phone_number") or "").strip()
    if submitted_phone:
        manager = canonical_phone(submitted_phone, business.timezone)
        if is_real_phone(manager):
            business.manager_phone_number = manager
        else:
            phone_error = "That WhatsApp number doesn't look right. Use the full number, e.g. +91 98765 43210. Other settings were saved."
    business.accepting_bookings = form.get("accept_online_bookings") == "on"
    business.enable_service_selection = form.get("show_services_during_booking") == "on"
    business.max_parallel_bookings = int(form.get("max_parallel_bookings") or business.max_parallel_bookings or 1)
    allowed_intervals = {5, 10, 15, 30, 45, 60, 90, 120}
    try:
        submitted_interval = int(form.get("slot_duration") or 30)
        business.slot_interval = submitted_interval if submitted_interval in allowed_intervals else 30
    except (ValueError, TypeError):
        business.slot_interval = 30
    business.advance_booking_days = int(form.get("advance_booking_days") or business.advance_booking_days or 14)
    business.approval_mode = "manual" if form.get("require_owner_approval") == "on" else "automatic"
    business.operational_hours = _build_operational_hours(form)

    notification_preferences = business.notification_preferences or {}
    notification_preferences["whatsapp_owner"] = bool(notification_preferences.get("whatsapp_owner"))
    notification_preferences["whatsapp_customer"] = form.get("send_booking_confirmation") == "on"
    business.notification_preferences = notification_preferences

    db.commit()

    template_name = "owner/settings_mobile.html" if is_mobile(request) else "owner/settings.html"

    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_page": "settings",
            "owner": owner,
            "business": business,
            "hours_rows": _business_hours_rows(business),
            "message": None if phone_error else "Settings saved successfully.",
            "error": phone_error,
        },
    )