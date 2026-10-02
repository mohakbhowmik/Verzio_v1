import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import UserSession

logger = logging.getLogger("VERZIO_RUNTIME_STATE")

# Fields that define the tenant boundary and must never be changed via update_session.
PROTECTED_FIELDS = {"phone_number", "business_id"}


def _require_business_id(business_id) -> None:
    if not isinstance(business_id, int) or isinstance(business_id, bool) or business_id <= 0:
        raise ValueError(f"business_id must be a positive int, got {business_id!r}")


class RuntimeStateManager:
    def __init__(self, db: Session):
        self.db = db

    def _get(self, phone: str, business_id: int) -> UserSession | None:
        return (
            self.db.query(UserSession)
            .filter(
                UserSession.phone_number == phone,
                UserSession.business_id == business_id,
            )
            .first()
        )

    def get_or_create_session(self, phone: str, *, business_id: int) -> UserSession:
        _require_business_id(business_id)
        session = self._get(phone, business_id)
        if session:
            return session

        session = UserSession(phone_number=phone, business_id=business_id, step="START")
        self.db.add(session)
        try:
            self.db.commit()
        except IntegrityError:
            # Two messages from the same phone to the same business raced; reuse the winner.
            self.db.rollback()
            session = self._get(phone, business_id)
            if session is None:
                raise
            return session
        self.db.refresh(session)
        return session

    def update_session(self, phone: str, *, business_id: int, step: str, **kwargs) -> bool:
        _require_business_id(business_id)
        session = self._get(phone, business_id)
        if not session:
            logger.warning(
                "update_session: no session for phone=%s business_id=%s (step=%s)",
                phone, business_id, step,
            )
            return False

        for key in kwargs:
            if key in PROTECTED_FIELDS:
                raise ValueError(f"update_session cannot modify protected field '{key}'")
            if not hasattr(session, key):
                raise ValueError(f"update_session: unknown field '{key}'")

        session.step = step
        session.updated_at = datetime.utcnow()
        for key, value in kwargs.items():
            setattr(session, key, value)
        self.db.commit()
        return True

    def clear_session(self, phone: str, *, business_id: int) -> None:
        _require_business_id(business_id)
        (
            self.db.query(UserSession)
            .filter(
                UserSession.phone_number == phone,
                UserSession.business_id == business_id,
            )
            .delete()
        )
        self.db.commit()