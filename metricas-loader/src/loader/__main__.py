"""Entrypoint: escolhe worker (loop), backfill ou aplicação do schema pelo MODO."""

from __future__ import annotations

import logging
import sys

from . import db, schema
from .backfill import executar_backfill
from .config_metricas import carregar_config_metricas
from .settings import carregar_settings, configurar_logging
from .worker import Worker


def _aplicar_schema(settings) -> int:  # noqa: ANN001
    """MODO=schema: só provisiona o banco e sai (útil como Job/initContainer)."""
    conn = db.conectar(settings.postgres_dsn)
    try:
        schema.aplicar_schema(conn)
    finally:
        conn.close()
    return 0


def main() -> None:
    settings = carregar_settings()  # fail-fast em env obrigatória ausente
    configurar_logging(settings.log_level)
    log = logging.getLogger("loader")

    if settings.modo == "schema":
        sys.exit(_aplicar_schema(settings))

    # Valida o JSON de métricas no boot (fail-fast em schema inválido).
    config = carregar_config_metricas(settings.config_metricas_path)
    log.info(
        "configuração carregada",
        extra={"metrica": f"{len(config.ativas())} métricas ativas", "dia": settings.modo},
    )

    if settings.modo == "backfill":
        sys.exit(executar_backfill(settings))
    Worker(settings).executar()


if __name__ == "__main__":
    main()
