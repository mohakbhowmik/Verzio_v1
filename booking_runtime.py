"""
booking_runtime.py — WhatsApp interactive state machine for customer bookings.
Includes W5 (stale tap recovery), W6 (revalidation), and W11 (profile name capture).
"""
import os
import logging
from datetime import datetime, date, time, timedelta
from typing import Optional, Dict, Any, List
import httpx
from sqlalchemy.orm import Session

from database import Business, Service, Appointment, UserSession
from booking_engine import get_available_slots_for_day, validate_and_book

logger = logging.getLogger("VERZIO_BOOKING_RUNTIME")

META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")


def send_whatsapp_raw(payload: dict, phone_number_id: Optional[str] = None) -> bool:
    """Send a raw JSON payload to the WhatsApp Cloud API."""
    token = os.getenv("META_ACCESS_TOKEN", "")
    pid = phone_number_id or os.getenv("PHONE_NUMBER_ID", "")
    if not token or not pid:
        logger.warning("WhatsApp API credentials missing; message dispatch skipped.")
        return False

    url = f"https://graph.facebook.com/v20.0/{pid}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code not in (200, 201):
                logger.error(f"WhatsApp API error {resp.status_code}: {resp.text}")
                return False
            return True
    except Exception as exc:
        logger.error(f"Failed to post to WhatsApp API: {exc}")
        return False


def send_text_message(to: str, text: str, phone_number_id: Optional[str] = None) -> bool:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    return send_whatsapp_raw(payload, phone_number_id)


def send_button_message(to: str, body_text: str, buttons: List[Dict[str, str]], phone_number_id: Optional[str] = None) -> bool:
    """Send up to 3 interactive reply buttons."""
    button_rows = [
        {"type": "reply", "reply": {"id": btn["id"], "title": btn["title"][:20]}}
        for btn in buttons[:3]
    ]
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": button_rows},
        },
    }
    return send_whatsapp_raw(payload, phone_number_id)


def send_list_message(to: str, body_text: str, button_label: str, rows: List[Dict[str, str]], phone_number_id: Optional[str] = None) -> bool:
    """Send an interactive list message (up to 10 rows)."""
    list_rows = [
        {
            "id": r["id"],
            "title": r["title"][:24],
            "description": r.get("description", "")[:72],
        }
        for r in rows[:10]
    ]
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": button_label[:20],
                "sections": [{"title": "Options", "rows": list_rows}],
            },
        },
    }
    return send_whatsapp_raw(payload, phone_number_id)


