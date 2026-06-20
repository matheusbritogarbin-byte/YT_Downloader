from fastapi import APIRouter
from app.api.auth import router as auth_router
from app.api.download import router as download_router
from app.api.payments import router as payments_router

# Centralizador oficial de rotas da API (Versionamento V1)
api_router = APIRouter(prefix="/api/v1")

# Acopla os sub-módulos criados ao roteador principal
api_router.include_router(auth_router)
api_router.include_router(download_router)
api_router.include_router(payments_router)
