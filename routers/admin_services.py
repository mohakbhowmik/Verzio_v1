"""
admin_services.py
================================================================================
VERZIO STUDIO — ADMIN: SERVICE MANAGEMENT MODULE
================================================================================
CRUD screens for Services, rendered with Jinja2 + Bootstrap 5.
Reuses the existing `Service` SQLAlchemy model as-is (no schema changes).
Follows the same architecture as admin_businesses.py: fully self-contained,
does not import from or alter booking_engine.py, booking_runtime.py, or the
/webhook route in server.py.
"""

import logging
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import get_db, Service, Business

logger = logging.getLogger("VERZIO_ADMIN_SERVICES")

router = APIRouter(prefix="/admin/services", tags=["admin-services"])
templates = Jinja2Templates(directory="templates")


# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------

def _form_context(db: Session, svc: Service | None = None, error: str | None = None) -> dict:
    """Shared context builder for the create/edit form template."""
    businesses = db.query(Business).order_by(Business.name.asc()).all()
    return {
        "active_page": "services",
        "service": svc,
        "businesses": businesses,
        "error": error,
    }


def _parse_price(raw: str):
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# ------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------

@router.get("")
async def list_services(request: Request, db: Session = Depends(get_db)):
    services = (
        db.query(Service)
        .join(Business, Service.business_id == Business.id)
        .order_by(Business.name.asc(), Service.name.asc())
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name="services.html",
        context={
            "active_page": "services",
            "services": services,
        },
    )


@router.get("/new")
async def new_service_form(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request,
        name="admin_service_form.html",
        context=_form_context(db, svc=None) | {"form_action": "/admin/services"},
    )


@router.post("")
async def create_service(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    name = form.get("name", "").strip()
    business_id_raw = form.get("business_id", "")

    if not name or not business_id_raw:
        return templates.TemplateResponse(
            request=request,
            name="admin_service_form.html",
            context=_form_context(db, svc=None, error="Name and business are required.")
            | {"form_action": "/admin/services"},
            status_code=400,
        )

    business = db.get(Business, int(business_id_raw))
    if not business:
        return templates.TemplateResponse(
            request=request,
            name="admin_service_form.html",
            context=_form_context(db, svc=None, error="Selected business could not be found.")
            | {"form_action": "/admin/services"},
            status_code=400,
        )

    svc = Service(
        business_id=business.id,
        name=name,
        duration=int(form.get("duration") or 30),
        price=_parse_price(form.get("price", "")),
        is_active=form.get("is_active") == "on",
    )

    db.add(svc)
    db.commit()

    logger.info("Created service '%s' (id=%s) for business_id=%s", svc.name, svc.id, svc.business_id)
    return RedirectResponse(url="/admin/services", status_code=303)


@router.get("/{service_id}/edit")
async def edit_service_form(service_id: int, request: Request, db: Session = Depends(get_db)):
    svc = db.get(Service, service_id)
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found.")

    return templates.TemplateResponse(
        request=request,
        name="admin_service_form.html",
        context=_form_context(db, svc=svc) | {"form_action": f"/admin/services/{svc.id}"},
    )


@router.post("/{service_id}")
async def update_service(service_id: int, request: Request, db: Session = Depends(get_db)):
    svc = db.get(Service, service_id)
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found.")

    form = await request.form()

    name = form.get("name", "").strip()
    business_id_raw = form.get("business_id", "")

    if not name or not business_id_raw:
        return templates.TemplateResponse(
            request=request,
            name="admin_service_form.html",
            context=_form_context(db, svc=svc, error="Name and business are required.")
            | {"form_action": f"/admin/services/{svc.id}"},
            status_code=400,
        )

    business = db.get(Business, int(business_id_raw))
    if not business:
        return templates.TemplateResponse(
            request=request,
            name="admin_service_form.html",
            context=_form_context(db, svc=svc, error="Selected business could not be found.")
            | {"form_action": f"/admin/services/{svc.id}"},
            status_code=400,
        )

    svc.name = name
    svc.business_id = business.id
    svc.duration = int(form.get("duration") or 30)
    svc.price = _parse_price(form.get("price", ""))
    svc.is_active = form.get("is_active") == "on"

    db.commit()

    logger.info("Updated service '%s' (id=%s)", svc.name, svc.id)
    return RedirectResponse(url="/admin/services", status_code=303)


@router.post("/{service_id}/toggle")
async def toggle_service_active(service_id: int, db: Session = Depends(get_db)):
    svc = db.get(Service, service_id)
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found.")

    svc.is_active = not svc.is_active
    db.commit()

    logger.info("Toggled is_active for '%s' (id=%s) -> %s", svc.name, svc.id, svc.is_active)
    return RedirectResponse(url="/admin/services", status_code=303)
