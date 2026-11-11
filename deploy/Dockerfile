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
RUN uv sync --locked --no-dev --no-install-project

# The default embedding model is downloaded once, here, into a fixed directory of the image,
# so that the running stack never needs the network (NFR-3, ARCHITECTURE section 10). The model
# and the directory must equal embedding.fastembed in config/makna.toml (a test checks that).
# This layer sits before `COPY src` and `COPY config`, so editing the code or the settings does
# not fetch it again.
ARG MAKNA_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
ENV MAKNA_EMBEDDING_CACHE_DIR=/opt/makna/models
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(model_name='$MAKNA_EMBEDDING_MODEL', cache_dir=os.environ['MAKNA_EMBEDDING_CACHE_DIR']); print('model ready')"
# From here on nothing may reach the network for a model: fastembed and the Hugging Face client
# both stay offline.
ENV HF_HUB_OFFLINE=1

COPY src ./src
RUN uv sync --locked --no-dev

COPY config ./config
COPY data ./data
COPY semantic ./semantic
COPY deploy ./deploy

# The app runs as a normal user; the generated sample data lives in a volume it can write.
RUN useradd --create-home --uid 10001 makna \
    && mkdir -p /app/data/generated \
    && chown makna /app/data/generated \
    && chown -R makna /opt/makna/models
USER makna

EXPOSE 8000
CMD ["makna", "serve", "--host", "0.0.0.0", "--port", "8000"]
