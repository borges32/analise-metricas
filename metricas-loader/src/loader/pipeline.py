"""Orquestração da janela: Mimir -> staging (COPY) -> processar_staging -> watermark.

Contém também os cálculos puros de janela (testáveis sem I/O): recorte por atraso,
subdivisão e detecção de dias fechados no fuso de negócio.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

from . import db
from .config_metricas import MetricaConfig
from .mimir import MimirClient, matrix_para_linhas

log = logging.getLogger("loader.pipeline")


def agora_utc() -> datetime:
    """Wrapper de `now()` em UTC (facilita mock em teste)."""
    return datetime.now(timezone.utc)


def calcular_janela(
    watermark: datetime, agora: datetime, janela_minutos: int, atraso_minutos: int
) -> tuple[datetime, datetime]:
    """Janela [watermark, min(watermark + JANELA, agora - ATRASO)].

    Se o fim resultante for <= watermark, a janela é vazia (start == end).
    """
    teto = agora - timedelta(minutes=atraso_minutos)
    fim = min(watermark + timedelta(minutes=janela_minutos), teto)
    if fim <= watermark:
        return watermark, watermark
    return watermark, fim


def dias_fechados(inicio: datetime, fim: datetime, tz_nome: str) -> list[date]:
    """Dias (no fuso de negócio) cuja meia-noite final caiu dentro de (inicio, fim]."""
    tz = ZoneInfo(tz_nome)
    utc = timezone.utc
    d = inicio.astimezone(tz).date()
    d_fim = fim.astimezone(tz).date()
    fechados: list[date] = []
    while d <= d_fim:
        fronteira = datetime.combine(d + timedelta(days=1), time.min, tzinfo=tz).astimezone(utc)
        if inicio < fronteira <= fim:
            fechados.append(d)
        d += timedelta(days=1)
    return fechados


def subjanelas(inicio: datetime, fim: datetime, janela_minutos: int) -> list[tuple[datetime, datetime]]:
    """Divide [inicio, fim] em fatias de no máximo JANELA_MINUTOS."""
    passo = timedelta(minutes=janela_minutos)
    janelas: list[tuple[datetime, datetime]] = []
    atual = inicio
    while atual < fim:
        prox = min(atual + passo, fim)
        janelas.append((atual, prox))
        atual = prox
    return janelas


@dataclass
class ResultadoJanela:
    inicio: datetime
    fim: datetime
    linhas_recebidas: int
    linhas_gravadas: int


def carregar_janela(
    conn: psycopg.Connection,
    mimir: MimirClient,
    metricas: list[MetricaConfig],
    inicio: datetime,
    fim: datetime,
    *,
    avancar_watermark: bool,
) -> ResultadoJanela:
    """Carrega uma janela de forma atômica (staging + processar + watermark no mesmo commit)."""
    buffer = []
    for metrica in metricas:
        series = mimir.coletar_series(metrica.promql(), inicio, fim)
        linhas = matrix_para_linhas(series, metrica)
        buffer.extend(linhas)
        log.info(
            "janela metrica",
            extra={
                "metrica": metrica.nome,
                "janela_inicio": inicio.isoformat(),
                "janela_fim": fim.isoformat(),
                "series_recebidas": len(series),
                "linhas_gravadas": len(linhas),
            },
        )

    try:
        gravadas = db.copiar_staging(conn, buffer)
        db.processar_staging(conn)
        if avancar_watermark:
            db.gravar_watermark(conn, fim)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if avancar_watermark:
        log.info(
            "watermark avancado",
            extra={"watermark": fim.isoformat(), "linhas_gravadas": gravadas},
        )
    return ResultadoJanela(inicio, fim, len(buffer), gravadas)
