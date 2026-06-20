from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import api_router
from app.middleware import RateLimitMiddleware
from app.core import settings

app = FastAPI(
    title=str(settings.PROJECT_NAME),
    version="1.0.0",
    docs_url="/api/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url=None,
)

app.add_middleware(RateLimitMiddleware, requests_per_minute=60)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[str(settings.FRONTEND_URL)],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(api_router)


@app.get("/health", tags=["Infrastructure"])
async def health_check() -> dict[str, str | None]:
    return {
        "status": "healthy",
        "environment": settings.ENVIRONMENT,
        "pci_compliance": "SAQ-A",
    }
