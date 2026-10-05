"""
test_onboarding.py — Embedded Signup onboarding, per-client tokens and human
takeover, against a FAKE Meta Graph API. Uses a throwaway database and never
calls Meta, so it is safe to run anytime:

    python test_onboarding.py
"""
import os
import tempfile

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()

from cryptography.fernet import Fernet

# Test-only settings, set before any project import (your real .env is ignored).
os.environ.update(
    DATABASE_URL=f"sqlite:///{tmp.name}",
    META_APP_SECRET="test-secret",
    META_ACCESS_TOKEN="GLOBAL_TOKEN",
    META_APP_ID="1234567890",
    META_ES_CONFIG_ID="CONFIG_42",
    TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    VERZIO_ENV="development",
    VERZIO_SECRET_KEY="test-only-secret-key-not-for-production-use",
    ADMIN_USERNAME="admin",
    ADMIN_PASSWORD="verzio-dev-admin",
    COOKIE_SECURE="false",
)

import hashlib
import hmac
import itertools
import json
import re
import types
from datetime import datetime, timedelta

import httpx
from fastapi.testclient import TestClient

import routers.whatsapp_onboarding as onboarding
import server
import whatsapp_client
from database import ActivityEvent, Business, Incident, SessionLocal, Service
from whatsapp_accounts import BotPause, OnboardingLink, WhatsAppAccount, account_credentials, decrypt

fails = 0


def check(label, ok, extra=""):
    global fails
    fails += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if (extra and not ok) else ""))


# ------------------------------------------------------------------ fake Meta
meta_calls = []
meta = {
    "numbers": {"WABA_NEW": [{"id": "PN_NEW", "display_phone_number": "+91 98765 00001", "verified_name": "Glow Salon"}],
                "WABA_COEX": [{"id": "PN_COEX", "display_phone_number": "+91 98765 00002", "verified_name": "Aura Clinic"}],
                "WABA_FALLBACK": [{"id": "PN_FB", "display_phone_number": "+91 98765 00003", "verified_name": "Fallback"}],
                "WABA_CLASH": [{"id": "PN_TAKEN", "display_phone_number": "+91 98765 00004", "verified_name": "Clash"}],
                "WABA_SELF": [{"id": "PN_SELF", "display_phone_number": "+91 90000 00009", "verified_name": "Self"}]},
    "bad_code": "EXPIRED",
}


def meta_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.split("/", 2)[-1]           # strip "/vXX.X/"
    auth = request.headers.get("authorization", "")
    body = json.loads(request.content) if request.content else {}
    meta_calls.append({"method": request.method, "path": path, "auth": auth, "body": body,
                       "params": dict(request.url.params)})
    if path == "oauth/access_token":
        code = request.url.params.get("code")
        if code == meta["bad_code"]:
            return httpx.Response(400, json={"error": {"message": "This authorization code has expired."}})
        return httpx.Response(200, json={"access_token": f"BIZTOKEN_{code}", "token_type": "bearer"})
    if path == "debug_token":
        return httpx.Response(200, json={"data": {"granular_scopes": [
            {"scope": "whatsapp_business_management", "target_ids": ["WABA_FALLBACK"]},
            {"scope": "whatsapp_business_messaging", "target_ids": ["WABA_FALLBACK"]}]}})
    m = re.fullmatch(r"(\w+)/phone_numbers", path)
    if m:
        return httpx.Response(200, json={"data": meta["numbers"].get(m.group(1), [])})
    if path.endswith("/subscribed_apps") or path.endswith("/register") or path.endswith("/smb_app_data"):
        return httpx.Response(200, json={"success": True})
    if path.endswith("/message_templates"):
        return httpx.Response(200, json={"id": "T1", "status": "PENDING", "category": "UTILITY"})
    return httpx.Response(404, json={"error": {"message": f"unexpected call {path}"}})


_RealAsyncClient = httpx.AsyncClient


