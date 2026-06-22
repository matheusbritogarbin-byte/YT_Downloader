import asyncio
import os
import re
from datetime import datetime
from typing import Any, cast
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import redis.asyncio as aioredis
import yt_dlp
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
    Extrai metadados + URL directa do Google Video usando o IP nativo
    do Railway (client-side). SEM proxy, SEM extract_flat.
    """
    url_limpa = url
    if "list=" in url_limpa:
        url_limpa = re.sub(r"[&?]list=[^&]+", "", url_limpa)

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "format": "best",
        "allowed_extractors": ["youtube"],
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
    }

    title = "Vídeo Sem Título"
    duration = 0
    thumbnail = ""
    download_url = ""

    try:
        with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
            extracted = ydl.extract_info(url_limpa, download=False)
            if not extracted:
                raise ValueError("YouTube anti-bot block.")

            info = cast(dict[str, Any], extracted)
            title = info.get("title", title)
            duration = info.get("duration", duration)
            thumbnail = info.get("thumbnail", "")
            video_id = info.get("id", "")

            if not thumbnail and video_id:
                thumbnail = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"

            # Extrair URL real do Google Video da árvore formats[]
            formats = info.get("formats", [])
            if formats:
                for f in formats:
                    u = f.get("url")
                    if (
                        isinstance(u, str)
                        and u.startswith("http")
                        and f.get("vcodec") != "none"
                        and f.get("acodec") != "none"
                    ):
                        download_url = u
                        break

            if not download_url:
                direct_url = info.get("url")
                if isinstance(direct_url, str) and direct_url.startswith("http"):
                    download_url = direct_url

            if download_url and download_url.startswith("http://"):
                download_url = download_url.replace("http://", "https://", 1)

    except Exception:
        pass

    return {
        "title": str(title),
        "download_url": download_url,
        "duration": int(duration) if isinstance(duration, (int, float)) else 0,
        "thumbnail": str(thumbnail),
        "status": "success" if download_url else "failed",
        "error_message": None if download_url else "Não foi possível obter a stream.",
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
async def stream_youtube_bytes(url: str, title: str = "video", ext: str = "mp3"):
    """
    Rota simplificada: redirecciona directamente para a URL real
    do Google Video. O download é feito pelo browser do cliente
    usando o IP residencial dele.
    """
    if not url:
        raise HTTPException(status_code=400, detail="URL ausente.")

    url_real = url

    if "youtube.com" in url_real or "youtu.be" in url_real:
        try:
            ydl_opts: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "ignoreerrors": True,
                "format": "best",
                "allowed_extractors": ["youtube"],
                "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url_real, download=False)
                if info:
                    info_dict = cast(dict[str, Any], info)
                    formats = info_dict.get("formats", [])
                    for f in formats:
                        u = f.get("url")
                        if (
                            isinstance(u, str)
                            and u.startswith("http")
                            and f.get("vcodec") != "none"
                            and f.get("acodec") != "none"
                        ):
                            url_real = u
                            break
                    if "youtube.com" in url_real or "youtu.be" in url_real:
                        direct = info_dict.get("url")
                        if isinstance(direct, str) and direct.startswith("http"):
                            url_real = direct

            if url_real.startswith("http://"):
                url_real = url_real.replace("http://", "https://", 1)
        except Exception:
            pass

    return RedirectResponse(url=url_real)


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
