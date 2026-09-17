# Análise de Métricas OpenTelemetry (Mimir → Postgres → Grafana)

Plataforma de **análise de volumetria** de métricas OpenTelemetry. As métricas
mapeadas ficam no Mimir; um worker as copia para um Postgres analítico, que
responde às perguntas de negócio ("quanto rodou hoje × ontem × no dia recorde?")
em milissegundos, e um plugin do Grafana apresenta isso em tela.

O repositório também traz uma **stack LGTM completa** (Loki · Grafana · Tempo ·
Mimir) com uma aplicação geradora de telemetria, para desenvolver tudo localmente.

## Os três projetos

| Projeto | O que é | Papel |
| --- | --- | --- |
| [`metricas-loader/`](metricas-loader/) | Worker Python | Coleta as métricas mapeadas do Mimir e carrega no Postgres. **Também cria toda a estrutura do banco.** |
| [`analitico-metrica-ddl/`](analitico-metrica-ddl/) | Documentação | Modelagem do banco analítico: tabelas, particionamento, procedures e o contrato consumido pela tela. |
| [`plugin-analitico/`](plugin-analitico/) | App plugin do Grafana (`lgtm-analitico-app`) | Apresenta os dados analíticos: gráfico dia × dia × recorde, perfil mediano e cards de resumo. |

Apoio: [`app/`](app/) (gerador de telemetria), [`config/`](config/) (configuração
da stack LGTM), [`k8s/`](k8s/) (manifestos do loader para OpenShift/ARO).

## Fluxo de dados

```text
 app (OTLP) ──► OTel Collector ──┬──► Tempo  (traces)  ──► metrics-generator ──┐
                                 ├──► Loki   (logs)                            │
                                 └──► Mimir  (métricas) ◄──────────────────────┘
                                          │
                                          │  query_range, janela de 30 min
                                          ▼
                                   metricas-loader  ──► aplica o DDL no boot
                                          │              (schema único)
                                          │  COPY binário → staging
                                          ▼
                                  Postgres (schema `metricas`)
                                   dim_serie · metrica_minuto
                                   metrica_minuto_app · stats_alvo
                                          │
                                          │  fn_grafico / fn_perfil
                                          ▼
                                Grafana + plugin-analitico
```

## Subir tudo

```bash
docker network create lgtm-shared   # só na primeira vez
docker compose up -d --build
```

| Serviço | Imagem | Porta |
| --- | --- | --- |
| Grafana (+ plugin analítico) | `grafana/grafana:latest` | 3000 |
| Mimir | `grafana/mimir:2.15.0` | 9009 |
| Loki | `grafana/loki:3.5.0` | 3100 |
| Tempo | `grafana/tempo:2.7.0` | 3200 |
| OTel Collector | `otel/opentelemetry-collector-contrib:0.148.0` | 4317/4318, 8889 |
| **Postgres analítico** | `postgres:16` | 5432 |
| **metricas-loader** | build local (`./metricas-loader`) | — |
| pgAdmin | `dpage/pgadmin4:8` | 5050 |
| App geradora | build local (`./app`) | — |

- Grafana: <http://localhost:3000> (login anônimo como Admin; `admin`/`admin`).
  Datasources **Mimir**, **Loki** e **Tempo** já provisionados com correlação.
- pgAdmin: <http://localhost:5050> (`admin@admin.com`/`admin`), com o servidor
  "Metricas Postgres" já cadastrado.

### Quanto tempo até aparecer dado no Postgres

No primeiro boot, sem watermark, o loader começa em `now - JANELA_INICIAL_MINUTOS`
(60 min por padrão) — ou seja, antes de a stack existir — e avança 30 min de
janela a cada rodada de 10 min. Leva **~15-20 minutos** até alcançar o presente e
as tabelas saírem do zero. É esperado, não é falha. Acompanhe com:

```bash
docker logs -f metricas-loader
```

## O banco analítico

