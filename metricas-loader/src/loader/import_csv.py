"""Carga de histórico de métricas a partir de um arquivo CSV.

O CSV tem as mesmas colunas da `metricas.staging_metrica`:
    ts, produto, app, jornada, escopo, status, valor

Fluxo (sem tocar no watermark do worker):
  1. Cria partições RETROATIVAS conforme o período do arquivo.
  2. Carrega em lotes via COPY binário -> processar_staging (idempotente).
  3. Recalcula stats (recorde/mediana) por dia e o perfil mediano.

Uso dentro do container:
    python -m loader.import_csv --file /data/historico.csv
    python -m loader.import_csv < historico.csv          # via stdin
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db
from .mimir import LinhaStaging
from .settings import configurar_logging

log = logging.getLogger("loader.import_csv")

COLUNAS = ["ts", "produto", "app", "jornada", "escopo", "status", "valor"]


def parse_ts(valor: str) -> datetime:
    """Aceita ISO 8601 ('YYYY-MM-DDTHH:MM:SS[±tz]') ou 'YYYY-MM-DD HH:MM:SS'.

    Sem timezone => assume UTC. Sempre retorna aware em UTC.
    """
    dt = datetime.fromisoformat(valor.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _linha(row: dict[str, str], truncar: bool) -> LinhaStaging:
    ts = parse_ts(row["ts"])
    if truncar:
        ts = ts.replace(second=0, microsecond=0)
    return LinhaStaging(
        ts=ts,
        produto=row["produto"].strip(),
        app=row["app"].strip(),
        jornada=row["jornada"].strip(),
        escopo=row["escopo"].strip(),
        status=row["status"].strip(),
        valor=int(round(float(row["valor"]))),
    )


def _flush(conn, buffer: list[LinhaStaging], dias: set, tz: ZoneInfo) -> int:
    if not buffer:
        return 0
    d_min = min(x.ts for x in buffer).date()
    d_max = max(x.ts for x in buffer).date()
    db.criar_particoes_intervalo(conn, d_min, d_max)  # partições retroativas
    gravadas = db.copiar_staging(conn, buffer)
    db.processar_staging(conn)
    conn.commit()
    for x in buffer:
        dias.add(x.ts.astimezone(tz).date())
    return gravadas


def main() -> None:
    ap = argparse.ArgumentParser(description="Carga de histórico via CSV")
    ap.add_argument("--file", default=None, help="caminho do CSV (default: stdin)")
    ap.add_argument("--batch", type=int, default=50_000, help="linhas por lote (default 50000)")
    ap.add_argument("--sem-truncar", action="store_true",
                    help="não truncar ts para o minuto")
    ap.add_argument("--sem-stats", action="store_true",
                    help="não recalcular stats_alvo/perfil ao final")
    args = ap.parse_args()

    configurar_logging(os.getenv("LOG_LEVEL", "INFO"))
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        sys.exit("ERRO: variável POSTGRES_DSN não definida.")

    fonte = open(args.file, newline="", encoding="utf-8") if args.file else sys.stdin
    conn = db.conectar(dsn)
    total = 0
    dias: set = set()
    origem = args.file or "stdin"
    log.info("import iniciado", extra={"metrica": origem})

    try:
        tz = ZoneInfo(db.timezone_negocio(conn))
        reader = csv.DictReader(fonte)
        faltando = [c for c in COLUNAS if c not in (reader.fieldnames or [])]
        if faltando:
            sys.exit(f"ERRO: CSV sem colunas obrigatórias: {faltando}. Esperado: {COLUNAS}")

        buffer: list[LinhaStaging] = []
        for n, row in enumerate(reader, start=2):  # linha 1 é o cabeçalho
            try:
                buffer.append(_linha(row, truncar=not args.sem_truncar))
            except (KeyError, ValueError) as exc:
                sys.exit(f"ERRO na linha {n} do CSV: {exc}")
            if len(buffer) >= args.batch:
                total += _flush(conn, buffer, dias, tz)
                buffer = []
                log.info("lote carregado", extra={"linhas_gravadas": total})

        total += _flush(conn, buffer, dias, tz)
        log.info("carga concluída",
                 extra={"linhas_gravadas": total, "dia": f"{len(dias)} dias"})

        if not args.sem_stats and dias:
            for d in sorted(dias):
                db.atualizar_stats_alvo(conn, d)
            conn.commit()
            db.atualizar_perfil_mediano(conn)
            conn.commit()
            log.info("stats e perfil recalculados", extra={"dia": f"{len(dias)} dias"})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if args.file:
            fonte.close()


if __name__ == "__main__":
    main()
