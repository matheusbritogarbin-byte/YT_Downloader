#!/usr/bin/env python3
import json
import logging
import os
import re
import sys
from typing import Any, cast

import httpx
import webview
import yt_dlp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("DesktopDownloader")

if hasattr(sys, "_MEIPASS"):
    BASE_DIR = cast(str, sys._MEIPASS)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FRONTEND_DIR = os.path.join(BASE_DIR, "apps", "web-frontend")
INDEX_HTML = os.path.join(FRONTEND_DIR, "index.html")
PREMIUM_HTML = os.path.join(FRONTEND_DIR, "premium.html")

RAILWAY_BASE = "https://backend-production-5a6c0.up.railway.app"
RAILWAY_VERIFY_TOKEN = f"{RAILWAY_BASE}/api/v1/payments/verify-token"

YOUTUBE_REGEX = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com|youtu\.be)/(watch\?v=|shorts/|embed/|playlist\?list=)?([a-zA-Z0-9_-]{11})(\S*)?$"
)

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".yt_downloader_config.json")
logger.info(f"Caminho de configuracao de sessao definido: {CONFIG_PATH}")


def ler_token_local() -> str:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                token = str(data.get("token", ""))
                logger.info("Token de sessao localizado no disco rigido com sucesso.")
                return token
        except Exception as e:
            logger.error(f"Erro ao ler arquivo de sessao do disco: {str(e)}")
    return ""


def salvar_token_local(token: str) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"token": token}, f, ensure_ascii=False, indent=2)
        logger.info("Sessao premium persistida e salva fisicamente no disco.")
    except Exception as e:
        logger.error(f"Falha ao salvar sessao em disco: {str(e)}")


