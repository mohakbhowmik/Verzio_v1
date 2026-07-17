from database import SessionLocal, Business, Service, Appointment, UserSession, init_db
from datetime import datetime

init_db()
db = SessionLocal()

try:
    print("🧹 Purging old data...")

    db.query(Appointment).delete()
    db.query(UserSession).delete()
    db.query(Service).delete()
    db.query(Business).delete()
    db.commit()

    print("🌱 Seeding Verzio Business...")

    biz_config = {
        "operational_hours": {
            "mon": ["09:00", "18:00"],
            "tue": ["09:00", "18:00"],
            "wed": ["09:00", "18:00"],
            "thu": ["09:00", "18:00"],
            "fri": ["09:00", "18:00"],
            "sat": ["10:00", "15:00"]
        },
        "holidays": [],
        "slot_interval": 60,
        "advance_booking_days": 14,
        "approval_mode": "manual",
        "accepting_bookings": True,
        "max_parallel_bookings": 2,
        "notification_preferences": {
            "whatsapp_owner": True,
            "whatsapp_customer": True
        }
    }

    new_business = Business(
        name="Verzio Parallel Salon",
        whatsapp_business_phone_number_id="1265387899980753",
        manager_phone_number="918208559570",
        timezone="Asia/Kolkata",
        is_active=True,
        **biz_config
    )

    db.add(new_business)
    db.commit()
    db.refresh(new_business)

    services = [
        Service(
            business_id=new_business.id,
            name="Haircut",
            duration=60,
            price=300.0,
            is_active=True
        ),
        Service(
            business_id=new_business.id,
            name="Beard Trim",
            duration=60,
            price=150.0,
            is_active=True
        ),
        Service(
            business_id=new_business.id,
            name="Hair Spa",
            duration=60,
            price=800.0,
            is_active=True
        )
    ]

    db.add_all(services)
    db.commit()

    print(f"✅ Seeded '{new_business.name}' successfully.")
    print(f"📅 Slot Interval: {biz_config['slot_interval']} minutes")
    print(f"👥 Online Capacity: {biz_config['max_parallel_bookings']} concurrent bookings")
    print(f"📆 Advance Booking Window: {biz_config['advance_booking_days']} days")

finally:
    db.close()