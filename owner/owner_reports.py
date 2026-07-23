from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import ActivityEvent, Appointment, Business, get_db
from reports import generate_daily_excel_report

router = APIRouter(prefix="/owner/reports", tags=["owner-reports"])
templates = Jinja2Templates(directory="templates")


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
async def owner_reports_page(
    request: Request,
    db: Session = Depends(get_db),
    report_date: date | None = Query(default=None, alias="date"),
):
    business = _resolve_business(request, db)
    selected_date = report_date or date.today()
    selected_start = datetime.combine(selected_date, datetime.min.time())
    selected_end = datetime.combine(selected_date, datetime.max.time())

    appointments = []
    recent_activity = []
    if business:
        appointments = (
            db.query(Appointment)
            .filter(
                Appointment.business_id == business.id,
                Appointment.appointment_time >= selected_start,
                Appointment.appointment_time <= selected_end,
            )
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

    total_count = len(appointments)
    pending_count = sum(1 for appointment in appointments if appointment.status == "pending")
    confirmed_count = sum(1 for appointment in appointments if appointment.status == "confirmed")
    completed_count = sum(1 for appointment in appointments if appointment.status == "completed")
    cancelled_count = sum(1 for appointment in appointments if appointment.status == "cancelled")
    revenue = sum(
        appointment.service.price or 0
        for appointment in appointments
        if appointment.status in {"confirmed", "completed"} and appointment.service
    )

    return templates.TemplateResponse(
        request=request,
        name="owner/reports.html",
        context={
            "active_page": "reports",
            "business": business,
            "selected_date": selected_date.isoformat(),
            "total_count": total_count,
            "pending_count": pending_count,
            "confirmed_count": confirmed_count,
            "completed_count": completed_count,
            "cancelled_count": cancelled_count,
            "revenue": revenue,
            "recent_activity": recent_activity,
        },
    )


@router.get("/export")
async def export_owner_daily_report(
    request: Request,
    db: Session = Depends(get_db),
):
    business = _resolve_business(request, db)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")

    report_path = generate_daily_excel_report(
        business_id=business.id,
        business_name=business.name,
        db=db,
    )
    return FileResponse(
        path=report_path,
        filename=report_path.rsplit("/", 1)[-1],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )