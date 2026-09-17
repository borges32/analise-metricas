"""Garante que o DDL único está embarcado, é idempotente por construção e cobre
todos os objetos que loader e plugin consomem."""

from __future__ import annotations

import re

import pytest

from loader import schema

DDL = schema.ler_ddl()

# Objetos exigidos pelo loader (db.py) e pelo plugin-analitico (sqlSafe.ts).
OBJETOS = [
    "metricas.config",
    "metricas.dim_serie",
    "metricas.metrica_minuto",
    "metricas.metrica_minuto_app",
    "metricas.metrica_dia_alvo",
    "metricas.perfil_mediano_alvo",
    "metricas.stats_alvo",
    "metricas.staging_metrica",
    "metricas.criar_particoes",
    "metricas.criar_particoes_intervalo",
    "metricas.aplicar_retencao",
    "metricas.processar_staging",
    "metricas.atualizar_perfil_mediano",
    "metricas.atualizar_stats_alvo",
    "metricas.fn_grafico",
    "metricas.fn_perfil",
]


@pytest.mark.parametrize("objeto", OBJETOS)
def test_objeto_definido_no_schema(objeto: str) -> None:
    assert re.search(rf"CREATE (OR REPLACE )?[A-Z ]*{re.escape(objeto)}\b", DDL), objeto


def test_tabelas_sao_idempotentes() -> None:
    """Todo CREATE TABLE precisa de IF NOT EXISTS (o DDL é reaplicado a cada boot)."""
    faltando = [
        linha.strip()
        for linha in DDL.splitlines()
        if re.match(r"\s*CREATE (UNLOGGED )?TABLE ", linha) and "IF NOT EXISTS" not in linha
    ]
    assert faltando == []


def test_indices_sao_idempotentes() -> None:
    faltando = [
        linha.strip()
        for linha in DDL.splitlines()
        if re.match(r"\s*CREATE INDEX ", linha) and "IF NOT EXISTS" not in linha
    ]
    assert faltando == []


def test_rotinas_fixam_search_path() -> None:
    """Sem `SET search_path`, as rotinas quebram quando o chamador não tem o
    schema `metricas` no search_path (o loader conecta com o default)."""
    rotinas = re.findall(
        r"CREATE OR REPLACE (?:FUNCTION|PROCEDURE) metricas\.(\w+)(.*?)AS \$\$",
        DDL,
        re.DOTALL,
    )
    assert rotinas, "nenhuma rotina encontrada no DDL"
    sem_search_path = [nome for nome, cabecalho in rotinas if "search_path" not in cabecalho]
    assert sem_search_path == []
