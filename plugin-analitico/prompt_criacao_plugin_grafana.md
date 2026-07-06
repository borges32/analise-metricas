# Prompt — Implementação do Plugin Grafana de Análise de Métricas


Implemente um **plugin de aplicativo (app plugin) para Grafana** chamado **`plugin-analitico`**: uma tela analítica de volumetria que consome o PostgreSQL do projeto, seguindo estritamente os contratos definidos no `README.md` (seções "Conceito central: alvo de análise" e "Contrato da API backend") e no `modelagem_metricas_otel_postgres.md` deste repositório. Leia esses dois arquivos antes de escrever qualquer código — o schema, as functions de interface (`fn_grafico`, `fn_perfil`) e as tabelas materializadas **já existem e são a única forma de acesso aos dados**; o plugin não cria queries analíticas próprias sobre as tabelas fato.

### 1. Tipo de plugin e stack

- **App plugin** do Grafana (não panel isolado): a análise é uma página completa com seletores, gráfico e cards, registrada no menu lateral do Grafana.
- Compatibilidade: Grafana **>= 10.4**
- Como backend próprio em go que irá conectar na base Postgres.

### 2. Acesso aos dados (regra central)

- O plugin executa SQL **exclusivamente via datasource PostgreSQL do Grafana** (`getDataSourceSrv().get(uid)` + `query()` com `rawSql`), nunca conexão direta.
- O **UID do datasource é configurável** na página de configuração do app (`plugin.json` → `includes`/`configPages`), persistido em `jsonData`. O usuário do banco configurado no datasource deve ter apenas `SELECT` + `EXECUTE` no schema `metricas` (documentar no README do plugin o `GRANT` mínimo).
- **Somente estas queries são permitidas** (as mesmas do contrato da API no README do projeto):
  1. Combo de apps: `SELECT DISTINCT app FROM metricas.dim_serie ORDER BY app;`
  2. Combo de séries do app: `SELECT serie_id, produto, jornada, escopo, status FROM metricas.dim_serie WHERE app = ... ORDER BY jornada, escopo, status;`
  3. Gráfico: `SELECT * FROM metricas.fn_grafico(nivel, alvo, dia_a, dia_b);`
  4. Dia típico: `SELECT * FROM metricas.fn_perfil(nivel, alvo, dia_a);`
  5. Cards: `SELECT ... FROM metricas.stats_alvo WHERE nivel = ... AND alvo = ...;`
- **Segurança contra injeção** (o datasource SQL do Grafana não tem bind parameters no `rawSql`): valores interpolados no SQL devem ser **sempre validados por allowlist antes da interpolação** — `nivel` só aceita `'app'|'serie'`; `alvo` de app deve existir na lista retornada pela query 1; `alvo` de série deve ser um `serie_id` retornado pela query 2 (inteiro validado com `Number.isInteger`); datas devem passar por parse estrito `YYYY-MM-DD` e serem re-serializadas a partir do objeto de data (nunca ecoar a string do input). Centralizar isso num módulo `sqlSafe.ts` com testes.

### 3. UX da tela de análise

Layout em três blocos (usar componentes do `@grafana/ui`):

**a) Barra de seleção (topo):**
- `RadioButtonGroup` **Nível**: `APP` | `Série`.
- `Select` **App** (query 1). Se nível = Série, um segundo `Select` **Série** (query 2), com label amigável `jornada / escopo / status` e value = `serie_id`.
- `DatePicker` **Dia analisado** e **Dia comparativo** (default: ontem e mesmo dia da semana anterior, no fuso `America/Sao_Paulo`).
- Botão **Analisar** (a tela não dispara queries a cada mudança de seletor; só no clique e no load com estado da URL).
- Todo o estado dos seletores sincronizado na **URL** (query params `nivel, alvo, dia, dia_comp`) para permitir compartilhar links de análise.

**b) Gráfico timeseries (centro):**
- Painel de série temporal (visualização `timeseries` via Scenes ou `TimeSeries` do `@grafana/ui`) com **até 4 curvas sobrepostas**: Dia analisado, Dia comparativo, Dia recorde, Dia típico (perfil mediano).
- **Alinhamento por horário do dia:** as functions retornam `horario` (`time`); para sobrepor as curvas no eixo X do Grafana, converter cada `horario` para um timestamp sintético de um **dia-base fixo** (ex.: `1970-01-02T{horario}Z`) e formatar o eixo como `HH:mm`. As 4 curvas compartilham o mesmo eixo; o dia real de cada curva aparece na legenda (ex.: `Analisado — 03/07/2026`, `Recorde — 15/12/2025`).
- Estilo: analisado em linha sólida destacada; comparativo sólida secundária; recorde tracejada; dia típico pontilhada com opacidade reduzida. Tooltip compartilhado mostrando os 4 valores do minuto.
- Minutos ausentes nas respostas (sem tráfego) devem virar `0` no dataframe (as functions não retornam minutos inexistentes).
- Curva do recorde ausente quando `stats_alvo` não tem o alvo (alvo novo): omitir a série com aviso discreto, sem erro.

**c) Cards de resumo (abaixo ou à direita):**
- 4 cards (`Card`/`BigValue` do `@grafana/ui`): **Dia recorde** (data + total), **Minuto recorde** (timestamp no fuso de negócio + valor), **Mediana 3m** (volume/min), **Total do dia analisado** (soma da curva, calculada no front) com **variação % vs. dia comparativo**.
- Rodapé com `atualizado_em` de `stats_alvo` ("estatísticas atualizadas em ...").

Estados obrigatórios: loading (skeleton), erro de query (alerta com mensagem e retry), alvo sem dados no dia (mensagem "sem volumetria no período"), datasource não configurado (call-to-action para a página de configuração do app).

### 4. Conversão de resultados (dataframes)

- As respostas do datasource chegam como `DataFrame`; criar um módulo `transform.ts` que:
  - Junta os resultados de `fn_grafico` (4ª coluna pode ser toda nula se não houver recorde) e `fn_perfil` num único conjunto de 4 séries alinhadas pelo `horario`;
  - Gera o eixo X sintético (dia-base) e preenche buracos de minuto com `0` (grade completa 00:00–23:59);
  - Produz os agregados dos cards (total do dia, variação %).
- Testar `transform.ts` unitariamente (Jest) com fixtures: dia completo, dia parcial (em andamento), alvo sem recorde, série sem dados.

