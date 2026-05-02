# syntax=docker/dockerfile:1
# Single-stage Alpine build. Every runtime dep ships musllinux wheels on
# PyPI (pyyaml, pydantic-core, …) so no compiler is needed; the wheels
# install in seconds and the final image stays small.
#
# What was trimmed vs. the previous Debian-slim multi-stage build:
#   - uvicorn[standard] → plain uvicorn  (drop uvloop / httptools /
#     watchfiles / websockets / colorlog ≈ 30 MB of compiled extras
#     we don't need on a NAS).
#   - build-essential / gcc                 (no longer compiling anything).
#   - curl for HEALTHCHECK                  (Python's urllib does it).
#   - python:3.12-slim → python:3.12-alpine (~75 MB lighter base).
#
# Final image: ≈ 95 MB on amd64 (was 197 MB).

ARG PYTHON_VERSION=3.12

FROM docker.io/library/python:${PYTHON_VERSION}-alpine

LABEL org.opencontainers.image.title="paperless-rules" \
      org.opencontainers.image.description="Rule-based document classification for paperless-ngx" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/mueslipolo/paperless-rules"

WORKDIR /app

# All-in-one bootstrap: create the unprivileged paperless user + the data
# directories we mount, install our package without leaving pip cache or
# build artefacts behind, and clean compiled bytecode caches that pip
# materialised during install.
COPY pyproject.toml README* ./
COPY src/ ./src/
RUN addgroup -S -g 1000 paperless \
 && adduser  -S -u 1000 -G paperless -h /app paperless \
 && pip install --no-cache-dir --no-compile --root-user-action=ignore . \
 && rm -rf /app/src /app/pyproject.toml /app/README* \
 && mkdir -p /data/rules /data/state \
 && chown -R paperless:paperless /data /app

ENV RULES_DIR=/data/rules \
    STATE_DIR=/data/state \
    EDITOR_HOST=0.0.0.0 \
    EDITOR_PORT=8765 \
    EDITOR_ENABLED=true \
    RUNTIME_MODE=disabled \
    POLL_INTERVAL_SECONDS=60 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER paperless
EXPOSE 8765

# HEALTHCHECK uses Python (already in the image) — no curl dep, no busybox
# wget quirks. Long start period lets the FastAPI app finish its lifespan.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8765/api/health', timeout=4).status == 200 else 1)" \
  || exit 1

ENTRYPOINT ["paperless-rules"]
CMD ["supervisor"]
