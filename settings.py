"""
settings.py — single source of truth for security-sensitive configuration.
In production (VERZIO_ENV=production) the app refuses to start with missing or weak secrets.
"""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("VERZIO_SETTINGS")

VERZIO_ENV = os.getenv("VERZIO_ENV", "development").strip().lower()
IS_PRODUCTION = VERZIO_ENV == "production"

_LEGACY_DEFAULT_SECRET = "verzio-studio-secret-key-v1"   # previously hardcoded; must never be accepted
_DEV_SECRET = "dev-only-insecure-secret-do-not-use-in-production"
_DEV_ADMIN_PASSWORD = "verzio-dev-admin"


def _load_secret_key() -> str:
    value = os.getenv("VERZIO_SECRET_KEY", "").strip()
    if IS_PRODUCTION:
        if not value or value == _LEGACY_DEFAULT_SECRET or len(value) < 32:
            raise RuntimeError(
                "VERZIO_SECRET_KEY must be a random value of at least 32 characters in production."
            )
        return value
    if not value:
        logger.warning("VERZIO_SECRET_KEY not set; using an insecure development key.")
        return _DEV_SECRET
    return value


def _load_admin_password() -> str:
    value = os.getenv("ADMIN_PASSWORD", "")
    if IS_PRODUCTION:
        if len(value) < 16:
            raise RuntimeError("ADMIN_PASSWORD must be set (16+ characters) in production.")
        return value
    if not value:
        logger.warning("ADMIN_PASSWORD not set; using development default '%s'.", _DEV_ADMIN_PASSWORD)
        return _DEV_ADMIN_PASSWORD
    return value


SECRET_KEY = _load_secret_key()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = _load_admin_password()

# Optional explicit override for the cookie Secure flag: "true" / "false" / "" (auto).
COOKIE_SECURE_OVERRIDE = os.getenv("COOKIE_SECURE", "").strip().lower()