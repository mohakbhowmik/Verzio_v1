"""
account_security.py: password changes and recovery for owners and the admin.

Owners
- Change password (signed in): /owner/password. Every other signed-in device
  is signed out, because a session token is tied to the password it was issued for.
- Forgot password: there is no email or SMS service, so recovery goes through
  Verzio. The owner taps "Forgot password?" and messages us on WhatsApp; the
  admin creates a one-time reset link (Admin -> Businesses -> Reset password)
  and sends it to the owner. The link works once, for RESET_LINK_HOURS.

Admin
- Change password (signed in): /admin/password. The new password is stored as a
  hash in the database and replaces ADMIN_PASSWORD from .env; every admin
  session is signed out.
- Forgot the admin password: on the server run
      docker exec -it verzio_saas python reset_admin_password.py
  which removes the stored password, so ADMIN_PASSWORD in ~/verzio/.env works
  again. Only someone with server access can do this.

New tables only (password_resets, admin_credentials): safe on a live database.
"""
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, Integer, String

from database import Base, SessionLocal, engine

logger = logging.getLogger("VERZIO_SECURITY")

RESET_LINK_HOURS = int(os.getenv("RESET_LINK_HOURS", "24"))
MIN_OWNER_PASSWORD = 8
MIN_ADMIN_PASSWORD = 12


class PasswordReset(Base):
    __tablename__ = "password_resets"
    token = Column(String, primary_key=True)
    owner_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)


class AdminCredential(Base):
    __tablename__ = "admin_credentials"
    id = Column(Integer, primary_key=True)          # always 1
    password_hash = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def ensure_tables() -> None:
    Base.metadata.create_all(bind=engine, tables=[PasswordReset.__table__, AdminCredential.__table__])
    refresh_admin_hash()


def fingerprint(secret: str) -> str:
    """Short, non-reversible tag of a password or password hash, mixed into session signatures."""
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()[:16]


def password_problem(new: str, confirm: str, minimum: int) -> str | None:
    if len(new) < minimum:
        return f"Use at least {minimum} characters."
    if new != confirm:
        return "The two new passwords don't match."
    if new.strip() != new:
        return "Passwords can't start or end with a space."
    return None


# ------------------------------------------------------------------------
# Owner reset links
# ------------------------------------------------------------------------

def create_reset_link(db, owner_id: int) -> PasswordReset:
    """A new one-time link; any older unused links for this owner stop working."""
    now = datetime.utcnow()
    for old in db.query(PasswordReset).filter(PasswordReset.owner_id == owner_id, PasswordReset.used_at.is_(None)):
        old.used_at = now
    link = PasswordReset(token=secrets.token_urlsafe(32), owner_id=owner_id,
                         created_at=now, expires_at=now + timedelta(hours=RESET_LINK_HOURS))
    db.add(link)
    db.commit()
    return link


def valid_reset_link(db, token: str) -> PasswordReset | None:
    if not token or len(token) > 100:
        return None
    link = db.get(PasswordReset, token)
    if link is None or link.used_at is not None or link.expires_at < datetime.utcnow():
        return None
    return link


# ------------------------------------------------------------------------
# Admin password stored in the database (overrides .env once set)
# ------------------------------------------------------------------------

_admin_hash: str | None = None      # one app worker: an in-memory copy is enough


def refresh_admin_hash() -> None:
    global _admin_hash
    db = SessionLocal()
    try:
        row = db.get(AdminCredential, 1)
        _admin_hash = row.password_hash if row else None
    except Exception:
        logger.exception("Could not read the stored admin password; using ADMIN_PASSWORD")
        _admin_hash = None
    finally:
        db.close()


def stored_admin_hash() -> str | None:
    return _admin_hash


def set_admin_password(new_password: str) -> None:
    from owner.owner_auth import hash_password
    db = SessionLocal()
    try:
        row = db.get(AdminCredential, 1)
        if row is None:
            row = AdminCredential(id=1, password_hash=hash_password(new_password))
            db.add(row)
        else:
            row.password_hash = hash_password(new_password)
            row.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    refresh_admin_hash()


def clear_admin_password() -> bool:
    db = SessionLocal()
    try:
        row = db.get(AdminCredential, 1)
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True
    finally:
        db.close()
        refresh_admin_hash()
