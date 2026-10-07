"""
test_e2e.py — simulated WhatsApp conversations through the real /webhook route,
plus the owner portal walk-in and reject flows. Uses a throwaway database and
stubs out outbound WhatsApp, so it is safe to run anytime:

    python test_e2e.py
"""
import os, sys, json, hmac, hashlib, tempfile, itertools
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
# Test-only settings, set before any project import. load_dotenv() never
# overrides variables that already exist, so your real .env is ignored here.
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
)
sys.path.insert(0, os.getcwd())
from datetime import timedelta
from fastapi.testclient import TestClient
import server, whatsapp_client, owner.owner_appointments as oa
from database import SessionLocal, Business, Service, Appointment, UserSession
from booking_engine import business_today

# Stub only the final HTTP call to Meta, so the real 24h-window and template
# logic in whatsapp_client.send_whatsapp runs. `template_ok` simulates whether
# the client's templates are approved yet.
outbox = []
meta = {"template_ok": True}
async def fake_post(recipient, pnid, payload, token=None):
    if payload.get("type") == "template" and not meta["template_ok"]:
        return "Meta API 404: template name does not exist"
    outbox.append({"to": recipient, "from": pnid, "payload": payload}); return None
whatsapp_client._post = fake_post
tname = lambda m: m["payload"].get("template", {}).get("name") if m["payload"].get("type") == "template" else None

fails = 0
def check(label, ok, extra=""):
    global fails; fails += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if (extra and not ok) else ""))

ids = itertools.count(1)
def webhook(client, pnid, sender, msg_body, name="Priya Sharma", wamid=None):
    wamid = wamid or f"wamid.{next(ids)}"
    msg = {"id": wamid, "from": sender, **msg_body}
    body = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid},
            "contacts": [{"wa_id": sender, "profile": {"name": name}}], "messages": [msg]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    outbox.clear()
    r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    return r, list(outbox)
