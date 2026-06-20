import asyncio
import os
import re
from typing import Any, cast
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
import yt_dlp
from app.middleware.rate_limiter import verificar_limite_requisicoes

router = APIRouter(prefix="/download", tags=["Media Downloader"])

YOUTUBE_REGEX = re.compile(r"^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+$")

ADMIN_IPS = ["127.0.0.1", "100.64.0.2", "100.64.0.3", "100.64.0.4"]


class DownloadItemRequest(BaseModel):
    url: str
    quality_profile: str


class BatchDownloadRequest(BaseModel):
    items: list[DownloadItemRequest]


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


def extrair_midia_com_seguranca(
    url: str, quality_profile: str, is_premium: bool
) -> dict[str, Any]:
    if not is_premium:
        format_opt = "bestaudio/best"
    else:
        if quality_profile == "mp4_1080p":
            format_opt = "bestvideo[height<=1080]+bestaudio/best"
        elif quality_profile == "mp4_720p":
            format_opt = "bestvideo[height<=720]+bestaudio/best"
        elif quality_profile == "mp3_320k":
            format_opt = "bestaudio/best"
        else:
            format_opt = "bestaudio/best"

    cookie_path = "/tmp/youtube_cookies.txt"
    cookies_content = os.getenv("YOUTUBE_COOKIES_DATA", "")

    if cookies_content:
        try:
            with open(cookie_path, "w", encoding="utf-8") as f:
                f.write(cookies_content)
        except Exception:
            pass

    ydl_opts: dict[str, Any] = {
        "format": format_opt,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "allowed_extractors": ["youtube"],
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
    }

    if os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0:
        ydl_opts["cookiefile"] = cookie_path

    try:
        with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
            extracted = ydl.extract_info(url, download=False)
            info = cast(dict[str, Any], extracted)

            title = info.get("title")
            download_url = info.get("url")
            duration = info.get("duration")
            thumbnail = info.get("thumbnail")

            return {
                "title": str(title) if title is not None else "Vídeo Sem Título",
                "download_url": str(download_url) if download_url is not None else "",
                "duration": int(duration) if isinstance(duration, (int, float)) else 0,
                "thumbnail": str(thumbnail) if thumbnail is not None else "",
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
    profile = str(item.quality_profile)
    res = await loop.run_in_executor(
        None, extrair_midia_com_seguranca, str(item.url), profile, is_premium
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
        client_ip = ip_list[0].strip()
    except Exception:
        client_ip = "127.0.0.1"

    is_premium = client_ip in ADMIN_IPS

    if not is_premium and len(request.items) > 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Downloads simultâneos são exclusivos do Plano Premium.",
        )

    for item in request.items:
        if not YOUTUBE_REGEX.match(item.url.strip()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"URL inválida ou não suportada: {item.url}",
            )

    tasks = [processar_item_async(item, is_premium) for item in request.items]
    raw_results = await asyncio.gather(*tasks)

    results_list = [
        DownloadResponseItem(
            url=str(r.get("url", "")),
            title=str(r.get("title", "Vídeo Sem Título")),
            download_url=str(r.get("download_url", "")),
            duration=int(r.get("duration", 0)),
            thumbnail=str(r.get("thumbnail", "")),
            status=str(r.get("status", "failed")),
            error_message=(
                None if r.get("error_message") is None else str(r.get("error_message"))
            ),
        )
        for r in raw_results
    ]

    return BatchDownloadResponse(results=results_list)
