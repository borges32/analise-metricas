# README — Plataforma de Análise de Métricas OpenTelemetry (Mimir → Postgres) — v2

> **Propósito deste documento:** contexto completo da solução para desenvolvimento assistido por IA (Claude Code). Contém a arquitetura, o modelo de dados, as convenções, os contratos entre componentes e as decisões de projeto já fechadas. **As decisões aqui descritas não devem ser alteradas sem alinhamento** — em especial o modelo de dados, o particionamento, o conceito de "alvo de análise" e o padrão de idempotência da carga.
>
> **Changelog v2:** o usuário agora pode analisar um **APP** (soma de todas as séries) **ou uma série individual**. Consequências: `metrica_minuto` retém 24 meses (removidas `metrica_hora` e a degradação); tabelas analíticas unificadas por alvo (`nivel`/`alvo`); interface alimentada por functions SQL; recorde de minuto mantido incrementalmente.

---

## 1. Visão geral do problema

Métricas OpenTelemetry do tipo **counter cumulative delta** no Mimir, no padrão:

```
bradesco.app.{produto}.total{app="nome_app", jornada="jornada_app", escopo="escopo_app", status="sucesso"}
```

Site de análise de volumetria onde o usuário:

1. Seleciona o **nível de análise**: um **app** (soma de todas as séries do app) **ou uma série individual** (combinação produto/app/jornada/escopo/status).
2. Seleciona um **dia analisado** e um **dia comparativo** (análise sempre dia fechado × dia fechado, 24h × 24h).
3. Visualiza um **gráfico de série temporal com volume por minuto**, até 4 curvas alinhadas pelo horário do dia (00:00–23:59):
   - Dia analisado
   - Dia comparativo
   - Dia recorde do alvo (dia com maior volume total da história)
   - Perfil mediano ("dia típico") dos últimos 90 dias, segmentado por dia útil × fim de semana
4. Vê cards de resumo: dia recorde (data e total), minuto recorde (timestamp e valor), mediana de volume/minuto dos últimos 3 meses.

**Escala:** ~2.500 séries ativas, granularidade de minuto, retenção de 24 meses **nos dois níveis**.
**Restrição de plataforma:** **PostgreSQL puro** (sem TimescaleDB). Versão 14+, recomendado 15/16.
**Infra dimensionada:** ~250–280 GB de dados; 64 GB+ RAM; SSD/NVMe.

---

## 2. Arquitetura

```
┌────────┐   query_range    ┌──────────────────┐   COPY (binary)   ┌──────────────────┐
│ Mimir  │ ───────────────► │  Loader Python   │ ────────────────► │ staging_metrica  │
└────────┘  (janelas 15-30m)│  (job contínuo)  │                   │   (UNLOGGED)     │
                            └──────────────────┘                   └────────┬─────────┘
                                                                            │ CALL processar_staging()
                                                                            ▼
                                        ┌───────────────────────────────────────────────┐
                                        │ dim_serie / metrica_minuto (24m, semanal) /   │
                                        │ metrica_minuto_app (24m) / metrica_dia_alvo   │
                                        └────────┬──────────────────────────────────────┘
                                                 │ jobs diários/semanais
                                                 ▼
                                        ┌───────────────────────────────────────────────┐
                                        │ perfil_mediano_alvo / stats_alvo / retenção   │
                                        └────────┬──────────────────────────────────────┘
                                                 │ fn_grafico / fn_perfil / selects (ms)
                                                 ▼
                                        ┌──────────────────┐      ┌───────────────┐
                                        │  API (backend)   │ ───► │  Frontend web │
                                        └──────────────────┘      └───────────────┘
```

