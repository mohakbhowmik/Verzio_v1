"""
loyalty/admin.py: Admin -> Loyalty. Switch loyalty on/off per business and
choose the mode. Protected by the /admin login like every other admin page.

- Off (default): nothing changes for the business.
- On, "with booking": the booking bot works as before; STAMP / WALLET / CLAIM
  messages go to loyalty.
- On, "loyalty only": every message on that number goes to loyalty (a cafe
  with no bookings). The owner portal shows only Loyalty and Settings.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from activity_log import log_event
from database import Business, get_db
from loyalty import engine as L
from loyalty.models import LoyaltyMember, LoyaltySettings

router = APIRouter(prefix="/admin/loyalty", tags=["admin-loyalty"])
templates = Jinja2Templates(directory="templates")


@router.get("")
def admin_loyalty(request: Request, db: Session = Depends(get_db)):
    businesses = db.query(Business).order_by(Business.name.asc()).all()
    settings = {s.business_id: s for s in db.query(LoyaltySettings).all()}
    members = dict(db.query(LoyaltyMember.business_id, func.count(LoyaltyMember.id))
                   .group_by(LoyaltyMember.business_id).all())
    return templates.TemplateResponse(
        request=request, name="admin_loyalty.html",
        context={"active_page": "loyalty", "businesses": businesses, "settings": settings, "members": members,
                 "saved": request.query_params.get("saved")},
    )


@router.post("/{business_id}")
def admin_loyalty_save(business_id: int, db: Session = Depends(get_db),
                       enabled: str = Form(""), mode: str = Form("with_booking")):
    biz = db.get(Business, business_id)
    if biz is None:
        return RedirectResponse(url="/admin/loyalty", status_code=303)
    row = L.ensure_settings(db, biz)
    row.enabled = enabled in ("on", "true", "1")
    row.mode = "only" if mode == "only" else "with_booking"
    if not row.whatsapp_number:
        row.whatsapp_number = L._connected_number(biz)
    db.commit()
    log_event(db=db, event_type="loyalty_switched", status="info",
              message=f"Loyalty {'ON' if row.enabled else 'OFF'} ({row.mode.replace('_', ' ')})", business_id=biz.id)
    return RedirectResponse(url=f"/admin/loyalty?saved={biz.id}", status_code=303)
