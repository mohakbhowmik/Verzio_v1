"""
loyalty/bot.py: loyalty messages on WhatsApp.

server.py calls handle_message() for every incoming message of a business that
has loyalty switched on. It returns True when the message was a loyalty message
(and has been answered), False to let the booking bot handle it as usual.

What customers can send:
  STAMP <code>        from the counter QR: +points (once per day)
  WALLET / POINTS     their balance, progress bars and wallet link
  CLAIM               claim an unlocked reward -> 4-digit code for the counter
  JOIN <code>         a friend's invite link (Loyalty Plus referrals)
  INVITE              their own invite message to forward
  STOP / START        no more birthday offers / offers back on (members only)
  14 Aug              their birthday, right after we asked for it
  buttons             loy_wallet, loy_claim, loy_claim_<reward id>, loy_invite, loy_stop

In "loyalty only" mode (a cafe with no bookings) every message is handled
here: greetings get the wallet, anything else gets a short note at most
every HELP_HOURS (the owner reads and answers it in the WhatsApp Business app).

Every reply is a response to a message the customer just sent, so it's inside
WhatsApp's 24-hour window: no templates are needed.
"""
import logging
import os
import re
from datetime import datetime, timedelta

import pytz
from sqlalchemy.orm import Session

from activity_log import log_event
from database import Business
from loyalty import engine as L
from whatsapp_client import mask_phone, normalize_phone, send_whatsapp

logger = logging.getLogger("VERZIO_LOYALTY_BOT")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://verzio.codes").rstrip("/")
HELP_HOURS = 12
MAX_BODY = 1000                    # WhatsApp allows 1024 characters in a button message

STAMP_RE = re.compile(r"\bstamp\b\W*([a-z0-9]{4,10})\b", re.I)
WALLET_WORDS = {"wallet", "my wallet", "points", "my points", "point", "balance", "rewards", "reward",
                "my rewards", "loyalty", "stamps", "my stamps"}
CLAIM_WORDS = {"claim", "claim reward", "redeem", "claim my reward"}
CLAIM_ID_RE = re.compile(r"loy_claim_(\d+)")
JOIN_RE = re.compile(r"\bjoin\b\W*([a-z0-9]{4,10})\b", re.I)
INVITE_WORDS = {"invite", "refer", "invite friends", "refer a friend", "invite a friend"}
STOP_WORDS = {"stop", "unsubscribe", "stop offers", "stop messages"}
START_WORDS = {"start", "start offers", "subscribe"}


# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------

def wallet_url(member) -> str:
    return f"{PUBLIC_BASE_URL}/w/{member.wallet_token}"


def local_clock(biz: Business, utc_naive: datetime) -> str:
    try:
        tz = pytz.timezone(biz.timezone or "UTC")
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC
    return pytz.UTC.localize(utc_naive).astimezone(tz).strftime("%I:%M %p").lstrip("0")


def _profile_name(val: dict, wa_id: str) -> str | None:
    contacts = (val or {}).get("contacts") or []
    for contact in contacts:
        if contact.get("wa_id") == wa_id:
            return (contact.get("profile") or {}).get("name")
    if len(contacts) == 1:
        return (contacts[0].get("profile") or {}).get("name")
    return None


def _interaction_id(msg: dict) -> str | None:
    if msg.get("type") == "button":
        return (msg.get("button") or {}).get("payload")
    interactive = msg.get("interactive") or {}
    kind = interactive.get("type")
    if kind in ("button_reply", "list_reply"):
        return (interactive.get(kind) or {}).get("id")
    return None


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def rewards_block(db: Session, member) -> str:
    lines = []
    for p in L.progress(db, member)[:6]:
        status = "✅ Ready to claim!" if p.unlocked else f"{p.have}/{p.need}"
        lines.append(f"*{p.reward.name}*\n{p.bar} {status}")
    return "\n\n".join(lines)


def tier_line(settings, member) -> str:
    tier = L.tier_for(settings, member.visits)
    nxt = L.next_tier(settings, member.visits)
    parts = []
    if tier.key:
        parts.append(f"{tier.emoji} {tier.label} member: +{tier.bonus}% points")
    if nxt:
        need = nxt[1]
        parts.append(f"{need} more visit{'' if need == 1 else 's'} to {nxt[0].label}")
    return " · ".join(parts)


