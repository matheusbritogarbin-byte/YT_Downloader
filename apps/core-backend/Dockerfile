FROM python:3.11-slim

WORKDIR /app

RUN useradd -m appuser && chown -R appuser /app

USER appuser

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/home/appuser/.local/bin:${PATH}"

COPY --chown=appuser:appuser requirements.txt .

RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=appuser:appuser . .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
