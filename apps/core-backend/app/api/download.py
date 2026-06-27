import asyncio
import os
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, cast
import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import redis.asyncio as aioredis

router = APIRouter(prefix="/download", tags=["Media Downloader"])

CACHE_TTL_SECONDS = 3600
MAX_RETRIES = 3
RETRY_DELAYS = [1, 2, 4]
QUOTA_MAX_FREE_DAILY = 2

YOUTUBE_REGEX = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com|youtu\.be)/(watch\?v=|shorts/|embed/|playlist\?list=)?([a-zA-Z0-9_-]{11})(\S*)?$"
)

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
    file_size_bytes: int
    status: str
    error_message: str | None = None


class BatchDownloadResponse(BaseModel):
    results: list[DownloadResponseItem]


async def verificar_quota(ip: str, token: str | None) -> None:
    """Verifica se o IP já atingiu o limite diário gratuito."""
    if token:
        is_premium = await redis_client.get(f"premium:status:{token}")
        if is_premium == "active":
            return
    hoje = datetime.utcnow().strftime("%Y-%m-%d")
    quota_key = f"quota:ip:{ip}:{hoje}"
    count = int(await redis_client.get(quota_key) or 0)
    if count >= QUOTA_MAX_FREE_DAILY:
        raise HTTPException(
            status_code=403,
            detail="Limite gratuito de 2 downloads por dia atingido. Faça upgrade para Premium!",
        )


async def incrementar_quota(ip: str) -> None:
    """Incrementa o contador de downloads do IP."""
    hoje = datetime.utcnow().strftime("%Y-%m-%d")
    quota_key = f"quota:ip:{ip}:{hoje}"
    await redis_client.incr(quota_key)
    await redis_client.expire(quota_key, 86400 * 2)


def extrair_midia_com_seguranca(url: str) -> dict[str, Any]:
    url_limpa = url.strip()
    if "list=" in url_limpa:
        url_limpa = re.sub(r"[&?]list=[^&]+", "", url_limpa)

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

    tamanho_estimado = 0
    if video_id:
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"https://www.youtube.com/watch?v={video_id}")
                size_match = re.search(r'"contentLength":"(\d+)"', resp.text)
                if size_match:
                    tamanho_estimado = int(size_match.group(1))
        except Exception:
            pass

    return {
        "title": str(title),
        "download_url": video_url,
        "duration": duration,
        "thumbnail": str(thumbnail),
        "file_size_bytes": tamanho_estimado,
        "status": "success" if video_id else "failed",
        "error_message": None if video_id else "Falha ao obter metadados via oEmbed.",
    }


async def processar_item_async(item: DownloadItemRequest) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, extrair_midia_com_seguranca, str(item.url))
    res["url"] = item.url
    return res


@router.post("/processar", response_model=BatchDownloadResponse)
async def process_youtube_video(
    request: BatchDownloadRequest, fastapi_request: Request
) -> BatchDownloadResponse:
    if not request.items:
        raise HTTPException(status_code=400, detail="Nenhum item enviado.")

    for item in request.items:
        if not YOUTUBE_REGEX.match(item.url.strip()):
            raise HTTPException(status_code=400, detail=f"URL inválida: {item.url}")

    ip = fastapi_request.client.host if fastapi_request.client else "unknown"
    await verificar_quota(ip, request.token)

    tasks = [processar_item_async(item) for item in request.items]
    raw_results = await asyncio.gather(*tasks)

    results_list: list[DownloadResponseItem] = []
    for r in raw_results:
        file_size_val = r.get("file_size_bytes", 0)
        if file_size_val is None:
            file_size_val = 0
        results_list.append(
            DownloadResponseItem(
                url=str(r.get("url", "")),
                title=str(r.get("title", "Vídeo Sem Título")),
                download_url=str(r.get("download_url", "")),
                duration=int(r.get("duration", 0)),
                thumbnail=str(r.get("thumbnail", "")),
                file_size_bytes=int(file_size_val),
                status=str(r.get("status", "failed")),
                error_message=(
                    str(r.get("error_message")) if r.get("error_message") else None
                ),
            )
        )

    await incrementar_quota(ip)

    return BatchDownloadResponse(results=results_list)
