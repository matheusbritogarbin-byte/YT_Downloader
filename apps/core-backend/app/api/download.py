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


def extrair_midia_com_seguranca(url: str) -> dict[str, Any]:
    cookie_path = "/tmp/youtube_cookies.txt"
    cookies_content = os.getenv("YOUTUBE_COOKIES_DATA", "")

    if cookies_content and not os.path.exists(cookie_path):
        try:
            with open(cookie_path, "w", encoding="utf-8") as f:
                f.write(cookies_content)
        except Exception:
            pass

    url_limpa = url
    if "list=" in url_limpa:
        url_limpa = re.sub(r"[&?]list=[^&]+", "", url_limpa)

    ydl_opts: dict[str, Any] = {
        "proxy": PROXY_URL,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "extract_flat": True,
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
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
                raise ValueError("YouTube anti-bot block.")

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

    try:
        scheme = fastapi_request.url.scheme
        host = fastapi_request.url.hostname
        port = fastapi_request.url.port
        base_url = f"{scheme}://{host}"
        if port and port not in (80, 443):
            base_url += f":{port}"
    except Exception:
        base_url = "https://backend-production-5a6c0.up.railway.app"

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

        orig_url = str(r.get("download_url", ""))
        title_limpo = str(r.get("title", "arquivo")).replace(" ", "_")
        url_codificada = urllib.parse.quote_plus(orig_url)

        extensao = "mp3"
        for item in request.items:
            if item.url == r.get("url") and "mp4" in item.quality_profile:
                extensao = "mp4"

        proxy_download_url = (
            f"{stream_endpoint}?url={url_codificada}&title={title_limpo}&ext={extensao}"
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
            opts = {
                "proxy": PROXY_URL,
                "format": "best",
                "quiet": True,
                "no_warnings": True,
                "ignoreerrors": True,
                "youtube_include_dash_manifest": False,
                "youtube_include_hls_manifest": False,
                "allowed_extractors": ["youtube"],
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                res_dict = ydl.extract_info(url_real, download=False)
                if res_dict:
                    download_url_resolved = ""
                    formats = res_dict.get("formats", [])
                    if formats:
                        for f in formats:
                            u = f.get("url")
                            if (
                                isinstance(u, str)
                                and u.startswith("http")
                                and f.get("vcodec") != "none"
                                and f.get("acodec") != "none"
                            ):
                                download_url_resolved = u
                                break
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
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url_real, headers=headers) as response:
                    if response.status_code != 200:
                        yield b"Erro de transmissao"
                        return
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 64):
                        yield chunk
        except Exception:
            yield b"Erro de conexao"

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


# ---------------------------------------------------------------------------
# Rotas Administrativas (protegidas por X-Admin-Token)
# ---------------------------------------------------------------------------
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
