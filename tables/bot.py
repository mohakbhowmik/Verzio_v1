"""
tables/bot.py: Verzio Tables on WhatsApp.

server.py calls handle_message() for every incoming message (before loyalty).
For a business with Tables on, it returns True for everything except loyalty
commands (STAMP / WALLET / CLAIM), so the Book bot never answers on a Tables
number. Most messages get no reply at all: the venue's number is also the
owner's WhatsApp Business app (Coexistence), so the bot speaks only when a
guest asks for a table, says hi, wants to cancel, or taps one of our buttons.

What guests get
  "hi" / "book a table" / "table for 4 tonight?"  -> one "Book a table" link (/t/<token>)
  "cancel"                                        -> their upcoming booking with a Cancel button
  STOP                                            -> no more "book again" offers
  buttons                                         -> tbl_book, tbl_yes_<id>, tbl_cancel_<id>,
                                                     tbl_rate_<loved|ok|bad>_<id>, tbl_wl_<id>, tbl_stop
The manager gets tbl_ok_<id> / tbl_no_<id> (accept / decline a big group).

Every outgoing message that may be sent outside the 24-hour window passes an
approved template as well (whatsapp_client.TEMPLATE_DEFINITIONS, table_*).
"""
import logging
import os
import re

from sqlalchemy.orm import Session

from activity_log import log_event
from database import Business
from tables import engine as T
from tables.models import TablesBooking, TablesWaitlist
from whatsapp_client import (_first_name, build_template, canonical_phone, is_real_phone, mask_phone,
                             normalize_phone, send_whatsapp, window_open)

logger = logging.getLogger("VERZIO_TABLES_BOT")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://verzio.codes").rstrip("/")

_GREETINGS = {"hi", "hello", "hey", "hii", "hiii", "helo", "hlo", "hallo", "hola", "namaste", "namaskar",
              "good evening", "good afternoon", "good morning", "hi there", "hello there", "hey there", "menu", "start"}
_TABLE_WORDS = {"table", "tables", "book", "booking", "bookings", "reserve", "reservation", "reservations",
                "seat", "seats", "available", "availability", "slot", "slots"}
_CANCEL_WORDS = {"cancel", "cancellation"}
_STOP_WORDS = {"stop", "unsubscribe", "stop offers", "stop messages"}
_RESUME = {"book", "book a table", "table", "booking"}
MAX_INTENT_WORDS = 14

ID_RE = re.compile(r"tbl_(yes|cancel|ok|no|wl)_(\d+)")
RATE_RE = re.compile(r"tbl_rate_(loved|ok|bad)_(\d+)")


def _words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^\w\s]", " ", (text or "").lower()).split() if w]


def classify_text(body: str) -> str:
    """book | greeting | cancel | stop | other"""
    words = _words(body)
    joined = " ".join(words)
    if not words:
        return "other"
    if joined in _STOP_WORDS:
        return "stop"
    if joined in _GREETINGS:
        return "greeting"
    if len(words) <= MAX_INTENT_WORDS:
        if _CANCEL_WORDS & set(words):
            return "cancel"
        if _TABLE_WORDS & set(words):
            return "book"
    return "other"


def _interaction_id(msg: dict) -> str | None:
    if msg.get("type") == "button":
        return (msg.get("button") or {}).get("payload")
    interactive = msg.get("interactive") or {}
    kind = interactive.get("type")
    if kind in ("button_reply", "list_reply"):
        return (interactive.get(kind) or {}).get("id")
    return None


def _profile_name(val: dict, wa_id: str) -> str | None:
    contacts = (val or {}).get("contacts") or []
    for contact in contacts:
        if contact.get("wa_id") == wa_id:
            return (contact.get("profile") or {}).get("name")
    if len(contacts) == 1:
        return (contacts[0].get("profile") or {}).get("name")
    return None


def link_url(link) -> str:
    return f"{PUBLIC_BASE_URL}/t/{link.token}"


def _zone_name(b: TablesBooking) -> str:
    return b.zone.name if b.zone else "Table"


def _guests(n: int) -> str:
    return f"{n} {'person' if n == 1 else 'people'}"


def _manager(biz: Business) -> str | None:
    from booking_engine import owner_cannot_approve
    number = canonical_phone(biz.manager_phone_number, biz.timezone)
    if not is_real_phone(number) or owner_cannot_approve(biz):
        return None
    return number


