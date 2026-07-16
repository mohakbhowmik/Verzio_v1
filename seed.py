"""
================================================================================
VERZIO STUDIO — REPO TESTING SEED SCRIPT (MISSION 2 FINAL FIX)
================================================================================
"""
from database import SessionLocal, Business, Appointment, UserSession, Service, init_db
from datetime import datetime, timedelta

# Initialize Schema
init_db()
db = SessionLocal()

try:
    print("🧹 Purging old stale data rows...")
    db.query(Appointment).delete()
    db.query(UserSession).delete()
    db.query(Service).delete()
    db.query(Business).delete()
    db.commit()

    # 1. Meta Identifiers
    REAL_META_PHONE_NUMBER_ID = "1265387899980753" 
    YOUR_PERSONAL_PHONE = "918208559570" 

    print("🌱 Seeding active test salon profile with full configuration...")
    
    # Define realistic operational hours
    ops_hours = {
        "mon": ["09:00", "18:00"],
        "tue": ["09:00", "18:00"],
        "wed": ["09:00", "18:00"],
        "thu": ["09:00", "18:00"],
        "fri": ["09:00", "18:00"],
        "sat": ["10:00", "15:00"]
        # Sunday omitted (Closed)
    }

    new_business = Business(
        name="Cozmo Salon Pune",
        whatsapp_business_phone_number_id=REAL_META_PHONE_NUMBER_ID,
        manager_phone_number=YOUR_PERSONAL_PHONE,
        is_active=True,
        timezone="Asia/Kolkata",
        
        # Configuration Block
        operational_hours=ops_hours,
        holidays=[],
        slot_interval=30,
        buffer_minutes=10,
        advance_booking_days=14,
        approval_mode="manual",
        notification_preferences={}
    )
    db.add(new_business)
    db.commit()
    db.refresh(new_business)

    print("✂️ Seeding active services for the Booking Runtime...")
    test_service = Service(
        business_id=new_business.id,
        name="Haircut & Styling",
        duration=30,
        price=50.0,
        is_active=True
    )
    db.add(test_service)
    db.commit()

    # DYNAMIC TIME: 2 hours from now
    target_test_time = datetime.now() + timedelta(hours=2)
    target_test_time = target_test_time.replace(second=0, microsecond=0)

    print(f"📅 Seeding a pending slot for: {target_test_time.strftime('%Y-%m-%d %I:%M %p')}")
    mock_appt = Appointment(
        business_id=new_business.id,
        service_id=test_service.id,
        customer_phone=YOUR_PERSONAL_PHONE,
        customer_name="Mohak",
        appointment_time=target_test_time,
        status="pending"
    )
    
    db.add(mock_appt)
    db.commit()
    
    print("\n✅ Database fixed and successfully seeded!")
    print(f"Business ID: {new_business.id}")
    print(f"Service ID: {test_service.id}")

finally:
    db.close()