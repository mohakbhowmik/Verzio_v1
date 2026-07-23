from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import ActivityEvent, Appointment, Business, get_db

router = APIRouter(prefix="/owner/reports", tags=["owner-reports"])
templates = Jinja2Templates(directory="templates")


@router.get("")
async def owner_reports_page(
    request: Request,
    db: Session = Depends(get_db),
):
    owner_phone = (
        request.cookies.get("verzio_owner_phone")
        or request.headers.get("x-verzio-owner-phone")
        or ""
    ).strip()

    business = None
    if owner_phone:
        business = (
            db.query(Business)
            .filter(Business.manager_phone_number == owner_phone)
            .first()
        )

    today = date.today()
    today_start = datetime.combine(today, datetime.min.time())
    tomorrow_start = today_start + timedelta(days=1)

    appointments = []
    recent_activity = []
    if business:
        appointments = (
            db.query(Appointment)
            .filter(Appointment.business_id == business.id)
            .order_by(Appointment.appointment_time.asc())
            .all()
        )

        recent_activity = (
            db.query(ActivityEvent)
            .filter(ActivityEvent.business_id == business.id)
            .order_by(ActivityEvent.created_at.desc())
            .limit(10)
            .all()
        )

    todays_count = sum(
        1
        for appointment in appointments
        if today_start <= appointment.appointment_time < tomorrow_start
    )

    pending_count = sum(1 for appointment in appointments if appointment.status == "pending")
    confirmed_count = sum(1 for appointment in appointments if appointment.status == "confirmed")
    completed_count = sum(1 for appointment in appointments if appointment.status == "completed")

    return templates.TemplateResponse(
        request=request,
        name="owner/reports.html",
        context={
            "active_page": "reports",
            "business": business,
            "todays_count": todays_count,
            "pending_count": pending_count,
            "confirmed_count": confirmed_count,
            "completed_count": completed_count,
            "recent_activity": recent_activity,
        },
    )