# -----------------------------------------------------------------------------
# metricas-loader sobre Chainguard (Wolfi) — imagem distroless, sem CVEs com
# correção pendente. Ver seção "Segurança da imagem" no README.
#
# - builder (`latest-dev`): tem shell e pip, só para montar o venv.
# - runtime (`latest`): só o Python e as libs que ele precisa. Sem shell, sem
#   gerenciador de pacotes, sem pip, sem tar. Usuário nonroot (UID 65532).
#
# As duas tags DEVEM vir do mesmo pull: o venv referencia o Python do builder.
# Sempre build com `--pull`; uma divergência de versão quebra o build na
# verificação final (não em produção).
# -----------------------------------------------------------------------------

# ---- build stage: instala dependências num venv ----
FROM cgr.dev/chainguard/python:latest-dev AS builder

USER root
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY src ./src
# O pip só é necessário para instalar; sai do venv para não ir para o runtime.
RUN pip install . \
    && pip uninstall -y pip

COPY config /app/config

# ---- runtime stage: distroless, usuário non-root ----
FROM cgr.dev/chainguard/python:latest AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CONFIG_METRICAS_PATH=/app/config/metricas.json \
    HEALTHCHECK_PATH=/tmp/healthy

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
WORKDIR /app

# Falha o BUILD se o venv não rodar neste Python (builder e runtime de pulls
# diferentes). Forma exec: a imagem não tem /bin/sh.
# Também garante a base de fusos (a imagem não tem /usr/share/zoneinfo).
RUN ["python", "-c", "import loader.__main__, loader.historico, loader.import_csv, psycopg, httpx, pydantic_settings; from zoneinfo import ZoneInfo; ZoneInfo('America/Sao_Paulo')"]

USER 65532

# Alarma se não houver rodada bem-sucedida em ~2x o intervalo (default 10min -> 20min).
# Forma exec (JSON): sem shell na imagem.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,time,sys; p=os.getenv('HEALTHCHECK_PATH','/tmp/healthy'); inter=int(os.getenv('INTERVALO_SEGUNDOS','600')); sys.exit(0 if os.path.exists(p) and (time.time()-os.path.getmtime(p)) < 2*inter else 1)"]

ENTRYPOINT ["python", "-m", "loader"]
