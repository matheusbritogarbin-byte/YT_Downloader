import time
from fastapi import Request, HTTPException, status

CLIENT_REQUESTS: dict[str, list[float]] = {}


async def verificar_limite_requisicoes(request: Request) -> None:
    path = request.url.path
    if "/payments" in path or "/webhook" in path:
        return

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

    if len(CLIENT_REQUESTS[client_ip]) >= 60:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Limite de requisições excedido. Aguarde 60 segundos.",
        )

    CLIENT_REQUESTS[client_ip].append(current_time)
