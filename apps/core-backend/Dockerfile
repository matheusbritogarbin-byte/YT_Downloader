# Imagem base oficial do Python em versão estável e reduzida (Slim)
FROM python:3.11-slim

WORKDIR /app

# Camada de Segurança: Cria um usuário comum sem privilégios administrativos
RUN useradd -m appuser && chown -R appuser /app

# Altera a execução do container para o usuário seguro criado acima
USER appuser

# Configurações do Python para performance em containers (evita cache em disco e travamentos de log)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/home/appuser/.local/bin:${PATH}"

# Copia o requirements.txt primeiro para aproveitar o cache de camadas do Docker
COPY --chown=appuser:appuser requirements.txt .

# Instala as dependências travadas de forma segura isolada no escopo do usuário
RUN pip install --no-cache-dir --user -r requirements.txt

# Copia todo o código da pasta app para dentro do container com as permissões restritas
COPY --chown=appuser:appuser . .

# Expõe a porta de rede interna oficial da API
EXPOSE 8000

# Executa o servidor ASGI Uvicorn apontando para o arquivo principal app.main
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
