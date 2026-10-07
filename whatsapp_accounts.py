"""
whatsapp_accounts.py
================================================================================
VERZIO STUDIO — PER-CLIENT WHATSAPP CONNECTIONS
================================================================================
Everything Verzio stores about each client's WhatsApp account after Embedded
Signup: their WABA ID, phone number ID, display number and their own access
token (encrypted at rest with TOKEN_ENCRYPTION_KEY).

Also holds:
- OnboardingLink: signed, expiring, single-use links you send to a client.
- BotPause: "human takeover". When the owner replies to a customer from the
  WhatsApp Business app (Coexistence), the bot stays quiet for that customer.

New tables only; no existing table is altered, so this is safe on a live DB.
"""
import logging
import os
import secrets
import time
from datetime import datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from database import Base, SessionLocal, engine

logger = logging.getLogger("VERZIO_WA_ACCOUNTS")

ONBOARDING_LINK_HOURS = int(os.getenv("ONBOARDING_LINK_HOURS", "72"))
BOT_PAUSE_HOURS = float(os.getenv("BOT_PAUSE_HOURS", "4"))


# ------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------

class WhatsAppAccount(Base):
    __tablename__ = "whatsapp_accounts"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), unique=True, nullable=False, index=True)
    waba_id = Column(String, nullable=True)
    phone_number_id = Column(String, unique=True, nullable=True, index=True)
    display_phone_number = Column(String, nullable=True)
    verified_name = Column(String, nullable=True)
    meta_business_id = Column(String, nullable=True)
    access_token_enc = Column(Text, nullable=True)
    registration_pin_enc = Column(Text, nullable=True)
    coexistence = Column(Boolean, default=False, nullable=False)
    # connected | no_number | error
    status = Column(String, default="error", nullable=False)
    last_error = Column(Text, nullable=True)
    templates_submitted = Column(Boolean, default=False, nullable=False)
    connected_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OnboardingLink(Base):
    __tablename__ = "onboarding_links"
    token = Column(String, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)


class BotPause(Base):
    __tablename__ = "bot_pauses"
    business_id = Column(Integer, ForeignKey("businesses.id"), primary_key=True)
    phone = Column(String, primary_key=True)
    paused_until = Column(DateTime, nullable=False)


def ensure_tables() -> None:
    """Create the three tables if missing (never alters existing tables)."""
    Base.metadata.create_all(
        bind=engine,
        tables=[WhatsAppAccount.__table__, OnboardingLink.__table__, BotPause.__table__],
    )


# ------------------------------------------------------------------------
# Encryption
# ------------------------------------------------------------------------

def _fernet() -> Fernet | None:
    key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        logger.critical("TOKEN_ENCRYPTION_KEY is not a valid key. Generate one with: "
                        "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
        return None


def encryption_ready() -> bool:
    return _fernet() is not None


def encrypt(value: str) -> str:
    f = _fernet()
    if f is None:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is missing or invalid; cannot store client tokens.")
    return f.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    f = _fernet()
    if f is None:
        logger.error("TOKEN_ENCRYPTION_KEY is missing or invalid; cannot read client tokens.")
        return None
    try:
        return f.decrypt(value.encode()).decode()
    except InvalidToken:
        logger.error("A stored client token could not be decrypted (was TOKEN_ENCRYPTION_KEY changed?).")
        return None


def new_registration_pin() -> str:
    return f"{secrets.randbelow(10**6):06d}"


# ------------------------------------------------------------------------
# Token lookup used by every outbound message
# ------------------------------------------------------------------------

_TOKEN_CACHE: dict[int, tuple[float, str | None, str | None]] = {}
_CACHE_SECONDS = 300


def forget_cached(business_id: int) -> None:
    _TOKEN_CACHE.pop(business_id, None)


def account_credentials(business_id: int | None) -> tuple[str | None, str | None]:
    """(access_token, display_phone_number) for this client, or (None, None)
    if the client has no stored connection (then the global META_ACCESS_TOKEN
    is used, e.g. for your own test number)."""
    if not business_id:
        return None, None
    hit = _TOKEN_CACHE.get(business_id)
    if hit and time.time() - hit[0] < _CACHE_SECONDS:
        return hit[1], hit[2]
    db = SessionLocal()
    try:
        acct = db.query(WhatsAppAccount).filter(WhatsAppAccount.business_id == business_id).first()
        token = decrypt(acct.access_token_enc) if acct and acct.status == "connected" else None
        display = acct.display_phone_number if acct else None
    except Exception:
        logger.exception("Could not read WhatsApp account for business_id=%s", business_id)
        token, display = None, None
    finally:
        db.close()
    _TOKEN_CACHE[business_id] = (time.time(), token, display)
    return token, display


def manager_is_business_number(biz) -> bool:
    """True when the manager number is the business's own WhatsApp number.
    WhatsApp can't message itself, so approval requests can never arrive."""
    from whatsapp_client import canonical_phone  # local import avoids a cycle
    if not biz or not getattr(biz, "id", None):
        return False
    _, display = account_credentials(biz.id)
    manager = canonical_phone(biz.manager_phone_number, biz.timezone)
    return bool(display and manager and canonical_phone(display, biz.timezone) == manager)


# ------------------------------------------------------------------------
# Onboarding links
# ------------------------------------------------------------------------

def create_onboarding_link(db, business_id: int) -> OnboardingLink:
    link = OnboardingLink(
        token=secrets.token_urlsafe(32),
        business_id=business_id,
        expires_at=datetime.utcnow() + timedelta(hours=ONBOARDING_LINK_HOURS),
    )
    db.add(link)
    db.commit()
    return link


def valid_onboarding_link(db, token: str) -> OnboardingLink | None:
    if not token or len(token) > 100:
        return None
    link = db.get(OnboardingLink, token)
    if link is None or link.used_at is not None or link.expires_at < datetime.utcnow():
        return None
    return link


# ------------------------------------------------------------------------
# Human takeover
# ------------------------------------------------------------------------

def pause_bot(business_id: int, phone: str, hours: float | None = None) -> None:
    from whatsapp_client import normalize_phone  # local import avoids a cycle
    digits = normalize_phone(phone)
    if not business_id or not digits:
        return
    until = datetime.utcnow() + timedelta(hours=hours if hours is not None else BOT_PAUSE_HOURS)
    db = SessionLocal()
    try:
        row = db.get(BotPause, (business_id, digits))
        if row:
            row.paused_until = until
        else:
            db.add(BotPause(business_id=business_id, phone=digits, paused_until=until))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not pause bot for business_id=%s", business_id)
    finally:
        db.close()


def bot_paused(business_id: int, phone: str) -> bool:
    from whatsapp_client import normalize_phone
    digits = normalize_phone(phone)
    if not business_id or not digits:
        return False
    db = SessionLocal()
    try:
        row = db.get(BotPause, (business_id, digits))
        return bool(row and row.paused_until > datetime.utcnow())
    except Exception:
        logger.exception("Could not read bot pause for business_id=%s", business_id)
        return False
    finally:
        db.close()


def resume_bot(business_id: int, phone: str) -> None:
    from whatsapp_client import normalize_phone
    db = SessionLocal()
    try:
        row = db.get(BotPause, (business_id, normalize_phone(phone)))
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()
