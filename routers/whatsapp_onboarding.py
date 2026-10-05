"""
whatsapp_onboarding.py
================================================================================
VERZIO STUDIO — WHATSAPP EMBEDDED SIGNUP (v4) ONBOARDING
================================================================================
Admin side (behind /admin Basic Auth):
    GET  /admin/whatsapp/{business_id}          connection status + onboarding links
    POST /admin/whatsapp/{business_id}/link     create a one-time onboarding link

Client side (public, protected by the one-time link):
    GET  /onboard/{token}                       page with the "Connect WhatsApp" buttons
    POST /onboard/{token}/complete              called by that page with Meta's code

After the client finishes Meta's pop-up, the server, with no manual steps:
  1. exchanges the 30-second code for the client's own business token
  2. finds the WhatsApp account and phone number (Coexistence often omits the id)
  3. subscribes Verzio's app to the client's webhooks
  4. new number: registers it for the Cloud API with a random PIN
     existing WhatsApp Business app number (Coexistence): skips registration
     and starts the contact + history sync (must happen within 24 hours)
  5. stores the token encrypted and routes the number to this business
  6. submits the five message templates on the client's account (background)
"""
import logging
import os
from datetime import datetime

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from activity_log import log_event
from database import Business, get_db
from whatsapp_accounts import (
    OnboardingLink,
    WhatsAppAccount,
    create_onboarding_link,
    encrypt,
    encryption_ready,
    forget_cached,
    new_registration_pin,
    valid_onboarding_link,
)
from whatsapp_client import GRAPH_API_VERSION, TEMPLATE_DEFINITIONS, mask_phone, normalize_phone

logger = logging.getLogger("VERZIO_ONBOARDING")

admin_router = APIRouter(prefix="/admin/whatsapp", tags=["admin-whatsapp"])
public_router = APIRouter(prefix="/onboard", tags=["onboarding"])
templates = Jinja2Templates(directory="templates")

GRAPH = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
FINISH_EVENTS = {"FINISH", "FINISH_ONLY_WABA", "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"}
COEX_EVENT = "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"


def _config() -> dict:
    return {
        "app_id": os.getenv("META_APP_ID", "").strip(),
        "config_id": os.getenv("META_ES_CONFIG_ID", "").strip(),
        "app_secret": os.getenv("META_APP_SECRET", "").strip(),
        "sdk_version": os.getenv("FB_SDK_VERSION", "v25.0").strip(),
    }


def _config_problems() -> list[str]:
    cfg = _config()
    problems = []
    if not cfg["app_id"]:
        problems.append("META_APP_ID is not set.")
    if not cfg["config_id"]:
        problems.append("META_ES_CONFIG_ID (Embedded Signup configuration ID) is not set.")
    if not cfg["app_secret"]:
        problems.append("META_APP_SECRET is not set.")
    if not encryption_ready():
        problems.append("TOKEN_ENCRYPTION_KEY is missing or invalid, so client tokens can't be stored.")
    return problems


# ------------------------------------------------------------------------
# Graph API helpers
# ------------------------------------------------------------------------

class GraphError(Exception):
    pass


