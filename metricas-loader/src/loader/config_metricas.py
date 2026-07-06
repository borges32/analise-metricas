"""Parse e validação do arquivo JSON de métricas (seção 3 do prompt)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

# Colunas da staging que precisam ser mapeadas a partir das labels do Mimir.
COLUNAS_LABEL = ("app", "jornada", "escopo", "status")

# Valor gravado quando a label não vem na série (colunas são NOT NULL).
LABEL_AUSENTE = "desconhecido"


class MetricaConfig(BaseModel):
    """Uma métrica declarada no JSON de configuração."""

    nome: str = Field(..., min_length=1)
    produto: str = Field(..., min_length=1)
    labels: dict[str, str]
    ativa: bool = True

    @field_validator("labels")
    @classmethod
    def _validar_labels(cls, v: dict[str, str]) -> dict[str, str]:
        faltando = [c for c in COLUNAS_LABEL if c not in v]
        if faltando:
            raise ValueError(
                f"labels deve conter as chaves obrigatórias {COLUNAS_LABEL}; "
                f"faltando: {faltando}"
            )
        return v

    def promql(self) -> str:
        """`sum by ({labels_mimir}) (increase({nome}[1m]))` (seção 2)."""
        labels_mimir = ", ".join(self.labels[c] for c in COLUNAS_LABEL)
        return f"sum by ({labels_mimir}) (increase({self.nome}[1m]))"


class ConfigMetricas(BaseModel):
    """Raiz do arquivo de configuração."""

    metricas: list[MetricaConfig]

    def ativas(self) -> list[MetricaConfig]:
        return [m for m in self.metricas if m.ativa]


def carregar_config_metricas(caminho: str | Path) -> ConfigMetricas:
    """Lê e valida o JSON. Erro de schema/arquivo -> exceção clara (fail-fast)."""
    p = Path(caminho)
    if not p.is_file():
        raise FileNotFoundError(f"arquivo de configuração de métricas não encontrado: {p}")
    try:
        dados = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON inválido em {p}: {exc}") from exc
    return ConfigMetricas.model_validate(dados)