def _fake_async_client(*args, **kwargs):
    kwargs["transport"] = httpx.MockTransport(meta_handler)
    return _RealAsyncClient(*args, **kwargs)


onboarding.httpx = types.SimpleNamespace(AsyncClient=_fake_async_client, HTTPError=httpx.HTTPError, Response=httpx.Response)

# Outbound WhatsApp: capture instead of calling Meta.
outbox = []


async def fake_post(recipient, pnid, payload, token=None):
    outbox.append({"to": recipient, "from": pnid, "payload": payload, "token": token})
    return None

whatsapp_client._post = fake_post

ids = itertools.count(1)


def webhook(client, value):
    body = json.dumps({"entry": [{"changes": [{"value": value, "field": "messages"}]}]}).encode()
    sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    outbox.clear()
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def inbound(pnid, sender, text="hi"):
    return {"metadata": {"phone_number_id": pnid}, "contacts": [{"wa_id": sender, "profile": {"name": "Riya"}}],
            "messages": [{"id": f"wamid.{next(ids)}", "from": sender, "type": "text", "text": {"body": text}}]}


def calls(path_suffix):
    return [c for c in meta_calls if c["path"].endswith(path_suffix)]


ADMIN = ("admin", "verzio-dev-admin")
ALL = {d: ["09:00", "18:00"] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}


def make_business(db, name, pnid, manager):
    b = Business(name=name, whatsapp_business_phone_number_id=pnid, manager_phone_number=manager,
                 timezone="Asia/Kolkata", operational_hours=ALL, holidays=[], slot_interval=30,
                 max_parallel_bookings=1, approval_mode="manual", is_active=True, accepting_bookings=True,
                 enable_service_selection=True, advance_booking_days=14, notification_preferences={})
    db.add(b); db.commit()
    db.add(Service(business_id=b.id, name="Haircut", duration=30, price=500, is_active=True)); db.commit()
    return b


def new_link(client, business_id):
    client.post(f"/admin/whatsapp/{business_id}/link", auth=ADMIN, follow_redirects=False)
    db = SessionLocal()
    try:
        link = (db.query(OnboardingLink).filter_by(business_id=business_id, used_at=None)
                .order_by(OnboardingLink.created_at.desc()).first())
        return link.token
    finally:
        db.close()


