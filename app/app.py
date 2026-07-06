"""
Gerador de telemetria correlacionada (logs + traces + métricas) para a stack LGTM.

Simula uma pequena malha de microsserviços (e-commerce + um worker de batch).
Cada serviço:
  * tem seu próprio `service.name`, `service.namespace` e `workload` (resource);
  * emite TRACES (spans encadeados entre serviços no mesmo trace);
  * emite LOGS já carregando trace_id/span_id do span ativo (correlação log->trace);
  * emite MÉTRICAS (counter / histogram / updowncounter / gauge observável) com
    EXEMPLARS, que carregam o trace_id do span ativo (correlação métrica->trace).

Tudo é enviado via OTLP/gRPC para o OpenTelemetry Collector, que distribui para
Tempo (traces), Loki (logs) e Mimir (métricas).
"""

import logging
import os
import random
import threading
import time
import uuid

from opentelemetry.metrics import Observation
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
ENVIRONMENT = os.getenv("DEPLOYMENT_ENVIRONMENT", "dev")

HTTP_METHODS = ["GET", "POST", "PUT"]

# ---------------------------------------------------------------------------
# Modelo de cluster Kubernetes (atributos de recurso no padrão OpenTelemetry).
# ---------------------------------------------------------------------------
CLUSTER_NAME = os.getenv("K8S_CLUSTER_NAME", "lgtm-demo-cluster")
NODES = [
    "ip-10-0-1-12.ec2.internal",
    "ip-10-0-2-34.ec2.internal",
    "ip-10-0-3-56.ec2.internal",
]


def _hexhash(n):
    return "".join(random.choices("abcdef0123456789", k=n))


def _pod_identity(deployment):
    """Gera nomes de ReplicaSet/Pod no formato do Kubernetes."""
    rs_hash = _hexhash(10)
    suffix = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=5))
    return {
        "replicaset": f"{deployment}-{rs_hash}",
        "pod_name": f"{deployment}-{rs_hash}-{suffix}",
        "pod_uid": str(uuid.uuid4()),
    }


def k8s_resource(
    service_name,
    *,
    namespace,
    workload,
    k8s_namespace=None,
    node=None,
    deployment=None,
    container=None,
    image=None,
    version="1.0.0",
    extra=None,
):
    """Cria um Resource com atributos de serviço + atributos k8s (semconv OTel)."""
    node = node or random.choice(NODES)
    deployment = deployment or service_name
    container = container or service_name
    k8s_ns = k8s_namespace or namespace
    image = image or f"registry.local/{service_name}"
    pid = _pod_identity(deployment)

    attrs = {
        # ---- service / deployment ----
        "service.name": service_name,
        "service.namespace": namespace,
        "service.version": version,
        "service.instance.id": pid["pod_uid"],
        "workload": workload,
        "deployment.environment": ENVIRONMENT,
        # ---- Kubernetes (OpenTelemetry semantic conventions) ----
        "k8s.cluster.name": CLUSTER_NAME,
        "k8s.namespace.name": k8s_ns,
        "k8s.node.name": node,
        "k8s.pod.name": pid["pod_name"],
        "k8s.pod.uid": pid["pod_uid"],
        "k8s.deployment.name": deployment,
        "k8s.replicaset.name": pid["replicaset"],
        "k8s.container.name": container,
        # ---- container / host ----
        "container.name": container,
        "container.image.name": image,
        "container.image.tag": version,
        "host.name": node,
        "os.type": "linux",
    }
    if extra:
        attrs.update(extra)
    return Resource.create(attrs)


class AppError(Exception):
    """Erro de aplicação com tipo e atributos no padrão de semântica do OpenTelemetry."""

    def __init__(self, message, error_kind, attrs=None):
        super().__init__(message)
        self.error_kind = error_kind  # nome da exceção/categoria
        self.attrs = attrs or {}      # atributos OTel (error.type, db.system, ...)


