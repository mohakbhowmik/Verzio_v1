"""
seed_demo_data.py — DEVELOPMENT / SALES DEMO ONLY.
Creates (or refreshes) one demo tenant, "Aura Aesthetic Clinic", with realistic
services, hours and a few days of bookings. It only touches the demo tenant's
own rows; other tenants are never modified.

Refuses to run when VERZIO_ENV=production.

For a live WhatsApp demo, set DEMO_PHONE_NUMBER_ID to a real test number's
phone_number_id that no other tenant uses. Without it, the demo works in the
owner portal only.
"""
# --- Production guard: runs before any database import --------------------
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # so VERZIO_ENV set only in .env is also respected

if os.getenv("VERZIO_ENV", "development").strip().lower() == "production":
    sys.exit("REFUSING TO RUN: seed_demo_data.py is for development and demos only (VERZIO_ENV=production).")
# ---------------------------------------------------------------------------

import secrets
from datetime import datetime, time, timedelta

import pytz

from database import (
    ActivityEvent,
    Appointment,
    Business,
    Owner,
    Service,
    SessionLocal,
    Subscription,
    UserSession,
    init_db,
)
from owner.owner_auth import hash_password

DEMO_NAME = "Aura Aesthetic Clinic"
DEMO_PHONE_NUMBER_ID = os.getenv("DEMO_PHONE_NUMBER_ID", "DEMO_AURA_CLINIC")
DEMO_MANAGER_PHONE = os.getenv("DEMO_MANAGER_PHONE", "910000000099")
DEMO_OWNER_NAME = os.getenv("DEMO_OWNER_NAME", "Dr. Ananya Rao")
DEMO_OWNER_EMAIL = os.getenv("DEMO_OWNER_EMAIL", "demo@aura-clinic.test")
DEMO_OWNER_PASSWORD = os.getenv("DEMO_OWNER_PASSWORD") or secrets.token_urlsafe(9)
TIMEZONE = "Asia/Kolkata"

# Mon–Sat 10:00–20:00, Sun 11:00–17:00
HOURS = {d: ["10:00", "20:00"] for d in ("mon", "tue", "wed", "thu", "fri", "sat")}
HOURS["sun"] = ["11:00", "17:00"]

SERVICES = {
    "consult":   ("Skin Consultation", 30, 1500.0),
    "hydra":     ("HydraFacial", 60, 4500.0),
    "peel":      ("Chemical Peel", 45, 3500.0),
    "laser":     ("Laser Hair Reduction", 45, 3000.0),
    "micro":     ("Microneedling", 60, 5000.0),
    "prp":       ("PRP Hair Therapy", 60, 6000.0),
    "antiage":   ("Anti-Ageing Consultation", 30, 2000.0),
}

# (day offset from today, start time, service key, customer name)
# Times sit inside both weekday and Sunday hours, are 90 min apart and every
# service is <= 60 min, so bookings never overlap and never exceed capacity.
BOOKINGS = [
    (-2, "11:00", "hydra",   "Meera Iyer"),
    (-2, "12:30", "consult", "Rohan Mehta"),
    (-2, "14:00", "peel",    "Sneha Kulkarni"),
    (-1, "11:00", "laser",   "Ishita Jain"),
    (-1, "12:30", "micro",   "Kabir Das"),
    (-1, "15:30", "consult", "Pooja Nair"),
    (0,  "11:00", "consult", "Priya Patel"),
    (0,  "12:30", "hydra",   "Simran Kaur"),
    (0,  "14:00", "prp",     "Aditya Rao"),
    (0,  "15:30", "antiage", "Nikita Roy"),
    (1,  "11:00", "peel",    "Riya Bose"),
    (1,  "14:00", "hydra",   "Ananya Gupta"),
    (2,  "12:30", "laser",   "Vikram Joshi"),
    (2,  "15:30", "consult", "Neha Singh"),
    (3,  "11:00", "micro",   "Harsh Kapoor"),
    (4,  "14:00", "antiage", "Karan Shah"),
]

PAST_STATUSES = ["completed", "completed", "completed", "no_show", "completed", "cancelled"]
FUTURE_STATUSES = ["confirmed", "pending", "confirmed", "confirmed", "pending"]

init_db()
db = SessionLocal()

