from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter()

templates = Jinja2Templates(directory="templates")


@router.get("/owner")
async def owner_dashboard(request: Request):
    return templates.TemplateResponse(
    request=request,
    name="owner/dashboard.html",
    context={}
)