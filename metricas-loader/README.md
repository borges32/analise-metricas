# metricas-loader

Worker de carga contínua **Mimir → PostgreSQL** para a plataforma de análise de
métricas OpenTelemetry. Consulta o Mimir (`query_range`), grava na `staging` via
COPY binário e chama as procedures do banco (`processar_staging`,
`atualizar_stats_alvo`). Segue os contratos de `analitico-metrica-ddl/`.

## Modos de execução

| Modo | Como | O que faz |
| --- | --- | --- |
| `worker` | `MODO=worker` (default) | Loop a cada `INTERVALO_SEGUNDOS` (10 min), janela deslizante com catch-up. |
| `backfill` | `MODO=backfill` + `BACKFILL_INICIO`/`BACKFILL_FIM` | Recarrega um período do **Mimir** dia a dia. |
| **CSV** | `python -m loader.import_csv` | Carga de **histórico a partir de um arquivo CSV** (ver abaixo). |
| Seed | `./script.sh` | Gera 2 meses de dados sintéticos para testar a solução. |

## Configuração (variáveis de ambiente)

Todas as configurações vêm de variáveis de ambiente, validadas no boot com
`pydantic-settings` (falha explícita se faltar uma obrigatória). Contrato completo:

### Obrigatórias

| Variável | Exemplo | Descrição |
| --- | --- | --- |
| `POSTGRES_DSN` | `postgresql://metricas:metricas@metricas-postgres:5432/metricas` | DSN completo do Postgres de destino (schema `metricas`). |
| `MIMIR_URL` | `http://mimir:9009/prometheus` | Base URL da API Prometheus do Mimir. **Inclua o prefixo `/prometheus`** (o cliente concatena `/api/v1/query_range`). Não obrigatória para o import CSV, mas exigida pelo worker. |

### Autenticação do Mimir (opcionais)

| Variável | Default | Descrição |
| --- | --- | --- |
| `MIMIR_TENANT` | — | Valor do header `X-Scope-OrgID` (multi-tenant). Ex.: `anonymous`. |
| `MIMIR_BEARER_TOKEN` | — | Autenticação `Authorization: Bearer <token>`. |
| `MIMIR_BASIC_AUTH_USER` | — | Usuário para basic auth. |
| `MIMIR_BASIC_AUTH_PASS` | — | Senha para basic auth. |
| `MIMIR_TIMEOUT_SEGUNDOS` | `60` | Timeout (s) das requisições HTTP ao Mimir. |

### Janela e ritmo do worker

| Variável | Default | Descrição |
| --- | --- | --- |
| `INTERVALO_SEGUNDOS` | `600` | Tempo de espera entre rodadas do worker (**10 min**). Em atraso, o worker emenda rodadas (catch-up) sem dormir até alcançar o presente. |
| `JANELA_MINUTOS` | `30` | Tamanho máximo da janela consultada por rodada. Regime normal carrega ~10 min; o teto de 30 só é atingido em recuperação de atraso. |
| `ATRASO_MINUTOS` | `3` | Atraso de segurança sobre `now()`. Nunca consulta além de `now() - ATRASO_MINUTOS` (evita minutos incompletos no Mimir). |
| `JANELA_INICIAL_MINUTOS` | `60` | No primeiro boot (sem watermark), quanto voltar no tempo para iniciar a carga. |

### Métricas e logs

