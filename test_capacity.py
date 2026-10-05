"""
test_capacity.py — verifies the booking engine's calendar rules on a throwaway
database. Safe to run anytime; it never touches your real data.

    python test_capacity.py

Covers: duration-aware overlaps, finishing by closing time, peak concurrency,
holidays, closed days, past times, and the C2 race (two patients, last seat).
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

import threading
import time as time_mod
from datetime import datetime, time, timedelta

from database import Appointment, Business, Service, SessionLocal
from booking_engine import BookingEngineException, VerzioSaaSEngine, business_today

ALL_DAYS = {d: ["09:00", "18:00"] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
eng = VerzioSaaSEngine()
db = SessionLocal()
failures = 0


def check(label, ok):
    global failures
    print(("PASS " if ok else "FAIL ") + label)
    failures += 0 if ok else 1


def make_biz(name, pnid, capacity, **extra):
    b = Business(name=name, whatsapp_business_phone_number_id=pnid, manager_phone_number="910000000001",
                 timezone="Asia/Kolkata", operational_hours=ALL_DAYS, holidays=[], slot_interval=30,
                 max_parallel_bookings=capacity, approval_mode="manual",
                 is_active=True, accepting_bookings=True, **extra)
    db.add(b); db.commit(); return b


def make_svc(biz, name, minutes):
    s = Service(business_id=biz.id, name=name, duration=minutes, price=100.0, is_active=True)
    db.add(s); db.commit(); return s


def at(day, hhmm):
    return datetime.combine(day, time.fromisoformat(hhmm))


def book(biz, svc, day, hhmm):
    db.add(Appointment(business_id=biz.id, service_id=svc.id, customer_phone="910000000000",
                       appointment_time=at(day, hhmm), status="confirmed"))
    db.commit()


def expect_error(label, code, fn):
    try:
        fn()
        check(f"{label} (no error raised)", False)
    except BookingEngineException as e:
        check(f"{label} -> {e.error_code}", e.error_code == code)


try:
    a = make_biz("Alpha", "T_A", 1)
    day = business_today(a) + timedelta(days=1)
    long_a, short_a = make_svc(a, "Long", 90), make_svc(a, "Short", 30)
    book(a, long_a, day, "10:00")                                  # occupies 10:00-11:30

    short = eng.get_available_slots(db, a, day, short_a)
    long_ = eng.get_available_slots(db, a, day, long_a)
    check("09:30 30-min ends at 10:00 -> offered", "09:30" in short)
    check("10:30 inside 90-min booking -> blocked", "10:30" not in short)
    check("11:00 inside 90-min booking -> blocked", "11:00" not in short)
    check("11:30 after booking ends -> offered", "11:30" in short)
    check("09:00 90-min would overlap 10:00 -> blocked", "09:00" not in long_)
    check("16:30 90-min ends exactly at close -> offered", "16:30" in long_)
    check("17:00 90-min would run past close -> blocked", "17:00" not in long_)

    expect_error("overlapping booking rejected", "ERR_CAPACITY",
                 lambda: eng.validate_and_book(db, "T_A", at(day, "10:30"), "919999999999", short_a.id))
    expect_error("90-min at 17:00 past closing rejected", "ERR_OUTSIDE_HOURS",
                 lambda: eng.validate_and_book(db, "T_A", at(day, "17:00"), "919999999999", long_a.id))
    expect_error("past time rejected", "ERR_PAST_TIME",
                 lambda: eng.validate_and_book(db, "T_A", at(day - timedelta(days=3), "10:00"), "919999999999", short_a.id))

    appt = eng.validate_and_book(db, "T_A", at(day, "12:00"), "919999999999", short_a.id, "Priya")
    check("valid booking succeeds as pending with name", appt.status == "pending" and appt.customer_name == "Priya")

    # Peak concurrency, not overlap count (capacity 2)
    b = make_biz("Beta", "T_B", 2)
    s30, s60 = make_svc(b, "Thirty", 30), make_svc(b, "Sixty", 60)
    book(b, s30, day, "10:00"); book(b, s30, day, "10:30")       # back-to-back, peak 1
    check("60-min at 10:00 fits (peak 1 < capacity 2)", "10:00" in eng.get_available_slots(db, b, day, s60))
    book(b, s60, day, "10:00")                                     # peak 2 from 10:00-11:00
    after = eng.get_available_slots(db, b, day, s60)
    check("60-min at 10:00 now full", "10:00" not in after)
    check("60-min at 11:00 still free", "11:00" in after)

    # Holidays, closed days and the advance window
    c = make_biz("Gamma", "T_C", 1, advance_booking_days=7)
    c.holidays = [day.isoformat()]
    c.operational_hours = {k: v for k, v in ALL_DAYS.items() if k != (day + timedelta(days=1)).strftime("%a").lower()[:3]}
    db.commit()
    sc = make_svc(c, "Cut", 30)
    check("holiday has no slots", eng.get_available_slots(db, c, day, sc) == [])
    check("closed weekday has no slots", eng.get_available_slots(db, c, day + timedelta(days=1), sc) == [])
    dates = eng.get_available_dates(db, c)
    check("holiday and closed day not offered as dates",
          day not in dates and (day + timedelta(days=1)) not in dates)
    check("dates stay inside the 7-day window", all((d - business_today(c)).days < 7 for d in dates))
    expect_error("booking on a holiday rejected", "ERR_HOLIDAY",
                 lambda: eng.validate_and_book(db, "T_C", at(day, "10:00"), "919999999999", sc.id))

    # C2 race: two patients grab the last seat at the same moment.
    r = make_biz("Race", "T_R", 1)
    sr = make_svc(r, "Cut", 30)
    race_service_id = sr.id
    original = VerzioSaaSEngine._day_occupancy

    def slow_occupancy(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        time_mod.sleep(0.4)               # widen the window between check and insert
        return result

    VerzioSaaSEngine._day_occupancy = slow_occupancy
    outcomes = []

    def attempt(phone):
        s = SessionLocal()
        try:
            VerzioSaaSEngine().validate_and_book(s, "T_R", at(day, "15:00"), phone, sr.id)
            outcomes.append("booked")
        except BookingEngineException as e:
            outcomes.append(e.error_code)
        finally:
            s.close()

    threads = [threading.Thread(target=attempt, args=(p,)) for p in ("919000000001", "919000000002")]
    for t in threads: t.start()
    for t in threads: t.join()
    VerzioSaaSEngine._day_occupancy = original
    check(f"race on last seat: exactly one wins {sorted(outcomes)}",
          sorted(outcomes) == ["ERR_CAPACITY", "booked"])
finally:
    db.close()
    from database import engine as _engine
    _engine.dispose()
    try:
        os.remove(_tmp.name)
    except OSError:
        pass

print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILED'}")
raise SystemExit(1 if failures else 0)