# Catálogo de erros simulados (conexão, banco, rede, gateway, regra de negócio...).
ERROR_SCENARIOS = {
    "db_connection": lambda: AppError(
        "could not connect to database 'orders' at postgres:5432: connection refused",
        "DatabaseConnectionError",
        {"error.type": "db_connection_refused", "db.system": "postgresql",
         "server.address": "postgres", "server.port": 5432},
    ),
    "db_timeout": lambda: AppError(
        "canceling statement due to statement timeout after 5000ms: SELECT * FROM orders",
        "DatabaseTimeoutError",
        {"error.type": "db_query_timeout", "db.system": "postgresql",
         "db.statement": "SELECT * FROM orders WHERE status = $1", "timeout.ms": 5000},
    ),
    "db_deadlock": lambda: AppError(
        "deadlock detected on relation 'inventory' (process 4821 waits for ShareLock)",
        "DatabaseDeadlockError",
        {"error.type": "db_deadlock", "db.system": "postgresql"},
    ),
    "cache_unavailable": lambda: AppError(
        "redis: connection reset by peer (redis:6379)",
        "CacheConnectionError",
        {"error.type": "cache_connection_reset", "db.system": "redis",
         "server.address": "redis", "server.port": 6379},
    ),
    "gateway_timeout": lambda: AppError(
        "payment gateway did not respond within 3000ms (HTTP 504)",
        "UpstreamTimeoutError",
        {"error.type": "upstream_timeout", "http.response.status_code": 504,
         "peer.service": "payment-gateway"},
    ),
    "gateway_5xx": lambda: AppError(
        "upstream returned HTTP 502 Bad Gateway from payment-gateway",
        "UpstreamServiceError",
        {"error.type": "upstream_bad_gateway", "http.response.status_code": 502,
         "peer.service": "payment-gateway"},
    ),
    "network_reset": lambda: AppError(
        "connection reset by peer while reading response (ECONNRESET)",
        "NetworkError",
        {"error.type": "connection_reset", "net.peer.error": "ECONNRESET"},
    ),
    "service_unavailable": lambda: AppError(
        "dependency unavailable: returned HTTP 503 Service Unavailable",
        "ServiceUnavailableError",
        {"error.type": "service_unavailable", "http.response.status_code": 503},
    ),
    "out_of_stock": lambda: AppError(
        f"insufficient stock for SKU-{random.randint(1000, 9999)}: requested 3, available 0",
        "BusinessRuleError",
        {"error.type": "out_of_stock"},
    ),
    "auth_expired": lambda: AppError(
        "authentication token expired (JWT 'exp' in the past)",
        "AuthError",
        {"error.type": "token_expired", "http.response.status_code": 401},
    ),
    "validation": lambda: AppError(
        "invalid request payload: field 'amount' must be greater than 0",
        "ValidationError",
        {"error.type": "validation_failed", "http.response.status_code": 422},
    ),
    "oom": lambda: AppError(
        "worker terminated: out of memory (rss=512MiB, limit=512MiB)",
        "ResourceExhaustedError",
        {"error.type": "out_of_memory"},
    ),
}

# Cenários típicos por serviço, para erros mais realistas.
SERVICE_ERRORS = {
    "frontend-service": ["gateway_timeout", "service_unavailable", "network_reset", "auth_expired"],
    "checkout-service": ["service_unavailable", "db_timeout", "validation", "gateway_5xx"],
    "payment-service": ["gateway_timeout", "gateway_5xx", "auth_expired", "network_reset"],
    "inventory-service": ["db_connection", "db_deadlock", "out_of_stock", "cache_unavailable"],
    "notification-service": ["gateway_timeout", "network_reset", "service_unavailable"],
    "batch-worker": ["db_connection", "db_timeout", "db_deadlock", "oom"],
}


def make_error(service_name):
    keys = SERVICE_ERRORS.get(service_name, list(ERROR_SCENARIOS))
    return ERROR_SCENARIOS[random.choice(keys)]()


