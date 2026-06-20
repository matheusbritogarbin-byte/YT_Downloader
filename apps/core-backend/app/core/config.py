from pathlib import Path
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Define o caminho raiz do backend para localizar o arquivo .env se necessário
BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """
    Classe de configuração global do sistema com validação rígida de tipos.
    Utiliza SecretStr para chaves confidenciais para que elas não vazem em logs de erro ou prints involuntários.
    """

    # --- CONFIGURAÇÕES DA API ---
    PROJECT_NAME: str = "YT Downloader - Industrial API"
    ENVIRONMENT: str = Field(
        default="development", pattern="^(development|staging|production)$"
    )
    FRONTEND_URL: str = Field(default="http://localhost:3000")

    # --- CHAVES DE SEGURANÇA E SESSÃO ---
    # SecretStr oculta o valor real ao dar print() ou gerar logs automáticos
    JWT_SECRET_KEY: SecretStr
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15

    # --- GATEWAY STRIPE ---
    STRIPE_SECRET_KEY: SecretStr
    STRIPE_WEBHOOK_SECRET: SecretStr
    STRIPE_PRICE_ID_PREMIUM: str

    # --- INFRAESTRUTURA ---
    REDIS_URL: str = Field(default="redis://localhost:6379/0")

    # Configurações do comportamento do Pydantic Settings
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,  # Obriga as variáveis no .env a serem MAIÚSCULAS
        extra="ignore",  # Ignora variáveis extras que não foram mapeadas aqui
    )


# Instancia a configuração para ser importada como um Singleton em toda a aplicação
try:
    settings = Settings()
except Exception as e:
    # Em um ambiente industrial, isso previne o deploy de rodar com chaves ausentes
    print(f"❌ ERRO CRÍTICO DE CONFIGURAÇÃO DE SEGURANÇA: {e}")
    raise e
