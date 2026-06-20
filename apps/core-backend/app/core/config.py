from pathlib import Path
from typing import Any, cast
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    PROJECT_NAME: str = "YT Downloader - Industrial API"
    ENVIRONMENT: str = Field(
        default="development", pattern="^(development|staging|production)$"
    )
    FRONTEND_URL: str = Field(default="http://localhost:3000")

    JWT_SECRET_KEY: SecretStr | None = Field(default=cast(Any, None))
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15

    STRIPE_SECRET_KEY: SecretStr | None = Field(default=cast(Any, None))
    STRIPE_WEBHOOK_SECRET: SecretStr | None = Field(default=cast(Any, None))
    STRIPE_PRICE_ID_PREMIUM: str | None = Field(default=cast(Any, None))

    REDIS_URL: str = Field(default="redis://localhost:6379/0")

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


try:
    settings = Settings()
except Exception as e:
    raise e