**Não há migrations.** Todo o DDL vive num **arquivo único e idempotente**,
[`metricas-loader/src/loader/sql/schema.sql`](metricas-loader/src/loader/sql/schema.sql):
schema, tabelas, partições, índices, procedures e as functions que o plugin
consome. O próprio `metricas-loader` o aplica **no boot**, protegido por advisory
lock — apontar o `POSTGRES_DSN` para um banco vazio é suficiente.

```bash
# Provisionar sem subir o worker (Job/initContainer):
docker exec metricas-loader env MODO=schema python -m loader

# Ou aplicar o arquivo direto:
psql "$POSTGRES_DSN" -v ON_ERROR_STOP=1 -f metricas-loader/src/loader/sql/schema.sql
```

Para **evoluir o modelo** enquanto o produto não está em produção: edite o objeto
no próprio `schema.sql` (nada de arquivos `NNN_alter_*.sql`) e, se a mudança não
for compatível com a base local, recrie com `docker compose down -v`. A versão
corrente fica em `metricas.config`, chave `schema_versao`.

## Carga de histórico de uma métrica

Para recarregar **uma métrica num período**, chame o comando **dentro do container
do loader**, com o worker rodando normalmente:

```bash
docker exec metricas-loader python -m loader.historico \
  --metrica bradesco_app_mobilepf_total \
  --inicio 2026-09-01 \
  --fim 2026-09-10
```

No OpenShift/ARO, a mesma chamada dentro do pod:

```bash
oc -n metricas rsh deploy/metricas-loader \
  python -m loader.historico --metrica bradesco_app_mobilepf_total \
  --inicio 2026-09-01 --fim 2026-09-10
```

### Argumentos

| Argumento | Obrigatório | Descrição |
| --- | --- | --- |
| `--metrica` | sim | Nome da métrica no Mimir, exatamente como está no `metricas.json`. |
| `--inicio` | sim | Primeiro dia do período (`YYYY-MM-DD`, no fuso de negócio). |
| `--fim` | sim | Último dia do período, **inclusivo**. |
| `--permitir-nova` | não | Libera métrica mapeada que ainda não tem séries em `dim_serie` (primeira carga). |
| `--sem-perfil` | não | Não recalcular o perfil mediano ao final. |

### O que acontece

O comando cria as partições retroativas do período, consulta o Mimir dia a dia em
janelas de `JANELA_MINUTOS`, grava via COPY na staging e chama as mesmas
procedures do fluxo normal. A cada dia concluído roda `atualizar_stats_alvo`
(recordes/mediana); ao final, o perfil mediano. A saída é uma linha JSON por
evento:

```json
{"evento": "métrica validada no analítico", "metrica": "bradesco_app_mobilepf_total", "series_recebidas": 10}
{"evento": "dia carregado", "metrica": "bradesco_app_mobilepf_total", "dia": "2026-09-01", "linhas_gravadas": 710}
{"evento": "carga histórica concluída", "metrica": "bradesco_app_mobilepf_total", "linhas_gravadas": 7100, "dia": "10 dias"}
```

### Regras e garantias

- **Não duplica registro.** A escrita final passa pela mesma
  `metricas.processar_staging()` do worker: `metrica_minuto` tem PK
  `(serie_id, ts)` e a gravação é um upsert que *substitui* o valor do minuto.
  Rodar o mesmo período duas vezes deixa o banco no estado exatamente idêntico.
- **Só carrega métrica já existente no analítico.** Duas checagens: a métrica
  precisa estar declarada e `"ativa": true` no `metricas.json`, e já ter séries
  em `metricas.dim_serie`. Métrica desconhecida é recusada com **exit code 2** e
  a lista do que existe:

  ```
  ERRO: métrica 'metrica_inventada' não está mapeada no analítico.
  Métricas ativas: bradesco_app_mobilepf_total, bradesco_app_pix_mobilepf_total
  ```

- **Não interfere no worker.** O watermark não é tocado — a carga contínua segue
  de onde estava. Rodar as duas coisas ao mesmo tempo é seguro.
