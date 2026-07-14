import os
import logging
from fastapi import FastAPI, Depends, HTTPException, Request, Query, status
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from database import init_db, get_db, Business, Appointment
from booking_engine import process_persistent_booking, BookingEngineException

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("VERZIO_SERVER")

app = FastAPI(title="Verzio Studio API")

# Secrets
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("VERZIO_VERIFY_TOKEN")

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/health")
async def health():
    return {"status": "healthy", "version": "1.2.0"}

@app.get("/webhook")
async def verify(mode: str = Query(None, alias="hub.mode"), 
                 token: str = Query(None, alias="hub.verify_token"), 
                 challenge: str = Query(None, alias="hub.challenge")):
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge)
    raise HTTPException(status_code=403)

@app.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return {"status": "ignored"}

        msg = entry["messages"][0]
        from_phone = msg["from"]
        tenant_id = entry["metadata"]["phone_number_id"]
        
        # Handle Interactive Buttons (Confirm/Cancel from your diagram)
        if msg.get("type") == "interactive":
            btn_id = msg["interactive"]["button_reply"]["id"]
            action, appt_id = btn_id.split("_")
            appt = db.query(Appointment).filter(Appointment.id == int(appt_id)).first()
            if appt:
                appt.status = "confirmed" if action == "confirm" else "cancelled"
                db.commit()
                logger.info(f"Booking {appt_id} updated to {appt.status}")

        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))