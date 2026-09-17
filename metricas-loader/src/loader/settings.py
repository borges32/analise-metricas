"""Configuração via variáveis de ambiente (pydantic-settings) e logging JSON."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Campos "extras" reconhecidos no log estruturado (além dos padrões do LogRecord).
_CAMPOS_EVENTO = (
    "evento",
    "janela_inicio",
    "janela_fim",
    "metrica",
    "series_recebidas",
    "linhas_gravadas",
    "duracao_ms",
    "dia",
    "watermark",
    "erro",
)


class Settings(BaseSettings):
    """Contrato completo de variáveis de ambiente (seção 4 do prompt)."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    postgres_dsn: str = Field(..., alias="POSTGRES_DSN")
    mimir_url: str = Field(..., alias="MIMIR_URL")

    mimir_tenant: str | None = Field(None, alias="MIMIR_TENANT")
    mimir_bearer_token: str | None = Field(None, alias="MIMIR_BEARER_TOKEN")
    mimir_basic_auth_user: str | None = Field(None, alias="MIMIR_BASIC_AUTH_USER")
    mimir_basic_auth_pass: str | None = Field(None, alias="MIMIR_BASIC_AUTH_PASS")
    mimir_timeout_segundos: float = Field(60, alias="MIMIR_TIMEOUT_SEGUNDOS")

    intervalo_segundos: int = Field(600, alias="INTERVALO_SEGUNDOS")
    janela_minutos: int = Field(30, alias="JANELA_MINUTOS")
    atraso_minutos: int = Field(3, alias="ATRASO_MINUTOS")
    janela_inicial_minutos: int = Field(60, alias="JANELA_INICIAL_MINUTOS")

    config_metricas_path: str = Field("/app/config/metricas.json", alias="CONFIG_METRICAS_PATH")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # O loader provisiona o schema `metricas` no boot (DDL único e idempotente).
    # Desligue apenas se o banco for gerenciado por outro processo/DBA.
    aplicar_schema: bool = Field(True, alias="APLICAR_SCHEMA")

    modo: Literal["worker", "backfill", "schema"] = Field("worker", alias="MODO")
    backfill_inicio: date | None = Field(None, alias="BACKFILL_INICIO")
    backfill_fim: date | None = Field(None, alias="BACKFILL_FIM")

    healthcheck_path: str = Field("/tmp/healthy", alias="HEALTHCHECK_PATH")

    @model_validator(mode="after")
    def _validar_backfill(self) -> Settings:
        if self.modo == "backfill" and (self.backfill_inicio is None or self.backfill_fim is None):
            raise ValueError(
                "MODO=backfill exige BACKFILL_INICIO e BACKFILL_FIM (YYYY-MM-DD)."
            )
        if (
            self.backfill_inicio is not None
            and self.backfill_fim is not None
            and self.backfill_fim < self.backfill_inicio
        ):
            raise ValueError("BACKFILL_FIM não pode ser anterior a BACKFILL_INICIO.")
        return self

    @property
    def mimir_base(self) -> str:
        return self.mimir_url.rstrip("/")


class _JsonFormatter(logging.Formatter):
    """Formata cada log como uma linha JSON (stdout)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "evento": record.getMessage(),
            "logger": record.name,
        }
        for campo in _CAMPOS_EVENTO:
            valor = getattr(record, campo, None)
            if valor is not None and campo != "evento":
                payload[campo] = valor
        if record.exc_info:
            payload["erro"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configurar_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def carregar_settings() -> Settings:
    """Carrega e valida as settings (fail-fast em variável obrigatória ausente)."""
    return Settings()  # type: ignore[call-arg]
