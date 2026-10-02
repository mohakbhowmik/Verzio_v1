import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean, JSON, ForeignKey, Float, Text, inspect
from sqlalchemy.orm import sessionmaker, relationship, declarative_base
from dotenv import load_dotenv



load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./verzio_saas.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Business(Base):
    __tablename__ = "businesses"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    whatsapp_business_phone_number_id = Column(String, unique=True, index=True, nullable=False)
    manager_phone_number = Column(String, nullable=False)
    timezone = Column(String, default="UTC")
    is_active = Column(Boolean, default=True)
    
    # MISSION 4: Capacity Management
    accepting_bookings = Column(Boolean, default=True)
    max_parallel_bookings = Column(Integer, default=1)
    
    # Configuration
    operational_hours = Column(JSON, nullable=False) 
    holidays = Column(JSON, default=list) 
    slot_interval = Column(Integer, default=30) 
    advance_booking_days = Column(Integer, default=14)
    enable_service_selection = Column(
    Boolean,
    default=True,
    server_default="true",
    nullable=False
    )
    approval_mode = Column(String, default="manual") 
    notification_preferences = Column(JSON, default=dict) 

    appointments = relationship("Appointment", back_populates="business")
    services = relationship("Service", back_populates="business")
    staff_members = relationship("Staff", back_populates="business")
    owners = relationship("Owner", back_populates="business")

class Owner(Base):
    __tablename__ = "owners"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    full_name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    phone_number = Column(String, unique=True, index=True, nullable=True)
    password_hash = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    business = relationship("Business", back_populates="owners")

class UserSession(Base):
    __tablename__ = "user_sessions"
    # Composite primary key: a phone number has one independent session PER business.
    phone_number = Column(String, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), primary_key=True, index=True)
    step = Column(String, default="START") 
    selected_service_id = Column(Integer, nullable=True)
    selected_date = Column(String, nullable=True)
    selected_time = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(Integer, primary_key=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    severity = Column(String, nullable=False)      # warning | error | critical
    module = Column(String, nullable=False)

    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=True)

    phone_number = Column(String, nullable=True)

    title = Column(String, nullable=False)

    message = Column(Text, nullable=False)

    stack_trace = Column(Text, nullable=True)

    resolved = Column(Boolean, default=False)

    resolved_at = Column(DateTime, nullable=True)


class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    name = Column(String, nullable=False)
    duration = Column(Integer, default=30)
    price = Column(Float, nullable=True)
    is_active = Column(Boolean, default=True)
    is_deleted = Column(Boolean, default=False, nullable=False)
    business = relationship("Business", back_populates="services")
    appointments = relationship("Appointment", back_populates="service")

class Staff(Base):
    __tablename__ = "staff"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    name = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    working_days = Column(JSON, default=list)
    business = relationship("Business", back_populates="staff_members")
    appointments = relationship("Appointment", back_populates="staff")

class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=True)
    staff_id = Column(Integer, ForeignKey("staff.id"), nullable=True)
    customer_phone = Column(String, nullable=False)
    customer_name = Column(String, nullable=True)
    appointment_time = Column(DateTime, nullable=False)
    status = Column(String, default="pending")  # Valid: pending, confirmed, completed, cancelled, no_show
    created_at = Column(DateTime, default=datetime.utcnow)
    business = relationship("Business", back_populates="appointments")
    service = relationship("Service", back_populates="appointments")
    staff = relationship("Staff", back_populates="appointments")

def init_db():
    _reset_legacy_user_sessions()
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- APPEND TO database.py ---

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), unique=True, nullable=False)
    plan = Column(String, default="trial") 
    status = Column(String, default="active") 
    monthly_amount = Column(Float, nullable=True)
    next_billing_date = Column(String, nullable=True) 
    notes = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    business = relationship("Business", backref="subscription", uselist=False)

class PaymentRecord(Base):
    __tablename__ = "payment_records"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="INR")
    paid_on = Column(String, nullable=False)
    status = Column(String, default="paid")
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    business = relationship("Business", backref="payment_records")

class ActivityEvent(Base):
    __tablename__ = "activity_events"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=True, index=True)
    event_type = Column(String, nullable=False, index=True)
    status = Column(String, default="info") 
    message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    business = relationship("Business", backref="activity_events")


def _reset_legacy_user_sessions():
    """user_sessions is disposable runtime state. If the table still has the old
    single-column (phone_number) primary key, drop it so create_all rebuilds it
    with the composite (phone_number, business_id) key."""
    insp = inspect(engine)
    if "user_sessions" not in insp.get_table_names():
        return
    columns = {c["name"] for c in insp.get_columns("user_sessions")}
    if "business_id" not in columns:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE user_sessions")


_reset_legacy_user_sessions()
Base.metadata.create_all(bind=engine)