def _summary(db: Session, biz: Business, member, headline: str) -> str:
    body = f"{headline}\n\nWallet: *{member.points} points*"
    line = tier_line(L.get_settings(db, biz.id), member)
    if line:
        body += f"\n{line}"
    block = rewards_block(db, member)
    if block:
        body += "\n\n" + block
    nxt = next((p for p in L.progress(db, member) if not p.unlocked), None)
    if nxt:
        body += f"\n\n{nxt.missing} more points to {nxt.reward.name}."
    return body[:MAX_BODY]


def _wallet_buttons(db: Session, member) -> list[dict]:
    buttons = []
    if any(p.unlocked for p in L.progress(db, member)):
        buttons.append({"id": "loy_claim", "title": "Claim reward"})
    buttons.append({"id": "loy_wallet", "title": "My wallet"})
    settings = L.get_settings(db, member.business_id)
    if settings and settings.referral_on and L.plus_on(settings) and L.is_real_phone(settings.whatsapp_number or ""):
        buttons.append({"id": "loy_invite", "title": "Invite friends"})
    return buttons


# ------------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------------

def classify(msg: dict) -> tuple[str | None, str | None]:
    """(action, argument) for a loyalty message, or (None, None)."""
    mtype = msg.get("type")
    if mtype in ("interactive", "button"):
        iid = _interaction_id(msg) or ""
        if iid == "loy_wallet":
            return "wallet", None
        if iid == "loy_claim":
            return "claim", None
        if iid == "loy_invite":
            return "invite", None
        if iid == "loy_stop":
            return "stop", None
        m = CLAIM_ID_RE.fullmatch(iid)
        if m:
            return "claim_one", m.group(1)
        return None, None
    if mtype == "text":
        body = ((msg.get("text") or {}).get("body") or "").strip()
        m = STAMP_RE.search(body)
        if m:
            return "stamp", m.group(1)
        m = JOIN_RE.search(body)
        if m:
            return "join", m.group(1)
        words = _words(body)
        if words in WALLET_WORDS:
            return "wallet", None
        if words in CLAIM_WORDS:
            return "claim", None
        if words in INVITE_WORDS:
            return "invite", None
    return None, None


async def handle_message(db: Session, biz: Business, tenant_id: str, sender: str, msg: dict, val: dict) -> bool:
    """True = handled here (loyalty message, or loyalty-only business)."""
    settings = L.get_settings(db, biz.id)
    if not settings or not settings.enabled:
        return False
    only = settings.mode == "only"
    action, arg = classify(msg)
    if action is None:
        # Stateful replies that only make sense for a loyalty member: STOP / START
        # for offers, and their birthday right after we asked for it.
        action, arg = _member_reply(db, biz, sender, msg)
    if action is None and not only:
        return False

    async def send(payload: dict) -> bool:
        return await send_whatsapp(sender, tenant_id, payload, biz.id)

    try:
        if not L.running(db, biz):
            await send({"type": "text", "body": f"Sorry, rewards at {biz.name} aren't available right now."})
            return True
        name = _profile_name(val, sender)
        if action == "stamp":
            await _stamp(db, biz, sender, arg, name, send)
        elif action == "wallet":
            await _wallet(db, biz, sender, send)
        elif action == "claim":
            await _claim(db, biz, sender, None, send)
        elif action == "claim_one":
            await _claim(db, biz, sender, int(arg), send)
        elif action == "join":
            await _join(db, biz, sender, arg, send)
        elif action == "invite":
            await _invite(db, biz, sender, send)
        elif action in ("stop", "start"):
            await _offers(db, biz, sender, action == "stop", send)
        elif action == "birthday":
            await _birthday(db, biz, sender, arg, send)
        else:
            await _loyalty_only_other(db, biz, sender, msg, send)
    except Exception as exc:
        db.rollback()
        logger.exception("Loyalty message failed for business_id=%s", biz.id)
        log_event(db=db, event_type="loyalty_failed", status="error", message=str(exc)[:500], business_id=biz.id)
        await send({"type": "text", "body": "Sorry, something went wrong with your rewards. Please try again."})
    return True


# ------------------------------------------------------------------------
# Actions
# ------------------------------------------------------------------------

