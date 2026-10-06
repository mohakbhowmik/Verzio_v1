"""
preflight.py — one command that tells you whether Verzio is ready to onboard
clients. Run it on the server, inside the app container:

    docker exec verzio_saas python preflight.py

It checks configuration, the database, the live HTTPS endpoints, your Meta app,
token, test number, webhook subscription and templates, and every connected
client. It only READS (no messages sent, nothing changed) and never prints
secrets or full phone numbers.

Exit code 0 = no FAILs (WARNs are things to know, not blockers).
"""
import os
import re
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

RESULTS: list[tuple[str, str, str]] = []
REQUIRED_TABLES = [
    "businesses", "services", "appointments", "owners", "user_sessions",
    "processed_messages", "conversation_windows",
    "whatsapp_accounts", "onboarding_links", "bot_pauses",
]


def record(status: str, label: str, detail: str = "") -> None:
    RESULTS.append((status, label, detail))


def ok(label, detail=""): record("PASS", label, detail)
def warn(label, detail=""): record("WARN", label, detail)
def fail(label, detail=""): record("FAIL", label, detail)


def mask(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    return f"***{digits[-4:]}" if digits else "-"


def env(name: str) -> str:
    return os.getenv(name, "").strip()


# ------------------------------------------------------------------ config

def check_config() -> None:
    required = ["VERZIO_ENV", "APP_DOMAIN", "META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN",
                "PHONE_NUMBER_ID", "VERZIO_VERIFY_TOKEN", "ADMIN_USERNAME", "ADMIN_PASSWORD",
                "VERZIO_SECRET_KEY", "TOKEN_ENCRYPTION_KEY", "META_ES_CONFIG_ID"]
    missing = [n for n in required if not env(n)]
    if missing:
        fail("Configuration: all variables set", "missing: " + ", ".join(missing))
    else:
        ok("Configuration: all variables set")

    if env("VERZIO_ENV").lower() == "production":
        ok("Production mode on")
    else:
        fail("Production mode on", "VERZIO_ENV is not 'production'")

    if len(env("VERZIO_SECRET_KEY")) >= 32:
        ok("Secret key is strong")
    else:
        fail("Secret key is strong", "VERZIO_SECRET_KEY must be 32+ characters")

    pw = env("ADMIN_PASSWORD")
    if pw == "verzio-dev-admin" or len(pw) < 16:
        fail("Admin password is strong", "too short or the development default")
    else:
        ok("Admin password is strong")

    try:
        from whatsapp_accounts import decrypt, encrypt, encryption_ready
        if encryption_ready() and decrypt(encrypt("probe")) == "probe":
            ok("Client-token encryption works")
        else:
            fail("Client-token encryption works", "TOKEN_ENCRYPTION_KEY missing or invalid")
    except Exception as exc:
        fail("Client-token encryption works", str(exc)[:200])


# ------------------------------------------------------------------ database

def check_database() -> None:
    try:
        from sqlalchemy import inspect, text
        from database import Business, SessionLocal, engine
        from whatsapp_accounts import ensure_tables
        from whatsapp_client import ensure_window_table
        ensure_tables()
        ensure_window_table()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        tables = set(inspect(engine).get_table_names())
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            fail("Database tables present", "missing: " + ", ".join(missing) + " (restart the app once)")
        else:
            ok("Database tables present")
        db = SessionLocal()
        try:
            total = db.query(Business).count()
        finally:
            db.close()
        ok("Database reachable", f"{total} business(es)")
        url = env("DATABASE_URL")
        if url and "/app/data/" not in url:
            warn("Database on the persistent volume", "DATABASE_URL doesn't point to /app/data/ (compose normally overrides this)")
    except Exception as exc:
        fail("Database reachable", str(exc)[:200])


# ------------------------------------------------------------------ public endpoints

def check_endpoints(client: httpx.Client) -> None:
    domain = env("APP_DOMAIN")
    if not domain:
        fail("Public site", "APP_DOMAIN not set")
        return
    base = f"https://{domain}"
    try:
        r = client.get(f"{base}/health", timeout=15)
        if r.status_code == 200 and r.json().get("database") == "ok":
            ok("HTTPS + health check", base)
        else:
            fail("HTTPS + health check", f"{r.status_code} {r.text[:120]}")
    except Exception as exc:
        fail("HTTPS + health check", f"{base}: {exc}"[:200])
        return

    checks = [
        ("Unsigned webhook rejected", "POST", "/webhook", 403),
        ("Admin requires a password", "GET", "/admin", 401),
        ("API docs hidden", "GET", "/docs", 404),
    ]
    for label, method, path, expected in checks:
        try:
            r = client.request(method, base + path, content=b"{}" if method == "POST" else None, timeout=15)
            (ok if r.status_code == expected else fail)(label, f"got {r.status_code}, expected {expected}")
        except Exception as exc:
            fail(label, str(exc)[:200])

    try:
        r = client.get(f"{base}/webhook", timeout=15, params={
            "hub.mode": "subscribe", "hub.verify_token": env("VERZIO_VERIFY_TOKEN"), "hub.challenge": "preflight-42"})
        if r.status_code == 200 and r.text.strip() == "preflight-42":
            ok("Meta webhook verification handshake")
        else:
            fail("Meta webhook verification handshake", f"got {r.status_code}")
    except Exception as exc:
        fail("Meta webhook verification handshake", str(exc)[:200])


# ------------------------------------------------------------------ Meta

def _graph(client: httpx.Client, path: str, token: str | None = None, **params) -> dict:
    version = env("GRAPH_API_VERSION") or "v23.0"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = client.get(f"https://graph.facebook.com/{version}/{path}", params=params, headers=headers, timeout=20)
    data = r.json() if r.content else {}
    if r.status_code != 200:
        msg = (data.get("error") or {}).get("message") or r.text[:200]
        raise RuntimeError(f"{r.status_code}: {msg}")
    return data


def check_meta(client: httpx.Client) -> None:
    app_id, secret, token = env("META_APP_ID"), env("META_APP_SECRET"), env("META_ACCESS_TOKEN")
    if not (app_id and secret and token):
        fail("Meta checks", "META_APP_ID, META_APP_SECRET and META_ACCESS_TOKEN are needed")
        return

    # 1. App ID + secret are correct, and the token is valid for this app.
    try:
        info = _graph(client, "debug_token", input_token=token, access_token=f"{app_id}|{secret}").get("data", {})
    except Exception as exc:
        fail("Meta App ID + App Secret correct", str(exc)[:200])
        return
    ok("Meta App ID + App Secret correct")
    if not info.get("is_valid"):
        fail("Access token valid", (info.get("error") or {}).get("message", "token is not valid"))
        return
    if str(info.get("app_id")) != app_id:
        fail("Access token valid", "token belongs to a different Meta app")
        return
    expires = info.get("expires_at", 0)
    ok("Access token valid", "never expires" if not expires else "expires — use a system user token that never expires")
    scopes = set(info.get("scopes", []))
    for perm in ("whatsapp_business_messaging", "whatsapp_business_management"):
        (ok if perm in scopes else fail)(f"Token has {perm}")

    # 2. Your test number.
    pnid = env("PHONE_NUMBER_ID")
    try:
        num = _graph(client, pnid, token, fields="display_phone_number,verified_name,quality_rating,name_status")
        ok("Test number reachable", f"{mask(num.get('display_phone_number'))} '{num.get('verified_name', '')}', "
                                     f"quality {num.get('quality_rating', '?')}")
    except Exception as exc:
        fail("Test number reachable", f"PHONE_NUMBER_ID: {exc}"[:200])

    # 3. Which WABA(s) the token covers -> webhook subscription + templates.
    wabas = []
    for scope in info.get("granular_scopes", []):
        if scope.get("scope") == "whatsapp_business_management":
            wabas = scope.get("target_ids") or []
    if not wabas:
        warn("Webhook subscription + templates", "couldn't find your WhatsApp account from the token; check them in WhatsApp Manager")
        return

    from whatsapp_client import TEMPLATE_NAMES
    for waba in wabas:
        try:
            subs = _graph(client, f"{waba}/subscribed_apps", token).get("data", [])
            ids = {str((s.get("whatsapp_business_api_data") or {}).get("id") or s.get("id")) for s in subs}
            (ok if app_id in ids else fail)(f"WABA …{waba[-4:]}: app subscribed to webhooks",
                                           "" if app_id in ids else "run POST /{waba}/subscribed_apps or re-check App Dashboard webhooks")
        except Exception as exc:
            fail(f"WABA …{waba[-4:]}: app subscribed to webhooks", str(exc)[:200])
        try:
            tpls = _graph(client, f"{waba}/message_templates", token, fields="name,status,category", limit=200).get("data", [])
            by_name = {t["name"]: t for t in tpls}
            for name in TEMPLATE_NAMES.values():
                t = by_name.get(name)
                if not t:
                    fail(f"WABA …{waba[-4:]}: template '{name}'", "not submitted — run create_templates.py")
                elif t.get("status") != "APPROVED":
                    warn(f"WABA …{waba[-4:]}: template '{name}'", f"status {t.get('status')}")
                elif t.get("category") != "UTILITY":
                    warn(f"WABA …{waba[-4:]}: template '{name}'", f"approved but category {t.get('category')} (costs more)")
                else:
                    ok(f"WABA …{waba[-4:]}: template '{name}' approved")
        except Exception as exc:
            fail(f"WABA …{waba[-4:]}: templates", str(exc)[:200])


# ------------------------------------------------------------------ clients

def check_clients(client: httpx.Client) -> None:
    try:
        from database import Business, Service, SessionLocal
        from whatsapp_accounts import WhatsAppAccount, decrypt
    except Exception as exc:
        fail("Client checks", str(exc)[:200])
        return
    app_id, secret = env("META_APP_ID"), env("META_APP_SECRET")
    db = SessionLocal()
    try:
        businesses = db.query(Business).filter(Business.is_active == True).all()
        if not businesses:
            warn("Businesses", "no active businesses yet")
        for b in businesses:
            name = b.name[:30]
            problems = []
            if not db.query(Service).filter(Service.business_id == b.id, Service.is_active == True,
                                            Service.is_deleted == False).count():
                problems.append("no active services")
            if not b.operational_hours:
                problems.append("no opening hours")
            if str(b.whatsapp_business_phone_number_id or "").startswith("pending"):
                problems.append("WhatsApp not connected yet")
            if not b.accepting_bookings:
                problems.append("bookings paused")
            acct = db.query(WhatsAppAccount).filter(WhatsAppAccount.business_id == b.id).first()
            if acct and acct.display_phone_number and \
                    re.sub(r"\D", "", acct.display_phone_number) == re.sub(r"\D", "", b.manager_phone_number or ""):
                problems.append("manager number = business number (approvals only via portal)")
            if acct:
                if acct.status != "connected":
                    problems.append(f"connection status '{acct.status}'")
                tok = decrypt(acct.access_token_enc)
                if not tok:
                    problems.append("stored token can't be decrypted")
                elif app_id and secret:
                    try:
                        d = _graph(client, "debug_token", input_token=tok, access_token=f"{app_id}|{secret}").get("data", {})
                        if not d.get("is_valid"):
                            problems.append("client token no longer valid (client may have removed access)")
                    except Exception as exc:
                        problems.append(f"token check failed: {str(exc)[:80]}")
                if not acct.templates_submitted:
                    problems.append("templates not submitted")
            (warn if problems else ok)(f"Business '{name}'", "; ".join(problems) if problems else
                                       ("connected via Embedded Signup" if acct else "uses the global token"))
    finally:
        db.close()


# ------------------------------------------------------------------ main

def run(client: httpx.Client | None = None) -> int:
    own = client is None
    client = client or httpx.Client()
    try:
        check_config()
        check_database()
        check_endpoints(client)
        check_meta(client)
        check_clients(client)
    finally:
        if own:
            client.close()

    width = max(len(label) for _, label, _ in RESULTS) + 2
    for status, label, detail in RESULTS:
        icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[status]
        print(f"{icon} {status}  {label.ljust(width)}{detail}")
    fails = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    warns = sum(1 for s, _, _ in RESULTS if s == "WARN")
    print(f"\n{len(RESULTS) - fails - warns} passed, {warns} warnings, {fails} failed")
    print("READY TO ONBOARD" if fails == 0 else "NOT READY: fix the ❌ items above")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