| Variável | Default | Descrição |
| --- | --- | --- |
| `CONFIG_METRICAS_PATH` | `/app/config/metricas.json` | Caminho do JSON com as métricas a carregar (nome, produto, labels, `ativa`). Recarregado a cada rodada — dá para adicionar métricas sem reiniciar. |
| `LOG_LEVEL` | `INFO` | Nível de log (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Logs em JSON, uma linha por evento. |
| `HEALTHCHECK_PATH` | `/tmp/healthy` | Arquivo tocado a cada rodada bem-sucedida; o `HEALTHCHECK` do Dockerfile alarma se ficar velho (> 2× `INTERVALO_SEGUNDOS`). |

### Modo de execução

| Variável | Default | Descrição |
| --- | --- | --- |
| `MODO` | `worker` | `worker` (loop contínuo) ou `backfill` (carga histórica do Mimir, sem loop). |
| `BACKFILL_INICIO` | — | Data inicial `YYYY-MM-DD` — **obrigatória** quando `MODO=backfill`. |
| `BACKFILL_FIM` | — | Data final `YYYY-MM-DD` — **obrigatória** quando `MODO=backfill`. |

> O import CSV (`python -m loader.import_csv`) usa apenas `POSTGRES_DSN` e `LOG_LEVEL`
> — não depende das variáveis do Mimir nem do `MODO`.

### Exemplo mínimo (worker)

```bash
POSTGRES_DSN=postgresql://metricas:metricas@metricas-postgres:5432/metricas \
MIMIR_URL=http://mimir:9009/prometheus \
MIMIR_TENANT=anonymous \
python -m loader
```

---

## Carga de histórico via CSV

Script para carregar **dados retroativos de um período grande** a partir de um CSV.
Ele **cria as partições retroativas** automaticamente (o `criar_particoes()` padrão
só cobre do presente para frente), carrega em lotes via COPY + `processar_staging`
(idempotente) e recalcula `stats_alvo` (recorde/mediana) por dia e o perfil mediano.
**Não altera o watermark do worker.**

### Estrutura do arquivo CSV

O CSV tem **exatamente as colunas da `metricas.staging_metrica`**, com cabeçalho:

```
ts,produto,app,jornada,escopo,status,valor
```

| Coluna | Tipo | Regras |
| --- | --- | --- |
| `ts` | timestamp | ISO 8601 (`2026-02-10T09:00:00+00:00`) ou `YYYY-MM-DD HH:MM:SS`. **Sem timezone assume-se UTC.** Truncado ao minuto por padrão. |
| `produto` | texto | Não pode ser vazio (coluna NOT NULL). Ex.: `mobilepf`. |
| `app` | texto | Não pode ser vazio. Ex.: `mobilepf`. |
| `jornada` | texto | Não pode ser vazio. Ex.: `login`. |
| `escopo` | texto | Não pode ser vazio. Ex.: `obter-token`. |
| `status` | texto | Não pode ser vazio. Ex.: `sucesso` / `falha`. |
| `valor` | inteiro | Delta do minuto (aceita float, é arredondado). Ex.: `135`. |

Observações:
- É o **delta por minuto** (não o acumulado), igual ao que o loader grava.
- Linhas com a **mesma** `(ts, produto, app, jornada, escopo, status)` são **somadas**.
- Datas em UTC no arquivo; o "dia" analítico é derivado no fuso de negócio
  (`America/Sao_Paulo`, lido de `metricas.config`).

### Exemplo (`historico.csv`)

```csv
ts,produto,app,jornada,escopo,status,valor
2026-02-10T09:00:00+00:00,mobilepf,mobilepf,extrato,consulta,sucesso,120
2026-02-10T09:01:00+00:00,mobilepf,mobilepf,extrato,consulta,sucesso,135
2026-02-10T09:01:00+00:00,mobilepf,mobilepf,extrato,consulta,falha,4
2026-02-10 09:02:00,mobilepf,mobilepf,saldo,consulta,sucesso,98
2026-02-11T14:30:00+00:00,pix-mobilepf,pix-mobilepf,pagamento,efetivacao,sucesso,210
```

### Como chamar dentro do container

O script roda **dentro do container** do loader (`metricas-loader`).

**Opção A — via stdin (sem montar volume, mais simples):**

```bash
docker exec -i metricas-loader python -m loader.import_csv < historico.csv
```

**Opção B — arquivo montado no container:**

```bash
# monte o arquivo/pasta (ex.: -v $(pwd)/dados:/data) e aponte com --file
docker exec metricas-loader python -m loader.import_csv --file /data/historico.csv
```

**Opção C — abrindo um shell no container:**

```bash
docker exec -it metricas-loader sh
python -m loader.import_csv --file /data/historico.csv
```

### No OpenShift (`oc`)

No OpenShift o equivalente ao `docker exec` é o `oc exec`. Primeiro descubra o pod
do loader; depois execute o import via stdin (sem precisar copiar arquivo).

```bash
# 1) Selecionar o projeto (namespace) e achar o pod do loader
oc project meu-namespace
POD=$(oc get pods -l app=metricas-loader -o jsonpath='{.items[0].metadata.name}')

# 2) Carregar o CSV via stdin  (-i mantém o stdin aberto)
oc exec -i "$POD" -- python -m loader.import_csv < historico.csv
```

Alternativa — copiar o arquivo para o pod e usar `--file`:

```bash
oc cp historico.csv "$POD":/tmp/historico.csv
oc exec "$POD" -- python -m loader.import_csv --file /tmp/historico.csv --batch 100000
```

Ou abrir um shell no pod (`oc rsh`) e rodar interativamente:

```bash
oc rsh "$POD"
python -m loader.import_csv --file /tmp/historico.csv
```

> Dica: se preferir uma execução isolada (sem usar o pod do worker), rode como um
> Job pontual com a mesma imagem, montando o CSV via ConfigMap/volume:
> `oc run import-hist --image=<imagem-do-loader> --restart=Never --rm -i \
>   --env POSTGRES_DSN="$POSTGRES_DSN" -- python -m loader.import_csv < historico.csv`

### Opções

| Flag | Default | Descrição |
| --- | --- | --- |
| `--file <path>` | stdin | Caminho do CSV dentro do container. Sem a flag, lê do stdin. |
| `--batch <n>` | `50000` | Linhas por lote (COPY + processar_staging por lote). |
| `--sem-truncar` | (off) | Não trunca `ts` para o minuto (mantém segundos do arquivo). |
| `--sem-stats` | (off) | Não recalcula `stats_alvo`/perfil ao final (mais rápido; recalcule depois). |

### O que o script garante

- **Partições retroativas**: cria as partições semanais (`metrica_minuto`) e mensais
  (`metrica_minuto_app`) do período do arquivo, com a mesma convenção de nomes do
  `criar_particoes()` — idempotente (`CREATE TABLE IF NOT EXISTS ... PARTITION OF`).
- **Idempotência**: reexecutar o mesmo CSV não duplica (as procedures usam
  `ON CONFLICT DO UPDATE`).
- **Recordes/perfil**: ao final, roda `atualizar_stats_alvo(dia)` para cada dia
  carregado e `atualizar_perfil_mediano()`, populando o que o plugin/API consomem.
- **Streaming em lotes**: arquivos grandes são processados sem carregar tudo em
  memória (lotes de `--batch` linhas).
