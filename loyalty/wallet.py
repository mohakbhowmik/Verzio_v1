"""
loyalty/wallet.py: the customer's wallet page.

/w/<token> is a private link (sent on WhatsApp). No login: the random token is
the key, so the page never shows the full phone number. Customers can see
their points, watch the bars fill, and claim an unlocked reward right here
(they get the same 4-digit code as on WhatsApp).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import Business, get_db
from loyalty import engine as L
from loyalty.bot import local_clock
from loyalty.models import LoyaltyMember
from whatsapp_client import mask_phone

router = APIRouter(tags=["loyalty-wallet"])
templates = Jinja2Templates(directory="templates")

ENTRY_LABELS = {"visit": "Visit", "welcome": "Welcome bonus", "manual": "Visit", "adjust": "Adjustment",
                "claim": "Reward",
                "streak_daily": "Daily streak bonus", "streak_weekly": "Weekly streak bonus"}
NO_CACHE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"}


def _member(db: Session, token: str) -> tuple[LoyaltyMember | None, Business | None]:
    if not token or len(token) > 64:
        return None, None
    member = db.query(LoyaltyMember).filter(LoyaltyMember.wallet_token == token).first()
    if member is None:
        return None, None
    biz = db.get(Business, member.business_id)
    if biz is None or not L.is_enabled(db, biz):
        return None, None
    return member, biz


@router.get("/w/{token}", response_class=HTMLResponse)
def wallet_page(token: str, request: Request, db: Session = Depends(get_db)):
    member, biz = _member(db, token)
    if member is None:
        return templates.TemplateResponse(request=request, name="loyalty/wallet_missing.html",
                                          context={}, status_code=404, headers=NO_CACHE)
    settings = L.get_settings(db, biz.id)
    claim = L.active_claim(db, member)
    error = request.query_params.get("error")
    return templates.TemplateResponse(
        request=request,
        name="loyalty/wallet.html",
        context={
            "biz": biz,
            "member": member,
            "masked": mask_phone(member.phone),
            "progress": L.progress(db, member),
            "settings": settings,
            "running": L.running(db, biz),
            "claim": claim,
            "claim_until": local_clock(biz, claim.expires_at) if claim else None,
            "claim_seconds": max(0, int((claim.expires_at - datetime.utcnow()).total_seconds())) if claim else 0,
            "history": L.history(db, member, 15),
            "streak": L.streak_info(biz, settings, member),
            "labels": ENTRY_LABELS,
            "error": error[:120] if error else None,
            "token": token,
        },
        headers=NO_CACHE,
    )


@router.post("/w/{token}/claim/{reward_id}")
def wallet_claim(token: str, reward_id: int, db: Session = Depends(get_db)):
    member, biz = _member(db, token)
    if member is None:
        return RedirectResponse(url=f"/w/{token}", status_code=303)
    if not L.running(db, biz):
        return RedirectResponse(url=f"/w/{token}?error=Rewards+aren%27t+available+right+now.", status_code=303)
    try:
        L.start_claim(db, biz, member, reward_id)
    except L.LoyaltyError as err:
        db.rollback()
        from urllib.parse import quote_plus
        return RedirectResponse(url=f"/w/{token}?error={quote_plus(err.message)}", status_code=303)
    return RedirectResponse(url=f"/w/{token}#claim", status_code=303)
