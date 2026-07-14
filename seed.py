"""
================================================================================
VERZIO STUDIO — REPO TESTING SEED SCRIPT (FORCE OVERWRITE)
================================================================================
"""
from database import SessionLocal, Business, Appointment, init_db
from datetime import datetime, timedelta

init_db()
db = SessionLocal()

try:
    print("🧹 Purging old stale data rows to prevent routing collision...")
    db.query(Appointment).delete()
    db.query(Business).delete()
    db.commit()

    # 1. Your 15-Digit Meta Identifiers
    REAL_META_PHONE_NUMBER_ID = "1265387899980753" 
    
    # 2. Target handset for delivery testing
    YOUR_PERSONAL_PHONE = "918208559570" 

    print("🌱 Seeding active test salon profile with live credentials...")
    new_business = Business(
        name="Cozmo Salon Pune",
        whatsapp_business_phone_number_id=REAL_META_PHONE_NUMBER_ID,
        manager_phone_number=YOUR_PERSONAL_PHONE,
        is_active=True
    )
    db.add(new_business)
    db.commit()
    db.refresh(new_business)
    
    # DYNAMIC TIME BUG FIX: Ensure the target appointment time is always set to
    # 2 hours from RIGHT NOW on the exact current day, eliminating date-boundary drops.
    target_test_time = datetime.now() + timedelta(hours=2)
    target_test_time = target_test_time.replace(second=0, microsecond=0)

    print(f"📅 Seeding a pending slot targeting your handset for: {target_test_time.strftime('%Y-%m-%d %I:%M %p')}")
    mock_appt = Appointment(
        business_id=new_business.id,
        customer_phone=YOUR_PERSONAL_PHONE,
        customer_name="Mohak",
        appointment_time=target_test_time,
        status="pending"
    )
    
    db.add(mock_appt)
    db.commit()
    print("\n✅ Database successfully overwritten and refreshed! Try running your morning pulse now.")

finally:
    db.close()