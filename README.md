# Stack LGTM local (Loki · Grafana · Tempo · Mimir) + gerador de telemetria

Ambiente de desenvolvimento para testar tools de um MCP LGTM: dashboards/alertas
no Grafana, queries de métricas no Mimir, e queries de logs/traces no Loki/Tempo.
Uma aplicação gera **logs, traces e métricas correlacionados** e os envia via OTLP
para um **OpenTelemetry Collector**, que distribui para a stack.

## Componentes

| Serviço        | Imagem                                              | Porta(s)            |
|----------------|-----------------------------------------------------|---------------------|
| Grafana        | `grafana/grafana:13.0.0`                             | 3000                |
| Mimir          | `grafana/mimir:2.15.0`                              | 9009                |
| Loki           | `grafana/loki:3.5.0`                               | 3100                |
| Tempo          | `grafana/tempo:2.7.0`                              | 3200                |
| OTel Collector | `otel/opentelemetry-collector-contrib:0.148.0`     | 4317/4318, 8889     |
| App geradora   | build local (`./app`, Python + OpenTelemetry SDK)   | —                   |

## Subir

```bash
docker compose up -d --build
```

Grafana: <http://localhost:3000> (login anônimo como Admin já habilitado;
usuário/senha `admin`/`admin` se preferir).

Os datasources **Mimir**, **Loki** e **Tempo** já vêm provisionados com correlação.

## Fluxo de dados

```
                      ┌──────────────────────┐
 app (OTLP/gRPC) ───► │  OTel Collector       │
                      │  contrib 0.148.0      │
                      └──────────────────────┘
                         │        │        │
              traces ────┘  metrics│   logs └──── Loki (OTLP /otlp)
                ▼              ▼
              Tempo          Mimir (remote_write /api/v1/push)
                └─ metrics-generator ─► Mimir (span-metrics + service-graph)
```

## Correlação (a "história" que dá pra contar nas tools do MCP)

Os três sinais compartilham os mesmos atributos de recurso — **`service.name`**,
**`service.namespace`**, **`workload`** e **`deployment.environment`** — então dá
para pivotar entre eles:

- **Métrica → Trace**: métricas são gravadas dentro de spans ativos, gerando
  *exemplars* com `trace_id`. No Grafana, exemplars do Mimir abrem o trace no Tempo.
- **Log → Trace**: cada log carrega `trace_id`/`span_id` do span ativo (structured
  metadata no Loki). O campo derivado `trace_id` abre o trace no Tempo.
- **Trace → Logs**: configurado via *Trace to logs* (filtra por `trace_id` no Loki).
- **Trace → Métricas / Service Map**: o metrics-generator do Tempo escreve
  `traces_spanmetrics_*` e o service-graph no Mimir.

### Dimensões para queries

A app simula uma malha de microsserviços com **workloads e service names distintos**:

| service.name           | namespace | workload | papel                         |
|------------------------|-----------|----------|-------------------------------|
| `frontend-service`     | shop      | web      | ponto de entrada HTTP         |
| `checkout-service`     | shop      | api      | orquestra o pedido            |
| `payment-service`      | shop      | api      | cobrança/estorno              |
| `inventory-service`    | shop      | api      | reserva/estoque               |
| `notification-service` | shop      | worker   | e-mail/SMS                    |
| `batch-worker`         | ops       | batch    | jobs periódicos (root spans)  |

Métricas emitidas: `http_server_requests_total`, `http_server_duration_*`
(histograma), `http_server_active_requests` (up/down), `service_queue_depth` (gauge).
Todas com labels `service_name`, `workload`, `http_route`, `http_response_status_code`.

## Exemplos rápidos de query

**Mimir (PromQL)** — taxa de requisições por serviço/workload:
```promql
sum by (service_name, workload) (rate(http_server_requests_total[5m]))
```

**Loki (LogQL)** — erros de um serviço:
```logql
{service_name="payment-service"} |= "falha"
```

**Tempo (TraceQL)** — traces lentos do checkout:
```traceql
{ resource.service.name = "checkout-service" && duration > 200ms }
```

## Derrubar

```bash
docker compose down          # mantém volumes
docker compose down -v       # remove dados persistidos
```
