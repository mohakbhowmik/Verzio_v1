"""
tables/models.py: the Verzio Tables tables.

New tables only, plus one added column on owners (role, for door-staff logins).
No booking table is altered, so this is safe on a live DB.

- TablesSettings   one row per business that has (or had) Tables switched on
- TablesZone       a bookable area on the venue's floor plan (a coloured block)
- TablesBooking    one reservation (or a walk-in added at the door)
- TablesLink       a private booking link sent on WhatsApp (/t/<token>)
- TablesWaitlist   a guest waiting for a full zone / time
- TablesOptOut     guests who replied STOP to "book again" offers
- TablesOffer      every "book again" (marketing) message sent: the monthly cap
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import backref, relationship

from database import Base, engine


class TablesSettings(Base):
    __tablename__ = "tables_settings"
    business_id = Column(Integer, ForeignKey("businesses.id"), primary_key=True)
    enabled = Column(Boolean, default=False, nullable=False)

    # Booking rules
    slot_minutes = Column(Integer, default=30, nullable=False)          # arrival times every N minutes
    seating_minutes = Column(Integer, default=150, nullable=False)      # how long a table is held
    last_arrival_minutes = Column(Integer, default=60, nullable=False)  # last arrival N min before closing
    book_ahead_days = Column(Integer, default=14, nullable=False)
    min_notice_minutes = Column(Integer, default=60, nullable=False)
    max_party = Column(Integer, default=20, nullable=False)
    approval_party = Column(Integer, default=8, nullable=False)         # this many people or more: manager approves
    release_minutes = Column(Integer, default=20, nullable=False)       # table freed N min after a no-show

    # Proof: "Verzio saved you ₹X"
    avg_bill = Column(Integer, default=8000, nullable=False)            # ₹ per booking, when no bill is typed
    commission_pct = Column(Integer, default=8, nullable=False)         # what the platforms charge

    # Growth features
    reminder_on = Column(Boolean, default=True, nullable=False)
    review_on = Column(Boolean, default=True, nullable=False)
    google_review_url = Column(String, nullable=True)
    rebook_on = Column(Boolean, default=True, nullable=False)
    rebook_weeks = Column(Integer, default=4, nullable=False)
    waitlist_on = Column(Boolean, default=True, nullable=False)
    offers_per_month = Column(Integer, default=500, nullable=False)     # "book again" messages included

    # Verzio Tables' own plan, separate from Book and Loyalty.
    plan = Column(String, default="trial", nullable=False)              # trial | active | suspended
    paid_until = Column(String, nullable=True)                          # YYYY-MM-DD
    monthly_amount = Column(Integer, default=4999, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    business = relationship("Business", backref=backref("tables_settings", uselist=False))


class TablesZone(Base):
    __tablename__ = "tables_zones"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    note = Column(String, nullable=True)                  # "Open air", "Sofa seating"...
    # "tables": N tables of S seats, a group takes whole tables (joined if needed)
    # "people": counted in guests only (standing areas, bar counters, lounges)
    kind = Column(String, default="tables", nullable=False)
    table_count = Column(Integer, default=4, nullable=False)
    seats_per_table = Column(Integer, default=4, nullable=False)
    people_capacity = Column(Integer, default=20, nullable=False)
    min_party = Column(Integer, default=1, nullable=False)
    # Position on the floor plan grid (GRID_COLS x GRID_ROWS cells).
    x = Column(Integer, default=0, nullable=False)
    y = Column(Integer, default=0, nullable=False)
    w = Column(Integer, default=4, nullable=False)
    h = Column(Integer, default=3, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)      # off: shown grey, not bookable
    is_deleted = Column(Boolean, default=False, nullable=False)    # kept for old bookings
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class TablesBooking(Base):
    __tablename__ = "tables_bookings"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    zone_id = Column(Integer, ForeignKey("tables_zones.id"), nullable=False, index=True)
    phone = Column(String, nullable=True, index=True)     # digits with country code; walk-ins may have none
    name = Column(String, nullable=True)
    party = Column(Integer, nullable=False)
    tables_used = Column(Integer, default=0, nullable=False)   # 0 for "people" zones
    start_at = Column(DateTime, nullable=False, index=True)    # business-local, naive (like appointments)
    end_at = Column(DateTime, nullable=False)
    # pending | confirmed | arrived | no_show | cancelled | declined
    status = Column(String, default="confirmed", nullable=False, index=True)
    source = Column(String, default="whatsapp", nullable=False)   # whatsapp | walkin | staff
    bill_amount = Column(Integer, nullable=True)
    guest_reply = Column(String, nullable=True)           # coming | cancelled (from the reminder)
    rating = Column(String, nullable=True)                # loved | ok | bad (review ask)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    arrived_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    zone = relationship("TablesZone")


class TablesLink(Base):
    __tablename__ = "tables_links"
    token = Column(String, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    phone = Column(String, nullable=False)
    name = Column(String, nullable=True)                  # WhatsApp profile name, prefilled
    party = Column(Integer, nullable=True)                # prefilled (e.g. from a waitlist offer)
    day = Column(String, nullable=True)                   # prefilled YYYY-MM-DD
    time = Column(String, nullable=True)                  # prefilled HH:MM
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    booking_id = Column(Integer, nullable=True)


class TablesWaitlist(Base):
    __tablename__ = "tables_waitlist"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    zone_id = Column(Integer, nullable=True)              # None: any zone
    phone = Column(String, nullable=False)
    name = Column(String, nullable=True)
    party = Column(Integer, nullable=False)
    start_at = Column(DateTime, nullable=False)
    status = Column(String, default="waiting", nullable=False)   # waiting | offered | booked | expired | cancelled
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    offered_at = Column(DateTime, nullable=True)


class TablesOptOut(Base):
    __tablename__ = "tables_optouts"
    __table_args__ = (UniqueConstraint("business_id", "phone", name="uq_tables_optout"),)
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    phone = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class TablesOffer(Base):
    __tablename__ = "tables_offers"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    phone = Column(String, nullable=False, index=True)
    month = Column(String, nullable=False, index=True)    # YYYY-MM, for the monthly cap
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)


TABLES = [TablesSettings.__table__, TablesZone.__table__, TablesBooking.__table__, TablesLink.__table__,
          TablesWaitlist.__table__, TablesOptOut.__table__, TablesOffer.__table__]

# Columns added after the first version. ensure_tables() adds any that are missing.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {}


def ensure_tables() -> None:
    """Create the Tables tables if missing, add newer columns, and add owners.role
    (owner | staff) for door-staff logins. Nothing else is altered."""
    from sqlalchemy import inspect
    Base.metadata.create_all(bind=engine, tables=TABLES)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in have:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        if "role" not in {c["name"] for c in insp.get_columns("owners")}:
            conn.exec_driver_sql("ALTER TABLE owners ADD COLUMN role VARCHAR NOT NULL DEFAULT 'owner'")
