from datetime import datetime, timezone

from loader.mimir import JANELA_MINIMA_SEG, MimirLimitError
from loader.pipeline import calcular_janela, dias_fechados, subjanelas

UTC = timezone.utc


def test_calcular_janela_normal():
    wm = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    agora = datetime(2026, 7, 4, 12, 8, tzinfo=UTC)  # 8 min à frente
    inicio, fim = calcular_janela(wm, agora, janela_minutos=30, atraso_minutos=3)
    # teto = agora - 3min = 12:05; menor que wm+30 -> fim = 12:05
    assert inicio == wm
    assert fim == datetime(2026, 7, 4, 12, 5, tzinfo=UTC)


def test_calcular_janela_limitada_pela_janela_maxima():
    wm = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    agora = datetime(2026, 7, 4, 14, 0, tzinfo=UTC)  # muito atrás (catch-up)
    inicio, fim = calcular_janela(wm, agora, janela_minutos=30, atraso_minutos=3)
    assert fim == datetime(2026, 7, 4, 12, 30, tzinfo=UTC)  # teto do JANELA_MINUTOS


def test_calcular_janela_vazia_quando_atraso_engole_tudo():
    wm = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    agora = datetime(2026, 7, 4, 12, 2, tzinfo=UTC)  # teto = 11:59 < wm
    inicio, fim = calcular_janela(wm, agora, janela_minutos=30, atraso_minutos=3)
    assert inicio == fim  # janela vazia


def test_subjanelas_divide_em_fatias():
    ini = datetime(2026, 7, 4, 0, 0, tzinfo=UTC)
    fim = datetime(2026, 7, 4, 1, 10, tzinfo=UTC)
    janelas = subjanelas(ini, fim, janela_minutos=30)
    assert len(janelas) == 3
    assert janelas[0] == (ini, datetime(2026, 7, 4, 0, 30, tzinfo=UTC))
    assert janelas[-1][1] == fim


def test_dias_fechados_uma_virada():
    # America/Sao_Paulo = UTC-3. Meia-noite local = 03:00 UTC.
    inicio = datetime(2026, 7, 4, 2, 30, tzinfo=UTC)  # 03/07 23:30 local
    fim = datetime(2026, 7, 4, 3, 30, tzinfo=UTC)     # 04/07 00:30 local
    fechados = dias_fechados(inicio, fim, "America/Sao_Paulo")
    assert [d.isoformat() for d in fechados] == ["2026-07-03"]


def test_dias_fechados_nenhuma_virada():
    inicio = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    fim = datetime(2026, 7, 4, 12, 30, tzinfo=UTC)
    assert dias_fechados(inicio, fim, "America/Sao_Paulo") == []


def test_dias_fechados_multiplos_dias_catchup():
    # Catch-up de 03/07 09:00 a 05/07 09:00 (local): fecham 03/07 e 04/07.
    inicio = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)  # 09:00 local 03/07
    fim = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)     # 09:00 local 05/07
    fechados = [d.isoformat() for d in dias_fechados(inicio, fim, "America/Sao_Paulo")]
    assert fechados == ["2026-07-03", "2026-07-04"]


def test_bissecao_para_no_minimo(monkeypatch):
    # coletar_series deve desistir (propagar) quando a janela <= mínimo.
    from loader import mimir as mod

    class FakeClient(mod.MimirClient):
        def __init__(self):  # não abre httpx
            self._tentativas = 1

        def query_range(self, query, start, end, step=60):
            raise MimirLimitError("limit exceeded")

    c = FakeClient()
    ini = datetime(2026, 7, 4, 0, 0, tzinfo=UTC)
    fim = datetime(2026, 7, 4, 0, JANELA_MINIMA_SEG // 60, tzinfo=UTC)  # == mínimo
    try:
        c.coletar_series("q", ini, fim)
        raise AssertionError("deveria ter propagado MimirLimitError")
    except MimirLimitError:
        pass
