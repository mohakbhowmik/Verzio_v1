# Verzio V1 Engineering Principles

## Mission
Launch a production-ready MVP for 0–100 businesses.

## Engineering Principles

1. Every customer-facing piece of data belongs to exactly one Business.
2. Every customer-facing query must be scoped by business_id.
3. Simplicity over cleverness.
4. Easy debugging is more important than fancy architecture.
5. SQLite is the database for V1.
6. SQLAlchemy is the ORM.
7. Every new feature must answer:
   - Which Business owns this?
   - Which table stores it?
   - Which router manages it?
   - How will I debug it?

## Target Scale

- 100 Businesses
- ~5,000 bookings/day
- SQLite
- Single FastAPI server
- WhatsApp-first