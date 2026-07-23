from datetime import date

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from database import (
    SessionLocal,
    Business,
    Appointment,
    ActivityEvent,
)

router = APIRouter(
    prefix="/admin/system",
    tags=["Admin System"],
)

templates = Jinja2Templates(directory="templates")


@router.get("")
def system_page(request: Request):
    db = SessionLocal()

    try:
        businesses = db.query(Business).count()

        todays_appointments = (
            db.query(Appointment)
            .filter(Appointment.appointment_time >= date.today())
            .count()
        )

        recent_events = (
            db.query(ActivityEvent)
            .order_by(ActivityEvent.created_at.desc())
            .limit(50)
            .all()
        )

        return templates.TemplateResponse(
            request=request,
            name="system.html",
            context={
                "active_page": "system",
                "businesses": businesses,
                "todays_appointments": todays_appointments,
                "recent_events": recent_events,
                "api_status": "Healthy",
                "database_status": "Connected",
            },
        )

    finally:
        db.close()