import os
import logging
import httpx
from fastapi import FastAPI, Depends, HTTPException, Request, Query
from sqlalchemy.orm import Session
from database import init_db, get_db, Appointment
from booking_runtime import BookingRuntime

app = FastAPI(title="Verzio Studio API")

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VERZIO_SERVER")

@app.on_event("startup")
def startup():
    init_db()

@app.on_event("startup")
def startup():
    init_db()

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "1.2.0"
    }

async def dispatch_whatsapp(to: str, tenant_id: str, payload: dict):
    """
    Placeholder for Meta API Graph calls.
    In Mission 3, this will use httpx to send actual JSON to Meta.
    """
    logger.info(f"DEBUG: Dispatching {payload.get('type')} UI to {to} for tenant {tenant_id}")

async def dispatch_whatsapp(to: str, tenant_id: str, payload: dict):
    """
    Placeholder for Meta API Graph calls.
    In Mission 3, this will use httpx to send actual JSON to Meta.
    """
    logger.info(f"DEBUG: Dispatching {payload.get('type')} UI to {to} for tenant {tenant_id}")

@app.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        
        # Safe extraction of the value object
        entries = data.get("entry", [])
        if not entries:
            return {"status": "ok"}
            
        changes = entries[0].get("changes", [])
        if not changes:
            return {"status": "ok"}
            
        val = changes[0].get("value", {})
        if "messages" not in val:
            return {"status": "ok"}
        
        msg = val["messages"][0]
        phone = msg["from"]
        tenant_id = val["metadata"]["phone_number_id"]
        
        runtime = BookingRuntime(db)

        # 1. HANDLE INTERACTIVE REPLIES (Buttons & Lists)
        if msg.get("type") == "interactive":
            itype = msg["interactive"].get("type")
            
            # Extract ID based on interactive type
            if itype == "button_reply":
                iid = msg["interactive"]["button_reply"]["id"]
            elif itype == "list_reply":
                iid = msg["interactive"]["list_reply"]["id"]
            else:
                return {"status": "unsupported_interactive_type"}

            # BRANCH A: MANAGER FLOW (Approval/Rejection)
            if iid.startswith(("confirm_", "cancel_")):
                act, aid = iid.split("_")
                # Modern SQLAlchemy session.get()
                appt = db.get(Appointment, int(aid))
                if appt:
                    appt.status = "confirmed" if act == "confirm" else "cancelled"
                    db.commit()
                    logger.info(f"Appointment {aid} updated to {appt.status} by manager")
                return {"status": "ok"}
            
            # BRANCH B: CUSTOMER JOURNEY
            resp = await runtime.process_interaction(phone, tenant_id, iid)
            await dispatch_whatsapp(phone, tenant_id, resp)

        # 2. HANDLE ENTRY POINT (Text Messages)
        elif msg.get("type") == "text":
            # Any text (Hi, Book, etc.) triggers the service list
            resp = await runtime.process_interaction(phone, tenant_id, "action_start")
            await dispatch_whatsapp(phone, tenant_id, resp)

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Webhook Processing Error: {e}", exc_info=True)
        # Always return 200/ok to Meta to prevent retry loops on malformed payloads
        return {"status": "error"}

@app.get("/webhook")
async def verify(
    mode: str = Query(None, alias="hub.mode"), 
    token: str = Query(None, alias="hub.verify_token"), 
    challenge: str = Query(None, alias="hub.challenge")
):
    """Handle Meta's Webhook verification handshake."""
    verify_token = os.getenv("VERZIO_VERIFY_TOKEN")
    if mode == "subscribe" and token == verify_token:
        return int(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")