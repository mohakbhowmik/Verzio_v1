"""
loyalty/admin.py: Admin -> Products. One row per business, one switch per product.

- Verzio Book: on/off. Its plan (Basic/Pro/Trial) stays in Admin -> Subscriptions.
- Verzio Loyalty: on/off, plus its own plan (Trial / Active / Suspended), paid-until
  date and monthly price. Independent of the Book plan.

How the switches map to what the business gets:
  Book on,  Loyalty on   -> booking bot as usual; STAMP / WALLET / CLAIM go to loyalty
  Book off, Loyalty on   -> "loyalty only": the whole number is loyalty, no Book portal
  Book on,  Loyalty off  -> Verzio Book only (exactly as before loyalty existed)
  Book off, Loyalty off  -> refused: use Deactivate in Businesses to switch a business off

Protected by the /admin login like every other admin page.
"""
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from activity_log import log_event
from database import Business, get_db
from loyalty import engine as L
from loyalty.models import LoyaltyMember, LoyaltySettings

router = APIRouter(tags=["admin-products"])
templates = Jinja2Templates(directory="templates")


def _book_plan(biz: Business, db: Session):
    try:
        from plans import plan_for
        return plan_for(biz, db)
    except Exception:
        return None


@router.get("/admin/products")
def admin_products(request: Request, db: Session = Depends(get_db)):
    businesses = db.query(Business).order_by(Business.name.asc()).all()
    settings = {s.business_id: s for s in db.query(LoyaltySettings).all()}
    members = dict(db.query(LoyaltyMember.business_id, func.count(LoyaltyMember.id))
                   .group_by(LoyaltyMember.business_id).all())
    rows = []
    for b in businesses:
        s = settings.get(b.id)
        loy_on = bool(s and s.enabled)
        rows.append({
            "biz": b,
            "book_on": not (loy_on and s.mode == "only"),
            "book_plan": _book_plan(b, db),
            "loy_on": loy_on,
            "loy": s,
            "loy_plan": L.plan_info(s) if s else None,
            "members": members.get(b.id, 0),
        })
    return templates.TemplateResponse(
        request=request, name="admin_products.html",
        context={"active_page": "products", "rows": rows,
                 "saved": request.query_params.get("saved"), "err": request.query_params.get("err"),
                 "err_id": request.query_params.get("id")},
    )


@router.post("/admin/products/{business_id}")
def admin_products_save(business_id: int, db: Session = Depends(get_db),
                        book_on: str = Form(""), loyalty_on: str = Form(""),
                        loyalty_plan: str = Form("trial"), loyalty_paid_until: str = Form(""),
                        loyalty_amount: str = Form("799")):
    biz = db.get(Business, business_id)
    if biz is None:
        return RedirectResponse(url="/admin/products", status_code=303)
    book, loyalty = book_on in ("on", "1", "true"), loyalty_on in ("on", "1", "true")
    if not book and not loyalty:
        return RedirectResponse(url=f"/admin/products?id={biz.id}&err=A+business+needs+at+least+one+product.+"
                                    f"To+switch+it+off+completely%2C+use+Deactivate+in+Businesses.", status_code=303)
    paid_until = (loyalty_paid_until or "").strip()
    if paid_until:
        try:
            date.fromisoformat(paid_until)
        except ValueError:
            return RedirectResponse(url=f"/admin/products?id={biz.id}&err=Paid-until+must+be+a+date.", status_code=303)
    try:
        amount = max(0, int((loyalty_amount or "0").strip() or 0))
    except ValueError:
        return RedirectResponse(url=f"/admin/products?id={biz.id}&err=Price+must+be+a+whole+number.", status_code=303)

    row = L.ensure_settings(db, biz)
    row.enabled = loyalty
    row.mode = "with_booking" if book else "only"
    row.plan = loyalty_plan if loyalty_plan in L.PLAN_LABELS else "trial"
    row.paid_until = paid_until or None
    row.monthly_amount = amount
    if not row.whatsapp_number:
        row.whatsapp_number = L._connected_number(biz)
    db.commit()
    products = " + ".join(p for p, on in (("Book", book), ("Loyalty", loyalty)) if on)
    log_event(db=db, event_type="products_changed", status="info",
              message=f"Products: {products}; Loyalty plan {L.PLAN_LABELS[row.plan]}"
                      + (f" until {row.paid_until}" if row.paid_until else ""), business_id=biz.id)
    return RedirectResponse(url=f"/admin/products?saved={biz.id}", status_code=303)


# ------------------------------------------------------------------------
# Older address (v1/v2): /admin/loyalty -> Products
# ------------------------------------------------------------------------

@router.get("/admin/loyalty")
def admin_loyalty_old(request: Request):
    return RedirectResponse(url="/admin/products", status_code=303)


@router.post("/admin/loyalty/{business_id}")
def admin_loyalty_save(business_id: int, db: Session = Depends(get_db),
                       enabled: str = Form(""), mode: str = Form("with_booking")):
    """Kept so old bookmarks and scripts still work: loyalty on/off + mode only."""
    biz = db.get(Business, business_id)
    if biz is None:
        return RedirectResponse(url="/admin/products", status_code=303)
    row = L.ensure_settings(db, biz)
    row.enabled = enabled in ("on", "true", "1")
    row.mode = "only" if mode == "only" else "with_booking"
    if not row.whatsapp_number:
        row.whatsapp_number = L._connected_number(biz)
    db.commit()
    log_event(db=db, event_type="loyalty_switched", status="info",
              message=f"Loyalty {'ON' if row.enabled else 'OFF'} ({row.mode.replace('_', ' ')})", business_id=biz.id)
    return RedirectResponse(url=f"/admin/products?saved={biz.id}", status_code=303)
