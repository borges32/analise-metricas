# Prompt — Implementação do Loader de Carga (Mimir → Postgres)

> **Como usar:** cole o conteúdo abaixo como prompt no Claude Code, na raiz do repositório que já contém o `README.md` e o `modelagem_metricas_otel_postgres.md` do projeto. O prompt referencia esses arquivos como fonte de verdade.

---

## PROMPT

Implemente um projeto Python chamado **`metricas-loader`**: um worker de carga contínua que consulta métricas no **Mimir** (API compatível com Prometheus) e as carrega no **PostgreSQL**, seguindo estritamente os contratos definidos nos arquivos `analitico-metrica-ddl/README.md` (seção "Contrato do Loader Python") e `analitico-metrica-ddl/modelagem_metricas_otel_postgres.md` deste repositório. Leia esses dois arquivos antes de escrever qualquer código — o schema do banco, as procedures e o padrão de idempotência **já existem e não devem ser recriados nem alterados**; o loader apenas os consome.

### 1. Comportamento geral (worker)

- O processo roda **em container, em loop infinito**: a cada ciclo, executa uma rodada de carga e dorme pelo intervalo configurado via variável de ambiente `INTERVALO_SEGUNDOS` (**default 600 — o worker roda a cada 10 minutos**). O intervalo permanece parametrizável por ambiente, mas 10 minutos é o valor operacional do projeto.
- Em regime normal, cada rodada carrega ~10 minutos de dados (do watermark até `now() - ATRASO_MINUTOS`); o teto `JANELA_MINUTOS` (default 30) só é atingido em recuperação de atraso (catch-up após indisponibilidade). Nesse caso o worker deve **emendar rodadas sem dormir** até o watermark alcançar o presente, dormindo `INTERVALO_SEGUNDOS` apenas quando a janela disponível for menor que `JANELA_MINUTOS`.
- Cada rodada de carga:
  1. Chama `SELECT metricas.criar_particoes();` (defesa idempotente contra partição inexistente).
  2. Lê o **watermark** (último `ts` carregado com sucesso) da tabela `metricas.config` (chave `loader_watermark`, criar se não existir com valor default = `now() - JANELA_INICIAL_MINUTOS`).
  3. Define a janela de consulta: de `watermark` até `min(watermark + JANELA_MINUTOS, now() - ATRASO_MINUTOS)`. Se a janela resultante for vazia ou negativa, apenas loga e dorme.
  4. Para **cada métrica do arquivo de configuração JSON** (seção 3): executa `query_range` no Mimir com `step=60s` e a expressão PromQL montada a partir do config.
  5. Converte o resultado para linhas da staging e grava via **`COPY metricas.staging_metrica FROM STDIN (FORMAT BINARY)`** usando `psycopg` 3 (`cursor.copy()` + `write_row`). Proibido INSERT multi-row.
  6. Ao final da janela (todas as métricas), executa `CALL metricas.processar_staging();` **na mesma conexão/transação da escrita** e, se tudo OK, avança o watermark para o fim da janela (commit atômico: staging processada + watermark juntos).
  7. Se a janela consumida cruzou uma virada de dia no fuso de negócio (`America/Sao_Paulo`, lido de `metricas.config`), executa `CALL metricas.atualizar_stats_alvo(dia_fechado);` para o(s) dia(s) que fecharam.
- **Reprocessamento é sempre seguro** (a carga é idempotente via `ON CONFLICT`); em caso de dúvida ou falha parcial, a rodada seguinte retoma do watermark antigo.
- O loop deve capturar exceções por rodada: logar, aplicar **backoff exponencial com teto** (ex.: 5s → 10s → ... → máx 5 min) e continuar. O processo nunca morre por erro transitório de rede/banco.
- Tratar `SIGTERM`/`SIGINT` com **graceful shutdown**: terminar a janela em andamento (ou abortar a transação limpa) antes de sair — essencial para rolling updates de container.

### 2. Consulta ao Mimir

