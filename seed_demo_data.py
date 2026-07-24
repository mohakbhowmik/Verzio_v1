from datetime import datetime, timedelta
import random

from database import (
    SessionLocal,
    Business,
    Service,
    Appointment,
)

db = SessionLocal()

try:
    business = db.query(Business).first()

    if not business:
        raise Exception("No business found. Run seed.py first.")

    services = (
        db.query(Service)
        .filter(Service.business_id == business.id)
        .all()
    )

    if not services:
        raise Exception("No services found. Run seed.py first.")

    print("🧹 Removing existing appointments...")
    db.query(Appointment).delete()
    db.commit()

    print("🌱 Creating demo appointments...")

    names = [
        "Aarav Sharma",
        "Priya Patel",
        "Rohan Mehta",
        "Neha Singh",
        "Ananya Gupta",
        "Vikram Joshi",
        "Sneha Kulkarni",
        "Rahul Verma",
        "Ishita Jain",
        "Karan Shah",
        "Dev Malhotra",
        "Pooja Nair",
        "Aryan Desai",
        "Simran Kaur",
        "Aditya Rao",
        "Nikita Roy",
        "Harsh Kapoor",
        "Meera Iyer",
        "Kabir Das",
        "Riya Bose",
    ]

    statuses = (
        ["pending"] * 3 +
        ["confirmed"] * 5 +
        ["completed"] * 8 +
        ["cancelled"] * 3 +
        ["no_show"] * 1
    )

    random.shuffle(statuses)

    base = datetime.now()

    for i, name in enumerate(names):

        appointment = Appointment(
            business_id=business.id,
            service_id=random.choice(services).id,
            customer_name=name,
            customer_phone=f"9198000{10000+i}",
            appointment_time=base + timedelta(
                days=random.randint(-3, 7),
                hours=random.randint(9, 18),
            ),
            status=statuses[i],
        )

        db.add(appointment)

    db.commit()

    print(f"✅ Created {len(names)} demo appointments.")

finally:
    db.close()