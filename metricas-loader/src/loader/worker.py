"""Loop do worker: rodadas a cada INTERVALO_SEGUNDOS, com catch-up, backoff e sinais."""

from __future__ import annotations

import logging
import signal
import threading
from datetime import timedelta
from pathlib import Path

from . import db, schema
from .config_metricas import carregar_config_metricas
from .mimir import criar_mimir_client
from .pipeline import agora_utc, calcular_janela, carregar_janela, dias_fechados
from .settings import Settings

log = logging.getLogger("loader.worker")

_BACKOFF_INICIAL = 5
_BACKOFF_MAX = 300


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._parar = threading.Event()

    def _instalar_sinais(self) -> None:
        def _handler(signum, _frame):  # noqa: ANN001
            log.info("sinal recebido, encerrando", extra={"metrica": signal.Signals(signum).name})
            self._parar.set()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def _tocar_health(self) -> None:
        try:
            Path(self.s.healthcheck_path).touch()
        except OSError as exc:  # não deve derrubar o worker
            log.warning("falha ao tocar healthcheck", extra={"erro": str(exc)})

    def executar(self) -> None:
        self._instalar_sinais()
        mimir = criar_mimir_client(self.s)
        conn = db.conectar(self.s.postgres_dsn)
        if self.s.aplicar_schema:
            schema.aplicar_schema(conn)  # cria toda a estrutura se o banco estiver vazio
        backoff = _BACKOFF_INICIAL
        log.info("worker iniciado", extra={"metrica": self.s.modo})
        try:
            while not self._parar.is_set():
                try:
                    deve_dormir = self._rodada(conn, mimir)
                    backoff = _BACKOFF_INICIAL
                    self._tocar_health()
                    if deve_dormir and not self._parar.is_set():
                        self._parar.wait(self.s.intervalo_segundos)
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    log.error("rodada falhou; aplicando backoff", exc_info=exc,
                              extra={"duracao_ms": backoff * 1000})
                    self._parar.wait(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
        finally:
            conn.close()
            mimir.close()
            log.info("worker finalizado")

    def _rodada(self, conn, mimir) -> bool:  # noqa: ANN001
        # 1. Defesa idempotente de partições.
        db.criar_particoes(conn)
        conn.commit()

        tz = db.timezone_negocio(conn)
        metricas = carregar_config_metricas(self.s.config_metricas_path).ativas()

        # 2. Watermark (cria default = now - JANELA_INICIAL se ausente).
        default_wm = agora_utc() - timedelta(minutes=self.s.janela_inicial_minutos)
        watermark = db.ler_watermark(conn, default_wm)
        conn.commit()

        # 3. Janela desta rodada.
        agora = agora_utc()
        inicio, fim = calcular_janela(
            watermark, agora, self.s.janela_minutos, self.s.atraso_minutos
        )
        if fim <= inicio:
            log.info("nada a carregar (janela vazia)",
                     extra={"janela_inicio": inicio.isoformat(), "watermark": watermark.isoformat()})
            return True

        log.info("rodada iniciada",
                 extra={"janela_inicio": inicio.isoformat(), "janela_fim": fim.isoformat(),
                        "metrica": f"{len(metricas)} metricas"})

        carregar_janela(conn, mimir, metricas, inicio, fim, avancar_watermark=True)

        # 4. Stats por dia fechado no fuso de negócio.
        for dia in dias_fechados(inicio, fim, tz):
            db.atualizar_stats_alvo(conn, dia)
            conn.commit()
            log.info("stats_alvo executado", extra={"dia": dia.isoformat()})

        # 5. Catch-up: emenda rodadas enquanto a janela disponível >= JANELA_MINUTOS.
        teto = agora - timedelta(minutes=self.s.atraso_minutos)
        restante = teto - fim
        deve_dormir = restante < timedelta(minutes=self.s.janela_minutos)
        log.info("rodada concluída",
                 extra={"janela_fim": fim.isoformat(),
                        "duracao_ms": int(restante.total_seconds() * 1000)})
        return deve_dormir