| Componente | Responsabilidade |
|---|---|
| **Loader Python** | Consulta o Mimir (`query_range`, step=60s), grava na staging via COPY binário, chama `processar_staging()`. Loop com janela deslizante e atraso de 2–3 min sobre `now()`. |
| **Banco (schema `metricas`)** | Armazenamento, rollups, tabelas analíticas por alvo, functions de interface, jobs. DDL completo em `modelagem_metricas_otel_postgres.md`. |
| **API backend** | Somente leitura: chama `fn_grafico`/`fn_perfil` e lê `stats_alvo`/`dim_serie`. |
| **Frontend** | Seletor nível+alvo (app ou série do app), dias, gráfico com 4 curvas e cards. |

---

## 3. Conceito central: alvo de análise

Toda a camada analítica é indexada por um **alvo**:

| `nivel` | `alvo` | Significado |
|---|---|---|
| `'app'` | nome do app (`dim_serie.app`) | Soma de todas as séries do app |
| `'serie'` | `serie_id::text` | Uma série individual |

As tabelas `metrica_dia_alvo`, `perfil_mediano_alvo` e `stats_alvo` usam `(nivel, alvo)` como chave, e as functions de interface recebem `(p_nivel, p_alvo)`. **Todo código novo (API, frontend, jobs) deve seguir essa convenção** — nunca criar caminhos paralelos separados para app e série.

---

## 4. Decisões de projeto (fechadas)

1. **O loader grava o incremento por minuto (delta), nunca o acumulado.** PromQL: `sum by (produto, app, jornada, escopo, status) (increase(bradesco_app_produto_total[1m]))`, `step=60s`.
2. **Labels normalizadas em dimensão** (`dim_serie`); fato armazena `(ts, serie_id, valor)`.
3. **`metrica_minuto` retém 24 meses em granularidade de minuto** (exigência da análise por série), partição **semanal** (~105 partições ativas). Não existe camada de hora nem degradação.
4. **Rollup `metrica_minuto_app` mantido** (24 meses, partição mensal): fonte do nível app. Gráfico de app em 1 range scan; jobs de 90 dias no nível app leem ~13 M linhas em vez de agregar ~324 M do detalhe. Custo: ~3% do storage, mantido na mesma transação da carga.
5. **A interface lê exclusivamente via functions e tabelas materializadas** (`fn_grafico`, `fn_perfil`, `stats_alvo`, `dim_serie`). O gráfico por série lê `metrica_minuto` filtrando `serie_id` + range de 1 dia — barato por construção.
6. **Recorde/mediana/perfil nunca são calculados por request.** Jobs diários materializam em `stats_alvo` e `perfil_mediano_alvo`.
7. **Recorde de minuto é incremental**: o job varre apenas o dia carregado e compara com o recorde armazenado (`GREATEST` lógico). Nunca ordenar 24 meses por valor. Backfill histórico: chamar `atualizar_stats_alvo(dia)` por dia carregado.
8. **Retenção via `DROP` de partição**, nunca `DELETE` em massa (exceto a limpeza pontual de `metrica_dia_alvo`).
9. **Carga idempotente**: `ON CONFLICT ... DO UPDATE` em todas as tabelas finais; reprocessar janelas é seguro.
10. **Timestamps em UTC (`timestamptz`)**; conversão para o fuso de negócio (`America/Sao_Paulo`, em `metricas.config`) apenas nas bordas. "Dia" = dia no fuso de negócio.
11. **Queries sempre contra as tabelas-pai particionadas** — partition pruning roteia para as partições físicas; nenhum código referencia partições pelo nome.

---

## 5. Modelo de dados (schema `metricas`)

> DDL completo, funções e procedures: `modelagem_metricas_otel_postgres.md`.

