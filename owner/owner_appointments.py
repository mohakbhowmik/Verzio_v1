from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import Appointment, Business, Service, get_db

router = APIRouter(prefix="/owner/appointments", tags=["owner-appointments"])
templates = Jinja2Templates(directory="templates")


@router.get("")
async def owner_appointments_page(
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

    appointments = []
    if business:
        appointments = (
            db.query(Appointment)
            .filter(Appointment.business_id == business.id)
            .join(Service, Appointment.service_id == Service.id, isouter=True)
            .order_by(Appointment.appointment_time.desc())
            .all()
        )

    return templates.TemplateResponse(
        request=request,
        name="owner/appointments.html",
        context={
            "active_page": "appointments",
            "business": business,
            "appointments": appointments,
        },
    )