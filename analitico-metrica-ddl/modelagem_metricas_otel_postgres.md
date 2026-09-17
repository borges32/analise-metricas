# Modelagem Postgres — Análise de Métricas OpenTelemetry (Mimir → Postgres) — v2

**Cenário:** ~2.500 séries, granularidade de minuto, retenção 24 meses, análise dia vs. dia.
**Novo escopo (v2):** o usuário analisa um **APP** (soma de todas as séries do app) **ou uma série individual**. Ambos os níveis com gráfico, dia recorde, minuto recorde, mediana 3m e perfil mediano.

**Mudanças estruturais da v2:**
- `metrica_minuto` passa a reter **24 meses** (a análise por série exige minuto por 24 meses) — **removidas** `metrica_hora` e a degradação minuto→hora.
- Tabelas analíticas unificadas por **alvo** (`nivel in ('app','serie')`): `metrica_dia_alvo`, `perfil_mediano_alvo`, `stats_alvo`.
- Interface alimentada por **functions** (`fn_grafico`, `fn_perfil`) que fazem o branch app/série no banco.
- Recorde de minuto atualizado de forma **incremental** (nunca varre 24 meses).
- Rollup `metrica_minuto_app` **mantido** como fonte do nível app (gráfico em 1 range scan e jobs de 90 dias ~25× mais baratos).

> **DDL executável:** este documento é a *referência de modelagem*. O SQL que
> realmente roda vive num **arquivo único e idempotente**,
> `metricas-loader/src/loader/sql/schema.sql`, aplicado pelo próprio
> `metricas-loader` no boot (não existem migrations incrementais). Ao alterar o
> modelo aqui, altere lá — e vice-versa. Duas diferenças deliberadas no arquivo
> executável: todas as rotinas fixam `SET search_path = metricas, pg_catalog`
> (para funcionarem com qualquer usuário do banco) e existe
> `criar_particoes_intervalo(p_ini, p_fim)` para partições retroativas
> (backfill/import CSV).

**Requisitos:** PostgreSQL 14+ (recomendado 15/16). Fuso de negócio: `America/Sao_Paulo`. Timestamps em UTC (`timestamptz`).
**Infra:** ~250–280 GB em regime estável (24 meses de minuto por série); recomendado 64 GB+ de RAM e SSD/NVMe.

---

## 1. Schema e configurações iniciais

```sql
CREATE SCHEMA IF NOT EXISTS metricas;
SET search_path TO metricas;

CREATE TABLE IF NOT EXISTS config (
    chave text PRIMARY KEY,
    valor text NOT NULL
);
INSERT INTO config VALUES ('timezone_negocio', 'America/Sao_Paulo')
ON CONFLICT (chave) DO NOTHING;
```

---

## 2. Dimensão de séries

```sql
CREATE TABLE dim_serie (
    serie_id  int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    produto   text NOT NULL,
    app       text NOT NULL,
    jornada   text NOT NULL,
    escopo    text NOT NULL,
    status    text NOT NULL,
    criado_em timestamptz NOT NULL DEFAULT now(),
    UNIQUE (produto, app, jornada, escopo, status)
);

CREATE INDEX idx_dim_serie_app ON dim_serie (app);
```

---

## 3. Tabela fato detalhada — minuto (retenção: **24 meses**)

Partição **semanal** (~25 M linhas/partição; ~105 partições ativas). Retenção via `DROP` da partição.

```sql
CREATE TABLE metrica_minuto (
    ts       timestamptz NOT NULL,   -- minuto truncado, UTC
    serie_id int         NOT NULL,
    valor    bigint      NOT NULL,   -- incremento (delta) naquele minuto
    PRIMARY KEY (serie_id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX idx_metrica_minuto_ts_brin ON metrica_minuto USING brin (ts);
ALTER TABLE metrica_minuto SET (fillfactor = 100);
```

> A PK `(serie_id, ts)` atende o gráfico por série e o gráfico por app (N index scans, um por série do app). **Não** criar b-tree isolado em `ts`.

---

## 4. Rollup por app — minuto (retenção: 24 meses)

Fonte do nível **app** para gráfico e jobs. Alimentado em `processar_staging()`. Partição mensal.

