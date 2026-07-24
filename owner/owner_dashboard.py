from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import date, datetime, timedelta

from database import (
    ActivityEvent,
    Appointment,
    Business,
    Service,
    get_db,
)
from owner.owner_auth import resolve_owner_and_business

router = APIRouter()

templates = Jinja2Templates(directory="templates")

@router.get("/owner")
async def owner_root(request: Request, db: Session = Depends(get_db)):
    owner, business = resolve_owner_and_business(request, db)

    if owner and business:
        return RedirectResponse(url="/owner/dashboard", status_code=303)

    return RedirectResponse(url="/owner/login", status_code=303)

@router.get("/owner/dashboard")
async def owner_dashboard(request: Request, db: Session = Depends(get_db)):
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        return RedirectResponse(url="/owner/login", status_code=303)

    appointments = (
        db.query(Appointment)
        .filter(Appointment.business_id == business.id)
        .order_by(Appointment.appointment_time.asc())
        .all()
    )

    today = date.today()
    today_start = datetime.combine(today, datetime.min.time())
    tomorrow_start = today_start + timedelta(days=1)

    todays_appointments = [
        a for a in appointments
        if today_start <= a.appointment_time < tomorrow_start
    ]

    pending_count = sum(1 for a in appointments if a.status == "pending")
    confirmed_count = sum(1 for a in appointments if a.status == "confirmed")
    completed_count = sum(1 for a in appointments if a.status == "completed")
    cancelled_count = sum(1 for a in appointments if a.status == "cancelled")
    no_show_count = sum(1 for a in appointments if a.status == "no_show")
    service_count = db.query(Service).filter(Service.business_id == business.id).count()

    recent_activity = (
        db.query(ActivityEvent)
        .filter(ActivityEvent.business_id == business.id)
        .order_by(ActivityEvent.created_at.desc())
        .limit(10)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="owner/dashboard.html",
        context={
            "request": request,
            "active_page": "dashboard",
            "business": business,
            "appointments": appointments,
            "todays_appointments": todays_appointments,
            "pending_count": pending_count,
            "confirmed_count": confirmed_count,
            "completed_count": completed_count,
            "cancelled_count": cancelled_count,
            "no_show_count": no_show_count,
            "service_count": service_count,
            "recent_activity": recent_activity,
        },
    )