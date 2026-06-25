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

# Constantes para cache e retry
CACHE_TTL_SECONDS = 3600  # 1 hora
MAX_RETRIES = 3
RETRY_DELAYS = [1, 2, 4]  # segundos: 1s, 2s, 4s

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
)
async def process_youtube_video(
    request: BatchDownloadRequest, fastapi_request: Request
) -> BatchDownloadResponse:
    if not request.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Nenhum item enviado."
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
    title: str = "video",
    ext: str = "mp3",
    quality_profile: str = "mp4_max",
) -> StreamingResponse:
    """
    Extrai áudio/vídeo via yt-dlp com qualidade seleccionável.
    - quality_profile: mp4_max | mp4_1080p | mp4_720p | mp3_320k | mp3_128k
    """
    if not url:
        raise HTTPException(status_code=400, detail="URL ausente.")

    url_real = urllib.parse.unquote_plus(url)

    import yt_dlp

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "tv"],
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
        },
    }

    # Selectores de formato baseados no perfil de qualidade
    # Inspirado em formatos suportados pelo y2meta (mp3, mp4, webm, m4a, etc)
    format_map: dict[str, str] = {
        # Vídeo: prioriza MP4 (h264) pois é mais compatível, fallback para best
        "mp4_max": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "mp4_1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "mp4_720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]",
        "mp4_480p": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]",
        # Áudio: melhor qualidade disponível
        "mp3_320k": "bestaudio[ext=m4a]/bestaudio/best",
        "mp3_192k": "bestaudio[ext=m4a]/bestaudio/best",
        "mp3_128k": "bestaudio/best",
        "mp3_64k": "bestaudio/best",
        # Formatos adicionais gratuitos (yt-dlp suporta nativamente)
        "webm_max": "bestvideo[ext=webm]+bestaudio[ext=webm]/bestvideo+bestaudio/best",
        "webm_1080p": "bestvideo[height<=1080][ext=webm]+bestaudio[ext=webm]/bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "webm_720p": "bestvideo[height<=720][ext=webm]+bestaudio[ext=webm]/bestvideo[height<=720]+bestaudio/best[height<=720]",
        "m4a_320k": "bestaudio[ext=m4a]/bestaudio/best",
        "m4a_128k": "bestaudio[ext=m4a]/bestaudio/best",
    }
    selected_format = format_map.get(quality_profile, "bestvideo+bestaudio/best")

    if ext in ("mp3", "m4a"):
        ydl_opts["format"] = selected_format
        # Qualidade MP3: 320, 192, 128, 64 kbps
        mp3_quality_map = {
            "mp3_320k": "320",
            "mp3_192k": "192",
            "mp3_128k": "128",
            "mp3_64k": "64",
            "m4a_320k": "320",
            "m4a_128k": "128",
        }
        mp3_quality = mp3_quality_map.get(quality_profile, "320")
        postprocess_codec = "mp3" if ext == "mp3" else "m4a"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": postprocess_codec,
                "preferredquality": mp3_quality,
            }
        ]
    else:
        # Vídeo: usar selector específico
        ydl_opts["format"] = selected_format
        # Se o perfil pede qualidade específica, garantir extensão correta
        if quality_profile.startswith("mp4"):
            ydl_opts["format"] = selected_format.replace(
                "bestvideo", "bestvideo[ext=mp4]"
            ).replace("bestaudio", "bestaudio[ext=m4a]")
        elif quality_profile.startswith("webm"):
            ydl_opts["format"] = selected_format.replace(
                "bestvideo", "bestvideo[ext=webm]"
            ).replace("bestaudio", "bestaudio[ext=webm]")

    # Cache: verificar se já temos este URL+qualidade em cache
    cache_key = f"cache:{hash(url_real + quality_profile + ext)}"
    cached_data: dict[str, Any] | None = None
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            cached_data = {"title": cached, "url": cached}
    except Exception:
        pass

    if cached_data and cached_data.get("url"):
        resolved_url = cached_data["url"]
        video_title = cached_data.get("title", video_title)
    else:
        # Retry com backoff exponencial para contornar bloqueios temporários
        info = {}
        resolved_url = ""
        video_title = "video"

        for tentativa in range(MAX_RETRIES):
            try:
                with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
                    info = ydl.extract_info(url_real, download=False) or {}
                    video_title = str(info.get("title", "video"))

                    formats = info.get("formats", [])
                    requested_formats = info.get("requested_formats", [])

                    if requested_formats:
                        best = requested_formats[0]
                        resolved_url = best.get("url", "")
                    elif formats:
                        resolved_url = formats[-1].get("url", "")

                    if resolved_url and str(resolved_url).startswith("http"):
                        # Cachear sucesso
                        try:
                            await redis_client.setex(
                                cache_key,
                                CACHE_TTL_SECONDS,
                                resolved_url,
                            )
                        except Exception:
                            pass
                        break
            except Exception:
                resolved_url = ""
                if tentativa < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAYS[tentativa])

    if not resolved_url or not str(resolved_url).startswith("http"):
        raise HTTPException(
            status_code=502,
            detail="Stream temporariamente indisponível. Tente novamente.",
        )

    if str(resolved_url).startswith("http://"):
        resolved_url = str(resolved_url).replace("http://", "https://", 1)

    # Content-Disposition com RFC 5987 para suportar UTF-8
    # filename* (URL-encoded) é usado pelos browsers modernos
    # filename (ASCII) é fallback para browsers antigos
    filename_ascii = re.sub(r"[^\x20-\x7E]", "_", video_title)[:60]
    filename_ascii = re.sub(r"_+", "_", filename_ascii).strip("_. ")
    if not filename_ascii:
        filename_ascii = "arquivo"

    from urllib.parse import quote

    # URL-encode total do título para filename* (suporta acentos, espaços, símbolos)
    encoded_filename = quote(video_title, safe="")
    content_disposition = (
        f'attachment; filename="{filename_ascii}.{ext}"; '
        f"filename*=UTF-8''{encoded_filename}.{ext}"
    )

    async def generate_bytes():
        headers_client = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET", resolved_url, headers=headers_client
                ) as response:
                    if response.status_code == 200:
                        async for chunk in response.aiter_bytes(chunk_size=1024 * 64):
                            yield chunk
        except Exception:
            yield b""

    mime_type = "audio/mpeg" if ext == "mp3" else "video/mp4"

    return StreamingResponse(
        generate_bytes(),
        media_type=mime_type,
        headers={
            "Content-Disposition": content_disposition,
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "X-Content-Type-Options": "nosniff",
        },
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


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "yt-downloader", "version": "2.0"}


