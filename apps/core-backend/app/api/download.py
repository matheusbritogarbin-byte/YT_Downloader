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
# Opções unificadas do yt-dlp  (evita duplicação entre /processar e /stream)
# ---------------------------------------------------------------------------
def _build_ydl_opts(download_mode: bool = False) -> dict[str, Any]:
    """
    Constrói um dicionário de opções consistente para todas as chamadas ao yt-dlp.

    Args:
        download_mode:
            ``True``  → desliga *extract_flat* para obter a árvore completa de formatos
                       (necessário no /stream para extrair URL real de stream).
            ``False`` → mantém *extract_flat* ativo (apenas metadados textuais, usado no
                       /processar para gerar cards rapidamente).
    """
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
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
        # --- Bloqueio anti-PoToken: emular apps nativos iOS + TV ---
        "extractor_args": {
            "youtube": {
                "client": ["ios", "tv"],
                "skip": ["dash", "hls"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
                "Mobile/15E148 Safari/604.1"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "max-age=0",
        },
    }

    if os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0:
        opts["cookiefile"] = cookie_path

    if not download_mode:
        # --- Apenas metadados (cards) ----------------------------------------
        opts["extract_flat"] = True
    # Em download_mode NÃO definimos "format" — faremos a seleção manualmente
    # a partir da lista completa de formats[] (evita erro de formato + FFmpeg).
    return opts


# ---------------------------------------------------------------------------
# Selecção manual de formatos (SEM FFmpeg)
# ---------------------------------------------------------------------------
def _extrair_url_stream(info: dict[str, Any], ext: str) -> str | None:
    """
    Varre a árvore ``info["formats"]`` e retorna a URL directa do Google Video
    mais adequada para a extensão solicitada, **sem depender do seletor automático
    do yt-dlp** (que exige FFmpeg para fundir ``bestvideo+bestaudio``).

    Estratégia:
        * ``mp4`` → formatos **progressivos** (vcodec + acodec no mesmo ficheiro),
                    ordenados por altura descendente.
        * ``mp3``/áudio → formatos **só áudio** (vcodec == "none"), preferindo
                          ``m4a`` (AAC) por compatibilidade nativa com browsers.
    """
    formats: list[dict[str, Any]] = info.get("formats") or []

    # 1. URL directa no topo do dict (raro, mas acontece)
    direct_url = info.get("url")
    if direct_url and isinstance(direct_url, str) and direct_url.startswith("http"):
        return direct_url

    if not formats:
        return None

    def _tem_url(f: dict[str, Any]) -> bool:
        u = f.get("url")
        return bool(u) and isinstance(u, str) and u.startswith("http")

    if ext == "mp4":
        # --- Progressivo MP4 (video + áudio juntos, sem FFmpeg) -------------
        candidates = [
            f
            for f in formats
            if _tem_url(f)
            and f.get("vcodec") not in (None, "none")
            and f.get("acodec") not in (None, "none")
            and f.get("ext") == "mp4"
        ]
        candidates.sort(key=lambda f: f.get("height", 0) or 0, reverse=True)
        if candidates:
            return str(candidates[0]["url"])

        # Fallback: qualquer progressivo (qualquer extensão)
        candidates = [
            f
            for f in formats
            if _tem_url(f)
            and f.get("vcodec") not in (None, "none")
            and f.get("acodec") not in (None, "none")
        ]
        candidates.sort(key=lambda f: f.get("height", 0) or 0, reverse=True)
        if candidates:
            return str(candidates[0]["url"])

    else:
        # --- Áudio (mp3 / m4a) ----------------------------------------------
        candidates = [
            f
            for f in formats
            if _tem_url(f)
            and (f.get("vcodec") or "none") in ("none", None)
            and f.get("acodec") not in (None, "none")
        ]
        # Preferir m4a (AAC) → bitrate alto
        candidates.sort(
            key=lambda f: (
                0 if f.get("ext") == "m4a" else 1,
                -(f.get("abr", 0) or 0),
            )
        )
        if candidates:
            return str(candidates[0]["url"])

        # Fallback extremo: qualquer formato só áudio
        candidates = [
            f
            for f in formats
            if _tem_url(f) and (f.get("vcodec") or "none") in ("none", None)
        ]
        candidates.sort(key=lambda f: -(f.get("abr", 0) or 0))
        if candidates:
            return str(candidates[0]["url"])

    # Último recurso: qualquer formato que tenha URL
    for f in formats:
        if _tem_url(f):
            return str(f["url"])

    return None


def _determinar_ext_real(download_url: str, ext_solicitado: str) -> str:
    """
    Ajusta a extensão do ficheiro para corresponder ao que realmente vai ser
    servido (já que o yt-dlp pode devolver .webm mesmo quando pedimos mp3).
    """
    if ext_solicitado != "mp3":
        return ext_solicitado
    # Para áudio, se a URL terminar em .webm, servimos como webm
    path = urllib.parse.urlparse(download_url).path.lower()
    if path.endswith(".webm"):
        return "webm"
    if path.endswith(".m4a"):
        return "m4a"
    # Padrão: mantém mp3 (o navegador tenta reproduzir, pode falhar)
    return ext_solicitado


# ---------------------------------------------------------------------------
# Extração de metadados (usada pelo /processar)
# ---------------------------------------------------------------------------
def extrair_midia_com_seguranca(url: str, is_premium: bool) -> dict[str, Any]:
    url_limpa = url
    if "list=" in url_limpa:
        url_limpa = re.sub(r"[&?]list=[^&]+", "", url_limpa)

    ydl_opts = _build_ydl_opts(download_mode=False)

    try:
        with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
            extracted = ydl.extract_info(url_limpa, download=False)
            if not extracted:
                raise ValueError(
                    "O YouTube recusou o fornecimento dos metadados textuais "
                    "para este link."
                )

            info = cast(dict[str, Any], extracted)
            title = info.get("title", "Vídeo Sem Título")
            duration = info.get("duration", 0)
            thumbnail = info.get("thumbnail", "")

            if not thumbnail and info.get("id"):
                thumbnail = f"https://i.ytimg.com/vi/{info.get('id')}/mqdefault.jpg"

            # Apenas metadados — a URL de download real será resolvida
            # posteriormente pelo /stream quando o utilizador clicar.
            return {
                "title": str(title),
                "video_url": str(url_limpa),  # URL *original* do YouTube
                "duration": int(duration) if isinstance(duration, (int, float)) else 0,
                "thumbnail": str(thumbnail),
                "status": "success",
                "error_message": None,
            }
    except Exception as e:
        return {
            "title": "Erro",
            "video_url": "",
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
        None, extrair_midia_com_seguranca, str(item.url), is_premium
    )
    res["url"] = item.url
    return res


# ---------------------------------------------------------------------------
# POST /processar  —  extrai metadados e devolve links para /stream
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
        client_host = (
            fastapi_request.client.host if fastapi_request.client else "127.0.0.1"
        )
        raw_ip = fastapi_request.headers.get("x-forwarded-for", client_host)
        ip_parts = str(raw_ip).split(",")
        client_ip = ip_parts[0].strip()
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

    tasks = [processar_item_async(item, is_premium) for item in request.items]
    raw_results = await asyncio.gather(*tasks)

    # Construir URL base do próprio servidor para montar links do /stream
    try:
        scheme = fastapi_request.url.scheme  # http / https
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
                    parts = str(current_data).split("|")
                    count_part = parts[0].split(":")
                    count = int(count_part[1]) + 1
                except Exception:
                    count = 1

            hoje = datetime.now().strftime("%Y-%m-%d")
            await redis_client.set(redis_key, f"downloads:{count}|data:{hoje}")
            await redis_client.expire(redis_key, 86400)

        # A URL real do YouTube é passada como parâmetro para o /stream
        video_url = str(r.get("video_url", ""))
        title_limpo = str(r.get("title", "arquivo")).replace(" ", "_")
        url_codificada = urllib.parse.quote(video_url)

        extensao = "mp3"
        for item in request.items:
            if item.url == r.get("url") and "mp4" in item.quality_profile:
                extensao = "mp4"

        # Link funcional que aponta para o nosso /stream
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
# GET /stream  —  resolve a URL real do Google Video e faz stream
# ---------------------------------------------------------------------------
@router.get("/stream")
async def stream_youtube_bytes(
    url: str, title: str, ext: str = "mp3"
) -> StreamingResponse:
    if not url:
        raise HTTPException(status_code=400, detail="URL ausente.")

    url_real = urllib.parse.unquote_plus(url)

    # --- 1. Extrair informação completa com as mesmas opções anti-bloqueio --
    ydl_opts = _build_ydl_opts(download_mode=True)

    download_url_resolved: str | None = None
    try:
        with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
            info = ydl.extract_info(url_real, download=False)
            if not info:
                raise HTTPException(
                    status_code=500,
                    detail="Falha ao extrair informações do vídeo para stream.",
                )

            info_dict = cast(dict[str, Any], info)
            download_url_resolved = _extrair_url_stream(info_dict, ext)

            if not download_url_resolved:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Nenhuma stream directa encontrada para este vídeo. "
                        "O formato solicitado pode não estar disponível para "
                        "este conteúdo (ex.: protegido por restrições regionais)."
                    ),
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao resolver stream no YouTube: {str(exc)}",
        )

    # --- 2. Ajustar extensão real (ex.: .webm em vez de .mp3) -------------
    ext_real = _determinar_ext_real(download_url_resolved, ext)

    # --- 3. Stream via HTTPX -----------------------------------------------
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
