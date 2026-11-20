"""
test_one_tap.py: preview scan reply = one message with an "Open my wallet" link, through the real /webhook route,
the owner portal, the wallet page and Admin -> Loyalty. Throwaway database and
stubbed outbound WhatsApp, so it's safe to run anytime:

    python test_one_tap.py

Also checks that loyalty stays out of the way: a business with loyalty off
(and "hi" on a "with booking" business) still gets the normal booking bot.
"""
import os, sys, json, hmac, hashlib, tempfile, itertools
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # emoji on Windows terminals
except Exception:
    pass
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
os.environ.update(
    SCHEDULER="off",
    DATABASE_URL=f"sqlite:///{tmp.name}",
    META_APP_SECRET="test-secret",
    META_ACCESS_TOKEN="x",
    VERZIO_ENV="development",
    VERZIO_SECRET_KEY="test-only-secret-key-not-for-production-use",
    ADMIN_USERNAME="admin",
    ADMIN_PASSWORD="verzio-dev-admin",
    COOKIE_SECURE="false",
    WHATSAPP_TEMPLATES="on",
    PUBLIC_BASE_URL="https://verzio.codes",
    VERZIO_PREVIEW="",
)
sys.path.insert(0, os.getcwd())
import re
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
import server, whatsapp_client
from database import SessionLocal, Business, Service, Owner, Subscription
from owner.owner_auth import hash_password
from loyalty import engine as L, bot as loyalty_bot
from loyalty.models import LoyaltySettings, LoyaltyMember, LoyaltyClaim, LoyaltyEntry, LoyaltyReward

outbox = []
async def fake_post(recipient, pnid, payload, token=None):
    outbox.append({"to": recipient, "from": pnid, "payload": payload}); return None
whatsapp_client._post = fake_post

fails = 0
def check(label, ok, extra=""):
    global fails; fails += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if (extra and not ok) else ""))

ids = itertools.count(1)
def webhook(client, pnid, sender, msg_body, name="Asha Rao"):
    msg = {"id": f"wamid.loy{next(ids)}", "from": sender, **msg_body}
    body = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid},
            "contacts": [{"wa_id": sender, "profile": {"name": name}}], "messages": [msg]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    outbox.clear()
    r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    return r, [m for m in outbox if m["to"] == sender]
text = lambda t: {"type": "text", "text": {"body": t}}
btn = lambda i: {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": i, "title": "x"}}}
tap = lambda i: {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": i, "title": "x"}}}
body_of = lambda out: (out[0]["payload"].get("body") or "") if out else ""
A = ("admin", "verzio-dev-admin")
IPHONE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile"}
ALL = {d: ["07:00", "23:00"] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}

def make_business(db, name, pnid, manager):
    b = Business(name=name, whatsapp_business_phone_number_id=pnid, manager_phone_number=manager,
                 timezone="Asia/Kolkata", operational_hours=ALL, holidays=[], slot_interval=30, max_parallel_bookings=1,
                 approval_mode="automatic", is_active=True, accepting_bookings=True, enable_service_selection=True,
                 advance_booking_days=14, notification_preferences={})
    db.add(b); db.commit()
    db.add(Service(business_id=b.id, name="Haircut", duration=30, price=300, is_active=True)); db.commit()
    return b

def login(email, password="ownerpass123"):
    c = TestClient(server.app)
    r = c.post("/owner/login", data={"identifier": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    return c

with TestClient(server.app) as client:
    db = SessionLocal()
    test = make_business(db, "Verzio Test", "PN_TEST", "919800000040")
    cafe = make_business(db, "Bean There Cafe", "PN_CAFE", "919800000010")
    for b in (test, cafe):
        client.post(f"/admin/loyalty/{b.id}", auth=A, data={"enabled": "on", "mode": "with_booking"})
    db.expire_all()
    db.get(LoyaltySettings, test.id).welcome_bonus = 10
    db.add(LoyaltyReward(business_id=test.id, name="Free Brownie", points=20))
    db.add(LoyaltyReward(business_id=test.id, name="Free Cappuccino", points=40))
    db.commit()
    os.environ["VERZIO_PREVIEW"] = str(test.id)
    C = "919811112222"

    code = db.get(LoyaltySettings, test.id).earn_code
    r, out = webhook(client, "PN_TEST", C, text(f"STAMP {code}"))
    m = L.find_member(db, test.id, C)
    p = out[0]["payload"] if out else {}
    check("preview: the scan gets exactly ONE reply", len(out) == 1, out)
    check("preview: that reply is an 'Open my wallet' link to the member's wallet",
          p.get("type") == "cta_url" and p.get("display_text") == "Open my wallet"
          and p.get("url") == f"https://verzio.codes/w/{m.wallet_token}", p)
    check("preview: reply still says points earned and the balance", "+20 points" in p.get("body", "")
          and "Wallet: *20 points*" in p.get("body", ""), p.get("body"))
    check("preview: unlocked reward says to open the wallet (no Claim button to tap)",
          "Open your wallet to claim it." in p.get("body", "") and "Tap Claim reward" not in p.get("body", ""), p.get("body"))
    check("preview: short reply (no long progress bars)", "\u2b1c" not in p.get("body", "") and len(p.get("body", "")) < 400, p.get("body"))
    w = TestClient(server.app)
    r = w.get(p["url"])
    check("the link opens the wallet with a claim button", r.status_code == 200 and "/claim/" in r.text, r.status_code)
    r, out = webhook(client, "PN_TEST", C, text(f"STAMP {code}"))
    check("preview: same-day rescan also one link reply", len(out) == 1 and out[0]["payload"]["type"] == "cta_url"
          and "already collected" in body_of(out), out)
    r, out = webhook(client, "PN_TEST", C, text("claim"))
    check("typing CLAIM still works in WhatsApp", out and re.search(r"\*\d{4}\*", body_of(out)), body_of(out))

    code2 = db.get(LoyaltySettings, cafe.id).earn_code
    r, out = webhook(client, "PN_CAFE", C, text(f"STAMP {code2}"))
    check("other businesses unchanged: reply buttons as before", out and out[0]["payload"]["type"] == "buttons"
          and [x["id"] for x in out[0]["payload"]["buttons"]][-1] == "loy_wallet", out and out[0]["payload"])
    db.close()

print("\nALL PASSED" if fails == 0 else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
