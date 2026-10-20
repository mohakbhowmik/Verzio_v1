"""admin_login.py — the admin sign-in page and Log out (see admin_auth.py)."""
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from admin_auth import (
    ADMIN_COOKIE, ADMIN_SESSION_HOURS, clear_failures, client_ip, credentials_match,
    locked_out, new_session_token, record_failure, session_valid,
)
from owner.owner_auth import _cookie_secure

logger = logging.getLogger("VERZIO_ADMIN_LOGIN")
router = APIRouter(prefix="/admin", tags=["admin-login"])
templates = Jinja2Templates(directory="templates")


def _safe_next(raw: str | None) -> str:
    raw = (raw or "").strip()
    return raw if raw.startswith("/admin") and not raw.startswith("//") and raw not in ("/admin/login", "/admin/logout") else "/admin"


def _page(request: Request, error: str | None = None, notice: str | None = None, status: int = 200, next_url: str = "/admin"):
    return templates.TemplateResponse(request=request, name="admin_login.html",
                                      context={"error": error, "notice": notice, "next": next_url},
                                      status_code=status)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    next_url = _safe_next(request.query_params.get("next"))
    if session_valid(request.cookies.get(ADMIN_COOKIE)):
        return RedirectResponse(url=next_url, status_code=303)
    notice = "You're signed out." if request.query_params.get("out") else None
    return _page(request, notice=notice, next_url=next_url)


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/admin")):
    from account_security import refresh_admin_hash
    refresh_admin_hash()            # picks up reset_admin_password.py without a restart
    ip = client_ip(request)
    next_url = _safe_next(next)
    if locked_out(ip):
        logger.warning("Admin login blocked (too many attempts) from %s", ip)
        return _page(request, error="Too many wrong attempts. Try again in 15 minutes.", status=429, next_url=next_url)
    if not credentials_match(username.strip(), password):
        record_failure(ip)
        logger.warning("Admin login failed from %s", ip)
        return _page(request, error="Wrong username or password.", status=401, next_url=next_url)
    clear_failures(ip)
    logger.info("Admin signed in from %s", ip)
    response = RedirectResponse(url=next_url, status_code=303)
    response.set_cookie(ADMIN_COOKIE, new_session_token(), max_age=int(ADMIN_SESSION_HOURS * 3600),
                        httponly=True, samesite="strict", secure=_cookie_secure(request), path="/admin")
    return response


@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse(url="/admin/login?out=1", status_code=303)
    response.delete_cookie(ADMIN_COOKIE, path="/admin")
    return response
