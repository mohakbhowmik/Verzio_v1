"""
test_loyalty.py: Verzio Loyalty end to end, through the real /webhook route,
the owner portal, the wallet page and Admin -> Loyalty. Throwaway database and
stubbed outbound WhatsApp, so it's safe to run anytime:

    python test_loyalty.py

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
    cafe = make_business(db, "Bean There Cafe", "PN_CAFE", "919800000010")
    salon = make_business(db, "Glow Salon", "PN_SALON", "919800000020")
    other = make_business(db, "Other Cafe", "PN_OTHER", "919800000030")
    for b, email in ((cafe, "o@cafe.test"), (salon, "o@salon.test"), (other, "o@other.test")):
        db.add(Owner(business_id=b.id, full_name=f"{b.name} Owner", email=email, phone_number=None,
                     password_hash=hash_password("ownerpass123"), is_active=True))
    db.commit()
    C = "919811112222"       # a customer
    D = "919833334444"       # another customer

    # ------------------------------------------------------------------ off by default
    r, out = webhook(client, "PN_SALON", C, text("STAMP ABC123"))
    check("loyalty OFF: 'STAMP ...' goes to the booking bot as before (no points, no loyalty reply)",
          "Would you like to book" in body_of(out) and db.query(LoyaltyMember).count() == 0, body_of(out))
    r, out = webhook(client, "PN_SALON", C, text("hi"))
    check("loyalty OFF: 'hi' still gets the booking list", out and out[0]["payload"]["type"] == "list", out)
    salon_owner = login("o@salon.test")
    r = salon_owner.get("/owner/appointments")
    check("loyalty OFF: owner menu has no Loyalty link", r.status_code == 200 and "/owner/loyalty" not in r.text)
    r = salon_owner.get("/owner/loyalty")
    check("loyalty OFF: Loyalty page says it isn't switched on", r.status_code == 200 and "isn't switched on" in r.text)
    r = salon_owner.post("/owner/loyalty/rewards", data={"name": "Free", "points": "10"}, follow_redirects=False)
    check("loyalty OFF: owner can't add rewards", r.status_code == 303 and db.query(LoyaltyReward).count() == 0)

    # ------------------------------------------------------------------ admin switch
    r = client.get("/admin/loyalty", auth=A)
    check("admin Loyalty page renders with every business", r.status_code == 200 and "Bean There Cafe" in r.text
          and "Glow Salon" in r.text and 'href="/admin/businesses"' in r.text, r.status_code)
    check("admin Loyalty page needs the admin login", TestClient(server.app).get("/admin/loyalty").status_code == 401)
    r = client.post(f"/admin/loyalty/{cafe.id}", auth=A, data={"enabled": "on", "mode": "with_booking"}, follow_redirects=False)
    db.expire_all()
    st = db.get(LoyaltySettings, cafe.id)
    check("admin switches loyalty ON for the cafe (with booking)", r.status_code == 303 and st and st.enabled
          and st.mode == "with_booking" and len(st.earn_code) == 6, r.status_code)
    client.post(f"/admin/loyalty/{other.id}", auth=A, data={"enabled": "on", "mode": "with_booking"})
    CODE = st.earn_code

    # ------------------------------------------------------------------ owner setup
    cafe_owner = login("o@cafe.test")
    r = cafe_owner.get("/owner/appointments")
    check("loyalty ON: owner menu shows Loyalty", "/owner/loyalty" in r.text)
    cafe_owner.post("/owner/loyalty/rewards", data={"name": "Free Brownie", "points": "20"})
    cafe_owner.post("/owner/loyalty/rewards", data={"name": "Free Cappuccino", "points": "40"})
    r = cafe_owner.post("/owner/loyalty/rewards", data={"name": "Bad", "points": "-5"}, follow_redirects=False)
    check("negative reward points refused", "err=" in r.headers["location"] and db.query(LoyaltyReward).count() == 2,
          r.headers["location"])
    r = cafe_owner.post("/owner/loyalty/settings", data={"points_per_visit": "10", "welcome_bonus": "10",
                                                         "whatsapp_number": "98765 43210"}, follow_redirects=False)
    db.expire_all(); st = db.get(LoyaltySettings, cafe.id)
    check("owner saves settings; 10-digit number gets +91", st.points_per_visit == 10 and st.welcome_bonus == 10
          and st.whatsapp_number == "919876543210", (st.points_per_visit, st.welcome_bonus, st.whatsapp_number))
    r = cafe_owner.get("/owner/loyalty")
    check("Loyalty dashboard (desktop): own layout, Give a reward, no Book menu items",
          r.status_code == 200 and "Give a reward" in r.text and "Verzio Loyalty" in r.text
          and "/owner/appointments" not in r.text and "/owner/services" not in r.text and "OWNER PORTAL" not in r.text, r.status_code)
    check("Loyalty dashboard (with booking): link back to Verzio Book", 'href="/owner/dashboard"' in r.text)
    r = cafe_owner.get("/owner/loyalty", headers=IPHONE)
    check("Loyalty dashboard (phone): 4 loyalty tabs + Book", r.status_code == 200 and "m-tabbar" in r.text
          and "repeat(5, 1fr)" in r.text and "Give a reward" in r.text, r.status_code)
    r = cafe_owner.get("/owner/loyalty/rewards")
    check("Rewards page lists the rewards", r.status_code == 200 and "Free Brownie" in r.text and "Free Cappuccino" in r.text
          and "Add a reward" in r.text, r.status_code)
    r = cafe_owner.get("/owner/loyalty/settings")
    check("Loyalty settings page: points, streaks, QR code, no booking settings", r.status_code == 200 and CODE in r.text
          and 'name="daily_streak_on"' in r.text and 'name="weekly_streak_on"' in r.text and 'name="business_name"' in r.text
          and "opening hours" not in r.text.lower() and "Bookings this month" not in r.text, r.status_code)
    r = cafe_owner.get("/owner/loyalty/settings", headers=IPHONE)
    check("Loyalty settings page works on a phone", r.status_code == 200 and 'name="daily_streak_on"' in r.text)
    r = cafe_owner.get("/owner/appointments")
    check("Book portal (both products): menu links to Verzio Loyalty", r.status_code == 200 and "Verzio Loyalty" in r.text)
    r = cafe_owner.get("/owner/loyalty/qr")
    check("QR poster encodes wa.me/<number>?text=STAMP <code>", r.status_code == 200
          and "<svg" in r.text and "QR library missing" not in r.text, r.status_code)

    # ------------------------------------------------------------------ earning
    r, out = webhook(client, "PN_CAFE", C, text("STAMP WRONG1"))
    check("wrong code -> 'didn't work', no points", "didn't work" in body_of(out) and db.query(LoyaltyMember).count() == 0,
          body_of(out))
    r, out = webhook(client, "PN_CAFE", C, text(f"stamp {CODE.lower()}"))
    b = body_of(out)
    db.expire_all(); m = L.find_member(db, cafe.id, C)
    check("first scan: +20 (10 visit + 10 welcome), name saved", m and m.points == 20 and m.visits == 1
          and m.name == "Asha Rao" and "+20 points" in b and "welcome bonus" in b, b)
    check("reply shows emoji bars: Brownie full ✅, Cappuccino half",
          "🟩" * 10 + " ✅ Ready to claim!" in b and "🟩" * 5 + "⬜" * 5 + " 20/40" in b, b)
    check("unlock announced + buttons Claim reward / My wallet", "You unlocked *Free Brownie*" in b
          and [x["id"] for x in out[0]["payload"]["buttons"]] == ["loy_claim", "loy_wallet"], out and out[0]["payload"])
    r, out = webhook(client, "PN_CAFE", C, text(f"STAMP {CODE}"))
    db.expire_all(); m = L.find_member(db, cafe.id, C)
    check("second scan same day -> 'already collected', no points", m.points == 20 and "already collected" in body_of(out),
          body_of(out))
    m.last_visit_date = "2000-01-01"; db.commit()
    r, out = webhook(client, "PN_CAFE", C, text(f"Add my points: STAMP {CODE}"))
    db.expire_all(); m = L.find_member(db, cafe.id, C)
    check("next day: +10 (no welcome bonus again), code found inside a longer message", m.points == 30 and m.visits == 2
          and "+10 points" in body_of(out), (m.points, body_of(out)))
    entries = db.query(LoyaltyEntry).filter_by(member_id=m.id).order_by(LoyaltyEntry.id).all()
    check("history rows: welcome, visit, visit with running balance", [(e.kind, e.delta, e.balance_after) for e in entries]
          == [("welcome", 10, 10), ("visit", 10, 20), ("visit", 10, 30)], [(e.kind, e.delta, e.balance_after) for e in entries])

    r, out = webhook(client, "PN_CAFE", C, text("hi"))
    check("with booking: 'hi' still gets the booking list", out and out[0]["payload"]["type"] == "list", out)
    r, out = webhook(client, "PN_CAFE", C, text("My points!"))
    p = out[0]["payload"] if out else {}
    check("'my points' -> wallet summary with an 'Open my wallet' link button",
          p.get("type") == "cta_url" and p.get("url") == f"https://verzio.codes/w/{m.wallet_token}"
          and "Wallet: *30 points*" in p.get("body", ""), p)
    wire = whatsapp_client.build_message_body(C, p)
    check("link button is sent to Meta as interactive cta_url",
          wire["interactive"]["type"] == "cta_url" and wire["interactive"]["action"]["parameters"]["url"] == p.get("url"), wire)
    r, out = webhook(client, "PN_CAFE", D, btn("loy_wallet"))
    check("wallet for someone with no points -> how to start", "don't have any points" in body_of(out), body_of(out))

    # ------------------------------------------------------------------ claiming (WhatsApp)
    r, out = webhook(client, "PN_CAFE", C, btn("loy_claim"))
    claim = db.query(LoyaltyClaim).filter_by(member_id=m.id, status="pending").first()
    check("one reward unlocked: Claim -> 4-digit code straight away", claim and re.search(rf"\*{claim.code}\*", body_of(out))
          and claim.reward_name == "Free Brownie" and "Valid until" in body_of(out), body_of(out))
    db.expire_all(); m = L.find_member(db, cafe.id, C)
    check("claiming takes no points yet", m.points == 30)
    first_code = claim.code

    # ------------------------------------------------------------------ giving (owner portal)
    r = cafe_owner.post("/owner/loyalty/lookup", data={"code": "12"}, follow_redirects=False)
    check("lookup needs 4 digits", "err=" in r.headers["location"])
    r = cafe_owner.get(f"/owner/loyalty?code={first_code}")
    check("owner checks the code: reward + Give reward button", "Free Brownie" in r.text and "Give reward" in r.text
          and f"/owner/loyalty/claims/{claim.id}/give" in r.text)
    r = cafe_owner.get("/owner/loyalty")
    check("live code listed under 'Waiting at the counter'", first_code in r.text and "Waiting at the counter" in r.text)
    other_owner = login("o@other.test")
    r = other_owner.get(f"/owner/loyalty?code={first_code}")
    check("another business can't see this code", "No claim with that code" in r.text)
    r = other_owner.post(f"/owner/loyalty/claims/{claim.id}/give", follow_redirects=False)
    db.expire_all()
    check("another business can't give this claim", "err=" in r.headers["location"]
          and db.get(LoyaltyClaim, claim.id).status == "pending", r.headers["location"])
    outbox.clear()
    r = cafe_owner.post(f"/owner/loyalty/claims/{claim.id}/give", follow_redirects=False)
    db.expire_all(); m = L.find_member(db, cafe.id, C)
    check("Give reward: points taken (30 -> 10), claim closed", m.points == 10 and db.get(LoyaltyClaim, claim.id).status == "given"
          and "ok=" in r.headers["location"], (m.points, r.headers["location"]))
    told = [x for x in outbox if x["to"] == C]
    check("customer told on WhatsApp: 'Enjoy your Free Brownie', balance and wallet link",
          told and "Enjoy your Free Brownie" in told[0]["payload"]["body"] and "10 points" in told[0]["payload"]["body"], told)
    r = cafe_owner.post(f"/owner/loyalty/claims/{claim.id}/give", follow_redirects=False)
    db.expire_all()
    check("Give twice -> 'already given', points unchanged", "already+given" in r.headers["location"]
          and L.find_member(db, cafe.id, C).points == 10, r.headers["location"])

    r, out = webhook(client, "PN_CAFE", C, text("claim"))
    check("nothing unlocked -> 'Nothing to claim yet' with points to go", "Nothing to claim yet" in body_of(out)
          and "10 more points to Free Brownie" in body_of(out), body_of(out))

    # expired / not enough points / replaced
    m.points = 100; db.commit()
    c1 = L.start_claim(db, cafe, m, db.query(LoyaltyReward).filter_by(name="Free Brownie").first().id)
    c2 = L.start_claim(db, cafe, m, db.query(LoyaltyReward).filter_by(name="Free Cappuccino").first().id)
    db.expire_all()
    check("a new claim replaces the old code", db.get(LoyaltyClaim, c1.id).status == "replaced"
          and db.get(LoyaltyClaim, c2.id).status == "pending")
    r = cafe_owner.post(f"/owner/loyalty/claims/{c1.id}/give", follow_redirects=False)
    check("replaced code can't be given", "claimed+something+else" in r.headers["location"], r.headers["location"])
    c2 = db.get(LoyaltyClaim, c2.id); c2.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1); db.commit()
    r = cafe_owner.post(f"/owner/loyalty/claims/{c2.id}/give", follow_redirects=False)
    check("expired code can't be given", "expired" in r.headers["location"], r.headers["location"])
    c3 = L.start_claim(db, cafe, m, db.query(LoyaltyReward).filter_by(name="Free Cappuccino").first().id)
    m = L.find_member(db, cafe.id, C); m.points = 5; db.commit()
    r = cafe_owner.post(f"/owner/loyalty/claims/{c3.id}/give", follow_redirects=False)
    db.expire_all()
    check("not enough points at the counter -> refused", "only+has+5+points" in r.headers["location"]
          and L.find_member(db, cafe.id, C).points == 5, r.headers["location"])

    m = L.find_member(db, cafe.id, C); m.points = 45; db.commit()
    r, out = webhook(client, "PN_CAFE", C, btn("loy_claim"))
    p = out[0]["payload"] if out else {}
    rows = [x["id"] for x in p.get("sections", [{}])[0].get("rows", [])] if p.get("type") == "list" else []
    check("two rewards unlocked: Claim -> pick from a list", len(rows) == 2 and all(x.startswith("loy_claim_") for x in rows), p)
    r, out = webhook(client, "PN_CAFE", C, tap(rows[-1] if rows else "loy_claim_0"))
    check("picking from the list -> code for that reward", "Free Cappuccino" in body_of(out) and "Show this code" in body_of(out),
          body_of(out))
    r, out = webhook(client, "PN_CAFE", C, tap("loy_claim_999999"))
    check("unknown reward id -> 'isn't available'", "isn't available" in body_of(out), body_of(out))

    # ------------------------------------------------------------------ wallet page
    m = L.find_member(db, cafe.id, C)
    w = TestClient(server.app)
    r = w.get(f"/w/{m.wallet_token}")
    check("wallet page: name, balance, rewards, live code, no full phone", r.status_code == 200 and "Bean There Cafe" in r.text
          and 'data-to="45"' in r.text and "Free Cappuccino" in r.text and "Show this code at the counter" in r.text
          and C not in r.text and r.headers.get("cache-control") == "no-store", r.status_code)
    check("wallet page: unlocked rewards are marked for the green animation",
          r.text.count('data-unlocked="1">') == 2 and 'data-pct="100"' in r.text)
    brownie = db.query(LoyaltyReward).filter_by(business_id=cafe.id, name="Free Brownie").first()
    r = w.post(f"/w/{m.wallet_token}/claim/{brownie.id}", follow_redirects=False)
    db.expire_all(); live = L.active_claim(db, L.find_member(db, cafe.id, C))
    check("claim from the wallet page -> new code shown", r.status_code == 303 and live and live.reward_name == "Free Brownie"
          and live.code in w.get(f"/w/{m.wallet_token}").text)
    expensive = LoyaltyReward(business_id=cafe.id, name="Big Lunch", points=500); db.add(expensive); db.commit()
    r = w.post(f"/w/{m.wallet_token}/claim/{expensive.id}", follow_redirects=False)
    check("claiming a locked reward from the page -> friendly error", "error=" in r.headers["location"], r.headers["location"])
    other_reward = LoyaltyReward(business_id=other.id, name="Other's Pie", points=1); db.add(other_reward); db.commit()
    r = w.post(f"/w/{m.wallet_token}/claim/{other_reward.id}", follow_redirects=False)
    check("can't claim another business's reward from the wallet", "error=" in r.headers["location"])
    check("unknown wallet link -> 404 page", w.get("/w/not-a-real-token").status_code == 404)

    # ------------------------------------------------------------------ owner points + rewards
    r = cafe_owner.post("/owner/loyalty/points", data={"phone": "98333 34444", "name": "Dev", "points": "", "kind": "visit"},
                        follow_redirects=False)
    db.expire_all(); dm = L.find_member(db, cafe.id, D)
    check("owner adds a visit by phone (uses points per visit)", dm and dm.points == 10 and dm.name == "Dev" and dm.visits == 1,
          r.headers["location"])
    r = cafe_owner.post("/owner/loyalty/points", data={"phone": "919833334444", "points": "-25", "kind": "adjust"},
                        follow_redirects=False)
    db.expire_all()
    check("owner removes points: never below 0", L.find_member(db, cafe.id, D).points == 0, r.headers["location"])
    r = cafe_owner.post("/owner/loyalty/points", data={"phone": "919000000000", "points": "-5", "kind": "adjust"},
                        follow_redirects=False)
    check("removing points from someone new -> refused, nobody created", "no+points+to+remove" in r.headers["location"]
          and L.find_member(db, cafe.id, "919000000000") is None, r.headers["location"])
    r = cafe_owner.post("/owner/loyalty/points", data={"phone": "123", "points": "10", "kind": "visit"}, follow_redirects=False)
    check("bad phone number refused", "valid+mobile" in r.headers["location"], r.headers["location"])
    r = other_owner.post(f"/owner/loyalty/rewards/{brownie.id}", data={"name": "Hacked", "points": "1"}, follow_redirects=False)
    r2 = other_owner.post(f"/owner/loyalty/rewards/{brownie.id}/delete", follow_redirects=False)
    db.expire_all()
    check("another business can't edit or delete this reward", db.get(LoyaltyReward, brownie.id).name == "Free Brownie"
          and not db.get(LoyaltyReward, brownie.id).is_deleted)
    cafe_owner.post(f"/owner/loyalty/rewards/{expensive.id}", data={"name": "Big Lunch Combo", "points": "450"})
    cafe_owner.post(f"/owner/loyalty/rewards/{expensive.id}/toggle")
    db.expire_all(); e = db.get(LoyaltyReward, expensive.id)
    check("owner edits and hides a reward", e.name == "Big Lunch Combo" and e.points == 450 and not e.is_active)
    cafe_owner.post(f"/owner/loyalty/rewards/{expensive.id}/delete")
    db.expire_all()
    check("deleted reward disappears for customers", "Big Lunch" not in w.get(f"/w/{m.wallet_token}").text
          and db.get(LoyaltyReward, expensive.id).is_deleted)

    # ------------------------------------------------------------------ new QR code
    r = cafe_owner.post("/owner/loyalty/rotate-code", follow_redirects=False)
    db.expire_all(); NEW = db.get(LoyaltySettings, cafe.id).earn_code
    m = L.find_member(db, cafe.id, C); m.last_visit_date = "2000-01-01"; db.commit()
    r, out = webhook(client, "PN_CAFE", C, text(f"STAMP {CODE}"))
    check("after 'Make a new QR code' the old code stops working", NEW != CODE and "didn't work" in body_of(out), body_of(out))
    r, out = webhook(client, "PN_CAFE", C, text(f"STAMP {NEW}"))
    check("...and the new one works", "+10 points" in body_of(out), body_of(out))

    # ------------------------------------------------------------------ streaks
    from datetime import date as D
    r = cafe_owner.post("/owner/loyalty/settings", data={"business_name": "Bean There Cafe", "whatsapp_number": "9876543210",
        "points_per_visit": "10", "welcome_bonus": "0", "daily_streak_on": "on", "daily_streak_step": "5",
        "daily_streak_max": "15", "weekly_streak_on": "on", "weekly_streak_step": "10", "weekly_streak_max": "30"},
        follow_redirects=False)
    db.expire_all(); st = db.get(LoyaltySettings, cafe.id)
    check("owner turns on daily + weekly streaks in Loyalty settings", st.daily_streak_on and st.weekly_streak_on
          and (st.daily_streak_step, st.daily_streak_max, st.weekly_streak_step, st.weekly_streak_max) == (5, 15, 10, 30),
          r.headers["location"])
    r = cafe_owner.post("/owner/loyalty/settings", data={"points_per_visit": "10", "daily_streak_step": "20", "daily_streak_max": "5"},
                        follow_redirects=False)
    check("streak cap smaller than the step is refused", "err=" in r.headers["location"], r.headers["location"])
    db.expire_all()
    E = "919877700001"
    got = []
    for day in (D(2026, 10, 14), D(2026, 10, 15), D(2026, 10, 16), D(2026, 10, 17), D(2026, 10, 18)):
        res = L.earn_visit(db, cafe, E, NEW, today=day)
        got.append((res.earned, res.daily_bonus, res.weekly_bonus, res.member.daily_streak))
    check("daily streak: +0, +5, +10, +15, then capped at +15 (Wed to Sun)",
          got == [(10, 0, 0, 1), (15, 5, 0, 2), (20, 10, 0, 3), (25, 15, 0, 4), (25, 15, 0, 5)], got)
    res = L.earn_visit(db, cafe, E, NEW, today=D(2026, 10, 21))
    check("2 days missed: daily streak back to 1; new week -> 2-week streak, +10", (res.member.daily_streak, res.daily_bonus,
          res.member.weekly_streak, res.weekly_bonus) == (1, 0, 2, 10), (res.member.daily_streak, res.daily_bonus,
          res.member.weekly_streak, res.weekly_bonus))
    res = L.earn_visit(db, cafe, E, NEW, today=D(2026, 10, 22))
    check("second visit in the same week: daily bonus yes, weekly bonus not again", (res.daily_bonus, res.weekly_bonus) == (5, 0),
          (res.daily_bonus, res.weekly_bonus))
    res = L.earn_visit(db, cafe, E, NEW, today=D(2026, 11, 5))
    check("a whole week skipped: weekly streak back to 1, no bonus", (res.member.weekly_streak, res.weekly_bonus) == (1, 0),
          (res.member.weekly_streak, res.weekly_bonus))
    kinds = [e.kind for e in db.query(LoyaltyEntry).filter_by(member_id=res.member.id)]
    check("streak bonuses are their own history rows", kinds.count("streak_daily") == 5 and kinds.count("streak_weekly") == 1, kinds)

    monday_off = Business(name="Closed Mondays", whatsapp_business_phone_number_id="PN_MON", manager_phone_number="919800000040",
                          timezone="Asia/Kolkata", operational_hours={k: v for k, v in ALL.items() if k != "mon"}, holidays=[],
                          is_active=True, accepting_bookings=True)
    db.add(monday_off); db.commit()
    client.post(f"/admin/loyalty/{monday_off.id}", auth=A, data={"enabled": "on", "mode": "only"})
    db.expire_all(); ms = db.get(LoyaltySettings, monday_off.id); ms.daily_streak_on = True; db.commit()
    L.earn_visit(db, monday_off, E, ms.earn_code, today=D(2026, 10, 18))              # Sunday
    res = L.earn_visit(db, monday_off, E, ms.earn_code, today=D(2026, 10, 20))        # Tuesday
    check("closed on Monday: Sunday -> Tuesday still counts as a streak (+5)", (res.member.daily_streak, res.daily_bonus) == (2, 5),
          (res.member.daily_streak, res.daily_bonus))
    info = L.streak_info(monday_off, ms, res.member, today=D(2026, 10, 20))
    check("next visit hint skips nothing when tomorrow is open", info.next_visit_day == "tomorrow" and info.next_daily_bonus == 10,
          (info.next_visit_day, info.next_daily_bonus))
    info = L.streak_info(monday_off, ms, res.member, today=D(2026, 10, 25))           # Sunday, after a gap
    check("broken streak shows as 0 on the wallet", info.daily == 0, info.daily)
    L.earn_visit(db, monday_off, "919877700002", ms.earn_code, today=D(2026, 10, 25))  # Sunday
    info = L.streak_info(monday_off, ms, L.find_member(db, monday_off.id, "919877700002"), today=D(2026, 10, 25))
    check("visited Sunday, closed Monday: hint says 'on Tuesday'", info.next_visit_day == "on Tuesday", info.next_visit_day)

    from booking_engine import business_today
    m = L.find_member(db, cafe.id, C)
    m.last_visit_date = L.previous_open_day(cafe, business_today(cafe)).isoformat(); m.daily_streak = 2; db.commit()
    r, out = webhook(client, "PN_CAFE", C, text(f"STAMP {NEW}"))
    check("WhatsApp reply shows the streak: '3-day streak! +10 bonus' and what tomorrow brings",
          "3-day streak! +10 bonus" in body_of(out) and "Come back tomorrow for a +15 streak bonus" in body_of(out), body_of(out))
    db.expire_all(); m = L.find_member(db, cafe.id, C)
    r = TestClient(server.app).get(f"/w/{m.wallet_token}")
    check("wallet page shows the daily and weekly streak cards", "days in a row" in r.text and r.text.count(" in a row</small>") == 2, r.status_code)
    r = cafe_owner.get("/owner/loyalty/customers")
    check("Customers page shows streaks", r.status_code == 200 and "3 days" in r.text, r.status_code)
    r = cafe_owner.get("/owner/loyalty/customers?q=asha")
    check("Customers page search by name", "Asha Rao" in r.text and "Dev" not in r.text)
    r = cafe_owner.get("/owner/loyalty/qr")
    check("QR poster mentions streak bonuses when they're on", "streak bonus points" in r.text)

    o = "919877700003"
    L.earn_visit(db, cafe, o, NEW, today=D(2026, 10, 14))
    mem, delta, _ = L.owner_adjust(db, cafe, o, "10", None, as_visit=True, today=D(2026, 10, 15))
    check("a visit added by staff counts towards the streak", mem.daily_streak == 2 and delta == 15, (mem.daily_streak, delta))

    from sqlalchemy import inspect as _insp
    from database import engine as _engine
    from loyalty.models import ensure_tables as _ensure
    with _engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE loyalty_members DROP COLUMN weekly_streak")
    _ensure()
    check("an older loyalty database gets the new streak columns added", "weekly_streak" in
          {c["name"] for c in _insp(_engine).get_columns("loyalty_members")})
    db.expire_all()

    # ------------------------------------------------------------------ plan / safety
    db.add(Subscription(business_id=cafe.id, plan="suspended")); db.commit()
    r, out = webhook(client, "PN_CAFE", C, text("wallet"))
    check("plan suspended -> 'rewards aren't available right now'", "aren't available right now" in body_of(out), body_of(out))
    db.query(Subscription).filter_by(business_id=cafe.id).delete(); db.commit()

    real = L.earn_visit
    def boom(*a, **k): raise RuntimeError("test crash")
    L.earn_visit = boom
    import logging; quiet = logging.getLogger("VERZIO_LOYALTY_BOT"); quiet.disabled = True   # expected error, keep output clean
    r, out = webhook(client, "PN_CAFE", C, text(f"STAMP {NEW}"))
    L.earn_visit = real; quiet.disabled = False
    check("a loyalty crash -> polite 'try again', webhook still 200", r.status_code == 200 and "try again" in body_of(out), body_of(out))
    r, out = webhook(client, "PN_CAFE", C, text("hi"))
    check("...and booking still works right after", out and out[0]["payload"]["type"] == "list", out)

    # ------------------------------------------------------------------ loyalty only
    client.post(f"/admin/loyalty/{cafe.id}", auth=A, data={"enabled": "on", "mode": "only"})
    loyalty_bot._last_help.clear()
    r, out = webhook(client, "PN_CAFE", C, text("hi"))
    check("loyalty only: 'hi' -> rewards summary, not the booking list",
          out and out[0]["payload"]["type"] == "buttons" and "Your rewards at Bean There Cafe" in body_of(out), out)
    r, out = webhook(client, "PN_CAFE", "919855556666", text("Hello"))
    check("loyalty only: 'hello' from someone new -> welcome + how to collect", "scan the QR code" in body_of(out), body_of(out))
    r, out = webhook(client, "PN_CAFE", C, text("Are you open on Sunday?"))
    check("loyalty only: a question -> 'someone will reply', once", "will reply here soon" in body_of(out), body_of(out))
    r, out = webhook(client, "PN_CAFE", C, text("And do you have oat milk?"))
    check("loyalty only: second question within 12 h -> no repeat note", out == [], out)
    r, out = webhook(client, "PN_CAFE", C, btn("action_start"))
    check("loyalty only: an old booking button does nothing", out == [], out)
    r = cafe_owner.get("/owner/dashboard", follow_redirects=False)
    check("loyalty only: owner dashboard opens Loyalty", r.status_code == 303 and r.headers["location"] == "/owner/loyalty")
    for path in ("/owner", "/owner/appointments", "/owner/services", "/owner/reports", "/owner/settings"):
        r = cafe_owner.get(path, follow_redirects=False)
        check(f"loyalty only: {path} (a Book page) -> Loyalty dashboard", r.status_code == 303 and r.headers["location"] == "/owner/loyalty",
              (r.status_code, r.headers.get("location")))
    r = cafe_owner.post("/owner/settings", data={"business_name": "Hacked"}, follow_redirects=False)
    db.expire_all()
    check("loyalty only: Book settings can't be saved either", r.status_code == 303 and db.get(Business, cafe.id).name == "Bean There Cafe")
    r = cafe_owner.get("/owner/loyalty")
    check("loyalty only: dashboard has no Verzio Book link", 'href="/owner/dashboard"' not in r.text and "Verzio Book" not in r.text
          and "Rewards on WhatsApp" in r.text)
    r = cafe_owner.get("/owner/loyalty", headers=IPHONE)
    check("loyalty only: phone tab bar has the 4 loyalty tabs", "repeat(4, 1fr)" in r.text and "/owner/appointments" not in r.text)
    r = salon_owner.get("/owner/appointments")
    check("loyalty only elsewhere doesn't affect other businesses' Book portal", r.status_code == 200)

    client.post(f"/admin/loyalty/{cafe.id}", auth=A, data={"mode": "with_booking"})        # unticked = off
    db.expire_all()
    r, out = webhook(client, "PN_CAFE", C, text(f"STAMP {NEW}"))
    check("switched OFF again: loyalty stops, booking bot answers", not db.get(LoyaltySettings, cafe.id).enabled
          and "Would you like to book" in body_of(out), body_of(out))
    check("switched OFF: wallet links stop working", w.get(f"/w/{m.wallet_token}").status_code == 404)
    r = cafe_owner.get("/owner/dashboard")
    check("switched OFF: owner dashboard is back to normal", r.status_code == 200 and "/owner/loyalty" not in r.text)

    db.close()

print("\nALL PASSED" if fails == 0 else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
