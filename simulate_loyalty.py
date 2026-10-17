"""
simulate_loyalty.py: try Verzio Loyalty on your PC without WhatsApp.

Real WhatsApp messages only reach the server, so this fakes them. It uses its
own demo database (loyalty_demo.db), never your real one, and never sends
anything to Meta.

Two terminals, both in the project folder:

  Terminal 1 (the website):   python simulate_loyalty.py --serve
      Owner portal:  http://127.0.0.1:8000/owner/login
                     demo@cafe.test / demo-pass-123  ->  Loyalty
      Admin:         http://127.0.0.1:8000/admin/loyalty  (admin / verzio-dev-admin)

  Terminal 2 (the customer):  python simulate_loyalty.py
      Type what a customer would send on WhatsApp:
        scan          same as scanning the counter QR (sends STAMP <code>)
        wallet        balance, bars and the wallet link (open it in your browser)
        claim         claim a reward -> 4-digit code (type it in the owner portal)
        tap <id>      tap a button, e.g.  tap loy_claim   or   tap loy_claim_2
        nextday       pretend a day has passed (scan again; your streak continues)
        skipday       pretend you skipped a few days (your daily streak breaks)
        as <number>   switch to another customer, e.g.  as 919800001111
        reset         delete the demo database and start again
        quit
      Anything else is sent as a normal message ("hi", "STAMP ABC123", ...).
"""
import os
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # emoji on Windows terminals
except Exception:
    pass

DEMO_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "loyalty_demo.db")
if "reset" in sys.argv[1:] and os.path.exists(DEMO_DB):
    os.remove(DEMO_DB)

