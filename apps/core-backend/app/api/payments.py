from typing import Any, cast
from fastapi import APIRouter, Depends, HTTPException, Request, Header, status
import stripe
from app.api.auth import get_current_user
from app.core import settings

router = APIRouter(prefix="/payments", tags=["Billing & Payments"])

if settings.STRIPE_SECRET_KEY is None:
    raise RuntimeError("Configuração STRIPE_SECRET_KEY corrompida ou ausente.")

stripe.api_key = settings.STRIPE_SECRET_KEY.get_secret_value()


@router.post("/checkout/create-session")
async def create_checkout_session(
    request: Request, current_user: Any = Depends(get_current_user)
) -> dict[str, str]:
    user_email = "cliente_anonimo@teste.com"

    if current_user and hasattr(current_user, "email") and current_user.email:
        user_email = current_user.email

    if settings.STRIPE_PRICE_ID_PREMIUM is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ID do preço do Stripe não configurado no servidor.",
        )

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[
                {
                    "price": settings.STRIPE_PRICE_ID_PREMIUM,
                    "quantity": 1,
                }
            ],
            success_url=f"{settings.FRONTEND_URL}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{settings.FRONTEND_URL}/cancel.html",
            customer_email=user_email,
            metadata={"user_email": user_email},
        )
        return {"checkout_url": str(session.url)}

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao inicializar o gateway de pagamentos seguro.",
        )


@router.post("/webhook")
@router.post("/webhook/")
async def stripe_webhook(
    request: Request, stripe_signature: str = Header(None)
) -> dict[str, str]:
    if not stripe_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assinatura de segurança ausente.",
        )

    if settings.STRIPE_WEBHOOK_SECRET is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Segredo do webhook Stripe não configurado no servidor.",
        )

    try:
        payload = await request.body()
        webhook_helper = cast(Any, stripe.Webhook)
        event = webhook_helper.construct_event(
            payload, stripe_signature, settings.STRIPE_WEBHOOK_SECRET.get_secret_value()
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Payload inválido."
        )
    except stripe.SignatureVerificationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Assinatura inválida."
        )

    event_type = str(event.type)

    if event_type == "checkout.session.completed":
        session_obj = event.data.object
        session = cast(dict[str, Any], session_obj.to_dict())

        metadata = cast(dict[str, Any], session.get("metadata", {}))
        user_email = metadata.get("user_email")

        if not user_email:
            customer_details = cast(dict[str, Any], session.get("customer_details", {}))
            user_email = (
                customer_details.get("email")
                if customer_details
                else "email_desconhecido@teste.com"
            )

        print(f" Ativação Premium com sucesso para: {user_email}")

    return {"status": "success"}
