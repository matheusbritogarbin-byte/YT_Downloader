from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import api_router
from app.middleware import RateLimitMiddleware
from app.core import settings

# Inicialização do Servidor FastAPI com metadados industriais
app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    docs_url="/api/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url=None,
)

# 1. Barreira de Proteção Anti-DDoS (Middleware)
app.add_middleware(RateLimitMiddleware, requests_per_minute=60)

# 2. Configuração Estrita de CORS (Bloqueia requisições de sites maliciosos)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.FRONTEND_URL
    ],  # Permite requisições apenas do domínio do seu Frontend
    allow_credentials=True,
    allow_methods=["GET", "POST"],  # Permite apenas os métodos necessários para o app
    allow_headers=["Authorization", "Content-Type"],
)

# 3. Acoplamento do Centralizador de Rotas V1 (Auth, Payments, Download)
app.include_router(api_router)


@app.get("/health", tags=["Infrastructure"])
async def health_check():
    """Endpoint público para o Railway monitorar a saúde do contêiner."""
    return {
        "status": "healthy",
        "environment": settings.ENVIRONMENT,
        "pci_compliance": "SAQ-A",
    }
