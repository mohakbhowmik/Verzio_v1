import logging
from datetime import datetime, timedelta, time as dt_time
from sqlalchemy.orm import Session
from database import Business, Appointment, Service

logger = logging.getLogger("VERZIO_ENGINE")

class BookingEngineException(Exception):
    def __init__(self, message: str, error_code: str):
        super().__init__(message)
        self.error_code = error_code

class VerzioSaaSEngine:
    def get_tenant_config(self, db: Session, tenant_id: str) -> Business:
        biz = db.query(Business).filter(Business.whatsapp_business_phone_number_id == tenant_id).first()
        if not biz or not biz.is_active:
            raise BookingEngineException("Business inactive or not found.", "ERR_TENANT_LOCKED")
        return biz

    def get_available_dates(self, db: Session, biz: Business) -> list:
        available_dates = []
        today = datetime.now().date()
        for i in range(biz.advance_booking_days):
            target = today + timedelta(days=i)
            if target.isoformat() not in (biz.holidays or []):
                available_dates.append(target)
        return available_dates[:10]

    def get_available_slots(self, db: Session, biz: Business, target_date: datetime) -> list:
        day_name = target_date.strftime("%a").lower()[:3]
        hours = biz.operational_hours.get(day_name)
        if not hours: return []

        start_h, end_h = hours
        start_time = datetime.strptime(start_h, "%H:%M").time()
        end_time = datetime.strptime(end_h, "%H:%M").time()

        existing = db.query(Appointment.appointment_time).filter(
            Appointment.business_id == biz.id,
            Appointment.status.in_(["pending", "confirmed"])
        ).all()
        booked = [b[0].strftime("%H:%M") for b in existing if b[0].date() == target_date.date()]

        slots = []
        curr = datetime.combine(target_date.date(), start_time)
        limit = datetime.combine(target_date.date(), end_time)
        while curr < limit:
            s_str = curr.strftime("%H:%M")
            if s_str not in booked:
                slots.append(s_str)
            curr += timedelta(minutes=biz.slot_interval)
        return slots

    def validate_and_book(self, db: Session, tenant_id: str, target_dt: datetime, customer_phone: str, service_id: int) -> Appointment:
        biz = self.get_tenant_config(db, tenant_id)
        
        collision = db.query(Appointment).filter(
            Appointment.business_id == biz.id,
            Appointment.appointment_time == target_dt,
            Appointment.status.in_(["pending", "confirmed"])
        ).first()
        if collision:
            raise BookingEngineException("Slot taken.", "ERR_COLLISION")

        status = "confirmed" if biz.approval_mode == "automatic" else "pending"
        new_appt = Appointment(
            business_id=biz.id, service_id=service_id,
            customer_phone=customer_phone, appointment_time=target_dt, status=status
        )
        db.add(new_appt)
        db.commit()
        db.refresh(new_appt)
        return new_appt

def validate_and_book(db, tenant_id, target_dt, customer_phone, service_id):
    return VerzioSaaSEngine().validate_and_book(db, tenant_id, target_dt, customer_phone, service_id)