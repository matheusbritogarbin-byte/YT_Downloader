import asyncio
import os
import re
import subprocess
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

# Configuração de Proxy Residencial Dinâmica via f-string para facilitar manutenções futuras
user = "lrxlolkp"
senha = "nr93zkbnhywf"
ip = "45.38.107.97"
porta = "6014"
PROXY_URL = f"http://{user}:{senha}@{ip}:{porta}"


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


# ---------------------------------------------------------------------------
# Opções base do yt-dlp (partilhadas entre rotas)
# ---------------------------------------------------------------------------
def _build_ydl_opts() -> dict[str, Any]:
    """Constrói opções base — APENAS para metadados (extract_flat)."""
    cookie_path = "/tmp/youtube_cookies.txt"
    cookies_content = os.getenv("YOUTUBE_COOKIES_DATA", "")

    if cookies_content and not os.path.exists(cookie_path):
        try:
            with open(cookie_path, "w", encoding="utf-8") as f:
                f.write(cookies_content)
        except Exception:
            pass

    opts: dict[str, Any] = {
        "proxy": PROXY_URL,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "allowed_extractors": ["youtube"],
        "extract_flat": True,  # APENAS metadados — sem validação de formatos
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
        "extractor_args": {"youtube": {"client": ["android"]}},
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro Build/UD1A.230803.041; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/120.0.6099.230 Mobile Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    }

    if os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0:
        opts["cookiefile"] = cookie_path

    return opts


# ---------------------------------------------------------------------------
# Selecção manual de formatos (apenas para o caso de fallback da API Python)
# ---------------------------------------------------------------------------
def _extrair_url_stream(info: dict[str, Any], ext: str) -> str | None:
    formats: list[dict[str, Any]] = info.get("formats") or []

    direct_url = info.get("url")
    if direct_url and isinstance(direct_url, str) and direct_url.startswith("http"):
        return direct_url

    if not formats:
        return None

    def _tem_url(f: dict[str, Any]) -> bool:
        u = f.get("url")
        return bool(u) and isinstance(u, str) and u.startswith("http")

    def _tem_audio(f: dict[str, Any]) -> bool:
        return f.get("acodec") not in (None, "none")

    def _tem_video_e_audio(f: dict[str, Any]) -> bool:
        return f.get("vcodec") not in (None, "none") and f.get("acodec") not in (
            None,
            "none",
        )

    if ext == "mp4":
        # Progressivo MP4
        candidates = [
            f
            for f in formats
            if _tem_url(f) and _tem_video_e_audio(f) and f.get("ext") == "mp4"
        ]
        candidates.sort(key=lambda f: f.get("height", 0) or 0, reverse=True)
        if candidates:
            return str(candidates[0]["url"])
        candidates = [f for f in formats if _tem_url(f) and _tem_video_e_audio(f)]
        candidates.sort(key=lambda f: f.get("height", 0) or 0, reverse=True)
        if candidates:
            return str(candidates[0]["url"])

    # Áudio dedicado
    candidates = [
        f
        for f in formats
        if _tem_url(f)
        and (f.get("vcodec") or "none") in ("none", None)
        and _tem_audio(f)
    ]
    candidates.sort(
        key=lambda f: (
            0 if f.get("ext") in ("m4a", "mp4") else 1,
            -(f.get("abr", 0) or 0),
        )
    )
    if candidates:
        return str(candidates[0]["url"])

    # Qualquer formato com áudio
    candidates = [f for f in formats if _tem_url(f) and _tem_audio(f)]
    candidates.sort(
        key=lambda f: (
            0 if (f.get("vcodec") or "none") in ("none", None) else 1,
            -(f.get("abr", 0) or 0),
        )
    )
    if candidates:
        return str(candidates[0]["url"])

    # Último recurso
    for f in formats:
        if _tem_url(f):
            return str(f["url"])

    return None


def _determinar_ext_real(download_url: str, ext_solicitado: str) -> str:
    if ext_solicitado != "mp3":
        return ext_solicitado
    path = urllib.parse.urlparse(download_url).path.lower()
    if path.endswith(".webm"):
        return "webm"
    if path.endswith(".m4a"):
        return "m4a"
    return ext_solicitado


