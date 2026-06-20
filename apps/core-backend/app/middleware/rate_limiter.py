import time
from typing import Any, Awaitable, Callable
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

CLIENT_REQUESTS: dict[str, list[float]] = {}


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, requests_per_minute: int = 60) -> None:
        super().__init__(app)
        self.requests_per_minute = requests_per_minute

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if "/payments" in request.url.path:
            return await call_next(request)

        try:
            client_host = request.client.host if request.client else "127.0.0.1"
            raw_ip = request.headers.get("x-forwarded-for", client_host)
            client_ip = str(raw_ip).split(",")[0].strip()
        except Exception:
            client_ip = "127.0.0.1"

        current_time = time.time()

        if client_ip not in CLIENT_REQUESTS:
            CLIENT_REQUESTS[client_ip] = []

        CLIENT_REQUESTS[client_ip] = [
            timestamp
            for timestamp in CLIENT_REQUESTS[client_ip]
            if current_time - timestamp < 60
        ]

        if len(CLIENT_REQUESTS[client_ip]) >= self.requests_per_minute:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Limite de requisições excedido. Aguarde 60 segundos.",
            )

        CLIENT_REQUESTS[client_ip].append(current_time)
        return await call_next(request)
