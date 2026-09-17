"""Camada de banco (psycopg 3): COPY binário, watermark e chamadas de procedures.

Regras invioláveis (README seção 8):
- Escrita na staging só via COPY binário (nunca INSERT multi-row).
- Escrita final só via CALL processar_staging().
- Watermark avançado na MESMA transação do processar_staging().
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

import psycopg

from .mimir import LinhaStaging

WATERMARK_CHAVE = "loader_watermark"

_COPY_SQL = (
    "COPY metricas.staging_metrica (ts, produto, app, jornada, escopo, status, valor) "
    "FROM STDIN (FORMAT BINARY)"
)
_COPY_TYPES = ["timestamptz", "text", "text", "text", "text", "text", "int8"]


def conectar(dsn: str) -> psycopg.Connection:
    """Abre conexão com transação explícita (autocommit desligado)."""
    return psycopg.connect(dsn, autocommit=False)


def criar_particoes(conn: psycopg.Connection) -> None:
    """Defesa idempotente contra partição inexistente (início de cada rodada)."""
    with conn.cursor() as cur:
        cur.execute("SELECT metricas.criar_particoes();")


def timezone_negocio(conn: psycopg.Connection) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT valor FROM metricas.config WHERE chave = 'timezone_negocio'")
        row = cur.fetchone()
    return row[0] if row else "America/Sao_Paulo"


def ler_watermark(conn: psycopg.Connection, default_ts: datetime) -> datetime:
    """Lê o watermark; cria com `default_ts` se ausente."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT valor FROM metricas.config WHERE chave = %s", (WATERMARK_CHAVE,)
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO metricas.config (chave, valor) VALUES (%s, %s) "
                "ON CONFLICT (chave) DO NOTHING",
                (WATERMARK_CHAVE, default_ts.isoformat()),
            )
            return default_ts
    return datetime.fromisoformat(row[0])


def gravar_watermark(conn: psycopg.Connection, ts: datetime) -> None:
    """Persiste o watermark (upsert em metricas.config)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO metricas.config (chave, valor) VALUES (%s, %s) "
            "ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor",
            (WATERMARK_CHAVE, ts.isoformat()),
        )


def copiar_staging(conn: psycopg.Connection, linhas: Iterable[LinhaStaging]) -> int:
    """Grava linhas na staging via COPY binário. Retorna a contagem escrita."""
    total = 0
    with conn.cursor() as cur, cur.copy(_COPY_SQL) as copy:
        copy.set_types(_COPY_TYPES)
        for linha in linhas:
            copy.write_row(linha)
            total += 1
    return total


def processar_staging(conn: psycopg.Connection) -> None:
    """Move a staging para as tabelas finais (idempotente) e trunca a staging."""
    with conn.cursor() as cur:
        cur.execute("CALL metricas.processar_staging();")


def atualizar_stats_alvo(conn: psycopg.Connection, dia: date) -> None:
    """Recalcula recordes/mediana do dia fechado (incremental)."""
    with conn.cursor() as cur:
        cur.execute("CALL metricas.atualizar_stats_alvo(%s);", (dia,))


def atualizar_perfil_mediano(conn: psycopg.Connection) -> None:
    """Recalcula o perfil mediano (dia típico) dos últimos 90 dias."""
    with conn.cursor() as cur:
        cur.execute("CALL metricas.atualizar_perfil_mediano();")


def criar_particoes_intervalo(conn: psycopg.Connection, d_ini: date, d_fim: date) -> None:
    """Cria partições RETROATIVAS cobrindo [d_ini, d_fim] (idempotente).

    `criar_particoes()` só cobre presente->futuro; a carga histórica (backfill /
    import CSV) precisa das semanas/meses passados. A lógica de nomes vive no
    schema (`metricas.criar_particoes_intervalo`), fonte única do DDL.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT metricas.criar_particoes_intervalo(%s, %s);", (d_ini, d_fim))
