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


async def is_premium_user(token: str | None) -> bool:
    """Verifica se o token corresponde a um usuário premium ativo."""
    if not token:
        return False
    status = await redis_client.get(f"premium:status:{token}")
    return status == "active"


async def verificar_quota(ip: str, token: str | None) -> None:
    """Verifica se o IP já atingiu o limite diário gratuito.

    Usuários premium (token ativo) são liberados imediatamente sem tocar no Redis de quota.
    Esta função também garante que apenas usuários NÃO premium sejam rate-limited.
    """
    if await is_premium_user(token):
        return
    # Apenas usuários free chegam aqui
    hoje = datetime.now().strftime("%Y-%m-%d")
    quota_key = f"quota:ip:{ip}:{hoje}"
    count = int(await redis_client.get(quota_key) or 0)
    if count >= QUOTA_MAX_FREE_DAILY:
        raise HTTPException(
            status_code=403,
            detail="Limite gratuito de 2 downloads por dia atingido. Faça upgrade para Premium!",
        )


async def incrementar_quota(ip: str) -> None:
    """Incrementa o contador de downloads do IP."""
    hoje = datetime.now().strftime("%Y-%m-%d")
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

    # Usar X-Forwarded-For como fallback para IP real do cliente
    forwarded_ip = fastapi_request.headers.get("X-Forwarded-For")
    ip = (
        forwarded_ip.split(",")[0].strip()
        if forwarded_ip
        else (fastapi_request.client.host if fastapi_request.client else "unknown")
    )

    # Garantir que o token está presente — se não veio no body, tentar extrair de headers/query
    token = request.token
    if not token:
        token = fastapi_request.headers.get(
            "X-Premium-Token"
        ) or fastapi_request.query_params.get("token")

    await verificar_quota(ip, token)

    tasks = [processar_item_async(item) for item in request.items]
    raw_results = await asyncio.gather(*tasks)

    results_list: list[DownloadResponseItem] = []
    sucessos = 0
    for r in raw_results:
        file_size_val = r.get("file_size_bytes", 0)
        if file_size_val is None:
            file_size_val = 0
        status = str(r.get("status", "failed"))
        if status == "success":
            sucessos += 1
        results_list.append(
            DownloadResponseItem(
                url=str(r.get("url", "")),
                title=str(r.get("title", "Vídeo Sem Título")),
                download_url=str(r.get("download_url", "")),
                duration=int(r.get("duration", 0)),
                thumbnail=str(r.get("thumbnail", "")),
                file_size_bytes=int(file_size_val),
                status=status,
                error_message=(
                    str(r.get("error_message")) if r.get("error_message") else None
                ),
            )
        )

    # Só incrementa quota se houver pelo menos um download bem-sucedido E NÃO for premium
    if sucessos > 0 and not await is_premium_user(request.token):
        await incrementar_quota(ip)

    return BatchDownloadResponse(results=results_list)


async def get_cookies_file() -> str | None:
    """Busca cookies: env var YT_COOKIES > Redis > arquivo local."""
    import tempfile

    # 1. Prioridade máxima: variável de ambiente YT_COOKIES
    env_cookies = os.getenv("YT_COOKIES")
    if env_cookies and env_cookies.strip():
        try:
            temp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
            temp.write(env_cookies.strip())
            temp.close()
            return temp.name
        except Exception:
            pass

    # 2. Fallback: Redis (salvo via update_cookies.py)
    try:
        cookies_text = await redis_client.get("yt_cookies")
        if cookies_text and cookies_text.strip():
            temp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
            temp.write(cookies_text)
            temp.close()
            return temp.name
    except Exception:
        pass

    # 3. Último fallback: arquivo local
    base = os.path.dirname(__file__)
    for name in ["cookies.txt", "youtube_cookies.txt"]:
        local_path = os.path.join(base, "../../../", name)
        if os.path.exists(local_path):
            return local_path
    return None