| Tabela | PK | Papel | Retenção |
|---|---|---|---|
| `config` | `chave` | Parâmetros (`timezone_negocio`) | — |
| `dim_serie` | `serie_id`; UNIQUE nas 5 labels | Dimensão de séries | — |
| `staging_metrica` | — (UNLOGGED) | Destino do COPY; truncada após processamento | efêmera |
| `metrica_minuto` | `(serie_id, ts)` | **Fato detalhado, 1 min, partição semanal — fonte do nível série** | 24 meses |
| `metrica_minuto_app` | `(app, ts)` | **Rollup por app, partição mensal — fonte do nível app** | 24 meses |
| `metrica_dia_alvo` | `(nivel, alvo, dia)` | Totais diários por alvo (base do dia recorde) | 24 meses |
| `perfil_mediano_alvo` | `(nivel, alvo, tipo_dia, horario)` | Dia típico (90 dias), `tipo_dia in ('util','fds')` | recalculada |
| `stats_alvo` | `(nivel, alvo)` | Recordes e mediana escalar materializados | recalculada |

**Índices:** PKs cobrem o padrão entidade+range; BRIN em `ts` nas fato. **Não criar** b-tree isolado em `ts`.

**Rotinas de banco existentes (chamar, não recriar):**

| Rotina | Tipo | Quando |
|---|---|---|
| `criar_particoes(semanas, meses)` | function | Semanal + **início de cada execução do loader** (idempotente) |
| `processar_staging()` | procedure | Pelo loader, após cada COPY (alimenta dimensão, fato, rollup e totais diários dos 2 níveis) |
| `aplicar_retencao()` | function | Semanal |
| `atualizar_perfil_mediano()` | procedure | Diário (~04:30) — nível série é pesado (~324 M linhas), rodar de madrugada |
| `atualizar_stats_alvo(dia)` | procedure | Diário (~05:30); e por dia no backfill |
| `fn_grafico(nivel, alvo, dia_a, dia_b)` | function | Pela API — gráfico (analisado, comparativo, recorde) |
| `fn_perfil(nivel, alvo, dia)` | function | Pela API — curva do dia típico compatível |

---

## 6. Contrato do Loader Python (Mimir → Postgres)

1. **Consulta ao Mimir:** `query_range`, `step=60s`, expressão PromQL da seção 4.1. **Paginar em janelas de 15–30 min** (2.500 séries estouram limites de resposta em janelas grandes). Alternativa: quebrar por app.
2. **Atraso de segurança:** nunca consultar além de `now() - 3 minutos`.
3. **Watermark:** persistir o último `ts` carregado (tabela própria ou `config`); na dúvida, reprocessar (carga idempotente).
4. **Escrita:** `COPY metricas.staging_metrica FROM STDIN (FORMAT BINARY)` com `psycopg` 3 (`cursor.copy()` + `write_row`). Nunca INSERT multi-row.
5. **Finalização por janela:** `CALL metricas.processar_staging();`.
6. **Início de cada execução:** `SELECT metricas.criar_particoes();`.
7. **Backfill histórico:** mesmo fluxo por janelas passadas; ao final de cada dia carregado, `CALL metricas.atualizar_stats_alvo(dia)` para construir os recordes incrementais.
8. **Observabilidade:** logar por janela: range, linhas recebidas, linhas gravadas, duração; erro claro para resposta truncada/`resultType` inesperado.

**Linha da staging:** `(ts timestamptz, produto, app, jornada, escopo, status text, valor bigint)` — arredondar o float do Mimir para `bigint`.

---

## 7. Contrato da API backend

Somente leitura (usuário de banco com `SELECT` + `EXECUTE` nas functions).

| Endpoint | Fonte | Observações |
|---|---|---|
| `GET /apps` | `dim_serie` | `SELECT DISTINCT app ... ORDER BY app` |
| `GET /apps/{app}/series` | `dim_serie` | `SELECT serie_id, produto, jornada, escopo, status WHERE app=$1` — popula o combo de séries |
| `GET /analise?nivel=&alvo=&dia=&dia_comp=` | `fn_grafico` + `fn_perfil` + `stats_alvo` | Resposta consolidada: 4 curvas + cards |
| `GET /perfil?nivel=&alvo=&dia=` | `fn_perfil` | Curva do dia típico isolada (se o front buscar separado) |