def record_error(span, logger, exc, context_attrs):
    """Marca o span como erro e emite log de erro detalhado (com trace_id automático)."""
    error_kind = getattr(exc, "error_kind", exc.__class__.__name__)
    attrs = getattr(exc, "attrs", {})
    error_type = attrs.get("error.type", error_kind)

    span.set_status(Status(StatusCode.ERROR, str(exc)))
    span.set_attribute("error.type", error_type)
    for k, v in attrs.items():
        span.set_attribute(k, v)
    span.record_exception(exc)

    # extra -> atributos do LogRecord -> structured metadata no Loki (consultável).
    log_attrs = {"error.type": error_type, "error.kind": error_kind, **context_attrs, **attrs}
    logger.error("[%s] %s", error_kind, exc, extra=log_attrs)


class Service:
    """Encapsula providers (trace/metric/log) e instrumentos de um serviço."""

    def __init__(self, name, namespace, workload, routes, error_rate=0.03,
                 node=None, deployment=None, image=None, k8s_namespace=None,
                 version="1.0.0", emit_queue=True):
        self.name = name
        self.workload = workload
        self.routes = routes
        self.error_rate = error_rate
        self.downstreams = []

        resource = k8s_resource(
            name,
            namespace=namespace,
            workload=workload,
            k8s_namespace=k8s_namespace,
            node=node,
            deployment=deployment,
            image=image,
            version=version,
        )
        # Guarda a identidade k8s para uso em spans/logs.
        self.node = resource.attributes.get("k8s.node.name")
        self.pod = resource.attributes.get("k8s.pod.name")
        self.k8s_namespace = resource.attributes.get("k8s.namespace.name")

        # ---- Traces ----
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT, insecure=True))
        )
        self.tracer = tracer_provider.get_tracer(name)

        # ---- Métricas (export a cada 5s) ----
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=ENDPOINT, insecure=True),
            export_interval_millis=5000,
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = meter_provider.get_meter(name)
        self.meter = meter  # disponível para subclasses (JVM, node-exporter, ...)

        self.requests = meter.create_counter(
            "http.server.requests",
            unit="1",
            description="Total de requisições HTTP atendidas",
        )
        self.duration = meter.create_histogram(
            "http.server.duration",
            unit="s",
            description="Duração das requisições HTTP",
        )
        self.inflight = meter.create_up_down_counter(
            "http.server.active_requests",
            unit="1",
            description="Requisições em andamento",
        )
        # Gauge observável: profundidade de fila simulada por serviço (serviços de
        # aplicação; infra como node-exporter desliga com emit_queue=False).
        self._queue_depth = 0
        if emit_queue:
            meter.create_observable_gauge(
                "service.queue.depth",
                callbacks=[self._observe_queue_depth],
                unit="1",
                description="Profundidade da fila interna do serviço",
            )

        # ---- Logs (bridge logging do Python -> OTLP) ----
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=ENDPOINT, insecure=True))
        )
        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(handler)

    def _observe_queue_depth(self, options):
        self._queue_depth = max(0, self._queue_depth + random.randint(-2, 3))
        return [Observation(self._queue_depth, {"workload": self.workload})]

    def handle(self, operation):
        """Processa uma 'requisição': abre span, chama downstreams e registra métricas."""
        method = random.choice(HTTP_METHODS)
        route = random.choice(self.routes)
        span_name = f"{method} {route}"

        with self.tracer.start_as_current_span(span_name, kind=SpanKind.SERVER) as span:
            span.set_attribute("http.request.method", method)
            span.set_attribute("http.route", route)
            span.set_attribute("workload", self.workload)
            span.set_attribute("operation", operation)

            base_attrs = {"http.request.method": method, "http.route": route}
            self.inflight.add(1, base_attrs)
            start = time.monotonic()
            status = 200

            try:
                # latência simulada de trabalho local
                time.sleep(random.uniform(0.005, 0.06))

                # chama serviços a jusante (mesmo trace, spans filhos)
                for dep in self.downstreams:
                    dep.handle(operation)

                if random.random() < self.error_rate:
                    raise make_error(self.name)

            except Exception as exc:  # noqa: BLE001
                status = 500
                record_error(
                    span, self.logger, exc,
                    {"http.route": route, "http.request.method": method, "operation": operation},
                )
            finally:
                self.inflight.add(-1, base_attrs)

            duration = time.monotonic() - start
            metric_attrs = dict(base_attrs, **{"http.response.status_code": status})

            # Métricas registradas DENTRO do span -> exemplars com trace_id.
            self.requests.add(1, metric_attrs)
            self.duration.record(duration, metric_attrs)

            if status == 200:
                self.logger.info(
                    "%s respondeu %s em %.1fms (op=%s)",
                    self.name, span_name, duration * 1000, operation,
                )
            return status