# Set before any project import. load_dotenv() never overrides these, so your
# real .env (database, tokens) is not used.
os.environ.update(
    DATABASE_URL=f"sqlite:///{DEMO_DB}",
    VERZIO_ENV="development",
    SCHEDULER="off",
    META_APP_SECRET="",
    META_ACCESS_TOKEN="simulator",
    TOKEN_ENCRYPTION_KEY="",
    VERZIO_SECRET_KEY="simulator-only-secret-key-not-for-production",
    ADMIN_USERNAME="admin",
    ADMIN_PASSWORD="verzio-dev-admin",
    COOKIE_SECURE="false",
    PUBLIC_BASE_URL="http://127.0.0.1:8000",
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import itertools
import json
import logging

logging.disable(logging.INFO)          # keep the chat readable

import warnings
warnings.filterwarnings("ignore")
import server
import whatsapp_client
from database import Business, Owner, SessionLocal, init_db
from loyalty import engine as L
from loyalty.models import LoyaltyMember, LoyaltyReward, ensure_tables
from owner.owner_auth import hash_password

PNID = "SIM_CAFE"
replies: list[dict] = []


async def _fake_post(recipient, phone_number_id, payload, token=None):
    replies.append({"to": recipient, "payload": payload})
    return None

whatsapp_client._post = _fake_post     # nothing ever goes to Meta


def setup() -> int:
    """Create the demo cafe once. Returns its business id."""
    init_db()
    ensure_tables()
    whatsapp_client.ensure_window_table()
    db = SessionLocal()
    try:
        biz = db.query(Business).filter_by(whatsapp_business_phone_number_id=PNID).first()
        if biz is None:
            hours = {d: ["07:00", "23:00"] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
            biz = Business(name="Bean There Cafe (demo)", whatsapp_business_phone_number_id=PNID,
                           manager_phone_number="919800000099", timezone="Asia/Kolkata", operational_hours=hours,
                           holidays=[], is_active=True, accepting_bookings=False, approval_mode="automatic",
                           notification_preferences={})
            db.add(biz)
            db.commit()
            db.add(Owner(business_id=biz.id, full_name="Demo Owner", email="demo@cafe.test",
                         password_hash=hash_password("demo-pass-123"), is_active=True))
            settings = L.ensure_settings(db, biz)
            settings.enabled, settings.mode = True, "only"
            settings.points_per_visit, settings.welcome_bonus = 10, 10
            settings.daily_streak_on, settings.weekly_streak_on = True, True
            settings.whatsapp_number = "919876543210"
            for name, pts in (("Free Brownie", 30), ("Free Cappuccino", 60), ("Lunch Combo", 150)):
                db.add(LoyaltyReward(business_id=biz.id, name=name, points=pts))
            db.commit()
        return biz.id
    finally:
        db.close()


def show(payload: dict) -> None:
    kind = payload.get("type")
    print()
    print("  " + (payload.get("body") or "").replace("\n", "\n  "))
    if kind == "buttons":
        print("  " + "   ".join(f"[{b['title']}] (tap {b['id']})" for b in payload["buttons"]))
    elif kind == "list":
        print(f"  [{payload.get('button')}]")
        for section in payload.get("sections", []):
            for row in section.get("rows", []):
                print(f"    - {row['title']}  {row.get('description', '')}  (tap {row['id']})")
    elif kind == "cta_url":
        print(f"  [{payload['display_text']}] -> {payload['url']}")
    print()


def chat() -> None:
    from fastapi.testclient import TestClient
    biz_id = setup()
    phone = "919811112222"
    ids = itertools.count(1)
    client = TestClient(server.app)
    client.__enter__()
    db = SessionLocal()
    code = L.get_settings(db, biz_id).earn_code
    print(__doc__.split("Terminal 2 (the customer):")[1].split("Anything else")[0])
    print(f"Demo cafe ready. Counter QR code: {code}. You are customer +{phone}.\n")

    while True:
        try:
            line = input(f"+{phone[-4:]} > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        low = line.lower()
        if low in ("quit", "exit", "q"):
            break
        if low == "reset":
            print("Close this window, then run:  python simulate_loyalty.py reset")
            continue
        if low.startswith("as "):
            phone = whatsapp_client.canonical_phone(line[3:], "Asia/Kolkata")
            print(f"Now you are customer +{phone}.")
            continue
        if low in ("nextday", "skipday"):
            # Move this customer's last visit back in time instead of moving the clock.
            from datetime import timedelta
            from booking_engine import business_today
            db.expire_all()
            member = L.find_member(db, biz_id, phone)
            biz = db.get(Business, biz_id)
            if member and member.last_visit_date:
                today = business_today(biz)
                back = L.previous_open_day(biz, today) if low == "nextday" else today - timedelta(days=3)
                member.last_visit_date = back.isoformat()
                db.commit()
            print("OK, it's tomorrow. You can scan again." if low == "nextday"
                  else "OK, you skipped a few days. Your daily streak will restart.")
            continue
        db.expire_all()
        code = L.get_settings(db, biz_id).earn_code          # in case the owner made a new QR
        if low == "scan":
            msg = {"type": "text", "text": {"body": f"STAMP {code}"}}
            print(f"(sending: STAMP {code})")
        elif low.startswith("tap "):
            msg = {"type": "interactive", "interactive": {"type": "button_reply",
                   "button_reply": {"id": line[4:].strip(), "title": "tap"}}}
        else:
            msg = {"type": "text", "text": {"body": line}}
        body = {"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PNID},
            "contacts": [{"wa_id": phone, "profile": {"name": "Asha Rao"}}],
            "messages": [{"id": f"sim.{os.getpid()}.{next(ids)}", "from": phone, **msg}]}}]}]}
        replies.clear()
        client.post("/webhook", content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        if not replies:
            print("  (no reply)\n")
        for r in replies:
            show(r["payload"])
    client.__exit__(None, None, None)


def serve() -> None:
    import uvicorn
    setup()
    print("Demo website on http://127.0.0.1:8000  (Ctrl+C to stop)")
    print("Owner portal: http://127.0.0.1:8000/owner/login   demo@cafe.test / demo-pass-123")
    print("Messages the portal would send on WhatsApp are printed here instead.\n")

    async def printing_post(recipient, phone_number_id, payload, token=None):
        print(f"\n[WhatsApp to +{recipient}]")
        show(payload)
        return None
    whatsapp_client._post = printing_post
    uvicorn.run(server.app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    serve() if "--serve" in sys.argv[1:] else chat()