```sql
CREATE TABLE metrica_minuto_app (
    ts    timestamptz NOT NULL,
    app   text        NOT NULL,
    valor bigint      NOT NULL,
    PRIMARY KEY (app, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX idx_metrica_minuto_app_ts_brin ON metrica_minuto_app USING brin (ts);
ALTER TABLE metrica_minuto_app SET (fillfactor = 100);
```

---

## 5. Tabelas analíticas por alvo (app ou série)

Convenção de alvo: `nivel = 'app'` → `alvo = nome do app`; `nivel = 'serie'` → `alvo = serie_id::text`.

```sql
-- Totais diários por alvo (base do dia recorde)
CREATE TABLE metrica_dia_alvo (
    nivel text   NOT NULL CHECK (nivel IN ('app','serie')),
    alvo  text   NOT NULL,
    dia   date   NOT NULL,   -- dia no fuso de negócio
    total bigint NOT NULL,
    PRIMARY KEY (nivel, alvo, dia)
);

-- Perfil mediano por minuto do dia (90 dias), útil × fim de semana
CREATE TABLE perfil_mediano_alvo (
    nivel    text    NOT NULL CHECK (nivel IN ('app','serie')),
    alvo     text    NOT NULL,
    tipo_dia text    NOT NULL CHECK (tipo_dia IN ('util','fds')),
    horario  time    NOT NULL,
    mediana  numeric NOT NULL,
    PRIMARY KEY (nivel, alvo, tipo_dia, horario)
);

-- Estatísticas materializadas por alvo
CREATE TABLE stats_alvo (
    nivel                text NOT NULL CHECK (nivel IN ('app','serie')),
    alvo                 text NOT NULL,
    dia_recorde          date,
    total_dia_recorde    bigint,
    minuto_recorde_ts    timestamptz,
    minuto_recorde_valor bigint,      -- mantido incrementalmente
    mediana_3m           numeric,
    atualizado_em        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (nivel, alvo)
);
```

---

## 6. Staging da carga (Python → COPY)

```sql
CREATE UNLOGGED TABLE staging_metrica (
    ts      timestamptz NOT NULL,
    produto text NOT NULL,
    app     text NOT NULL,
    jornada text NOT NULL,
    escopo  text NOT NULL,
    status  text NOT NULL,
    valor   bigint NOT NULL
);
```

---

## 7. Gestão de partições (criação e retenção)

```sql
CREATE OR REPLACE FUNCTION criar_particoes(p_semanas_futuras int DEFAULT 4,
                                           p_meses_futuros  int DEFAULT 2)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    d_ini date; d_fim date; nome text; i int;
BEGIN
    -- metrica_minuto: partições semanais
    FOR i IN 0..p_semanas_futuras LOOP
        d_ini := date_trunc('week', current_date)::date + (i * 7);
        d_fim := d_ini + 7;
        nome  := format('metrica_minuto_%s', to_char(d_ini, 'IYYY"w"IW'));
        IF to_regclass('metricas.' || nome) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE metricas.%I PARTITION OF metricas.metrica_minuto
                 FOR VALUES FROM (%L) TO (%L)', nome, d_ini, d_fim);
        END IF;
    END LOOP;

    -- metrica_minuto_app: partições mensais
    FOR i IN 0..p_meses_futuros LOOP
        d_ini := (date_trunc('month', current_date) + (i * interval '1 month'))::date;
        d_fim := (d_ini + interval '1 month')::date;
        nome  := format('metrica_minuto_app_%s', to_char(d_ini,'YYYY_MM'));
        IF to_regclass('metricas.' || nome) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE metricas.%I PARTITION OF metricas.metrica_minuto_app
                 FOR VALUES FROM (%L) TO (%L)', nome, d_ini, d_fim);
        END IF;
    END LOOP;
END $$;

-- Retenção: ambas as tabelas fato agora com 24 meses
CREATE OR REPLACE FUNCTION aplicar_retencao()
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    r record;
    lim_24m date := (date_trunc('month', current_date) - interval '24 months')::date;
BEGIN
    FOR r IN
        SELECT c.relname AS particao,
               (regexp_match(pg_get_expr(c.relpartbound, c.oid),
                             'TO \(''([\d-]+)'''))[1]::date AS fim_particao
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_namespace n ON n.oid = p.relnamespace
        WHERE n.nspname = 'metricas'
          AND p.relname IN ('metrica_minuto','metrica_minuto_app')
    LOOP
        IF r.fim_particao <= lim_24m THEN
            EXECUTE format('DROP TABLE metricas.%I', r.particao);
            RAISE NOTICE 'Partição % removida (retenção)', r.particao;
        END IF;
    END LOOP;

    -- Limpa linhas analíticas fora da janela
    DELETE FROM metrica_dia_alvo WHERE dia < lim_24m;
END $$;
```

