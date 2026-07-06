# plugin-analitico — App Grafana de Análise de Volumetria

App plugin do Grafana (>= 10.4) que exibe uma tela analítica de volumetria por
**app** ou **série**, consumindo o PostgreSQL do projeto **exclusivamente pelas
functions de interface** (`fn_grafico`, `fn_perfil`) e pela tabela `stats_alvo`.

## Acesso aos dados

Todo SQL é executado **via datasource PostgreSQL do Grafana** (`getDataSourceSrv`),
nunca por conexão direta. As únicas queries emitidas são as do contrato da API
(README do projeto): apps, séries do app, `fn_grafico`, `fn_perfil`, `stats_alvo`.

O SQL é montado em [`src/sqlSafe.ts`](src/sqlSafe.ts) com **validação por allowlist**
antes de qualquer interpolação (o `rawSql` do datasource não tem bind params):
`nivel` ∈ `app|serie`; `alvo` de série validado como inteiro; datas em parse estrito
`YYYY-MM-DD` re-serializadas; strings escapadas.

### GRANT mínimo do usuário do datasource

```sql
GRANT USAGE ON SCHEMA metricas TO grafana_ro;
GRANT SELECT ON metricas.dim_serie, metricas.stats_alvo TO grafana_ro;
GRANT EXECUTE ON FUNCTION
  metricas.fn_grafico(text, text, date, date),
  metricas.fn_perfil(text, text, date) TO grafana_ro;
```

## Build

```bash
npm install --omit=peer   # deps do Grafana são externals (fornecidas em runtime)
npm run build             # gera dist/ (module.js + plugin.json + img)
npm test                  # Jest: sqlSafe + transform
```

## Instalar no Grafana

Monte `dist/` em `/var/lib/grafana/plugins/lgtm-analitico-app` e permita o plugin
não-assinado:

```
GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=lgtm-analitico-app
```

Depois, em **Configuração do app**, selecione o datasource PostgreSQL. O estado é
persistido em `jsonData.datasourceUid`. A página aparece no menu lateral (Análise).

## Estrutura

```
src/
├── module.tsx        # AppPlugin (root page + config page)
├── plugin.json       # metadados do app plugin
├── sqlSafe.ts        # allowlist + montagem segura de SQL  (testado)
├── transform.ts      # fn_grafico/fn_perfil -> 4 curvas alinhadas por horário (testado)
├── api.ts            # getDataSourceSrv + query rawSql, DataFrame -> linhas
├── dates.ts          # datas no fuso America/Sao_Paulo
├── types.ts, constants.ts
└── components/
    ├── App.tsx           # rotas
    ├── AnalysisPage.tsx  # seletores + estados + URL sync
    ├── AnalysisChart.tsx # timeseries (PanelRenderer) com 4 curvas
    ├── SummaryCards.tsx  # cards de resumo
    └── ConfigPage.tsx    # seleção do datasource
```