- Endpoint: `GET/POST {MIMIR_URL}/api/v1/query_range` com `query`, `start`, `end`, `step=60`.
- Headers: `X-Scope-OrgID: {MIMIR_TENANT}` quando `MIMIR_TENANT` estiver definido; suportar também `MIMIR_BEARER_TOKEN` e/ou `MIMIR_BASIC_AUTH_USER`/`MIMIR_BASIC_AUTH_PASS` (todos opcionais).
- A expressão PromQL por métrica é: `sum by ({labels}) (increase({nome_metrica}[1m]))`, onde `{labels}` são as labels declaradas no JSON de configuração.
- **Nunca consultar além de `now() - ATRASO_MINUTOS`** (default 3) para não capturar minutos incompletos.
- **Paginação obrigatória**: janelas de no máximo `JANELA_MINUTOS` (default 30). Com ~2.500 séries, janelas maiores estouram limites de resposta do Mimir. Se a resposta vier truncada ou com erro de limite, dividir a janela ao meio e tentar novamente (bisseção, com mínimo de 5 minutos).
- Validar `status == "success"` e `data.resultType == "matrix"`; qualquer outra coisa é erro logado com o corpo da resposta.
- Conversão de valores: o Mimir devolve float; arredondar para `int` (`round()`) antes de gravar (`valor bigint`). Descartar amostras `NaN`. Truncar o timestamp para o minuto.
- Usar `httpx` com timeout configurável (`MIMIR_TIMEOUT_SEGUNDOS`, default 60) e retry (3 tentativas com backoff) para erros 5xx/timeout.

### 3. Arquivo de configuração JSON das métricas

As métricas e suas dimensões **não são hardcoded**: são lidas de um arquivo JSON cujo caminho vem da variável `CONFIG_METRICAS_PATH` (default `/app/config/metricas.json`, montado como volume/ConfigMap no container). Formato:

```json
{
  "metricas": [
    {
      "nome": "bradesco_app_pix_total",
      "produto": "pix",
      "labels": {
        "app": "app",
        "jornada": "jornada",
        "escopo": "escopo",
        "status": "status"
      },
      "ativa": true
    },
    {
      "nome": "bradesco_app_cartoes_total",
      "produto": "cartoes",
      "labels": {
        "app": "app",
        "jornada": "jornada",
        "escopo": "escopo",
        "status": "status"
      },
      "ativa": true
    }
  ]
}
```

Regras de interpretação:
- `nome`: nome da métrica no Mimir (usado na expressão PromQL).
- `produto`: valor fixo gravado na coluna `produto` da staging (o produto vem do nome da métrica no padrão `bradesco.app.{produto}.total`, não de label).
- `labels`: mapeamento **coluna da staging → nome da label no Mimir**. As chaves obrigatórias são `app`, `jornada`, `escopo`, `status` (colunas da `metricas.staging_metrica`); os valores permitem que a label tenha nome diferente no Mimir (ex.: `"jornada": "journey"`). Se uma label vier ausente numa série, gravar string `"desconhecido"` na coluna correspondente (nunca NULL — as colunas são NOT NULL).
- `ativa`: métricas com `false` são ignoradas (permite desligar sem remover do arquivo).
- Validar o arquivo no startup com **Pydantic**; erro de schema → falha explícita no boot com mensagem clara (fail-fast). Recarregar o arquivo a cada rodada (permite adicionar métricas sem reiniciar o container).

### 4. Variáveis de ambiente (contrato completo)

| Variável | Obrigatória | Default | Descrição |
|---|---|---|---|
| `POSTGRES_DSN` | sim | — | DSN completo (`postgresql://user:pass@host:5432/db`) |
| `MIMIR_URL` | sim | — | Base URL do Mimir (sem `/api/v1`) |
| `MIMIR_TENANT` | não | — | Valor do header `X-Scope-OrgID` |
| `MIMIR_BEARER_TOKEN` | não | — | Autenticação Bearer |
| `MIMIR_BASIC_AUTH_USER` / `MIMIR_BASIC_AUTH_PASS` | não | — | Autenticação básica |
| `MIMIR_TIMEOUT_SEGUNDOS` | não | `60` | Timeout HTTP |
| `INTERVALO_SEGUNDOS` | não | `600` | **Tempo de espera entre rodadas do worker (10 min)** |
| `JANELA_MINUTOS` | não | `30` | Tamanho máximo da janela por rodada |
| `ATRASO_MINUTOS` | não | `3` | Atraso de segurança sobre `now()` |
| `JANELA_INICIAL_MINUTOS` | não | `60` | Quanto voltar no primeiro boot (sem watermark) |
| `CONFIG_METRICAS_PATH` | não | `/app/config/metricas.json` | Caminho do JSON de métricas |
| `LOG_LEVEL` | não | `INFO` | Nível de log |
| `MODO` | não | `worker` | `worker` (loop) ou `backfill` |
| `BACKFILL_INICIO` / `BACKFILL_FIM` | se `MODO=backfill` | — | Range de datas `YYYY-MM-DD` para carga histórica |

Carregar e validar com **pydantic-settings** (fail-fast em variável obrigatória ausente).

### 5. Modo backfill

