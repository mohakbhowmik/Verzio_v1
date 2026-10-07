"""
hours.py — opening hours with an optional daily break.

operational_hours stays a {day: [...]} JSON dict, so nothing is migrated:
    {"mon": ["09:00", "18:00"]}                    open 9–6
    {"mon": ["09:00", "18:00", "13:00", "14:00"]}  open 9–6, closed 1–2 for a break
The forms set one break for all open days (what salons and clinics need), but
the engine reads the break per day, so per-day breaks can come later.
"""
from datetime import datetime

WEEK_DAYS = [
    ("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"), ("thu", "Thursday"),
    ("fri", "Friday"), ("sat", "Saturday"), ("sun", "Sunday"),
]


def _hhmm(value) -> str | None:
    try:
        return datetime.strptime((value or "").strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def clean_break(start, end) -> tuple[str, str] | None:
    """('13:00', '14:00') if both are valid times and start < end, else None."""
    s, e = _hhmm(start), _hhmm(end)
    if s and e and s < e:
        return s, e
    return None


def build_hours(form) -> dict:
    """operational_hours from form fields hours_<day>_enabled/_open/_close plus
    break_start / break_end (optional, applied to every open day)."""
    brk = clean_break(form.get("break_start"), form.get("break_end"))
    hours = {}
    for key, _label in WEEK_DAYS:
        if form.get(f"hours_{key}_enabled") == "on":
            open_t = _hhmm(form.get(f"hours_{key}_open")) or "09:00"
            close_t = _hhmm(form.get(f"hours_{key}_close")) or "18:00"
            entry = [open_t, close_t]
            if brk and open_t < brk[0] and brk[1] < close_t:
                entry += list(brk)
            hours[key] = entry
    return hours


def daily_break(business) -> tuple[str, str]:
    """The break shown in the forms: the first open day's break, or ('', '')."""
    hours = (getattr(business, "operational_hours", None) or {}) if business else {}
    for key, _label in WEEK_DAYS:
        entry = hours.get(key) or []
        if len(entry) >= 4 and clean_break(entry[2], entry[3]):
            return entry[2], entry[3]
    return "", ""
