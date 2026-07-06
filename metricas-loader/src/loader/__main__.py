"""Entrypoint: escolhe worker (loop) ou backfill pelo MODO."""

from __future__ import annotations

import logging
import sys

from .backfill import executar_backfill
from .config_metricas import carregar_config_metricas
from .settings import carregar_settings, configurar_logging
from .worker import Worker


def main() -> None:
    settings = carregar_settings()  # fail-fast em env obrigatória ausente
    configurar_logging(settings.log_level)
    log = logging.getLogger("loader")

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
