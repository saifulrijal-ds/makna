# The Makna image: used by the `init` and `app` services of docker-compose.yml.
# Build context is the repository root.
# Pinned to a patch release; raise it deliberately.
FROM python:3.12.16-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# uv is pinned to the version CI uses; installed with pip so no other registry is needed.
RUN pip install --no-cache-dir uv==0.11.32

WORKDIR /app

# Dependencies first (cached while pyproject.toml and uv.lock do not change).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

COPY config ./config
COPY data ./data
COPY deploy ./deploy

# The app runs as a normal user; the generated sample data lives in a volume it can write.
RUN useradd --create-home --uid 10001 makna \
    && mkdir -p /app/data/generated \
    && chown makna /app/data/generated
USER makna

EXPOSE 8000
CMD ["makna", "serve", "--host", "0.0.0.0", "--port", "8000"]
