import os
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
import stripe
from pydantic import BaseModel
from typing import Optional
import redis.asyncio as aioredis
from app.core import settings

router = APIRouter(prefix="/payments", tags=["Payments"])

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client = aioredis.from_url(redis_url, decode_responses=True)


class CreateCheckoutRequest(BaseModel):
    email: str
    success_url: str
    cancel_url: str


class CheckoutSessionResponse(BaseModel):
    checkout_url: str
    session_id: str


class StatusResponse(BaseModel):
    premium_active: bool
    email: Optional[str] = None


@router.post("/checkout/create-session", response_model=CheckoutSessionResponse)
async def create_checkout_session(request: CreateCheckoutRequest):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe não configurado.")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            customer_email=request.email,
            line_items=[
                {
                    "price": PRICE_ID,
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url=request.success_url,
            cancel_url=request.cancel_url,
            metadata={
                "email": request.email,
            },
        )
        return {"checkout_url": session.url, "session_id": session.id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/webhook")
async def stripe_webhook(request: Request):
    body = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload=body,
            sig_header=sig_header,
            secret=WEBHOOK_SECRET,
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Webhook inválido.")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        email = session.get("metadata", {}).get("email")
        if email:
            await redis_client.setex(f"premium:status:{email}", 31536000, "active")
            await redis_client.setex(f"premium:token:{email}", 31536000, email)

    return {"status": "ok"}


@router.get("/status", response_model=StatusResponse)
async def get_premium_status(email: str):
    status = await redis_client.get(f"premium:status:{email}")
    if status == "active":
        return {"premium_active": True, "email": email}
    return {"premium_active": False, "email": None}
