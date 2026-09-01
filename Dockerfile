# syntax=docker/dockerfile:1.7
# Reusable CPU image. Models and Piper voices are downloaded into /config on first use.
FROM python:3.12-slim-bookworm

ARG TORCH_VERSION=2.6.0
ARG VCS_REF=""
ARG BUILD_DATE=""

LABEL org.opencontainers.image.title="OpenPronounce" \
      org.opencontainers.image.description="Self-hosted phoneme-level pronunciation assessment API" \
      org.opencontainers.image.source="https://github.com/KenGrinder/OpenPronounce" \
      org.opencontainers.image.url="https://github.com/KenGrinder/OpenPronounce" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/config \
    HF_HOME=/config/huggingface \
    XDG_CACHE_HOME=/config/cache \
    OPENPRONOUNCE_CACHE_DIR=/config/tts \
    OPENPRONOUNCE_TTS=piper \
    OPENPRONOUNCE_DEVICE=cpu \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install "torch==${TORCH_VERSION}" --index-url https://download.pytorch.org/whl/cpu

COPY pyproject.toml README.md ./
COPY openpronounce ./openpronounce
RUN python -m pip install ".[app,tts-piper]"

COPY server.py ./
COPY templates ./templates
COPY static ./static

RUN mkdir -p /config/huggingface /config/cache /config/tts \
    && chown -R 99:100 /config /app

USER 99:100
VOLUME ["/config"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-127.0.0.1}"]
