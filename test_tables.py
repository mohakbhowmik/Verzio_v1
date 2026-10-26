"""
test_tables.py: Verzio Tables end to end, through the real /webhook route, the
guest booking page (/t/<token>), the owner portal, Admin -> Products and the
scheduler jobs. Throwaway database, stubbed outbound WhatsApp and a fixed
"now" (a Friday afternoon), so it's safe to run anytime:

    python test_tables.py

Also checks Tables stays out of the way: a Verzio Book business still gets the
normal booking bot, and Book + Tables can't both be on for one number.
"""
import os, sys, json, hmac, hashlib, tempfile, itertools
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
import asyncio, re
from datetime import date, datetime, timedelta
from fastapi.testclient import TestClient
import server, whatsapp_client, scheduler
from database import SessionLocal, Business, Service, Owner
from owner.owner_auth import hash_password
from tables import engine as T
from tables.models import TablesBooking, TablesLink, TablesOffer, TablesSettings, TablesWaitlist, TablesZone

outbox = []
async def fake_post(recipient, pnid, payload, token=None):
    outbox.append({"to": recipient, "from": pnid, "payload": payload}); return None
whatsapp_client._post = fake_post

# A fixed "now": next Friday at 3 pm, business time.
_today = date.today()
FRIDAY = _today + timedelta(days=(4 - _today.weekday()) % 7 or 7)
NOW = [datetime.combine(FRIDAY, datetime.min.time()).replace(hour=15)]
T.business_now = lambda biz: NOW[0]

fails = 0
def check(label, ok, extra=""):
    global fails; fails += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if (extra and not ok) else ""))

ids = itertools.count(1)
def webhook(client, pnid, sender, msg_body, name="Asha Rao"):
    msg = {"id": f"wamid.tbl{next(ids)}", "from": sender, **msg_body}
    body = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid},
            "contacts": [{"wa_id": sender, "profile": {"name": name}}], "messages": [msg]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    outbox.clear()
    client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    return [m for m in outbox if m["to"] == sender]
