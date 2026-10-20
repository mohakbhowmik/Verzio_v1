"""
admin_auth.py — guards every /admin path.

Browsers sign in on a login page (/admin/login) and get a signed session
cookie for ADMIN_SESSION_HOURS, with a Log out button in the admin header.
Scripts and tests (anything that isn't a browser page request) may still send
HTTP Basic credentials. Basic credentials sent by a browser are ignored, so a
password the browser remembered can't bypass Log out.

Wrong passwords: after LOGIN_MAX_FAILURES from one IP within LOGIN_WINDOW,
that IP is locked out until the window passes.
"""
import base64
import binascii
import hashlib
import hmac
import logging
import os
import time

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from settings import ADMIN_PASSWORD, ADMIN_USERNAME, SECRET_KEY

logger = logging.getLogger("VERZIO_ADMIN_AUTH")

ADMIN_COOKIE = "verzio_admin_session"
ADMIN_SESSION_HOURS = float(os.getenv("ADMIN_SESSION_HOURS", "12"))
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = 15 * 60                     # seconds
PUBLIC_ADMIN_PATHS = {"/admin/login", "/admin/logout"}

_failures: dict[str, list[float]] = {}


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def _is_browser_page(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


def password_matches(password: str) -> bool:
    """The password set in Admin -> Password if there is one, otherwise ADMIN_PASSWORD from .env."""
    from account_security import stored_admin_hash
    from owner.owner_auth import verify_password
    stored = stored_admin_hash()
    if stored:
        return verify_password(password or "", stored)
    return hmac.compare_digest((password or "").encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))


def credentials_match(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest((username or "").encode("utf-8"), ADMIN_USERNAME.encode("utf-8"))
    pass_ok = password_matches(password)
    return user_ok and pass_ok


def _basic_valid(auth_header: str | None) -> bool:
    if not auth_header or not auth_header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    username, sep, password = decoded.partition(":")
    return bool(sep) and credentials_match(username, password)


# ------------------------------------------------------------------------
# Session cookie
# ------------------------------------------------------------------------

def _password_fingerprint() -> str:
    # Changing the admin password (in Admin -> Password or in .env) signs everyone out.
    from account_security import stored_admin_hash
    return hashlib.sha256((stored_admin_hash() or ADMIN_PASSWORD).encode("utf-8")).hexdigest()[:16]


def _sign(exp: int) -> str:
    payload = f"admin:{exp}:{_password_fingerprint()}"
    return hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def new_session_token() -> str:
    exp = int(time.time() + ADMIN_SESSION_HOURS * 3600)
    return f"admin:{exp}:{_sign(exp)}"


def session_valid(token: str | None) -> bool:
    try:
        kind, exp_s, sig = (token or "").split(":")
        exp = int(exp_s)
    except ValueError:
        return False
    return kind == "admin" and time.time() < exp and hmac.compare_digest(sig, _sign(exp))


# ------------------------------------------------------------------------
# Wrong-password lockout
# ------------------------------------------------------------------------

def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def locked_out(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < LOGIN_WINDOW]
    _failures[ip] = recent
    return len(recent) >= LOGIN_MAX_FAILURES


def record_failure(ip: str) -> None:
    _failures.setdefault(ip, []).append(time.time())


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)


# ------------------------------------------------------------------------
# Middleware
# ------------------------------------------------------------------------

async def admin_auth_middleware(request: Request, call_next):
    path = request.url.path
    if not _is_admin_path(path) or path in PUBLIC_ADMIN_PATHS:
        return await call_next(request)

    if session_valid(request.cookies.get(ADMIN_COOKIE)):
        return await call_next(request)

    browser = _is_browser_page(request)
    auth_header = request.headers.get("authorization")
    if not browser and _basic_valid(auth_header):
        return await call_next(request)          # scripts and tests

    if auth_header and not browser:
        logger.warning("Admin auth failed: %s %s from %s", request.method, path, client_ip(request))
    if browser:
        from urllib.parse import quote
        return RedirectResponse(url=f"/admin/login?next={quote(path)}", status_code=303)
    return Response(status_code=401, content="Authentication required",
                    headers={"WWW-Authenticate": 'Basic realm="Verzio Admin", charset="UTF-8"'})