# ---------------------------------------------------------------------------
# Subprocess yt-dlp --get-url (contorna validação interna da API Python)
# ---------------------------------------------------------------------------
def _resolver_url_via_subprocess(url: str) -> str | None:
    """
    Usa `yt-dlp --get-url --format "worst"` via subprocess para obter a URL
    real da stream do Google Video.

    O subprocess contorna a validação interna de formatos que a API Python
    do yt-dlp faz e que dispara o erro "Requested format is not available"
    para conteúdo protegido.
    """
    cookie_path = "/tmp/youtube_cookies.txt"
    cookie_arg = (
        f'--cookiefile "{cookie_path}"'
        if os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0
        else ""
    )

    cmd = (
        f"yt-dlp --quiet --no-warnings "
        f'--proxy "{PROXY_URL}" '
        f'--format "worst" '
        f'--extractor-args "youtube:client=android" '
        f"--get-url "
        f"{cookie_arg} "
        f'"{url}"'
    )

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        stdout = result.stdout.strip()
        if stdout and stdout.startswith("http"):
            return stdout
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Extração de metadados (usada pelo /processar)
# ---------------------------------------------------------------------------
def extrair_midia_com_seguranca(url: str, is_premium: bool) -> dict[str, Any]:
    """
    Extrai APENAS metadados textuais via extract_flat=True.
    A URL de stream NÃO é resolvida aqui — isso acontece no GET /stream
    via subprocess yt-dlp --get-url, que contorna a validação de formatos.
    """
    url_limpa = url
    if "list=" in url_limpa:
        url_limpa = re.sub(r"[&?]list=[^&]+", "", url_limpa)

    opts = _build_ydl_opts()

    title = "Vídeo Sem Título"
    duration = 0
    thumbnail = ""

    try:
        with yt_dlp.YoutubeDL(cast(Any, opts)) as ydl:
            extracted = ydl.extract_info(url_limpa, download=False)
            if extracted:
                info = cast(dict[str, Any], extracted)
                title = info.get("title", title)
                duration = info.get("duration", duration)
                thumbnail = info.get("thumbnail", "")
                video_id = info.get("id", "")

                if not thumbnail and video_id:
                    thumbnail = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    except Exception:
        pass

    return {
        "title": str(title),
        "download_url": str(url_limpa),  # A stream real será resolvida no /stream
        "video_url": str(url_limpa),
        "duration": int(duration) if isinstance(duration, (int, float)) else 0,
        "thumbnail": str(thumbnail),
        "status": "success" if title != "Vídeo Sem Título" else "failed",
        "error_message": (
            None if title != "Vídeo Sem Título" else "Falha ao obter título do vídeo."
        ),
    }


async def processar_item_async(
    item: DownloadItemRequest, is_premium: bool
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None, extrair_midia_com_seguranca, str(item.url), is_premium
    )
    res["url"] = item.url
    return res


