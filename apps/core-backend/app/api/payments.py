import os
from fastapi import APIRouter, Depends, HTTPException, Request, Header, status
import stripe
from app.api.auth import get_current_user

router = APIRouter(prefix="/payments", tags=["Billing & Payments"])

# --- CONFIGURAÇÕES DO GATEWAY ---
# Em produção, estas chaves devem ser injetadas estritamente do core/config.py
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "sk_test_placeholder")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_placeholder")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

stripe.api_key = STRIPE_SECRET_KEY

# ID do preço recorrente mensal de R$ 4,99 criado no painel do Stripe
SUBSCRIPTION_PRICE_ID = "price_placeholder_id"

# --- ROTAS DA API (ENDPOINTS) ---


@router.post("/checkout/create-session")
async def create_checkout_session(current_user: dict = Depends(get_current_user)):
    """
    Cria uma sessão de Checkout criptografada e segura na infraestrutura do Stripe.
    Garante conformidade PCI-DSS absoluta (Zero armazenamento de cartões no nosso banco).
    """
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=[
                "card"
            ],  # Suporte estrito a cartões de crédito industriais
            line_items=[
                {
                    "price": SUBSCRIPTION_PRICE_ID,
                    "quantity": 1,
                }
            ],
            success_url=f"{FRONTEND_URL}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_URL}/cancel.html",
            customer_email=current_user.email,  # Vincula o cliente de forma segura pelo e-mail autenticado
            metadata={"user_email": current_user.email},
        )
        return {
            "checkout_url": session.url
        }  # Retorna o link oficial e seguro da Stripe

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao inicializar o gateway de pagamentos seguro.",
        )


@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    """
    Endpoint assíncrono blindado para escutar atualizações de cobrança da Stripe.
    Contém validação de assinatura por criptografia de chave simétrica integrada.
    """
    if not stripe_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assinatura de segurança ausente.",
        )

    try:
        # Lê o corpo bruto da requisição (necessário para validar o hash criptográfico)
        payload = await request.body()

        # Constrói o evento garantindo que ele realmente veio da Stripe e não foi alterado
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Payload inválido."
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Assinatura inválida."
        )

    # --- PROCESSAMENTO SEGURO DE EVENTOS ---
    event_type = event["type"]

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        user_email = session.get("metadata", {}).get("user_email")
        stripe_customer_id = session.get("customer")
        stripe_subscription_id = session.get("subscription")

        # LOGICA DE NEGÓCIO: Atualizar o banco de dados marcando o usuário como Ativo/Premium
        print(f" Ativação Premium com sucesso para: {user_email}")

    elif event_type == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        stripe_customer_id = subscription.get("customer")

        # LOGICA DE NEGÓCIO: Revogar imediatamente o acesso do usuário ao downloader do YouTube
        print(
            f" Assinatura cancelada ou inadimplente para o cliente: {stripe_customer_id}"
        )

    return {"status": "success"}
