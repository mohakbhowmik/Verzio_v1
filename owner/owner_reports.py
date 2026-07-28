import os
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import ActivityEvent, Appointment, Business, get_db
from reports import generate_excel_report  # Updated name
from owner.owner_auth import is_mobile, resolve_owner_and_business

router = APIRouter(prefix="/owner/reports", tags=["owner-reports"])
templates = Jinja2Templates(directory="templates")




@router.get("")
async def owner_reports_page(
    request: Request,
    db: Session = Depends(get_db),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
):
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        return RedirectResponse(url="/owner/login", status_code=303)
        
    selected_from = from_date or date.today()
    selected_to = to_date or selected_from

    selected_start = datetime.combine(
        selected_from,
        datetime.min.time()
    )

    selected_end = datetime.combine(
        selected_to,
        datetime.max.time()
    )

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
            .filter(
                ActivityEvent.business_id == business.id,
                ActivityEvent.created_at >= selected_start,
                ActivityEvent.created_at <= selected_end,
            )
            .order_by(ActivityEvent.created_at.desc())
            .all()
        )


    total_count = len(appointments)
    pending_count = sum(1 for appointment in appointments if appointment.status == "pending")
    confirmed_count = sum(1 for appointment in appointments if appointment.status == "confirmed")
    completed_count = sum(1 for appointment in appointments if appointment.status == "completed")
    cancelled_count = sum(1 for appointment in appointments if appointment.status == "cancelled")
    no_show_count = sum(1 for appointment in appointments if appointment.status == "no_show")
    # Revenue is computed strictly for confirmed and completed bookings (no_show excluded)
    revenue = sum(
        appointment.service.price or 0
        for appointment in appointments
        if appointment.status in {"confirmed", "completed"} and appointment.service
    )

    template_name = "owner/reports_mobile.html" if is_mobile(request) else "owner/reports.html"

    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_page": "reports",
            "business": business,
            "selected_from": selected_from.isoformat(),
            "selected_to": selected_to.isoformat(),
            "total_count": total_count,
            "pending_count": pending_count,
            "confirmed_count": confirmed_count,
            "completed_count": completed_count,
            "cancelled_count": cancelled_count,
            "no_show_count": no_show_count,
            "revenue": revenue,
            "recent_activity": recent_activity,
        },
    )


@router.get("/export")
async def export_owner_daily_report(
    request: Request,
    db: Session = Depends(get_db),
    from_date: date | None = Query(default=None), # Added
    to_date: date | None = Query(default=None),   # Added
):
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Default to today if no dates provided
    export_from = from_date or date.today()
    export_to = to_date or export_from

    # Updated function call with dates
    report_path = generate_excel_report(
        business_id=business.id,
        business_name=business.name,
        db=db,
        from_date=export_from,
        to_date=export_to
    )
    
    return FileResponse(
        path=report_path,
        filename=os.path.basename(report_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )