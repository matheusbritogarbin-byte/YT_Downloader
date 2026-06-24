import asyncio
import os
import re
import urllib.parse
from datetime import datetime
from typing import Any, cast
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import redis.asyncio as aioredis
from app.middleware.rate_limiter import verificar_limite_requisicoes

router = APIRouter(prefix="/download", tags=["Media Downloader"])

YOUTUBE_REGEX = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com|youtu\.be)/(watch\?v=|shorts/|embed/|playlist\?list=)?([a-zA-Z0-9_-]{11})(\S*)?$"
)
ADMIN_IPS = ["127.0.0.1", "100.64.0.2", "100.64.0.3", "100.64.0.4"]

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client: Any = cast(Any, aioredis).from_url(redis_url, decode_responses=True)


class DownloadItemRequest(BaseModel):
    url: str
    quality_profile: str


class BatchDownloadRequest(BaseModel):
    items: list[DownloadItemRequest]
    token: str | None = None


class DownloadResponseItem(BaseModel):
    url: str
    title: str
    download_url: str
    duration: int
    thumbnail: str
    status: str
    error_message: str | None = None


class BatchDownloadResponse(BaseModel):
    results: list[DownloadResponseItem]


def extrair_midia_com_seguranca(url: str) -> dict[str, Any]:
    """
    Extrai metadados via oEmbed oficial do YouTube.
    SEM yt-dlp, SEM proxy, SEM bloqueios de CAPTCHA.
    Usa a API oficial https://www.youtube.com/oembed.
    """
    url_limpa = url.strip()
    if "list=" in url_limpa:
        url_limpa = re.sub(r"[&?]list=[^&]+", "", url_limpa)

    # Extrai e normaliza o video_id para o padrão canónico do oEmbed
    video_id_match = re.search(
        r"(?:v=|\/shorts\/|\/embed\/|\/v\/|youtu\.be\/|\/watch\?v=)"
        r"([a-zA-Z0-9_-]{11})",
        url_limpa,
    )
    if video_id_match:
        url_limpa = f"https://youtube.com/watch?v={video_id_match.group(1)}"

    title = "Vídeo Sem Título"
    duration = 0
    thumbnail = ""
    video_id = ""
    video_url = url_limpa
    data: dict[str, Any] = {}

    oembed_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url_limpa)}&format=json"

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(oembed_url)
            if resp.status_code == 200:
                data = resp.json()
                title = data.get("title", title)
                thumbnail = data.get("thumbnail_url", "")
    except Exception:
        pass

    match = re.search(
        r"(?:v=|\/shorts\/|\/embed\/|\/v\/|youtu\.be\/|\/watch\?v=)"
        r"([a-zA-Z0-9_-]{11})",
        url_limpa,
    )
    if match:
        video_id = match.group(1)
        video_url = f"https://www.youtube.com/watch?v={video_id}"

    if video_id:
        thumbnail = (
            data.get("thumbnail_url")
            or f"https://youtube.com{video_id}/maxresdefault.jpg"
        )

    return {
        "title": str(title),
        "download_url": video_url,
        "duration": duration,
        "thumbnail": str(thumbnail),
        "status": "success" if video_id else "failed",
        "error_message": None if video_id else "Falha ao obter metadados via oEmbed.",
    }


async def processar_item_async(item: DownloadItemRequest) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, extrair_midia_com_seguranca, str(item.url))
    res["url"] = item.url
    return res


@router.post(
    "/processar",
    response_model=BatchDownloadResponse,
    dependencies=[Depends(verificar_limite_requisicoes)],
)
async def process_youtube_video(
    request: BatchDownloadRequest, fastapi_request: Request
) -> BatchDownloadResponse:
    if not request.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Nenhum item enviado."
        )

    try:
        client_host = (
            fastapi_request.client.host if fastapi_request.client else "127.0.0.1"
        )
        raw_ip = fastapi_request.headers.get("x-forwarded-for", client_host)
        ip_parts = str(raw_ip).split(",")
        client_ip = str(ip_parts[0]).strip()
    except Exception:
        client_ip = "127.0.0.1"

    is_premium = client_ip in ADMIN_IPS

    if not is_premium and request.token:
        token_status = await redis_client.get(f"token:{request.token}")
        if token_status and str(token_status).startswith("premium"):
            is_premium = True

    if not is_premium:
        redis_key = f"quota:{client_ip}"
        current_data = await redis_client.get(redis_key)
        if current_data and str(current_data).startswith("downloads:"):
            try:
                parts = str(current_data).split("|")
                count_part = parts[0].split(":")
                count = int(count_part[1])
                if count >= 2:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Limite diário excedido no servidor. Adquira o Plano Premium para downloads ilimitados.",
                    )
            except HTTPException:
                raise
            except Exception:
                pass

    if not is_premium and len(request.items) > 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Downloads simultâneos são exclusivos do Plano Premium.",
        )

    for item in request.items:
        if not YOUTUBE_REGEX.match(item.url.strip()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"URL inválida: {item.url}",
            )

    tasks = [processar_item_async(item) for item in request.items]
    raw_results = await asyncio.gather(*tasks)

    results_list: list[DownloadResponseItem] = []

    for r in raw_results:
        if r.get("status") == "success" and not is_premium:
            redis_key = f"quota:{client_ip}"
            current_data = await redis_client.get(redis_key)
            count = 1
            if current_data and str(current_data).startswith("downloads:"):
                try:
                    parts = str(current_data).split("|")
                    count_part = parts[0].split(":")
                    count = int(count_part[1]) + 1
                except Exception:
                    count = 1

            hoje = datetime.now().strftime("%Y-%m-%d")
            await redis_client.set(redis_key, f"downloads:{count}|data:{hoje}")
            await redis_client.expire(redis_key, 86400)

        # A URL de download já é a stream real do Google Video
        download_url = str(r.get("download_url", ""))

        results_list.append(
            DownloadResponseItem(
                url=str(r.get("url", "")),
                title=str(r.get("title", "Vídeo Sem Título")),
                download_url=download_url,
                duration=int(r.get("duration", 0)),
                thumbnail=str(r.get("thumbnail", "")),
                status=str(r.get("status", "failed")),
                error_message=(
                    None
                    if r.get("error_message") is None
                    else str(r.get("error_message"))
                ),
            )
        )

    return BatchDownloadResponse(results=results_list)


@router.get("/stream")
async def stream_youtube_bytes(
    url: str,
    title: str,
    ext: str = "mp3",
    captchaToken: str | None = None,
) -> RedirectResponse:
    if not url:
        raise HTTPException(status_code=400, detail="URL ausente.")

    url_real = urllib.parse.unquote_plus(url)

    # Se o CAPTCHA foi resolvido, usa a API v11 oficial; senão fallback para mirror community
    api_url = "https://api.cobalt.tools/" if captchaToken else "https://cobalt.moe"

    payload = {
        "url": url_real,
        "videoQuality": "1080" if ext == "mp4" else "audio",
        "downloadMode": "audio" if ext == "mp3" else "video",
        "audioFormat": "mp3",
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    if captchaToken:
        headers["cf-turnstile-response"] = captchaToken

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(api_url, json=payload, headers=headers)
            if response.status_code == 200:
                stream_resolved = response.json().get("url")
                if stream_resolved and str(stream_resolved).startswith("http"):
                    # Força HTTPS contra erros de Mixed Content do Chrome
                    if str(stream_resolved).startswith("http://"):
                        stream_resolved = str(stream_resolved).replace(
                            "http://", "https://", 1
                        )

                    # Redireciona o download instantaneamente para o navegador do usuário iniciar a gravação
                    return RedirectResponse(url=stream_resolved)

            # Se a API principal do Cobalt der cota cheia, faz o fallback dinâmico para a Lunes API
            elif response.status_code == 429 or response.status_code == 403:
                fallback_url = "https://lunes.host"
                response_fb = await client.post(
                    fallback_url, json=payload, headers=headers
                )
                if response_fb.status_code == 200:
                    stream_resolved = response_fb.json().get("url")
                    if stream_resolved:
                        if str(stream_resolved).startswith("http://"):
                            stream_resolved = str(stream_resolved).replace(
                                "http://", "https://", 1
                            )
                        return RedirectResponse(url=stream_resolved)

    except Exception:
        pass

    raise HTTPException(
        status_code=502,
        detail="O servidor de processamento de mídia está congestionado. Tente novamente em 30 segundos.",
    )


ADMIN_TOKEN = os.getenv("ADMIN_SECRET_TOKEN", "@Matheus07052008")


def _validar_admin_token(request: Request) -> None:
    token_header: str | None = request.headers.get("X-Admin-Token")
    if not token_header or token_header != ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acesso proibido. Tentativa de invasao registrada.",
        )


@router.get("/admin/tokens")
async def admin_listar_tokens(fastapi_request: Request) -> dict[str, Any]:
    _validar_admin_token(fastapi_request)
    cursor: int = 0
    chaves: list[str] = []
    while True:
        cursor, batch = await redis_client.scan(
            cursor=cursor, match="token:*", count=100
        )
        chaves.extend(batch)
        if cursor == 0:
            break
    valores: dict[str, str | None] = {}
    for chave in chaves:
        valor = await redis_client.get(chave)
        if valor is not None:
            valores[chave] = str(valor)
    return {"tokens": valores}


class AdminResetQuotaRequest(BaseModel):
    ip: str


@router.post("/admin/reset-quota")
async def admin_reset_quota(
    body: AdminResetQuotaRequest, fastapi_request: Request
) -> dict[str, str]:
    _validar_admin_token(fastapi_request)
    redis_key: str = f"quota:{body.ip}"
    await redis_client.delete(redis_key)
    return {"status": "ok", "mensagem": f"Quota do IP {body.ip} foi resetada."}
