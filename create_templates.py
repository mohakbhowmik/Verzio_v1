"""
create_templates.py — submit Verzio's message templates to a client's WhatsApp
Business Account (WABA) for Meta's approval, and check their status.

Run this once for every new client, right after Embedded Signup:

    python create_templates.py --waba-id 123456789012345            # submit all
    python create_templates.py --waba-id 123456789012345 --status   # check approval
    python create_templates.py --dry-run                             # print payloads only

Where to find the WABA ID: WhatsApp Manager -> Account tools -> the account's
details, or the `waba_id` returned by the Embedded Signup flow.

Uses META_ACCESS_TOKEN from .env (or --token). The token needs the
whatsapp_business_management permission for that WABA.

The template text comes from whatsapp_client.TEMPLATE_DEFINITIONS, the same
definitions the app uses when sending, so they can never drift apart.
Submitting a template that already exists is reported and skipped.
"""
import argparse
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import httpx

from whatsapp_client import GRAPH_API_VERSION, TEMPLATE_DEFINITIONS, TEMPLATE_LANG, TEMPLATE_NAMES


def build_request(key: str) -> dict:
    definition = TEMPLATE_DEFINITIONS[key]
    components = [{
        "type": "BODY",
        "text": definition["text"],
        "example": {"body_text": [definition["example"]]},
    }]
    if definition.get("buttons"):
        components.append({
            "type": "BUTTONS",
            "buttons": [{"type": "QUICK_REPLY", "text": label} for label in definition["buttons"]],
        })
    return {
        "name": TEMPLATE_NAMES[key],
        "language": TEMPLATE_LANG,
        "category": "UTILITY",
        "parameter_format": "POSITIONAL",
        "components": components,
    }


def submit(client: httpx.Client, waba_id: str) -> int:
    failures = 0
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{waba_id}/message_templates"
    for key in TEMPLATE_DEFINITIONS:
        payload = build_request(key)
        response = client.post(url, json=payload)
        data = response.json() if response.content else {}
        if response.status_code == 200:
            print(f"  ✅ {payload['name']}: submitted (status: {data.get('status', 'PENDING')}, "
                  f"category: {data.get('category', 'UTILITY')})")
            continue
        error = data.get("error", {})
        message = error.get("error_user_msg") or error.get("message") or response.text[:200]
        if "already exists" in message.lower() or error.get("error_subcode") == 2388024:
            print(f"  ↪️  {payload['name']}: already exists, skipped")
        else:
            failures += 1
            print(f"  ❌ {payload['name']}: {message}")
    return failures


def show_status(client: httpx.Client, waba_id: str) -> None:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{waba_id}/message_templates"
    response = client.get(url, params={"fields": "name,status,category,language", "limit": 100})
    response.raise_for_status()
    ours = set(TEMPLATE_NAMES.values())
    found = {t["name"]: t for t in response.json().get("data", []) if t["name"] in ours}
    for name in TEMPLATE_NAMES.values():
        t = found.get(name)
        if not t:
            print(f"  ⚠️  {name}: not submitted")
        else:
            flag = "✅" if t["status"] == "APPROVED" else ("❌" if t["status"] == "REJECTED" else "⏳")
            note = "  <- Meta changed the category; utility is expected" if t.get("category") != "UTILITY" else ""
            print(f"  {flag} {name}: {t['status']} ({t.get('category')}, {t.get('language')}){note}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--waba-id", help="The client's WhatsApp Business Account ID")
    parser.add_argument("--token", default=os.getenv("META_ACCESS_TOKEN", ""), help="Defaults to META_ACCESS_TOKEN")
    parser.add_argument("--status", action="store_true", help="Show approval status instead of submitting")
    parser.add_argument("--dry-run", action="store_true", help="Print the requests without calling Meta")
    args = parser.parse_args()

    if args.dry_run:
        for key in TEMPLATE_DEFINITIONS:
            print(json.dumps(build_request(key), indent=2, ensure_ascii=False))
        return 0

    if not args.waba_id:
        parser.error("--waba-id is required (or use --dry-run)")
    if not args.token:
        parser.error("META_ACCESS_TOKEN is not set (or pass --token)")

    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(headers=headers, timeout=30) as client:
        if args.status:
            print(f"Templates on WABA {args.waba_id}:")
            show_status(client, args.waba_id)
            return 0
        print(f"Submitting {len(TEMPLATE_DEFINITIONS)} templates to WABA {args.waba_id}:")
        failures = submit(client, args.waba_id)
    print("\nMeta usually reviews utility templates quickly. Check with --status.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
