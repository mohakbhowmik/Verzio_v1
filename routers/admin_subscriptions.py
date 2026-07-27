from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import Business, Subscription, get_db

router = APIRouter(
    prefix="/admin/subscriptions",
    tags=["Admin Subscriptions"]
)

templates = Jinja2Templates(directory="templates")


def _format_expiry_date(raw_value):
    if not raw_value:
        return None

    if isinstance(raw_value, date):
        return raw_value

    try:
        return datetime.fromisoformat(str(raw_value)).date()
    except ValueError:
        return None


def _subscription_status(subscription: Subscription | None, expiry_date: date | None) -> str:
    if not subscription:
        return "No subscription"

    if expiry_date and expiry_date < date.today():
        return "Expired"

    if subscription.status:
        return subscription.status.replace("_", " ").title()

    return "Active"


def _plan_label(subscription: Subscription | None) -> str:
    if not subscription or not subscription.plan:
        return "Trial"

    return subscription.plan.replace("_", " ").title()


def _latest_subscription_for_business(business: Business, db: Session) -> Subscription | None:
    subscriptions = getattr(business, "subscriptions", None)

    if isinstance(subscriptions, list):
        if not subscriptions:
            return None

        return max(
            subscriptions,
            key=lambda sub: getattr(sub, "updated_at", None) or getattr(sub, "id", 0),
        )

    subscription = getattr(business, "subscription", None)
    if isinstance(subscription, list):
        if not subscription:
            return None

        return max(
            subscription,
            key=lambda sub: getattr(sub, "updated_at", None) or getattr(sub, "id", 0),
        )

    if subscription is not None:
        return subscription

    return (
        db.query(Subscription)
        .filter(Subscription.business_id == business.id)
        .order_by(Subscription.updated_at.desc(), Subscription.id.desc())
        .first()
    )


@router.get("")
def subscriptions_page(request: Request, db: Session = Depends(get_db)):
    businesses = db.query(Business).order_by(Business.name.asc()).all()

    subscription_rows = []
    for business in businesses:
        subscription = _latest_subscription_for_business(business, db)
        expiry_date = _format_expiry_date(subscription.next_billing_date) if subscription else None

        subscription_rows.append({
            "business": business,
            "subscription": subscription,
            "plan_type": _plan_label(subscription),
            "expiry_date": expiry_date,
            "status_label": _subscription_status(subscription, expiry_date),
        })

    return templates.TemplateResponse(
        request=request,
        name="subscriptions.html",
        context={
            "active_page": "subscriptions",
            "subscription_rows": subscription_rows,
            "plan_options": ["Trial", "Pro", "Suspended"],
        }
    )


@router.post("/{business_id}")
def update_subscription(
    business_id: int,
    plan_type: str = Form(...),
    expiry_date: str = Form(""),
    db: Session = Depends(get_db),
):
    business = db.get(Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")

    subscription = _latest_subscription_for_business(business, db)
    if not subscription:
        subscription = Subscription(business_id=business_id, plan=plan_type)
        db.add(subscription)

    subscription.plan = plan_type
    subscription.status = "suspended" if plan_type.lower() == "suspended" else "active"
    subscription.next_billing_date = expiry_date or None

    db.commit()

    return RedirectResponse(url="/admin/subscriptions", status_code=303)