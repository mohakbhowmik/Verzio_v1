"""
admin_messages.py — messages the booking bot didn't treat as a booking.

Every question, photo or "can I bring my daughter?" a customer sends is kept in
customer_messages (see customer_inbox.py). This page lists them across all
businesses so you can spot what customers ask most and decide what to build.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from customer_inbox import CustomerMessage
from database import Business, get_db

router = APIRouter(prefix="/admin/messages", tags=["admin-messages"])
templates = Jinja2Templates(directory="templates")

IST = timedelta(hours=5, minutes=30)
PAGE_SIZE = 300


@router.get("")
async def messages_page(request: Request, db: Session = Depends(get_db)):
    try:
        business_id = int(request.query_params.get("business") or 0)
    except ValueError:
        business_id = 0
    businesses = db.query(Business).order_by(Business.name.asc()).all()
    names = {b.id: b.name for b in businesses}

    query = db.query(CustomerMessage)
    if business_id:
        query = query.filter(CustomerMessage.business_id == business_id)
    rows = query.order_by(CustomerMessage.id.desc()).limit(PAGE_SIZE).all()

    messages = [{
        "when": (m.created_at + IST).strftime("%d %b, %I:%M %p"),
        "business": names.get(m.business_id, f"#{m.business_id}"),
        "customer": m.customer_name or "",
        "phone": m.phone,
        "body": m.body,
        "forwarded": m.forwarded,
    } for m in rows]

    return templates.TemplateResponse(
        request=request,
        name="admin_messages.html",
        context={
            "active_page": "messages",
            "messages": messages,
            "businesses": businesses,
            "business_id": business_id,
            "page_size": PAGE_SIZE,
        },
    )
