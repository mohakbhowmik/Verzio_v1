from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter(
    prefix="/admin/subscriptions",
    tags=["Admin Subscriptions"]
)

templates = Jinja2Templates(directory="templates")


@router.get("")
def subscriptions_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="subscriptions.html",
        context={
            "active_page": "subscriptions"
        }
    )