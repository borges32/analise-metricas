from datetime import datetime, timezone

import pytest

from loader import db, pipeline
from loader.config_metricas import MetricaConfig

UTC = timezone.utc

SERIES = [{
    "metric": {"app": "mobilepf", "jornada": "extrato", "escopo": "consulta", "status": "sucesso"},
    "values": [[1_700_000_040, "10"], [1_700_000_100, "20"]],
}]


class FakeConn:
    def __init__(self):
        self.eventos: list[str] = []

    def commit(self):
        self.eventos.append("commit")

    def rollback(self):
        self.eventos.append("rollback")


class FakeMimir:
    def __init__(self, series):
        self.series = series
        self.chamadas = 0

    def coletar_series(self, query, inicio, fim, step=60):
        self.chamadas += 1
        return self.series


def _metrica():
    return MetricaConfig(
        nome="bradesco_app_mobilepf_total", produto="mobilepf",
        labels={"app": "app", "jornada": "jornada", "escopo": "escopo", "status": "status"},
    )


@pytest.fixture
def mock_db(monkeypatch):
    chamadas: dict[str, list] = {"copy": [], "processar": 0, "watermark": []}

    def copiar(conn, linhas):
        linhas = list(linhas)
        chamadas["copy"].append(linhas)
        conn.eventos.append("copy")
        return len(linhas)

    def processar(conn):
        chamadas["processar"] += 1
        conn.eventos.append("processar")

    def gravar_wm(conn, ts):
        chamadas["watermark"].append(ts)
        conn.eventos.append("watermark")

    monkeypatch.setattr(db, "copiar_staging", copiar)
    monkeypatch.setattr(db, "processar_staging", processar)
    monkeypatch.setattr(db, "gravar_watermark", gravar_wm)
    return chamadas


def test_ordem_atomica_e_watermark(mock_db):
    conn = FakeConn()
    mimir = FakeMimir(SERIES)
    ini = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    fim = datetime(2026, 7, 4, 12, 30, tzinfo=UTC)

    res = pipeline.carregar_janela(conn, mimir, [_metrica()], ini, fim, avancar_watermark=True)

    # COPY -> processar -> watermark -> commit, nesta ordem (atomicidade)
    assert conn.eventos == ["copy", "processar", "watermark", "commit"]
    assert mock_db["watermark"] == [fim]  # watermark = fim da janela
    assert res.linhas_gravadas == 2


def test_watermark_nao_avanca_no_backfill(mock_db):
    conn = FakeConn()
    mimir = FakeMimir(SERIES)
    ini = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    fim = datetime(2026, 7, 4, 12, 30, tzinfo=UTC)

    pipeline.carregar_janela(conn, mimir, [_metrica()], ini, fim, avancar_watermark=False)

    assert "watermark" not in conn.eventos
    assert mock_db["watermark"] == []
    assert conn.eventos == ["copy", "processar", "commit"]


def test_idempotencia_mesma_janela_gera_mesmas_linhas(mock_db):
    conn = FakeConn()
    mimir = FakeMimir(SERIES)
    ini = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    fim = datetime(2026, 7, 4, 12, 30, tzinfo=UTC)

    pipeline.carregar_janela(conn, mimir, [_metrica()], ini, fim, avancar_watermark=True)
    pipeline.carregar_janela(conn, mimir, [_metrica()], ini, fim, avancar_watermark=True)

    # Duas rodadas da mesma janela -> staging idêntico (dedup fica no ON CONFLICT do banco)
    assert mock_db["copy"][0] == mock_db["copy"][1]
    assert mock_db["processar"] == 2


def test_rollback_em_falha_nao_commita_watermark(mock_db, monkeypatch):
    conn = FakeConn()
    mimir = FakeMimir(SERIES)

    def processar_falha(conn):
        conn.eventos.append("processar")
        raise RuntimeError("erro no processar_staging")

    monkeypatch.setattr(db, "processar_staging", processar_falha)

    with pytest.raises(RuntimeError, match="processar_staging"):
        pipeline.carregar_janela(
            conn, mimir, [_metrica()],
            datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
            datetime(2026, 7, 4, 12, 30, tzinfo=UTC),
            avancar_watermark=True,
        )

    assert "rollback" in conn.eventos
    assert "commit" not in conn.eventos
    assert mock_db["watermark"] == []  # watermark NÃO avançou
