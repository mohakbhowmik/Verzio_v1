import os
from dotenv import load_dotenv

load_dotenv()

print("VERIFY TOKEN:", os.getenv("VERZIO_VERIFY_TOKEN"))
print("PHONE NUMBER ID:", os.getenv("PHONE_NUMBER_ID"))


import logging
import httpx
from fastapi import FastAPI, Depends, HTTPException, Request, Query
from sqlalchemy.orm import Session
from database import init_db, get_db, Appointment
from booking_runtime import BookingRuntime

print("ACCESS TOKEN:", os.getenv("META_ACCESS_TOKEN"))
app = FastAPI(title="Verzio Studio API")

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VERZIO_SERVER")

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
    access_token = os.getenv("META_ACCESS_TOKEN")
    phone_number_id = os.getenv("PHONE_NUMBER_ID")
    api_version = os.getenv("GRAPH_API_VERSION", "v23.0")

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {
            "body": "Hello from Verzio! 🚀"
        }
    }

    print("\n========== OUTGOING REQUEST ==========")
    print(body)

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            headers=headers,
            json=body
        )

    print("\n========== META RESPONSE ==========")
    print(response.status_code)
    print(response.text)

@app.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    try:
        print("\n" + "=" * 80)
        print("🚀 WEBHOOK RECEIVED")
        print("=" * 80)

        data = await request.json()

        logger.info(f"Received webhook: {data}")
        print("Raw Payload:")
        print(data)

        # ---------------------------------------------------------------------
        # Extract payload safely
        # ---------------------------------------------------------------------
        entries = data.get("entry", [])
        if not entries:
            print("❌ No entries found")
            return {"status": "ok"}

        changes = entries[0].get("changes", [])
        if not changes:
            print("❌ No changes found")
            return {"status": "ok"}

        val = changes[0].get("value", {})
        if "messages" not in val:
            print("❌ No messages in payload")
            return {"status": "ok"}

        msg = val["messages"][0]

        phone = msg.get("from")
        tenant_id = val.get("metadata", {}).get("phone_number_id")

        print(f"📞 Phone: {phone}")
        print(f"🏢 Tenant ID: {tenant_id}")
        print(f"📩 Message Type: {msg.get('type')}")

        if msg.get("type") == "text":
            print(f"💬 Text: {msg['text']['body']}")

        if not tenant_id:
            print("❌ Tenant ID missing")
            return {"status": "ok"}

        runtime = BookingRuntime(db)

        print("✅ BookingRuntime initialized")

        # ---------------------------------------------------------------------
        # INTERACTIVE
        # ---------------------------------------------------------------------
        if msg.get("type") == "interactive":

            print("➡ Processing Interactive Message")

            interactive = msg["interactive"]
            itype = interactive.get("type")

            print(f"Interactive Type: {itype}")

            if itype == "button_reply":
                iid = interactive["button_reply"]["id"]

            elif itype == "list_reply":
                iid = interactive["list_reply"]["id"]

            else:
                print("❌ Unsupported interactive type")
                return {"status": "unsupported_interactive_type"}

            print(f"Interaction ID: {iid}")

            # Manager buttons
            if iid.startswith(("confirm_", "cancel_")):

                print("➡ Manager Action")

                act, aid = iid.split("_")

                appt = db.get(Appointment, int(aid))

                if appt:
                    appt.status = (
                        "confirmed"
                        if act == "confirm"
                        else "cancelled"
                    )

                    db.commit()

                    print(f"✅ Appointment {aid} updated")

                return {"status": "ok"}

            print("➡ Calling process_interaction()")

            resp = await runtime.process_interaction(
                phone,
                tenant_id,
                iid
            )

            print("✅ process_interaction() completed")
            print(resp)

            print("➡ Calling dispatch_whatsapp()")

            await dispatch_whatsapp(
                phone,
                tenant_id,
                resp
            )

            print("✅ dispatch_whatsapp() completed")

        # ---------------------------------------------------------------------
        # TEXT
        # ---------------------------------------------------------------------
        elif msg.get("type") == "text":

            print("➡ Text flow started")

            print("Calling process_interaction(action_start)...")

            resp = await runtime.process_interaction(
                phone,
                tenant_id,
                "action_start"
            )

            print("✅ process_interaction returned")
            print("Response:")
            print(resp)

            print("Calling dispatch_whatsapp...")

            await dispatch_whatsapp(
                phone,
                tenant_id,
                resp
            )

            print("✅ dispatch_whatsapp completed")

        else:
            print(f"⚠ Unsupported message type: {msg.get('type')}")

        print("=" * 80)
        print("🎉 WEBHOOK FINISHED SUCCESSFULLY")
        print("=" * 80)

        return {"status": "ok"}

    except Exception as e:

        print("\n" + "=" * 80)
        print("🔥 WEBHOOK EXCEPTION")
        print("=" * 80)

        import traceback
        traceback.print_exc()

        logger.error(
            f"Webhook Processing Error: {e}",
            exc_info=True
        )

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