with TestClient(server.app) as client:
    db = SessionLocal()
    glow = make_business(db, "Glow Salon", "pending-glow", "919811111111")
    aura = make_business(db, "Aura Clinic", "pending-aura", "919822222222")
    fb = make_business(db, "Fallback Spa", "pending-fb", "919833333333")
    other = make_business(db, "Taken Studio", "PN_TAKEN", "919844444444")
    selfb = make_business(db, "Self Barber", "pending-self", "919000000009")   # manager == business number
    legacy = make_business(db, "Legacy Test", "PN_LEGACY", "919855555555")     # never onboarded

    # --- admin screens are behind Basic Auth
    r = client.get(f"/admin/whatsapp/{glow.id}")
    check("admin WhatsApp page needs a password", r.status_code == 401, r.status_code)
    r = client.get(f"/admin/whatsapp/{glow.id}", auth=ADMIN)
    check("admin WhatsApp page renders, no config problems", r.status_code == 200 and "Create onboarding link" in r.text
          and "can't run until" not in r.text, r.status_code)
    r = client.get("/admin/businesses", auth=ADMIN)
    check("businesses list has a WhatsApp button", f'/admin/whatsapp/{glow.id}' in r.text)

    # --- links
    token = new_link(client, glow.id)
    r = client.get(f"/admin/whatsapp/{glow.id}", auth=ADMIN)
    check("new link is shown in admin", f"/onboard/{token}" in r.text)
    r = client.get(f"/onboard/{token}")
    check("client page renders with business name and Meta config",
          r.status_code == 200 and "Glow Salon" in r.text and '"CONFIG_42"' in r.text and "connect.facebook.net" in r.text, r.status_code)
    r = client.get("/onboard/not-a-real-token")
    check("bad link -> friendly 'isn't active' page", r.status_code == 404 and "isn't active" in r.text)

    # --- Meta error on code exchange: link stays usable
    r = client.post(f"/onboard/{token}/complete", json={"code": "EXPIRED", "event": "FINISH", "waba_id": "WABA_NEW", "phone_number_id": "PN_NEW"})
    check("expired code -> clear error, nothing saved", r.status_code == 502 and "expired" in r.json()["error"]
          and not db.query(WhatsAppAccount).filter_by(business_id=glow.id).first(), r.text)

    # --- new number (Cloud API)
    meta_calls.clear()
    r = client.post(f"/onboard/{token}/complete", json={"code": "C1", "event": "FINISH", "waba_id": "WABA_NEW",
                                                         "phone_number_id": "PN_NEW", "business_id": "BM_1"})
    res = r.json()
    check("new number: onboarding succeeds", r.status_code == 200 and res["ok"] and res["status"] == "connected"
          and not res["coexistence"], r.text)
    db.expire_all()
    acct = db.query(WhatsAppAccount).filter_by(business_id=glow.id).first()
    biz = db.get(Business, glow.id)
    check("number now routes to this business", biz.whatsapp_business_phone_number_id == "PN_NEW")
    check("token stored encrypted, decrypts correctly",
          acct and "BIZTOKEN_C1" not in (acct.access_token_enc or "") and decrypt(acct.access_token_enc) == "BIZTOKEN_C1")
    reg = calls("PN_NEW/register")
    check("registered with a 6-digit PIN using the client's token",
          len(reg) == 1 and re.fullmatch(r"\d{6}", reg[0]["body"].get("pin", "")) and reg[0]["auth"] == "Bearer BIZTOKEN_C1", reg)
    check("app subscribed to the client's webhooks", len(calls("WABA_NEW/subscribed_apps")) == 1)
    check("no Coexistence sync for a new number", not calls("smb_app_data"))
    tpl = calls("WABA_NEW/message_templates")
    check("all 5 templates submitted with the client's token",
          len(tpl) == 5 and all(c["auth"] == "Bearer BIZTOKEN_C1" for c in tpl), len(tpl))
    db.expire_all()
    check("account marked templates_submitted", db.query(WhatsAppAccount).filter_by(business_id=glow.id).first().templates_submitted)
    r = client.post(f"/onboard/{token}/complete", json={"code": "C1b", "event": "FINISH", "waba_id": "WABA_NEW"})
    check("link can't be reused", r.status_code == 404)

    # --- messages for this client go out with ITS token
    r = webhook(client, inbound("PN_NEW", "917000000001"))
    check("customer reply uses the client's own token and number",
          outbox and outbox[0]["token"] == "BIZTOKEN_C1" and outbox[0]["from"] == "PN_NEW", outbox)
    r = webhook(client, inbound("PN_LEGACY", "917000000002"))
    check("business without a stored connection falls back to META_ACCESS_TOKEN",
          outbox and outbox[0]["token"] is None, outbox)  # None -> _post uses META_ACCESS_TOKEN

    # --- Coexistence: session has only the WABA id
    token2 = new_link(client, aura.id)
    meta_calls.clear()
    r = client.post(f"/onboard/{token2}/complete", json={"code": "C2", "event": "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
                                                          "waba_id": "WABA_COEX"})
    res = r.json()
    check("Coexistence: onboarding succeeds and finds the number", r.status_code == 200 and res["ok"] and res["coexistence"]
          and res["display_phone_number"] == "+91 98765 00002", r.text)
    check("Coexistence: NOT registered (already on the app)", not calls("/register"))
    syncs = [c["body"].get("sync_type") for c in calls("PN_COEX/smb_app_data")]
    check("Coexistence: contact and history sync started", syncs == ["smb_app_state_sync", "history"], syncs)

    # --- human takeover
    webhook(client, {"metadata": {"phone_number_id": "PN_COEX"},
                     "message_echoes": [{"from": "919876500002", "to": "917111111111", "id": f"wamid.e{next(ids)}",
                                         "type": "text", "text": {"body": "Hi Riya, yes we have 4pm free"}}]})
    db.expire_all()
    check("owner's app reply pauses the bot for that customer",
          db.get(BotPause, (aura.id, "917111111111")) is not None)
    r = webhook(client, inbound("PN_COEX", "917111111111", "ok 4pm works"))
    check("paused: bot stays quiet while the owner chats", outbox == [], outbox)
    r = webhook(client, inbound("PN_COEX", "917222222222"))
    check("other customers still get the bot", outbox and outbox[0]["payload"]["type"] == "list", outbox)
    db.get(BotPause, (aura.id, "917111111111")).paused_until = datetime.utcnow() - timedelta(minutes=1); db.commit()
    r = webhook(client, inbound("PN_COEX", "917111111111"))
    check("after the pause expires the bot answers again", outbox and outbox[0]["payload"]["type"] == "list", outbox)

    # --- session message lost: fall back to the token's granted WABA
    token3 = new_link(client, fb.id)
    meta_calls.clear()
    r = client.post(f"/onboard/{token3}/complete", json={"code": "C3"})
    db.expire_all()
    check("no session details: finds the WhatsApp account from the token", r.status_code == 200 and r.json()["ok"]
          and calls("debug_token") and db.get(Business, fb.id).whatsapp_business_phone_number_id == "PN_FB", r.text)

    # --- a number already used by another business
    meta["numbers"]["WABA_CLASH"] = [{"id": "PN_TAKEN", "display_phone_number": "+91 98765 00004"}]
    token4 = new_link(client, glow.id)
    r = client.post(f"/onboard/{token4}/complete", json={"code": "C4", "event": "FINISH", "waba_id": "WABA_CLASH", "phone_number_id": "PN_TAKEN"})
    db.expire_all()
    check("number already used by another business is refused", r.status_code == 502 and "another business" in r.json()["error"]
          and db.get(Business, glow.id).whatsapp_business_phone_number_id == "PN_NEW", r.text)

    # --- manager number == business number
    token5 = new_link(client, selfb.id)
    r = client.post(f"/onboard/{token5}/complete", json={"code": "C5", "event": "FINISH", "waba_id": "WABA_SELF", "phone_number_id": "PN_SELF"})
    check("onboarding flags manager number = business number", r.json().get("manager_is_business_number") is True, r.text)
    r = client.get(f"/admin/whatsapp/{selfb.id}", auth=ADMIN)
    check("admin page shows the warning", "can't message itself" in r.text)
    import asyncio
    outbox.clear()
    ok = asyncio.run(whatsapp_client.send_whatsapp("+91 90000 00009", "PN_SELF", {"type": "text", "body": "x"}, selfb.id))
    check("sender refuses to message the business's own number", ok is False and outbox == [], (ok, outbox))
    check("...and records an incident", db.query(Incident).filter(Incident.title == "Manager number is the business number").count() >= 1)

    # --- config missing -> onboarding blocked with a clear message
    saved = os.environ.pop("META_ES_CONFIG_ID")
    r = client.get(f"/admin/whatsapp/{glow.id}", auth=ADMIN)
    check("admin page lists missing configuration", "META_ES_CONFIG_ID" in r.text)
    token6 = new_link(client, glow.id)
    r = client.post(f"/onboard/{token6}/complete", json={"code": "C6", "event": "FINISH", "waba_id": "WABA_NEW"})
    check("onboarding refuses to run while misconfigured", r.status_code == 503, r.status_code)
    os.environ["META_ES_CONFIG_ID"] = saved
    db.close()

from database import engine as _engine
_engine.dispose()
try:
    os.remove(tmp.name)
except OSError:
    pass
print(f"\n{'ALL PASSED' if fails == 0 else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
