"""
test_loyalty_plus.py: Verzio Loyalty Plus end to end, through the real /webhook
route, the owner portal, the wallet page and the scheduler jobs. Throwaway
database, stubbed outbound WhatsApp and a controllable business clock:

    python test_loyalty_plus.py

Covers tiers, double points, outlets, bill points, invites, birthdays, the
Google review ask, counter-staff logins, the Results page and the Monday
summary, and checks that a business with none of it switched on behaves
exactly as before.
"""
import os, sys, json, hmac, hashlib, tempfile, itertools, asyncio, time
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
)
sys.path.insert(0, os.getcwd())
import re
from datetime import datetime, timedelta, date
from fastapi.testclient import TestClient
import server, whatsapp_client, booking_engine
from database import SessionLocal, Business, Service, Owner
from owner.owner_auth import hash_password
from loyalty import engine as L, jobs as LJ
from loyalty.models import (LoyaltySettings, LoyaltyMember, LoyaltyEntry, LoyaltyReward, LoyaltyOutlet,
                            LoyaltyBillCode, LoyaltyReferral, LoyaltyBoost)

outbox = []
async def fake_post(recipient, pnid, payload, token=None):
    outbox.append({"to": recipient, "from": pnid, "payload": payload}); return None
whatsapp_client._post = fake_post

# A business clock the test can move: earn_visit, streaks, boosts and jobs all read it.
CLOCK = {"now": datetime(2026, 11, 10, 12, 0)}          # a Tuesday, noon
booking_engine.business_now = lambda biz: CLOCK["now"]
def set_clock(dt): CLOCK["now"] = dt

fails = 0
def check(label, ok, extra=""):
    global fails; fails += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if (extra and not ok) else ""))

ids = itertools.count(1)
def webhook(client, pnid, sender, msg_body, name="Asha Rao"):
    msg = {"id": f"wamid.plus{next(ids)}", "from": sender, **msg_body}
    body = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid},
            "contacts": [{"wa_id": sender, "profile": {"name": name}}], "messages": [msg]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    outbox.clear()
    client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    return [m for m in outbox if m["to"] == sender]
