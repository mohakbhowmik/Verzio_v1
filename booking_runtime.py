from sqlalchemy.orm import Session
from runtime_state import RuntimeStateManager
from booking_engine import VerzioSaaSEngine, BookingEngineException
from database import Business, Service
from datetime import datetime

class BookingRuntime:
    def __init__(self, db: Session):
        self.db = db
        self.state_mgr = RuntimeStateManager(db)
        self.engine = VerzioSaaSEngine()

    async def process_interaction(self, phone: str, tenant_id: str, interaction_id: str):
        session = self.state_mgr.get_or_create_session(phone, tenant_id)
        biz = self.engine.get_tenant_config(self.db, tenant_id)

        if interaction_id == "action_start":
            # FIX 1: Pass the phone number to advance state
            return self._render_services(phone, biz)

        elif session.step == "SELECT_SERVICE":
            svc_id = int(interaction_id.split("_")[1])
            self.state_mgr.update_session(phone, "SELECT_DATE", selected_service_id=svc_id)
            dates = self.engine.get_available_dates(self.db, biz)
            return self._render_dates(dates, phone)

        elif session.step == "SELECT_DATE":
            date_str = interaction_id.split("_")[1]
            self.state_mgr.update_session(phone, "SELECT_TIME", selected_date=date_str)
            target_date = datetime.strptime(date_str, "%Y-%m-%d")
            slots = self.engine.get_available_slots(self.db, biz, target_date)
            return self._render_slots(slots, phone)

        elif session.step == "SELECT_TIME":
            time_str = interaction_id.split("_")[1]
            self.state_mgr.update_session(phone, "CONFIRM", selected_time=time_str)
            return self._render_confirmation(session, phone)

        elif session.step == "CONFIRM":
            if interaction_id == "action_confirm":
                return self._finalize(phone, tenant_id, session)
            self.state_mgr.clear_session(phone)
            return {"type": "text", "body": "Cancelled."}

    def _render_services(self, phone, biz):
        # FIX 3: Filter by is_active == True
        svcs = self.db.query(Service).filter(
            Service.business_id == biz.id,
            Service.is_active == True,
            Service.is_deleted == False
        ).limit(10).all()

        if not svcs:
            self.state_mgr.clear_session(phone)
            return {"type": "text", "body": "Sorry, this business currently has no active services available."}
        
        rows = [{
            "id": f"svc_{s.id}",
            "title": s.name[:24],
            "description": f"₹{s.price:.0f}" if s.price is not None else f"{s.duration} mins"
        } for s in svcs]
        
        # FIX 1: Use customer phone to advance step
        self.state_mgr.update_session(phone, "SELECT_SERVICE")
        
        return {
            "type": "list", 
            "header": "Services", 
            "body": "Pick a service:", 
            "button": "Services", 
            "sections": [{"title": "Our Menu", "rows": rows}]
        }

    def _render_dates(self, dates, phone):
        if not dates:
            self.state_mgr.clear_session(phone)
            return {"type": "text", "body": "No booking dates are currently available. Please check back soon."}

        btns = [{"id": f"date_{d.isoformat()}", "title": d.strftime("%a %d %b")} for d in dates[:3]]
        return {"type": "buttons", "body": "Pick a date:", "buttons": btns}

    def _render_slots(self, slots, phone):
        if not slots:
            self.state_mgr.clear_session(phone)
            return {"type": "text", "body": "All slots for this date are fully booked or unavailable. Please message again to choose another date."}

        rows = [{"id": f"time_{s}", "title": s} for s in slots[:10]]
        return {"type": "list", "header": "Times", "body": "Pick a time:", "button": "Times", "sections": [{"title": "Slots", "rows": rows}]}

    def _render_confirmation(self, session, phone):
        # FIX 2: Modern SQLAlchemy session.get()
        svc = self.db.get(Service, session.selected_service_id)
        if svc is None:
            self.state_mgr.clear_session(phone)
            return {"type": "text", "body": "Sorry, the selected service is no longer available. Please start again."}

        body = f"Confirm: {svc.name} on {session.selected_date} at {session.selected_time}?"
        return {
            "type": "buttons", 
            "body": body, 
            "buttons": [
                {"id": "action_confirm", "title": "Confirm ✅"}, 
                {"id": "action_cancel", "title": "Cancel ❌"}
            ]
        }

    def _finalize(self, phone, tenant_id, session):
        try:
            dt = datetime.strptime(
                f"{session.selected_date} {session.selected_time}",
                "%Y-%m-%d %H:%M"
            )

            appt = self.engine.validate_and_book(
                self.db,
                tenant_id,
                dt,
                phone,
                session.selected_service_id
            )

            biz = self.db.get(Business, appt.business_id)
            svc = self.db.get(Service, appt.service_id)

            dt_str = appt.appointment_time.strftime("%A, %b %d")
            tm_str = appt.appointment_time.strftime("%I:%M %p")

            owner_payload = {
                "type": "buttons",
                "body": (
                    f"📅 New Booking Request\n\n"
                    f"Customer: {phone}\n"
                    f"Service: {svc.name}\n"
                    f"Date: {dt_str}\n"
                    f"Time: {tm_str}\n"
                    f"Booking ID: {appt.id}"
                ),
                "buttons": [
                    {
                        "id": f"confirm_{appt.id}",
                        "title": "Approve ✅"
                    },
                    {
                        "id": f"cancel_{appt.id}",
                        "title": "Reject ❌"
                    }
                ]
            }

            self.state_mgr.clear_session(phone)

            return {
                "customer": {
                    "type": "text",
                    "body": (
                        "✅ Booking request received.\n\n"
                        "Waiting for business approval."
                    )
                },
                "owner": {
                    "recipient": biz.manager_phone_number,
                    "payload": owner_payload
                }
            }

        except BookingEngineException as e:

            if e.error_code == "ERR_TENANT_LOCKED":
                message = "This business is temporarily not accepting bookings."

            elif e.error_code == "ERR_HOLIDAY":
                message = "The business is closed on the selected date."

            elif e.error_code == "ERR_CAPACITY":
                message = "Sorry, that time slot has just become full. Please choose another."

            elif e.error_code == "ERR_INVALID_CONFIG":
                message = "The business configuration is currently unavailable."

            else:
                message = "Unable to complete your booking."

            self.state_mgr.clear_session(phone)

            return {
                "type": "text",
                "body": message
            }