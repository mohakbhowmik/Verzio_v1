"""
admin_security.py: the admin's own password, and owner password-reset links.

    GET/POST /admin/password                         change the admin password
    POST     /admin/businesses/{id}/reset-password   one-time reset link for that business's owner

See account_security.py for how recovery works.
"""
import logging
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from account_security import MIN_ADMIN_PASSWORD, RESET_LINK_HOURS, create_reset_link, password_problem, set_admin_password
from activity_log import log_event
from admin_auth import ADMIN_COOKIE, ADMIN_SESSION_HOURS, new_session_token, password_matches
from database import Business, Owner, get_db
from owner.owner_auth import _cookie_secure
from whatsapp_client import normalize_phone

logger = logging.getLogger("VERZIO_ADMIN_SECURITY")
router = APIRouter(prefix="/admin", tags=["admin-security"])
templates = Jinja2Templates(directory="templates")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://verzio.codes").rstrip("/")


def _page(request: Request, **extra):
    return templates.TemplateResponse(request=request, name="admin_password.html",
                                      context={"active_page": "password", "minimum": MIN_ADMIN_PASSWORD, **extra})


@router.get("/password", response_class=HTMLResponse)
def password_page(request: Request):
    return _page(request, done=request.query_params.get("done") == "1")


@router.post("/password", response_class=HTMLResponse)
def change_password(request: Request, current_password: str = Form(""), new_password: str = Form(""),
                    confirm_password: str = Form("")):
    if not password_matches(current_password):
        return _page(request, error="Your current password isn't right.")
    problem = password_problem(new_password, confirm_password, MIN_ADMIN_PASSWORD)
    if problem is None and password_matches(new_password):
        problem = "Choose a different password from the current one."
    if problem:
        return _page(request, error=problem)
    set_admin_password(new_password)
    logger.warning("Admin password changed")
    response = RedirectResponse(url="/admin/password?done=1", status_code=303)
    # Every other admin session is now signed out; keep this browser signed in.
    response.set_cookie(ADMIN_COOKIE, new_session_token(), max_age=int(ADMIN_SESSION_HOURS * 3600),
                        httponly=True, samesite="strict", secure=_cookie_secure(request), path="/admin")
    return response


@router.post("/businesses/{business_id}/reset-password", response_class=HTMLResponse)
def owner_reset_link(business_id: int, request: Request, db: Session = Depends(get_db)):
    business = db.get(Business, business_id)
    owner = db.query(Owner).filter(Owner.business_id == business_id).first() if business else None
    if owner is None:
        raise HTTPException(status_code=404, detail="This business has no owner login.")
    link = create_reset_link(db, owner.id)
    url = f"{PUBLIC_BASE_URL}/owner/reset/{link.token}"
    log_event(db=db, event_type="owner_password_reset_link", status="warning",
              message=f"Admin created a password reset link for owner #{owner.id}", business_id=business_id)
    phone = normalize_phone(owner.phone_number or business.manager_phone_number or "")
    text = (f"Hi {owner.full_name.split()[0] if owner.full_name else ''}, here is your link to set a new "
            f"Verzio password. It works once, for {RESET_LINK_HOURS} hours:\n{url}")
    wa_link = f"https://wa.me/{phone}?text={quote(text)}" if phone else None
    return templates.TemplateResponse(
        request=request, name="admin_reset_link.html",
        context={"active_page": "businesses", "business": business, "owner": owner, "url": url,
                 "hours": RESET_LINK_HOURS, "wa_link": wa_link},
        headers={"Cache-Control": "no-store"},
    )