class BookingRuntime:
    """State machine governing customer booking sessions."""

    def get_or_create_session(self, db: Session, phone_number: str, business_id: int) -> UserSession:
        session = db.query(UserSession).filter(
            UserSession.phone_number == phone_number,
            UserSession.business_id == business_id
        ).first()

        if not session:
            session = UserSession(
                phone_number=phone_number,
                business_id=business_id,
                current_state="START",
                selected_service_id=None,
                selected_date=None,
                selected_time=None
            )
            db.add(session)
            db.commit()
            db.refresh(session)
        return session

    def reset_session(self, db: Session, session: UserSession) -> None:
        session.current_state = "START"
        session.selected_service_id = None
        session.selected_date = None
        session.selected_time = None
        db.commit()

    def render_service_menu(self, db: Session, business: Business, to: str, prefix_msg: str = "") -> None:
        services = (
            db.query(Service)
            .filter(
                Service.business_id == business.id,
                Service.is_active == True,
                Service.is_deleted == False
            )
            .all()
        )

        if not services:
            send_text_message(to, f"Welcome to {business.name}. We are currently updating our service menu. Please check back shortly!")
            return

        body = f"{prefix_msg}\n\nWelcome to *{business.name}*!\nPlease choose a service to begin your booking:" if prefix_msg else f"Welcome to *{business.name}*!\nPlease choose a service to begin your booking:"

        if len(services) <= 3:
            buttons = [
                {"id": f"svc_{s.id}", "title": f"{s.name} (₹{int(s.price)})"}
                for s in services
            ]
            send_button_message(to, body, buttons)
        else:
            rows = [
                {
                    "id": f"svc_{s.id}",
                    "title": s.name,
                    "description": f"₹{int(s.price)} • {s.duration} mins"
                }
                for s in services
            ]
            send_list_message(to, body, "View Services", rows)

    def render_date_picker(self, business: Business, to: str) -> None:
        """Presents the next 7 calendar dates as an interactive list."""
        today = date.today()
        rows = []
        for i in range(7):
            d = today + timedelta(days=i)
            day_label = "Today" if i == 0 else ("Tomorrow" if i == 1 else d.strftime("%A"))
            date_str = d.strftime("%Y-%m-%d")
            rows.append({
                "id": f"date_{date_str}",
                "title": f"{day_label} ({d.strftime('%b %d')})",
                "description": f"Book for {d.strftime('%A, %d %B')}"
            })

        send_list_message(
            to=to,
            body_text=f"Please choose a date for your appointment at *{business.name}*:",
            button_label="Select Date",
            rows=rows
        )

    def render_time_picker(self, db: Session, business: Business, session: UserSession, to: str) -> None:
        service = db.query(Service).filter(Service.id == session.selected_service_id).first()
        target_date = session.selected_date

        slots = get_available_slots_for_day(db, business, target_date, service)
        if not slots:
            buttons = [{"id": "restart_flow", "title": "Pick Another Date"}]
            send_button_message(
                to,
                f"No open slots found for {target_date.strftime('%a, %b %d')}. Please choose another date:",
                buttons
            )
            session.current_state = "AWAITING_DATE"
            db.commit()
            return

        formatted_date = target_date.strftime("%a, %b %d")
        body_text = f"Available times for *{service.name}* on {formatted_date}:"

        if len(slots) <= 3:
            buttons = [{"id": f"time_{t}", "title": datetime.strptime(t, "%H:%M").strftime("%I:%M %p")} for t in slots]
            send_button_message(to, body_text, buttons)
        else:
            rows = [
                {
                    "id": f"time_{t}",
                    "title": datetime.strptime(t, "%H:%M").strftime("%I:%M %p"),
                    "description": f"{service.duration} mins slot"
                }
                for t in slots[:10]
            ]
            send_list_message(to, body_text, "Choose Time", rows)

    def process_interaction(
        self,
        db: Session,
        business_id: int,
        sender_phone: str,
        interaction_id: str,
        raw_text: str = "",
        profile_name: str = "Client"
    ) -> None:
        """Main dispatcher for incoming WhatsApp customer taps."""
        biz = db.query(Business).filter(Business.id == business_id).first()
        if not biz:
            logger.error(f"Interaction received for non-existent business #{business_id}")
            return

        if not biz.accepting_bookings:
            send_text_message(sender_phone, f"*{biz.name}* is currently not accepting automated bookings. Please contact the front desk directly.")
            return

        session = self.get_or_create_session(db, sender_phone, business_id)

        # W5: Manual or explicit restart
        if interaction_id in ("restart_flow", "menu", "hi", "hello", "start"):
            self.reset_session(db, session)
            self.render_service_menu(db, biz, sender_phone)
            session.current_state = "AWAITING_SERVICE"
            db.commit()
            return

        try:
            # 1. SERVICE SELECTION
            if interaction_id.startswith("svc_"):
                svc_id = int(interaction_id.split("_")[1])
                service = db.query(Service).filter(Service.id == svc_id, Service.business_id == biz.id).first()
                if not service:
                    raise ValueError("Service not found")

                session.selected_service_id = service.id
                session.current_state = "AWAITING_DATE"
                db.commit()
                self.render_date_picker(biz, sender_phone)
                return

            # 2. DATE SELECTION
            elif interaction_id.startswith("date_"):
                date_str = interaction_id.split("_")[1]
                selected_d = datetime.strptime(date_str, "%Y-%m-%d").date()

                session.selected_date = selected_d
                session.current_state = "AWAITING_TIME"
                db.commit()
                self.render_time_picker(db, biz, session, sender_phone)
                return

            # 3. TIME SELECTION
            elif interaction_id.startswith("time_"):
                time_str = interaction_id.split("_")[1]
                selected_t = datetime.strptime(time_str, "%H:%M").time()

                session.selected_time = selected_t
                session.current_state = "AWAITING_CONFIRMATION"
                db.commit()

                service = db.query(Service).filter(Service.id == session.selected_service_id).first()
                d_fmt = session.selected_date.strftime("%A, %d %B %Y")
                t_fmt = selected_t.strftime("%I:%M %p")

                summary = (
                    f"Please confirm your booking at *{biz.name}*:\n\n"
                    f"💇 *Service:* {service.name} (₹{int(service.price)})\n"
                    f"📅 *Date:* {d_fmt}\n"
                    f"⏰ *Time:* {t_fmt}\n"
                    f"⏳ *Duration:* {service.duration} mins\n\n"
                    f"Tap *Confirm* below to secure your appointment."
                )
                buttons = [
                    {"id": "confirm_booking", "title": "✅ Confirm"},
                    {"id": "restart_flow", "title": "❌ Cancel"}
                ]
                send_button_message(sender_phone, summary, buttons)
                return

            # 4. FINAL CONFIRMATION (Atomic validate_and_book with W6 revalidation)
            elif interaction_id == "confirm_booking":
                if not (session.selected_service_id and session.selected_date and session.selected_time):
                    raise ValueError("Missing session booking state")

                success, msg, appt = validate_and_book(
                    db=db,
                    business_id=biz.id,
                    service_id=session.selected_service_id,
                    customer_phone=sender_phone,
                    target_date=session.selected_date,
                    target_time=session.selected_time,
                    customer_name=profile_name
                )

                if not success:
                    # Slot expired or collision
                    send_button_message(
                        sender_phone,
                        f"⚠️ {msg}",
                        [{"id": "restart_flow", "title": "Pick Another Time"}]
                    )
                    return

                # Success response
                d_fmt = appt.appointment_date.strftime("%A, %d %B")
                t_fmt = appt.appointment_time.strftime("%I:%M %p")

                if appt.status == "confirmed":
                    final_msg = f"🎉 *Booking Confirmed!*\n\nWe look forward to seeing you at *{biz.name}* on {d_fmt} at {t_fmt}."
                else:
                    final_msg = f"⏳ *Booking Request Received!*\n\nYour appointment for {appt.service.name} on {d_fmt} at {t_fmt} has been sent to the manager for confirmation. We will notify you shortly."

                send_text_message(sender_phone, final_msg)

                # Reset session
                self.reset_session(db, session)
                return

            else:
                # Unrecognized interaction ID
                raise ValueError(f"Unknown interaction: {interaction_id}")

        except Exception as exc:
            # W5: Graceful stale-tap recovery
            logger.warning(f"W5: Stale or out-of-order interaction '{interaction_id}' from {sender_phone}: {exc}")
            self.reset_session(db, session)
            self.render_service_menu(
                db, biz, sender_phone,
                prefix_msg="⚠️ That selection has expired or was interrupted. Let's start fresh:"
            )
            session.current_state = "AWAITING_SERVICE"
            db.commit()