async def _stamp(db, biz, sender, code, name, send) -> None:
    try:
        result = L.earn_visit(db, biz, sender, code, name)
    except L.LoyaltyError as err:
        db.rollback()
        if err.message == "wrong_code":
            logger.info("Wrong loyalty code from %s for business_id=%s", mask_phone(sender), biz.id)
            await send({"type": "text", "body":
                        "Hmm, that code didn't work. Please scan the QR code at the counter again."})
        else:
            await send({"type": "text", "body": err.message})
        return

    member = result.member
    settings = L.get_settings(db, biz.id)
    where = f"{biz.name} ({result.outlet.name})" if result.outlet else biz.name
    if result.already_today:
        headline = (f"You've already collected today's points at {where} 🙂 Come back tomorrow for more."
                    + streak_teaser(result.streak))
    else:
        headline = f"☕ +{result.earned} points at {where}!"
        if result.welcome:
            headline += f"\n(Includes a {result.welcome}-point welcome bonus.)"
        if result.boost > 1:
            headline += f"\n⚡ {result.boost}x points right now!"
        if result.tier and result.tier.key:
            headline += f"\n{result.tier.emoji} {result.tier.label} bonus: +{result.tier.bonus}%"
        if result.referral_bonus:
            friend = (result.referrer.name or "your friend").split()[0] if result.referrer else "your friend"
            headline += f"\n🤝 +{result.referral_bonus} for joining with {friend}'s invite"
        if result.daily_bonus:
            headline += f"\n🔥 {result.member.daily_streak}-day streak! +{result.daily_bonus} bonus"
        if result.weekly_bonus:
            headline += f"\n📅 {result.member.weekly_streak} weeks in a row! +{result.weekly_bonus} bonus"
        headline += streak_teaser(result.streak)
        if result.tier_up:
            t = result.tier_up
            headline += f"\n\n{t.emoji} You're now a *{t.label}* member! +{t.bonus}% points on every visit."
        for reward in result.newly_unlocked or []:
            headline += f"\n\n🎉 You unlocked *{reward.name}*! Tap Claim reward when you'd like it."
        log_event(db=db, event_type="loyalty_earned", status="success",
                  message=f"{mask_phone(sender)} earned {result.earned} points (balance {member.points})"
                          + (f" at {result.outlet.name}" if result.outlet else ""),
                  business_id=biz.id)
    if result.bill_code:
        headline += (f"\n\n🧾 Bill code: *{result.bill_code.code}*\nShow it when you pay to get "
                     f"{settings.points_per_100} points per ₹100.")
    if result.ask_review:
        headline += f"\n\n⭐ Enjoying {biz.name}? A quick Google review helps us a lot: {settings.review_link}"
    elif result.ask_birthday:
        headline += "\n\n🎂 Get a birthday treat from us: reply with your birthday, like *14 Aug*."
    await send({"type": "buttons", "body": _summary(db, biz, member, headline),
                "buttons": _wallet_buttons(db, member)})
    if result.referrer and result.referrer_bonus:
        await tell_referrer(db, biz, result.referrer, member, result.referrer_bonus)


async def tell_referrer(db, biz, referrer, friend, bonus: int) -> None:
    """'Your friend came!' to the one who invited (free inside 24 h, else a Utility template)."""
    from whatsapp_client import _first_name, build_template
    first, friend_first = _first_name(referrer.name), _first_name(friend.name, "Your friend")
    body = (f"🤝 {friend_first} just made their first visit to {biz.name} with your invite! "
            f"+{bonus} points for you.\n\nWallet: {referrer.points} points.")
    template = build_template("loyalty_referral", [first, friend_first, biz.name, bonus, referrer.points],
                              button_payloads=["loy_wallet"])
    await send_whatsapp(referrer.phone, biz.whatsapp_business_phone_number_id,
                        {"type": "buttons", "body": body, "buttons": [{"id": "loy_wallet", "title": "My wallet"}]},
                        biz.id, template=template)