class BatchWorker(Service):
    """Worker de batch: jobs periódicos com root spans próprios e métricas de job."""

    def __init__(self):
        super().__init__(
            name="batch-worker",
            namespace="ops",
            workload="batch",
            routes=["/job/reconcile", "/job/export", "/job/cleanup"],
            error_rate=0.08,
        )

    def run_forever(self):
        while True:
            job = random.choice(self.routes)
            with self.tracer.start_as_current_span(f"batch {job}", kind=SpanKind.INTERNAL) as span:
                span.set_attribute("workload", self.workload)
                span.set_attribute("job.name", job)
                items = random.randint(50, 500)
                span.set_attribute("job.items", items)
                start = time.monotonic()
                try:
                    time.sleep(random.uniform(0.2, 1.2))
                    if random.random() < self.error_rate:
                        raise make_error(self.name)
                    self.requests.add(items, {"http.route": job, "http.response.status_code": 200})
                    self.duration.record(time.monotonic() - start, {"http.route": job})
                    self.logger.info("job %s concluído: %d itens", job, items)
                except Exception as exc:  # noqa: BLE001
                    self.requests.add(items, {"http.route": job, "http.response.status_code": 500})
                    record_error(span, self.logger, exc, {"job.name": job, "job.items": items})
            time.sleep(random.uniform(1.0, 3.0))


class JavaPlatformService(Service):
    """Serviço de plataforma Java (Spring-like) com métricas estilo JVM/Micrometer."""

    def __init__(self):
        super().__init__(
            name="opbk-srv-plataforma-test-java",
            namespace="plataforma",
            workload="backend-java",
            routes=["/api/v1/process", "/api/v1/status", "/actuator/health"],
            error_rate=0.05,
            deployment="opbk-srv-plataforma-test-java",
            image="registry.local/openbank/plataforma-test-java:1.4.2",
            version="1.4.2",
        )
        # Métricas no padrão JVM (viram jvm_memory_used_bytes, jvm_threads_count...).
        self.meter.create_observable_gauge(
            "jvm.memory.used", callbacks=[self._jvm_memory], unit="By",
            description="Memória JVM utilizada por área",
        )
        self.meter.create_observable_gauge(
            "jvm.threads.count", callbacks=[self._jvm_threads],
            description="Threads ativas na JVM",
        )
        self.meter.create_observable_gauge(
            "process.runtime.jvm.cpu.utilization", callbacks=[self._jvm_cpu],
            unit="1", description="Utilização de CPU do processo JVM",
        )
        self.gc_count = self.meter.create_counter(
            "jvm.gc.collections", unit="1", description="Coletas de lixo da JVM",
        )

    def _jvm_memory(self, options):
        return [
            Observation(random.randint(200, 480) * 1024 * 1024, {"jvm.memory.type": "heap", "jvm.memory.pool.name": "G1 Eden Space"}),
            Observation(random.randint(60, 120) * 1024 * 1024, {"jvm.memory.type": "non_heap", "jvm.memory.pool.name": "Metaspace"}),
        ]

    def _jvm_threads(self, options):
        return [Observation(random.randint(24, 96), {"jvm.thread.state": "runnable"})]

    def _jvm_cpu(self, options):
        return [Observation(round(random.uniform(0.05, 0.85), 3), {})]

    def handle(self, operation):
        # Conta GC esporádico durante o processamento (gera jvm_gc_collections_total).
        if random.random() < 0.3:
            self.gc_count.add(1, {"jvm.gc.name": random.choice(["G1 Young Generation", "G1 Old Generation"])})
        return super().handle(operation)


