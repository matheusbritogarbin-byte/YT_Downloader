import re
from typing import Any, cast
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
import yt_dlp
from app.api.auth import get_current_user, TokenData

router = APIRouter(prefix="/download", tags=["Media Downloader"])

YOUTUBE_REGEX = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[a-zA-Z0-9_-]{11}"
)


class DownloadRequest(BaseModel):
    url: str


class DownloadResponse(BaseModel):
    title: str
    download_url: str
    duration: int
    thumbnail: str


def extrair_midia_com_seguranca(url: str) -> dict[str, Any]:
    ydl_opts: dict[str, Any] = {
        "format": "bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "allowed_extractors": ["youtube"],
    }

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
            }
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Não foi possível processar este vídeo. Verifique se o link é público.",
        )


@router.post("/processar", response_model=DownloadResponse)
async def process_youtube_video(
    request: DownloadRequest, current_user: TokenData = Depends(get_current_user)
) -> DownloadResponse:
    url_limpa = request.url.strip()
    if not YOUTUBE_REGEX.match(url_limpa):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URL do YouTube inválida ou em formato não permitido por motivos de segurança.",
        )

    if current_user.email is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso negado. Esta funcionalidade exige uma assinatura ativa.",
        )

    resultado = extrair_midia_com_seguranca(url_limpa)

    return DownloadResponse(
        title=str(resultado["title"]),
        download_url=str(resultado["download_url"]),
        duration=int(resultado["duration"]),
        thumbnail=str(resultado["thumbnail"]),
    )