async def _wallet(db, biz, sender, send) -> None:
    member = L.find_member(db, biz.id, sender)
    if member is None:
        await send({"type": "text", "body":
                    f"You don't have any points at {biz.name} yet. On your next visit, scan the QR code "
                    f"at the counter to start collecting rewards. ☕"})
        return
    body = _summary(db, biz, member, f"Your rewards at {biz.name}")
    body = (body + "\n\nOpen your wallet to see your progress and claim rewards.")[:MAX_BODY]
    await send({"type": "cta_url", "body": body, "display_text": "Open my wallet", "url": wallet_url(member)})


async def _claim(db, biz, sender, reward_id, send) -> None:
    member = L.find_member(db, biz.id, sender)
    if member is None:
        await _wallet(db, biz, sender, send)
        return
    unlocked = [p.reward for p in L.progress(db, member) if p.unlocked]
    if reward_id is None:
        if not unlocked:
            nxt = next(iter(L.progress(db, member)), None)
            hint = (f" {nxt.missing} more points to {nxt.reward.name}." if nxt else "")
            await send({"type": "buttons", "body":
                        f"Nothing to claim yet. You have {member.points} points.{hint}",
                        "buttons": [{"id": "loy_wallet", "title": "My wallet"}]})
            return
        if len(unlocked) > 1:
            rows = [{"id": f"loy_claim_{r.id}", "title": r.name[:24], "description": f"{r.points} points"}
                    for r in unlocked[:10]]
            await send({"type": "list", "header": "Claim a reward",
                        "body": f"You have {member.points} points. Which reward would you like?",
                        "button": "Choose reward", "sections": [{"title": "Unlocked", "rows": rows}]})
            return
        reward_id = unlocked[0].id
    try:
        claim = L.start_claim(db, biz, member, reward_id)
    except L.LoyaltyError as err:
        db.rollback()
        await send({"type": "text", "body": err.message})
        return
    log_event(db=db, event_type="loyalty_claim_started", status="info",
              message=f"{mask_phone(sender)} claimed {claim.reward_name} (code {claim.code})", business_id=biz.id)
    await send({"type": "text", "body": claim_message(biz, claim)})


def _member_reply(db, biz, sender, msg) -> tuple[str | None, str | None]:
    if msg.get("type") != "text":
        return None, None
    member = L.find_member(db, biz.id, sender)
    if member is None:
        return None, None
    body = ((msg.get("text") or {}).get("body") or "").strip()
    words = _words(body)
    if words in STOP_WORDS:
        return "stop", None
    if words in START_WORDS and member.offers_off:
        return "start", None
    if member.birthday_asked and not member.birthday and L.plus_on(L.get_settings(db, biz.id)):
        mmdd = L.parse_birthday(body)
        if mmdd:
            return "birthday", mmdd
    return None, None


async def _join(db, biz, sender, code, send) -> None:
    settings = L.get_settings(db, biz.id)
    status, referrer = L.register_referral(db, biz, sender, code)
    first = (referrer.name or "Your friend").split()[0] if referrer else "Your friend"
    pts = settings.referral_points if settings else 0
    if status in ("ok", "already"):
        log_event(db=db, event_type="loyalty_invite", status="info",
                  message=f"{mask_phone(sender)} joined with an invite", business_id=biz.id)
        body = (f"Welcome to {biz.name} rewards! 👋 {first} invited you.\n\nOn your first visit, scan the QR "
                f"code at the counter and you'll both get +{pts} points.")
    elif status == "member":
        body = f"You're already collecting points at {biz.name} 🙂 Reply WALLET to see them."
    elif status == "self":
        body = "That's your own invite link 🙂 Forward it to friends: you both get points on their first visit."
    else:
        body = (f"Welcome to {biz.name} rewards! ☕\n\nOn each visit, scan the QR code at the counter to "
                f"collect points, then swap them for treats.")
    await send({"type": "text", "body": body})


async def _invite(db, biz, sender, send) -> None:
    settings = L.get_settings(db, biz.id)
    member = L.find_member(db, biz.id, sender)
    if not settings or not settings.referral_on or not L.plus_on(settings):
        await send({"type": "text", "body": f"Invites aren't on at {biz.name} right now."})
        return
    if member is None:
        await _wallet(db, biz, sender, send)
        return
    link = L.invite_link(db, settings, member)
    if not link:
        await send({"type": "text", "body": "Sorry, invites aren't ready yet. Please try again later."})
        return
    await send({"type": "text", "body":
                f"Invite friends to {biz.name}: when they make their first visit, you both get "
                f"+{settings.referral_points} points. Forward the next message to them 👇"})
    await send({"type": "text", "body":
                f"Join me at {biz.name} rewards on WhatsApp! ☕ Tap here to get +{settings.referral_points} "
                f"bonus points on your first visit: {link}"})


