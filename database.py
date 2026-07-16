import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean, JSON, ForeignKey, Float
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
    
    # Configuration
    timezone = Column(String, default="UTC")
    is_active = Column(Boolean, default=True)
    billing_expiry = Column(DateTime, nullable=True)
    
    # Validation Rules (Constraint: operational_hours cannot be NULL)
    operational_hours = Column(JSON, nullable=False) 
    holidays = Column(JSON, default=list) 
    slot_interval = Column(Integer, default=30) 
    buffer_minutes = Column(Integer, default=0)
    advance_booking_days = Column(Integer, default=14)
    approval_mode = Column(String, default="manual") 
    notification_preferences = Column(JSON, default=dict) 
    
    # Relationships
    appointments = relationship("Appointment", back_populates="business")
    services = relationship("Service", back_populates="business")
    staff_members = relationship("Staff", back_populates="business")

class UserSession(Base):
    __tablename__ = "user_sessions"
    phone_number = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False)
    step = Column(String, default="START") 
    selected_service_id = Column(Integer, nullable=True)
    selected_date = Column(String, nullable=True)
    selected_time = Column(String, nullable=True)
    # Automatically update the timestamp whenever the session is touched
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    name = Column(String, nullable=False)
    duration = Column(Integer, default=30)
    price = Column(Float, nullable=True)
    is_active = Column(Boolean, default=True)

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
    
    # Statuses: pending, confirmed, cancelled
    status = Column(String, default="pending") 
    created_at = Column(DateTime, default=datetime.utcnow)

    business = relationship("Business", back_populates="appointments")
    service = relationship("Service", back_populates="appointments")
    # FIXED: staff relationship now correctly back_populates "appointments" on the Staff model
    staff = relationship("Staff", back_populates="appointments")

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()