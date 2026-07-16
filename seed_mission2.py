from database import SessionLocal, Business, Service, Staff, init_db
from booking_engine import validate_and_book
from datetime import datetime, timedelta

def run_test():
    init_db()
    db = SessionLocal()
    
    # 1. Create Salon Alpha (Manual Approval, 7 day advance)
    alpha = Business(
        name="Salon Alpha",
        whatsapp_business_phone_number_id="ALPHA_ID",
        manager_phone_number="123",
        operational_hours={}, 
        advance_booking_days=7,
        approval_mode="manual"
    )
    db.add(alpha)
    db.flush()
    
    # 2. Create Clinic Beta (Automatic Approval, 30 day advance)
    beta = Business(
        name="Clinic Beta",
        whatsapp_business_phone_number_id="BETA_ID",
        manager_phone_number="456",
        operational_hours={},
        advance_booking_days=30,
        approval_mode="automatic"
    )
    db.add(beta)
    db.commit()

    print("Test 1: Isolation Check")
    target = datetime.now() + timedelta(days=2)
    appt_alpha = validate_and_book(db, "ALPHA_ID", target, "999")
    appt_beta = validate_and_book(db, "BETA_ID", target, "888")
    
    print(f"Alpha Appt Status: {appt_alpha.status} (Expected: pending)")
    print(f"Beta Appt Status: {appt_beta.status} (Expected: confirmed)")
    
    print("\nTest 2: Config-driven Rule Check")
    future_date = datetime.now() + timedelta(days=10)
    try:
        validate_and_book(db, "ALPHA_ID", future_date, "999")
    except Exception as e:
        print(f"Alpha rejected 10-day booking (Expected: True). Error: {e}")

if __name__ == "__main__":
    run_test()