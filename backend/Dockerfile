# ==============================================================================
# STAGE 1: BUILDER (Multi-Stage Build para Performance e Imagem Enxuta)
# Compila e instala pacotes Python em um ambiente virtual isolado (/opt/venv)
# compatível tanto com x86_64 (Contabo VPS) quanto com ARM64.
# ==============================================================================
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    find /opt/venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true && \
    python -m compileall -q /opt/venv

# ==============================================================================
# STAGE 2: RUNTIME HARDENED (Execução Non-Root Restrita - UID/GID 1000)
# Sem binários de rede desnecessários (como curl/wget) para reduzir superfície
# ==============================================================================
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONOPTIMIZE=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app

# Instala apenas tzdata (healthcheck usa urllib nativo do Python, sem curl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/* && \
    groupadd -g 1000 nonroot && \
    useradd -u 1000 -g nonroot -s /usr/sbin/nologin -M nonroot && \
    mkdir -p /app/data/ota && \
    chown -R 1000:1000 /app && \
    chmod 700 /app/data /app/data/ota

COPY --from=builder --chown=1000:1000 /opt/venv /opt/venv
COPY --chown=1000:1000 ./app /app/app

# Executa estritamente sem privilégios de root
USER 1000:1000

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=4s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "120", "--timeout-keep-alive", "30", "--no-server-header", "--proxy-headers", "--forwarded-allow-ips", "*"]
