import asyncio
import os
import re
import urllib.parse
from datetime import datetime
from typing import Any, cast, AsyncIterator
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
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


def extrair_midia_com_seguranca(url: str, quality_profile: str) -> dict[str, Any]:
    cookie_path = "/tmp/youtube_cookies.txt"
    cookies_content = os.getenv("YOUTUBE_COOKIES_DATA", "")

    if cookies_content:
        try:
            with open(cookie_path, "w", encoding="utf-8") as f:
                f.write(cookies_content)
        except Exception:
            pass

    url_limpa = url
    if "list=" in url_limpa:
        url_limpa = re.sub(r"[&?]list=[^&]+", "", url_limpa)

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "extract_flat": "discard_in_playlist",
        "allowed_extractors": ["youtube"],
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
    }

    if os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0:
        ydl_opts["cookiefile"] = cookie_path

    try:
        with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
            extracted = ydl.extract_info(url_limpa, download=False)
            if not extracted:
                raise ValueError(
                    "O YouTube bloqueou o acesso temporariamente para este link."
                )

            info = cast(dict[str, Any], extracted)
            title = info.get("title", "Vídeo Sem Título")
            duration = info.get("duration", 0)

            thumbnail = info.get("thumbnail", "")
            if not thumbnail and info.get("id"):
                thumbnail = f"https://youtube.com{info.get('id')}/mqdefault.jpg"

            video_id = info.get("id", "video_id")
            download_url = f"https://youtube.com{video_id}"

            return {
                "title": str(title),
                "download_url": download_url,
                "duration": int(duration) if isinstance(duration, (int, float)) else 0,
                "thumbnail": str(thumbnail),
                "status": "success",
                "error_message": None,
            }
    except Exception as e:
        return {
            "title": "Erro",
            "download_url": "",
            "duration": 0,
            "thumbnail": "",
            "status": "failed",
            "error_message": str(e),
        }


async def processar_item_async(
    item: DownloadItemRequest, is_premium: bool
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None, extrair_midia_com_seguranca, str(item.url), item.quality_profile
    )
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
        ip_list = str(raw_ip).split(",")
        client_ip = ip_list.strip()
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
                count_part = parts.split(":")
                count = int(count_part)
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

    tasks = [processar_item_async(item, is_premium) for item in request.items]
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
                    count_part = parts.split(":")
                    count = int(count_part) + 1
                except Exception:
                    count = 1

            hoje = datetime.now().strftime("%Y-%m-%d")
            await redis_client.set(redis_key, f"downloads:{count}|data:{hoje}")
            await redis_client.expire(redis_key, 86400)

        orig_url = str(r.get("download_url", ""))
        title_limpo = str(r.get("title", "arquivo")).replace(" ", "_")
        url_codificada = urllib.parse.quote_plus(orig_url)

        extensao = "mp3"
        for item in request.items:
            if item.url == r.get("url") and "mp4" in item.quality_profile:
                extensao = "mp4"

        proxy_download_url = (
            f"https://railway.app{url_codificada}&title={title_limpo}&ext={extensao}"
        )

        results_list.append(
            DownloadResponseItem(
                url=str(r.get("url", "")),
                title=str(r.get("title", "Vídeo Sem Título")),
                download_url=proxy_download_url,
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
    url: str, title: str, ext: str = "mp3"
) -> StreamingResponse:
    if not url:
        raise HTTPException(status_code=400, detail="URL ausente.")

    url_real = urllib.parse.unquote_plus(url)

    if "youtube.com" in url_real or "youtu.be" in url_real:
        try:
            # Força o formato elástico único "best" para pular qualquer validação que exija FFmpeg no servidor
            opts = {
                "format": "best",
                "quiet": True,
                "no_warnings": True,
                "ignoreerrors": True,
                "allowed_extractors": ["youtube"],
            }

            cookie_path = "/tmp/youtube_cookies.txt"
            if os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0:
                opts["cookiefile"] = cookie_path

            with yt_dlp.YoutubeDL(opts) as ydl:
                res_dict = ydl.extract_info(url_real, download=False)
                if res_dict:
                    download_url_resolved = ""
                    formats = res_dict.get("formats", [])
                    if formats:
                        if ext == "mp3":
                            audio_streams = [
                                f
                                for f in formats
                                if f.get("vcodec") == "none" and f.get("url")
                            ]
                            download_url_resolved = (
                                str(audio_streams[-1].get("url", ""))
                                if audio_streams
                                else str(formats[-1].get("url", ""))
                            )
                        else:
                            video_streams = [
                                f
                                for f in formats
                                if f.get("vcodec") != "none" and f.get("url")
                            ]
                            download_url_resolved = (
                                str(video_streams[-1].get("url", ""))
                                if video_streams
                                else str(formats[-1].get("url", ""))
                            )

                    if not download_url_resolved:
                        download_url_resolved = str(res_dict.get("url", ""))

                    if download_url_resolved:
                        url_real = download_url_resolved
        except Exception:
            pass

    async def generate_bytes() -> AsyncIterator[bytes]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url_real, headers=headers) as response:
                if response.status_code != 200:
                    yield b"Erro ao transmitir arquivo"
                    return
                async for chunk in response.aiter_bytes(chunk_size=1024 * 64):
                    yield chunk

    filename = f"{title}.{ext}"
    mime_type = "video/mp4" if ext == "mp4" else "audio/mpeg"
    return StreamingResponse(
        generate_bytes(),
        media_type=mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        },
    )
