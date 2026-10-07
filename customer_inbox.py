"""
customer_inbox.py
================================================================================
VERZIO STUDIO — MESSAGES THAT AREN'T BOOKINGS
================================================================================
Not every WhatsApp message is a booking. "Hi" is, but "I had a facial on Monday
and my skin is red, is that normal?" is not, and must reach a human.

- classify_text(): "book" for greetings and short booking requests, otherwise
  "other". When unsure it says "other", so the customer gets a one-tap choice
  ("Book appointment" / "Talk to us") instead of a booking list they didn't
  ask for.
- customer_messages table: every non-booking message is kept, so it can be
  forwarded to the owner and, later, shown in the owner portal.

New table only; no existing table is altered, so this is safe on a live DB.
"""
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from database import Base, SessionLocal, engine

logger = logging.getLogger("VERZIO_INBOX")

# A second non-booking message within this many minutes of the first means the
# customer is clearly not booking: hand over to a human without asking again.
FOLLOW_UP_MINUTES = 10


class CustomerMessage(Base):
    __tablename__ = "customer_messages"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, nullable=False, index=True)
    phone = Column(String, nullable=False, index=True)
    customer_name = Column(String, nullable=True)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    forwarded = Column(Boolean, default=False, nullable=False)


def ensure_tables() -> None:
    Base.metadata.create_all(bind=engine, tables=[CustomerMessage.__table__])


# ------------------------------------------------------------------------
# Intent
# ------------------------------------------------------------------------

# Whole-message greetings / menu words (any language customers commonly use in India, UK, UAE).
_GREETINGS = {
    "hi", "hello", "hey", "hii", "hiii", "hiiii", "helo", "hlo", "hallo", "yo", "hola",
    "namaste", "namaskar", "namaskaram", "vanakkam", "salaam", "salam", "assalamualaikum",
    "marhaba", "ola", "start", "menu", "book", "booking", "bookings", "appointment",
    "appointments", "slot", "slots", "gm", "good morning", "good afternoon", "good evening",
    "hi there", "hello there", "hey there",
}
# Words that make a short message a booking request ("book a haircut", "any slot tomorrow?").
_BOOK_WORDS = {"book", "booking", "appointment", "appt", "slot", "slots", "reserve", "schedule", "available", "availability"}
# Words that mean "something about an existing booking or a problem": always a human.
_HUMAN_WORDS = {
    "cancel", "reschedule", "change", "postpone", "refund", "complaint", "problem", "issue",
    "pain", "red", "redness", "swelling", "rash", "itching", "burning", "allergy", "reaction",
    "ago", "yesterday", "previous", "booked", "sorry", "urgent",
    "price", "cost", "charges", "offer", "discount", "address", "location",
    "call", "talk", "speak", "human", "doctor", "staff", "help",
}
MAX_BOOKING_WORDS = 8
# "ok", "thanks", "👍" after a confirmation: nothing to answer.
_ACK_WORDS = {"ok", "okay", "okk", "k", "kk", "thanks", "thank", "you", "u", "thx", "ty", "tq", "great", "cool",
              "done", "sure", "fine", "alright", "perfect", "noted", "got", "it", "see", "then", "shukriya",
              "dhanyavad", "welcome", "nice", "good", "super", "awesome"}


def _words(body: str) -> list[str]:
    cleaned = re.sub(r"[^\w\s]", " ", (body or "").lower())
    return [w for w in cleaned.split() if w]


def classify_text(body: str) -> str:
    """'book', 'ack' (no reply needed) or 'other'."""
    words = _words(body)
    if not words:
        return "ack"                        # emoji-only "👍", "🙏"
    phrase = " ".join(words)
    if len(words) <= 4 and all(w in _ACK_WORDS for w in words):
        return "ack"
    if phrase in _GREETINGS or re.fullmatch(r"h+i+|he+y+|hel+o+", phrase):
        return "book"
    if any(w in _HUMAN_WORDS for w in words):
        return "other"
    if len(words) <= MAX_BOOKING_WORDS and any(w in _BOOK_WORDS for w in words):
        return "book"
    return "other"


MEDIA_LABELS = {
    "image": "[sent a photo]", "audio": "[sent a voice note]", "video": "[sent a video]",
    "document": "[sent a document]", "sticker": "[sent a sticker]", "location": "[sent a location]",
    "contacts": "[sent a contact]",
}


# ------------------------------------------------------------------------
# Storage
# ------------------------------------------------------------------------

def save_message(business_id: int, phone: str, name: str | None, body: str) -> bool:
    """Store a non-booking message. Returns True if the same customer already sent
    one in the last FOLLOW_UP_MINUTES that hasn't been passed to the owner yet."""
    db = SessionLocal()
    try:
        since = datetime.utcnow() - timedelta(minutes=FOLLOW_UP_MINUTES)
        recent = (
            db.query(CustomerMessage)
            .filter(CustomerMessage.business_id == business_id, CustomerMessage.phone == phone,
                    CustomerMessage.forwarded.is_(False), CustomerMessage.created_at >= since)
            .count()
        )
        db.add(CustomerMessage(business_id=business_id, phone=phone, customer_name=name, body=(body or "")[:2000]))
        db.commit()
        return recent > 0
    except Exception:
        db.rollback()
        logger.exception("Could not store customer message for business_id=%s", business_id)
        return False
    finally:
        db.close()


def take_unforwarded(business_id: int, phone: str, limit: int = 5) -> list[str]:
    """Bodies not yet passed to the owner (oldest first); marks them forwarded."""
    db = SessionLocal()
    try:
        rows = (
            db.query(CustomerMessage)
            .filter(CustomerMessage.business_id == business_id, CustomerMessage.phone == phone,
                    CustomerMessage.forwarded.is_(False))
            .order_by(CustomerMessage.id.desc())
            .limit(limit)
            .all()
        )
        for row in rows:
            row.forwarded = True
        db.commit()
        return [r.body for r in reversed(rows)]
    except Exception:
        db.rollback()
        logger.exception("Could not read customer messages for business_id=%s", business_id)
        return []
    finally:
        db.close()
