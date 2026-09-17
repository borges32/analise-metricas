"""Carga de histórico de UMA métrica, por período, a partir do Mimir.

Pensado para ser chamado dentro do container, sob demanda:

    docker exec metricas-loader python -m loader.historico \\
        --metrica bradesco_app_mobilepf_total --inicio 2026-09-01 --fim 2026-09-10

Garantias:

- **Não duplica registro.** A escrita final passa pela mesma
  `metricas.processar_staging()` do fluxo normal, que é idempotente: o fato
  detalhado tem PK `(serie_id, ts)` e a gravação é um upsert que *substitui* o
  valor do minuto. Reexecutar o mesmo período produz exatamente o mesmo estado.
- **Só carrega métrica já existente no analítico.** A métrica precisa estar
  declarada e ativa no `metricas.json` (o mapeamento label->coluna sem o qual
  não há como interpretar a série) e já ter séries em `metricas.dim_serie`.
  Para a primeira carga de uma métrica recém-mapeada, use `--permitir-nova`.
- **Não mexe no watermark** do worker: a carga contínua segue de onde estava.

O período é inclusivo nas duas pontas e interpretado no fuso de negócio
(`metricas.config.timezone_negocio`). O fim é limitado a `now - ATRASO_MINUTOS`
para nunca ler um minuto ainda incompleto no Mimir.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

from . import db, schema
from .config_metricas import ConfigMetricas, MetricaConfig, carregar_config_metricas
from .mimir import criar_mimir_client
from .pipeline import agora_utc, carregar_janela, subjanelas
from .settings import Settings, carregar_settings, configurar_logging

log = logging.getLogger("loader.historico")


class ErroCarga(RuntimeError):
    """Erro de validação da carga histórica (mensagem já pronta para o usuário)."""


@dataclass
class Resultado:
    dias: int
    linhas: int


def _parse_data(valor: str) -> date:
    try:
        return date.fromisoformat(valor)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"data inválida '{valor}' (use YYYY-MM-DD)") from exc


def selecionar_metrica(config: ConfigMetricas, nome: str) -> MetricaConfig:
    """Resolve o nome da métrica no `metricas.json` (declarada e ativa)."""
    for m in config.metricas:
        if m.nome == nome:
            if not m.ativa:
                raise ErroCarga(
                    f"métrica '{nome}' está com \"ativa\": false no metricas.json. "
                    "Ative-a antes de carregar o histórico."
                )
            return m
    disponiveis = ", ".join(sorted(m.nome for m in config.ativas())) or "(nenhuma)"
    raise ErroCarga(
        f"métrica '{nome}' não está mapeada no analítico. Métricas ativas: {disponiveis}"
    )


def validar_existe_no_analitico(conn: psycopg.Connection, metrica: MetricaConfig) -> int:
    """Exige que a métrica já tenha séries em `dim_serie`. Retorna a contagem."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM metricas.dim_serie WHERE produto = %s", (metrica.produto,)
        )
        (total,) = cur.fetchone()
    if total == 0:
        raise ErroCarga(
            f"métrica '{metrica.nome}' (produto '{metrica.produto}') ainda não tem séries "
            "em metricas.dim_serie — não existe no analítico. Deixe o worker coletá-la ao "
            "menos uma vez, ou use --permitir-nova para carregar o histórico mesmo assim."
        )
    return total


def validar_periodo(inicio: date, fim: date) -> None:
    if fim < inicio:
        raise ErroCarga(f"--fim ({fim}) não pode ser anterior a --inicio ({inicio}).")


