"""
admin_auth.py — HTTP Basic Auth guard for every /admin path.
Applied as middleware so new admin routes are protected by default.
"""
import base64
import binascii
import hmac
import logging

from fastapi import Request
from fastapi.responses import Response

from settings import ADMIN_USERNAME, ADMIN_PASSWORD

logger = logging.getLogger("VERZIO_ADMIN_AUTH")


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def _credentials_valid(auth_header: str | None) -> bool:
    if not auth_header or not auth_header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    username, sep, password = decoded.partition(":")
    if not sep:
        return False
    user_ok = hmac.compare_digest(username.encode("utf-8"), ADMIN_USERNAME.encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
    return user_ok and pass_ok


async def admin_auth_middleware(request: Request, call_next):
    if _is_admin_path(request.url.path):
        auth_header = request.headers.get("authorization")
        if not _credentials_valid(auth_header):
            if auth_header:
                client = request.client.host if request.client else "unknown"
                logger.warning("Admin auth failed: %s %s from %s", request.method, request.url.path, client)
            return Response(
                status_code=401,
                content="Authentication required",
                headers={"WWW-Authenticate": 'Basic realm="Verzio Admin", charset="UTF-8"'},
            )
    return await call_next(request)