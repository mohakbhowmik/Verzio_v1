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
        print("=" * 60)
        print("TENANT ID RECEIVED:", repr(tenant_id))
        print("TYPE:", type(tenant_id))
        print("=" * 60)

        biz = db.query(Business).filter(
            Business.whatsapp_business_phone_number_id == tenant_id
        ).first()

        print("BUSINESS FOUND:", biz)

        if not biz:
            raise BookingEngineException("Business not found.", "ERR_NOT_FOUND")

        if not biz.is_active or not biz.accepting_bookings:
            raise BookingEngineException(
                "Business is not currently accepting bookings.",
                "ERR_TENANT_LOCKED"
            )

        return biz

    def get_available_dates(self, db: Session, biz: Business) -> list:
        available_dates = []
        today = datetime.now().date()
        for i in range(biz.advance_booking_days):
            target = today + timedelta(days=i)
            if target.isoformat() not in (biz.holidays or []):
                # Only include if business has operational hours for this day
                day_name = target.strftime("%a").lower()[:3]
                if day_name in biz.operational_hours:
                    available_dates.append(target)
        return available_dates[:10]

    def get_available_slots(self, db: Session, biz: Business, target_date: datetime) -> list:
        day_name = target_date.strftime("%a").lower()[:3]
        hours = biz.operational_hours.get(day_name)
        if not hours: 
            return []

        start_h, end_h = hours
        start_time = datetime.strptime(start_h, "%H:%M").time()
        end_time = datetime.strptime(end_h, "%H:%M").time()

        # Fetch existing occupancy for the date (only active pending/confirmed bookings count against capacity; no_show/completed/cancelled do not)
        existing = db.query(Appointment.appointment_time).filter(
             Appointment.business_id == biz.id,
             Appointment.status.in_(["pending", "confirmed"]),
             Appointment.appointment_time >= datetime.combine(target_date.date(), dt_time.min),
             Appointment.appointment_time <= datetime.combine(target_date.date(), dt_time.max)
        ).all()
        
        occupancy_map = {}
        for appt in existing:
            time_key = appt[0].strftime("%H:%M")
            occupancy_map[time_key] = occupancy_map.get(time_key, 0) + 1

        slots = []
        curr = datetime.combine(target_date.date(), start_time)
        limit = datetime.combine(target_date.date(), end_time)
        
        while curr < limit:
            s_str = curr.strftime("%H:%M")
            # Only show if capacity remains
            if occupancy_map.get(s_str, 0) < biz.max_parallel_bookings:
                slots.append(s_str)
            # Increment only by slot_interval (no buffers)
            curr += timedelta(minutes=biz.slot_interval)
        return slots

    def validate_and_book(self, db: Session, tenant_id: str, target_dt: datetime, customer_phone: str, service_id: int) -> Appointment:
        biz = self.get_tenant_config(db, tenant_id)
        if biz.max_parallel_bookings < 1:
            raise BookingEngineException(
                "Invalid business configuration.",
                "ERR_INVALID_CONFIG"
            )
        
        if target_dt.date().isoformat() in (biz.holidays or []):
            raise BookingEngineException("Business is closed on this date.", "ERR_HOLIDAY")

        # Re-verify capacity to prevent race conditions
        count = db.query(Appointment).filter(
            Appointment.business_id == biz.id,
            Appointment.appointment_time == target_dt,
            Appointment.status.in_(["pending", "confirmed"])
        ).count()
        
        if count >= biz.max_parallel_bookings:
            raise BookingEngineException("This slot just reached full capacity.", "ERR_CAPACITY")

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