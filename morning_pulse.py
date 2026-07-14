"""
morning_pulse.py
================================================================================
VERZIO STUDIO — DAILY DISPATCHER & AUTHENTICATION TESTS
================================================================================
Queries the local SQLite database for pending slots and dispatches the live
approved Meta interactive template card to the target testing device.
"""
import os
import httpx
import asyncio
from datetime import datetime, date, time
from database import SessionLocal, Business, Appointment

# Your permanent Meta app access token
META_ACCESS_TOKEN = "EAAZAdv3p7VSQBRyZBDJ1DSo1HTPmNz1kHtgQwF7ZBHUTyqJzZCZB8GBwTuex9Ql3Wy6ZC7pn1rCpxNZBcQrnbUt7VmyKL0RVKRHMmZCWfp21XmHGQub6YZAnpMAvEVgoInRRVxDwSJIQpuzltEup3UIbZA6UEdgg2Js64ORzzlb5dWWO7ZCrjqguVCthUI4FdEjQrFE4QZDZD"

async def dispatch_interactive_confirmation(recipient_phone: str, appointment: Appointment, business_name: str, business_phone_id: str):
    url = f"https://graph.facebook.com/v17.0/{business_phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # 🌟 FIXED: Points to your active approved template 'appointment_confirmation'
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_phone,
        "type": "template",
        "template": {
            "name": "appointment_confirmation",
            "language": {
                "code": "en_US"
            },
"components": [
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": appointment.customer_name or "Client"
                        },
                        {
                            "type": "text",
                            "text": appointment.appointment_time.strftime("%I:%M %p")
                        }
                    ]
                }
            ]
        }
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            print(f"📡 API Dispatch Code for {recipient_phone}: {response.status_code}")
            if response.status_code != 200:
                print(f"⚠️ Meta Rejection Response Details: {response.text}")
            else:
                print(f"🚀 Template successfully pushed to handset!")
            return response.json()
        except Exception as api_err:
            print(f"❌ Failed connecting to Meta HTTP Network endpoints: {str(api_err)}")

async def run_morning_validation_routine():
    print("⚡ Starting Verzio Dispatch Engine Subsystem...")
    db = SessionLocal()
    try:
        # Pull everything pending for the current day window window
        today_start = datetime.combine(date.today(), time.min)
        today_end = datetime.combine(date.today(), time.max)
        
        todays_pending = db.query(Appointment).filter(
            Appointment.appointment_time >= today_start,
            Appointment.appointment_time <= today_end,
            Appointment.status == "pending"
        ).all()
        
        if not todays_pending:
            print("🍃 No pending appointments found for today in the database. Run seed.py first!")
            return

        print(f"🔍 Found {len(todays_pending)} pending slots requiring live authentication.")
        
        tasks = []
        for appt in todays_pending:
            business = db.query(Business).filter(Business.id == appt.business_id).first()
            if not business or not business.whatsapp_business_phone_number_id:
                print(f"⚠️ Skipping appointment {appt.id}: Missing Business metadata mapping link.")
                continue
                
            tasks.append(
                dispatch_interactive_confirmation(
                    recipient_phone=appt.customer_phone,
                    appointment=appt,
                    business_name=business.name,
                    business_phone_id=business.whatsapp_business_phone_number_id
                )
            )
            
        await asyncio.gather(*tasks)
        print("✅ Dispatch process loop ended.")
    except Exception as e:
        print(f"❌ Error during routine execution: {str(e)}")
    finally:
        db.close()

if __name__ == "__main__":
    asyncio.run(run_morning_validation_routine())