class NodeExporter(Service):
    """node-exporter como DaemonSet: 1 pod por nó, expondo métricas node_* do host."""

    def __init__(self, node):
        super().__init__(
            name="node-exporter",
            namespace="monitoring",
            workload="daemonset",
            routes=["/metrics"],
            error_rate=0.0,
            node=node,
            deployment="node-exporter",
            image="quay.io/prometheus/node-exporter:v1.8.2",
            version="1.8.2",
            k8s_namespace="monitoring",
            emit_queue=False,  # infra: sem fila de aplicação
        )
        # Métricas no padrão node_exporter.
        self.cpu_seconds = self.meter.create_counter(
            "node.cpu.seconds", unit="s", description="Tempo de CPU por modo",
        )
        self.net_rx = self.meter.create_counter(
            "node.network.receive", unit="By", description="Bytes recebidos por interface",
        )
        self.meter.create_observable_gauge(
            "node.load1", callbacks=[self._load1], description="Load average de 1 minuto",
        )
        self.meter.create_observable_gauge(
            "node.memory.available", callbacks=[self._mem_avail], unit="By",
            description="Memória disponível",
        )
        self.meter.create_observable_gauge(
            "node.filesystem.avail", callbacks=[self._fs_avail], unit="By",
            description="Espaço livre em disco",
        )
        self._cur_load = 0.5

    def _load1(self, options):
        self._cur_load = max(0.05, min(8.0, self._cur_load + random.uniform(-0.4, 0.5)))
        return [Observation(round(self._cur_load, 2), {})]

    def _mem_avail(self, options):
        return [Observation(random.randint(2, 12) * 1024 ** 3, {})]

    def _fs_avail(self, options):
        return [Observation(random.randint(10, 90) * 1024 ** 3, {"mountpoint": "/", "fstype": "ext4"})]

    def run_forever(self):
        while True:
            with self.tracer.start_as_current_span("scrape /metrics", kind=SpanKind.INTERNAL) as span:
                span.set_attribute("k8s.node.name", self.node)
                span.set_attribute("scrape.target", "node-exporter:9100")
                for mode in ("user", "system", "idle", "iowait"):
                    self.cpu_seconds.add(round(random.uniform(0.1, 2.0), 3), {"cpu": "0", "mode": mode})
                self.net_rx.add(random.randint(1000, 500_000), {"device": "eth0"})
                time.sleep(random.uniform(0.01, 0.05))
            if self._cur_load > 4.0:
                self.logger.warning("high load on node %s: load1=%.2f", self.node, self._cur_load)
            else:
                self.logger.info("scrape completed on node %s (load1=%.2f)", self.node, self._cur_load)
            time.sleep(random.uniform(5.0, 10.0))


# Séries de negócio (app, jornada, escopo, status permitidos) — reproduzem
# exatamente as métricas solicitadas.
BRADESCO_SERIES = [
    ("mobilepf", "extrato", "consulta", ["sucesso", "falha"]),
    ("mobilepf", "saldo", "consulta", ["sucesso", "falha"]),
    ("mobilepf", "login", "obter-token", ["sucesso", "falha"]),
    ("mobilepf", "login", "validar-token", ["sucesso", "falha"]),
    ("mobilepf", "login", "confirma-autenticacao", ["sucesso", "falha"]),
    ("pix-mobilepf", "pagamento", "efetivacao", ["sucesso", "falha"]),
    ("pix-mobilepf", "adesao", "adesao", ["falha"]),
]