@router.get("/stream")
async def stream_youtube_bytes(
    url: str,
    title: str = "video",
    ext: str = "mp3",
    quality_profile: str = "mp4_max",
) -> StreamingResponse:
    """Stream de áudio/vídeo com qualidade seleccionável."""
    if not url:
        raise HTTPException(status_code=400, detail="URL ausente.")

    url_real = urllib.parse.unquote_plus(url)
    import yt_dlp

    resolved_url = ""
    video_title = title or "video"

    # Mapeamento de qualidade → formato yt-dlp
    FORMAT_MAP = {
        "mp4_360p": "bestvideo[height<=360]+bestaudio/best[height<=360]",
        "mp4_480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "mp4_720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "mp3_128k": "bestaudio/best",
        "mp3_192k": "bestaudio/best",
        "mp3_320k": "bestaudio/best",
        "mp4_max": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    }
    selected_format = FORMAT_MAP.get(
        quality_profile, "bestvideo[height<=360]+bestaudio/best[height<=360]"
    )
    if ext in ("mp3", "m4a") and quality_profile not in FORMAT_MAP:
        selected_format = "bestaudio/best"

    postprocessors = None
    if ext == "mp3":
        quality_map = {
            "mp3_320k": "320",
            "mp3_192k": "192",
            "mp3_128k": "128",
            "mp3_64k": "64",
        }
        postprocessors = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality_map.get(quality_profile, "320"),
            }
        ]
    elif ext == "m4a":
        postprocessors = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "320",
            }
        ]

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": selected_format,
        "merge_output_format": "mp4",
        "extractor_args": {
            "youtube": {
                "player_client": ["tvhtml5", "ios", "android", "web"],
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Origin": "https://www.youtube.com",
            "Referer": "https://www.youtube.com/",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        },
    }

    cookies_file = await get_cookies_file()
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file

    if postprocessors:
        ydl_opts["postprocessors"] = postprocessors

    def extrair_url_stream_robusta(formats: list[dict[str, Any]], ext: str) -> str:
        """Varre formats procurando por URL de stream progressiva direta."""
        if not formats:
            return ""
        # Para áudio: aceitar apenas acodec presente (vcodec pode ser none em DASH)
        if ext in ("mp3", "m4a"):
            for fmt in reversed(formats):
                url = fmt.get("url", "")
                if not url or not str(url).startswith("http"):
                    continue
                acodec = fmt.get("acodec", "none")
                ext_fmt = fmt.get("ext", "")
                if acodec != "none" and ext_fmt in ["mp3", "m4a", "webm", "mp4"]:
                    return str(url)
        # Para vídeo: exigir ambos audio e video
        for fmt in reversed(formats):
            url = fmt.get("url", "")
            if not url or not str(url).startswith("http"):
                continue
            acodec = fmt.get("acodec", "none")
            vcodec = fmt.get("vcodec", "none")
            ext_fmt = fmt.get("ext", "")
            if (
                acodec != "none"
                and vcodec != "none"
                and ext_fmt in ["mp4", "m4a", "mp3", "webm"]
            ):
                return str(url)
        # Último fallback: qualquer URL HTTP válida
        for fmt in reversed(formats):
            url = fmt.get("url", "")
            if url and str(url).startswith("http"):
                return str(url)
        return ""

    resolved_url = ""
    for tentativa in range(MAX_RETRIES):
        try:
            # Tentar múltiplos formatos em ordem de preferência
            formatos_para_tentar = [selected_format]
            if ext in ("mp3", "m4a"):
                formatos_para_tentar.extend(["bestaudio", "best", "worstaudio/worst"])
            elif ext == "mp4":
                formatos_para_tentar.extend(["best[ext=mp4]", "best", "worst"])
            else:
                formatos_para_tentar.extend(["best", "worst"])

            for fmt_try in formatos_para_tentar:
                try:
                    ydl_opts_try = {**ydl_opts, "format": fmt_try}
                    with yt_dlp.YoutubeDL(cast(Any, ydl_opts_try)) as ydl:
                        info = ydl.extract_info(url_real, download=False) or {}
                        video_title = str(info.get("title", video_title))
                        formats = list(info.get("formats", []))

                        # Tentar requested_formats primeiro
                        requested_formats = list(info.get("requested_formats", []))
                        if requested_formats:
                            merged_formats = requested_formats + formats
                        else:
                            merged_formats = formats

                        resolved_url = extrair_url_stream_robusta(merged_formats, ext)

                        if resolved_url:
                            break
                except Exception:
                    continue

            if resolved_url:
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

    resolved_url = (
        str(resolved_url).replace("http://", "https://", 1)
        if str(resolved_url).startswith("http://")
        else resolved_url
    )

    filename_ascii = re.sub(r"[^\x20-\x7E]", "_", video_title)[:60]
    filename_ascii = re.sub(r"_+", "_", filename_ascii).strip("_. ") or "arquivo"
    encoded_filename = urllib.parse.quote(video_title, safe="")
    content_disposition = f"attachment; filename=\"{filename_ascii}.{ext}\"; filename*=UTF-8''{encoded_filename}.{ext}"

    async def generate_bytes():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET", resolved_url, headers={"User-Agent": "Mozilla/5.0"}
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
    token_header = request.headers.get("X-Admin-Token")
    if not token_header or token_header != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Acesso proibido.")


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "yt-downloader", "version": "2.0"}


