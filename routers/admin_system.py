# routers/admin_system.py

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter(
    prefix="/admin/system",
    tags=["Admin System"]
)

templates = Jinja2Templates(directory="templates")


@router.get("")
def system_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="system.html",
        context={
            "active_page": "system"
        }
    )
    