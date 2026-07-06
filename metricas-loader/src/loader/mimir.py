"""Cliente do Mimir (API Prometheus query_range) com retry, bisseção e parsing."""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import NamedTuple

import httpx

from .config_metricas import COLUNAS_LABEL, LABEL_AUSENTE, MetricaConfig

log = logging.getLogger("loader.mimir")

# Palavras-chave que indicam resposta truncada / estouro de limite no Mimir.
_LIMIT_KEYWORDS = ("limit", "too many", "maximum", "exceeded", "max number")

# Janela mínima ao bisseccionar (segundos) — 5 minutos.
JANELA_MINIMA_SEG = 300


class LinhaStaging(NamedTuple):
    """Linha da `metricas.staging_metrica` (ordem = ordem do COPY)."""

    ts: datetime
    produto: str
    app: str
    jornada: str
    escopo: str
    status: str
    valor: int


class MimirError(RuntimeError):
    """Erro genérico de consulta ao Mimir."""


class MimirLimitError(MimirError):
    """Resposta truncada ou estouro de limite — dispara bisseção da janela."""


def truncar_minuto(epoch: float) -> datetime:
    """Trunca um epoch (segundos) para o início do minuto, em UTC."""
    minuto = int(epoch) - (int(epoch) % 60)
    return datetime.fromtimestamp(minuto, tz=timezone.utc)


def matrix_para_linhas(
    series: list[dict], metrica: MetricaConfig
) -> list[LinhaStaging]:
    """Converte o `data.result` (matrix) em linhas da staging.

    - produto: valor fixo do config (não vem de label).
    - labels ausentes -> 'desconhecido' (colunas NOT NULL).
    - descarta amostras NaN; arredonda float -> int; trunca ts para o minuto.
    """
    linhas: list[LinhaStaging] = []
    for serie in series:
        labels = serie.get("metric", {})
        dims = {
            col: labels.get(metrica.labels[col], LABEL_AUSENTE) or LABEL_AUSENTE
            for col in COLUNAS_LABEL
        }
        for amostra in serie.get("values", []):
            epoch, bruto = amostra[0], amostra[1]
            try:
                valor_float = float(bruto)
            except (TypeError, ValueError):
                continue
            if math.isnan(valor_float):
                continue
            linhas.append(
                LinhaStaging(
                    ts=truncar_minuto(float(epoch)),
                    produto=metrica.produto,
                    app=dims["app"],
                    jornada=dims["jornada"],
                    escopo=dims["escopo"],
                    status=dims["status"],
                    valor=round(valor_float),
                )
            )
    return linhas


class MimirClient:
    """Cliente HTTP para `query_range`, com autenticação, retry e bisseção."""

    def __init__(
        self,
        base_url: str,
        *,
        tenant: str | None = None,
        bearer_token: str | None = None,
        basic_auth: tuple[str, str] | None = None,
        timeout: float = 60.0,
        tentativas: int = 3,
    ) -> None:
        headers: dict[str, str] = {"Accept": "application/json"}
        if tenant:
            headers["X-Scope-OrgID"] = tenant
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        auth = httpx.BasicAuth(*basic_auth) if basic_auth else None
        # Guarda a base completa (pode conter prefixo, ex.: .../prometheus) e concatena
        # o path manualmente — usar base_url do httpx com path + "/rota" descartaria o prefixo.
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(headers=headers, auth=auth, timeout=timeout)
        self._tentativas = tentativas

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MimirClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def query_range(
        self, query: str, start: datetime, end: datetime, step: int = 60
    ) -> list[dict]:
        """Executa query_range e devolve `data.result` (matrix). Retry em 5xx/timeout."""
        params = {
            "query": query,
            "start": str(int(start.timestamp())),
            "end": str(int(end.timestamp())),
            "step": str(step),
        }
        ultimo_erro: Exception | None = None
        for tentativa in range(1, self._tentativas + 1):
            try:
                resp = self._client.post(f"{self._base}/api/v1/query_range", data=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                ultimo_erro = exc
                self._backoff(tentativa, str(exc))
                continue

            if resp.status_code >= 500:
                ultimo_erro = MimirError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                self._backoff(tentativa, f"HTTP {resp.status_code}")
                continue

            corpo_baixo = resp.text.lower()
            if resp.status_code >= 400:
                if any(k in corpo_baixo for k in _LIMIT_KEYWORDS):
                    raise MimirLimitError(resp.text[:300])
                raise MimirError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            payload = resp.json()
            if payload.get("status") != "success":
                if any(k in corpo_baixo for k in _LIMIT_KEYWORDS):
                    raise MimirLimitError(str(payload)[:300])
                raise MimirError(f"status != success: {str(payload)[:300]}")

            data = payload.get("data", {})
            if data.get("resultType") != "matrix":
                raise MimirError(f"resultType inesperado: {data.get('resultType')}")

            # Aviso de truncamento explícito
            if any(k in corpo_baixo for k in _LIMIT_KEYWORDS) and payload.get("warnings"):
                raise MimirLimitError(str(payload.get("warnings"))[:300])

            return data.get("result", [])

        raise MimirError(f"query_range falhou após {self._tentativas} tentativas: {ultimo_erro}")

    def coletar_series(
        self, query: str, start: datetime, end: datetime, step: int = 60
    ) -> list[dict]:
        """query_range com bisseção automática da janela em caso de limite/truncamento."""
        try:
            return self.query_range(query, start, end, step)
        except MimirLimitError:
            dur = int(end.timestamp() - start.timestamp())
            if dur <= JANELA_MINIMA_SEG:
                raise
            meio_epoch = int(start.timestamp()) + (dur // 2 // 60) * 60
            meio = datetime.fromtimestamp(meio_epoch, tz=timezone.utc)
            log.warning(
                "bisseção acionada",
                extra={"janela_inicio": start.isoformat(), "janela_fim": end.isoformat()},
            )
            esquerda = self.coletar_series(query, start, meio, step)
            direita = self.coletar_series(query, meio, end, step)
            return esquerda + direita

    def _backoff(self, tentativa: int, motivo: str) -> None:
        espera = min(2 ** (tentativa - 1), 8)
        log.warning(
            "retry mimir",
            extra={"erro": motivo, "duracao_ms": espera * 1000},
        )
        time.sleep(espera)


def criar_mimir_client(settings) -> MimirClient:  # noqa: ANN001 (evita import circular)
    """Constrói o MimirClient a partir das settings."""
    basic = None
    if settings.mimir_basic_auth_user:
        basic = (settings.mimir_basic_auth_user, settings.mimir_basic_auth_pass or "")
    return MimirClient(
        settings.mimir_base,
        tenant=settings.mimir_tenant,
        bearer_token=settings.mimir_bearer_token,
        basic_auth=basic,
        timeout=settings.mimir_timeout_segundos,
    )
