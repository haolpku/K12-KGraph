FROM python:3.11-slim

ARG UV_VERSION=0.8.16

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/src \
    RETRIEVAL_API_APP=retrieval.api:app \
    RETRIEVAL_API_WORKERS=1 \
    RETRIEVAL_LOG_LEVEL=info

WORKDIR /app

RUN pip install --upgrade pip "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY . /app

EXPOSE 8000
CMD ["sh", "-c", "python /app/docker/scripts/wait_for_neo4j.py && exec uvicorn ${RETRIEVAL_API_APP} --host 0.0.0.0 --port 8000 --workers ${RETRIEVAL_API_WORKERS} --log-level ${RETRIEVAL_LOG_LEVEL}"]
