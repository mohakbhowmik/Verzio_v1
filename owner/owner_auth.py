import hmac
import hashlib
import secrets
import time
import logging
from typing import Optional, Tuple
from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import or_
from database import get_db, Owner, Business
from settings import SECRET_KEY, IS_PRODUCTION, COOKIE_SECURE_OVERRIDE

logger = logging.getLogger("VERZIO_OWNER_AUTH")
router = APIRouter(prefix="/owner", tags=["owner-auth"])
templates = Jinja2Templates(directory="templates")
SESSION_COOKIE_NAME = "verzio_owner_session"
LEGACY_COOKIE_NAME = "verzio_owner_phone"


# ------------------------------------------------------------------------
# Password Hashing Strategy (PBKDF2-HMAC-SHA256)
# ------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with a random salt."""
    salt = secrets.token_hex(16)
    iterations = 260000
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations
    )
    return f"pbkdf2:sha256:{iterations}${salt}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify password against stored PBKDF2-HMAC-SHA256 hash."""
    try:
        parts = stored_hash.split("$")
        if len(parts) != 3:
            return False
        header, salt, expected_hex = parts
        _, algo, iterations_str = header.split(":")
        iterations = int(iterations_str)
        derived = hashlib.pbkdf2_hmac(
            algo,
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations
        )
        return hmac.compare_digest(derived.hex(), expected_hex)
    except Exception as e:
        logger.warning(f"Password verification error: {e}")
        return False


# ------------------------------------------------------------------------
# Session Token Utilities (HMAC Signed Tokens)
# ------------------------------------------------------------------------

def _signature(owner_id: int, exp: int, password_hash: str) -> str:
    # The password hash is part of what is signed, so changing the password
    # signs the owner out everywhere else.
    from account_security import fingerprint
    payload = f"{owner_id}:{exp}:{fingerprint(password_hash)}"
    return hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(owner_id: int, remember_me: bool = False, password_hash: Optional[str] = None) -> Tuple[str, Optional[int]]:
    """
    Generate an HMAC-signed session token string.
    Returns (token, max_age_in_seconds).
    """
    duration = 30 * 86400 if remember_me else 86400
    exp = int(time.time()) + duration
    payload = f"{owner_id}:{exp}"
    if password_hash is None:
        from database import SessionLocal
        db = SessionLocal()
        try:
            owner = db.get(Owner, owner_id)
            password_hash = owner.password_hash if owner else ""
        finally:
            db.close()
    signature = _signature(owner_id, exp, password_hash)
    token = f"{payload}:{signature}"
    max_age = duration if remember_me else None
    return token, max_age


def _cookie_secure(request: Request) -> bool:
    if COOKIE_SECURE_OVERRIDE in ("true", "1"):
        return True
    if COOKIE_SECURE_OVERRIDE in ("false", "0"):
        return False
    return IS_PRODUCTION or request.url.scheme == "https"