@router.get("/stats")
async def obter_estatisticas(fastapi_request: Request) -> dict[str, Any]:
    _validar_admin_token(fastapi_request)

    hoje = datetime.utcnow().strftime("%Y-%m-%d")
    stats: dict[str, Any] = {
        "data": hoje,
        "total_downloads": 0,
        "total_erros": 0,
        "taxa_sucesso": "0%",
        "cache_hits": 0,
        "qualidades_populares": {},
    }

    try:
        stats["total_downloads"] = int(
            await redis_client.get(f"stats:downloads:{hoje}") or 0
        )
        stats["total_erros"] = int(await redis_client.get(f"stats:errors:{hoje}") or 0)
        stats["cache_hits"] = int(
            await redis_client.get(f"stats:cache_hits:{hoje}") or 0
        )

        # Taxa de sucesso
        total = stats["total_downloads"] + stats["total_erros"]
        if total > 0:
            stats["taxa_sucesso"] = f"{(stats['total_downloads']/total)*100:.1f}%"

        # Qualidades mais usadas
        cursor = 0
        while True:
            cursor, keys = await redis_client.scan(
                cursor=cursor, match="popular_quality:*", count=100
            )
            for key in keys:
                quality = key.split(":")[-1]
                count = await redis_client.get(key)
                if count:
                    stats["qualidades_populares"][quality] = int(count)
            if cursor == 0:
                break
    except Exception:
        pass

    return stats


@router.post("/admin/clear-cache")
async def limpar_cache(fastapi_request: Request) -> dict[str, str]:
    _validar_admin_token(fastapi_request)
    cursor = 0
    deleted = 0
    while True:
        cursor, keys = await redis_client.scan(
            cursor=cursor, match="cache:*", count=100
        )
        if keys:
            deleted += await redis_client.delete(*keys)
        if cursor == 0:
            break
    return {"status": "ok", "mensagem": f"{deleted} itens removidos do cache."}


@router.post("/admin/record-download")
async def registrar_download(fastapi_request: Request) -> dict[str, str]:
    _validar_admin_token(fastapi_request)
    hoje = datetime.utcnow().strftime("%Y-%m-%d")
    await redis_client.incr(f"stats:downloads:{hoje}")
    return {"status": "ok"}


@router.post("/admin/record-error")
async def registrar_erro(fastapi_request: Request) -> dict[str, str]:
    _validar_admin_token(fastapi_request)
    hoje = datetime.utcnow().strftime("%Y-%m-%d")
    await redis_client.incr(f"stats:errors:{hoje}")
    return {"status": "ok"}


@router.post("/admin/record-quality")
async def registrar_qualidade(
    quality_profile: str, fastapi_request: Request
) -> dict[str, str]:
    _validar_admin_token(fastapi_request)
    hoje = datetime.utcnow().strftime("%Y-%m-%d")
    await redis_client.incr(f"popular_quality:{quality_profile}:{hoje}")
    return {"status": "ok"}
