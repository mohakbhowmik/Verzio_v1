import logging
from datetime import datetime
from sqlalchemy.orm import Session
from database import Business, Appointment

logger = logging.getLogger("VERZIO_ENGINE")

class BookingEngineException(Exception):
    def __init__(self, message: str, error_code: str):
        super().__init__(message)
        self.error_code = error_code

class VerzioSaaSEngine:
    def audit_tenant(self, db: Session, tenant_id: str) -> Business:
        business = db.query(Business).filter(
            Business.whatsapp_business_phone_number_id == tenant_id
        ).first()

        if not business:
            logger.error(f"Tenant {tenant_id} not found.")
            raise BookingEngineException("Business not registered.", "ERR_TENANT_NOT_FOUND")

        if not business.is_active:
            raise BookingEngineException("Account suspended.", "ERR_BILLING_SUSPENSION")

        if business.billing_expiry and business.billing_expiry < datetime.now():
            business.is_active = False
            db.commit()
            logger.warning(f"Billing expired for {business.name}")
            raise BookingEngineException("Automation paused due to expiry.", "ERR_BILLING_EXPIRED")

        return business

def process_persistent_booking(
    db: Session,
    tenant_id: str,
    raw_date: str,
    raw_time: str,
    customer_phone: str
) -> Appointment:
    engine = VerzioSaaSEngine()
    business = engine.audit_tenant(db, tenant_id)

    try:
        target_dt = datetime.strptime(f"{raw_date} {raw_time}", "%Y-%m-%d %I:%M %p")
    except ValueError:
        raise BookingEngineException("Invalid date/time format.", "ERR_BAD_INPUT")

    # Validate hours
    if target_dt.strftime("%I:%M %p") not in business.operational_hours:
        raise BookingEngineException("Time slot unavailable.", "ERR_OUT_OF_HOURS")

    # Collision check
    existing = db.query(Appointment).filter(
        Appointment.business_id == business.id,
        Appointment.appointment_time == target_dt,
        Appointment.status != "cancelled"
    ).first()

    if existing:
        raise BookingEngineException("Slot already booked.", "ERR_SLOT_COLLISION")

    new_appt = Appointment(
        business_id=business.id,
        customer_phone=customer_phone,
        appointment_time=target_dt,
        status="pending"
    )
    db.add(new_appt)
    db.commit()
    db.refresh(new_appt)
    
    logger.info(f"New pending booking: {new_appt.id} for tenant {tenant_id}")
    return new_appt