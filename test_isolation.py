import hmac
import hashlib
import json
import uuid
import os
import requests
from dotenv import load_dotenv

# Load the matching environment variables from .env
load_dotenv()

APP_SECRET = os.getenv("META_APP_SECRET", "")
URL = "http://localhost:8000/webhook"
PHONE = "919999999999"

TENANT_A_ID = os.getenv("PHONE_NUMBER_ID", "1265387899980753")
TENANT_B_ID = "TENANT_B"

def send(phone_number_id: str, text: str):
    wamid = f"wamid.TEST_{uuid.uuid4().hex[:10]}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "123456789",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": str(phone_number_id)},
                    "contacts": [{"profile": {"name": "Test User"}, "wa_id": PHONE}],
                    "messages": [{
                        "from": PHONE,
                        "id": wamid,
                        "timestamp": "1720000000",
                        "type": "text",
                        "text": {"body": text}
                    }]
                },
                "field": "messages"
            }]
        }]
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    
    res = requests.post(URL, data=raw_body, headers={
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig
    })
    print(f"[{phone_number_id}] Status: {res.status_code} | Body: {res.text}")

print(f"Using APP_SECRET: {APP_SECRET[:6]}... (len: {len(APP_SECRET)})")
print("--- Sending message to Tenant A ---")
send(TENANT_A_ID, "hi")

print("\n--- Sending message to Tenant B with the SAME user phone ---")
send(TENANT_B_ID, "hi")