from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates

from database import (
    engine,
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


def _resolve_database_file() -> Path:
    db_url = engine.url.database or "verzio_saas.db"
    db_path = Path(db_url)

    if not db_path.is_absolute():
        db_path = Path(__file__).resolve().parents[1] / db_path

    return db_path


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


@router.get("/backup-db")
def backup_database():
    db_path = _resolve_database_file()

    if not db_path.exists():
        raise HTTPException(status_code=404, detail="Database file not found.")

    return FileResponse(
        path=str(db_path),
        filename=db_path.name,
        media_type="application/x-sqlite3",
    )