Com `MODO=backfill`, o processo não entra em loop: itera as janelas de `BACKFILL_INICIO` a `BACKFILL_FIM` (mesmo pipeline de janela da rodada normal), e **ao fechar cada dia** executa `CALL metricas.atualizar_stats_alvo(dia);` (obrigatório para construir os recordes incrementais — ver README, decisão 7). Ao terminar, encerra com exit code 0. O backfill não toca o watermark do modo worker.

### 6. Estrutura do projeto

```
metricas-loader/
├── pyproject.toml            # deps: psycopg[binary]>=3.1, httpx, pydantic, pydantic-settings
├── Dockerfile                # python:3.12-slim, multi-stage, non-root user, ENTRYPOINT python -m loader
├── docker-compose.yml        # loader + postgres local para desenvolvimento
├── config/
│   └── metricas.example.json
├── src/loader/
│   ├── __main__.py           # entrypoint: escolhe worker ou backfill pelo MODO
│   ├── settings.py           # pydantic-settings (env vars)
│   ├── config_metricas.py    # parse/validação do JSON (Pydantic)
│   ├── mimir.py              # cliente query_range (httpx, retry, bisseção de janela)
│   ├── db.py                 # conexão psycopg3, COPY binário, watermark, calls das procedures
│   ├── pipeline.py           # orquestração da janela: mimir → staging → processar_staging → watermark
│   ├── worker.py             # loop, sleep INTERVALO_SEGUNDOS, backoff, sinais
│   └── backfill.py           # iteração histórica + atualizar_stats_alvo por dia
└── tests/
    ├── test_config_metricas.py
    ├── test_mimir_parse.py   # parsing de matrix, NaN, labels ausentes, truncamento p/ minuto
    ├── test_pipeline.py      # idempotência (mocks), atomicidade watermark+staging
    └── test_janela.py        # cálculo de janelas, atraso, bisseção, virada de dia no fuso
```

### 7. Logging e observabilidade

- Logs **estruturados em JSON** (uma linha por evento) para stdout: `timestamp, level, evento, janela_inicio, janela_fim, metrica, series_recebidas, linhas_gravadas, duracao_ms, erro`.
- Eventos mínimos: início/fim de rodada, início/fim de janela por métrica, bisseção acionada, watermark avançado, stats_alvo executado, erro com stacktrace.
- Expor um arquivo de **healthcheck** simples (ex.: tocar `/tmp/healthy` a cada rodada bem-sucedida) e declarar `HEALTHCHECK` no Dockerfile verificando a idade do arquivo contra `2 × INTERVALO_SEGUNDOS` (com o default de 10 min, alarma após ~20 min sem rodada bem-sucedida) — permite liveness probe em Kubernetes.

### 8. Regras invioláveis (do README do projeto)

1. O loader **não faz INSERT direto** em `metrica_minuto`, `metrica_minuto_app` ou tabelas analíticas — toda escrita final passa por `CALL metricas.processar_staging()`.
2. Escrita na staging **exclusivamente via COPY binário**.
3. Avanço do watermark **na mesma transação** do `processar_staging()` — nunca avançar antes.
4. Nunca consultar o Mimir além de `now() - ATRASO_MINUTOS`.
5. Grava o **delta por minuto** (resultado do `increase(...[1m])`), nunca valor acumulado.
6. Datas/dias no fuso de negócio lido de `metricas.config`; timestamps sempre `timestamptz` UTC no banco.
7. Nenhuma referência a partições físicas por nome.

### 9. Critérios de aceite

- [ ] `docker compose up` sobe Postgres local **vazio**; o próprio loader aplica o DDL único e idempotente (`src/loader/sql/schema.sql`) no boot e roda contra um mock/stub do Mimir.
- [ ] Rodar a mesma janela duas vezes não altera os totais no banco (idempotência comprovada em teste).
- [ ] Derrubar o processo no meio de uma janela e religar retoma do watermark sem perda nem duplicação.
- [ ] Métrica adicionada ao JSON entra na carga na rodada seguinte, sem restart.
- [ ] `MODO=backfill` de 2 dias popula `metrica_minuto`, `metrica_minuto_app`, `metrica_dia_alvo` e `stats_alvo` corretamente.
- [ ] Com os defaults, o worker executa uma rodada a cada 10 minutos carregando os ~10 minutos pendentes; alterar `INTERVALO_SEGUNDOS` muda o ritmo sem mudança de código.
- [ ] Após 1h de parada simulada, o worker religa e recupera o atraso emendando rodadas (catch-up) até voltar ao regime de 10 em 10 minutos.
- [ ] `ruff` e `pytest` passando; type hints completos; sem segredos hardcoded.

Comece propondo o esqueleto dos módulos e o `settings.py`, e siga na ordem: config JSON → cliente Mimir → camada de banco → pipeline → worker/backfill → Dockerfile/compose → testes.
