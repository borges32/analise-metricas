from datetime import datetime, timezone

from loader.config_metricas import MetricaConfig
from loader.mimir import matrix_para_linhas, truncar_minuto


def _metrica(labels=None):
    return MetricaConfig(
        nome="bradesco_app_pix_total",
        produto="pix",
        labels=labels or {"app": "app", "jornada": "jornada", "escopo": "escopo", "status": "status"},
    )


def test_parse_matrix_basico():
    series = [{
        "metric": {"app": "mobilepf", "jornada": "extrato", "escopo": "consulta", "status": "sucesso"},
        "values": [[1_700_000_040, "12.4"], [1_700_000_100, "8.9"]],
    }]
    linhas = matrix_para_linhas(series, _metrica())
    assert len(linhas) == 2
    l0 = linhas[0]
    assert l0.produto == "pix"
    assert l0.app == "mobilepf" and l0.jornada == "extrato"
    assert l0.status == "sucesso"
    assert l0.valor == 12  # round(12.4)


def test_arredonda_e_trunca_para_minuto():
    series = [{
        "metric": {"app": "a", "jornada": "j", "escopo": "e", "status": "sucesso"},
        "values": [[1_700_000_045, "9.6"]],  # 45s dentro do minuto
    }]
    linhas = matrix_para_linhas(series, _metrica())
    assert linhas[0].valor == 10  # round(9.6)
    assert linhas[0].ts == datetime(2023, 11, 14, 22, 14, 0, tzinfo=timezone.utc)
    assert linhas[0].ts.second == 0


def test_descarta_nan():
    series = [{
        "metric": {"app": "a", "jornada": "j", "escopo": "e", "status": "s"},
        "values": [[1_700_000_040, "NaN"], [1_700_000_100, "5"]],
    }]
    linhas = matrix_para_linhas(series, _metrica())
    assert len(linhas) == 1
    assert linhas[0].valor == 5


def test_label_ausente_vira_desconhecido():
    series = [{
        "metric": {"app": "mobilepf", "jornada": "extrato"},  # sem escopo/status
        "values": [[1_700_000_040, "1"]],
    }]
    linhas = matrix_para_linhas(series, _metrica())
    assert linhas[0].escopo == "desconhecido"
    assert linhas[0].status == "desconhecido"


def test_nome_de_label_diferente_no_mimir():
    series = [{
        "metric": {"app": "a", "journey": "login", "escopo": "e", "status": "s"},
        "values": [[1_700_000_040, "3"]],
    }]
    m = _metrica({"app": "app", "jornada": "journey", "escopo": "escopo", "status": "status"})
    linhas = matrix_para_linhas(series, m)
    assert linhas[0].jornada == "login"


def test_truncar_minuto():
    assert truncar_minuto(1_700_000_059).second == 0
    assert truncar_minuto(1_700_000_059) == datetime(2023, 11, 14, 22, 14, 0, tzinfo=timezone.utc)
