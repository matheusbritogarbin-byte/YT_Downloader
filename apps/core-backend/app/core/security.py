from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from pwd_context import (
    CryptContext,
)  # Biblioteca que gerencia hashing sob padrões rígidos
from app.core.config import settings

# Configuração de Hashing de Senhas usando Argon2id (Recomendação OWASP / PCI-DSS)
# Argon2id é altamente resistente a ataques de força bruta com GPUs/ASICs
pwd_context = CryptContext(
    schemes=["argon2"],
    argon2__memory_cost=65536,  # 64MB de memória para dificultar ataques massivos por hardware
    argon2__time_cost=3,  # Número de iterações do algoritmo
    argon2__parallelism=4,  # Número de threads paralelas utilizadas
)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica com tempo constante se a senha limpa corresponde ao hash criptográfico."""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Gera um hash único e seguro com salt embutido usando Argon2id."""
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    Gera um Token JWT efêmero e assinado com chave simétrica forte.
    Lê a chave secreta de forma protegida extraindo o valor real do SecretStr.
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    # Atualiza o payload com o tempo exato de expiração e o emissor da assinatura (claims padrão RFC 7519)
    to_encode.update(
        {
            "exp": expire,
            "iat": datetime.now(timezone.utc),
            "iss": "yt-downloader-backend",
        }
    )

    # Extrai o valor seguro da chave secreta (.get_secret_value()) sem expô-lo em logs
    secret_key = settings.JWT_SECRET_KEY.get_secret_value()
    encoded_jwt = jwt.encode(to_encode, secret_key, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> dict | None:
    """Decodifica e valida a assinatura/expiração do Token JWT recebido."""
    try:
        secret_key = settings.JWT_SECRET_KEY.get_secret_value()
        payload = jwt.decode(
            token,
            secret_key,
            algorithms=[settings.JWT_ALGORITHM],
            issuer="yt-downloader-backend",
        )
        return payload
    except JWTError:
        # Qualquer erro de assinatura inválida ou expiração retorna None de forma silenciosa
        return None