text = lambda t: {"type": "text", "text": {"body": t}}
tap = lambda i: {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": i, "title": "x"}}}
btn = lambda i: {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": i, "title": "x"}}}
first_row = lambda p: p["sections"][0]["rows"][0]["id"]

ALL = {d: ["09:00", "18:00"] for d in ["mon","tue","wed","thu","fri","sat","sun"]}
with TestClient(server.app) as client:
    db = SessionLocal()
    A = Business(name="Glow Salon", whatsapp_business_phone_number_id="PN_A", manager_phone_number="910000000001",
                 timezone="Asia/Kolkata", operational_hours=ALL, holidays=[], slot_interval=30, max_parallel_bookings=1,
                 approval_mode="manual", is_active=True, accepting_bookings=True, enable_service_selection=True,
                 advance_booking_days=14, notification_preferences={"whatsapp_owner": True, "whatsapp_customer": True})
    B = Business(name="Aura Clinic", whatsapp_business_phone_number_id="PN_B", manager_phone_number="910000000002",
                 timezone="Asia/Kolkata", operational_hours=ALL, holidays=[], slot_interval=30, max_parallel_bookings=1,
                 approval_mode="automatic", is_active=True, accepting_bookings=True, enable_service_selection=True,
                 advance_booking_days=14, notification_preferences={"whatsapp_owner": True})
    db.add_all([A, B]); db.commit()
    sa = Service(business_id=A.id, name="Haircut", duration=45, price=None, is_active=True)   # price None must not crash
    sb = Service(business_id=B.id, name="HydraFacial", duration=60, price=4500, is_active=True)
    db.add_all([sa, sb]); db.commit()
    from database import Owner
    db.add(Owner(business_id=A.id, full_name="Glow Owner", email="o@glow.test", phone_number="910000000001", password_hash="x", is_active=True)); db.commit()
    P = "919999999999"

    # --- signature + dedupe
    r = client.post("/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=bad"})
    check("bad signature -> 403", r.status_code == 403)

    r, out = webhook(client, "PN_A", P, text("hi"), wamid="wamid.dup")
    check("'hi' -> 200 + service list from tenant A's number",
          r.status_code == 200 and out and out[0]["payload"]["type"] == "list" and out[0]["from"] == "PN_A", out)
    check("greets by first name, no 'expired' warning",
          out and "Hi Priya!" in out[0]["payload"]["body"] and "expired" not in out[0]["payload"]["body"], out and out[0]["payload"]["body"])
    r, out = webhook(client, "PN_A", P, text("hi"), wamid="wamid.dup")
    check("duplicate wamid -> nothing sent", r.status_code == 200 and out == [], out)

    # --- full flow, tenant A (manual approval)
    svc_id = first_row(webhook(client, "PN_A", P, text("hi"))[1][0]["payload"])
    r, out = webhook(client, "PN_A", P, tap(svc_id)); dates = out[0]["payload"]
    check("service -> date list", dates["type"] == "list" and dates["sections"][0]["rows"][0]["id"].startswith("date_"), dates)
    first_date = dates["sections"][0]["rows"][0]["id"]
    # choose tomorrow to avoid end-of-day timing issues
    tomorrow_id = f"date_{(business_today(A) + timedelta(days=1)).isoformat()}"
    date_ids = [row["id"] for row in dates["sections"][0]["rows"]]
    check("tomorrow offered", tomorrow_id in date_ids, date_ids)
    r, out = webhook(client, "PN_A", P, tap(tomorrow_id)); times = out[0]["payload"]
    rows = times["sections"][0]["rows"]
    check("date -> time list (<=10 rows, has 'Later times')", len(rows) <= 10 and rows[-1]["id"] == "page_1", [r_["id"] for r_ in rows])
    r, out = webhook(client, "PN_A", P, tap("page_1"))
    check("'Later times' -> next page", out[0]["payload"]["sections"][0]["rows"][0]["id"].startswith("time_"), out)
    r, out = webhook(client, "PN_A", P, tap("time_10:00"))
    check("time -> confirm buttons", out[0]["payload"]["type"] == "buttons" and "Confirm" in out[0]["payload"]["buttons"][0]["title"], out)
    r, out = webhook(client, "PN_A", P, btn("action_confirm"))
    to_customer = [m for m in out if m["to"] == P]; to_owner = [m for m in out if m["to"] == "910000000001"]
    check("confirm -> customer 'request received'", to_customer and "Request received" in to_customer[0]["payload"]["body"], out)
    check("owner hasn't messaged in 24h -> gets 'new_booking_request' TEMPLATE from tenant A",
          to_owner and tname(to_owner[0]) == "new_booking_request" and to_owner[0]["from"] == "PN_A", out)
    if to_owner and tname(to_owner[0]):
        comps = to_owner[0]["payload"]["template"]["components"]
        payloads = [c["parameters"][0]["payload"] for c in comps if c["type"] == "button"]
        params = [p_["text"] for p_ in comps[0]["parameters"]]
        check("template has Approve/Reject payloads and clean parameters",
              payloads[0].startswith("confirm_") and payloads[1].startswith("cancel_")
              and all("\n" not in x and x for x in params), (payloads, params))
    appt = db.query(Appointment).filter_by(business_id=A.id).first()
    check("appointment saved: pending, name, 10:00", appt and appt.status == "pending" and appt.customer_name == "Priya Sharma"
          and appt.appointment_time.strftime("%H:%M") == "10:00", appt and (appt.status, appt.customer_name))

    # --- stale tap
    r, out = webhook(client, "PN_A", P, tap(tomorrow_id))
    check("stale date tap -> polite restart", out and "expired" in out[0]["payload"]["body"], out)

    # --- T5 manager actions
    r, out = webhook(client, "PN_A", "918888888888", btn(f"confirm_{appt.id}"))
    db.refresh(appt)
    check("stranger tapping Approve is ignored", out == [] and appt.status == "pending", (out, appt.status))
    tpl_btn = lambda payload: {"type": "button", "button": {"payload": payload, "text": "Approve"}}
    r, out = webhook(client, "PN_A", "910000000001", tpl_btn(f"confirm_{appt.id}"))
    db.refresh(appt)
    check("manager taps Approve on the TEMPLATE -> confirmed", appt.status == "confirmed")
    check("customer notified + manager acknowledged",
          any(m["to"] == P and "confirmed" in m["payload"]["body"] for m in out)
          and any(m["to"] == "910000000001" and "Customer notified" in m["payload"]["body"] for m in out), out)
    r, out = webhook(client, "PN_A", "910000000001", btn(f"confirm_{appt.id}"))
    check("second Approve -> 'already confirmed'", out and "already confirmed" in out[0]["payload"]["body"], out)
    r, out = webhook(client, "PN_B", "910000000002", btn(f"cancel_{appt.id}"))
    db.refresh(appt)
    check("other tenant's manager can't touch it (sees 'not found')", appt.status == "confirmed" and "wasn't found" in out[0]["payload"]["body"], (out, appt.status))

    # --- capacity: 10:00 now taken for 45 min at capacity 1
    webhook(client, "PN_A", "917777777777", text("hi")); webhook(client, "PN_A", "917777777777", tap(svc_id))
    r, out = webhook(client, "PN_A", "917777777777", tap(tomorrow_id))
    all_ids = [x["id"] for x in out[0]["payload"]["sections"][0]["rows"]]
    check("10:00 and 10:30 no longer offered to the next patient", "time_10:00" not in all_ids and "time_10:30" not in all_ids, all_ids)
    r, out = webhook(client, "PN_A", "917777777777", tap("time_10:30"))
    check("tapping a taken time re-offers times", out and "just taken" in out[0]["payload"]["body"], out)

    # --- tenant B: same patient, auto-confirm, isolated session
    r, out = webhook(client, "PN_B", P, text("hello"))
    check("tenant B replies from its own number with its own services",
          out and out[0]["from"] == "PN_B" and out[0]["payload"]["sections"][0]["rows"][0]["title"] == "HydraFacial", out)
    webhook(client, "PN_B", P, tap(f"svc_{sb.id}"))
    webhook(client, "PN_B", P, tap(tomorrow_id)); webhook(client, "PN_B", P, tap("time_11:00"))
    r, out = webhook(client, "PN_B", P, btn("action_confirm"))
    # Tenant B's owner tapped a button earlier in this test, so their window is
    # open and a normal message is correct (no template charge).
    check("auto-confirm business -> 'You're booked' + owner FYI (window open -> normal message)",
          any(m["to"] == P and "You're booked" in m["payload"].get("body", "") for m in out)
          and any(m["to"] == "910000000002" and m["payload"]["type"] == "text" for m in out), out)
    r, out = webhook(client, "PN_B", P, tap(f"svc_{sa.id}"))
    check("tenant A's service id rejected at tenant B", out and "expired" in out[0]["payload"]["body"], out)

    # --- paused business
    A.accepting_bookings = False; db.commit()
    r, out = webhook(client, "PN_A", P, text("hi"))
    check("paused business -> polite reply", out and "isn't taking WhatsApp bookings" in out[0]["payload"]["body"], out)
    A.accepting_bookings = True; db.commit()

    # --- owner portal: walk-in + portal reject
    r = client.post(f"/admin/businesses/{A.id}/impersonate", auth=("admin", "verzio-dev-admin"), follow_redirects=False)
    check("admin impersonation -> session cookie", r.status_code == 303 and "verzio_owner_session" in r.headers.get("set-cookie", ""), r.status_code)
    client.cookies.set("verzio_owner_session", r.cookies.get("verzio_owner_session"))
    r = client.get("/owner/appointments/new")
    check("New booking form renders", r.status_code == 200 and "Add a booking" in r.text, r.status_code)
    day = (business_today(A) + timedelta(days=1)).isoformat()
    form = {"customer_name": "Walk In", "customer_phone": "9876543210", "service_id": str(sa.id), "date": day, "time": "14:00"}
    r = client.post("/owner/appointments/manual", data=form, follow_redirects=False)
    walk = db.query(Appointment).filter_by(customer_name="Walk In").first()
    check("walk-in saved as confirmed with +91", r.status_code == 303 and walk and walk.status == "confirmed"
          and walk.customer_phone == "919876543210", (r.status_code, walk and walk.status))
    r = client.post("/owner/appointments/manual", data={**form, "customer_name": "Clash"}, follow_redirects=False)
    check("clashing walk-in -> form error, not saved", r.status_code == 400 and "fully booked" in r.text
          and not db.query(Appointment).filter_by(customer_name="Clash").first(), r.status_code)
    r = client.post("/owner/appointments/manual", data={**form, "customer_name": "Override", "allow_overbook": "on"}, follow_redirects=False)
    check("override saves anyway", r.status_code == 303 and db.query(Appointment).filter_by(customer_name="Override").first())
    webhook(client, "PN_A", "916666666666", text("hi")); webhook(client, "PN_A", "916666666666", tap(svc_id))
    r, out = webhook(client, "PN_A", "916666666666", tap(tomorrow_id))
    ids_ = [x["id"] for x in out[0]["payload"]["sections"][0]["rows"]] + [x["id"] for x in webhook(client, "PN_A", "916666666666", tap("page_1"))[1][0]["payload"]["sections"][0]["rows"]]
    check("walk-in at 14:00 blocks WhatsApp slot", "time_14:00" not in ids_, ids_)

    pend = Appointment(business_id=A.id, service_id=sa.id, customer_phone="915555555555", customer_name="Ravi",
                       appointment_time=walk.appointment_time + timedelta(hours=2), status="pending")
    db.add(pend); db.commit()
    outbox.clear()
    r = client.post(f"/owner/appointments/{pend.id}/reject", follow_redirects=False)
    db.refresh(pend)
    check("portal Reject, customer never messaged -> 'booking_declined' TEMPLATE",
          r.status_code == 303 and pend.status == "cancelled"
          and any(m["to"] == "915555555555" and tname(m) == "booking_declined" for m in outbox), outbox)

    # --- 24h window: customer inside window gets free-form; after 24h gets a template
    from whatsapp_client import record_inbound
    import time as _t
    pend2 = Appointment(business_id=A.id, service_id=sa.id, customer_phone="914444444444", customer_name="Asha",
                        appointment_time=walk.appointment_time + timedelta(hours=3), status="pending")
    db.add(pend2); db.commit()
    record_inbound(A.id, "914444444444")                        # messaged just now
    outbox.clear()
    client.post(f"/owner/appointments/{pend2.id}/approve", follow_redirects=False)
    check("customer messaged recently -> normal free-form confirmation",
          any(m["to"] == "914444444444" and m["payload"]["type"] == "text" and "confirmed" in m["payload"]["body"]
              for m in outbox), outbox)
    record_inbound(A.id, "914444444444", at=_t.time() - 25 * 3600)   # last message 25h ago
    outbox.clear()
    client.post(f"/owner/appointments/{pend2.id}/cancel", follow_redirects=False)
    check("customer's last message 25h ago -> 'booking_cancelled' TEMPLATE",
          any(m["to"] == "914444444444" and tname(m) == "booking_cancelled" for m in outbox), outbox)

    # --- template not approved yet -> falls back to a normal message + incident
    from database import Incident
    meta["template_ok"] = False
    pend3 = Appointment(business_id=A.id, service_id=sa.id, customer_phone="913333333333", customer_name="Dev",
                        appointment_time=walk.appointment_time + timedelta(hours=4), status="pending")
    db.add(pend3); db.commit()
    outbox.clear()
    client.post(f"/owner/appointments/{pend3.id}/approve", follow_redirects=False)
    check("template not approved -> falls back to normal message",
          any(m["to"] == "913333333333" and m["payload"]["type"] == "text" for m in outbox), outbox)
    check("...and records an incident so you notice",
          db.query(Incident).filter(Incident.title == "Message template not delivered").count() >= 1)
    meta["template_ok"] = True

    # --- HX: messages that aren't bookings
    from customer_inbox import classify_text
    for phrase in ["hi", "Hii", "hello!", "Namaste", "book", "I want to book a haircut", "any slot tomorrow?", "Good morning"]:
        check(f"'{phrase}' -> booking", classify_text(phrase) == "book")
    for phrase in ["I had a facial 3 days ago and my skin is red", "can I reschedule my appointment",
                   "what is the price of hydrafacial", "where is your clinic located? is there parking near the main road"]:
        check(f"'{phrase}' -> person", classify_text(phrase) == "other")

    for phrase in ["ok", "Thanks!", "👍", "ok thank you", "🙏"]:
        check(f"'{phrase}' -> no reply needed", classify_text(phrase) == "ack")
    r, out = webhook(client, "PN_A", P, text("Thank you!"))
    check("'Thank you!' after a booking -> silence, not a menu", out == [], out)
    Q, MGR_A = "917777777777", "910000000001"
    def mgr_text(m):   # the words the manager sees, template or normal message
        if tname(m) == "customer_message":
            return " ".join(p_["text"] for p_ in m["payload"]["template"]["components"][0]["parameters"])
        return m["payload"].get("body", "")
    r, out = webhook(client, "PN_A", Q, text("I had a facial 3 days ago and my skin is still red"))
    check("follow-up question -> 'Book appointment / Talk to us' buttons, NOT the booking list",
          len(out) == 1 and out[0]["to"] == Q and out[0]["payload"]["type"] == "buttons"
          and [b_["id"] for b_ in out[0]["payload"]["buttons"]] == ["action_start", "action_human"], out)
    r, out = webhook(client, "PN_A", Q, btn("action_human"))
    to_q = [m for m in out if m["to"] == Q]; to_mgr = [m for m in out if m["to"] == MGR_A]
    check("'Talk to us' -> customer told it's passed on", to_q and "passed your message" in to_q[0]["payload"]["body"], out)
    check("...manager gets the customer's words with a wa.me link",
          to_mgr and "skin is still red" in mgr_text(to_mgr[0]) and "wa.me/917777777777" in mgr_text(to_mgr[0]), out)
    r, out = webhook(client, "PN_A", Q, text("also it itches a bit"))
    check("while a person handles it, new texts go to the manager, bot stays quiet with the customer",
          out and all(m["to"] == MGR_A for m in out) and "itches" in mgr_text(out[0]), out)
    r, out = webhook(client, "PN_A", Q, text("hi"))
    check("'hi' during the handoff doesn't restart the bot", all(m["to"] == MGR_A for m in out), out)
    r, out = webhook(client, "PN_A", Q, text("Book"))
    check("typing 'Book' brings the booking menu back", out and out[0]["to"] == Q and out[0]["payload"]["type"] == "list", out)

    R = "916666666666"
    webhook(client, "PN_A", R, text("do you do bridal makeup packages for 5 people"))
    r, out = webhook(client, "PN_A", R, text("for 20th december"))
    check("two non-booking texts in a row -> handed to a person without asking again",
          any(m["to"] == R and "passed your message" in m["payload"].get("body", "") for m in out)
          and any(m["to"] == MGR_A and "bridal" in mgr_text(m) and "20th december" in mgr_text(m) for m in out), out)

    # owner hasn't messaged the business number in 24h -> approved template is used
    from sqlalchemy import text as sql_text
    with server.engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM conversation_windows WHERE phone = :p"), {"p": MGR_A})
    T = "914444444444"
    webhook(client, "PN_A", T, text("is the clinic open on diwali"))
    r, out = webhook(client, "PN_A", T, btn("action_human"))
    to_mgr = [m for m in out if m["to"] == MGR_A]
    check("manager outside 24h -> 'customer_message' TEMPLATE with the customer's words",
          to_mgr and tname(to_mgr[0]) == "customer_message" and "diwali" in mgr_text(to_mgr[0]), out)

    S = "915555555555"
    r, out = webhook(client, "PN_A", S, {"type": "image", "image": {"id": "MEDIA1", "caption": "is this normal?"}})
    check("photo -> choice buttons (not 'send Hi')", out and out[0]["payload"]["type"] == "buttons", out)
    r, out = webhook(client, "PN_A", S, btn("action_start"))
    check("'Book appointment' tap -> booking list", out and out[0]["payload"]["type"] == "list", out)

    # --- Meta reports a failed delivery (e.g. 131047) -> logged, once
    from database import ActivityEvent
    status_body = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "PN_A"},
        "statuses": [{"id": "wamid.out1", "status": "failed", "recipient_id": "913333333333",
                      "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"test-secret", status_body, hashlib.sha256).hexdigest()
    for _ in range(2):
        client.post("/webhook", content=status_body, headers={"X-Hub-Signature-256": sig})
    failed_logs = db.query(ActivityEvent).filter(ActivityEvent.event_type == "whatsapp_delivery_failed").all()
    check("failed delivery status logged once with a 24h hint",
          len(failed_logs) == 1 and "24-hour window" in failed_logs[0].message, [e.message for e in failed_logs])
    # --- Tier 1 fixes ---------------------------------------------------
    from whatsapp_client import canonical_phone
    for raw, tz, want in [("98765 43210", "Asia/Kolkata", "919876543210"), ("+91 98765-43210", "Asia/Kolkata", "919876543210"),
                          ("09876543210", "Asia/Kolkata", "919876543210"), ("919876543210", "Asia/Kolkata", "919876543210"),
                          ("07700 900123", "Europe/London", "447700900123"), ("050 123 4567", "Asia/Dubai", "971501234567"),
                          ("0091 98765 43210", "UTC", "919876543210"), ("9876543210", "UTC", "919876543210")]:
        check(f"canonical_phone({raw!r}, {tz}) -> {want}", canonical_phone(raw, tz) == want, canonical_phone(raw, tz))

    def book(pnid, phone, day_offset=1, slot_index=0):
        _, out = webhook(client, pnid, phone, text("hi"))
        if out[0]["payload"]["type"] != "list":
            return out
        _, out = webhook(client, pnid, phone, tap(first_row(out[0]["payload"])))
        day = f"date_{(business_today(A) + timedelta(days=day_offset)).isoformat()}"
        _, out = webhook(client, pnid, phone, tap(day))
        t = out[0]["payload"]["sections"][0]["rows"][slot_index]["id"]
        webhook(client, pnid, phone, tap(t))
        return webhook(client, pnid, phone, btn("action_confirm"))[1]

    # manager number saved without country code still gets requests and can approve
    C = Business(name="Desi Cuts", whatsapp_business_phone_number_id="PN_C", manager_phone_number="98000 00003",
                 timezone="Asia/Kolkata", operational_hours=ALL, holidays=[], slot_interval=30, max_parallel_bookings=1,
                 approval_mode="manual", is_active=True, accepting_bookings=True, enable_service_selection=True,
                 advance_booking_days=14, notification_preferences={"whatsapp_owner": True})
    db.add(C); db.commit()
    db.add(Service(business_id=C.id, name="Beard trim", duration=30, price=200, is_active=True)); db.commit()
    out = book("PN_C", "913000000001", day_offset=2)
    check("10-digit manager number -> request sent to 91XXXXXXXXXX", any(m["to"] == "919800000003" for m in out), out)
    appt_c = db.query(Appointment).filter_by(business_id=C.id).first()
    webhook(client, "PN_C", "919800000003", btn(f"confirm_{appt_c.id}"))
    db.refresh(appt_c)
    check("...and the manager's Approve tap is accepted", appt_c.status == "confirmed", appt_c.status)
    server._normalize_manager_numbers(); db.expire_all()
    check("startup fixes stored manager numbers", db.get(Business, C.id).manager_phone_number == "919800000003")

    # owner has one phone: manager number == business number -> auto-confirm, nothing sent to itself
    from whatsapp_accounts import WhatsAppAccount, forget_cached
    from database import Incident
    D = Business(name="Solo Spa", whatsapp_business_phone_number_id="PN_D", manager_phone_number="919700000004",
                 timezone="Asia/Kolkata", operational_hours=ALL, holidays=[], slot_interval=30, max_parallel_bookings=1,
                 approval_mode="manual", is_active=True, accepting_bookings=True, enable_service_selection=True,
                 advance_booking_days=14, notification_preferences={"whatsapp_owner": True})
    db.add(D); db.commit()
    db.add(Service(business_id=D.id, name="Massage", duration=60, price=1500, is_active=True))
    db.add(WhatsAppAccount(business_id=D.id, phone_number_id="PN_D", display_phone_number="+91 97000 00004",
                           coexistence=True, status="error")); db.commit()
    forget_cached(D.id)
    incidents_before = db.query(Incident).count()
    out = book("PN_D", "913000000002", day_offset=2)
    appt_d = db.query(Appointment).filter_by(business_id=D.id).first()
    check("one-phone owner + manual approval -> booking confirmed automatically", appt_d and appt_d.status == "confirmed",
          appt_d and appt_d.status)
    check("...customer told 'You're booked', nothing sent to the business's own number",
          any("You're booked" in m["payload"].get("body", "") for m in out) and all(m["to"] != "919700000004" for m in out), out)
    check("...and no 'manager is business number' incident per booking", db.query(Incident).count() == incidents_before)

    # one number can't block a day: max 2 upcoming bookings per business
    U = "913000000009"
    book("PN_B", U, day_offset=3, slot_index=0)
    book("PN_B", U, day_offset=3, slot_index=2)
    held = db.query(Appointment).filter(Appointment.business_id == B.id, Appointment.customer_phone == U).count()
    r, out = webhook(client, "PN_B", U, text("hi"))
    check("2 bookings held -> 3rd 'hi' lists them and offers 'Talk to us' instead of the menu",
          held == 2 and out and out[0]["payload"]["type"] == "buttons" and "2 upcoming bookings" in out[0]["payload"]["body"]
          and out[0]["payload"]["buttons"][0]["id"] == "action_human", (held, out))

    # admin form: number cleaned up, junk refused, Phone Number ID optional
    auth = ("admin", "verzio-dev-admin")
    form = {"name": "Form Salon", "manager_phone_number": "98111 22233", "timezone": "Asia/Kolkata",
            "is_active": "on", "accepting_bookings": "on", "approval_mode": "manual"}
    r = client.post("/admin/businesses", data=form, auth=auth, follow_redirects=False)
    fs = db.query(Business).filter_by(name="Form Salon").first()
    check("admin create: blank Phone Number ID ok, manager saved as 919811122233",
          r.status_code == 303 and fs and fs.manager_phone_number == "919811122233"
          and fs.whatsapp_business_phone_number_id.startswith("pending-"), (r.status_code, fs and fs.manager_phone_number))
    r = client.post("/admin/businesses", data=form | {"name": "Bad Number", "manager_phone_number": "12345"}, auth=auth, follow_redirects=False)
    check("admin create: junk manager number -> form error, not saved",
          r.status_code == 400 and not db.query(Business).filter_by(name="Bad Number").first(), r.status_code)

    # --- Launch features ---------------------------------------------------
    import asyncio, scheduler
    from datetime import datetime as _dt
    from booking_engine import business_now

    # more than 10 services -> paged list, every service reachable
    E = Business(name="Big Salon", whatsapp_business_phone_number_id="PN_E", manager_phone_number="919600000005",
                 timezone="Asia/Kolkata", operational_hours={d: ["09:00", "18:00", "13:00", "14:00"] for d in ALL},
                 holidays=[], slot_interval=30, max_parallel_bookings=1, approval_mode="manual", is_active=True,
                 accepting_bookings=True, enable_service_selection=True, advance_booking_days=14,
                 notification_preferences={"whatsapp_owner": True})
    db.add(E); db.commit()
    for i in range(23):
        db.add(Service(business_id=E.id, name=f"Service {i:02d}", duration=30, price=100 + i, is_active=True))
    db.commit()
    V = "912000000001"
    _, out = webhook(client, "PN_E", V, text("hi"))
    rows = out[0]["payload"]["sections"][0]["rows"]
    check("23 services -> page 1 shows 9 + 'More services'", len(rows) == 10 and rows[-1]["id"] == "svcpage_1", [r_["id"] for r_ in rows])
    seen = [r_["id"] for r_ in rows if r_["id"].startswith("svc_")]
    _, out = webhook(client, "PN_E", V, tap("svcpage_1")); rows = out[0]["payload"]["sections"][0]["rows"]
    seen += [r_["id"] for r_ in rows if r_["id"].startswith("svc_")]
    _, out = webhook(client, "PN_E", V, tap("svcpage_2")); rows = out[0]["payload"]["sections"][0]["rows"]
    seen += [r_["id"] for r_ in rows if r_["id"].startswith("svc_")]
    check("...all 23 services reachable across pages, last page has 'Back'", len(set(seen)) == 23 and rows[-1]["id"] == "svcpage_0", len(set(seen)))
    _, out = webhook(client, "PN_E", V, tap(seen[-1]))
    check("picking a service from page 3 -> date list", out[0]["payload"]["sections"][0]["rows"][0]["id"].startswith("date_"), out)

    # break 13:00-14:00 -> no slot that overlaps it
    tomorrow = business_today(E) + timedelta(days=1)
    _, out = webhook(client, "PN_E", V, tap(f"date_{tomorrow.isoformat()}"))
    times = []
    for page in range(4):
        rows = out[0]["payload"]["sections"][0]["rows"]
        times += [r_["id"][5:] for r_ in rows if r_["id"].startswith("time_")]
        nxt = [r_["id"] for r_ in rows if r_["id"].startswith("page_") and r_["id"] != "page_0"]
        if not nxt: break
        _, out = webhook(client, "PN_E", V, tap(nxt[0]))
    check("break 13:00-14:00: 12:30 and 14:00 offered, 13:00 and 13:30 not",
          "12:30" in times and "14:00" in times and "13:00" not in times and "13:30" not in times, times)
    from booking_engine import VerzioSaaSEngine, BookingEngineException as BEE
    try:
        VerzioSaaSEngine().validate_and_book(db, "PN_E", _dt.combine(tomorrow, _dt.strptime("13:00", "%H:%M").time()),
                                             "912000000002", int(seen[0][4:]))
        check("booking inside the break is refused by the engine", False)
    except BEE as exc:
        check("booking inside the break is refused by the engine", exc.error_code == "ERR_OUTSIDE_HOURS", exc.error_code)
    from hours import build_hours
    h = build_hours({"hours_mon_enabled": "on", "hours_mon_open": "09:00", "hours_mon_close": "18:00",
                     "break_start": "13:00", "break_end": "14:00", "hours_tue_enabled": "on",
                     "hours_tue_open": "14:00", "hours_tue_close": "20:00"})
    check("form: break saved on days it fits, skipped where it doesn't", h == {"mon": ["09:00", "18:00", "13:00", "14:00"], "tue": ["14:00", "20:00"]}, h)

    # scheduler -------------------------------------------------------------
    scheduler.QUIET_START, scheduler.QUIET_END = 24, 0          # any hour is fine in tests
    scheduler.SUMMARY_HOUR, scheduler.SUMMARY_LATEST_HOUR = 0, 24
    svc_e = db.query(Service).filter_by(business_id=E.id).first()
    now_e = business_now(E)
    def appt(when, status, phone, created_hours_ago=10, name="Kiran Rao"):
        a = Appointment(business_id=E.id, service_id=svc_e.id, customer_phone=phone, customer_name=name,
                        appointment_time=when.replace(second=0, microsecond=0), status=status,
                        created_at=_dt.utcnow() - timedelta(hours=created_hours_ago))
        db.add(a); db.commit(); return a
    remind = appt(now_e + timedelta(hours=20), "confirmed", "912100000001")
    fresh = appt(now_e + timedelta(hours=20), "confirmed", "912100000002", created_hours_ago=0.5)
    far = appt(now_e + timedelta(hours=40), "confirmed", "912100000003")
    old_pending = appt(now_e + timedelta(hours=30), "pending", "912100000004", created_hours_ago=3)
    late_pending = appt(now_e + timedelta(minutes=30), "pending", "912100000005", created_hours_ago=3)
    today_appt = appt(now_e.replace(hour=23, minute=0) if now_e.hour < 22 else now_e + timedelta(minutes=90), "confirmed", "912100000006")

    outbox.clear()
    result = asyncio.run(scheduler.tick())
    sent = list(outbox)
    to = lambda ph: [m for m in sent if m["to"] == ph]
    ids3 = [f"remind_ok_{remind.id}", f"remind_move_{remind.id}", f"remind_cancel_{remind.id}"]
    check("reminder sent ~20h ahead, with I'll be there / Reschedule / Cancel", to("912100000001")
          and (tname(to("912100000001")[0]) == "appointment_reminder_v2"
               or [b_["id"] for b_ in to("912100000001")[0]["payload"].get("buttons", [])] == ids3),
          to("912100000001"))
    rem = to("912100000001")[0] if to("912100000001") else None
    if rem and tname(rem):
        payloads = [c["parameters"][0]["payload"] for c in rem["payload"]["template"]["components"] if c["type"] == "button"]
        check("...reminder template carries the booking's button ids", payloads == ids3, payloads)
    check("no reminder for a booking made 30 min ago", not to("912100000002"))
    check("no reminder 40h ahead (too early)", not to("912100000003"))
    mgr_e = to("919600000005")
    check("owner nudged about a request waiting 3h", any(str(old_pending.id) in json.dumps(m["payload"]) for m in mgr_e), mgr_e)
    db.refresh(late_pending)
    check("request still pending 30 min before -> declined automatically", late_pending.status == "cancelled", late_pending.status)
    check("...and that customer is told", bool(to("912100000005")), sent)
    check("morning summary sent to the owner", any(tname(m) == "daily_summary" or "Good morning" in m["payload"].get("body", "") for m in mgr_e), mgr_e)

    outbox.clear()
    asyncio.run(scheduler.tick())
    check("second tick sends nothing again (no duplicate reminders, nudges, summaries)", outbox == [], outbox)

    # customer answers the reminder
    _, out = webhook(client, "PN_E", "912100000001", {"type": "button", "button": {"payload": f"remind_ok_{remind.id}", "text": "I'll be there"}})
    check("'I'll be there' -> thanks, booking stays confirmed", out and "See you" in out[0]["payload"]["body"], out)
    _, out = webhook(client, "PN_E", "918888800000", {"type": "button", "button": {"payload": f"remind_cancel_{remind.id}", "text": "Cancel"}})
    db.refresh(remind)
    check("someone else can't cancel it", out == [] and remind.status == "confirmed", (out, remind.status))
    _, out = webhook(client, "PN_E", "912100000001", {"type": "button", "button": {"payload": f"remind_cancel_{remind.id}", "text": "Cancel booking"}})
    db.refresh(remind)
    check("'Cancel booking' -> cancelled, customer told, owner told", remind.status == "cancelled"
          and any(m["to"] == "912100000001" and "cancelled" in m["payload"]["body"] for m in out)
          and any(m["to"] == "919600000005" for m in out), out)
    _, out = webhook(client, "PN_E", "912100000001", {"type": "button", "button": {"payload": f"remind_cancel_{remind.id}", "text": "Cancel booking"}})
    check("tapping Cancel again -> 'already cancelled'", out and "already cancelled" in out[0]["payload"]["body"], out)

    # --- Reschedule ------------------------------------------------------
    import scheduler as _sch
    W = "912300000001"
    mv = appt(now_e.replace(hour=10, minute=0) + timedelta(days=2), "confirmed", W, name="Meera Iyer")
    _sch.claim("reminder", mv.id)                       # pretend its reminder already went out
    old_time = mv.appointment_time
    btn_tpl = lambda p_: {"type": "button", "button": {"payload": p_, "text": "Reschedule"}}
    _, out = webhook(client, "PN_E", W, btn_tpl(f"remind_move_{mv.id}"), name="Meera Iyer")
    check("reminder 'Reschedule' -> date list for the same service, saying what's being moved",
          out and out[0]["payload"]["type"] == "list" and "Let's move your" in out[0]["payload"]["body"], out)
    new_day = (old_time + timedelta(days=1)).date()
    _, out = webhook(client, "PN_E", W, tap(f"date_{new_day.isoformat()}"))
    _, out = webhook(client, "PN_E", W, tap("time_11:00"))
    check("confirm screen says 'Move your booking' with Confirm new time / Keep old time",
          out and "Move your booking" in out[0]["payload"]["body"]
          and [b_["title"] for b_ in out[0]["payload"]["buttons"]] == ["Confirm new time", "Keep old time"], out)
    before = db.query(Appointment).filter_by(business_id=E.id).count()
    _, out = webhook(client, "PN_E", W, btn("action_confirm"))
    db.refresh(mv)
    check("booking moved in place: same id, new time, still confirmed, no new booking",
          mv.appointment_time.date() == new_day and mv.appointment_time.strftime("%H:%M") == "11:00"
          and mv.status == "confirmed" and db.query(Appointment).filter_by(business_id=E.id).count() == before,
          (mv.appointment_time, mv.status))
    check("customer told it's moved; owner told with old + new time",
          any(m["to"] == W and "moved" in m["payload"]["body"] for m in out)
          and any(m["to"] == "919600000005" and ("Was:" in json.dumps(m["payload"]) or tname(m) == "customer_rescheduled") for m in out), out)
    eng = VerzioSaaSEngine()
    check("old 10:00 slot is free again, new 11:00 is taken",
          "10:00" in eng.get_available_slots(db, E, old_time.date(), svc_e)
          and "11:00" not in eng.get_available_slots(db, E, new_day, svc_e))
    check("reminder will be sent again for the new time", not _sch.already_claimed("reminder", mv.id))

    # 'Keep old time' leaves it alone
    webhook(client, "PN_E", W, btn_tpl(f"remind_move_{mv.id}"))
    webhook(client, "PN_E", W, tap(f"date_{new_day.isoformat()}"))
    webhook(client, "PN_E", W, tap("time_15:00"))
    _, out = webhook(client, "PN_E", W, btn("action_cancel"))
    db.refresh(mv)
    check("'Keep old time' -> unchanged and says so", mv.appointment_time.strftime("%H:%M") == "11:00"
          and "stays" in out[0]["payload"]["body"], out)

    # moving to a slot overlapping its own current time works (own seat ignored)
    webhook(client, "PN_E", W, btn_tpl(f"remind_move_{mv.id}"))
    _, out = webhook(client, "PN_E", W, tap(f"date_{new_day.isoformat()}"))
    times_now = []
    for _p in range(4):
        rws = out[0]["payload"]["sections"][0]["rows"]; times_now += [r_["id"] for r_ in rws]
        nx = [r_["id"] for r_ in rws if r_["id"].startswith("page_") and r_["id"] != "page_0"]
        if not nx: break
        _, out = webhook(client, "PN_E", W, tap(nx[0]))
    check("its own current time (11:00) is offered when moving it", "time_11:00" in times_now and "time_11:30" in times_now, times_now)
    webhook(client, "PN_E", W, text("hi"))                       # abandon: fresh Hi is a normal booking again
    from reschedule import get_intent
    check("a fresh 'Hi' forgets the reschedule", get_intent(db, E.id, W) is None)

    # typed requests
    _, out = webhook(client, "PN_E", W, text("Hi, I need to reschedule my appointment please"))
    check("typed 'reschedule' with one booking -> Pick a new time / Cancel / Talk to us",
          out and out[0]["payload"]["type"] == "buttons"
          and [b_["id"] for b_ in out[0]["payload"]["buttons"]] == [f"remind_move_{mv.id}", f"remind_cancel_{mv.id}", "action_human"], out)
    mv2 = appt(now_e.replace(hour=16, minute=0) + timedelta(days=3), "confirmed", W, name="Meera Iyer")
    _, out = webhook(client, "PN_E", W, text("sorry I can't come, please cancel"))
    rows_ = out[0]["payload"]["sections"][0]["rows"] if out and out[0]["payload"]["type"] == "list" else []
    check("typed 'cancel' with two bookings -> list to pick which one",
          [r_["id"] for r_ in rows_][:2] == [f"remind_cancel_{mv.id}", f"remind_cancel_{mv2.id}"], out)
    _, out = webhook(client, "PN_E", W, tap(f"remind_cancel_{mv2.id}"))
    db.refresh(mv2)
    check("picking it cancels that one only", mv2.status == "cancelled" and db.get(Appointment, mv.id).status == "confirmed")
    _, out = webhook(client, "PN_E", "912399999999", text("I want to reschedule"))
    check("typed 'reschedule' with no bookings -> normal choice buttons", out and out[0]["payload"]["type"] == "buttons"
          and out[0]["payload"]["buttons"][-1]["id"] == "action_human", out)

    # pending request moved -> owner gets Approve/Reject again for the new time
    pend = appt(now_e.replace(hour=10, minute=0) + timedelta(days=4), "pending", "912300000002", created_hours_ago=0.2)
    webhook(client, "PN_E", "912300000002", btn_tpl(f"remind_move_{pend.id}"))
    webhook(client, "PN_E", "912300000002", tap(f"date_{(pend.appointment_time + timedelta(days=1)).date().isoformat()}"))
    webhook(client, "PN_E", "912300000002", tap("time_12:00"))
    _, out = webhook(client, "PN_E", "912300000002", btn("action_confirm"))
    db.refresh(pend)
    owner_msgs = [m for m in out if m["to"] == "919600000005"]
    check("moved pending request stays pending; owner gets Approve/Reject for the new time",
          pend.status == "pending" and owner_msgs and (tname(owner_msgs[0]) == "new_booking_request"
          or owner_msgs[0]["payload"]["buttons"][0]["id"] == f"confirm_{pend.id}"), out)

    # owner portal: Change time (cookie for Glow Salon = business A is still set)
    oday = business_today(A) + timedelta(days=5)
    om = Appointment(business_id=A.id, service_id=sa.id, customer_phone="912400000001", customer_name="Nisha",
                     appointment_time=_dt.combine(oday, _dt.strptime("10:00", "%H:%M").time()), status="confirmed")
    blocker = Appointment(business_id=A.id, service_id=sa.id, customer_phone="912400000002", customer_name="Blocker",
                          appointment_time=_dt.combine(oday, _dt.strptime("15:00", "%H:%M").time()), status="confirmed")
    db.add_all([om, blocker]); db.commit()
    r = client.get(f"/owner/appointments/{om.id}/move")
    check("owner 'Change time' page shows the booking and free times",
          r.status_code == 200 and "Booking #" in r.text and "'11:00'" in r.text and "11:00 AM" in r.text, r.status_code)
    r = client.get("/owner/appointments")
    check("appointments list has a Change time link", f"/owner/appointments/{om.id}/move" in r.text)
    outbox.clear()
    r = client.post(f"/owner/appointments/{om.id}/move", data={"date": oday.isoformat(), "time": "15:00"}, follow_redirects=False)
    db.refresh(om)
    check("moving onto a full time -> form error, booking unchanged", r.status_code == 400 and "fully booked" in r.text
          and om.appointment_time.strftime("%H:%M") == "10:00", r.status_code)
    outbox.clear()
    r = client.post(f"/owner/appointments/{om.id}/move", data={"date": oday.isoformat(), "time": "11:30"}, follow_redirects=False)
    db.refresh(om)
    check("owner moves booking -> new time saved, status kept", r.status_code == 303
          and om.appointment_time.strftime("%H:%M") == "11:30" and om.status == "confirmed", (r.status_code, om.appointment_time))
    msg = [m for m in outbox if m["to"] == "912400000001"]
    check("...customer gets 'moved' (template, as they never messaged), not 'cancelled'",
          msg and tname(msg[0]) == "booking_rescheduled", outbox)
    outbox.clear()
    r = client.post(f"/owner/appointments/{om.id}/move", data={"date": oday.isoformat(), "time": "15:00", "allow_overbook": "on"}, follow_redirects=False)
    db.refresh(om)
    check("override lets the owner double up deliberately", r.status_code == 303 and om.appointment_time.strftime("%H:%M") == "15:00")

    db.close()

from database import engine as _engine
_engine.dispose()                # release the file so Windows can delete it
try:
    os.remove(tmp.name)
except OSError:
    pass
print(f"\n{'ALL PASSED' if fails == 0 else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
