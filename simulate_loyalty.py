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

      Loyalty Plus:
        plus          switch on every Plus feature with demo values (or use Grow in the portal)
        codes         counter codes (main + outlets) and your live bill code
        scan <outlet> scan an outlet's QR, e.g.  scan koregaon
        invite        your invite message; then  as 919855556666  and paste the JOIN line
        birthday      pretend today is your birthday and run the birthday job
        monday        pretend it's next Monday 10 AM and send the weekly summary
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
    LOYALTY_PLUS="all",                  # demo database only: Loyalty Plus available
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
from loyalty.models import LoyaltyBillCode, LoyaltyMember, LoyaltyReward, ensure_tables
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
        if low in ("plus", "codes", "birthday", "monday"):
            plus_command(db, biz_id, phone, low)
            continue
        db.expire_all()
        code = L.get_settings(db, biz_id).earn_code          # in case the owner made a new QR
        if low.startswith("scan ") and len(low) > 5:
            want = low[5:].strip()
            outlet = next((o for o in L.outlets(db, biz_id) if want in o.name.lower()), None)
            if outlet is None:
                print("  No active outlet with that name. Add one in Settings -> Outlets, then type  codes\n")
                continue
            code, low = outlet.earn_code, "scan"
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
            if r["to"] != phone:
                print(f"\n[WhatsApp to +{r['to']}]")
            show(r["payload"])
    client.__exit__(None, None, None)


def plus_command(db, biz_id: int, phone: str, cmd: str) -> None:
    """Loyalty Plus helpers for the simulator."""
    import asyncio as _asyncio
    from datetime import datetime as _dt, timedelta as _td
    import booking_engine
    from loyalty import jobs
    db.expire_all()
    biz = db.get(Business, biz_id)
    settings = L.get_settings(db, biz_id)
    if cmd == "plus":
        settings.tiers_on, settings.silver_visits, settings.gold_visits = True, 3, 6
        settings.bill_points_on, settings.points_per_100, settings.avg_bill = True, 5, 300
        settings.birthday_on, settings.referral_on, settings.summary_on = True, True, True
        settings.review_on, settings.review_link, settings.review_after = True, "https://g.page/r/demo/review", 4
        db.commit()
        print("  Plus switched on: Silver after 3 visits, Gold after 6, bill points (5 per ₹100), invites,\n"
              "  birthday gift, review ask on visit 4, Monday summary. Change them in the portal: Grow.\n"
              "  Add outlets and staff logins in Settings; double points in Grow.\n")
        return
    if cmd == "codes":
        print(f"  {settings.main_outlet_name or 'Main counter'}: {settings.earn_code}")
        for o in L.outlets(db, biz_id):
            print(f"  {o.name}: {o.earn_code}   (type: scan {o.name.split()[0].lower()})")
        member = L.find_member(db, biz_id, phone)
        bill = None
        if member:
            bill = (db.query(LoyaltyBillCode).filter(LoyaltyBillCode.member_id == member.id,
                                                     LoyaltyBillCode.status == "pending",
                                                     LoyaltyBillCode.expires_at > _dt.utcnow())
                    .order_by(LoyaltyBillCode.id.desc()).first())
        print(f"  Your bill code: {bill.code if bill else '(none: scan first, with bill points on)'}\n")
        return
    real_now = booking_engine.business_now
    if cmd == "birthday":
        member = L.find_member(db, biz_id, phone)
        if member is None:
            print("  Scan first so you're a member.\n")
            return
        if not settings.birthday_on:
            print("  Birthday gift is off. Type  plus  or switch it on in Grow.\n")
            return
        today = real_now(biz).date()
        member.birthday, member.offers_off = today.strftime("%m-%d"), False
        db.commit()
        from scheduler import release
        release("loy_bday", f"{member.id}:{today.year}")
        fake = _dt.combine(today, _dt.min.time()).replace(hour=12)
    else:
        if not settings.summary_on:
            print("  Monday summary is off. Type  plus  or switch it on in Grow.\n")
            return
        d = real_now(biz).date()
        monday = d + _td(days=7 - d.weekday())          # next Monday: "last week" includes today
        fake = _dt.combine(monday, _dt.min.time()).replace(hour=10)
        from scheduler import release
        year, week, _ = (monday - _td(days=7)).isocalendar()
        release("loy_week", f"{biz_id}:{year}-{week:02d}")
    booking_engine.business_now = lambda b: fake
    replies.clear()
    jdb = SessionLocal()
    try:
        result = _asyncio.run(jobs.run(jdb))
    finally:
        jdb.close()
        booking_engine.business_now = real_now
    if not replies:
        print(f"  (nothing sent: {result})\n")
    for r in replies:
        print(f"\n[WhatsApp to +{r['to']}]")
        p = r["payload"]
        if p.get("type") == "template":
            params = [x["text"] for c in p["template"]["components"] if c["type"] == "body" for x in c["parameters"]]
            print(f"  (template {p['template']['name']}: {', '.join(params)})\n")
        else:
            show(p)


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