async def _offers(db, biz, sender, stop: bool, send) -> None:
    member = L.find_member(db, biz.id, sender)
    if member is None:
        await send({"type": "text", "body": "OK, you won't get offers from us."})
        return
    L.stop_offers(db, member, stop)
    if stop:
        log_event(db=db, event_type="loyalty_offers_stopped", status="info",
                  message=f"{mask_phone(sender)} replied STOP", business_id=biz.id)
        await send({"type": "text", "body":
                    f"Done: no more offers from {biz.name}. You still collect points as usual. "
                    f"Reply START any time to get offers again."})
    else:
        await send({"type": "text", "body": f"Welcome back! You'll get birthday treats from {biz.name} again. 🎂"})


async def _birthday(db, biz, sender, mmdd, send) -> None:
    member = L.find_member(db, biz.id, sender)
    if member is None:
        return
    L.set_birthday(db, member, mmdd)
    settings = L.get_settings(db, biz.id)
    gift = f" On your birthday you'll get +{settings.birthday_points} points" if settings and settings.birthday_on else ""
    await send({"type": "buttons", "body":
                f"🎂 Saved: {L.birthday_label(mmdd)}.{gift} from {biz.name}. See you soon!",
                "buttons": [{"id": "loy_wallet", "title": "My wallet"}]})


def streak_teaser(info) -> str:
    """'Come back tomorrow for a +15 streak bonus.' lines, for streaks that are on."""
    if info is None:
        return ""
    lines = []
    if info.daily_on and info.next_daily_bonus:
        lines.append(f"Come back {info.next_visit_day} for a +{info.next_daily_bonus} streak bonus.")
    if info.weekly_on and info.next_weekly_bonus:
        when = "next week" if info.visited_this_week else "this week"
        lines.append(f"Visit again {when} for a +{info.next_weekly_bonus} weekly bonus.")
    return ("\n" + "\n".join(lines)) if lines else ""


def claim_message(biz: Business, claim) -> str:
    return (f"🎁 *{claim.reward_name}*\n\nShow this code at the counter:\n\n*{claim.code}*\n\n"
            f"Valid until {local_clock(biz, claim.expires_at)}. Your points are only used "
            f"when staff give you the reward.")


async def _loyalty_only_other(db, biz, sender, msg, send) -> None:
    """Loyalty-only business, and not a loyalty command."""
    from customer_inbox import classify_text
    from whatsapp_accounts import bot_paused
    if msg.get("type") == "text":
        body = ((msg.get("text") or {}).get("body") or "").strip()
        if classify_text(body) == "book" and not bot_paused(biz.id, sender):     # "hi", "hello", "menu"...
            member = L.find_member(db, biz.id, sender)
            if member:
                await send({"type": "buttons", "body": _summary(db, biz, member, f"Hi! Your rewards at {biz.name}"),
                            "buttons": _wallet_buttons(db, member)})
            else:
                await send({"type": "text", "body":
                            f"Hi! Welcome to {biz.name} rewards. ☕\n\nOn each visit, scan the QR code at the "
                            f"counter to collect points, then swap them for treats. Reply WALLET any time to "
                            f"see your points."})
            return
    if msg.get("type") not in ("text", "image", "audio", "video", "document", "sticker", "location", "contacts"):
        return                                      # reactions and system events: ignore
    if bot_paused(biz.id, sender):
        return                                      # the owner is chatting with them
    key, now = (biz.id, normalize_phone(sender)), datetime.utcnow()
    last = _last_help.get(key)
    if last and now - last < timedelta(hours=HELP_HOURS):
        return
    _last_help[key] = now
    await send({"type": "text", "body":
                f"Thanks for your message! Someone from {biz.name} will reply here soon.\n\n"
                f"Reply WALLET to see your reward points."})


# (business_id, phone) -> when we last sent the loyalty-only note. In memory is
# enough: one app worker, and after a restart one extra note is harmless.
_last_help: dict[tuple[int, str], datetime] = {}
