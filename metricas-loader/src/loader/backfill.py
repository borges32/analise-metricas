"""Modo backfill: carga histórica por dia, com atualizar_stats_alvo ao fechar cada dia.

Não entra em loop e não toca o watermark do modo worker (avancar_watermark=False).
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .config_metricas import carregar_config_metricas
from .mimir import criar_mimir_client
from .pipeline import agora_utc, carregar_janela, subjanelas
from .settings import Settings

log = logging.getLogger("loader.backfill")


def executar_backfill(settings: Settings) -> int:
    mimir = criar_mimir_client(settings)
    conn = db.conectar(settings.postgres_dsn)
    try:
        db.criar_particoes(conn)
        conn.commit()

        tz = ZoneInfo(db.timezone_negocio(conn))
        metricas = carregar_config_metricas(settings.config_metricas_path).ativas()
        teto = agora_utc() - timedelta(minutes=settings.atraso_minutos)

        assert settings.backfill_inicio and settings.backfill_fim  # validado nas settings
        dia = settings.backfill_inicio
        log.info("backfill iniciado",
                 extra={"janela_inicio": str(settings.backfill_inicio),
                        "janela_fim": str(settings.backfill_fim)})

        while dia <= settings.backfill_fim:
            inicio = datetime.combine(dia, time.min, tzinfo=tz).astimezone(timezone.utc)
            fim = datetime.combine(dia + timedelta(days=1), time.min, tzinfo=tz).astimezone(
                timezone.utc
            )
            fim = min(fim, teto)
            if fim <= inicio:
                log.info("dia incompleto/futuro, pulando", extra={"dia": dia.isoformat()})
                dia += timedelta(days=1)
                continue

            for s_ini, s_fim in subjanelas(inicio, fim, settings.janela_minutos):
                carregar_janela(conn, mimir, metricas, s_ini, s_fim, avancar_watermark=False)

            # Fecha o dia: recordes incrementais.
            db.atualizar_stats_alvo(conn, dia)
            conn.commit()
            log.info("dia backfill concluído", extra={"dia": dia.isoformat()})
            dia += timedelta(days=1)

        log.info("backfill finalizado")
        return 0
    finally:
        conn.close()
        mimir.close()
