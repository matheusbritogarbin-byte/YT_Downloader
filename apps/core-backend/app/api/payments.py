import os
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
import stripe
from pydantic import BaseModel
from typing import Optional
import redis.asyncio as aioredis
from app.core import settings

router = APIRouter(prefix="/payments", tags=["Payments"])

ADMIN_TOKEN = os.getenv("ADMIN_SECRET_TOKEN", "@Matheus07052008")


def _validar_admin_token(request: Request) -> None:
    token_header = request.headers.get("X-Admin-Token")
    if not token_header or token_header != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Acesso proibido.")


stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client = aioredis.from_url(redis_url, decode_responses=True)


async def get_email_from_customer(customer_id: str) -> str | None:
    try:
        customer = stripe.Customer.retrieve(customer_id)
        return customer.get("email")
    except Exception:
        return None


async def activate_premium(email: str, session_id: str | None = None) -> None:
    await redis_client.set(f"premium:status:{email}", "active")
    await redis_client.set(f"premium:token:{email}", email)
    if session_id:
        await redis_client.set(f"premium:status:{session_id}", "active")
        await redis_client.set(f"premium:token:{session_id}", email)


async def deactivate_premium(email: str, session_id: str | None = None) -> None:
    await redis_client.delete(f"premium:status:{email}")
    await redis_client.delete(f"premium:token:{email}")
    if session_id:
        await redis_client.delete(f"premium:status:{session_id}")
        await redis_client.delete(f"premium:token:{session_id}")


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
        raise HTTPException(
            status_code=500, detail="STRIPE_SECRET_KEY não configurada."
        )
    if not PRICE_ID:
        raise HTTPException(status_code=500, detail="STRIPE_PRICE_ID não configurada.")

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
            mode="subscription",
            success_url=f"{request.success_url}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=request.cancel_url,
            metadata={
                "email": request.email,
            },
        )
        return {"checkout_url": session.url, "session_id": session.id}
    except stripe.error.InvalidRequestError as e:
        raise HTTPException(status_code=400, detail=f"Stripe erro: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro inesperado: {str(e)}")


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

    event_type = event["type"]

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        email = session.get("metadata", {}).get("email") or session.get(
            "customer_email"
        )
        session_id = session.get("id")
        if email and session_id:
            await activate_premium(email, session_id)

    elif event_type == "customer.subscription.created":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer")
        if customer_id:
            email = await get_email_from_customer(customer_id)
            if email:
                await activate_premium(email)

    elif event_type == "customer.subscription.payment_succeeded":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        if customer_id:
            email = await get_email_from_customer(customer_id)
            if email:
                await activate_premium(email)

    elif event_type == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer")
        session_id = subscription.get("metadata", {}).get("session_id")
        if customer_id:
            email = await get_email_from_customer(customer_id)
            if email:
                await deactivate_premium(email, session_id)

    elif event_type == "customer.subscription.paused":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer")
        session_id = subscription.get("metadata", {}).get("session_id")
        if customer_id:
            email = await get_email_from_customer(customer_id)
            if email:
                await deactivate_premium(email, session_id)

    elif event_type == "customer.subscription.updated":
        subscription = event["data"]["object"]
        status = subscription.get("status")
        negative_statuses = {
            "canceled",
            "past_due",
            "unpaid",
            "paused",
            "incomplete",
            "incomplete_expired",
        }
        customer_id = subscription.get("customer")
        session_id = subscription.get("metadata", {}).get("session_id")
        if customer_id:
            email = await get_email_from_customer(customer_id)
            if email:
                if status in negative_statuses:
                    await deactivate_premium(email, session_id)
                else:
                    await activate_premium(email, session_id)

    elif event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        session_id = invoice.get("metadata", {}).get("session_id")
        if customer_id:
            email = await get_email_from_customer(customer_id)
            if email:
                await deactivate_premium(email, session_id)

    return {"status": "ok"}


class AdminActivateRequest(BaseModel):
    email: str
    session_id: str | None = None


class AdminDeactivateRequest(BaseModel):
    email: str
    session_id: str | None = None


@router.get("/status", response_model=StatusResponse)
async def get_premium_status(email: str):
    status = await redis_client.get(f"premium:status:{email}")
    if status == "active":
        return {"premium_active": True, "email": email}
    return {"premium_active": False, "email": None}


@router.post("/admin/activate")
async def admin_activate_premium(
    request: AdminActivateRequest, fastapi_request: Request
):
    _validar_admin_token(fastapi_request)
    session_id = request.session_id if request.session_id else None
    await activate_premium(request.email, session_id)
    return {"status": "ok", "mensagem": f"Premium ativado para {request.email}"}


@router.post("/admin/deactivate")
async def admin_deactivate_premium(
    request: AdminDeactivateRequest, fastapi_request: Request
):
    _validar_admin_token(fastapi_request)
    session_id = request.session_id if request.session_id else None
    await deactivate_premium(request.email, session_id)
    return {"status": "ok", "mensagem": f"Premium desativado para {request.email}"}
