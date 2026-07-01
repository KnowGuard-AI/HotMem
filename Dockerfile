# syntax=docker/dockerfile:1.7

# ── Builder ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --upgrade pip && \
    pip wheel --no-deps --wheel-dir /wheels . && \
    pip wheel --wheel-dir /wheels click fastapi httpx uvicorn

# ── Runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="HotMem" \
      org.opencontainers.image.description="Local-first memory sidecar for agent applications" \
      org.opencontainers.image.source="https://github.com/KnowGuard-AI/HotMem" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 1001 hotmem && \
    useradd --system --uid 1001 --gid hotmem --create-home hotmem && \
    mkdir -p /data && chown hotmem:hotmem /data

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

USER hotmem
VOLUME ["/data"]
EXPOSE 8711

ENTRYPOINT ["hotmem", "serve", "--mount", "/data", "--host", "0.0.0.0", "--port", "8711"]
