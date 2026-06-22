#!/usr/bin/env python3
import json
import os
import re
import sys
from typing import Any, cast

import httpx
import webview
import yt_dlp

if hasattr(sys, "_MEIPASS"):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FRONTEND_DIR = os.path.join(BASE_DIR, "apps", "web-frontend")
INDEX_HTML = os.path.join(FRONTEND_DIR, "index.html")
PREMIUM_HTML = os.path.join(FRONTEND_DIR, "premium.html")

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".yt_downloader_config.json")

RAILWAY_BASE = os.getenv(
    "RAILWAY_API_URL",
    "https://backend-production-5a6c0.up.railway.app",
)
RAILWAY_VERIFY_TOKEN = f"{RAILWAY_BASE}/api/v1/payments/verify-token"

YOUTUBE_REGEX = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com|youtu\.be)/"
    r"(watch\?v=|shorts/|embed/|playlist\?list=)?"
    r"([a-zA-Z0-9_-]{11})(\S*)?$"
)


def ler_token_local() -> str:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                token = data.get("premium_token", "")
                if isinstance(token, str):
                    return token
    except Exception:
        pass
    return ""


def salvar_token_local(token: str) -> None:
    if not token:
        return
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"premium_token": token}, f)
    except Exception:
        pass


class Bridge:
    def verificar_token(self, token: str) -> str:
        try:
            resp = httpx.get(
                f"{RAILWAY_VERIFY_TOKEN}?token={token}",
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                valido = data.get("active", False)
                if valido:
                    salvar_token_local(token)
                return json.dumps(
                    {
                        "valido": valido,
                        "detalhe": data.get("status", "ok"),
                    }
                )
            return json.dumps(
                {
                    "valido": False,
                    "detalhe": f"HTTP {resp.status_code}",
                }
            )
        except Exception as exc:
            return json.dumps({"valido": False, "detalhe": str(exc)})

    def processar(self, urls_json: str, quality: str = "mp3") -> str:
        try:
            urls = json.loads(urls_json)
        except Exception:
            urls = [urls_json]

        if not isinstance(urls, list):
            urls = [urls_json]

        results: list[dict[str, Any]] = []

        for url in urls:
            url = str(url).strip()
            if not YOUTUBE_REGEX.match(url):
                results.append(
                    {
                        "url": url,
                        "title": "URL inválida",
                        "download_url": "",
                        "duration": 0,
                        "thumbnail": "",
                        "status": "failed",
                        "error_message": "Formato de URL não reconhecido.",
                    }
                )
                continue

            try:
                meta = self._extrair_metadados(url)
                stream_url = self._resolver_stream(url)
                results.append(
                    {
                        "url": url,
                        "title": meta.get("title", "Vídeo Sem Título"),
                        "download_url": stream_url if stream_url else url,
                        "duration": meta.get("duration", 0),
                        "thumbnail": meta.get("thumbnail", ""),
                        "status": "success" if stream_url else "failed",
                        "error_message": (
                            None if stream_url else "Não foi possível obter a stream."
                        ),
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "url": url,
                        "title": "Erro",
                        "download_url": "",
                        "duration": 0,
                        "thumbnail": "",
                        "status": "failed",
                        "error_message": str(e),
                    }
                )

        return json.dumps({"results": results})

    def baixar_ficheiro(
        self,
        stream_url: str,
        titulo: str,
        ext: str = "mp3",
        destino: str = "",
    ) -> str:
        if not destino:
            destino = os.path.join(os.path.expanduser("~"), "Downloads")

        nome_seguro = re.sub(r'[\\/*?:"<>|]', "", titulo).strip()
        if not nome_seguro:
            nome_seguro = "video"

        caminho = os.path.join(destino, f"{nome_seguro}.{ext}")

        if stream_url.startswith("http") and (
            "googlevideo.com" in stream_url or "youtube.com" not in stream_url
        ):
            try:
                with httpx.Client(timeout=None) as client:
                    with client.stream("GET", stream_url) as resp:
                        resp.raise_for_status()
                        with open(caminho, "wb") as f:
                            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                                f.write(chunk)
                return json.dumps(
                    {
                        "sucesso": True,
                        "caminho": caminho,
                        "erro": None,
                    }
                )
            except Exception as exc:
                return json.dumps(
                    {
                        "sucesso": False,
                        "caminho": "",
                        "erro": str(exc),
                    }
                )

        try:
            ydl_opts: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "outtmpl": caminho,
                "format": "best",
                "noplaylist": True,
                "ignoreerrors": True,
                "extractor_args": {"youtube": {"client": ["android", "ios"]}},
            }
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
                ydl.download([stream_url])
            return json.dumps(
                {
                    "sucesso": True,
                    "caminho": caminho,
                    "erro": None,
                }
            )
        except Exception as exc:
            return json.dumps(
                {
                    "sucesso": False,
                    "caminho": "",
                    "erro": str(exc),
                }
            )

    def obter_downloads_path(self) -> str:
        return os.path.join(os.path.expanduser("~"), "Downloads")

    def abrir_pasta(self, caminho: str) -> None:
        if not os.path.isdir(caminho):
            return
        if sys.platform == "win32":
            import subprocess

            subprocess.run(["explorer", caminho], shell=True)
        elif sys.platform == "darwin":
            import subprocess

            subprocess.run(["open", caminho])
        else:
            import subprocess

            subprocess.run(["xdg-open", caminho])

    def _extrair_metadados(self, url: str) -> dict[str, Any]:
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ignoreerrors": True,
            "allowed_extractors": ["youtube"],
            "extract_flat": True,
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
            "extractor_args": {"youtube": {"client": ["android", "ios"]}},
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        }

        title = "Vídeo Sem Título"
        duration = 0
        thumbnail = ""

        try:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
                extracted = ydl.extract_info(url, download=False)
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

        return {"title": title, "duration": duration, "thumbnail": thumbnail}

    def _resolver_stream(self, url: str) -> str | None:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ignoreerrors": True,
            "format": "best",
            "extractor_args": {"youtube": {"client": ["android", "ios"]}},
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
        }
        try:
            with yt_dlp.YoutubeDL(cast(Any, opts)) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    info_dict = cast(dict[str, Any], info)
                    direct_url = info_dict.get("url")
                    if isinstance(direct_url, str) and direct_url.startswith("http"):
                        return direct_url

                    formats = info_dict.get("formats", [])
                    if isinstance(formats, list) and formats:
                        first = formats[0]
                        if isinstance(first, dict):
                            u = first.get("url")
                            if isinstance(u, str) and u.startswith("http"):
                                return u
        except Exception:
            pass
        return None


def main() -> None:
    token = ler_token_local()
    if not token:
        token = os.getenv("PREMIUM_TOKEN", "")

    bridge = Bridge()

    if token:
        try:
            raw = bridge.verificar_token(token)
            data = json.loads(raw)
            if data.get("valido"):
                html = PREMIUM_HTML
            else:
                html = INDEX_HTML
        except Exception:
            html = INDEX_HTML
    else:
        html = INDEX_HTML

    webview.create_window(
        title="YT Downloader Desktop",
        url=html,
        js_api=bridge,
        width=480,
        height=720,
        resizable=True,
        min_size=(400, 600),
        text_select=False,
        confirm_close=False,
    )

    webview.start(
        debug=os.getenv("DEBUG", "").lower() in ("1", "true", "yes"),
        http_server=True,
    )


if __name__ == "__main__":
    main()
