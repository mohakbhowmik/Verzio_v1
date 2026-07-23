from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import get_db, Appointment

router = APIRouter(
    prefix="/admin/appointments",
    tags=["Admin Appointments"]
)

templates = Jinja2Templates(directory="templates")


@router.get("")
def appointments_page(
    request: Request,
    db: Session = Depends(get_db)
):
    appointments = (
        db.query(Appointment)
        .order_by(Appointment.appointment_time.desc())
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="appointments.html",
        context={
            "active_page": "appointments",
            "appointments": appointments,
        }
    )