"""
loyalty/admin.py: Admin -> Products. One row per business, one switch per product.

- Verzio Book: on/off. Its plan (Basic/Pro/Trial) stays in Admin -> Subscriptions.
- Verzio Loyalty: on/off, plus its own plan (Trial / Active / Suspended), paid-until
  date and monthly price. Independent of the Book plan.
- Verzio Tables: on/off, plus its own plan (same pattern). Tables and Book can't
  both be on for one number (both would answer "hi"); Loyalty works with either.

How the switches map to what the business gets:
  Book on,  Loyalty on   -> booking bot as usual; STAMP / WALLET / CLAIM go to loyalty
  Book off, Loyalty on   -> "loyalty only": the whole number is loyalty, no Book portal
  Book on,  Loyalty off  -> Verzio Book only (exactly as before loyalty existed)
  Tables on               -> table booking bot; Book is off; Loyalty can be on too
  nothing on             -> refused: use Deactivate in Businesses to switch a business off

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
from tables import engine as T
from tables.models import TablesSettings

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
    tables = {t.business_id: t for t in db.query(TablesSettings).all()}
    rows = []
    for b in businesses:
        s = settings.get(b.id)
        t = tables.get(b.id)
        loy_on = bool(s and s.enabled)
        tbl_on = bool(t and t.enabled)
        rows.append({
            "biz": b,
            "book_on": not (loy_on and s.mode == "only") and not tbl_on,
            "tbl_on": tbl_on,
            "tbl": t,
            "tbl_plan": T.plan_info(t) if t else None,
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
                        loyalty_amount: str = Form("799"), tables_on: str = Form(""),
                        tables_plan: str = Form("trial"), tables_paid_until: str = Form(""),
                        tables_amount: str = Form("4999")):
    biz = db.get(Business, business_id)
    if biz is None:
        return RedirectResponse(url="/admin/products", status_code=303)
    book, loyalty = book_on in ("on", "1", "true"), loyalty_on in ("on", "1", "true")
    tables = tables_on in ("on", "1", "true")
    if book and tables:
        return RedirectResponse(url=f"/admin/products?id={biz.id}&err=Verzio+Book+and+Verzio+Tables+can%27t+both+"
                                    f"be+on+for+one+WhatsApp+number.+Switch+one+off.", status_code=303)
    if not book and not loyalty and not tables:
        return RedirectResponse(url=f"/admin/products?id={biz.id}&err=A+business+needs+at+least+one+product.+"
                                    f"To+switch+it+off+completely%2C+use+Deactivate+in+Businesses.", status_code=303)
    paid_until = (loyalty_paid_until or "").strip()
    if paid_until:
        try:
            date.fromisoformat(paid_until)
        except ValueError:
            return RedirectResponse(url=f"/admin/products?id={biz.id}&err=Paid-until+must+be+a+date.", status_code=303)
    tbl_paid_until = (tables_paid_until or "").strip()
    if tbl_paid_until:
        try:
            date.fromisoformat(tbl_paid_until)
        except ValueError:
            return RedirectResponse(url=f"/admin/products?id={biz.id}&err=Paid-until+must+be+a+date.", status_code=303)
    try:
        amount = max(0, int((loyalty_amount or "0").strip() or 0))
        tbl_amount = max(0, int((tables_amount or "0").strip() or 0))
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
    tbl = T.get_settings(db, biz.id)
    if tables or tbl is not None:
        tbl = T.ensure_settings(db, biz)
        tbl.enabled = tables
        tbl.plan = tables_plan if tables_plan in T.PLAN_LABELS else "trial"
        tbl.paid_until = tbl_paid_until or None
        tbl.monthly_amount = tbl_amount
    db.commit()
    products = " + ".join(p for p, on in (("Book", book), ("Loyalty", loyalty), ("Tables", tables)) if on)
    log_event(db=db, event_type="products_changed", status="info",
              message=f"Products: {products}; Loyalty plan {L.PLAN_LABELS[row.plan]}"
                      + (f" until {row.paid_until}" if row.paid_until else "")
                      + (f"; Tables plan {T.PLAN_LABELS[tbl.plan]}" if tbl is not None and tables else ""),
              business_id=biz.id)
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