- **Rollup por app preservado.** `metrica_minuto_app` é recalculado a partir de
  `metrica_minuto`, não da staging: carregar uma métrica isolada não apaga a
  contribuição de outros produtos que compartilham o mesmo `app`.
- O período é limitado a `now - ATRASO_MINUTOS`: dias futuros são pulados e o dia
  corrente carrega só até o último minuto fechado.

### Conferir o resultado

```bash
docker exec metricas-postgres psql -U metricas -d metricas -c \
  "SELECT dia, total FROM metricas.metrica_dia_alvo
    WHERE nivel='app' AND alvo='mobilepf' ORDER BY dia DESC LIMIT 10"
```

## Outras formas de carga

| Cenário | Comando |
| --- | --- |
| Backfill de **todas** as métricas | `MODO=backfill` + `BACKFILL_INICIO`/`BACKFILL_FIM` |
| Histórico vindo de **arquivo CSV** | `docker exec -i metricas-loader python -m loader.import_csv < historico.csv` |
| Dados sintéticos para testar a tela | `cd metricas-loader && ./script.sh` |

Todas são idempotentes. Detalhes e variáveis de ambiente:
[`metricas-loader/README.md`](metricas-loader/README.md).

## Quais métricas são coletadas

O mapeamento fica em
[`metricas-loader/config/metricas.json`](metricas-loader/config/metricas.json) —
nome da métrica no Mimir, `produto` e de quais labels saem as colunas `app`,
`jornada`, `escopo` e `status`. O arquivo é relido a cada rodada: dá para
adicionar métrica sem reiniciar o container.

Para cada métrica ativa o loader consulta
`sum by (app, jornada, escopo, status) (increase(<metrica>[1m]))`.

## A stack LGTM (desenvolvimento)

A app de [`app/`](app/) simula uma malha de microsserviços e emite **logs, traces
e métricas correlacionados** via OTLP.

### Correlação entre os sinais

Os três sinais compartilham os mesmos atributos de recurso — **`service.name`**,
**`service.namespace`**, **`workload`** e **`deployment.environment`** — então dá
para pivotar entre eles:

- **Métrica → Trace**: métricas gravadas dentro de spans ativos geram *exemplars*
  com `trace_id`; no Grafana, o exemplar do Mimir abre o trace no Tempo.
- **Log → Trace**: cada log carrega `trace_id`/`span_id` do span ativo.
- **Trace → Logs**: *Trace to logs* filtra por `trace_id` no Loki.
- **Trace → Métricas / Service Map**: o metrics-generator do Tempo escreve
  `traces_spanmetrics_*` e o service-graph no Mimir.

### Dimensões para queries

| service.name | namespace | workload | papel |
| --- | --- | --- | --- |
| `frontend-service` | shop | web | ponto de entrada HTTP |
| `checkout-service` | shop | api | orquestra o pedido |
| `payment-service` | shop | api | cobrança/estorno |
| `inventory-service` | shop | api | reserva/estoque |
| `notification-service` | shop | worker | e-mail/SMS |
| `batch-worker` | ops | batch | jobs periódicos (root spans) |

Métricas emitidas: `http_server_requests_total`, `http_server_duration_*`
(histograma), `http_server_active_requests` (up/down), `service_queue_depth`
(gauge), além das métricas de negócio `bradesco_app_*_total` consumidas pelo
analítico.

### Exemplos rápidos de query

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

**Postgres** — o que a tela consulta:

```sql
SELECT * FROM metricas.fn_grafico('app', 'mobilepf', '2026-09-16', '2026-09-15');
```

## Deploy no OpenShift/ARO

Manifestos do worker em [`k8s/`](k8s/) (`oc apply -k k8s/`). O banco recomendado é
gerenciado (Azure Database for PostgreSQL Flexible Server); basta apontar o
`POSTGRES_DSN` do Secret para ele — o loader provisiona o schema sozinho, desde
que o usuário tenha permissão de DDL. Ver [`k8s/README.md`](k8s/README.md).

## Derrubar

```bash
docker compose down          # mantém volumes
docker compose down -v       # remove dados persistidos (Postgres inclusive)
```
