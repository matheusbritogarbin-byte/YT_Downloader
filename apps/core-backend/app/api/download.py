import re
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, HttpUrl
import yt_dlp
from app.api.auth import get_current_user

router = APIRouter(prefix="/download", tags=["Media Downloader"])

# Expressão regular restrita para validar URLs oficiais do YouTube (Evita injeção de parâmetros maliciosos)
YOUTUBE_REGEX = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[a-zA-Z0-9_-]{11}"
)


# --- MODELOS DE DADOS PARA VALIDAÇÃO DE ENTRADA ---
class DownloadRequest(BaseModel):
    url: str  # Recebe a string e valida manualmente contra a Regex estrita


class DownloadResponse(BaseModel):
    title: str
    download_url: str
    duration: int
    thumbnail: str


# --- FUNÇÃO AUXILIAR DE SEGURANÇA (ISOLAMENTO DE PROCESSO) ---
def extrair_midia_com_seguranca(url: str) -> dict:
    """Extrai metadados e links de stream usando yt-dlp sem expor o Shell do sistema."""
    # Configurações industriais do yt-dlp para segurança e performance
    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,  # Sanitiza nomes de arquivos contra caracteres especiais
        "allowed_extractors": [
            "youtube"
        ],  # Bloqueia qualquer outro extrator não autorizado
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # extract_info com download=False garante que não salvaremos lixo no disco do servidor
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", "Vídeo Sem Título"),
                "download_url": info.get(
                    "url"
                ),  # Link direto e temporário gerado pelo Google
                "duration": info.get("duration", 0),
                "thumbnail": info.get("thumbnail", ""),
            }
    except Exception as e:
        # Logar o erro internamente em produção, mas nunca exibir detalhes do sistema para o usuário externo
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Não foi possível processar este vídeo. Verifique se o link é público.",
        )


# --- ROTAS DA API (ENDPOINTS) ---
@router.post("/processar", response_model=DownloadResponse)
async def process_youtube_video(
    request: DownloadRequest, current_user: dict = Depends(get_current_user)
):
    """
    Endpoint protegido por JWT que processa e gera o link seguro de download.
    Apenas usuários autenticados e ativos podem acessar.
    """
    # 1. Sanitização e Validação Rígida contra Input Malicioso (Injeção de Comando)
    url_limpa = request.url.strip()
    if not YOUTUBE_REGEX.match(url_limpa):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URL do YouTube inválida ou em formato não permitido por motivos de segurança.",
        )

    # 2. Validação de Assinatura (Simulação de Negócio/Escopo)
    # OBSERVAÇÃO: Em produção, aqui validaremos se o 'current_user' possui assinatura ativa no Stripe
    usuario_premium = True  # Mock da regra de negócio para a assinatura de R$ 4,99

    if not usuario_premium:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso negado. Esta funcionalidade exige uma assinatura ativa.",
        )

    # 3. Execução Segura do Extrator
    resultado = extrair_midia_com_seguranca(url_limpa)

    return DownloadResponse(
        title=resultado["title"],
        download_url=resultado["download_url"],
        duration=resultado["duration"],
        thumbnail=resultado["thumbnail"],
    )