---

## 8. Procedure de carga (staging → tabelas finais)

Idempotente. Alimenta dimensão, fato, rollup por app e totais diários **dos dois níveis**.

```sql
CREATE OR REPLACE PROCEDURE processar_staging()
LANGUAGE plpgsql AS $$
DECLARE tz text := (SELECT valor FROM config WHERE chave = 'timezone_negocio');
BEGIN
    -- 1. Novas séries → dimensão
    INSERT INTO dim_serie (produto, app, jornada, escopo, status)
    SELECT DISTINCT produto, app, jornada, escopo, status FROM staging_metrica
    ON CONFLICT (produto, app, jornada, escopo, status) DO NOTHING;

    -- 2. Fato detalhado
    INSERT INTO metrica_minuto (ts, serie_id, valor)
    SELECT s.ts, d.serie_id, sum(s.valor)
    FROM staging_metrica s
    JOIN dim_serie d USING (produto, app, jornada, escopo, status)
    GROUP BY s.ts, d.serie_id
    ON CONFLICT (serie_id, ts) DO UPDATE SET valor = EXCLUDED.valor;

    -- 3. Rollup por app
    -- Recalculado a partir de metrica_minuto (não da staging): a staging pode
    -- conter só um subconjunto das métricas (carga histórica por métrica), e
    -- somar só ela apagaria a contribuição de outros produtos do mesmo app.
    INSERT INTO metrica_minuto_app (ts, app, valor)
    SELECT m.ts, d.app, sum(m.valor)
    FROM metrica_minuto m
    JOIN dim_serie d USING (serie_id)
    JOIN (SELECT DISTINCT app, ts FROM staging_metrica) a
      ON a.app = d.app AND a.ts = m.ts
    WHERE m.ts >= (SELECT min(ts) FROM staging_metrica)
      AND m.ts <= (SELECT max(ts) FROM staging_metrica)
    GROUP BY m.ts, d.app
    ON CONFLICT (app, ts) DO UPDATE SET valor = EXCLUDED.valor;

    -- 4. Totais diários — nível APP (recalcula dias tocados)
    INSERT INTO metrica_dia_alvo (nivel, alvo, dia, total)
    SELECT 'app', m.app, (m.ts AT TIME ZONE tz)::date, sum(m.valor)
    FROM metrica_minuto_app m
    WHERE (m.ts AT TIME ZONE tz)::date IN
          (SELECT DISTINCT (ts AT TIME ZONE tz)::date FROM staging_metrica)
      AND m.app IN (SELECT DISTINCT app FROM staging_metrica)
    GROUP BY 2, 3
    ON CONFLICT (nivel, alvo, dia) DO UPDATE SET total = EXCLUDED.total;

    -- 5. Totais diários — nível SÉRIE (recalcula dias tocados)
    INSERT INTO metrica_dia_alvo (nivel, alvo, dia, total)
    SELECT 'serie', m.serie_id::text, (m.ts AT TIME ZONE tz)::date, sum(m.valor)
    FROM metrica_minuto m
    WHERE (m.ts AT TIME ZONE tz)::date IN
          (SELECT DISTINCT (ts AT TIME ZONE tz)::date FROM staging_metrica)
      AND m.serie_id IN (
          SELECT d.serie_id FROM staging_metrica s
          JOIN dim_serie d USING (produto, app, jornada, escopo, status))
    GROUP BY 2, 3
    ON CONFLICT (nivel, alvo, dia) DO UPDATE SET total = EXCLUDED.total;

    TRUNCATE staging_metrica;
END $$;
```

---

## 9. Jobs diários — perfil mediano e stats