def set_session_cookie(response, request: Request, token: str, max_age: Optional[int]) -> None:
    """The only place an owner session cookie is ever set."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def verify_session_token(token: str, db: Session) -> Optional[Owner]:
    """Verify signed session token and return active Owner object."""
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        owner_id_str, exp_str, signature = parts
        owner_id = int(owner_id_str)
        exp = int(exp_str)

        if time.time() > exp:
            return None

        owner = db.get(Owner, owner_id)
        if not owner or not owner.is_active:
            return None
        if not hmac.compare_digest(signature, _signature(owner_id, exp, owner.password_hash)):
            return None
        return owner
    except Exception as e:
        logger.debug(f"Session token verification error: {e}")
        return None


# ------------------------------------------------------------------------
# Centralized Owner & Business Resolution Helper
# ------------------------------------------------------------------------

def is_mobile(request: Request) -> bool:
    """Detect a mobile user-agent for responsive owner-portal templates."""
    user_agent = (request.headers.get("user-agent") or "").lower()
    return any(marker in user_agent for marker in ("mobi", "android", "iphone"))


def resolve_owner_and_business(request: Request, db: Session) -> Tuple[Optional[Owner], Optional[Business]]:
    """Resolve the owner strictly from the signed session cookie. No fallbacks."""
    token = (request.cookies.get(SESSION_COOKIE_NAME) or "").strip()
    if not token:
        return None, None
    owner = verify_session_token(token, db)
    if owner and owner.business:
        return owner, owner.business
    return None, None


def get_current_business_or_redirect(request: Request, db: Session) -> Tuple[Optional[Owner], Optional[Business], Optional[RedirectResponse]]:
    """
    Helper for owner routes. Returns (owner, business, None) if authenticated,
    or (None, None, RedirectResponse) if unauthenticated.
    """
    owner, business = resolve_owner_and_business(request, db)
    if not owner or not business:
        redirect = RedirectResponse(url="/owner/login", status_code=303)
        return None, None, redirect
    return owner, business, None


# ------------------------------------------------------------------------
# Authentication Routes (/owner/login & /owner/logout)
# ------------------------------------------------------------------------

@router.get("/login")
async def login_page(request: Request, db: Session = Depends(get_db)):
    """Render Owner Login Page."""
    owner, business = resolve_owner_and_business(request, db)
    if owner and business:
        return RedirectResponse(url="/owner/dashboard", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="owner/login.html",
        context={"request": request, "error": None}
    )


@router.post("/login")
async def login_action(
    request: Request,
    db: Session = Depends(get_db),
    identifier: str = Form(""),
    password: str = Form(""),
    remember_me: Optional[str] = Form(None),
):
    """Authenticate Owner and create session."""
    clean_identifier = identifier.strip()
    clean_password = password.strip()
    is_remember = remember_me in ("on", "true", "1")

    if not clean_identifier or not clean_password:
        return templates.TemplateResponse(
            request=request,
            name="owner/login.html",
            context={"request": request, "error": "Please provide your email/phone and password."},
        )

    owner = db.query(Owner).filter(
        or_(
            Owner.email == clean_identifier,
            Owner.phone_number == clean_identifier,
        )
    ).first()

    if not owner or not owner.is_active or not verify_password(clean_password, owner.password_hash):
        logger.warning(f"Failed login attempt for identifier '{clean_identifier}'")
        return templates.TemplateResponse(
            request=request,
            name="owner/login.html",
            context={"request": request, "error": "Invalid email/phone or password."},
        )

    token, max_age = create_session_token(owner.id, remember_me=is_remember, password_hash=owner.password_hash)
    response = RedirectResponse(url="/owner/dashboard", status_code=303)
    set_session_cookie(response, request, token, max_age)
    logger.info(f"Owner #{owner.id} ('{owner.full_name}') logged in successfully.")
    return response


@router.post("/logout")
async def logout_action():
    """Destroy session and redirect to login."""
    response = RedirectResponse(url="/owner/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ------------------------------------------------------------------------
# Change password (signed in) and reset by one-time link (see account_security.py)
# ------------------------------------------------------------------------

def _password_page(request: Request, owner: Optional[Owner], business: Optional[Business], **extra):
    return templates.TemplateResponse(request=request, name="owner/password.html",
                                      context={"request": request, "owner": owner, "business": business, **extra})


@router.get("/password")
async def password_page(request: Request, db: Session = Depends(get_db)):
    owner, business, redirect = get_current_business_or_redirect(request, db)
    if redirect:
        return redirect
    return _password_page(request, owner, business, done=request.query_params.get("done") == "1")


@router.post("/password")
async def change_password(
    request: Request,
    db: Session = Depends(get_db),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    from account_security import MIN_OWNER_PASSWORD, password_problem
    owner, business, redirect = get_current_business_or_redirect(request, db)
    if redirect:
        return redirect
    if not verify_password(current_password.strip(), owner.password_hash):
        return _password_page(request, owner, business, error="Your current password isn't right.")
    problem = password_problem(new_password, confirm_password, MIN_OWNER_PASSWORD)
    if problem is None and verify_password(new_password, owner.password_hash):
        problem = "Choose a password you haven't used here before."
    if problem:
        return _password_page(request, owner, business, error=problem)
    owner.password_hash = hash_password(new_password)
    db.commit()
    logger.info("Owner #%s changed their password", owner.id)
    token, max_age = create_session_token(owner.id, remember_me=True, password_hash=owner.password_hash)
    response = RedirectResponse(url="/owner/password?done=1", status_code=303)
    set_session_cookie(response, request, token, max_age)    # this device stays signed in
    return response


@router.get("/forgot")
async def forgot_page(request: Request):
    return templates.TemplateResponse(request=request, name="owner/forgot.html", context={"request": request})


def _reset_page(request: Request, token: str, **extra):
    return templates.TemplateResponse(request=request, name="owner/reset.html",
                                      context={"request": request, "token": token, **extra},
                                      headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/reset/{token}")
async def reset_page(token: str, request: Request, db: Session = Depends(get_db)):
    from account_security import valid_reset_link
    link = valid_reset_link(db, token)
    owner = db.get(Owner, link.owner_id) if link else None
    if not owner or not owner.is_active:
        return _reset_page(request, token, expired=True)
    return _reset_page(request, token, owner_name=owner.full_name)


@router.post("/reset/{token}")
async def reset_action(token: str, request: Request, db: Session = Depends(get_db),
                       new_password: str = Form(""), confirm_password: str = Form("")):
    from account_security import MIN_OWNER_PASSWORD, password_problem, valid_reset_link
    link = valid_reset_link(db, token)
    owner = db.get(Owner, link.owner_id) if link else None
    if not owner or not owner.is_active:
        return _reset_page(request, token, expired=True)
    problem = password_problem(new_password, confirm_password, MIN_OWNER_PASSWORD)
    if problem:
        return _reset_page(request, token, owner_name=owner.full_name, error=problem)
    owner.password_hash = hash_password(new_password)
    link.used_at = datetime.utcnow()
    db.commit()
    logger.info("Owner #%s set a new password with a reset link", owner.id)
    token_, max_age = create_session_token(owner.id, remember_me=False, password_hash=owner.password_hash)
    response = RedirectResponse(url="/owner/dashboard", status_code=303)
    set_session_cookie(response, request, token_, max_age)
    return response
