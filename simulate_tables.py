"""
simulate_tables.py: try Verzio Tables on your PC without WhatsApp.

Real WhatsApp messages only reach the server, so this fakes them. It uses its
own demo database (tables_demo.db), never your real one, and never sends
anything to Meta.

Two terminals, both in the project folder:

  Terminal 1 (the website):   python simulate_tables.py --serve
      Owner portal:  http://127.0.0.1:8000/owner/login
                     demo@rooftop.test / demo-pass-123   (owner)
                     door.demo / door-pass-123           (door staff)
      Admin:         http://127.0.0.1:8000/admin/products  (admin / verzio-dev-admin)
      WhatsApp messages the website sends (confirmations, approvals...) are printed here.

  Terminal 2 (the guest's WhatsApp):  python simulate_tables.py
      Type what a guest would send on WhatsApp:
        hi            get the "Book a table" link: open it in your browser
        tap <id>      tap a button, e.g.  tap tbl_cancel_3   (the ids are shown under each message)
        manager       become the manager's phone (to Accept / Decline big groups)
        as <number>   become another guest, e.g.  as 919800001111
        jobs          run the timed jobs now: reminders, waitlist offers, review asks, offers
        older         pretend every booking was made yesterday (so "jobs" sends reminders)
        reset         start again with an empty demo
        quit
      Anything else is sent as a normal message ("table for 4?", "cancel", "STOP"...).

The demo rooftop is open 10 am to 2 am every day, so there are always times to book.
"""
import os
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # emoji on Windows terminals
except Exception:
    pass

DEMO_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tables_demo.db")
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
from owner.owner_auth import hash_password
from tables import engine as T
from tables.models import ensure_tables

PNID = "SIM_TABLES"
MANAGER = "919800000099"
replies: list[dict] = []


async def _fake_post(recipient, phone_number_id, payload, token=None):
    replies.append({"to": recipient, "payload": payload})
    return None

whatsapp_client._post = _fake_post     # nothing ever goes to Meta


def setup() -> int:
    """Create the demo rooftop once. Returns its business id."""
    init_db()
    ensure_tables()
    whatsapp_client.ensure_window_table()
    db = SessionLocal()
    try:
        biz = db.query(Business).filter_by(whatsapp_business_phone_number_id=PNID).first()
        if biz is None:
            hours = {d: ["10:00", "02:00"] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
            biz = Business(name="Skyline Social (demo)", whatsapp_business_phone_number_id=PNID,
                           manager_phone_number=MANAGER, timezone="Asia/Kolkata", operational_hours=hours,
                           holidays=[], is_active=True, accepting_bookings=False, approval_mode="automatic",
                           notification_preferences={})
            db.add(biz)
            db.commit()
            db.add(Owner(business_id=biz.id, full_name="Demo Owner", email="demo@rooftop.test",
                         password_hash=hash_password("demo-pass-123"), is_active=True))
            db.commit()
            settings = T.ensure_settings(db, biz)
            settings.enabled = True
            settings.google_review_url = "https://g.page/r/demo/review"
            db.commit()
            T.add_demo_zones(db, biz)
            T.create_staff(db, biz, "Door (demo)", "door.demo", "door-pass-123")
        return biz.id
    finally:
        db.close()


def show(payload: dict, to: str | None = None) -> None:
    kind = payload.get("type")
    print()
    if to:
        print(f"  [to +{to}]")
    if kind == "template":
        t = payload["template"]
        params = [p.get("text") for c in t.get("components", []) if c["type"] == "body" for p in c["parameters"]]
        taps = [c["parameters"][0]["payload"] for c in t.get("components", []) if c["type"] == "button"]
        print(f"  (approved template '{t['name']}', sent because this person hasn't messaged in 24 h)")
        print("  " + " | ".join(params))
        if taps:
            print("  buttons: " + "   ".join(f"(tap {p})" for p in taps))
        print()
        return
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
    import scheduler
    from datetime import datetime, timedelta
    from tables.models import TablesBooking
    biz_id = setup()
    phone, name = "919811122233", "Asha Rao"
    ids = itertools.count(1)
    client = TestClient(server.app)
    client.__enter__()
    print(__doc__.split("Terminal 2 (the guest's WhatsApp):")[1].split("The demo rooftop")[0])
    print(f"Demo rooftop ready. You are guest +{phone}. Make sure Terminal 1 is running, then type: hi\n")

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
            print("Close both windows, then run:  python simulate_tables.py reset")
            continue
        if low == "manager":
            phone, name = MANAGER, "Manager"
            print(f"Now you are the manager's phone (+{phone}).")
            continue
        if low.startswith("as "):
            phone, name = whatsapp_client.canonical_phone(line[3:], "Asia/Kolkata"), "Guest"
            print(f"Now you are guest +{phone}.")
            continue
        if low == "older":
            db = SessionLocal()
            db.query(TablesBooking).update({TablesBooking.created_at: datetime.utcnow() - timedelta(days=1)})
            db.commit(); db.close()
            print("OK, every booking now looks like it was made yesterday. Type jobs.")
            continue
        if low == "jobs":
            replies.clear()
            result = asyncio.run(scheduler.tick())
            print(f"  jobs ran: {result.get('tables')}")
            if not replies:
                print("  (nothing to send right now)\n")
            for r in replies:
                show(r["payload"], r["to"])
            continue
        if low.startswith("tap "):
            msg = {"type": "interactive", "interactive": {"type": "button_reply",
                   "button_reply": {"id": line[4:].strip(), "title": "tap"}}}
        else:
            msg = {"type": "text", "text": {"body": line}}
        body = {"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PNID},
            "contacts": [{"wa_id": phone, "profile": {"name": name}}],
            "messages": [{"id": f"sim.{os.getpid()}.{next(ids)}", "from": phone, **msg}]}}]}]}
        replies.clear()
        client.post("/webhook", content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        if not replies:
            print("  (no reply: on a real number the owner answers this in the WhatsApp Business app)\n")
        for r in replies:
            show(r["payload"], None if r["to"] == phone else r["to"])
    client.__exit__(None, None, None)


def serve() -> None:
    import uvicorn
    setup()
    print("Demo website on http://127.0.0.1:8000  (Ctrl+C to stop)")
    print("Owner portal: http://127.0.0.1:8000/owner/login   demo@rooftop.test / demo-pass-123")
    print("Door staff:   door.demo / door-pass-123")
    print("WhatsApp messages the website sends are printed here instead.\n")

    async def printing_post(recipient, phone_number_id, payload, token=None):
        print(f"\n[WhatsApp to +{recipient}]")
        show(payload)
        return None
    whatsapp_client._post = printing_post
    uvicorn.run(server.app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    serve() if "--serve" in sys.argv[1:] else chat()