```sql
-- ============================================================
-- Perfil mediano (90 dias) — nível app (lê rollup, barato)
-- e nível série (lê o detalhe, job pesado: rodar de madrugada)
-- ============================================================
CREATE OR REPLACE PROCEDURE atualizar_perfil_mediano()
LANGUAGE plpgsql AS $$
DECLARE
    tz  text := (SELECT valor FROM config WHERE chave = 'timezone_negocio');
    ini timestamptz;
BEGIN
    ini := (current_date - 90)::timestamp AT TIME ZONE tz;

    -- Nível APP (~13 M linhas do rollup)
    INSERT INTO perfil_mediano_alvo (nivel, alvo, tipo_dia, horario, mediana)
    SELECT 'app', app,
           CASE WHEN extract(isodow FROM (ts AT TIME ZONE tz)) IN (6,7)
                THEN 'fds' ELSE 'util' END,
           (ts AT TIME ZONE tz)::time,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY valor)
    FROM metrica_minuto_app
    WHERE ts >= ini
    GROUP BY 2, 3, 4
    ON CONFLICT (nivel, alvo, tipo_dia, horario)
    DO UPDATE SET mediana = EXCLUDED.mediana;

    -- Nível SÉRIE (~324 M linhas do detalhe; minutos de execução)
    INSERT INTO perfil_mediano_alvo (nivel, alvo, tipo_dia, horario, mediana)
    SELECT 'serie', serie_id::text,
           CASE WHEN extract(isodow FROM (ts AT TIME ZONE tz)) IN (6,7)
                THEN 'fds' ELSE 'util' END,
           (ts AT TIME ZONE tz)::time,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY valor)
    FROM metrica_minuto
    WHERE ts >= ini
    GROUP BY 2, 3, 4
    ON CONFLICT (nivel, alvo, tipo_dia, horario)
    DO UPDATE SET mediana = EXCLUDED.mediana;
END $$;

-- ============================================================
-- Stats por alvo:
--   dia recorde  → full em metrica_dia_alvo (tabela pequena)
--   minuto recorde → INCREMENTAL: varre só o dia informado e
--                    compara com o recorde armazenado
--   mediana 3m   → escalar, por nível (app via rollup, série via detalhe)
-- ============================================================
CREATE OR REPLACE PROCEDURE atualizar_stats_alvo(p_dia date DEFAULT current_date - 1)
LANGUAGE plpgsql AS $$
DECLARE
    tz  text := (SELECT valor FROM config WHERE chave = 'timezone_negocio');
    ini timestamptz := p_dia::timestamp AT TIME ZONE tz;
    fim timestamptz := (p_dia + 1)::timestamp AT TIME ZONE tz;
    ini90 timestamptz := (current_date - 90)::timestamp AT TIME ZONE tz;
BEGIN
    -- 1. Dia recorde (full scan em tabela pequena: ~1,9 M linhas)
    INSERT INTO stats_alvo (nivel, alvo, dia_recorde, total_dia_recorde)
    SELECT DISTINCT ON (nivel, alvo) nivel, alvo, dia, total
    FROM metrica_dia_alvo
    ORDER BY nivel, alvo, total DESC, dia
    ON CONFLICT (nivel, alvo) DO UPDATE
    SET dia_recorde       = EXCLUDED.dia_recorde,
        total_dia_recorde = EXCLUDED.total_dia_recorde,
        atualizado_em     = now();

    -- 2. Minuto recorde — incremental, nível APP
    WITH max_dia AS (
        SELECT app, ts, valor,
               row_number() OVER (PARTITION BY app ORDER BY valor DESC, ts) rn
        FROM metrica_minuto_app WHERE ts >= ini AND ts < fim
    )
    UPDATE stats_alvo s
    SET minuto_recorde_ts    = m.ts,
        minuto_recorde_valor = m.valor,
        atualizado_em        = now()
    FROM max_dia m
    WHERE m.rn = 1 AND s.nivel = 'app' AND s.alvo = m.app
      AND (s.minuto_recorde_valor IS NULL OR m.valor > s.minuto_recorde_valor);

    -- 3. Minuto recorde — incremental, nível SÉRIE
    WITH max_dia AS (
        SELECT serie_id, ts, valor,
               row_number() OVER (PARTITION BY serie_id ORDER BY valor DESC, ts) rn
        FROM metrica_minuto WHERE ts >= ini AND ts < fim
    )
    UPDATE stats_alvo s
    SET minuto_recorde_ts    = m.ts,
        minuto_recorde_valor = m.valor,
        atualizado_em        = now()
    FROM max_dia m
    WHERE m.rn = 1 AND s.nivel = 'serie' AND s.alvo = m.serie_id::text
      AND (s.minuto_recorde_valor IS NULL OR m.valor > s.minuto_recorde_valor);

    -- 4. Mediana escalar 3m — APP (rollup) e SÉRIE (detalhe)
    UPDATE stats_alvo s SET mediana_3m = x.md, atualizado_em = now()
    FROM (SELECT app, percentile_cont(0.5) WITHIN GROUP (ORDER BY valor) md
          FROM metrica_minuto_app WHERE ts >= ini90 GROUP BY app) x
    WHERE s.nivel = 'app' AND s.alvo = x.app;

    UPDATE stats_alvo s SET mediana_3m = x.md, atualizado_em = now()
    FROM (SELECT serie_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY valor) md
          FROM metrica_minuto WHERE ts >= ini90 GROUP BY serie_id) x
    WHERE s.nivel = 'serie' AND s.alvo = x.serie_id::text;
END $$;
```