class Bridge:
    def verificar_token(self, token: str) -> str:
        logger.info(f"Iniciando Phone Home para validar o token: {token}")
        try:
            salvar_token_local(token)
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(f"{RAILWAY_VERIFY_TOKEN}?token={token}")
                logger.info(
                    f"Resposta do Railway recebida. Status HTTP: {resp.status_code}"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    valido = bool(data.get("active", False))
                    logger.info(
                        f"Status da licenca verificado no Stripe/Redis: {valido}"
                    )
                    if not valido:
                        salvar_token_local("")
                    return json.dumps({"valido": valido, "detalhe": "ok"})
                return json.dumps(
                    {"valido": False, "detalhe": f"HTTP {resp.status_code}"}
                )
        except Exception as exc:
            logger.error(f"Falha na conexao com o servidor de licenca: {str(exc)}")
            return json.dumps({"valido": False, "detalhe": str(exc)})

    def processar(self, urls_json: str, quality: str = "mp3") -> str:
        logger.info(
            f"Requisicao de processamento recebida do front-end local. Perfil: {quality}"
        )
        try:
            urls = json.loads(urls_json)
        except Exception:
            urls = [urls_json]

        if not isinstance(urls, list):
            urls = [urls_json]

        results: list[dict[str, Any]] = []
        for url in urls:
            url_str = str(url).strip()
            logger.info(f"Analisando URL: {url_str}")
            if "list=" in url_str:
                url_str = re.sub(r"[&?]list=[^&]+", "", url_str)

            if not YOUTUBE_REGEX.match(url_str):
                logger.warning(f"URL recusada pelo validador regex: {url_str}")
                results.append(
                    {
                        "url": url_str,
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
                logger.info("Iniciando extracao flat de metadados textuais...")
                meta = self._extrair_metadados(url_str)
                logger.info(f"Metadados obtidos: {meta.get('title')}")

                logger.info("Resolvendo stream elastica de midia no YouTube...")
                stream_url = self._resolver_stream(url_str)

                if stream_url:
                    logger.info("Stream de midia interceptada e resolvida com sucesso!")
                else:
                    logger.error(
                        "Falha ao descriptografar canais de stream do YouTube."
                    )

                results.append(
                    {
                        "url": url_str,
                        "title": meta.get("title", "Vídeo Sem Título"),
                        "download_url": stream_url if stream_url else url_str,
                        "duration": meta.get("duration", 0),
                        "thumbnail": meta.get("thumbnail", ""),
                        "status": "success" if stream_url else "failed",
                        "error_message": (
                            None if stream_url else "Não foi possível obter a stream."
                        ),
                    }
                )
            except Exception as e:
                logger.error(f"Erro critico na esteira de processamento: {str(e)}")
                results.append(
                    {
                        "url": url_str,
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
        self, stream_url: str, titulo: str, ext: str = "mp3", destino: str = ""
    ) -> str:
        if not destino:
            destino = os.path.join(os.path.expanduser("~"), "Downloads")

        nome_seguro = re.sub(r'[\\/*?:"<>|]', "", titulo).strip()
        if not nome_seguro:
            nome_seguro = "arquivo"

        caminho = os.path.join(destino, f"{nome_seguro}.{ext}")
        logger.info(f"Iniciando download do stream de bytes. Destino final: {caminho}")

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            bytes_baixados = 0
            with httpx.Client(timeout=None) as client:
                with client.stream("GET", stream_url, headers=headers) as resp:
                    resp.raise_for_status()
                    with open(caminho, "wb", buffering=1048576) as f:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            f.write(chunk)
                            f.flush()
                            bytes_baixados += len(chunk)
            logger.info(
                f"Download concluido com sucesso! Total de bytes gravados em disco: {bytes_baixados}"
            )
            return json.dumps({"sucesso": True, "caminho": caminho, "erro": None})
        except Exception as exc:
            logger.error(f"Falha ao transferir bytes do fluxo HTTPX: {str(exc)}")
            return json.dumps({"sucesso": False, "caminho": "", "erro": str(exc)})

    def obter_downloads_path(self) -> str:
        return os.path.join(os.path.expanduser("~"), "Downloads")

    def abrir_pasta(self, caminho: str) -> None:
        pasta = os.path.dirname(caminho)
        logger.info(f"Abrindo pasta do explorador de arquivos: {pasta}")
        if not os.path.isdir(pasta):
            return
        if sys.platform == "win32":
            os.startfile(pasta)
        elif sys.platform == "darwin":
            import subprocess

            subprocess.run(["open", pasta])

    def _extrair_metadados(self, url: str) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ignoreerrors": True,
            "extract_flat": True,
            "allowed_extractors": ["youtube"],
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "web"],
                    "skip": ["dash", "hls"],
                }
            },
            "cookiesfrombrowser": ("chrome", "default", None, None),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    info_dict = cast(dict[str, Any], info)
                    return {
                        "title": info_dict.get("title", "Vídeo Sem Título"),
                        "duration": info_dict.get("duration", 0),
                        "thumbnail": info_dict.get("thumbnail", ""),
                    }
        except Exception as e:
            logger.error(f"Erro yt-dlp metadados: {str(e)}")
        return {"title": "Vídeo Sem Título", "duration": 0, "thumbnail": ""}

    def _resolver_stream(self, url: str) -> str | None:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ignoreerrors": True,
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "web"],
                    "skip": ["dash", "hls"],
                }
            },
            "cookiesfrombrowser": ("chrome", "default", None, None),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    info_dict = cast(dict[str, Any], info)
                    direct_url = info_dict.get("url")
                    if isinstance(direct_url, str) and direct_url.startswith("http"):
                        return direct_url
                    formats = info_dict.get("formats", [])
                    if isinstance(formats, list) and formats:
                        for fmt in formats:
                            u = fmt.get("url")
                            if (
                                isinstance(u, str)
                                and u.startswith("http")
                                and fmt.get("vcodec") != "none"
                                and fmt.get("acodec") != "none"
                            ):
                                return u
                        last = formats[-1]
                        if isinstance(last, dict):
                            last_url = last.get("url")
                            if isinstance(last_url, str) and last_url.startswith(
                                "http"
                            ):
                                return last_url
        except Exception as e:
            logger.error(f"Erro yt-dlp stream resolver: {str(e)}")
        return None


def main() -> None:
    logger.info("==========================================")
    logger.info("INICIALIZANDO MOTOR SAAS YT-DOWNLOADER LOGS")
    logger.info("==========================================")

    token = ler_token_local()
    if not token:
        token = os.getenv("PREMIUM_TOKEN", "")

    html_inicial = INDEX_HTML

    if token:
        try:
            bridge_temp = Bridge()
            raw = bridge_temp.verificar_token(token)
            data = json.loads(raw)
            if data.get("valido"):
                logger.info(
                    "Sessao Premium Confirmada. Inicializando painel multilinks."
                )
                html_inicial = f"{PREMIUM_HTML}?token={token}"
            else:
                logger.warning(
                    "Token local expirado no Stripe. Redirecionando para painel free."
                )
        except Exception as e:
            logger.error(f"Falha ao processar Phone Home inicial: {str(e)}")
            html_inicial = INDEX_HTML

    bridge = Bridge()
    window = webview.create_window(
        title="YT Downloader Premium - Console Ativo",
        url=html_inicial,
        js_api=bridge,
        width=480,
        height=720,
        resizable=True,
        min_size=(400, 600),
    )

    if token and html_inicial.startswith(PREMIUM_HTML):

        def set_token(w: Any, t: str) -> None:
            w.evaluate_js(f"localStorage.setItem('premium_token', '{t}');")

        webview.start(set_token, (window, token), http_server=True)
    else:
        webview.start(http_server=True)


if __name__ == "__main__":
    main()
