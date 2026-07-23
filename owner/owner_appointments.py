from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from activity_log import log_event
from database import Appointment, Business, Service, get_db

router = APIRouter(prefix="/owner/appointments", tags=["owner-appointments"])
templates = Jinja2Templates(directory="templates")

ALLOWED_STATUSES = {"pending", "confirmed", "completed", "cancelled"}
ACTION_TARGETS = {
    "approve": ("pending", "confirmed"),
    "reject": ("pending", "cancelled"),
    "complete": ("confirmed", "completed"),
    "cancel": ("confirmed", "cancelled"),
}


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


@router.get("")
async def owner_appointments_page(
    request: Request,
    db: Session = Depends(get_db),
):
    business = _resolve_business(request, db)
    search = request.query_params.get("search", "").strip()
    status = request.query_params.get("status", "all").strip().lower()
    sort = request.query_params.get("sort", "newest").strip().lower()

    appointments = []
    if business:
        query = (
            db.query(Appointment)
            .filter(Appointment.business_id == business.id)
            .join(Service, Appointment.service_id == Service.id, isouter=True)
        )

        if search:
            search_value = f"%{search}%"
            query = query.filter(
                (Appointment.customer_name.ilike(search_value))
                | (Appointment.customer_phone.ilike(search_value))
            )

        if status in ALLOWED_STATUSES:
            query = query.filter(Appointment.status == status)
        else:
            status = "all"

        appointments = (
            query
            .order_by(
                Appointment.appointment_time.asc()
                if sort == "oldest"
                else Appointment.appointment_time.desc()
            )
            .all()
        )

    return templates.TemplateResponse(
        request=request,
        name="owner/appointments.html",
        context={
            "active_page": "appointments",
            "business": business,
            "appointments": appointments,
            "search": search,
            "status": status,
            "sort": sort if sort in {"oldest", "newest"} else "newest",
        },
    )


@router.post("/{appointment_id}/{action}")
async def update_owner_appointment(
    appointment_id: int,
    action: str,
    request: Request,
    db: Session = Depends(get_db),
):
    business = _resolve_business(request, db)
    transition = ACTION_TARGETS.get(action)

    if not business or not transition:
        raise HTTPException(status_code=404, detail="Appointment action not found.")

    appointment = (
        db.query(Appointment)
        .filter(
            Appointment.id == appointment_id,
            Appointment.business_id == business.id,
        )
        .first()
    )

    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found.")

    expected_status, next_status = transition
    if appointment.status != expected_status:
        raise HTTPException(
            status_code=409,
            detail="This appointment cannot be changed from its current status.",
        )

    appointment.status = next_status
    db.commit()

    if next_status == "completed":
        log_event(
            db=db,
            event_type="appointment_completed",
            status="success",
            message=f"Appointment #{appointment.id} completed",
            business_id=appointment.business_id,
        )

    query = request.query_params
    redirect_url = "/owner/appointments"
    if query:
        redirect_url = f"{redirect_url}?{query}"
    return RedirectResponse(url=redirect_url, status_code=303)