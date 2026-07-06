# ---- build stage: instala dependências num venv ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# ---- runtime stage: imagem enxuta, usuário non-root ----
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CONFIG_METRICAS_PATH=/app/config/metricas.json \
    HEALTHCHECK_PATH=/tmp/healthy

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY config ./config

RUN useradd --create-home --uid 10001 loader \
    && chown -R loader:loader /app
USER loader

# Alarma se não houver rodada bem-sucedida em ~2x o intervalo (default 10min -> 20min).
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,time,sys; p=os.getenv('HEALTHCHECK_PATH','/tmp/healthy'); \
inter=int(os.getenv('INTERVALO_SEGUNDOS','600')); \
sys.exit(0 if os.path.exists(p) and (time.time()-os.path.getmtime(p)) < 2*inter else 1)"

ENTRYPOINT ["python", "-m", "loader"]
