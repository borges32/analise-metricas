"""Camada de banco (psycopg 3): COPY binário, watermark e chamadas de procedures.

Regras invioláveis (README seção 8):
- Escrita na staging só via COPY binário (nunca INSERT multi-row).
- Escrita final só via CALL processar_staging().
- Watermark avançado na MESMA transação do processar_staging().
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta

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
    """Cria partições RETROATIVAS cobrindo [d_ini, d_fim].

    Mesma convenção de nomes de `criar_particoes()` (semanal ISO em metrica_minuto,
    mensal em metrica_minuto_app), de forma idempotente (CREATE TABLE IF NOT EXISTS).
    Necessário para carga histórica: `criar_particoes()` só cobre presente->futuro.
    """
    stmts: list[tuple[str, date, date, str]] = []

    # Semanais (metrica_minuto): segunda-feira a segunda-feira (ISO week).
    pw = d_ini - timedelta(days=d_ini.weekday())  # segunda da semana de d_ini
    while pw <= d_fim:
        iso_year, iso_week, _ = pw.isocalendar()
        nome = f"metrica_minuto_{iso_year}w{iso_week:02d}"
        stmts.append((nome, pw, pw + timedelta(days=7), "metrica_minuto"))
        pw += timedelta(days=7)

    # Mensais (metrica_minuto_app).
    pm = d_ini.replace(day=1)
    while pm <= d_fim:
        nxt = (pm.replace(year=pm.year + 1, month=1)
               if pm.month == 12 else pm.replace(month=pm.month + 1))
        nome = f"metrica_minuto_app_{pm.year:04d}_{pm.month:02d}"
        stmts.append((nome, pm, nxt, "metrica_minuto_app"))
        pm = nxt

    with conn.cursor() as cur:
        for nome, ini, fim, parent in stmts:
            # nome/datas são gerados internamente (sem input externo) — seguro interpolar.
            cur.execute(
                f'CREATE TABLE IF NOT EXISTS metricas."{nome}" '
                f'PARTITION OF metricas.{parent} '
                f"FOR VALUES FROM ('{ini.isoformat()}') TO ('{fim.isoformat()}')"
            )