def executar(
    settings: Settings,
    nome_metrica: str,
    inicio: date,
    fim: date,
    *,
    permitir_nova: bool = False,
    atualizar_perfil: bool = True,
) -> Resultado:
    """Carrega [inicio, fim] (inclusivo, fuso de negócio) da métrica informada."""
    validar_periodo(inicio, fim)
    metrica = selecionar_metrica(carregar_config_metricas(settings.config_metricas_path), nome_metrica)

    mimir = criar_mimir_client(settings)
    conn = db.conectar(settings.postgres_dsn)
    total_linhas = 0
    dias_carregados: list[date] = []
    try:
        if settings.aplicar_schema:
            schema.aplicar_schema(conn)

        if permitir_nova:
            log.warning(
                "validação de existência ignorada (--permitir-nova)",
                extra={"metrica": metrica.nome},
            )
        else:
            series = validar_existe_no_analitico(conn, metrica)
            log.info(
                "métrica validada no analítico",
                extra={"metrica": metrica.nome, "series_recebidas": series},
            )

        tz = ZoneInfo(db.timezone_negocio(conn))
        teto = agora_utc() - timedelta(minutes=settings.atraso_minutos)
        db.criar_particoes_intervalo(conn, inicio, fim)
        conn.commit()

        log.info(
            "carga histórica iniciada",
            extra={
                "metrica": metrica.nome,
                "janela_inicio": inicio.isoformat(),
                "janela_fim": fim.isoformat(),
            },
        )

        dia = inicio
        while dia <= fim:
            d_ini = datetime.combine(dia, time.min, tzinfo=tz).astimezone(timezone.utc)
            d_fim = min(
                datetime.combine(dia + timedelta(days=1), time.min, tzinfo=tz).astimezone(
                    timezone.utc
                ),
                teto,
            )
            if d_fim <= d_ini:
                log.info("dia futuro/incompleto, pulando", extra={"dia": dia.isoformat()})
                dia += timedelta(days=1)
                continue

            linhas_dia = 0
            for s_ini, s_fim in subjanelas(d_ini, d_fim, settings.janela_minutos):
                # avancar_watermark=False: a carga histórica não interfere no worker.
                r = carregar_janela(
                    conn, mimir, [metrica], s_ini, s_fim, avancar_watermark=False
                )
                linhas_dia += r.linhas_gravadas

            # Recordes/mediana do dia (idempotente).
            db.atualizar_stats_alvo(conn, dia)
            conn.commit()

            total_linhas += linhas_dia
            dias_carregados.append(dia)
            log.info(
                "dia carregado",
                extra={"metrica": metrica.nome, "dia": dia.isoformat(),
                       "linhas_gravadas": linhas_dia},
            )
            dia += timedelta(days=1)

        if atualizar_perfil and dias_carregados:
            db.atualizar_perfil_mediano(conn)
            conn.commit()
            log.info("perfil mediano recalculado")

        log.info(
            "carga histórica concluída",
            extra={"metrica": metrica.nome, "linhas_gravadas": total_linhas,
                   "dia": f"{len(dias_carregados)} dias"},
        )
        return Resultado(dias=len(dias_carregados), linhas=total_linhas)
    finally:
        conn.close()
        mimir.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m loader.historico",
        description="Carga de histórico de uma métrica já mapeada, por período.",
    )
    ap.add_argument("--metrica", required=True,
                    help="nome da métrica no Mimir, como declarado no metricas.json")
    ap.add_argument("--inicio", required=True, type=_parse_data,
                    help="primeiro dia do período (YYYY-MM-DD, fuso de negócio)")
    ap.add_argument("--fim", required=True, type=_parse_data,
                    help="último dia do período, inclusivo (YYYY-MM-DD)")
    ap.add_argument("--permitir-nova", action="store_true",
                    help="permite carregar métrica que ainda não tem séries em dim_serie")
    ap.add_argument("--sem-perfil", action="store_true",
                    help="não recalcular o perfil mediano ao final")
    args = ap.parse_args(argv)

    settings = carregar_settings()
    configurar_logging(settings.log_level)

    try:
        executar(
            settings,
            args.metrica,
            args.inicio,
            args.fim,
            permitir_nova=args.permitir_nova,
            atualizar_perfil=not args.sem_perfil,
        )
    except ErroCarga as exc:
        log.error("carga histórica recusada", extra={"erro": str(exc)})
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
