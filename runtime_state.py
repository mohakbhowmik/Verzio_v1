from sqlalchemy.orm import Session
from database import UserSession
from datetime import datetime

class RuntimeStateManager:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create_session(self, phone: str, tenant_id: str) -> UserSession:
        session = self.db.query(UserSession).filter(UserSession.phone_number == phone).first()
        if not session:
            session = UserSession(phone_number=phone, tenant_id=tenant_id, step="START")
            self.db.add(session)
            self.db.commit()
            self.db.refresh(session)
        return session

    def update_session(self, phone: str, step: str, **kwargs):
        session = self.db.query(UserSession).filter(UserSession.phone_number == phone).first()
        if session:
            session.step = step
            session.updated_at = datetime.utcnow()
            for key, value in kwargs.items():
                if hasattr(session, key):
                    setattr(session, key, value)
            self.db.commit()

    def clear_session(self, phone: str):
        self.db.query(UserSession).filter(UserSession.phone_number == phone).delete()
        self.db.commit()