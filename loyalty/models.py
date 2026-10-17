"""
loyalty/models.py: the loyalty tables.

New tables only; no booking table is altered, so this is safe on a live DB.

- LoyaltySettings  one row per business that has (or had) loyalty switched on
- LoyaltyReward    the owner's reward catalog ("Free Cappuccino = 200 points")
- LoyaltyMember    one row per customer per business: balance + wallet link
- LoyaltyEntry     points history: every earn, claim and adjustment is a row
- LoyaltyClaim     a claim code the customer shows at the counter
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import backref, relationship

from database import Base, engine


class LoyaltySettings(Base):
    __tablename__ = "loyalty_settings"
    business_id = Column(Integer, ForeignKey("businesses.id"), primary_key=True)
    enabled = Column(Boolean, default=False, nullable=False)
    # "with_booking": booking bot keeps working, loyalty adds to it.
    # "only": the number is used for loyalty only (a cafe without bookings).
    mode = Column(String, default="with_booking", nullable=False)
    points_per_visit = Column(Integer, default=10, nullable=False)
    welcome_bonus = Column(Integer, default=0, nullable=False)
    earn_code = Column(String, nullable=False)          # printed in the counter QR
    whatsapp_number = Column(String, nullable=True)     # the cafe's WhatsApp number, for the QR
    # Streaks (each can be on or off). Bonus = step x (streak - 1), never above the cap.
    daily_streak_on = Column(Boolean, default=False, nullable=False)
    daily_streak_step = Column(Integer, default=5, nullable=False)
    daily_streak_max = Column(Integer, default=20, nullable=False)
    weekly_streak_on = Column(Boolean, default=False, nullable=False)
    weekly_streak_step = Column(Integer, default=10, nullable=False)
    weekly_streak_max = Column(Integer, default=30, nullable=False)
    # Verzio Loyalty's own plan, separate from the Verzio Book plan (plans.py).
    plan = Column(String, default="trial", nullable=False)          # trial | active | suspended
    paid_until = Column(String, nullable=True)                      # YYYY-MM-DD, for "active"
    monthly_amount = Column(Integer, default=799, nullable=False)   # ₹ per month, for your records
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # business.loyalty -> this row (used by the owner portal menu)
    business = relationship("Business", backref=backref("loyalty", uselist=False))


class LoyaltyReward(Base):
    __tablename__ = "loyalty_rewards"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    points = Column(Integer, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False)   # kept for old history rows
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class LoyaltyMember(Base):
    __tablename__ = "loyalty_members"
    __table_args__ = (UniqueConstraint("business_id", "phone", name="uq_loyalty_member"),)
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    phone = Column(String, nullable=False)              # digits with country code
    name = Column(String, nullable=True)
    points = Column(Integer, default=0, nullable=False)  # current balance (history is in LoyaltyEntry)
    wallet_token = Column(String, unique=True, nullable=False, index=True)
    visits = Column(Integer, default=0, nullable=False)
    last_visit_date = Column(String, nullable=True)     # business-local YYYY-MM-DD of the last visit
    daily_streak = Column(Integer, default=0, nullable=False)    # visits on consecutive open days
    weekly_streak = Column(Integer, default=0, nullable=False)   # consecutive weeks with a visit
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class LoyaltyEntry(Base):
    __tablename__ = "loyalty_entries"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("loyalty_members.id"), nullable=False, index=True)
    delta = Column(Integer, nullable=False)             # +earned / -spent
    # visit | welcome | manual | adjust | claim | streak_daily | streak_weekly
    kind = Column(String, nullable=False)
    note = Column(String, nullable=True)
    balance_after = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class LoyaltyClaim(Base):
    __tablename__ = "loyalty_claims"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("loyalty_members.id"), nullable=False, index=True)
    reward_id = Column(Integer, ForeignKey("loyalty_rewards.id"), nullable=False)
    reward_name = Column(String, nullable=False)        # as it was when claimed
    points = Column(Integer, nullable=False)            # as it was when claimed
    code = Column(String, nullable=False, index=True)   # 4 digits, shown at the counter
    # pending | given | replaced  (pending past expires_at = expired)
    status = Column(String, default="pending", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    given_at = Column(DateTime, nullable=True)

    member = relationship("LoyaltyMember")


TABLES = [LoyaltySettings.__table__, LoyaltyReward.__table__, LoyaltyMember.__table__,
          LoyaltyEntry.__table__, LoyaltyClaim.__table__]


# Columns added after the first version of these tables. ensure_tables() adds
# any that are missing, so an older loyalty database keeps working.
_ADDED_COLUMNS = {
    "loyalty_settings": {
        "daily_streak_on": "BOOLEAN NOT NULL DEFAULT 0",
        "daily_streak_step": "INTEGER NOT NULL DEFAULT 5",
        "daily_streak_max": "INTEGER NOT NULL DEFAULT 20",
        "weekly_streak_on": "BOOLEAN NOT NULL DEFAULT 0",
        "weekly_streak_step": "INTEGER NOT NULL DEFAULT 10",
        "weekly_streak_max": "INTEGER NOT NULL DEFAULT 30",
        "plan": "VARCHAR NOT NULL DEFAULT 'trial'",
        "paid_until": "VARCHAR",
        "monthly_amount": "INTEGER NOT NULL DEFAULT 799",
    },
    "loyalty_members": {
        "daily_streak": "INTEGER NOT NULL DEFAULT 0",
        "weekly_streak": "INTEGER NOT NULL DEFAULT 0",
    },
}


def ensure_tables() -> None:
    """Create the loyalty tables if missing, and add newer loyalty columns.
    Only loyalty tables are touched; booking tables are never altered."""
    from sqlalchemy import inspect
    Base.metadata.create_all(bind=engine, tables=TABLES)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in have:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
