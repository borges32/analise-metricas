"""Regras de validação da carga histórica por métrica."""

from __future__ import annotations

from datetime import date

import pytest

from loader.config_metricas import ConfigMetricas
from loader.historico import (
    ErroCarga,
    selecionar_metrica,
    validar_existe_no_analitico,
    validar_periodo,
)

LABELS = {"app": "app", "jornada": "jornada", "escopo": "escopo", "status": "status"}

CONFIG = ConfigMetricas.model_validate(
    {
        "metricas": [
            {"nome": "bradesco_app_mobilepf_total", "produto": "mobilepf",
             "labels": LABELS, "ativa": True},
            {"nome": "bradesco_app_desativada_total", "produto": "desativada",
             "labels": LABELS, "ativa": False},
        ]
    }
)


class _CursorFake:
    def __init__(self, total: int) -> None:
        self._total = total
        self.sql: str | None = None
        self.params: tuple | None = None

    def __enter__(self) -> _CursorFake:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.sql, self.params = sql, params

    def fetchone(self) -> tuple[int]:
        return (self._total,)


class _ConnFake:
    def __init__(self, total: int) -> None:
        self._cur = _CursorFake(total)

    def cursor(self) -> _CursorFake:
        return self._cur


def test_seleciona_metrica_mapeada() -> None:
    m = selecionar_metrica(CONFIG, "bradesco_app_mobilepf_total")
    assert m.produto == "mobilepf"


def test_recusa_metrica_nao_mapeada() -> None:
    with pytest.raises(ErroCarga) as exc:
        selecionar_metrica(CONFIG, "metrica_inventada_total")
    # A mensagem ajuda o operador listando o que existe.
    assert "não está mapeada" in str(exc.value)
    assert "bradesco_app_mobilepf_total" in str(exc.value)


def test_recusa_metrica_inativa() -> None:
    with pytest.raises(ErroCarga, match="ativa"):
        selecionar_metrica(CONFIG, "bradesco_app_desativada_total")


def test_recusa_metrica_sem_series_no_analitico() -> None:
    metrica = selecionar_metrica(CONFIG, "bradesco_app_mobilepf_total")
    with pytest.raises(ErroCarga, match="não existe no analítico"):
        validar_existe_no_analitico(_ConnFake(0), metrica)


def test_aceita_metrica_com_series_no_analitico() -> None:
    metrica = selecionar_metrica(CONFIG, "bradesco_app_mobilepf_total")
    conn = _ConnFake(13)
    assert validar_existe_no_analitico(conn, metrica) == 13
    # A checagem é pelo produto da métrica, não pelo nome no Mimir.
    assert conn._cur.params == ("mobilepf",)


def test_recusa_periodo_invertido() -> None:
    with pytest.raises(ErroCarga, match="anterior"):
        validar_periodo(date(2026, 9, 10), date(2026, 9, 1))


def test_aceita_periodo_de_um_dia() -> None:
    validar_periodo(date(2026, 9, 10), date(2026, 9, 10))