> **Backfill do recorde de minuto:** ao carregar histórico, rode `CALL atualizar_stats_alvo(dia)` para cada dia carregado (ou um script que itere os dias). A partir daí a manutenção é incremental e nunca varre 24 meses.

### Agendamento com pg_cron (se disponível)

```sql
SELECT cron.schedule('particoes', '0 1 * * 0',  $$SELECT metricas.criar_particoes()$$);
SELECT cron.schedule('retencao',  '0 3 * * 0',  $$SELECT metricas.aplicar_retencao()$$);
SELECT cron.schedule('perfil',    '30 4 * * *', $$CALL metricas.atualizar_perfil_mediano()$$);
SELECT cron.schedule('stats',     '30 5 * * *', $$CALL metricas.atualizar_stats_alvo()$$);
```

---

## 10. Functions de interface (contrato do backend)

### 10.1 `fn_grafico` — gráfico dia × dia × dia recorde, para app ou série

```sql
CREATE OR REPLACE FUNCTION fn_grafico(p_nivel text, p_alvo text,
                                      p_dia_a date, p_dia_b date)
RETURNS TABLE (horario time,
               dia_analisado bigint,
               dia_comparativo bigint,
               dia_recorde bigint)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    tz    text := (SELECT valor FROM config WHERE chave = 'timezone_negocio');
    dia_r date := (SELECT s.dia_recorde FROM stats_alvo s
                   WHERE s.nivel = p_nivel AND s.alvo = p_alvo);
BEGIN
    IF p_nivel = 'app' THEN
        RETURN QUERY
        SELECT (m.ts AT TIME ZONE tz)::time,
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = p_dia_a),
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = p_dia_b),
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = dia_r)
        FROM metrica_minuto_app m
        WHERE m.app = p_alvo
          AND ( (m.ts >= p_dia_a::timestamp AT TIME ZONE tz AND m.ts < (p_dia_a+1)::timestamp AT TIME ZONE tz)
             OR (m.ts >= p_dia_b::timestamp AT TIME ZONE tz AND m.ts < (p_dia_b+1)::timestamp AT TIME ZONE tz)
             OR (dia_r IS NOT NULL AND
                 m.ts >= dia_r::timestamp AT TIME ZONE tz AND m.ts < (dia_r+1)::timestamp AT TIME ZONE tz) )
        GROUP BY 1 ORDER BY 1;

    ELSIF p_nivel = 'serie' THEN
        RETURN QUERY
        SELECT (m.ts AT TIME ZONE tz)::time,
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = p_dia_a),
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = p_dia_b),
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = dia_r)
        FROM metrica_minuto m
        WHERE m.serie_id = p_alvo::int
          AND ( (m.ts >= p_dia_a::timestamp AT TIME ZONE tz AND m.ts < (p_dia_a+1)::timestamp AT TIME ZONE tz)
             OR (m.ts >= p_dia_b::timestamp AT TIME ZONE tz AND m.ts < (p_dia_b+1)::timestamp AT TIME ZONE tz)
             OR (dia_r IS NOT NULL AND
                 m.ts >= dia_r::timestamp AT TIME ZONE tz AND m.ts < (dia_r+1)::timestamp AT TIME ZONE tz) )
        GROUP BY 1 ORDER BY 1;
    ELSE
        RAISE EXCEPTION 'nivel inválido: % (esperado app|serie)', p_nivel;
    END IF;
END $$;

-- Uso:
-- SELECT * FROM metricas.fn_grafico('app',   'pix',  '2026-07-03', '2026-06-26');
-- SELECT * FROM metricas.fn_grafico('serie', '1042', '2026-07-03', '2026-06-26');
```

