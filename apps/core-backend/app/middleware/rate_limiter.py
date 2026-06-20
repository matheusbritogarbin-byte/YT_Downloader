import time
from starlette.types import ASGIApp, Receive, Scope, Send

CLIENT_REQUESTS: dict[str, list[float]] = {}


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, requests_per_minute: int = 60) -> None:
        self.app = app
        self.requests_per_minute = requests_per_minute

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if "/payments" in path:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        raw_forwarded = headers.get(b"x-forwarded-for", b"")

        if raw_forwarded:
            client_ip = raw_forwarded.decode("utf-8").split(",")[0].strip()
        else:
            client_tuple = scope.get("client")
            client_ip = client_tuple[0] if client_tuple else "127.0.0.1"

        current_time = time.time()

        if client_ip not in CLIENT_REQUESTS:
            CLIENT_REQUESTS[client_ip] = []

        CLIENT_REQUESTS[client_ip] = [
            t for t in CLIENT_REQUESTS[client_ip] if current_time - t < 60
        ]

        if len(CLIENT_REQUESTS[client_ip]) >= self.requests_per_minute:
            bytes_payload = (
                b'{"detail":"Limite de requisicoes excedido. Aguarde 60 segundos."}'
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": bytes_payload,
                    "more_body": False,
                }
            )
            return

        CLIENT_REQUESTS[client_ip].append(current_time)
        await self.app(scope, receive, send)