text = lambda t: {"type": "text", "text": {"body": t}}
btn = lambda i: {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": i, "title": "x"}}}
tpl_btn = lambda p: {"type": "button", "button": {"payload": p, "text": "x"}}
body_of = lambda out: (out[0]["payload"].get("body") or "") if out else ""
A = ("admin", "verzio-dev-admin")
NIGHT = {d: ["18:00", "01:00"] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
ALLDAY = {d: ["09:00", "21:00"] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
GUEST, GUEST2, MANAGER = "919811100001", "919811100002", "919800000099"

def make_business(db, name, pnid, manager, hours):
    b = Business(name=name, whatsapp_business_phone_number_id=pnid, manager_phone_number=manager,
                 timezone="Asia/Kolkata", operational_hours=hours, holidays=[], slot_interval=30,
                 max_parallel_bookings=1, approval_mode="automatic", is_active=True, accepting_bookings=True,
                 enable_service_selection=True, advance_booking_days=14, notification_preferences={})
    db.add(b); db.commit()
    db.add(Service(business_id=b.id, name="Haircut", duration=30, price=300, is_active=True)); db.commit()
    return b

def login(identifier, password="ownerpass123"):
    c = TestClient(server.app)
    r = c.post("/owner/login", data={"identifier": identifier, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    return c

def at(hh, mm=0, day=None):
    d = day or FRIDAY
    return datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm)

def run_jobs():
    outbox.clear()
    return asyncio.run(scheduler.tick())

def token_from(out):
    url = (out[0]["payload"].get("url") or "") if out else ""
    m = re.search(r"/t/([A-Za-z0-9_-]+)$", url)
    return m.group(1) if m else None

with TestClient(server.app) as client:
    db = SessionLocal()
    venue = make_business(db, "Skyline Social", "PN_SKY", MANAGER, NIGHT)
    salon = make_business(db, "Glow Salon", "PN_SALON", "919800000020", ALLDAY)
    for b, email in ((venue, "o@sky.test"), (salon, "o@salon.test")):
        db.add(Owner(business_id=b.id, full_name=f"{b.name} Owner", email=email, phone_number=None,
                     password_hash=hash_password("ownerpass123"), is_active=True))
    db.commit()
    VID = venue.id

    # ---------------------------------------------------------------- Admin -> Products
    r = client.get("/admin/products", auth=A)
    check("Admin Products shows the Verzio Tables switch", r.status_code == 200 and "Verzio Tables" in r.text)
    r = client.post(f"/admin/products/{VID}", auth=A, data={"book_on": "on", "tables_on": "on"}, follow_redirects=False)
    check("Book + Tables on one number is refused", "err=" in r.headers.get("location", ""))
    db.expire_all()
    check("...and nothing was switched on", T.get_settings(db, VID) is None)
    r = client.post(f"/admin/products/{VID}", auth=A, data={"tables_on": "on", "tables_plan": "trial",
                                                            "tables_amount": "4999"}, follow_redirects=False)
    db.expire_all()
    s = T.get_settings(db, VID)
    check("Tables switched on (trial, ₹4,999)", s is not None and s.enabled and s.plan == "trial" and s.monthly_amount == 4999,
          r.headers.get("location"))

    # ---------------------------------------------------------------- Owner portal + floor plan
    owner = login("o@sky.test")
    r = owner.get("/owner/dashboard", follow_redirects=False)
    check("Tables owner is sent from Book pages to /owner/tables", r.status_code == 303 and r.headers["location"] == "/owner/tables")
    r = owner.get("/owner/tables")
    check("Tonight page loads with setup steps", r.status_code == 200 and "Draw your floor plan" in r.text)
    owner.post("/owner/tables/zones/demo")
    db.expire_all()
    zones = {z.name: z for z in T.zones(db, VID)}
    check("Example rooftop plan added (4 areas)", set(zones) == {"Skyline edge", "Lounge", "Bar counter", "Private cabana"})
    SKY, BAR, CAB = zones["Skyline edge"].id, zones["Bar counter"].id, zones["Private cabana"].id
    r = owner.post("/owner/tables/zones/layout", content=json.dumps([{"id": SKY, "x": 0, "y": 0, "w": 12, "h": 3}]),
                   headers={"Content-Type": "application/json"})
    db.expire_all()
    check("Dragging a block saves its new size", r.json().get("ok") and db.get(TablesZone, SKY).h == 3)
    r = owner.post("/owner/tables/zones/layout", content=json.dumps([{"id": SKY, "x": 9, "y": 7, "w": 12, "h": 3}]),
                   headers={"Content-Type": "application/json"})
    db.expire_all()
    z = db.get(TablesZone, SKY)
    check("Blocks can't be dragged off the plan", z.x == 0 and z.y == 5 and z.w == 12)
    owner.post("/owner/tables/zones/layout", content=json.dumps([{"id": SKY, "x": 0, "y": 0, "w": 12, "h": 2}]),
               headers={"Content-Type": "application/json"})
    r = owner.get("/owner/tables/plan")
    check("Floor plan page lists the areas", r.status_code == 200 and "Private cabana" in r.text)
    r = owner.post("/owner/tables/zones", data={"name": "", "kind": "tables"}, follow_redirects=False)
    check("An area needs a name", "err=" in r.headers["location"])

    # ---------------------------------------------------------------- Opening hours after midnight
    times = [t.strftime("%H:%M") for t in T.arrival_times(venue, s, FRIDAY, NOW[0])]
    check("Arrival times run 18:00 to 00:00 (closes 1 am, last arrival 1 h before)",
          times[0] == "18:00" and times[-1] == "00:00" and "00:30" not in times, times[-3:])
    check("The midnight slot is Saturday's date", T.arrival_times(venue, s, FRIDAY, NOW[0])[-1].date() == FRIDAY + timedelta(days=1))

    # ---------------------------------------------------------------- WhatsApp: when the bot speaks
    out = webhook(client, "PN_SKY", GUEST, text("Hi"))
    tok = token_from(out)
    check("'Hi' gets one Book a table link", len(out) == 1 and out[0]["payload"]["type"] == "cta_url" and tok, out)
    out = webhook(client, "PN_SKY", GUEST, text("do you have live music on saturday?"))
    check("Other questions get no bot reply (the owner answers in the app)", out == [], out)
    out = webhook(client, "PN_SKY", GUEST2, text("table for 4 tonight?"), name="Rohan Mehta")
    tok2 = token_from(out)
    check("'table for 4 tonight?' gets a link", tok2 is not None)
    out = webhook(client, "PN_SALON", GUEST, text("Hi"))
    check("A Verzio Book business still gets the booking bot", out and out[0]["payload"]["type"] != "cta_url", out)

    # ---------------------------------------------------------------- The booking page
    r = client.get(f"/t/{tok}")
    check("Booking page shows the venue, its areas and the guest's name",
          r.status_code == 200 and "Skyline Social" in r.text and "Bar counter" in r.text and 'value="Asha Rao"' in r.text)
    check("Booking page never shows the full phone number", GUEST not in r.text and "***0001" in r.text)
    check("Booking page is private (no-store, noindex)", "no-store" in r.headers.get("cache-control", "")
          and "noindex" in r.headers.get("x-robots-tag", ""))
    r = client.get(f"/t/{tok}/zones", params={"at": at(20).strftime("%Y-%m-%dT%H:%M"), "party": 4})
    states = {d["id"]: d["state"] for d in r.json()["zones"]}
    check("At 8 pm for 4: the edge is free, the cabana is too big for them",
          states[SKY] == "free" and states[CAB] == "small", states)
    r = client.get(f"/t/{tok}/zones", params={"at": at(15).strftime("%Y-%m-%dT%H:%M"), "party": 4})
    check("A time outside opening hours is refused", r.status_code == 400)

    outbox.clear()
    r = client.post(f"/t/{tok}/book", data={"at": at(20).strftime("%Y-%m-%dT%H:%M"), "party": "4",
                                            "zone_id": str(SKY), "name": "Asha"}, follow_redirects=False)
    db.expire_all()
    b1 = db.query(TablesBooking).filter_by(phone=GUEST).first()
    check("Booking made from the page", r.status_code == 303 and r.headers["location"].endswith("/done") and b1
          and b1.status == "confirmed" and b1.tables_used == 1 and b1.end_at == at(22, 30), r.headers.get("location"))
    conf = [m for m in outbox if m["to"] == GUEST]
    check("Guest gets the confirmation on WhatsApp with a Cancel button",
          conf and "is booked" in body_of(conf) and conf[0]["payload"]["buttons"][0]["id"] == f"tbl_cancel_{b1.id}")
    r = client.get(f"/t/{tok}/done")
    check("Done page shows the booking", "Your table is booked" in r.text and "Skyline edge" in r.text)
    r = client.get(f"/t/{tok}")
    check("The link books only once", "booked with this link" in r.text)
    r = client.post(f"/t/{tok}/book", data={"at": at(21).strftime("%Y-%m-%dT%H:%M"), "party": "2", "zone_id": str(SKY)})
    db.expire_all()
    check("...a second POST doesn't create another booking", db.query(TablesBooking).filter_by(phone=GUEST).count() == 1)
    check("A made-up link shows a friendly page", client.get("/t/not-a-real-token").status_code == 404)

    # ---------------------------------------------------------------- Capacity
    for i in range(5):
        T.create_booking(db, venue, s, SKY, at(20), 4, f"91982220000{i}", f"Group {i}")
    st = T.zone_state(db, s, db.get(TablesZone, SKY), at(20), 2, NOW[0])
    check("Edge (6 tables) is full after 6 bookings at 8 pm", st.state == "full")
    check("...and still full at 9:30 (tables held 2.5 h)", T.zone_state(db, s, db.get(TablesZone, SKY), at(21, 30), 2, NOW[0]).state == "full")
    check("...and free again at 10:30", T.zone_state(db, s, db.get(TablesZone, SKY), at(22, 30), 2, NOW[0]).state in ("free", "few"))
    try:
        T.create_booking(db, venue, s, SKY, at(20), 2, "919822200099", "Late")
        check("A 7th booking at 8 pm is refused", False)
    except T.TablesError:
        db.rollback(); check("A 7th booking at 8 pm is refused", True)
    lounge = zones["Lounge"]
    big = T.create_booking(db, venue, s, lounge.id, at(19), 7, "919822200050", "Big group")
    check("A group of 7 at tables of 6 takes 2 tables, joined", big.tables_used == 2)
    T.create_booking(db, venue, s, BAR, at(21), 10, "919822200060", "Bar ten")
    check("Bar counter counts guests: 10 of 14 used, 5 more don't fit",
          T.zone_state(db, s, db.get(TablesZone, BAR), at(21), 5, NOW[0]).state == "full"
          and T.zone_state(db, s, db.get(TablesZone, BAR), at(21), 4, NOW[0]).state in ("free", "few"))
    try:
        T.create_booking(db, venue, s, BAR, at(21), 2, GUEST, "Asha again")
        check("Same guest can't hold two overlapping tables", False)
    except T.TablesError as e:
        db.rollback(); check("Same guest can't hold two overlapping tables", "already have a table" in e.message)

    # ---------------------------------------------------------------- Waitlist
    r = client.get(f"/t/{tok2}/zones", params={"at": at(20).strftime("%Y-%m-%dT%H:%M"), "party": 2})
    check("Guest 2 sees the edge as full at 8 pm", {d["id"]: d["state"] for d in r.json()["zones"]}[SKY] == "full")
    r = client.post(f"/t/{tok2}/waitlist", data={"at": at(20).strftime("%Y-%m-%dT%H:%M"), "party": "2",
                                                 "zone_id": str(SKY)}, follow_redirects=False)
    db.expire_all()
    wl = db.query(TablesWaitlist).filter_by(phone=GUEST2).first()
    check("Guest 2 joins the waitlist", wl is not None and wl.status == "waiting" and "/done?waitlist=" in r.headers["location"])
    out = webhook(client, "PN_SKY", GUEST, btn(f"tbl_cancel_{b1.id}"))
    db.expire_all()
    check("Guest 1 cancels from WhatsApp: table freed", db.get(TablesBooking, b1.id).status == "cancelled"
          and db.get(TablesBooking, b1.id).guest_reply == "cancelled" and "cancelled" in body_of(out))
    run_jobs()
    db.expire_all()
    offer = [m for m in outbox if m["to"] == GUEST2]
    check("Waitlist guest is offered the freed table", offer and db.get(TablesWaitlist, wl.id).status == "offered"
          and offer[0]["payload"]["buttons"][0]["id"] == f"tbl_wl_{wl.id}", offer)
    out = webhook(client, "PN_SKY", GUEST2, btn(f"tbl_wl_{wl.id}"), name="Rohan Mehta")
    tok3 = token_from(out)
    db.expire_all()
    link3 = db.get(TablesLink, tok3) if tok3 else None
    check("'Book it' sends a link with 8 pm and 2 guests preselected",
          link3 is not None and link3.time == "20:00" and link3.party == 2)
    r = client.get(f"/t/{tok3}")
    check("...and the page preselects that time", at(20).strftime("%Y-%m-%dT%H:%M") in r.text)

    # ---------------------------------------------------------------- Big groups need the manager
    out = webhook(client, "PN_SKY", "919811100003", text("book"), name="Kabir")
    tok4 = token_from(out)
    outbox.clear()
    client.post(f"/t/{tok4}/book", data={"at": at(21).strftime("%Y-%m-%dT%H:%M"), "party": "9",
                                         "zone_id": str(CAB)})
    db.expire_all()
    bg = db.query(TablesBooking).filter_by(phone="919811100003").first()
    to_mgr = [m for m in outbox if m["to"] == MANAGER]
    check("A group of 9 waits for the manager", bg and bg.status == "pending")
    tpl = to_mgr[0]["payload"].get("template", {}) if to_mgr else {}
    check("Manager gets Accept / Decline on WhatsApp (template: their window is closed)",
          tpl.get("name") == "table_request" and tpl["components"][1]["parameters"][0]["payload"] == f"tbl_ok_{bg.id}", to_mgr)
    webhook(client, "PN_SKY", "919811100003", btn(f"tbl_ok_{bg.id}"))
    db.expire_all()
    check("A guest can't accept their own request", db.get(TablesBooking, bg.id).status == "pending")
    out = webhook(client, "PN_SKY", MANAGER, tpl_btn(f"tbl_ok_{bg.id}"))
    db.expire_all()
    told = [m for m in outbox if m["to"] == "919811100003"]
    check("Manager accepts (template button): confirmed and the guest is told",
          db.get(TablesBooking, bg.id).status == "confirmed" and told and "confirmed" in body_of(told))

    # ---------------------------------------------------------------- Reminders
    db.query(TablesBooking).update({TablesBooking.created_at: datetime.utcnow() - timedelta(days=2)})
    db.commit()
    NOW[0] = at(17, 30)
    run_jobs()
    def is_reminder(m):
        p = m["payload"]
        if p.get("type") == "template":
            return p["template"]["name"] == "table_reminder"
        return bool(p.get("buttons")) and p["buttons"][0]["id"].startswith("tbl_yes_")
    rem = [m for m in outbox if is_reminder(m)]
    check("Reminders go out about 3 hours before (for 8 pm and 9 pm WhatsApp bookings)", len(rem) >= 1, len(rem))
    run_jobs()
    check("...and only once", not [m for m in outbox if is_reminder(m)])
    g1 = db.query(TablesBooking).filter_by(phone="919822200000").first()
    out = webhook(client, "PN_SKY", "919822200000", btn(f"tbl_yes_{g1.id}"))
    db.expire_all()
    check("'Yes, coming' is saved for the door", db.get(TablesBooking, g1.id).guest_reply == "coming" and "see you" in body_of(out).lower())

    # ---------------------------------------------------------------- The door
    NOW[0] = at(20, 5)
    r = owner.get("/owner/tables")
    check("Tonight lists bookings by time with Arrived buttons", "8:00 PM" in r.text and "Arrived" in r.text and "Group 0" in r.text)
    r = owner.post(f"/owner/tables/bookings/{g1.id}/arrived", data={"day": FRIDAY.isoformat()}, follow_redirects=False)
    owner.post(f"/owner/tables/bookings/{g1.id}/bill", data={"bill": "₹12,400", "day": FRIDAY.isoformat()})
    db.expire_all()
    check("Arrived + bill saved", db.get(TablesBooking, g1.id).status == "arrived" and db.get(TablesBooking, g1.id).bill_amount == 12400)
    g2 = db.query(TablesBooking).filter_by(phone="919822200001").first()
    NOW[0] = at(20, 25)
    check("A no-show's table is released 20 min after the time",
          T.zone_state(db, s, db.get(TablesZone, SKY), at(20, 30), 4, NOW[0]).state in ("free", "few"))
    owner.post(f"/owner/tables/bookings/{g2.id}/no_show", data={"day": FRIDAY.isoformat()})
    db.expire_all()
    check("No-show marked", db.get(TablesBooking, g2.id).status == "no_show")
    r = owner.post("/owner/tables/add", data={"kind": "walkin", "party": "3", "zone_id": str(BAR), "day": FRIDAY.isoformat()},
                   follow_redirects=False)
    db.expire_all()
    wk = db.query(TablesBooking).filter_by(source="walkin").first()
    check("Walk-in added and seated", wk is not None and wk.status == "arrived" and wk.party == 3)
    r = owner.post("/owner/tables/add", data={"kind": "phone", "party": "2", "zone_id": str(zones["Lounge"].id),
                                              "at": at(23).strftime("%Y-%m-%dT%H:%M"), "name": "Meera",
                                              "phone": "98765 43210", "notify": "1", "day": FRIDAY.isoformat()},
                   follow_redirects=False)
    db.expire_all()
    ph = db.query(TablesBooking).filter_by(source="staff").first()
    check("Phone booking added with the guest's number", ph is not None and ph.phone == "919876543210" and ph.status == "confirmed")
    check("...and the guest gets a WhatsApp confirmation (template: they never messaged)",
          any(m["to"] == "919876543210" and m["payload"]["type"] == "template"
              and m["payload"]["template"]["name"] == "table_confirmed" for m in outbox))

    # ---------------------------------------------------------------- Door-staff login
    r = owner.post("/owner/tables/staff", data={"name": "Rahul", "username": "door.sky", "password": "doorpass99"},
                   follow_redirects=False)
    check("Owner adds a door-staff login", "ok=" in r.headers["location"])
    staff = login("door.sky", "doorpass99")
    r = staff.get("/owner/tables")
    check("Staff see Tonight, without the owner menu", r.status_code == 200 and "Arrived" in r.text and "/owner/tables/results" not in r.text)
    for path in ("/owner/tables/settings", "/owner/tables/results", "/owner/tables/guests", "/owner/dashboard", "/owner/loyalty"):
        r = staff.get(path, follow_redirects=False)
        if not (r.status_code == 303 and r.headers["location"] == "/owner/tables"):
            check(f"Staff are kept out of {path}", False, r.status_code); break
    else:
        check("Staff are kept out of settings, results, guests, Book and Loyalty", True)
    g3 = db.query(TablesBooking).filter_by(phone="919822200002").first()
    r = staff.post(f"/owner/tables/bookings/{g3.id}/cancel", data={"day": FRIDAY.isoformat()}, follow_redirects=False)
    db.expire_all()
    check("Staff can't cancel bookings", "err=" in r.headers["location"] and db.get(TablesBooking, g3.id).status == "confirmed")
    staff.post(f"/owner/tables/bookings/{g3.id}/arrived", data={"day": FRIDAY.isoformat()})
    db.expire_all()
    check("Staff can mark Arrived", db.get(TablesBooking, g3.id).status == "arrived")

    # ---------------------------------------------------------------- Next morning: how was it?
    s.google_review_url = "https://g.page/r/skyline/review"; db.commit()
    NOW[0] = datetime.combine(FRIDAY + timedelta(days=1), datetime.min.time()).replace(hour=11, minute=30)
    run_jobs()
    asks = [m for m in outbox if (m["payload"].get("buttons") and m["payload"]["buttons"][0]["id"].startswith("tbl_rate_"))
            or (m["payload"].get("type") == "template" and m["payload"]["template"]["name"] == "table_feedback")]
    check("Arrived guests get 'How was it?' next morning", {m["to"] for m in asks} == {"919822200000", "919822200002"}, [m["to"] for m in asks])
    out = webhook(client, "PN_SKY", "919822200000", btn(f"tbl_rate_loved_{g1.id}"))
    check("'Loved it' gets the Google review link", out and out[0]["payload"]["type"] == "cta_url"
          and out[0]["payload"]["url"] == "https://g.page/r/skyline/review")
    out = webhook(client, "PN_SKY", "919822200002", btn(f"tbl_rate_bad_{g3.id}"))
    db.expire_all()
    check("'Not great' is saved and answered kindly", db.get(TablesBooking, g3.id).rating == "bad" and "sorry" in body_of(out).lower())

    # ---------------------------------------------------------------- Book-again offers
    NOW[0] = datetime.combine(FRIDAY + timedelta(weeks=4, days=1), datetime.min.time()).replace(hour=13)
    run_jobs()
    is_offer = lambda m: (m["payload"].get("buttons") and m["payload"]["buttons"][0]["id"] == "tbl_book") or \
        (m["payload"].get("type") == "template" and m["payload"]["template"]["name"] == "table_book_again")
    offers = [m for m in outbox if is_offer(m)]
    check("4 weeks later, past guests get one book-again offer", {m["to"] for m in offers} == {"919822200000", "919822200002"}, [m["to"] for m in offers])
    check("Offers count against the monthly cap", T.offers_this_month(db, VID) == 2)
    out = webhook(client, "PN_SKY", "919822200000", btn("tbl_stop"))
    check("'Stop offers' opts the guest out", T.opted_out(db, VID, "919822200000"))
    out = webhook(client, "PN_SKY", "919822200000", text("STOP"))
    check("Typing STOP works too", "won't send you offers" in body_of(out))
    run_jobs()
    check("No second offer", not [m for m in outbox if is_offer(m)])
    out = webhook(client, "PN_SKY", "919822200002", tpl_btn("tbl_book"))
    check("'Book a table' on the offer sends a fresh link", token_from(out) is not None)

    # ---------------------------------------------------------------- Results
    NOW[0] = at(23, 30)
    r = T.results(db, venue, s, today=FRIDAY)
    check("Results: 2 WhatsApp tables seated, bill + average, 8% saved",
          r.seated == 2 and r.bills == 12400 + 8000 and r.saved == round(20400 * 0.08) and r.no_shows == 1 and r.walkins == 1,
          (r.seated, r.bills, r.saved, r.no_shows, r.walkins))
    page = owner.get(f"/owner/tables/results")
    check("Results page shows the commission saved", page.status_code == 200 and "Commission saved" in page.text)
    check("Guests page lists guests", "Group 0" in owner.get("/owner/tables/guests").text)
    check("Settings page shows the share link section and staff", "door.sky" in owner.get("/owner/tables/settings").text)

    # ---------------------------------------------------------------- Templates
    from create_templates import build_request
    from whatsapp_client import TEMPLATE_DEFINITIONS
    check("Tables templates are defined", all(k in TEMPLATE_DEFINITIONS for k in
          ("table_confirmed", "table_request", "table_reminder", "table_feedback", "table_waitlist", "table_book_again")))
    check("Book-again is submitted as Marketing, the rest as Utility",
          build_request("table_book_again")["category"] == "MARKETING" and build_request("table_reminder")["category"] == "UTILITY")
    bad = [k for k, d in TEMPLATE_DEFINITIONS.items() if re.match(r"\s*\{\{", d["text"]) or re.search(r"\{\{\d+\}\}\s*$", d["text"])]
    check("No template starts or ends with a variable (Meta rule)", not bad, bad)

    # ---------------------------------------------------------------- Switching off
    client.post(f"/admin/products/{VID}", auth=A, data={"book_on": "on"})
    db.expire_all()
    check("Switching Tables off and Book on works", not T.get_settings(db, VID).enabled)
    out = webhook(client, "PN_SKY", "919811100009", text("Hi"))
    check("...then 'Hi' gets the Book bot again", out and out[0]["payload"]["type"] != "cta_url")
    db.close()

os.unlink(tmp.name)
print("\nALL PASSED" if fails == 0 else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