def _pnid(biz: Business) -> str:
    return biz.whatsapp_business_phone_number_id


# ------------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------------

async def handle_message(db: Session, biz: Business, tenant_id: str, sender: str, msg: dict, val: dict) -> bool:
    """True = handled here (or deliberately left unanswered)."""
    settings = T.get_settings(db, biz.id)
    if not settings or not settings.enabled:
        return False
    from loyalty import bot as loyalty_bot
    if loyalty_bot.classify(msg)[0] is not None:
        return False                                    # STAMP / WALLET / CLAIM: Verzio Loyalty answers

    async def send(payload: dict, template: dict | None = None) -> bool:
        return await send_whatsapp(sender, tenant_id, payload, biz.id, template=template)

    try:
        mtype = msg.get("type")
        name = _profile_name(val, sender)
        if mtype in ("interactive", "button"):
            await _handle_tap(db, biz, settings, sender, _interaction_id(msg) or "", name, send)
            return True
        if mtype != "text":
            return True                                  # photos, voice notes...: the owner sees them in the app
        body = ((msg.get("text") or {}).get("body") or "").strip()
        intent = classify_text(body)
        from whatsapp_accounts import bot_paused, resume_bot
        if bot_paused(biz.id, sender):
            if " ".join(_words(body)) not in _RESUME:
                return True                              # the owner is chatting with this guest
            resume_bot(biz.id, sender)
            intent = "book"
        if intent == "stop":
            T.opt_out(db, biz.id, sender)
            log_event(db=db, event_type="tables_opt_out", status="info",
                      message=f"{mask_phone(sender)} opted out of book-again offers", business_id=biz.id)
            await send({"type": "text", "body": f"Done. {biz.name} won't send you offers any more. "
                                                f"You can still book a table any time: just say hi."})
        elif intent == "cancel":
            if not await _offer_cancel(db, biz, sender, send):
                return True                              # nothing booked: leave it to the owner
        elif intent in ("book", "greeting"):
            await send_link(db, biz, sender, name, send)
        return True
    except Exception as exc:
        db.rollback()
        logger.exception("Tables message failed for business_id=%s", biz.id)
        log_event(db=db, event_type="tables_failed", status="error", message=str(exc)[:500], business_id=biz.id)
        return True


async def send_link(db: Session, biz: Business, phone: str, name: str | None, send, party: int | None = None,
                    start=None, headline: str | None = None) -> bool:
    if not T.running(db, biz):
        return await send({"type": "text", "body": f"Sorry, {biz.name} isn't taking WhatsApp table bookings "
                                                   f"right now. Please call the venue."})
    link = T.new_link(db, biz, phone, name, party=party, start=start)
    first = _first_name(name, "")
    body = headline or (f"Hi{(' ' + first) if first else ''}! 👋 Book your table at {biz.name}: pick the date, "
                        f"time and where you'd like to sit on our floor plan. It takes a few taps.")
    body += f"\n\nThis link is just for you and works for {T.LINK_HOURS} hours."
    return await send({"type": "cta_url", "body": body[:1000], "display_text": "Book a table",
                       "url": link_url(link)})


async def _offer_cancel(db: Session, biz: Business, sender: str, send) -> bool:
    upcoming = T.upcoming_for_phone(db, biz, sender, T.business_now(biz))
    if not upcoming:
        return False
    b = upcoming[0]
    await send({"type": "buttons",
                "body": f"Your table at {biz.name}:\n\n{T.when_label(b.start_at)}\n{_guests(b.party)} · "
                        f"{_zone_name(b)}\n\nDo you want to cancel it?",
                "buttons": [{"id": f"tbl_cancel_{b.id}", "title": "Yes, cancel it"},
                            {"id": "tbl_book", "title": "Book another time"}]})
    return True


