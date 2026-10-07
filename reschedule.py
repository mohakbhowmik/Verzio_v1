"""
reschedule.py — remembers that a customer is moving an existing booking.

When a customer taps "Reschedule" (on a reminder, or after typing "reschedule"),
the normal booking flow runs (pick a day, pick a time, confirm), and the final
"Confirm" moves the existing booking instead of creating a new one. This table
holds which booking is being moved. New table only; safe on a live DB.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, Integer, String

from database import Base, SessionLocal, engine

logger = logging.getLogger("VERZIO_RESCHEDULE")

INTENT_TTL = timedelta(hours=2)          # same as the booking session


class RescheduleIntent(Base):
    __tablename__ = "reschedule_intents"
    business_id = Column(Integer, primary_key=True)
    phone = Column(String, primary_key=True)
    appointment_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def ensure_tables() -> None:
    Base.metadata.create_all(bind=engine, tables=[RescheduleIntent.__table__])


def set_intent(db, business_id: int, phone: str, appointment_id: int) -> None:
    row = db.get(RescheduleIntent, (business_id, phone))
    if row:
        row.appointment_id = appointment_id
        row.created_at = datetime.utcnow()
    else:
        db.add(RescheduleIntent(business_id=business_id, phone=phone, appointment_id=appointment_id))
    db.commit()


def get_intent(db, business_id: int, phone: str) -> int | None:
    """The booking being moved, or None (also None once it's older than INTENT_TTL)."""
    row = db.get(RescheduleIntent, (business_id, phone))
    if row is None:
        return None
    if datetime.utcnow() - row.created_at > INTENT_TTL:
        db.delete(row)
        db.commit()
        return None
    return row.appointment_id


def clear_intent(db, business_id: int, phone: str) -> None:
    row = db.get(RescheduleIntent, (business_id, phone))
    if row:
        db.delete(row)
        db.commit()
