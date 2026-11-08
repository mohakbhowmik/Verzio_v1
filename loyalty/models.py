"""
loyalty/models.py: the loyalty tables.

New tables only; no booking table is altered, so this is safe on a live DB.

- LoyaltySettings  one row per business that has (or had) loyalty switched on
- LoyaltyReward    the owner's reward catalog ("Free Cappuccino = 200 points")
- LoyaltyMember    one row per customer per business: balance + wallet link
- LoyaltyEntry     points history: every earn, claim and adjustment is a row
- LoyaltyClaim     a claim code the customer shows at the counter

Loyalty Plus (Nov 2026), all optional and off until the owner switches them on:
- LoyaltyOutlet    extra counters/outlets of one brand (shared points, own QR each)
- LoyaltyBoost     "double points" rules (days of the week + optional hours)
- LoyaltyBillCode  4-digit code for points by bill amount, typed by staff
- LoyaltyReferral  "refer a friend": who invited whom, rewarded on the first visit
- LoyaltyStaff     counter-staff logins (owners.role = 'staff') and their outlet
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
    monthly_amount = Column(Integer, default=1299, nullable=False)  # ₹ per month, for your records
    # --- Loyalty Plus -------------------------------------------------------
    main_outlet_name = Column(String, nullable=True)                # name of the main counter, once outlets exist
    tiers_on = Column(Boolean, default=False, nullable=False)       # Silver / Gold by number of visits
    silver_visits = Column(Integer, default=5, nullable=False)
    silver_bonus = Column(Integer, default=25, nullable=False)      # % extra on visit and bill points
    gold_visits = Column(Integer, default=15, nullable=False)
    gold_bonus = Column(Integer, default=50, nullable=False)
    bill_points_on = Column(Boolean, default=False, nullable=False) # staff type the bill -> points per ₹100
    points_per_100 = Column(Integer, default=5, nullable=False)
    birthday_on = Column(Boolean, default=False, nullable=False)
    birthday_points = Column(Integer, default=50, nullable=False)
    referral_on = Column(Boolean, default=False, nullable=False)
    referral_points = Column(Integer, default=30, nullable=False)   # each: the friend and the one who invited
    review_on = Column(Boolean, default=False, nullable=False)
    review_link = Column(String, nullable=True)                     # the business's Google review link
    review_after = Column(Integer, default=3, nullable=False)       # ask on this visit number
    summary_on = Column(Boolean, default=False, nullable=False)     # Monday WhatsApp summary to the manager number
    avg_bill = Column(Integer, default=0, nullable=False)           # ₹, for the Results estimate when no bills are typed
    offers_per_month = Column(Integer, default=300, nullable=False) # cap on paid (Marketing) birthday messages
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
    # --- Loyalty Plus -------------------------------------------------------
    birthday = Column(String, nullable=True)            # "MM-DD" (no year is stored)
    birthday_asked = Column(Boolean, default=False, nullable=False)
    review_asked = Column(Boolean, default=False, nullable=False)
    ref_code = Column(String, nullable=True, index=True)  # their personal invite code
    offers_off = Column(Boolean, default=False, nullable=False)  # replied STOP: no birthday offers


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
    # --- Loyalty Plus -------------------------------------------------------
    outlet_id = Column(Integer, nullable=True)          # None = the main counter
    bill_amount = Column(Integer, nullable=True)        # ₹, for kind "bill"
    boost = Column(Integer, nullable=True)              # 2 or 3 when a double-points rule applied


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


class LoyaltyOutlet(Base):
    """Another counter of the same brand, on the same WhatsApp number. Points are
    shared across outlets; each outlet has its own counter QR code."""
    __tablename__ = "loyalty_outlets"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    earn_code = Column(String, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class LoyaltyBoost(Base):
    """Double (or triple) points on some days, optionally only between two times."""
    __tablename__ = "loyalty_boosts"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    days = Column(String, nullable=False)               # weekday numbers, Monday = 0: "1,2"
    start = Column(String, nullable=True)               # "15:00" (empty = all day)
    end = Column(String, nullable=True)                 # "18:00"
    multiplier = Column(Integer, default=2, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class LoyaltyBillCode(Base):
    """Given with each scan when bill points are on. Staff type it with the bill amount."""
    __tablename__ = "loyalty_bill_codes"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("loyalty_members.id"), nullable=False, index=True)
    outlet_id = Column(Integer, nullable=True)
    code = Column(String, nullable=False, index=True)
    status = Column(String, default="pending", nullable=False)   # pending | used | replaced
    bill_amount = Column(Integer, nullable=True)
    points = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)

    member = relationship("LoyaltyMember")


class LoyaltyReferral(Base):
    """A friend who tapped someone's invite link. Both get points on the friend's first visit."""
    __tablename__ = "loyalty_referrals"
    __table_args__ = (UniqueConstraint("business_id", "friend_phone", name="uq_loyalty_referral"),)
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    referrer_id = Column(Integer, ForeignKey("loyalty_members.id"), nullable=False, index=True)
    friend_phone = Column(String, nullable=False)
    friend_member_id = Column(Integer, nullable=True)
    status = Column(String, default="pending", nullable=False)   # pending | rewarded | over_cap
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    rewarded_at = Column(DateTime, nullable=True)

    referrer = relationship("LoyaltyMember")


class LoyaltyStaff(Base):
    """Counter staff: a login (owners row with role 'staff') and, for chains, their outlet."""
    __tablename__ = "loyalty_staff"
    owner_id = Column(Integer, ForeignKey("owners.id"), primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    outlet_id = Column(Integer, nullable=True)


TABLES = [LoyaltySettings.__table__, LoyaltyReward.__table__, LoyaltyMember.__table__,
          LoyaltyEntry.__table__, LoyaltyClaim.__table__, LoyaltyOutlet.__table__, LoyaltyBoost.__table__,
          LoyaltyBillCode.__table__, LoyaltyReferral.__table__, LoyaltyStaff.__table__]


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
        "main_outlet_name": "VARCHAR",
        "tiers_on": "BOOLEAN NOT NULL DEFAULT 0",
        "silver_visits": "INTEGER NOT NULL DEFAULT 5",
        "silver_bonus": "INTEGER NOT NULL DEFAULT 25",
        "gold_visits": "INTEGER NOT NULL DEFAULT 15",
        "gold_bonus": "INTEGER NOT NULL DEFAULT 50",
        "bill_points_on": "BOOLEAN NOT NULL DEFAULT 0",
        "points_per_100": "INTEGER NOT NULL DEFAULT 5",
        "birthday_on": "BOOLEAN NOT NULL DEFAULT 0",
        "birthday_points": "INTEGER NOT NULL DEFAULT 50",
        "referral_on": "BOOLEAN NOT NULL DEFAULT 0",
        "referral_points": "INTEGER NOT NULL DEFAULT 30",
        "review_on": "BOOLEAN NOT NULL DEFAULT 0",
        "review_link": "VARCHAR",
        "review_after": "INTEGER NOT NULL DEFAULT 3",
        "summary_on": "BOOLEAN NOT NULL DEFAULT 0",
        "avg_bill": "INTEGER NOT NULL DEFAULT 0",
        "offers_per_month": "INTEGER NOT NULL DEFAULT 300",
    },
    "loyalty_members": {
        "daily_streak": "INTEGER NOT NULL DEFAULT 0",
        "weekly_streak": "INTEGER NOT NULL DEFAULT 0",
        "birthday": "VARCHAR",
        "birthday_asked": "BOOLEAN NOT NULL DEFAULT 0",
        "review_asked": "BOOLEAN NOT NULL DEFAULT 0",
        "ref_code": "VARCHAR",
        "offers_off": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "loyalty_entries": {
        "outlet_id": "INTEGER",
        "bill_amount": "INTEGER",
        "boost": "INTEGER",
    },
}


def ensure_tables() -> None:
    """Create the loyalty tables if missing, and add newer loyalty columns.
    Only loyalty tables are touched (plus owners.role for staff logins);
    booking tables are never altered."""
    from sqlalchemy import inspect
    Base.metadata.create_all(bind=engine, tables=TABLES)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in have:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        # Counter-staff logins use owners.role (also added by Verzio Tables).
        if "role" not in {c["name"] for c in insp.get_columns("owners")}:
            conn.exec_driver_sql("ALTER TABLE owners ADD COLUMN role VARCHAR NOT NULL DEFAULT 'owner'")
