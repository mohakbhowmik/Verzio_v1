"""
preview.py
================================================================================
VERZIO STUDIO: WHO SEES NEW SCREENS AND FEATURES FIRST
================================================================================
New interface changes go to Verzio Test first, then to clients one by one
(see the project's release rules). One setting in the server .env decides who:

    VERZIO_PREVIEW=918262858396            a WhatsApp number (digits, any format)
    VERZIO_PREVIEW=2                       a business id (up to 6 digits)
    VERZIO_PREVIEW=918262858396,917066674848
    VERZIO_PREVIEW=all                     everyone
    (unset or empty)                       nobody: everyone sees the current screens

A business matches if its id is listed, or any of the numbers passed in
(e.g. its Loyalty WhatsApp number) is listed.
"""
import os


def _tokens() -> tuple[bool, set[str]]:
    raw = os.getenv("VERZIO_PREVIEW", "")
    everyone = False
    tokens = set()
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if part == "all":
            everyone = True
            continue
        digits = "".join(ch for ch in part if ch.isdigit())
        if digits:
            tokens.add(digits)
    return everyone, tokens


def preview_on(business=None, *numbers) -> bool:
    everyone, tokens = _tokens()
    if everyone:
        return True
    if not tokens:
        return False
    if business is not None and getattr(business, "id", None) is not None and str(business.id) in tokens:
        return True
    for number in numbers:
        digits = "".join(ch for ch in str(number or "") if ch.isdigit())
        if len(digits) > 6 and (digits in tokens or digits[-10:] in {t[-10:] for t in tokens if len(t) > 6}):
            return True
    return False
