from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import Business, Service, get_db

router = APIRouter(prefix="/owner/services", tags=["owner-services"])
templates = Jinja2Templates(directory="templates")


@router.get("")
async def owner_services_page(
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

    services = []
    if business:
        services = (
            db.query(Service)
            .filter(Service.business_id == business.id)
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