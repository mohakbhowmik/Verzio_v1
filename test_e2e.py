"""
test_e2e.py — simulated WhatsApp conversations through the real /webhook route,
plus the owner portal walk-in and reject flows. Uses a throwaway database and
stubs out outbound WhatsApp, so it is safe to run anytime:

    python test_e2e.py
"""
import os, sys, json, hmac, hashlib, tempfile, itertools
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
os.environ.update(DATABASE_URL=f"sqlite:///{tmp.name}", META_APP_SECRET="test-secret", META_ACCESS_TOKEN="x")
sys.path.insert(0, os.getcwd())
from datetime import timedelta
from fastapi.testclient import TestClient
import server, whatsapp_client, owner.owner_appointments as oa
from database import SessionLocal, Business, Service, Appointment, UserSession
from booking_engine import business_today

outbox = []
async def fake_send(to, pnid, payload, business_id=None):
    outbox.append({"to": to, "from": pnid, "payload": payload}); return True
server.send_whatsapp = fake_send; oa.send_whatsapp = fake_send

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
    check("confirm -> owner gets Approve/Reject from tenant A", to_owner and to_owner[0]["payload"]["type"] == "buttons"
          and to_owner[0]["from"] == "PN_A", out)
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
    r, out = webhook(client, "PN_A", "910000000001", btn(f"confirm_{appt.id}"))
    db.refresh(appt)
    check("manager Approve -> confirmed", appt.status == "confirmed")
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
    check("auto-confirm business -> 'You're booked' + owner FYI (no buttons)",
          any(m["to"] == P and "You're booked" in m["payload"]["body"] for m in out)
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
    check("portal Reject -> cancelled + customer messaged", r.status_code == 303 and pend.status == "cancelled"
          and any(m["to"] == "915555555555" and "couldn't confirm" in m["payload"]["body"] for m in outbox), outbox)
    db.close()

os.remove(tmp.name)
print(f"\n{'ALL PASSED' if fails == 0 else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
