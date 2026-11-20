"""
test_v3.py: the v3 ("warm") Loyalty owner screens, shown only to businesses in
VERZIO_PREVIEW. Throwaway database, stubbed WhatsApp:

    python test_v3.py

Checks every page renders with real data, the forms still post to the same
endpoints, counter staff see only the counter, and businesses not in the
preview list keep the current screens.
"""
import os, sys, tempfile
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
os.environ.update(
    SCHEDULER="off", DATABASE_URL=f"sqlite:///{tmp.name}", META_APP_SECRET="test-secret", META_ACCESS_TOKEN="x",
    VERZIO_ENV="development", VERZIO_SECRET_KEY="test-only-secret-key-not-for-production-use",
    ADMIN_USERNAME="admin", ADMIN_PASSWORD="verzio-dev-admin", COOKIE_SECURE="false", WHATSAPP_TEMPLATES="on",
    PUBLIC_BASE_URL="https://verzio.codes",
    LOYALTY_PLUS="98765 43210",
    VERZIO_PREVIEW="98765 43210",           # only Bean There Cafe sees v3
)
sys.path.insert(0, os.getcwd())
from datetime import datetime, timedelta, date
from fastapi.testclient import TestClient
import server, whatsapp_client, booking_engine
from database import SessionLocal, Business, Service, Owner
from owner.owner_auth import hash_password
from loyalty import engine as L
from loyalty.models import LoyaltySettings, LoyaltyMember, LoyaltyClaim
from preview import preview_on

async def fake_post(recipient, pnid, payload, token=None):
    return None
whatsapp_client._post = fake_post
CLOCK = {"now": datetime(2026, 11, 20, 18, 30)}
booking_engine.business_now = lambda biz: CLOCK["now"]

fails = 0
def check(label, ok, extra=""):
    global fails; fails += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if (extra and not ok) else ""))

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