Regras:
- `nivel` ∈ {`app`, `serie`}; `alvo` = nome do app ou `serie_id` (string). Validar `nivel` na API; a function também valida.
- Datas `YYYY-MM-DD` no fuso de negócio; sempre queries parametrizadas.
- Gráfico: até 1.440 pontos/curva, campo `horario` (`HH:MM`); minutos sem tráfego não existem na tabela — API/front decidem `0` vs `null`.
- Exemplo de chamada: `SELECT * FROM metricas.fn_grafico('serie', '1042', '2026-07-03', '2026-06-26');`

---

## 8. Jobs e agendamento

| Job | Frequência | Rotina | Custo |
|---|---|---|---|
| Criar partições | Semanal (dom 01:00) + a cada carga | `criar_particoes()` | ms |
| Retenção | Semanal (dom 03:00) | `aplicar_retencao()` | DROP instantâneo |
| Perfil mediano | Diário 04:30 | `atualizar_perfil_mediano()` | nível app: segundos; nível série: **minutos** (324 M linhas) |
| Stats por alvo | Diário 05:30 | `atualizar_stats_alvo()` | recorde: incremental (dia anterior); mediana 3m série: pesada |

Ordem no dia: perfil antes de stats é indiferente; ambos após a virada do dia no fuso de negócio e antes do horário comercial.

---

## 9. Volumetria e desempenho esperados

| Tabela | Linhas (regime) | Tamanho |
|---|---|---|
| `metrica_minuto` (24 meses) | ~2,6 bi | ~150 GB heap + ~100 GB índice |
| `metrica_minuto_app` (24 meses) | ~100 M | ~8 GB |
| `metrica_dia_alvo` | ~1,9 M | < 200 MB |
| `perfil_mediano_alvo` | ~7,5 M | < 1 GB |
| **Total** | | **~250–280 GB** |

- Carga: ~3,6 M linhas/dia via COPY binário.
- `fn_grafico` app: 1 range scan no rollup (≤ 4.320 linhas) → ms. `fn_grafico` série: range scans na PK do detalhe (≤ 4.320 linhas) → ms.
- SLO sugerido: p95 < 200 ms na API para `/analise`.

---

## 10. Diretrizes para desenvolvimento com IA (Claude Code)

- **Não alterar** DDL, PKs, particionamento, o conceito `nivel`/`alvo` ou as procedures sem discussão explícita.
- **Nunca** referenciar partições físicas no código; sempre a tabela-pai.
- **Nunca** calcular recorde/mediana/perfil em request; ler de `stats_alvo`/`perfil_mediano_alvo` via functions.
- **Nunca** varrer 24 meses de `metrica_minuto` em query online ou ordenar o fato por `valor`; o recorde é incremental por design.
- Toda escrita nas tabelas finais passa por `processar_staging()`; o loader não faz INSERT direto no fato.
- Toda query nova sobre tabelas fato **deve** filtrar `ts` por range (partition pruning); validar com `EXPLAIN` que só as partições esperadas aparecem.
- Datas de usuário no fuso de negócio; conversão via `AT TIME ZONE` com o valor de `metricas.config` — nunca offset fixo.
- Stack sugerida: loader Python 3.11+ (`psycopg` 3, `httpx`); API FastAPI ou similar; frontend com gráficos que suportem 4 séries × 1.440 pontos (ECharts, Chart.js, Recharts).
- Testes prioritários: idempotência da carga; branch app × série de `fn_grafico` (soma do app = soma das séries); recorde incremental (novo máximo substitui, valor menor não); perfil `util`/`fds`; pruning; alvo inexistente (curvas vazias sem erro).

## 11. Arquivos do projeto

- `modelagem_metricas_otel_postgres.md` — DDL v2 completo, functions, procedures, agendamento e tuning. **Executar antes de qualquer desenvolvimento.**
- `README.md` — este documento (contexto e contratos, v2).
