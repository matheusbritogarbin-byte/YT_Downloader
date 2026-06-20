import time
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.config import settings

# Dicionário em memória para rastreamento temporário de requisições por IP
CLIENT_REQUESTS = {}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware de barreira de tráfego anti-abuso.
    Bloqueia requisições automatizadas em excesso que tentem esgotar recursos.
    """

    def __init__(self, app, requests_per_minute: int = 60):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute

    async def dispatch(self, request: Request, call_next):
        # Captura o IP limpando espaços e isolando proxies de borda
        raw_ip = request.headers.get("x-forwarded-for", request.client.host)
        client_ip = raw_ip.split(",")[0].strip()
        current_time = time.time()

        if client_ip not in CLIENT_REQUESTS:
            CLIENT_REQUESTS[client_ip] = []

        # Remove timestamps com mais de 60 segundos
        CLIENT_REQUESTS[client_ip] = [
            timestamp
            for timestamp in CLIENT_REQUESTS[client_ip]
            if current_time - timestamp < 60
        ]

        # Bloqueia se o IP ultrapassar a cota do plano
        if len(CLIENT_REQUESTS[client_ip]) >= self.requests_per_minute:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Limite de requisições excedido. Aguarde 60 segundos.",
            )

        CLIENT_REQUESTS[client_ip].append(current_time)
        return await call_next(request)