# ---------------------------------------------------------------------------
# POST /processar  —  extrai metadados e devolve link para /stream
# ---------------------------------------------------------------------------
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
        client_host: str = (
            fastapi_request.client.host if fastapi_request.client else "127.0.0.1"
        )
        raw_ip: str = fastapi_request.headers.get("x-forwarded-for", client_host)
        ip_parts: list[str] = str(raw_ip).split(",")
        client_ip: str = ip_parts[0].strip()
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
                raw_parts: list[str] = str(current_data).split("|")
                count_segment: str = raw_parts[0]
                count_value: list[str] = count_segment.split(":")
                count: int = int(count_value[1])
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

    # Construir URL base do próprio servidor para montar links do /stream
    try:
        scheme = fastapi_request.url.scheme
        host = fastapi_request.url.hostname
        port = fastapi_request.url.port
        base_url = f"{scheme}://{host}"
        if port and port not in (80, 443):
            base_url += f":{port}"
    except Exception:
        base_url = os.getenv(
            "STREAM_BASE_URL",
            "https://backend-production-5a6c0.up.railway.app",
        )

    stream_endpoint = f"{base_url}/api/v1/download/stream"

    results_list: list[DownloadResponseItem] = []
    for r in raw_results:
        if r.get("status") == "success" and not is_premium:
            redis_key = f"quota:{client_ip}"
            current_data = await redis_client.get(redis_key)
            count = 1
            if current_data and str(current_data).startswith("downloads:"):
                try:
                    raw_parts_2: list[str] = str(current_data).split("|")
                    count_segment_2: str = raw_parts_2[0]
                    count_value_2: list[str] = count_segment_2.split(":")
                    count = int(count_value_2[1]) + 1
                except Exception:
                    count = 1

            hoje = datetime.now().strftime("%Y-%m-%d")
            await redis_client.set(redis_key, f"downloads:{count}|data:{hoje}")
            await redis_client.expire(redis_key, 86400)

        # A stream será resolvida no /stream
        video_url = str(r.get("video_url", ""))
        title_limpo = str(r.get("title", "arquivo")).replace(" ", "_")
        url_codificada = urllib.parse.quote(video_url)

        extensao = "mp3"
        for item in request.items:
            if item.url == r.get("url") and "mp4" in item.quality_profile:
                extensao = "mp4"

        download_url = (
            f"{stream_endpoint}?url={url_codificada}"
            f"&title={title_limpo}&ext={extensao}"
        )

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


# ---------------------------------------------------------------------------
# GET /stream  —  resolve a stream real via subprocess e faz proxy
# ---------------------------------------------------------------------------
@router.get("/stream")
async def stream_youtube_bytes(
    url: str, title: str, ext: str = "mp3"
) -> StreamingResponse:
    if not url:
        raise HTTPException(status_code=400, detail="URL ausente.")

    url_real = urllib.parse.unquote_plus(url)

    is_youtube_url = "youtube.com" in url_real or "youtu.be" in url_real

    download_url_resolved = url_real

    if is_youtube_url:
        # --- TÁCTICA PRINCIPAL: subprocess yt-dlp --get-url ---
        # Contorna COMPLETAMENTE a validação interna de formatos que a API
        # Python do yt-dlp faz e que dispara:
        #   ERROR: [youtube] VIDEO_ID: Requested format is not available
        resolved = _resolver_url_via_subprocess(url_real)
        if resolved:
            download_url_resolved = resolved
        else:
            # Fallback: yt-dlp Python API com opções mínimas
            try:
                fallback_opts = _build_ydl_opts()
                fallback_opts.pop("extract_flat", None)
                fallback_opts["youtube_include_dash_manifest"] = True
                fallback_opts["youtube_include_hls_manifest"] = True
                fallback_opts["ignore_no_formats_error"] = True
                fallback_opts["no_youtube_format_sort"] = True
                fallback_opts["format"] = "worst"
                with yt_dlp.YoutubeDL(cast(Any, fallback_opts)) as ydl:
                    info = ydl.extract_info(url_real, download=False)
                    if info:
                        resolved = _extrair_url_stream(cast(dict[str, Any], info), ext)
                        if resolved:
                            download_url_resolved = resolved
            except Exception:
                pass

    # Verificar se ainda temos URL do YouTube (stream não resolvida)
    if "youtube.com" in download_url_resolved or "youtu.be" in download_url_resolved:
        raise HTTPException(
            status_code=500,
            detail=(
                "Não foi possível obter a stream directa deste vídeo. "
                "O conteúdo pode estar protegido por restrições regionais "
                "ou de copyright. Tente novamente mais tarde."
            ),
        )

    ext_real = _determinar_ext_real(download_url_resolved, ext)

    async def generate_bytes() -> AsyncIterator[bytes]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET", download_url_resolved, headers=headers
                ) as response:
                    if response.status_code != 200:
                        yield (
                            b"Erro ao transmitir arquivo do servidor de origem "
                            b"(c\xc3\xb3digo: "
                            + str(response.status_code).encode()
                            + b")"
                        )
                        return
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 64):
                        yield chunk
        except Exception:
            yield b"Erro de conex\xc3\xa3o ao transmitir o arquivo."

    filename = f"{title}.{ext_real}"
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