async def _handle_tap(db: Session, biz: Business, settings, sender: str, iid: str, name: str | None, send) -> None:
    if iid == "tbl_book":
        await send_link(db, biz, sender, name, send)
        return
    if iid == "tbl_stop":
        T.opt_out(db, biz.id, sender)
        log_event(db=db, event_type="tables_opt_out", status="info",
                  message=f"{mask_phone(sender)} opted out of book-again offers", business_id=biz.id)
        await send({"type": "text", "body": f"Done. {biz.name} won't send you offers any more."})
        return
    rate = RATE_RE.fullmatch(iid)
    if rate:
        await _rating(db, biz, settings, sender, rate.group(1), int(rate.group(2)), send)
        return
    m = ID_RE.fullmatch(iid)
    if not m:
        return
    action, ref = m.group(1), int(m.group(2))
    if action in ("ok", "no"):
        await manager_decision(db, biz, sender, ref, "approve" if action == "ok" else "decline")
        return
    if action == "wl":
        entry = db.get(TablesWaitlist, ref)
        if entry is None or entry.business_id != biz.id or normalize_phone(entry.phone) != normalize_phone(sender):
            return
        await send_link(db, biz, sender, name or entry.name, send, party=entry.party, start=entry.start_at,
                        headline=f"Here's your link for {biz.name}. {T.clock(entry.start_at)} is preselected; "
                                 f"tap a green area to book it.")
        return
    booking = T.own_booking(db, biz, ref)
    if booking is None or normalize_phone(booking.phone) != normalize_phone(sender):
        logger.warning("Tables tap %s from a different number %s; ignored", iid, mask_phone(sender))
        return
    if action == "yes":
        if booking.status in ("confirmed", "pending"):
            booking.guest_reply = "coming"
            db.commit()
            await send({"type": "text", "body": f"Great, see you at {T.clock(booking.start_at)}! 🥂"})
        else:
            await send({"type": "text", "body": f"This booking is {T.STATUS_LABELS[booking.status].lower()}. "
                                                f"Say hi to book a new table."})
        return
    if action == "cancel":
        if booking.status not in ("pending", "confirmed"):
            await send({"type": "text", "body": f"This booking is already {T.STATUS_LABELS[booking.status].lower()}."})
            return
        booking.status = "cancelled"
        booking.guest_reply = "cancelled"
        db.commit()
        log_event(db=db, event_type="tables_cancelled", status="warning",
                  message=f"Guest cancelled table #{booking.id} ({T.when_label(booking.start_at)}, "
                          f"{booking.party} people)", business_id=biz.id)
        await send({"type": "buttons",
                    "body": f"Your table for {T.when_label(booking.start_at)} is cancelled. Thanks for letting us know!",
                    "buttons": [{"id": "tbl_book", "title": "Book another time"}]})


async def _rating(db, biz, settings, sender, rating: str, booking_id: int, send) -> None:
    booking = T.own_booking(db, biz, booking_id)
    if booking is None or normalize_phone(booking.phone) != normalize_phone(sender):
        return
    first_answer = booking.rating is None
    booking.rating = rating
    db.commit()
    if rating == "loved":
        if settings.google_review_url:
            await send({"type": "cta_url", "display_text": "Leave a review", "url": settings.google_review_url,
                        "body": f"So glad you had a great time! 🙌 Would you leave {biz.name} a quick Google review? "
                                f"It really helps."})
        else:
            await send({"type": "text", "body": f"So glad you had a great time! See you again soon at {biz.name}. 🙌"})
        return
    await send({"type": "text", "body": "Thank you for telling us. We're sorry it wasn't perfect: the manager "
                                        "will read this, and you can reply here with any details."})
    if first_answer:
        log_event(db=db, event_type="tables_feedback", status="warning",
                  message=f"{booking.name or mask_phone(sender)} rated their visit '{rating}'", business_id=biz.id)
        manager = _manager(biz)
        if manager and window_open(biz.id, manager):
            await send_whatsapp(manager, _pnid(biz), {"type": "text", "body":
                f"Feedback for {biz.name}: {booking.name or 'a guest'} (+{booking.phone}) said "
                f"\"{'It was OK' if rating == 'ok' else 'Not great'}\" about {T.when_label(booking.start_at)}. "
                f"Reply to them on WhatsApp: https://wa.me/{booking.phone}"}, biz.id)


# ------------------------------------------------------------------------
# Messages sent when something happens (page, portal, scheduler)
# ------------------------------------------------------------------------

def _details(b: TablesBooking) -> str:
    return f"{T.when_label(b.start_at)}\n{_guests(b.party)} · {_zone_name(b)}" + (f"\nName: {b.name}" if b.name else "")


