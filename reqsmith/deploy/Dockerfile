# Single container: FastAPI + in-process worker loop (Azure Container Apps, scale-to-zero)
FROM python:3.11-slim AS base

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY prompts ./prompts
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir .

EXPOSE 8000

# Migrations run on boot, then the app (cold-start tolerant: webhooks ACK before work)
CMD ["sh", "-c", "alembic upgrade head && uvicorn reqsmith.api.app:create_app --factory --host 0.0.0.0 --port ${SERVER_PORT:-8000}"]
