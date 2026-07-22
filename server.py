from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from routers.admin_businesses import router as business_router
from routers.admin_services import router as services_router



from activity_log import log_event

import os
from dotenv import load_dotenv

load_dotenv()

print("META TOKEN:", os.getenv("META_ACCESS_TOKEN"))
print("PHONE NUMBER ID:", os.getenv("PHONE_NUMBER_ID"))


import logging
import httpx
from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import Business, init_db, get_db, Appointment
from booking_runtime import BookingRuntime

print("ACCESS TOKEN:", os.getenv("META_ACCESS_TOKEN"))
app = FastAPI(title="Verzio Studio API")

app.include_router(business_router)
app.include_router(services_router)

templates = Jinja2Templates(directory="templates")

app.mount("/static", StaticFiles(directory="static"), name="static")

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

async def dispatch_whatsapp(
    to: str,
    tenant_id: str,
    payload: dict,
    db: Session = None,
    business_id: int = None
):
    access_token = os.getenv("META_ACCESS_TOKEN")
    phone_number_id = os.getenv("PHONE_NUMBER_ID")
    api_version = os.getenv("GRAPH_API_VERSION", "v23.0")

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # Base Meta Payload
    body = {
        "messaging_product": "whatsapp",
        "to": to
    }

    # Mapping Runtime response types to Meta Cloud API format
    msg_type = payload.get("type")

    if msg_type == "text":
        body["type"] = "text"
        body["text"] = {"body": payload["body"]}

    elif msg_type == "list":
        body["type"] = "interactive"
        body["interactive"] = {
            "type": "list",
            "header": {"type": "text", "text": payload.get("header", "")},
            "body": {"text": payload.get("body", "")},
            "action": {
                "button": payload.get("button", "Select"),
                "sections": payload.get("sections", [])
            }
        }

    elif msg_type == "buttons":
        body["type"] = "interactive"
        body["interactive"] = {
            "type": "button",
            "body": {"text": payload.get("body", "")},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": b["id"], "title": b["title"]}
                    } for b in payload.get("buttons", [])
                ]
            }
        }
    else:
        raise ValueError(f"Unsupported WhatsApp payload type: {msg_type}")

    print(f"\n========== OUTGOING {msg_type.upper()} REQUEST ==========")
    print(body)

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            headers=headers,
            json=body
        )

# ---- Activity Logging ----
        if db:
            if response.status_code == 200:
                log_event(
                    db=db,
                    event_type="whatsapp_sent",
                    status="success",
                    message=f"Sent {payload.get('type')} message to {to}",
                    business_id=business_id
                )
            else:
                log_event(
                    db=db,
                    event_type="whatsapp_failed",
                    status="error",
                    message=f"Meta API Error {response.status_code}: {response.text[:200]}",
                    business_id=business_id
                )

    print("\n========== META RESPONSE ==========")
    print(response.status_code)
    if response.status_code != 200:
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

        biz = db.query(Business).filter(
            Business.whatsapp_business_phone_number_id == tenant_id
        ).first()

        business_id = biz.id if biz else None

        log_event(
            db=db,
            event_type="webhook_received",
            status="info",
            message=f"Incoming {msg.get('type')} message",
            business_id=business_id
        )

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

                # Use modern SQLAlchemy get
                appt = db.get(Appointment, int(aid))

                if appt:
                    # 1. Update status in database
                    appt.status = (
                        "confirmed"
                        if act == "confirm"
                        else "cancelled"
                    )
                    db.commit()

                    if act == "confirm":
                        log_event(
                            db=db,
                            event_type="booking_confirmed",
                            status="success",
                            message=f"Appointment #{appt.id} confirmed",
                            business_id=appt.business_id
                        )
                    else:
                        log_event(
                            db=db,
                            event_type="booking_cancelled",
                            status="warning",
                            message=f"Appointment #{appt.id} cancelled",
                            business_id=appt.business_id
                        )
                    
                    print(f"✅ Appointment {aid} updated")

                    # 2. Determine message content
                    msg_text = (
                        "✅ Your appointment has been confirmed."
                        if act == "confirm"
                        else "❌ Unfortunately your booking could not be approved."
                    )

                    # 3. Dispatch notification to the CUSTOMER
                    # We use the customer_phone from the appt record and the current tenant_id
                    await dispatch_whatsapp(
                        to=appt.customer_phone,
                        tenant_id=tenant_id,
                        payload={
                            "type": "text",
                            "body": msg_text
                        },
                        db=db,
                        business_id=appt.business_id
                    )
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

            if isinstance(resp, dict) and "customer" in resp and "owner" in resp:

                # Send customer message
                await dispatch_whatsapp(
                    phone,
                    tenant_id,
                    resp["customer"],
                    db=db,
                    business_id=business_id
                )

                # Send owner message
                await dispatch_whatsapp(
                    resp["owner"]["recipient"],
                    tenant_id,
                    resp["owner"]["payload"],
                    db=db,
                    business_id=business_id
                )

            else:

                await dispatch_whatsapp(
                    phone,
                    tenant_id,
                    resp,
                    db=db,
                    business_id=business_id
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

            if isinstance(resp, dict) and "customer" in resp and "owner" in resp:

                await dispatch_whatsapp(
                    phone,
                    tenant_id,
                    resp["customer"]
                )

                await dispatch_whatsapp(
                    resp["owner"]["recipient"],
                    tenant_id,
                    resp["owner"]["payload"]
                )

            else:

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
        try:
            log_event(
                db,
                "webhook_failed",
                status="error",
                message=str(e)[:500]
            )
        except Exception:
            pass

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


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    db: Session = Depends(get_db)
):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "active_page": "dashboard"
        }
    )