async def notify_booked(db: Session, biz: Business, b: TablesBooking) -> None:
    """Right after a guest books on the page."""
    if b.status == "pending":
        await send_whatsapp(b.phone, _pnid(biz), {"type": "text", "body":
            f"Thanks! Your request for a table at {biz.name} is with the manager:\n\n{_details(b)}\n\n"
            f"We'll message you here as soon as it's confirmed."}, biz.id)
        await ask_manager(db, biz, b)
        return
    await send_whatsapp(b.phone, _pnid(biz), {"type": "buttons", "body":
        f"✅ Your table at {biz.name} is booked!\n\n{_details(b)}\n\nWe'll send a reminder on the day. "
        f"Plans changed? Tap Cancel and the table goes to someone else.",
        "buttons": [{"id": f"tbl_cancel_{b.id}", "title": "Cancel booking"}]},
        biz.id, template=confirmed_template(biz, b))


def confirmed_template(biz: Business, b: TablesBooking) -> dict:
    return build_template("table_confirmed", [_first_name(b.name), biz.name, T.when_label(b.start_at),
                                              _guests(b.party), _zone_name(b)])


async def ask_manager(db: Session, biz: Business, b: TablesBooking) -> bool:
    manager = _manager(biz)
    if not manager:
        return False
    who = f"{b.name or 'Guest'} (+{b.phone})"
    payload = {"type": "buttons",
               "body": f"🍸 New table request for {biz.name}\n\nGuest: {who}\n{_details(b)}\n\n"
                       f"Big groups need your OK. Accept or decline:",
               "buttons": [{"id": f"tbl_ok_{b.id}", "title": "Accept ✅"},
                           {"id": f"tbl_no_{b.id}", "title": "Decline ❌"}]}
    template = build_template("table_request", [biz.name, who, T.when_label(b.start_at), _guests(b.party),
                                                _zone_name(b)], button_payloads=[f"tbl_ok_{b.id}", f"tbl_no_{b.id}"])
    return await send_whatsapp(manager, _pnid(biz), payload, biz.id, template=template)


async def manager_decision(db: Session, biz: Business, sender: str, booking_id: int, action: str) -> None:
    """Accept / Decline tapped on WhatsApp. Only this business's manager may do it."""
    manager = canonical_phone(biz.manager_phone_number, biz.timezone)
    if normalize_phone(sender) != manager:
        log_event(db=db, event_type="manager_action_blocked", status="warning",
                  message=f"Table action on #{booking_id} from unauthorised number {mask_phone(sender)}",
                  business_id=biz.id)
        return
    try:
        booking, _ = T.change_status(db, biz, booking_id, action)
    except T.TablesError as err:
        await send_whatsapp(sender, _pnid(biz), {"type": "text", "body": f"Table #{booking_id}: {err.message}"}, biz.id)
        return
    await tell_guest_status(db, biz, booking)
    await send_whatsapp(sender, _pnid(biz), {"type": "text", "body":
        f"Table #{booking.id} {'accepted' if action == 'approve' else 'declined'}. The guest has been told."}, biz.id)


async def tell_guest_status(db: Session, biz: Business, b: TablesBooking) -> bool:
    """Tell the guest their booking was confirmed, declined or cancelled by the venue."""
    if not is_real_phone(b.phone):
        return False
    first, when = _first_name(b.name), T.when_label(b.start_at)
    if b.status == "confirmed":
        return await send_whatsapp(b.phone, _pnid(biz), {"type": "buttons", "body":
            f"✅ Good news! {biz.name} confirmed your table.\n\n{_details(b)}\n\nSee you then!",
            "buttons": [{"id": f"tbl_cancel_{b.id}", "title": "Cancel booking"}]},
            biz.id, template=confirmed_template(biz, b))
    if b.status == "declined":
        return await send_whatsapp(b.phone, _pnid(biz), {"type": "buttons", "body":
            f"Sorry {first}, {biz.name} couldn't confirm your table for {when}. Would you like to try another time?",
            "buttons": [{"id": "tbl_book", "title": "Pick another time"}]},
            biz.id, template=build_template("table_declined", [first, biz.name, when]))
    if b.status == "cancelled":
        return await send_whatsapp(b.phone, _pnid(biz), {"type": "buttons", "body":
            f"Hi {first}, your table at {biz.name} for {when} has been cancelled. Reply here if you have questions.",
            "buttons": [{"id": "tbl_book", "title": "Book another time"}]},
            biz.id, template=build_template("table_cancelled", [first, biz.name, when]))
    return False