try:
    business = (
        db.query(Business)
        .filter(Business.whatsapp_business_phone_number_id == DEMO_PHONE_NUMBER_ID)
        .first()
    )
    if business and business.name != DEMO_NAME:
        sys.exit(
            f"REFUSING TO RUN: phone_number_id {DEMO_PHONE_NUMBER_ID} already belongs to "
            f"'{business.name}'. Use a different DEMO_PHONE_NUMBER_ID."
        )

    clashing_owner = db.query(Owner).filter(Owner.email == DEMO_OWNER_EMAIL).first()
    if clashing_owner and (business is None or clashing_owner.business_id != business.id):
        sys.exit(f"REFUSING TO RUN: {DEMO_OWNER_EMAIL} already belongs to another business. "
                 f"Set a different DEMO_OWNER_EMAIL.")

    if business:
        print(f"♻️  Refreshing existing demo tenant '{DEMO_NAME}' (id={business.id})...")
        for model in (Appointment, UserSession, Service, Owner, ActivityEvent):
            db.query(model).filter(model.business_id == business.id).delete()
        db.commit()
    else:
        print(f"🌱 Creating demo tenant '{DEMO_NAME}'...")
        business = Business(name=DEMO_NAME, whatsapp_business_phone_number_id=DEMO_PHONE_NUMBER_ID,
                            manager_phone_number=DEMO_MANAGER_PHONE, operational_hours=HOURS)
        db.add(business)
        db.flush()

    business.manager_phone_number = DEMO_MANAGER_PHONE
    business.timezone = TIMEZONE
    business.is_active = True
    business.accepting_bookings = True
    business.operational_hours = HOURS
    business.holidays = []
    business.slot_interval = 30
    business.advance_booking_days = 14
    business.enable_service_selection = True
    business.approval_mode = "manual"
    business.max_parallel_bookings = 2
    business.notification_preferences = {"whatsapp_owner": True, "whatsapp_customer": True}
    db.commit()
    db.refresh(business)

    db.add(Owner(
        business_id=business.id,
        full_name=DEMO_OWNER_NAME,
        email=DEMO_OWNER_EMAIL,
        phone_number=None,  # login by email; avoids clashing with other owners' phones
        password_hash=hash_password(DEMO_OWNER_PASSWORD),
        is_active=True,
    ))

    services = {}
    for key, (name, minutes, price) in SERVICES.items():
        svc = Service(business_id=business.id, name=name, duration=minutes, price=price, is_active=True)
        db.add(svc)
        services[key] = svc
    db.flush()

    now_local = datetime.now(pytz.timezone(TIMEZONE)).replace(tzinfo=None)
    today = now_local.date()
    past_i = future_i = 0

    for i, (offset, hhmm, svc_key, customer) in enumerate(BOOKINGS):
        start = datetime.combine(today + timedelta(days=offset), time.fromisoformat(hhmm))
        if start < now_local:
            status = PAST_STATUSES[past_i % len(PAST_STATUSES)]
            past_i += 1
        else:
            status = FUTURE_STATUSES[future_i % len(FUTURE_STATUSES)]
            future_i += 1

        db.add(Appointment(
            business_id=business.id,
            service_id=services[svc_key].id,
            customer_name=customer,
            customer_phone=f"91000000{1000 + i}",  # deliberately invalid dummy numbers
            appointment_time=start,
            status=status,
        ))

    subscription = db.query(Subscription).filter(Subscription.business_id == business.id).first()
    if not subscription:
        subscription = Subscription(business_id=business.id)
        db.add(subscription)
    subscription.plan = "pro"
    subscription.status = "active"
    subscription.next_billing_date = (today + timedelta(days=30)).isoformat()

    db.commit()
    print(f"✅ '{DEMO_NAME}' ready: {len(SERVICES)} services, {len(BOOKINGS)} demo bookings.")
    if DEMO_PHONE_NUMBER_ID == "DEMO_AURA_CLINIC":
        print("ℹ️  DEMO_PHONE_NUMBER_ID not set: portal demo only (no live WhatsApp).")
finally:
    db.close()

print()
print("========== DEMO OWNER LOGIN ==========")
print(f"Email    : {DEMO_OWNER_EMAIL}")
print(f"Password : {DEMO_OWNER_PASSWORD}")
print("======================================")
