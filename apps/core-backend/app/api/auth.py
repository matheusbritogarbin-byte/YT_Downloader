from datetime import datetime, timedelta, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

# Configuração de Hashing Seguro de Senhas (Bcrypt com fator de custo industrial)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Configuração do fluxo de leitura do Token JWT no Header da requisição
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/token")

router = APIRouter(prefix="/auth", tags=["Authentication"])

# --- CONFIGURAÇÕES DE SEGURANÇA (Injetadas em produção via Variáveis de Ambiente) ---
# Em produção, esses valores serão lidos estritamente do arquivo core/config.py
JWT_SECRET = "MUDAR_PARA_UMA_CHAVE_ULTRA_SECRETA_E_LONGA_EM_PRODUCAO"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = (
    15  # Token de curta duração reduz o risco de roubo de sessão
)


# --- MODELOS DE DADOS PARA VALIDAÇÃO RÍGIDA ---
class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    email: str | None = None


# --- FUNÇÕES INTERNAS DE SEGURANÇA ---
def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica se a senha enviada bate com o hash seguro salvo no banco."""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Gera o hash Bcrypt da senha antes de salvar qualquer registro."""
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Gera um token JWT blindado com expiração estrita e carimbo de data (UTC)."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)
    return encoded_jwt


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]):
    """Middleware de Injeção de Dependência para proteger rotas confidenciais."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Não foi possível validar as credenciais de acesso.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        token_data = TokenData(email=email)
    except JWTError:
        raise credentials_exception

    return token_data


# --- ROTAS DA API (ENDPOINTS) ---
@router.post("/token", response_model=Token)
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
):
    """Rota de login que valida as credenciais e emite o token de acesso temporário."""
    # OBSERVAÇÃO: Em produção, a validação abaixo buscará os dados criptografados do banco de dados
    # Simulando um usuário cadastrado para fins de Scaffold inicial seguro
    user_placeholder_email = "cliente@exemplo.com"
    user_placeholder_hash = get_password_hash(
        "SenhaSuperSegura123"
    )  # Exemplo de hash salvo

    if form_data.username != user_placeholder_email or not verify_password(
        form_data.password, user_placeholder_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="E-mail ou senha incorretos.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": form_data.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}
