"""
booking_engine.py — Core calendar availability and atomic booking engine.
Hardened for C1 (duration-aware slot overlaps), C2 (atomic concurrency lock), and W6 (revalidation).
"""
from datetime import datetime, date, time, timedelta
from typing import Tuple, Optional, List
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text
from database import Business, Service, Appointment, Holiday

logger = logging.getLogger("VERZIO_BOOKING_ENGINE")


def is_holiday(db: Session, business_id: int, target_date: date) -> bool:
    """Check if the given date is marked as an active holiday for this business."""
    return db.query(Holiday).filter(
        Holiday.business_id == business_id,
        Holiday.holiday_date == target_date
    ).first() is not None


def get_available_slots_for_day(
    db: Session,
    business: Business,
    target_date: date,
    service: Optional[Service] = None
) -> List[str]:
    """
    C1: Duration-aware slot calculator.
    - Prevents overlapping appointments from exceeding max_parallel_bookings.
    - Ensures the selected service completes fully before business closing time.
    - Excludes times that have already elapsed today.
    """
    if not business.accepting_bookings:
        return []

    # 1. Holiday check
    if is_holiday(db, business.id, target_date):
        return []

    # 2. Operational hours check
    weekday_str = target_date.strftime("%a").lower()[:3]
    hours = (business.operational_hours or {}).get(weekday_str)
    if not hours or len(hours) < 2:
        return []

    try:
        open_time = datetime.strptime(hours[0], "%H:%M").time()
        close_time = datetime.strptime(hours[1], "%H:%M").time()
    except (ValueError, TypeError):
        return []

    open_dt = datetime.combine(target_date, open_time)
    close_dt = datetime.combine(target_date, close_time)

    duration_mins = service.duration if (service and service.duration) else (business.slot_interval or 30)
    service_duration = timedelta(minutes=duration_mins)
    slot_interval = timedelta(minutes=business.slot_interval or 30)
    max_capacity = business.max_parallel_bookings or 1

    # 3. Fetch all active or pending appointments for this business on target date
    existing_appts = (
        db.query(Appointment)
        .filter(
            Appointment.business_id == business.id,
            Appointment.appointment_date == target_date,
            Appointment.status.in_(["pending", "confirmed"])
        )
        .all()
    )

    # 4. Map existing appointments into occupied time intervals [start, end)
    booked_intervals = []
    for appt in existing_appts:
        appt_start = datetime.combine(appt.appointment_date, appt.appointment_time)
        appt_dur = appt.service.duration if (appt.service and appt.service.duration) else (business.slot_interval or 30)
        booked_intervals.append((appt_start, appt_start + timedelta(minutes=appt_dur)))

    now = datetime.now()
    available_slots = []
    current_slot = open_dt

    # 5. Evaluate prospective slots from opening to closing
    while current_slot + service_duration <= close_dt:
        slot_start = current_slot
        slot_end = current_slot + service_duration

        # Skip times in the past if scheduling for today
        if target_date == now.date() and slot_start <= now:
            current_slot += slot_interval
            continue

        # Overlap logic: A_start < B_end and B_start < A_end
        overlapping_count = sum(
            1 for b_start, b_end in booked_intervals
            if b_start < slot_end and b_end > slot_start
        )

        if overlapping_count < max_capacity:
            available_slots.append(slot_start.strftime("%H:%M"))

        current_slot += slot_interval

    return available_slots


def validate_and_book(
    db: Session,
    business_id: int,
    service_id: int,
    customer_phone: str,
    target_date: date,
    target_time: time,
    customer_name: Optional[str] = None
) -> Tuple[bool, str, Optional[Appointment]]:
    """
    C2 & W6: Atomic booking validation with transaction locking.
    Revalidates slot availability, operating hours, holiday status, and service validity.
    """
    try:
        # Atomic lock to serialize concurrent bookings in SQLite
        db.execute(text("BEGIN IMMEDIATE"))
    except Exception:
        pass

    biz = db.query(Business).filter(Business.id == business_id).first()
    if not biz or not biz.accepting_bookings:
        db.rollback()
        return False, "This business is currently not accepting online bookings.", None

    if is_holiday(db, business_id, target_date):
        db.rollback()
        return False, "The business is closed for a holiday on this date.", None

    service = db.query(Service).filter(
        Service.id == service_id,
        Service.business_id == business_id,
        Service.is_active == True,
        Service.is_deleted == False
    ).first()
    if not service:
        db.rollback()
        return False, "The selected service is no longer available.", None

    now = datetime.now()
    requested_dt = datetime.combine(target_date, target_time)
    if requested_dt <= now:
        db.rollback()
        return False, "This appointment time is in the past. Please select an upcoming slot.", None

    available_slots = get_available_slots_for_day(db, biz, target_date, service)
    time_str = target_time.strftime("%H:%M")
    if time_str not in available_slots:
        db.rollback()
        return False, "Sorry, this slot was just taken or is no longer available. Please select another time.", None

    initial_status = "confirmed" if biz.approval_mode == "automatic" else "pending"

    appt = Appointment(
        business_id=biz.id,
        service_id=service.id,
        customer_phone=customer_phone,
        customer_name=customer_name or "Client",
        appointment_date=target_date,
        appointment_time=target_time,
        status=initial_status
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)

    logger.info(f"Booked appointment #{appt.id} for {customer_phone} at business #{biz.id} ({initial_status})")
    return True, "SUCCESS", appt