import os
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

logger = logging.getLogger("VERZIO_OWNER_AUTH")

router = APIRouter(prefix="/owner", tags=["owner-auth"])
templates = Jinja2Templates(directory="templates")

SECRET_KEY = os.getenv("VERZIO_SECRET_KEY", "verzio-studio-secret-key-v1")
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

def create_session_token(owner_id: int, remember_me: bool = False) -> Tuple[str, Optional[int]]:
    """
    Generate an HMAC-signed session token string.
    Returns (token, max_age_in_seconds).
    """
    duration = 30 * 86400 if remember_me else 86400
    exp = int(time.time()) + duration
    payload = f"{owner_id}:{exp}"
    signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    token = f"{payload}:{signature}"
    max_age = duration if remember_me else None
    return token, max_age


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

        payload = f"{owner_id}:{exp}"
        expected_sig = hmac.new(
            SECRET_KEY.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_sig):
            return None

        owner = db.get(Owner, owner_id)
        if owner and owner.is_active:
            return owner
        return None
    except Exception as e:
        logger.debug(f"Session token verification error: {e}")
        return None


# ------------------------------------------------------------------------
# Centralized Owner & Business Resolution Helper
# ------------------------------------------------------------------------

def resolve_owner_and_business(request: Request, db: Session) -> Tuple[Optional[Owner], Optional[Business]]:
    """
    Centralized Owner & Business resolution.
    1. Reads verzio_owner_session cookie / header.
    2. Fallback to legacy verzio_owner_phone matching Business.manager_phone_number.
    Returns (Owner | None, Business | None).
    """
    session_token = (
        request.cookies.get(SESSION_COOKIE_NAME)
        or request.headers.get("x-verzio-owner-session")
        or ""
    ).strip()

    if session_token:
        owner = verify_session_token(session_token, db)
        if owner and owner.business:
            return owner, owner.business

    # Backwards compatibility legacy fallback
    legacy_phone = (
        request.cookies.get(LEGACY_COOKIE_NAME)
        or request.headers.get("x-verzio-owner-phone")
        or ""
    ).strip()

    if legacy_phone:
        biz = db.query(Business).filter(Business.manager_phone_number == legacy_phone).first()
        if biz:
            # Resolve or create temporary owner mapping if missing
            owner = db.query(Owner).filter(Owner.business_id == biz.id).first()
            return owner, biz

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
    remember_me: Optional[str] = Form(None)
):
    """Authenticate Owner and create session."""
    clean_identifier = identifier.strip()
    clean_password = password.strip()
    is_remember = remember_me in ("on", "true", "1")

    if not clean_identifier or not clean_password:
        return templates.TemplateResponse(
            request=request,
            name="owner/login.html",
            context={"request": request, "error": "Please provide your email/phone and password."}
        )

    # Lookup Owner by email or phone
    owner = db.query(Owner).filter(
        or_(
            Owner.email == clean_identifier,
            Owner.phone_number == clean_identifier
        )
    ).first()

    if not owner or not owner.is_active or not verify_password(clean_password, owner.password_hash):
        logger.warning(f"Failed login attempt for identifier '{clean_identifier}'")
        return templates.TemplateResponse(
            request=request,
            name="owner/login.html",
            context={"request": request, "error": "Invalid email/phone or password."}
        )

    # Generate session token
    token, max_age = create_session_token(owner.id, remember_me=is_remember)

    response = RedirectResponse(url="/owner/dashboard", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=False  # Set True in production with HTTPS
    )

    logger.info(f"Owner #{owner.id} ('{owner.full_name}') logged in successfully.")
    return response


@router.post("/logout")
async def logout_action():
    """Destroy session and redirect to login."""
    response = RedirectResponse(url="/owner/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie(LEGACY_COOKIE_NAME)
    return response