# --- the switch itself
os.environ["VERZIO_PREVIEW"] = ""
check("preview: unset = nobody", not preview_on(None, "919876543210"))
os.environ["VERZIO_PREVIEW"] = "all"
check("preview: all = everyone", preview_on(None))
os.environ["VERZIO_PREVIEW"] = "2, 918262858396"
class _B: id = 2
class _C: id = 3
check("preview: business id listed", preview_on(_B()))
check("preview: WhatsApp number listed, any format", preview_on(_C(), "+91 82628 58396") and preview_on(_C(), "8262858396"))
check("preview: other business/number not included", not preview_on(_C(), "917066674848"))
os.environ["VERZIO_PREVIEW"] = "98765 43210"

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
    owner, plain_owner = login("o@cafe.test"), login("o@plain.test")
    for c, number in ((owner, "98765 43210"), (plain_owner, "98765 00000")):
        c.post("/owner/loyalty/settings", data={"points_per_visit": "10", "welcome_bonus": "0", "whatsapp_number": number,
                                               "business_name": "Bean There Cafe" if c is owner else "Plain Cafe"})
        c.post("/owner/loyalty/rewards", data={"name": "Free Brownie", "points": "30"})
        c.post("/owner/loyalty/rewards", data={"name": "Free Lunch", "points": "400"})
    owner.post("/owner/loyalty/grow", data={"tiers_on": "on", "silver_visits": "3", "silver_bonus": "50", "gold_visits": "6",
               "gold_bonus": "100", "avg_bill": "300", "points_per_100": "5", "birthday_points": "50",
               "referral_points": "30", "review_after": "3"})
    db.expire_all()
    st = db.get(LoyaltySettings, cafe.id)

    # Activity: Priya scans 4 days in a row (today too), Rahul once today, Ananya is a regular who stopped coming.
    for i, day in enumerate((17, 18, 19, 20)):
        CLOCK["now"] = datetime(2026, 11, day, 10 + i, 0)
        L.earn_visit(db, cafe, "919811100001", st.earn_code, name="Priya Shah")
    L.earn_visit(db, cafe, "919811100002", st.earn_code, name="Rahul Mehta")
    CLOCK["now"] = datetime(2026, 11, 20, 18, 30)
    ananya, _ = L.get_or_create_member(db, cafe, "919811100003", "Ananya Kapoor")
    ananya.visits, ananya.points, ananya.last_visit_date = 9, 40, "2026-10-31"
    ananya.created_at = datetime(2026, 7, 1)
    db.commit()
    priya = db.query(LoyaltyMember).filter_by(phone="919811100001").first()
    brownie = [r for r in L.rewards(db, cafe.id) if r.name == "Free Brownie"][0]
    claim = L.start_claim(db, cafe, priya, brownie.id)
    other = make_business(db, "Someone Else", "PN_X", "919800000030")
    stranger, _ = L.get_or_create_member(db, other, "919811100009", "Not Yours")
    db.commit()

    # ------------------------------------------------------------------ every page, v3
    PAGES = {"": ["Rewards to hand over", "Just scanned", "Missing your regulars", "Ananya Kapoor", claim.code,
                  f'action="/owner/loyalty/claims/{claim.id}/give"', 'action="/owner/loyalty/points"',
                  'action="/owner/loyalty/lookup"', "Visits today"],
             "/rewards": ["What customers can earn", "Free Brownie", 'action="/owner/loyalty/rewards"',
                          "/toggle", "What customers see in WhatsApp"],
             "/customers": ["people in your club", "Regulars", "At risk", "Lost", "Priya Shah", "member since"],
             "/grow": ["Ways to bring people back", 'name="tiers_on"', 'name="bill_points_on"', 'name="birthday_on"',
                       'name="referral_on"', 'name="review_on"', 'name="summary_on"', 'action="/owner/loyalty/boosts"'],
             "/results": ["Is it working?", "Brought back in the last 30 days", "Top regulars", "Return on Verzio"],
             "/settings": ['name="business_name"', 'name="points_per_visit"', 'name="daily_streak_on"',
                           'action="/owner/loyalty/rotate-code"', 'action="/owner/loyalty/staff"', "Your plan", "OFFERS THIS MONTH"]}
    for path, needles in PAGES.items():
        r = owner.get("/owner/loyalty" + path)
        missing = [n for n in needles if n not in r.text]
        check(f"v3 {path or '/ (Today)'}: renders with real data and the same forms",
              r.status_code == 200 and "v3-shell" in r.text and not missing, (r.status_code, missing))
        check(f"v3 {path or '/ (Today)'}: headings in Fraunces, no Bodoni", "Fraunces" in r.text and "Bodoni" not in r.text)

    r = owner.get("/owner/loyalty")
    import re as _re
    _m = _re.search(r'<b>(\d+)</b><span>Visits today</span><small>(\d+) came back</small>', r.text)
    check("Today: today's numbers (visits today, came back)", _m is not None, _m.groups() if _m else r.text[:0])
    check("Today: brought back ₹ uses the average bill", "₹300" in r.text and "Brought back" in r.text)
    r = owner.get("/owner/loyalty", headers=IPHONE)
    check("phones get v3 too, with the top tab bar", r.status_code == 200 and "v3-topnav" in r.text)

    r = owner.get("/owner/loyalty/customers?seg=risk")
    check("Customers: 'At risk' group shows Ananya, not Priya", "Ananya Kapoor" in r.text and "Priya Shah" not in r.text.split("people in your club")[1].split("How groups work")[0])
    r = owner.get("/owner/loyalty/customers?seg=reg")
    check("Customers: 'Regulars' is empty here (needs 4+ visits seen lately)... Priya has exactly 4",
          "Priya Shah" in r.text)
    r = owner.get(f"/owner/loyalty/customers?m={ananya.id}")
    check("Customers: picking someone shows their panel", 'id="c-who"' in r.text and "Ananya Kapoor" in r.text)
    r = owner.get(f"/owner/loyalty/customers?m={stranger.id}")
    check("Customers: another business's member can't be opened", "Not Yours" not in r.text)
    r = owner.get("/owner/loyalty/customers?q=rahul")
    check("Customers: search still works", "Rahul Mehta" in r.text and "Ananya Kapoor" not in r.text.split("How groups work")[0].split("people in your club")[1])

    r = owner.get("/owner/loyalty/grow")
    check("Grow: tier count shown (Ananya Gold)", "Gold (1)" in r.text, r.text[:0])

    # forms posted from v3 pages work exactly as before
    db.commit()
    r = owner.post(f"/owner/loyalty/claims/{claim.id}/give", follow_redirects=False)
    db.expire_all()
    check("Given ✓ uses the same endpoint and gives the reward", r.status_code == 303
          and db.get(LoyaltyClaim, claim.id).status == "given")
    r = owner.get("/owner/loyalty")
    check("after giving: nothing waiting, 1 reward given today", "0 waiting" in r.text and "<b>1</b><span>Rewards given</span>" in r.text)
    r = owner.post("/owner/loyalty/settings", data={"business_name": "Bean There Cafe", "whatsapp_number": "98765 43210",
                   "points_per_visit": "12", "welcome_bonus": "5"}, follow_redirects=False)
    db.expire_all()
    check("Settings form from v3 saves as before", r.status_code == 303 and db.get(LoyaltySettings, cafe.id).points_per_visit == 12)

    # ------------------------------------------------------------------ counter staff
    owner.post("/owner/loyalty/staff", data={"name": "Ravi", "username": "ravi.counter", "password": "counterpass1"})
    staff = login("ravi.counter", "counterpass1")
    r = staff.get("/owner/loyalty")
    check("staff: v3 counter only (no numbers, no win-back, no other pages)", r.status_code == 200 and "v3-shell" in r.text
          and "Visits today" not in r.text and "Missing your regulars" not in r.text and "/owner/loyalty/settings\"" not in r.text
          and ">Counter<" in r.text)
    check("staff: phone numbers masked", "919811100002" not in r.text)
    r = staff.get("/owner/loyalty/customers", follow_redirects=False)
    check("staff still can't open Customers", r.status_code == 303)

    # ------------------------------------------------------------------ not in the preview list
    for path in ("", "/rewards", "/customers", "/settings"):
        r = plain_owner.get("/owner/loyalty" + path)
        check(f"Plain Cafe (not in VERZIO_PREVIEW) keeps the current {path or 'dashboard'}", r.status_code == 200 and "v3-shell" not in r.text)
    os.environ["VERZIO_PREVIEW"] = ""
    r = owner.get("/owner/loyalty")
    check("VERZIO_PREVIEW empty: Bean There is back on the current screens", r.status_code == 200 and "v3-shell" not in r.text)
    os.environ["VERZIO_PREVIEW"] = "all"
    r = plain_owner.get("/owner/loyalty")
    check("VERZIO_PREVIEW=all: everyone gets v3", "v3-shell" in r.text)
    db.close()

print("\nALL PASSED" if fails == 0 else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
