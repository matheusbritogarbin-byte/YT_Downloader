from app.core.config import settings
from app.core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    decode_access_token,
)

# Exporta os componentes globais e criptográficos do núcleo do sistema
__all__ = [
    "settings",
    "verify_password",
    "get_password_hash",
    "create_access_token",
    "decode_access_token",
]