@router.get("/debug/formats")
async def debug_formatos(url: str) -> dict[str, Any]:
    """Endpoint temporário para ver formatos disponíveis."""
    url_real = urllib.parse.unquote_plus(url)
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestvideo+bestaudio/best",
        "extractor_args": {"youtube": {"player_client": ["web"]}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
    }

    try:
        with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
            info = ydl.extract_info(url_real, download=False) or {}
            formats = list(info.get("formats", []))

            top_formatos = []
            for f in formats[-10:]:
                top_formatos.append(
                    {
                        "format_id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "resolution": f.get("resolution"),
                        "vcodec": f.get("vcodec"),
                        "acodec": f.get("acodec"),
                        "filesize": f.get("filesize"),
                        "height": f.get("height"),
                        "width": f.get("width"),
                    }
                )

            return {
                "title": info.get("title"),
                "total_formats": len(formats) if formats else 0,
                "top_10": top_formatos[::-1],
                "requested_formats": [
                    f.get("format_id") for f in list(info.get("requested_formats", []))
                ],
            }
    except Exception as e:
        return {"erro": str(e)}


@router.get("/stats")
async def obter_estatisticas(fastapi_request: Request) -> dict[str, Any]:
    _validar_admin_token(fastapi_request)
    hoje = datetime.now().strftime("%Y-%m-%d")
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
        total = stats["total_downloads"] + stats["total_erros"]
        if total > 0:
            stats["taxa_sucesso"] = f"{(stats['total_downloads']/total)*100:.1f}%"
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
    hoje = datetime.now().strftime("%Y-%m-%d")
    await redis_client.incr(f"stats:downloads:{hoje}")
    return {"status": "ok"}


@router.post("/admin/record-error")
async def registrar_erro(fastapi_request: Request) -> dict[str, str]:
    _validar_admin_token(fastapi_request)
    hoje = datetime.now().strftime("%Y-%m-%d")
    await redis_client.incr(f"stats:errors:{hoje}")
    return {"status": "ok"}


@router.post("/admin/record-quality")
async def registrar_qualidade(
    quality_profile: str, fastapi_request: Request
) -> dict[str, str]:
    _validar_admin_token(fastapi_request)
    hoje = datetime.now().strftime("%Y-%m-%d")
    await redis_client.incr(f"popular_quality:{quality_profile}:{hoje}")
    return {"status": "ok"}


@router.post("/admin/cookies/save")
async def salvar_cookies(fastapi_request: Request) -> dict[str, str]:
    data = await fastapi_request.json()
    cookies_text = data.get("cookies_text", "")
    _validar_admin_token(fastapi_request)
    await redis_client.setex("yt_cookies", 86400, cookies_text)
    return {"status": "ok", "mensagem": "Cookies salvos com sucesso!"}


@router.get("/admin/cookies/get")
async def buscar_cookies(fastapi_request: Request) -> dict[str, str]:
    _validar_admin_token(fastapi_request)
    cookies = await redis_client.get("yt_cookies")
    return {"cookies": cookies or ""}


@router.post("/cookies/save-file")
async def save_cookies_file(fastapi_request: Request) -> dict[str, str]:
    data = await fastapi_request.json()
    cookies_text = data.get("cookies_text", "")
    _validar_admin_token(fastapi_request)
    await redis_client.setex("yt_cookies", 2592000, cookies_text)  # 30 dias
    return {"status": "ok", "mensagem": "Cookies salvos com sucesso!"}
