import logging
from typing import Optional
from sqlalchemy.orm import Session
from database import ActivityEvent

logger = logging.getLogger("VERZIO_ACTIVITY")

def log_event(
    db: Session,
    event_type: str,
    status: str = "info",
    message: str = "",
    business_id: Optional[int] = None,
) -> None:
    """
    Append an event to the activity log.
    Standard event_types: booking_confirmed, booking_cancelled, appointment_completed, appointment_no_show, etc.
    FAILS SAFE: If logging fails, it will not interrupt the main process.
    """
    try:
        # Create the event record
        evt = ActivityEvent(
            business_id=business_id,
            event_type=event_type,
            status=status,
            message=(message or "")[:2000] or None,
        )
        db.add(evt)
        db.commit()
    except Exception as e:
        # We rollback to clean the session, but we do NOT raise the error.
        db.rollback()
        logger.warning(f"Observability Failure: Could not log event '{event_type}': {e}")