def _graph_error(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return err.get("error_user_msg") or err.get("message") or resp.text[:300]
    except ValueError:
        return resp.text[:300]


async def _graph(client: httpx.AsyncClient, method: str, path: str, token: str | None = None, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = await client.request(method, f"{GRAPH}/{path.lstrip('/')}", headers=headers, **kwargs)
    if resp.status_code != 200:
        raise GraphError(f"{method} {path.split('?')[0]} failed ({resp.status_code}): {_graph_error(resp)}")
    return resp.json() if resp.content else {}


async def exchange_code(client: httpx.AsyncClient, code: str) -> str:
    cfg = _config()
    data = await _graph(client, "GET", "oauth/access_token", params={
        "client_id": cfg["app_id"], "client_secret": cfg["app_secret"], "code": code,
    })
    token = data.get("access_token")
    if not token:
        raise GraphError("Meta did not return an access token.")
    return token


async def find_waba_ids(client: httpx.AsyncClient, token: str) -> list[str]:
    """Fallback when the pop-up's session message didn't reach the page:
    read which WhatsApp accounts this token was granted."""
    cfg = _config()
    data = await _graph(client, "GET", "debug_token", params={
        "input_token": token, "access_token": f"{cfg['app_id']}|{cfg['app_secret']}",
    })
    ids = []
    for scope in data.get("data", {}).get("granular_scopes", []):
        if scope.get("scope") in ("whatsapp_business_management", "whatsapp_business_messaging"):
            for target in scope.get("target_ids", []) or []:
                if target not in ids:
                    ids.append(target)
    return ids


async def list_phone_numbers(client: httpx.AsyncClient, token: str, waba_id: str) -> list[dict]:
    data = await _graph(client, "GET", f"{waba_id}/phone_numbers", token,
                        params={"fields": "id,display_phone_number,verified_name"})
    return data.get("data", [])


async def submit_templates(business_id: int, waba_id: str, token: str) -> None:
    """Background task: submit Verzio's templates on the client's account."""
    from create_templates import build_request  # same definitions the sender uses
    failures = []
    async with httpx.AsyncClient(timeout=30) as client:
        for key in TEMPLATE_DEFINITIONS:
            payload = build_request(key)
            try:
                await _graph(client, "POST", f"{waba_id}/message_templates", token, json=payload)
            except GraphError as exc:
                if "already exists" in str(exc).lower():
                    continue
                failures.append(f"{payload['name']}: {exc}")
            except httpx.HTTPError as exc:
                failures.append(f"{payload['name']}: network error {exc}")

    from database import SessionLocal
    db = SessionLocal()
    try:
        acct = db.query(WhatsAppAccount).filter(WhatsAppAccount.business_id == business_id).first()
        if acct:
            acct.templates_submitted = not failures
            if failures:
                acct.last_error = ("Template submission: " + "; ".join(failures))[:2000]
            db.commit()
        log_event(db=db, event_type="templates_submitted" if not failures else "templates_failed",
                  status="success" if not failures else "warning",
                  message="All templates submitted for review" if not failures else "; ".join(failures)[:500],
                  business_id=business_id)
    finally:
        db.close()


# ------------------------------------------------------------------------
# Admin pages
# ------------------------------------------------------------------------

def _business_or_404(db: Session, business_id: int) -> Business:
    biz = db.get(Business, business_id)
    if not biz:
        raise HTTPException(status_code=404, detail="Business not found.")
    return biz


@admin_router.get("/{business_id}", response_class=HTMLResponse)
def whatsapp_status_page(business_id: int, request: Request, db: Session = Depends(get_db)):
    biz = _business_or_404(db, business_id)
    acct = db.query(WhatsAppAccount).filter(WhatsAppAccount.business_id == business_id).first()
    now = datetime.utcnow()
    links = (
        db.query(OnboardingLink)
        .filter(OnboardingLink.business_id == business_id, OnboardingLink.used_at.is_(None),
                OnboardingLink.expires_at > now)
        .order_by(OnboardingLink.created_at.desc())
        .all()
    )
    base_url = str(request.base_url).rstrip("/")
    manager_is_business_number = bool(
        acct and acct.display_phone_number
        and normalize_phone(acct.display_phone_number) == normalize_phone(biz.manager_phone_number)
    )
    return templates.TemplateResponse(
        request=request,
        name="admin_whatsapp.html",
        context={
            "active_page": "businesses",
            "business": biz,
            "account": acct,
            "links": [{"url": f"{base_url}/onboard/{l.token}", "expires_at": l.expires_at} for l in links],
            "config_problems": _config_problems(),
            "manager_is_business_number": manager_is_business_number,
        },
    )


@admin_router.post("/{business_id}/link")
def create_link(business_id: int, db: Session = Depends(get_db)):
    biz = _business_or_404(db, business_id)
    create_onboarding_link(db, biz.id)
    log_event(db=db, event_type="onboarding_link_created", status="info",
              message="WhatsApp onboarding link created", business_id=biz.id)
    return RedirectResponse(url=f"/admin/whatsapp/{biz.id}", status_code=303)


# ------------------------------------------------------------------------
# Client-facing pages
# ------------------------------------------------------------------------

@public_router.get("/{token}", response_class=HTMLResponse)
def onboarding_page(token: str, request: Request, db: Session = Depends(get_db)):
    link = valid_onboarding_link(db, token)
    biz = db.get(Business, link.business_id) if link else None
    cfg = _config()
    return templates.TemplateResponse(
        request=request,
        name="onboard.html",
        context={
            "valid": bool(link and biz),
            "business_name": biz.name if biz else "",
            "token": token,
            "app_id": cfg["app_id"],
            "config_id": cfg["config_id"],
            "sdk_version": cfg["sdk_version"],
            "configured": not _config_problems(),
        },
        status_code=200 if link else 404,
    )


def _fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


@public_router.post("/{token}/complete")
async def complete_onboarding(
    token: str, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    link = valid_onboarding_link(db, token)
    if not link:
        return _fail("This link has expired or was already used. Ask Verzio for a new one.", 404)
    biz = db.get(Business, link.business_id)
    if not biz:
        return _fail("This link has expired or was already used. Ask Verzio for a new one.", 404)
    if _config_problems():
        logger.error("Onboarding blocked by configuration: %s", _config_problems())
        return _fail("Verzio's WhatsApp connection isn't fully configured yet. Please contact Verzio.", 503)

    try:
        body = await request.json()
    except ValueError:
        return _fail("Invalid request.")
    code = str(body.get("code") or "").strip()
    event = str(body.get("event") or "").strip()
    waba_id = str(body.get("waba_id") or "").strip() or None
    phone_number_id = str(body.get("phone_number_id") or "").strip() or None
    meta_business_id = str(body.get("business_id") or "").strip() or None
    if not code:
        return _fail("Meta didn't return a sign-in code. Please try again.")
    if event and event not in FINISH_EVENTS:
        return _fail("Signup didn't finish. Please try again.")
    coexistence = event == COEX_EVENT
    warnings: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # 1. The code is only valid for 30 seconds: exchange it first.
            access_token = await exchange_code(client, code)

            # 2. Which WhatsApp account?
            if not waba_id:
                ids = await find_waba_ids(client, access_token)
                if len(ids) != 1:
                    raise GraphError("Couldn't tell which WhatsApp account was connected. Please try again.")
                waba_id = ids[0]

            # 3. Which phone number?
            numbers = await list_phone_numbers(client, access_token, waba_id)
            number = None
            if phone_number_id:
                number = next((n for n in numbers if n.get("id") == phone_number_id), {"id": phone_number_id})
            elif len(numbers) == 1:
                number = numbers[0]
            elif len(numbers) > 1:
                raise GraphError("This WhatsApp account has several numbers. Contact Verzio to choose the right one.")

            # 4. Webhooks for this client's account go to Verzio.
            await _graph(client, "POST", f"{waba_id}/subscribed_apps", access_token)

            pin = None
            if number and number.get("id"):
                phone_number_id = number["id"]
                clash = (
                    db.query(Business)
                    .filter(Business.whatsapp_business_phone_number_id == phone_number_id, Business.id != biz.id)
                    .first()
                )
                if clash:
                    raise GraphError("This WhatsApp number is already connected to another business on Verzio.")

                if coexistence:
                    # Already registered by the WhatsApp Business app. Start the
                    # contact and history sync (Meta allows 24 hours for this).
                    for sync_type in ("smb_app_state_sync", "history"):
                        try:
                            await _graph(client, "POST", f"{phone_number_id}/smb_app_data", access_token,
                                         json={"messaging_product": "whatsapp", "sync_type": sync_type})
                        except GraphError as exc:
                            warnings.append(f"Sync '{sync_type}' didn't start: {exc}")
                else:
                    pin = new_registration_pin()
                    try:
                        await _graph(client, "POST", f"{phone_number_id}/register", access_token,
                                     json={"messaging_product": "whatsapp", "pin": pin})
                    except GraphError as exc:
                        warnings.append(f"Number registration: {exc}")
    except GraphError as exc:
        logger.warning("Onboarding failed for business_id=%s: %s", biz.id, exc)
        log_event(db=db, event_type="onboarding_failed", status="error", message=str(exc)[:500], business_id=biz.id)
        return _fail(str(exc), 502)
    except httpx.HTTPError as exc:
        logger.warning("Onboarding network error for business_id=%s: %s", biz.id, exc)
        return _fail("Couldn't reach Meta. Please check your internet connection and try again.", 502)

    # 5. Save the connection.
    acct = db.query(WhatsAppAccount).filter(WhatsAppAccount.business_id == biz.id).first()
    if acct is None:
        acct = WhatsAppAccount(business_id=biz.id)
        db.add(acct)
    acct.waba_id = waba_id
    acct.meta_business_id = meta_business_id
    acct.access_token_enc = encrypt(access_token)
    acct.coexistence = coexistence
    acct.templates_submitted = False
    acct.last_error = "; ".join(warnings)[:2000] or None
    if number and number.get("id"):
        acct.phone_number_id = phone_number_id
        acct.display_phone_number = number.get("display_phone_number")
        acct.verified_name = number.get("verified_name")
        acct.registration_pin_enc = encrypt(pin) if pin else acct.registration_pin_enc
        acct.status = "connected"
        acct.connected_at = datetime.utcnow()
        biz.whatsapp_business_phone_number_id = phone_number_id
    else:
        acct.status = "no_number"
    link.used_at = datetime.utcnow()
    db.commit()
    forget_cached(biz.id)

    manager_clash = bool(
        acct.display_phone_number
        and normalize_phone(acct.display_phone_number) == normalize_phone(biz.manager_phone_number)
    )
    log_event(db=db, event_type="whatsapp_connected" if acct.status == "connected" else "whatsapp_no_number",
              status="success" if acct.status == "connected" and not warnings else "warning",
              message=(f"WhatsApp {'(existing app number) ' if coexistence else ''}connected: "
                       f"{mask_phone(acct.display_phone_number)}" + (f". Warnings: {'; '.join(warnings)}" if warnings else ""))[:500],
              business_id=biz.id)

    # 6. Templates go to Meta for review in the background.
    background_tasks.add_task(submit_templates, biz.id, waba_id, access_token)

    return JSONResponse({
        "ok": True,
        "status": acct.status,
        "business_name": biz.name,
        "display_phone_number": acct.display_phone_number,
        "coexistence": coexistence,
        "warnings": warnings,
        "manager_is_business_number": manager_clash,
    })
