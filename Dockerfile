# syntax=docker/dockerfile:1
# Multi-stage build: produce wheels in a builder image, install into a slim
# runtime image. Single artifact serves both the editor (port 8765) and the
# runtime (poller / post-consume) — supervisor decides what to run from
# RUNTIME_MODE at startup.

ARG PYTHON_VERSION=3.12

# ── build stage ──────────────────────────────────────────────────────
FROM docker.io/library/python:${PYTHON_VERSION}-slim AS builder

WORKDIR /build

# Install build deps for compiled packages (uvloop, httptools …). pinned
# so the stage is reproducible.
RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential \
 && rm -rf /var/lib/apt/lists/*

# Copy source for editable install metadata. Build a wheel (no editable
# install in runtime — no src/ on the path means smaller, immutable image).
COPY pyproject.toml README* ./
COPY src/ ./src/

RUN pip wheel --no-cache-dir --wheel-dir=/wheels .

# ── runtime stage ────────────────────────────────────────────────────
FROM docker.io/library/python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="paperless-rules" \
      org.opencontainers.image.description="Rule-based document classification for paperless-ngx" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/yourname/paperless-rules"

# curl is used by the HEALTHCHECK; nothing else needed at runtime.
RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1000 paperless \
 && useradd --system --uid 1000 --gid paperless --home-dir /app paperless

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels paperless-rules \
 && rm -rf /wheels \
 && mkdir -p /data/rules /data/state \
 && chown -R paperless:paperless /data /app

ENV RULES_DIR=/data/rules \
    STATE_DIR=/data/state \
    EDITOR_HOST=0.0.0.0 \
    EDITOR_PORT=8765 \
    EDITOR_ENABLED=true \
    RUNTIME_MODE=poller \
    POLL_INTERVAL_SECONDS=60 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER paperless
EXPOSE 8765

# Health probes the editor's /api/health. Long start period lets the FastAPI
# app boot + run its lifespan handlers (paperless connectivity check).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8765/api/health || exit 1

ENTRYPOINT ["paperless-rules"]
CMD ["supervisor"]
