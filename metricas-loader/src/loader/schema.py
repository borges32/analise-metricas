"""Provisionamento do schema `metricas` pelo próprio loader (bootstrap).

O DDL é um arquivo ÚNICO e idempotente (`sql/schema.sql`, embarcado no pacote):
não há migrations incrementais. O loader o aplica no boot, de modo que apontar
`POSTGRES_DSN` para um banco vazio é suficiente para ter toda a estrutura
(schema, tabelas, partições, procedures e functions) criada.

Concorrência: a aplicação é protegida por `pg_advisory_lock`, então subir várias
instâncias/Jobs ao mesmo tempo é seguro (o segundo espera e reaplica sem efeito).
"""

from __future__ import annotations

import logging
from importlib import resources

import psycopg

log = logging.getLogger("loader.schema")

# Chave arbitrária e estável do advisory lock (namespace deste projeto).
_LOCK_ID = 782_314_501

_ARQUIVO_DDL = "schema.sql"


def ler_ddl() -> str:
    """Devolve o conteúdo do schema.sql embarcado no pacote."""
    return resources.files(__package__).joinpath("sql", _ARQUIVO_DDL).read_text(encoding="utf-8")


def aplicar_schema(conn: psycopg.Connection) -> None:
    """Aplica o DDL completo (idempotente) e commita.

    Deve ser chamado antes de qualquer outra operação de banco. Reaplicar em base
    já provisionada é no-op — `CREATE ... IF NOT EXISTS` / `CREATE OR REPLACE`.
    """
    ddl = ler_ddl()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
            cur.execute(ddl)
            versao = _versao(cur)
            cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
        conn.commit()
    except Exception:
        conn.rollback()  # libera o advisory lock junto com a transação
        raise
    log.info("schema aplicado", extra={"metrica": f"schema_versao={versao}"})


def _versao(cur: psycopg.Cursor) -> str:
    cur.execute("SELECT valor FROM metricas.config WHERE chave = 'schema_versao'")
    row = cur.fetchone()
    return row[0] if row else "?"
