"""
test_pages.py — renders every admin and owner page with sample data and checks
each loads with the RIGHT layout (admin pages must never show the owner portal
and vice versa) and its key content. Throwaway database; safe to run anytime:

    python test_pages.py
"""
import os, tempfile
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
from cryptography.fernet import Fernet
os.environ.update(
    SCHEDULER="off",DATABASE_URL=f"sqlite:///{tmp.name}", META_APP_SECRET="s", META_ACCESS_TOKEN="x", META_APP_ID="1",
    META_ES_CONFIG_ID="c", TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(), VERZIO_ENV="development",
    VERZIO_SECRET_KEY="test-only-secret-key-not-for-production-use", ADMIN_USERNAME="admin",
    ADMIN_PASSWORD="verzio-dev-admin", COOKIE_SECURE="false")
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
import server
from database import SessionLocal, Business, Service, Appointment, Owner, ActivityEvent
from owner.owner_auth import hash_password
ALL = {d: ["09:00", "18:00"] for d in ["mon","tue","wed","thu","fri","sat","sun"]}
A = ("admin", "verzio-dev-admin")
fails = 0
def check(label, ok, extra=""):
    global fails; fails += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if not ok and extra else ""))
ADMIN_MARK, OWNER_MARK = 'href="/admin/businesses"', "OWNER PORTAL"
with TestClient(server.app) as c:
    db = SessionLocal()
    b = Business(name="Glow Salon", whatsapp_business_phone_number_id="PN1", manager_phone_number="919800000001",
                 timezone="Asia/Kolkata", operational_hours=ALL, holidays=[], slot_interval=30, max_parallel_bookings=1,
                 approval_mode="manual", is_active=True, accepting_bookings=True, enable_service_selection=True,
                 advance_booking_days=14, notification_preferences={})
    paused = Business(name="Paused Spa", whatsapp_business_phone_number_id="PN2", manager_phone_number="919800000002",
                 timezone="Asia/Kolkata", operational_hours=ALL, holidays=[], is_active=True, accepting_bookings=False)
    db.add_all([b, paused]); db.commit()
    s = Service(business_id=b.id, name="Haircut", duration=30, price=500, is_active=True); db.add(s); db.commit()
    db.add(Appointment(business_id=b.id, service_id=s.id, customer_phone="919000000001", customer_name="Riya",
                       appointment_time=datetime.now() + timedelta(days=1), status="pending"))
    db.add(Owner(business_id=b.id, full_name="Glow Owner", email="o@glow.test", phone_number="919800000001",
                 password_hash=hash_password("ownerpass123"), is_active=True))
    db.add(ActivityEvent(event_type="webhook_failed", status="error", message="test failure", business_id=b.id))
    db.add(ActivityEvent(event_type="booking_confirmed", status="success", message="Appointment #1 confirmed", business_id=b.id))
    db.commit()

    admin_pages = {
        "/admin": ["Paused Spa", "test failure", "Businesses needing attention"],
        "/admin/businesses": ["Glow Salon", "Add business", "WhatsApp"],
        "/admin/businesses/new": ["<form", 'name="break_start"'],
        f"/admin/businesses/{b.id}/edit": ["Glow Salon"],
        "/admin/services": ["Haircut"],
        "/admin/services/new": ['name="business_id"', "Glow Salon"],
        f"/admin/services/{s.id}/edit": ['name="business_id"', "Haircut"],
        "/admin/appointments": ["919000000001"],
        "/admin/subscriptions": [""],
        "/admin/system": [""],
        f"/admin/whatsapp/{b.id}": ["Create onboarding link"],
    }
    for path, must in admin_pages.items():
        r = c.get(path, auth=A)
        ok = r.status_code == 200 and ADMIN_MARK in r.text and OWNER_MARK not in r.text and all(m in r.text for m in must if m)
        missing = [m for m in must if m and m not in r.text]
        check(f"admin {path:32} admin layout, key content", ok,
              f"status={r.status_code} admin_nav={ADMIN_MARK in r.text} owner_layout={OWNER_MARK in r.text} missing={missing}")
    r = c.post("/admin/services", auth=A, data={"name": "Beard Trim", "business_id": str(b.id), "duration": "20", "price": "200"},
               follow_redirects=False)
    check("admin can create a service", r.status_code in (302, 303) and db.query(Service).filter_by(name="Beard Trim").count() == 1,
          r.status_code)
    r = c.post("/admin/businesses/new", auth=A, data={}, follow_redirects=False)
    # owner side
    r = c.post("/owner/login", data={"identifier": "o@glow.test", "password": "ownerpass123"}, follow_redirects=False)
    check("owner login works", r.status_code == 303 and "verzio_owner_session" in r.headers.get("set-cookie", ""), r.status_code)
    c.cookies.set("verzio_owner_session", r.cookies.get("verzio_owner_session"))
    for path in ["/owner/dashboard", "/owner/appointments", "/owner/appointments/new", "/owner/services",
                 "/owner/services/new", f"/owner/services/{s.id}/edit", "/owner/reports", "/owner/settings"]:
        r = c.get(path)
        check(f"owner {path:32} loads (no admin nav)", r.status_code == 200 and ADMIN_MARK not in r.text, r.status_code)
    check("uptime monitors: HEAD /health -> 200", c.head("/health").status_code == 200)
    r = c.get("/owner/settings")
    check("owner settings has the daily break fields", 'name="break_start"' in r.text and 'name="break_end"' in r.text)
    r = c.get("/owner/settings", headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile"})
    check("owner settings (phone) has the daily break fields", r.status_code == 200 and 'name="break_start"' in r.text)
    form = {"business_name": "Glow Salon", "phone_number": "98000 11111", "timezone": "Asia/Kolkata",
            "accept_online_bookings": "on", "slot_duration": "30", "hours_mon_enabled": "on",
            "hours_mon_open": "10:00", "hours_mon_close": "19:00", "break_start": "13:30", "break_end": "14:30"}
    r = c.post("/owner/settings", data=form)
    db.expire_all(); saved = db.get(Business, b.id)
    check("owner saves a break + a 10-digit number", r.status_code == 200 and saved.operational_hours.get("mon") == ["10:00", "19:00", "13:30", "14:30"]
          and saved.manager_phone_number == "919800011111", (saved.operational_hours, saved.manager_phone_number))
    db.close()
from database import engine; engine.dispose()
try:
    os.remove(tmp.name)
except OSError:
    pass
print("\nALL PASSED" if fails == 0 else f"\n{fails} FAILED")
raise SystemExit(1 if fails else 0)
