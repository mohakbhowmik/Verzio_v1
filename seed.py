"""
seed.py — DEVELOPMENT ONLY.
Wipes EVERY tenant's data and creates one test business.
Refuses to run when VERZIO_ENV=production, and asks for confirmation otherwise
(pass --yes to skip the prompt in local scripts).
"""
# --- Production guard: runs before any database import --------------------
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # so VERZIO_ENV set only in .env is also respected

if os.getenv("VERZIO_ENV", "development").strip().lower() == "production":
    sys.exit("REFUSING TO RUN: seed.py wipes ALL tenant data and VERZIO_ENV=production.")

if "--yes" not in sys.argv:
    target = os.getenv("DATABASE_URL", "sqlite:///./verzio_saas.db")
    try:
        answer = input(f"This deletes ALL businesses, owners, services and appointments in {target}.\n"
                       f"Type WIPE to continue: ")
    except EOFError:
        answer = ""
    if answer.strip() != "WIPE":
        sys.exit("Aborted. Nothing was changed.")
# ---------------------------------------------------------------------------

import secrets

from sqlalchemy import text

from database import (
    ActivityEvent,
    Appointment,
    Business,
    Incident,
    Owner,
    PaymentRecord,
    Service,
    SessionLocal,
    Staff,
    Subscription,
    UserSession,
    init_db,
)
from owner.owner_auth import hash_password

# Values come from the environment so no real numbers live in the repo.
SEED_PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "TEST_PHONE_NUMBER_ID")
SEED_MANAGER_PHONE = os.getenv("SEED_MANAGER_PHONE", "910000000001")
SEED_OWNER_NAME = os.getenv("SEED_OWNER_NAME", "Test Owner")
SEED_OWNER_EMAIL = os.getenv("SEED_OWNER_EMAIL", "owner@verzio.test")
SEED_OWNER_PASSWORD = os.getenv("SEED_OWNER_PASSWORD") or secrets.token_urlsafe(9)

init_db()
db = SessionLocal()

try:
    print("🧹 Purging all tenant data...")
    for model in (Appointment, UserSession, Service, Staff, Owner,
                  Subscription, PaymentRecord, ActivityEvent, Incident, Business):
        db.query(model).delete()
    try:
        db.execute(text("DELETE FROM processed_messages"))
    except Exception:
        db.rollback()  # table is created by the server on first start
    db.commit()

    print("🌱 Seeding test business...")
    business = Business(
        name="Verzio Test Salon",
        whatsapp_business_phone_number_id=SEED_PHONE_NUMBER_ID,
        manager_phone_number=SEED_MANAGER_PHONE,
        timezone="Asia/Kolkata",
        is_active=True,
        accepting_bookings=True,
        operational_hours={
            "mon": ["09:00", "18:00"],
            "tue": ["09:00", "18:00"],
            "wed": ["09:00", "18:00"],
            "thu": ["09:00", "18:00"],
            "fri": ["09:00", "18:00"],
            "sat": ["10:00", "15:00"],
        },
        holidays=[],
        slot_interval=30,
        advance_booking_days=14,
        enable_service_selection=True,
        approval_mode="manual",
        max_parallel_bookings=2,
        notification_preferences={"whatsapp_owner": True, "whatsapp_customer": True},
    )
    db.add(business)
    db.commit()
    db.refresh(business)

    db.add(Owner(
        business_id=business.id,
        full_name=SEED_OWNER_NAME,
        email=SEED_OWNER_EMAIL,
        phone_number=SEED_MANAGER_PHONE,
        password_hash=hash_password(SEED_OWNER_PASSWORD),
        is_active=True,
    ))

    db.add_all([
        Service(business_id=business.id, name="Haircut", duration=45, price=500.0, is_active=True),
        Service(business_id=business.id, name="Beard Trim", duration=30, price=250.0, is_active=True),
        Service(business_id=business.id, name="Hair Spa", duration=60, price=1200.0, is_active=True),
        Service(business_id=business.id, name="Hair Colour", duration=90, price=2500.0, is_active=True),
    ])
    db.commit()

    print(f"✅ Seeded '{business.name}' (phone_number_id={SEED_PHONE_NUMBER_ID}).")
    if SEED_PHONE_NUMBER_ID == "TEST_PHONE_NUMBER_ID":
        print("⚠️  PHONE_NUMBER_ID is not set: WhatsApp messages won't reach this business.")
finally:
    db.close()

print()
print("========== OWNER LOGIN ==========")
print(f"Email    : {SEED_OWNER_EMAIL}")
print(f"Password : {SEED_OWNER_PASSWORD}")
print("=================================")
