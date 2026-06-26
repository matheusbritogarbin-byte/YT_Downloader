import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from app.api import api_router
from app.core import settings

app = FastAPI(
    title=str(settings.PROJECT_NAME),
    version="1.0.0",
    docs_url="/api/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

frontend_path = "/app/web-frontend"


@app.get("/admin.html", response_class=HTMLResponse)
async def admin_panel():
    return FileResponse(os.path.join(frontend_path, "admin.html"))


@app.get("/debug.html", response_class=HTMLResponse)
async def debug_panel():
    return FileResponse(os.path.join(frontend_path, "debug.html"))


@app.get("/formatos.html", response_class=HTMLResponse)
async def formatos_panel():
    return FileResponse(os.path.join(frontend_path, "formatos.html"))


@app.get("/success.html", response_class=HTMLResponse)
async def success_panel():
    return FileResponse(os.path.join(frontend_path, "success.html"))


@app.get("/cancel.html", response_class=HTMLResponse)
async def cancel_panel():
    return FileResponse(os.path.join(frontend_path, "cancel.html"))


@app.get("/health", tags=["Infrastructure"])
async def health_check() -> dict[str, str | None]:
    return {
        "status": "healthy",
        "environment": settings.ENVIRONMENT,
        "pci_compliance": "SAQ-A",
    }
