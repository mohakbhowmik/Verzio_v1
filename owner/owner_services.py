import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import Appointment, Business, Service, get_db
from owner.owner_auth import resolve_owner_and_business

logger = logging.getLogger("VERZIO_OWNER_SERVICES")

router = APIRouter(prefix="/owner/services", tags=["owner-services"])
templates = Jinja2Templates(directory="templates")



def _form_context(
    business: Business | None,
    service: Service | None = None,
    error: str | None = None,
) -> dict:
    return {
        "active_page": "services",
        "service": service,
        "businesses": [business] if business else [],
        "error": error,
    }


def _parse_price(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _service_for_business(
    service_id: int,
    business: Business,
    db: Session,
) -> Service:
    service = (
        db.query(Service)
        .filter(
            Service.id == service_id,
            Service.business_id == business.id,
        )
        .first()
    )
    if not service:
        raise HTTPException(status_code=404, detail="Service not found.")
    return service


@router.get("")
async def owner_services_page(
    request: Request,
    db: Session = Depends(get_db),
):
    owner, business = resolve_owner_and_business(request, db)

    services = []
    if business:
        services = (
            db.query(Service)
            .filter(
                Service.business_id == business.id,
                Service.is_deleted == False,
            )
            .order_by(Service.name.asc())
            .all()
        )

    return templates.TemplateResponse(
        request=request,
        name="owner/services.html",
        context={
            "active_page": "services",
            "business": business,
            "services": services,
        },
    )


@router.get("/new")
async def new_owner_service_form(request: Request, db: Session = Depends(get_db)):
    owner, business = resolve_owner_and_business(request, db)
    return templates.TemplateResponse(
        request=request,
        name="service_form.html",
        context=_form_context(business) | {
            "form_action": "/owner/services",
            "owner_portal": True,
        },
    )


@router.post("")
async def create_owner_service(request: Request, db: Session = Depends(get_db)):
    owner, business = resolve_owner_and_business(request, db)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")

    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return templates.TemplateResponse(
            request=request,
            name="service_form.html",
            context=_form_context(business, error="Service name is required.")
            | {
                "form_action": "/owner/services",
                "owner_portal": True,
            },
            status_code=400,
        )

    service = Service(
        business_id=business.id,
        name=name,
        duration=int(form.get("duration") or 30),
        price=_parse_price(form.get("price")),
        is_active=form.get("is_active") == "on",
    )
    db.add(service)
    db.commit()
    logger.info("Created owner service '%s' (id=%s)", service.name, service.id)
    return RedirectResponse(url="/owner/services", status_code=303)


@router.get("/{service_id}/edit")
async def edit_owner_service_form(
    service_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    owner, business = resolve_owner_and_business(request, db)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")

    service = _service_for_business(service_id, business, db)
    return templates.TemplateResponse(
        request=request,
        name="service_form.html",
        context=_form_context(business, service=service)
        | {
            "form_action": f"/owner/services/{service.id}",
            "owner_portal": True,
        },
    )


@router.post("/{service_id}")
async def update_owner_service(
    service_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    owner, business = resolve_owner_and_business(request, db)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")

    service = _service_for_business(service_id, business, db)
    appointment = (
        db.query(Appointment)
        .filter(Appointment.service_id == service.id)
        .first()
    )
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return templates.TemplateResponse(
            request=request,
            name="service_form.html",
            context=_form_context(
                business,
                service=service,
                error="Service name is required.",
            ) | {
                "form_action": f"/owner/services/{service.id}",
                "owner_portal": True,
            },
            status_code=400,
        )

    service.name = name
    service.duration = int(form.get("duration") or 30)
    service.price = _parse_price(form.get("price"))
    service.is_active = form.get("is_active") == "on"
    db.commit()
    logger.info("Updated owner service '%s' (id=%s)", service.name, service.id)
    return RedirectResponse(url="/owner/services", status_code=303)


@router.post("/{service_id}/toggle")
async def toggle_owner_service(
    service_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    owner, business = resolve_owner_and_business(request, db)

    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")

    service = _service_for_business(service_id, business, db)
    service.is_active = not service.is_active
    db.commit()
    logger.info("Toggled owner service '%s' (id=%s) -> %s", service.name, service.id, service.is_active)
    return RedirectResponse(url="/owner/services", status_code=303)


@router.post("/{service_id}/delete")
async def delete_owner_service(
    service_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    owner, business = resolve_owner_and_business(request, db)

    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")

    service = _service_for_business(service_id, business, db)

    appointment = (
        db.query(Appointment)
        .filter(Appointment.service_id == service.id)
        .first()
    )

    if appointment:
        service.is_deleted = True
        db.commit()

        logger.info(
            "Soft deleted owner service '%s' (id=%s)",
            service.name,
            service.id,
        )
    else:
        db.delete(service)
        db.commit()

        logger.info(
            "Deleted owner service '%s' (id=%s)",
            service.name,
            service.id,
        )

    return RedirectResponse(
        url="/owner/services",
        status_code=303,
    )



