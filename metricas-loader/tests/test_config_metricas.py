import json

import pytest

from loader.config_metricas import carregar_config_metricas


def _escrever(tmp_path, dados):
    p = tmp_path / "metricas.json"
    p.write_text(json.dumps(dados), encoding="utf-8")
    return p


def test_carrega_e_filtra_ativas(tmp_path):
    p = _escrever(tmp_path, {
        "metricas": [
            {"nome": "bradesco_app_pix_total", "produto": "pix",
             "labels": {"app": "app", "jornada": "jornada", "escopo": "escopo", "status": "status"},
             "ativa": True},
            {"nome": "bradesco_app_off_total", "produto": "off",
             "labels": {"app": "app", "jornada": "jornada", "escopo": "escopo", "status": "status"},
             "ativa": False},
        ]
    })
    cfg = carregar_config_metricas(p)
    assert len(cfg.metricas) == 2
    ativas = cfg.ativas()
    assert len(ativas) == 1
    assert ativas[0].nome == "bradesco_app_pix_total"


def test_promql_usa_nomes_de_label_do_mimir(tmp_path):
    p = _escrever(tmp_path, {
        "metricas": [
            {"nome": "bradesco_app_pix_total", "produto": "pix",
             "labels": {"app": "app", "jornada": "journey", "escopo": "escopo", "status": "status"}},
        ]
    })
    m = carregar_config_metricas(p).metricas[0]
    assert m.promql() == "sum by (app, journey, escopo, status) (increase(bradesco_app_pix_total[1m]))"


def test_label_obrigatoria_ausente_falha(tmp_path):
    p = _escrever(tmp_path, {
        "metricas": [
            {"nome": "x", "produto": "p", "labels": {"app": "app", "jornada": "jornada"}},
        ]
    })
    with pytest.raises(ValueError, match="obrigatórias"):
        carregar_config_metricas(p)


def test_arquivo_inexistente_falha(tmp_path):
    with pytest.raises(FileNotFoundError):
        carregar_config_metricas(tmp_path / "nao_existe.json")


def test_json_invalido_falha(tmp_path):
    p = tmp_path / "metricas.json"
    p.write_text("{ isto não é json", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON inválido"):
        carregar_config_metricas(p)