class BradescoMobileMetrics:
    """Emite counters de negócio com temporalidade DELTA (padrão OTel delta).

    O collector converte delta -> cumulative (processor deltatocumulative) antes
    de escrever no Mimir, que é cumulativo (Prometheus).
    """

    def __init__(self):
        resource = k8s_resource(
            "bradesco-mobile-apps",
            namespace="bradesco",
            workload="mobile-backend",
            deployment="bradesco-mobile-apps",
            image="registry.local/bradesco/mobile-apps:3.1.0",
            version="3.1.0",
        )

        # Temporalidade DELTA para todos os Sum/Counter deste provider.
        delta_temporality = {
            Counter: AggregationTemporality.DELTA,
            UpDownCounter: AggregationTemporality.DELTA,
            Histogram: AggregationTemporality.DELTA,
            ObservableCounter: AggregationTemporality.DELTA,
            ObservableUpDownCounter: AggregationTemporality.DELTA,
            ObservableGauge: AggregationTemporality.CUMULATIVE,
        }
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(
                endpoint=ENDPOINT,
                insecure=True,
                preferred_temporality=delta_temporality,
            ),
            export_interval_millis=5000,
        )
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = provider.get_meter("bradesco-mobile-apps")

        # Um counter por app; o sufixo _total é adicionado pelo collector
        # (add_metric_suffixes) -> bradesco_app_mobilepf_total / _pix_mobilepf_total.
        self.counters = {
            "mobilepf": meter.create_counter(
                "bradesco.app.mobilepf", unit="1",
                description="Total de operações do app mobilepf (delta)",
            ),
            "pix-mobilepf": meter.create_counter(
                "bradesco.app.pix-mobilepf", unit="1",
                description="Total de operações do app pix-mobilepf (delta)",
            ),
        }

    def run_forever(self):
        while True:
            for app, jornada, escopo, statuses in BRADESCO_SERIES:
                counter = self.counters[app]
                for status in statuses:
                    # sucesso mais frequente; falha esporádica
                    if status == "sucesso":
                        inc = random.randint(3, 40)
                    else:
                        inc = random.randint(0, 5)
                    if inc:
                        counter.add(inc, {
                            "app": app,
                            "jornada": jornada,
                            "escopo": escopo,
                            "status": status,
                        })
            time.sleep(random.uniform(2.0, 5.0))


def build_topology():
    """Monta as malhas de serviços e retorna os pontos de entrada (entrypoints)."""
    # ---- malha "shop" (e-commerce) ----
    frontend = Service("frontend-service", "shop", "web", ["/", "/cart", "/product"], 0.02)
    checkout = Service("checkout-service", "shop", "api", ["/checkout", "/order"], 0.04)
    payment = Service("payment-service", "shop", "api", ["/charge", "/refund"], 0.06)
    inventory = Service("inventory-service", "shop", "api", ["/reserve", "/stock"], 0.03)
    notification = Service("notification-service", "shop", "worker", ["/email", "/sms"], 0.05)

    frontend.downstreams = [checkout]
    checkout.downstreams = [payment, inventory, notification]

    # ---- malha "plataforma" (Java) + serviço de carrinho/histórico ----
    java_platform = JavaPlatformService()
    cart = Service(
        "encc-srv-gst-hst-cart-cli", "ecommerce", "backend",
        ["/cart/history", "/cart/items", "/cart/checkout"], 0.04,
        deployment="encc-srv-gst-hst-cart-cli",
        image="registry.local/encerramento/gst-hst-cart-cli:2.0.1",
        version="2.0.1",
    )
    java_platform.downstreams = [cart]

    return [frontend, java_platform]


def traffic_loop(entrypoint, ops):
    while True:
        entrypoint.handle(random.choice(ops))
        time.sleep(random.uniform(0.05, 0.4))


def main():
    logging.basicConfig(level=logging.WARNING)  # silencia ruído de libs
    print(f"[telemetry-generator] enviando OTLP para {ENDPOINT}", flush=True)

    entrypoints = build_topology()
    operations = ["browse", "purchase", "search", "wishlist"]

    threads = []
    # tráfego concorrente em cada entrypoint (shop e plataforma Java)
    for entry in entrypoints:
        for _ in range(3):
            t = threading.Thread(target=traffic_loop, args=(entry, operations), daemon=True)
            t.start()
            threads.append(t)

    # worker de batch independente (outro workload / namespace)
    worker = BatchWorker()
    tw = threading.Thread(target=worker.run_forever, daemon=True)
    tw.start()
    threads.append(tw)

    # node-exporter como DaemonSet: um pod por nó do cluster
    for node in NODES:
        ne = NodeExporter(node=node)
        tn = threading.Thread(target=ne.run_forever, daemon=True)
        tn.start()
        threads.append(tn)

    # métricas de negócio Bradesco (counters com temporalidade DELTA)
    bradesco = BradescoMobileMetrics()
    tb = threading.Thread(target=bradesco.run_forever, daemon=True)
    tb.start()
    threads.append(tb)

    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