### 10.2 `fn_perfil` — curva do dia típico compatível com o dia analisado

```sql
CREATE OR REPLACE FUNCTION fn_perfil(p_nivel text, p_alvo text, p_dia date)
RETURNS TABLE (horario time, mediana numeric)
LANGUAGE sql STABLE AS $$
    SELECT p.horario, p.mediana
    FROM metricas.perfil_mediano_alvo p
    WHERE p.nivel = p_nivel AND p.alvo = p_alvo
      AND p.tipo_dia = CASE WHEN extract(isodow FROM p_dia) IN (6,7)
                            THEN 'fds' ELSE 'util' END
    ORDER BY p.horario;
$$;
```

### 10.3 Cards de resumo

```sql
SELECT dia_recorde, total_dia_recorde, minuto_recorde_ts,
       minuto_recorde_valor, mediana_3m, atualizado_em
FROM metricas.stats_alvo
WHERE nivel = $1 AND alvo = $2;
```

### 10.4 Combos da UI (apps e séries do app)

```sql
-- Apps
SELECT DISTINCT app FROM metricas.dim_serie ORDER BY app;

-- Séries do app selecionado (alvo = serie_id::text)
SELECT serie_id, produto, jornada, escopo, status
FROM metricas.dim_serie
WHERE app = $1
ORDER BY jornada, escopo, status;
```

---

## 11. Sequência de instalação

Não é manual: basta apontar o `metricas-loader` para um banco **vazio**. Ele
aplica todo o DDL (seções 1 a 10 + partições iniciais) no boot, de forma
idempotente e protegida por advisory lock.

```bash
# Provisionar sem subir o worker (Job/initContainer):
MODO=schema POSTGRES_DSN=... python -m loader

# Ou aplicar o arquivo à mão:
psql "$POSTGRES_DSN" -v ON_ERROR_STOP=1 -f metricas-loader/src/loader/sql/schema.sql
```

Depois disso:

```sql
-- (Opcional) Agendar jobs — seção 9
-- Carga: COPY metricas.staging_metrica (FORMAT BINARY) → CALL metricas.processar_staging();
-- Backfill do recorde: CALL metricas.atualizar_stats_alvo(dia) para cada dia histórico
```

---

## 12. Configuração da instância e volumetria

| Parâmetro | Sugestão | Motivo |
|---|---|---|
| `shared_buffers` | 25% da RAM (16 GB em host de 64 GB) | Cache das partições quentes |
| `work_mem` | 128–256 MB (sessão dos jobs) | percentile/GROUP BY de 324 M linhas |
| `maintenance_work_mem` | 2 GB | Índices e vacuum |
| `max_wal_size` | 16 GB | Picos de COPY |
| `autovacuum_vacuum_scale_factor` | 0.02 nas partições fato | Vacuum frequente |
| `random_page_cost` | 1.1 (SSD) | Planos com index scan |

**Volumetria em regime estável (2.500 séries, 24 meses):**

| Tabela | Linhas | Tamanho aprox. |
|---|---|---|
| `metrica_minuto` | ~2,6 bi | ~150 GB heap + ~100 GB índice |
| `metrica_minuto_app` (~100 apps) | ~100 M | ~8 GB |
| `metrica_dia_alvo` | ~1,9 M | < 200 MB |
| `perfil_mediano_alvo` | ~7,5 M | < 1 GB |
| Demais | < 1 M | desprezível |
| **Total** | | **~250–280 GB** |
