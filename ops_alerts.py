"""
ops_alerts.py
================================================================================
VERZIO STUDIO: ALERTS FOR YOU (THE VERZIO TEAM), NOT FOR CLIENTS
================================================================================
Sends short alerts to a Telegram group through a Telegram bot. Free, no Meta
rules, works even when WhatsApp is the thing that's broken.

Server .env:
    TELEGRAM_BOT_TOKEN=123456:ABC...   from @BotFather (keep it secret)
    TELEGRAM_CHAT_ID=-1001234567890    the group's id (negative for groups)

Test from the server:
    docker compose exec app python ops_alerts.py "Hello from Verzio"

Never raises: an alert that can't be sent is logged and ignored.
"""
import logging
import os
import sys

import httpx

logger = logging.getLogger("VERZIO_OPS_ALERTS")


def configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip())


async def notify(text: str, urgent: bool = True) -> bool:
    """Send one alert. urgent=False arrives silently (no sound). Returns True if delivered."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                "chat_id": chat_id,
                "text": text[:4000],
                "disable_web_page_preview": True,
                "disable_notification": not urgent,
            })
        if resp.status_code == 200:
            return True
        # Telegram's description says what's wrong (e.g. "chat not found"); never log the token.
        try:
            detail = resp.json().get("description", "")
        except ValueError:
            detail = ""
        logger.warning("Telegram alert not sent (%s): %s", resp.status_code, detail[:200])
    except httpx.HTTPError as exc:
        logger.warning("Telegram alert not sent: %s", type(exc).__name__)
    return False


if __name__ == "__main__":
    import asyncio
    message = " ".join(sys.argv[1:]) or "Test alert from Verzio ✅"
    if not configured():
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not both set in .env")
        raise SystemExit(1)
    ok = asyncio.run(notify(message))
    print("Sent ✅" if ok else "Not sent: check the bot is in the group and the chat id is right (see logs).")
    raise SystemExit(0 if ok else 1)
