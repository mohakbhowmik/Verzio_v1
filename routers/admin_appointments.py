# routers/admin_system.py

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter(
    prefix="/admin/appointments",
    tags=["Admin Appointments"]
)

templates = Jinja2Templates(directory="templates")


@router.get("")
def appointments_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="appointments.html",
        context={
            "active_page": "appointments"
        }
    )
    