text = lambda t: {"type": "text", "text": {"body": t}}
btn = lambda i: {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": i, "title": "x"}}}
body_of = lambda out: (out[0]["payload"].get("body") or "") if out else ""
all_bodies = lambda out: " || ".join((m["payload"].get("body") or "") for m in out)
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

def run_jobs():
    db2 = SessionLocal()
    try:
        return asyncio.run(LJ.run(db2))
    finally:
        db2.close()

def member(db, phone):
    db.expire_all()
    return db.query(LoyaltyMember).filter(LoyaltyMember.phone == phone).first()

with TestClient(server.app) as client:
    db = SessionLocal()
    cafe = make_business(db, "Bean There Cafe", "PN_CAFE", "919800000010")
    plain = make_business(db, "Plain Cafe", "PN_PLAIN", "919800000020")
    for b, email in ((cafe, "o@cafe.test"), (plain, "o@plain.test")):
        db.add(Owner(business_id=b.id, full_name=f"{b.name} Owner", email=email, phone_number=None,
                     password_hash=hash_password("ownerpass123"), is_active=True))
    db.commit()
    for b in (cafe, plain):
        client.post(f"/admin/products/{b.id}", auth=A, data={"loyalty_on": "on", "loyalty_plan": "active",
                    "loyalty_paid_until": "2027-12-31", "loyalty_amount": "1299"})
    owner = login("o@cafe.test")
    plain_owner = login("o@plain.test")
    for c in (owner, plain_owner):
        c.post("/owner/loyalty/settings", data={"points_per_visit": "10", "welcome_bonus": "0",
                                               "whatsapp_number": "98765 43210"})
        c.post("/owner/loyalty/rewards", data={"name": "Free Brownie", "points": "60"})
        c.post("/owner/loyalty/rewards", data={"name": "Free Lunch", "points": "400"})
    db.expire_all()
    st = db.get(LoyaltySettings, cafe.id)
    plain_st = db.get(LoyaltySettings, plain.id)
    CODE, PCODE = st.earn_code, plain_st.earn_code
    check("new loyalty plans default to ₹1,299", st.monthly_amount == 1299, st.monthly_amount)

    # ------------------------------------------------------------------ nothing on = as before
    P = "919822220000"
    out = webhook(client, "PN_PLAIN", P, text(f"STAMP {PCODE}"))
    b = body_of(out)
    check("Plus off: plain scan reply has no tier, bill code, invite or birthday text",
          "+10 points" in b and "Bill code" not in b and "Silver" not in b and "birthday" not in b.lower()
          and all(x["id"] != "loy_invite" for x in out[0]["payload"]["buttons"]), b)
    out = webhook(client, "PN_PLAIN", P, text("JOIN ABCDEF"))
    check("Plus off: JOIN just welcomes, no referral saved", "Welcome to Plain Cafe rewards" in body_of(out)
          and db.query(LoyaltyReferral).count() == 0, body_of(out))
    check("templates defined: loyalty_birthday (Marketing), loyalty_referral, loyalty_weekly_summary (Utility)",
          whatsapp_client.TEMPLATE_DEFINITIONS["loyalty_birthday"]["category"] == "MARKETING"
          and "category" not in whatsapp_client.TEMPLATE_DEFINITIONS["loyalty_referral"]
          and "loyalty_weekly_summary" in whatsapp_client.TEMPLATE_DEFINITIONS)

    # ------------------------------------------------------------------ Grow page + validation
    r = owner.get("/owner/loyalty/grow")
    check("Grow page lists every Plus feature", r.status_code == 200 and all(k in r.text for k in (
          'name="tiers_on"', 'name="bill_points_on"', 'name="birthday_on"', 'name="referral_on"',
          'name="review_on"', 'name="summary_on"', 'action="/owner/loyalty/boosts"')), r.status_code)
    r = owner.get("/owner/loyalty/grow", headers=IPHONE)
    check("Grow page works on a phone (tab bar: Today, Rewards, Customers, Grow, Results)",
          r.status_code == 200 and "repeat(5, 1fr)" in r.text and "/owner/loyalty/results" in r.text)
    r = owner.post("/owner/loyalty/grow", data={"tiers_on": "on", "silver_visits": "5", "gold_visits": "5"},
                   follow_redirects=False)
    check("Gold must need more visits than Silver", "Gold+needs+more" in r.headers["location"], r.headers["location"])
    r = owner.post("/owner/loyalty/grow", data={"review_on": "on", "review_link": ""}, follow_redirects=False)
    check("review ask needs a link", "review+link" in r.headers["location"], r.headers["location"])
    r = owner.post("/owner/loyalty/grow", data={"review_on": "on", "review_link": "javascript:alert(1)"},
                   follow_redirects=False)
    check("review link must be http(s)", "https" in r.headers["location"], r.headers["location"])
    GROW = {"tiers_on": "on", "silver_visits": "3", "silver_bonus": "50", "gold_visits": "6", "gold_bonus": "100",
            "bill_points_on": "on", "points_per_100": "5", "avg_bill": "300",
            "birthday_on": "on", "birthday_points": "50", "referral_on": "on", "referral_points": "30",
            "review_on": "on", "review_link": "https://g.page/r/bean/review", "review_after": "4",
            "summary_on": "on"}
    r = owner.post("/owner/loyalty/grow", data=GROW, follow_redirects=False)
    db.expire_all(); st = db.get(LoyaltySettings, cafe.id)
    check("owner switches on every Plus feature", "ok=" in r.headers["location"] and st.tiers_on and st.bill_points_on
          and st.birthday_on and st.referral_on and st.review_on and st.summary_on and st.gold_bonus == 100,
          r.headers["location"])
    r = plain_owner.post("/owner/loyalty/grow", data={"bill_points_on": "on", "points_per_100": "0"}, follow_redirects=False)
    check("bill points need points per ₹100", "100" in r.headers["location"] and "err=" in r.headers["location"])

    # ------------------------------------------------------------------ outlets
    r = owner.post("/owner/loyalty/outlets", data={"main_name": "Baner", "name": "Koregaon Park"}, follow_redirects=False)
    db.expire_all()
    kp = db.query(LoyaltyOutlet).filter(LoyaltyOutlet.business_id == cafe.id).first()
    check("add an outlet: own 6-letter code; the main counter gets a name", kp and len(kp.earn_code) == 6
          and kp.earn_code != CODE and db.get(LoyaltySettings, cafe.id).main_outlet_name == "Baner", r.headers["location"])
    r = owner.post("/owner/loyalty/outlets", data={"name": "koregaon park"}, follow_redirects=False)
    check("duplicate outlet name refused", "already" in r.headers["location"])
    r = owner.get("/owner/loyalty/settings")
    check("Settings shows outlets with QR links and the staff section", kp.earn_code in r.text
          and f"/owner/loyalty/qr?outlet={kp.id}" in r.text and 'action="/owner/loyalty/staff"' in r.text)
    r = owner.get(f"/owner/loyalty/qr?outlet={kp.id}")
    check("outlet QR poster shows the outlet name", r.status_code == 200 and "Koregaon Park" in r.text and "<svg" in r.text)
    check("another business's outlet id -> not found", "Outlet+not+found" in
          plain_owner.get(f"/owner/loyalty/qr?outlet={kp.id}", follow_redirects=False).headers["location"])

    # ------------------------------------------------------------------ scan at an outlet + bill code
    C = "919811112222"
    set_clock(datetime(2026, 11, 10, 12, 0))
    out = webhook(client, "PN_CAFE", C, text(f"STAMP {kp.earn_code}"))
    b = body_of(out)
    m = member(db, C)
    e = db.query(LoyaltyEntry).filter(LoyaltyEntry.member_id == m.id, LoyaltyEntry.kind == "visit").first()
    bc = db.query(LoyaltyBillCode).filter(LoyaltyBillCode.member_id == m.id).first()
    check("scan with an outlet's code: points, outlet named in the reply, visit tagged with the outlet",
          "Bean There Cafe (Koregaon Park)" in b and m.points == 10 and e.outlet_id == kp.id, b)
    check("bill points on: reply carries a 4-digit bill code", bc and re.search(rf"Bill code: \*{bc.code}\*", b), b)
    check("Invite friends button shown when invites are on",
          any(x["id"] == "loy_invite" for x in out[0]["payload"]["buttons"]), out[0]["payload"]["buttons"])
    out = webhook(client, "PN_CAFE", C, text(f"STAMP {CODE}"))
    check("once a day across outlets: main counter the same day -> already collected",
          "already collected" in body_of(out) and member(db, C).points == 10, body_of(out))
    db.expire_all()
    codes = db.query(LoyaltyBillCode).filter(LoyaltyBillCode.member_id == m.id).order_by(LoyaltyBillCode.id).all()
    check("a second scan still gives a new bill code and replaces the old one",
          len(codes) == 2 and codes[0].status == "replaced" and codes[1].status == "pending")
    BILL = codes[1].code
    r = owner.post("/owner/loyalty/bill", data={"code": BILL, "amount": "₹1,250"}, follow_redirects=False)
    db.expire_all(); m = member(db, C)
    be = db.query(LoyaltyEntry).filter(LoyaltyEntry.member_id == m.id, LoyaltyEntry.kind == "bill").first()
    check("bill ₹1,250 -> 12 x 5 = 60 points, saved with the amount and the scan's counter (main)",
          m.points == 70 and be and be.bill_amount == 1250 and be.delta == 60 and be.outlet_id is None,
          (m.points, be and be.delta))
    check("customer told about bill points (inside the 24 h window)",
          any("+60 points for your ₹1,250 bill" in (x["payload"].get("body") or "") for x in outbox))
    r = owner.post("/owner/loyalty/bill", data={"code": BILL, "amount": "500"}, follow_redirects=False)
    check("a bill code works once", "already+used" in r.headers["location"], r.headers["location"])
    r = owner.post("/owner/loyalty/bill", data={"code": codes[0].code, "amount": "500"}, follow_redirects=False)
    check("a replaced bill code is refused", "newest+bill+code" in r.headers["location"], r.headers["location"])
    r = owner.post("/owner/loyalty/bill", data={"code": "", "phone": "98111 12222", "amount": "abc"}, follow_redirects=False)
    check("bill amount must be a number", "bill+amount" in r.headers["location"], r.headers["location"])
    r = owner.post("/owner/loyalty/bill", data={"phone": "98111 12222", "amount": "99"}, follow_redirects=False)
    check("bill under ₹100 by phone: saved, 0 points, explained", "under+%E2%82%B9100" in r.headers["location"]
          and member(db, C).points == 70, r.headers["location"])
    r = plain_owner.post("/owner/loyalty/bill", data={"code": BILL, "amount": "500"}, follow_redirects=False)
    check("a business without bill points can't add bills", "aren%27t+switched+on" in r.headers["location"], r.headers["location"])

    # ------------------------------------------------------------------ double points
    r = owner.post("/owner/loyalty/boosts", data={"days": ["2"], "start": "15:00", "end": "18:00", "multiplier": "2"},
                   follow_redirects=False)
    check("add a double-points rule (Wed 3–6 PM)", "ok=" in r.headers["location"]
          and db.query(LoyaltyBoost).count() == 1, r.headers["location"])
    r = owner.post("/owner/loyalty/boosts", data={"days": ["2"], "start": "18:00", "end": "15:00"}, follow_redirects=False)
    check("end before start refused", "end+time" in r.headers["location"], r.headers["location"])
    r = owner.post("/owner/loyalty/boosts", data={"start": "", "end": ""}, follow_redirects=False)
    check("a rule needs at least one day", "at+least+one+day" in r.headers["location"], r.headers["location"])
    set_clock(datetime(2026, 11, 11, 16, 0))            # Wednesday 4 PM: inside the rule
    r = owner.get("/owner/loyalty")
    check("dashboard shows '2x points right now'", "2x points right now" in r.text)
    out = webhook(client, "PN_CAFE", C, text(f"STAMP {CODE}"))
    db.expire_all(); m = member(db, C)
    check("scan during double points: +20 and '2x points' in the reply", "+20 points" in body_of(out)
          and "2x points right now" in body_of(out) and m.visits == 2, body_of(out))
    check("2nd visit: birthday asked once (review not due yet)", "reply with your birthday" in body_of(out)
          and m.birthday_asked and "g.page" not in body_of(out), body_of(out))
    ve = db.query(LoyaltyEntry).filter(LoyaltyEntry.member_id == m.id, LoyaltyEntry.kind == "visit").order_by(LoyaltyEntry.id.desc()).first()
    check("boosted visit recorded at the main counter with boost=2", ve.boost == 2 and ve.outlet_id is None)
    set_clock(datetime(2026, 11, 11, 18, 30))
    check("after 6 PM no boost", L.active_boost(db, cafe, CLOCK["now"]) == 1)

    # ------------------------------------------------------------------ tiers
    set_clock(datetime(2026, 11, 12, 11, 0))
    out = webhook(client, "PN_CAFE", C, text(f"STAMP {CODE}"))
    db.expire_all(); m = member(db, C)
    check("3rd visit reaches Silver: announced in the reply", m.visits == 3 and "You're now a *Silver* member" in body_of(out),
          body_of(out))
    set_clock(datetime(2026, 11, 13, 11, 0))
    before = m.points
    out = webhook(client, "PN_CAFE", C, text(f"STAMP {CODE}"))
    db.expire_all(); m = member(db, C)
    check("Silver visit: 10 + 50% = 15 points, 'Silver bonus' line", m.points - before == 15
          and "Silver bonus: +50%" in body_of(out), (m.points - before, body_of(out)))

    # ------------------------------------------------------------------ review ask (4th visit) and birthday ask
    check("4th visit: Google review link asked once, inside the reply",
          "https://g.page/r/bean/review" in body_of(out) and m.review_asked, body_of(out))
    set_clock(datetime(2026, 11, 14, 11, 0))
    out = webhook(client, "PN_CAFE", C, text(f"STAMP {CODE}"))
    db.expire_all(); m = member(db, C)
    check("next visit: no second review ask and no second birthday ask", "g.page" not in body_of(out)
          and "reply with your birthday" not in body_of(out), body_of(out))
    out = webhook(client, "PN_CAFE", C, text("14th Aug"))
    db.expire_all(); m = member(db, C)
    check("customer replies '14th Aug' -> birthday saved", m.birthday == "08-14" and "Saved: 14 August" in body_of(out),
          (m.birthday, body_of(out)))
    out = webhook(client, "PN_CAFE", C, text("20 Sep"))
    check("a later date message doesn't change it", member(db, C).birthday == "08-14")

    # ------------------------------------------------------------------ wallet page
    w = TestClient(server.app)
    r = w.get(f"/w/{m.wallet_token}")
    check("wallet: Silver badge, visits to Gold, invite share link, saved birthday",
          r.status_code == 200 and "Silver member" in r.text and "to Gold" in r.text and "Share on WhatsApp" in r.text
          and "JOIN%2520" in r.text and "Birthday: 14 August" in r.text, r.status_code)

    # ------------------------------------------------------------------ invites
    out = webhook(client, "PN_CAFE", C, btn("loy_invite"))
    m = member(db, C)
    check("Invite friends: two messages, the second has the wa.me JOIN link to forward",
          len(out) == 2 and f"wa.me/919876543210?text=JOIN%20{m.ref_code}" in out[1]["payload"]["body"], all_bodies(out))
    F = "919855556666"
    out = webhook(client, "PN_CAFE", F, text(f"JOIN {m.ref_code}"), name="Rahul Mehta")
    check("friend taps the link: welcomed, 'Asha invited you', referral saved", "Asha invited you" in body_of(out)
          and db.query(LoyaltyReferral).filter(LoyaltyReferral.friend_phone == F).count() == 1, body_of(out))
    out = webhook(client, "PN_CAFE", C, text(f"JOIN {m.ref_code}"))
    check("own invite code -> 'That's your own invite link'", "your own invite link" in body_of(out), body_of(out))
    out = webhook(client, "PN_CAFE", F, text("JOIN ZZZZZZ"))
    check("unknown invite code -> plain welcome", "Welcome to Bean There Cafe rewards" in body_of(out))
    set_clock(datetime(2026, 11, 15, 11, 0))
    asha_before = member(db, C).points
    out = webhook(client, "PN_CAFE", F, text(f"STAMP {CODE}"), name="Rahul Mehta")
    db.expire_all()
    rahul, asha = member(db, F), member(db, C)
    check("friend's first visit: +10 visit +30 invite bonus, both named in the reply",
          rahul.points == 40 and "+30 for joining with Asha's invite" in body_of(out), (rahul.points, body_of(out)))
    check("the one who invited gets +30 and a message", asha.points - asha_before == 30
          and any(x["to"] == C and "Rahul just made their first visit" in (x["payload"].get("body") or "") for x in outbox),
          asha.points - asha_before)
    out = webhook(client, "PN_CAFE", "919800001111", text(f"JOIN {asha.ref_code}"))
    set_clock(datetime(2026, 11, 16, 11, 0))
    out = webhook(client, "PN_CAFE", F, text(f"JOIN {asha.ref_code}"))
    check("existing member can't be 'invited' again", "already collecting points" in body_of(out), body_of(out))
    # Window closed for the inviter -> the Utility template is used.
    whatsapp_client.record_inbound(cafe.id, C, at=time.time() - 3 * 86400)
    db.add(LoyaltyReferral(business_id=cafe.id, referrer_id=asha.id, friend_phone="919800002222", status="pending")); db.commit()
    r = owner.post("/owner/loyalty/points", data={"phone": "98000 02222", "name": "Nina", "kind": "visit"},
                   follow_redirects=False)
    tpl = [x for x in outbox if x["to"] == C and x["payload"].get("type") == "template"]
    check("staff-added first visit also rewards the invite; inviter outside 24 h gets loyalty_referral template",
          tpl and tpl[0]["payload"]["template"]["name"] == "loyalty_referral", [x["payload"].get("type") for x in outbox])
    db.expire_all()
    asha = member(db, C)
    for i in range(12):
        db.add(LoyaltyReferral(business_id=cafe.id, referrer_id=asha.id, friend_phone=f"9198000030{i:02d}",
                               status="rewarded", rewarded_at=datetime.utcnow()))
    db.add(LoyaltyReferral(business_id=cafe.id, referrer_id=asha.id, friend_phone="919800004444", status="pending"))
    db.commit()
    before = asha.points
    L.owner_adjust(db, cafe, "919800004444", 10, None, as_visit=True)
    db.expire_all()
    ref = db.query(LoyaltyReferral).filter(LoyaltyReferral.friend_phone == "919800004444").first()
    check("over 10 rewarded invites this month: the friend still gets points, the inviter doesn't",
          ref.status == "over_cap" and member(db, C).points == before and member(db, "919800004444").points == 40,
          (ref.status, member(db, C).points - before))

    # ------------------------------------------------------------------ STOP / START
    out = webhook(client, "PN_CAFE", F, text("STOP"))
    check("member replies STOP -> offers off, still collects points", member(db, F).offers_off
          and "no more offers" in body_of(out), body_of(out))
    out = webhook(client, "PN_CAFE", F, text("start"))
    check("START -> offers back on", not member(db, F).offers_off and "Welcome back" in body_of(out))
    out = webhook(client, "PN_PLAIN", "919877778888", text("STOP"))
    check("STOP from someone who isn't a member: left alone", not out or "no more offers" not in body_of(out))

    # ------------------------------------------------------------------ birthdays (scheduler job)
    rahul = member(db, F)
    L.set_birthday(db, rahul, "11-16")
    set_clock(datetime(2026, 11, 16, 8, 0))
    check("before 10 AM: no birthday message", run_jobs()["birthdays"] == 0)
    set_clock(datetime(2026, 11, 16, 11, 0))
    outbox.clear()
    res = run_jobs()
    db.expire_all(); rahul = member(db, F)
    msg = [x for x in outbox if x["to"] == F]
    check("birthday: +50 points and a happy-birthday message with My wallet / Stop offers",
          res["birthdays"] == 1 and rahul.points == 90 and msg and "Happy birthday, Rahul" in msg[0]["payload"]["body"]
          and [b["id"] for b in msg[0]["payload"]["buttons"]] == ["loy_wallet", "loy_stop"], (res, rahul.points))
    outbox.clear()
    check("birthday sent once a year (second run: nothing)", run_jobs()["birthdays"] == 0 and not outbox)
    n = member(db, "919800002222")                       # Nina: never messaged -> window closed
    L.set_birthday(db, n, "11-17")
    L.set_birthday(db, member(db, "919800004444"), "11-17")
    member4 = member(db, "919800004444"); L.stop_offers(db, member4, True)
    set_clock(datetime(2026, 11, 17, 12, 0))
    outbox.clear()
    res = run_jobs()
    check("outside 24 h: loyalty_birthday Marketing template; STOP'd member skipped",
          res["birthdays"] == 1 and outbox and outbox[0]["payload"]["type"] == "template"
          and outbox[0]["payload"]["template"]["name"] == "loyalty_birthday"
          and all(x["to"] != "919800004444" for x in outbox), res)
    db.expire_all(); st = db.get(LoyaltySettings, cafe.id); st.offers_per_month = 1; db.commit()
    L.set_birthday(db, member(db, "919800001111") or L.get_or_create_member(db, cafe, "919800001111")[0], "11-18")
    m11 = member(db, "919800001111"); m11.last_visit_date = "2026-11-01"; db.commit()
    whatsapp_client.record_inbound(cafe.id, "919800001111", at=time.time() - 3 * 86400)
    set_clock(datetime(2026, 11, 18, 12, 0))
    check("monthly cap reached: no paid birthday message", run_jobs()["birthdays"] == 0)
    db.expire_all(); st = db.get(LoyaltySettings, cafe.id); st.offers_per_month = 300; db.commit()
    out = webhook(client, "PN_CAFE", F, btn("loy_stop"))
    check("'Stop offers' button on the birthday message works", member(db, F).offers_off)
    check("29 Feb birthdays are celebrated on 28 Feb in other years",
          L.birthday_today(type("M", (), {"birthday": "02-29"})(), date(2027, 2, 28))
          and not L.birthday_today(type("M", (), {"birthday": "02-29"})(), date(2028, 2, 28)))

    # wallet birthday form
    G = "919866667777"
    L.get_or_create_member(db, cafe, G, "Gita"); db.commit()
    g = member(db, G)
    r = w.get(f"/w/{g.wallet_token}")
    check("wallet without a birthday shows the day/month form", 'action="/w/' in r.text and "/birthday" in r.text)
    w.post(f"/w/{g.wallet_token}/birthday", data={"day": "3", "month": "12"})
    r = w.post(f"/w/{g.wallet_token}/birthday", data={"day": "4", "month": "12"}, follow_redirects=False)
    check("wallet birthday saved once; a second try is refused", member(db, G).birthday == "12-03"
          and "already" in r.headers["location"])
    r = owner.post(f"/owner/loyalty/customers/{g.id}/birthday", data={"birthday": "5 Dec"}, follow_redirects=False)
    check("owner can correct a birthday on Customers", member(db, G).birthday == "12-05")
    r = owner.get("/owner/loyalty/customers")
    check("Customers shows tier badges and birthdays", "🥈 Silver" in r.text and 'value="14 August"' in r.text)

    # ------------------------------------------------------------------ counter staff
    r = owner.post("/owner/loyalty/staff", data={"name": "Ravi", "username": "counter.kp", "password": "staffpass1",
                                                 "outlet_id": str(kp.id)}, follow_redirects=False)
    check("owner adds a counter staff login for Koregaon Park", "ok=" in r.headers["location"], r.headers["location"])
    staff = login("counter.kp", "staffpass1")
    r = staff.get("/owner/loyalty")
    check("staff dashboard: Counter, give reward, bill points; no Recent activity, no full phone numbers",
          r.status_code == 200 and "Koregaon Park" in r.text and "Give a reward" in r.text and "Bill points" in r.text
          and "Recent activity" not in r.text and "919811112222" not in r.text
          and "/owner/loyalty/settings" not in r.text, r.status_code)
    for path in ("/owner/loyalty/customers", "/owner/loyalty/results", "/owner/loyalty/grow",
                 "/owner/loyalty/settings", "/owner/loyalty/rewards", "/owner/appointments", "/owner/dashboard"):
        r = staff.get(path, follow_redirects=False)
        check(f"staff can't open {path}", r.status_code == 303 and r.headers["location"].startswith("/owner/loyalty"),
              (r.status_code, r.headers.get("location")))
    r = staff.post("/owner/loyalty/grow", data=GROW, follow_redirects=False)
    check("staff can't change Grow settings", r.status_code == 303 and "err=" in r.headers["location"]
          or r.headers["location"] == "/owner/loyalty")
    r = staff.post("/owner/loyalty/points", data={"phone": "98111 12222", "points": "500", "kind": "add"}, follow_redirects=False)
    check("staff can't add free points", "Only+the+owner" in r.headers["location"], r.headers["location"])
    set_clock(datetime(2026, 11, 19, 12, 0))
    out = webhook(client, "PN_CAFE", C, text(f"STAMP {CODE}"))
    db.expire_all()
    bill = db.query(LoyaltyBillCode).filter(LoyaltyBillCode.member_id == member(db, C).id,
                                            LoyaltyBillCode.status == "pending").first()
    r = staff.post("/owner/loyalty/bill", data={"code": bill.code, "amount": "400"}, follow_redirects=False)
    db.expire_all()
    be = db.query(LoyaltyEntry).filter(LoyaltyEntry.kind == "bill").order_by(LoyaltyEntry.id.desc()).first()
    check("staff adds a bill: Gold bonus applied after the 6th visit (4 x 5 x 2 = 40), no full number shown",
          "ok=" in r.headers["location"] and be.delta == 40 and "919811112222" not in r.headers["location"],
          (r.headers["location"], be.delta))
    r = staff.post("/owner/loyalty/points", data={"phone": "98444 55555", "name": "Walk-in", "kind": "visit"},
                   follow_redirects=False)
    db.expire_all()
    wv = db.query(LoyaltyEntry).filter(LoyaltyEntry.kind == "manual").order_by(LoyaltyEntry.id.desc()).first()
    check("staff adds a visit: tagged with the staff member's outlet", "ok=" in r.headers["location"]
          and wv.outlet_id == kp.id, (r.headers["location"], wv.outlet_id))
    r = staff.get("/owner/loyalty", headers=IPHONE)
    check("staff on a phone: one Counter tab, no Settings gear", "repeat(1, 1fr)" in r.text
          and 'href="/owner/loyalty/settings"' not in r.text)
    r = owner.post(f"/owner/loyalty/staff/{db.query(Owner).filter(Owner.email == 'counter.kp').first().id}/delete",
                   follow_redirects=False)
    check("owner removes the staff login", "removed" in r.headers["location"]
          and db.query(Owner).filter(Owner.email == "counter.kp").first() is None)

    # ------------------------------------------------------------------ results
    r = owner.get("/owner/loyalty/results")
    rs = L.results(db, cafe, db.get(LoyaltySettings, cafe.id), 30)
    check("Results: repeat visits, estimated sales (repeat x average of typed bills), by outlet",
          r.status_code == 200 and "Estimated sales from repeat visits" in r.text and "By outlet" in r.text
          and "Koregaon Park" in r.text and "Baner" in r.text and rs["repeat"] >= 5
          and rs["extra_revenue"] == rs["repeat"] * rs["avg_bill"] and rs["avg_from_bills"], rs)
    check("Results: invites and birthdays counted", rs["referrals"] >= 2 and rs["birthdays"] >= 2, rs)
    r = owner.get("/owner/loyalty/results?days=7", headers=IPHONE)
    check("Results 7 days works on a phone", r.status_code == 200 and "7 days" in r.text)
    r = plain_owner.get("/owner/loyalty/results")
    check("Results for a business with no bills: set your average bill prompt", "Set your average bill" in r.text)

    # ------------------------------------------------------------------ Monday summary
    set_clock(datetime(2026, 11, 23, 10, 0))            # Monday 10 AM
    outbox.clear()
    res = run_jobs()
    to_mgr = [x for x in outbox if x["to"] == "919800000010"]
    check("Monday 10 AM: weekly summary to the manager number", res["summaries"] == 1 and to_mgr
          and ("Your week at Bean There Cafe" in json.dumps(to_mgr[0]["payload"], ensure_ascii=False)
               or to_mgr[0]["payload"].get("template", {}).get("name") == "loyalty_weekly_summary"), res)
    outbox.clear()
    check("weekly summary only once per week", run_jobs()["summaries"] == 0)
    set_clock(datetime(2026, 11, 24, 10, 0))
    check("not on Tuesday", run_jobs()["summaries"] == 0)

    # ------------------------------------------------------------------ outlet pause / rotate
    owner.post(f"/owner/loyalty/outlets/{kp.id}/toggle")
    set_clock(datetime(2026, 11, 25, 12, 0))
    out = webhook(client, "PN_CAFE", C, text(f"STAMP {kp.earn_code}"))
    check("paused outlet's QR no longer gives points", "didn't work" in body_of(out), body_of(out))
    owner.post(f"/owner/loyalty/outlets/{kp.id}/toggle")
    old = kp.earn_code
    owner.post(f"/owner/loyalty/outlets/{kp.id}/rotate")
    db.expire_all()
    check("new code for an outlet replaces the old one", db.get(LoyaltyOutlet, kp.id).earn_code != old)

    # ------------------------------------------------------------------ plain business still unchanged
    set_clock(datetime(2026, 11, 26, 12, 0))
    out = webhook(client, "PN_PLAIN", P, text(f"STAMP {PCODE}"))
    check("Plus off business: second-day scan is plain +10 as before", "+10 points at Plain Cafe" in body_of(out)
          and "Bill code" not in body_of(out) and "birthday" not in body_of(out).lower(), body_of(out))
    res = run_jobs()
    check("jobs leave a Plus-off business alone", not any(x["to"] == "919800000020" for x in outbox))

    db.close()

print()
print("ALL PASSED" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
