-- ============================================================================
-- SCHEMA ÚNICO E COMPLETO do banco analítico (`metricas`).
--
-- Fonte de verdade do DDL. Não existem migrations incrementais: este arquivo
-- descreve o estado final desejado e é 100% IDEMPOTENTE — pode ser reaplicado
-- em banco vazio ou já provisionado, quantas vezes for necessário.
--
-- Aplicado automaticamente pelo `metricas-loader` no boot
-- (`loader.schema.aplicar_schema`, protegido por advisory lock).
-- Também pode ser aplicado manualmente:
--     psql "$POSTGRES_DSN" -v ON_ERROR_STOP=1 -f src/loader/sql/schema.sql
--
-- REGRA DE EVOLUÇÃO (enquanto o produto não está em produção): alterações de
-- modelo são editadas AQUI, no lugar onde o objeto é definido. Nada de arquivos
-- `NNN_alter_*.sql`. Se a mudança não for compatível com bases existentes de
-- desenvolvimento, recrie a base (`docker compose down -v`).
--
-- Documento de referência: analitico-metrica-ddl/modelagem_metricas_otel_postgres.md
-- ============================================================================

-- 1. Schema e configurações iniciais ----------------------------------------
CREATE SCHEMA IF NOT EXISTS metricas;

CREATE TABLE IF NOT EXISTS metricas.config (
    chave text PRIMARY KEY,
    valor text NOT NULL
);

INSERT INTO metricas.config (chave, valor)
VALUES ('timezone_negocio', 'America/Sao_Paulo'),
       ('schema_versao', '2')
ON CONFLICT (chave) DO NOTHING;

-- Mantém a versão do schema em dia mesmo em bases já provisionadas.
UPDATE metricas.config SET valor = '2' WHERE chave = 'schema_versao' AND valor <> '2';

-- 2. Dimensão de séries ------------------------------------------------------
CREATE TABLE IF NOT EXISTS metricas.dim_serie (
    serie_id  int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    produto   text NOT NULL,
    app       text NOT NULL,
    jornada   text NOT NULL,
    escopo    text NOT NULL,
    status    text NOT NULL,
    criado_em timestamptz NOT NULL DEFAULT now(),
    UNIQUE (produto, app, jornada, escopo, status)
);

CREATE INDEX IF NOT EXISTS idx_dim_serie_app ON metricas.dim_serie (app);

-- 3. Fato detalhado — minuto (retenção 24 meses, partição semanal) -----------
CREATE TABLE IF NOT EXISTS metricas.metrica_minuto (
    ts       timestamptz NOT NULL,   -- minuto truncado, UTC
    serie_id int         NOT NULL,
    valor    bigint      NOT NULL,   -- incremento (delta) naquele minuto
    PRIMARY KEY (serie_id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS idx_metrica_minuto_ts_brin
    ON metricas.metrica_minuto USING brin (ts);
-- NOTA: o doc-fonte traz `ALTER TABLE ... SET (fillfactor = 100)`, mas o Postgres
-- rejeita storage params na tabela particionada-pai. fillfactor=100 já é o default
-- (tabelas append-only), então a linha foi omitida sem mudança de comportamento.

-- 4. Rollup por app — minuto (retenção 24 meses, partição mensal) ------------
CREATE TABLE IF NOT EXISTS metricas.metrica_minuto_app (
    ts    timestamptz NOT NULL,
    app   text        NOT NULL,
    valor bigint      NOT NULL,
    PRIMARY KEY (app, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS idx_metrica_minuto_app_ts_brin
    ON metricas.metrica_minuto_app USING brin (ts);
-- fillfactor=100 (default) — ver nota acima; omitido por ser partitioned-parent.

-- 5. Tabelas analíticas por alvo (app ou série) ------------------------------
CREATE TABLE IF NOT EXISTS metricas.metrica_dia_alvo (
    nivel text   NOT NULL CHECK (nivel IN ('app','serie')),
    alvo  text   NOT NULL,
    dia   date   NOT NULL,   -- dia no fuso de negócio
    total bigint NOT NULL,
    PRIMARY KEY (nivel, alvo, dia)
);

CREATE TABLE IF NOT EXISTS metricas.perfil_mediano_alvo (
    nivel    text    NOT NULL CHECK (nivel IN ('app','serie')),
    alvo     text    NOT NULL,
    tipo_dia text    NOT NULL CHECK (tipo_dia IN ('util','fds')),
    horario  time    NOT NULL,
    mediana  numeric NOT NULL,
    PRIMARY KEY (nivel, alvo, tipo_dia, horario)
);

CREATE TABLE IF NOT EXISTS metricas.stats_alvo (
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

-- 6. Staging da carga (Python -> COPY) ---------------------------------------
CREATE UNLOGGED TABLE IF NOT EXISTS metricas.staging_metrica (
    ts      timestamptz NOT NULL,
    produto text NOT NULL,
    app     text NOT NULL,
    jornada text NOT NULL,
    escopo  text NOT NULL,
    status  text NOT NULL,
    valor   bigint NOT NULL
);

-- 7. Gestão de partições -----------------------------------------------------
-- Todas as rotinas fixam `search_path` para não dependerem da sessão do chamador
-- (o loader conecta com o search_path default e chama `metricas.<rotina>()`).
CREATE OR REPLACE FUNCTION metricas.criar_particoes(p_semanas_futuras int DEFAULT 4,
                                                    p_meses_futuros  int DEFAULT 2)
RETURNS void LANGUAGE plpgsql SET search_path = metricas, pg_catalog AS $$
DECLARE
    d_ini date; d_fim date; nome text; i int;
BEGIN
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

-- Partições RETROATIVAS cobrindo [p_ini, p_fim] — usada por backfill/import CSV.
-- Mesma convenção de nomes de `criar_particoes()`: semanal ISO em metrica_minuto,
-- mensal em metrica_minuto_app.
CREATE OR REPLACE FUNCTION metricas.criar_particoes_intervalo(p_ini date, p_fim date)
RETURNS void LANGUAGE plpgsql SET search_path = metricas, pg_catalog AS $$
DECLARE
    d date; nome text;
BEGIN
    IF p_ini IS NULL OR p_fim IS NULL OR p_fim < p_ini THEN
        RAISE EXCEPTION 'intervalo inválido: % a %', p_ini, p_fim;
    END IF;

    d := date_trunc('week', p_ini::timestamp)::date;
    WHILE d <= p_fim LOOP
        nome := format('metrica_minuto_%s', to_char(d, 'IYYY"w"IW'));
        IF to_regclass('metricas.' || nome) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE metricas.%I PARTITION OF metricas.metrica_minuto
                 FOR VALUES FROM (%L) TO (%L)', nome, d, d + 7);
        END IF;
        d := d + 7;
    END LOOP;

    d := date_trunc('month', p_ini::timestamp)::date;
    WHILE d <= p_fim LOOP
        nome := format('metrica_minuto_app_%s', to_char(d, 'YYYY_MM'));
        IF to_regclass('metricas.' || nome) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE metricas.%I PARTITION OF metricas.metrica_minuto_app
                 FOR VALUES FROM (%L) TO (%L)', nome, d, (d + interval '1 month')::date);
        END IF;
        d := (d + interval '1 month')::date;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION metricas.aplicar_retencao()
RETURNS void LANGUAGE plpgsql SET search_path = metricas, pg_catalog AS $$
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

    DELETE FROM metrica_dia_alvo WHERE dia < lim_24m;
END $$;

-- 8. Procedure de carga (staging -> tabelas finais) --------------------------
CREATE OR REPLACE PROCEDURE metricas.processar_staging()
LANGUAGE plpgsql SET search_path = metricas, pg_catalog AS $$
DECLARE tz text := (SELECT valor FROM config WHERE chave = 'timezone_negocio');
BEGIN
    INSERT INTO dim_serie (produto, app, jornada, escopo, status)
    SELECT DISTINCT produto, app, jornada, escopo, status FROM staging_metrica
    ON CONFLICT (produto, app, jornada, escopo, status) DO NOTHING;

    INSERT INTO metrica_minuto (ts, serie_id, valor)
    SELECT s.ts, d.serie_id, sum(s.valor)
    FROM staging_metrica s
    JOIN dim_serie d USING (produto, app, jornada, escopo, status)
    GROUP BY s.ts, d.serie_id
    ON CONFLICT (serie_id, ts) DO UPDATE SET valor = EXCLUDED.valor;

    -- Rollup por app: recalculado a partir de `metrica_minuto` (e NÃO da staging).
    -- A staging pode conter só um subconjunto das métricas — é o caso da carga
    -- histórica por métrica (`loader.historico`) e de uma métrica inativa no
    -- worker. Somar só a staging sobrescreveria a contribuição dos demais
    -- produtos que compartilham o mesmo `app`, corrompendo o rollup.
    INSERT INTO metrica_minuto_app (ts, app, valor)
    SELECT m.ts, d.app, sum(m.valor)
    FROM metrica_minuto m
    JOIN dim_serie d USING (serie_id)
    JOIN (SELECT DISTINCT app, ts FROM staging_metrica) a
      ON a.app = d.app AND a.ts = m.ts
    WHERE m.ts >= (SELECT min(ts) FROM staging_metrica)   -- ajuda o partition pruning
      AND m.ts <= (SELECT max(ts) FROM staging_metrica)
    GROUP BY m.ts, d.app
    ON CONFLICT (app, ts) DO UPDATE SET valor = EXCLUDED.valor;

    INSERT INTO metrica_dia_alvo (nivel, alvo, dia, total)
    SELECT 'app', m.app, (m.ts AT TIME ZONE tz)::date, sum(m.valor)
    FROM metrica_minuto_app m
    WHERE (m.ts AT TIME ZONE tz)::date IN
          (SELECT DISTINCT (ts AT TIME ZONE tz)::date FROM staging_metrica)
      AND m.app IN (SELECT DISTINCT app FROM staging_metrica)
    GROUP BY 2, 3
    ON CONFLICT (nivel, alvo, dia) DO UPDATE SET total = EXCLUDED.total;

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

-- 9. Jobs diários — perfil mediano e stats -----------------------------------
CREATE OR REPLACE PROCEDURE metricas.atualizar_perfil_mediano()
LANGUAGE plpgsql SET search_path = metricas, pg_catalog AS $$
DECLARE
    tz  text := (SELECT valor FROM config WHERE chave = 'timezone_negocio');
    ini timestamptz;
BEGIN
    ini := (current_date - 90)::timestamp AT TIME ZONE tz;

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

CREATE OR REPLACE PROCEDURE metricas.atualizar_stats_alvo(p_dia date DEFAULT current_date - 1)
LANGUAGE plpgsql SET search_path = metricas, pg_catalog AS $$
DECLARE
    tz  text := (SELECT valor FROM config WHERE chave = 'timezone_negocio');
    ini timestamptz := p_dia::timestamp AT TIME ZONE tz;
    fim timestamptz := (p_dia + 1)::timestamp AT TIME ZONE tz;
    ini90 timestamptz := (current_date - 90)::timestamp AT TIME ZONE tz;
BEGIN
    INSERT INTO stats_alvo (nivel, alvo, dia_recorde, total_dia_recorde)
    SELECT DISTINCT ON (nivel, alvo) nivel, alvo, dia, total
    FROM metrica_dia_alvo
    ORDER BY nivel, alvo, total DESC, dia
    ON CONFLICT (nivel, alvo) DO UPDATE
    SET dia_recorde       = EXCLUDED.dia_recorde,
        total_dia_recorde = EXCLUDED.total_dia_recorde,
        atualizado_em     = now();

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

    UPDATE stats_alvo s SET mediana_3m = x.md, atualizado_em = now()
    FROM (SELECT app, percentile_cont(0.5) WITHIN GROUP (ORDER BY valor) md
          FROM metrica_minuto_app WHERE ts >= ini90 GROUP BY app) x
    WHERE s.nivel = 'app' AND s.alvo = x.app;

    UPDATE stats_alvo s SET mediana_3m = x.md, atualizado_em = now()
    FROM (SELECT serie_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY valor) md
          FROM metrica_minuto WHERE ts >= ini90 GROUP BY serie_id) x
    WHERE s.nivel = 'serie' AND s.alvo = x.serie_id::text;
END $$;

-- 10. Functions de interface (contrato do plugin-analitico) ------------------
CREATE OR REPLACE FUNCTION metricas.fn_grafico(p_nivel text, p_alvo text,
                                               p_dia_a date, p_dia_b date)
RETURNS TABLE (horario time,
               dia_analisado bigint,
               dia_comparativo bigint,
               dia_recorde bigint)
LANGUAGE plpgsql STABLE SET search_path = metricas, pg_catalog AS $$
DECLARE
    tz    text := (SELECT valor FROM config WHERE chave = 'timezone_negocio');
    dia_r date := (SELECT s.dia_recorde FROM stats_alvo s
                   WHERE s.nivel = p_nivel AND s.alvo = p_alvo);
BEGIN
    IF p_nivel = 'app' THEN
        RETURN QUERY
        -- NOTA: doc-fonte usa sum() direto, mas sum(bigint)=numeric quebra o tipo de
        -- retorno bigint da function; cast ::bigint adicionado sem mudar o valor.
        SELECT (m.ts AT TIME ZONE tz)::time,
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = p_dia_a)::bigint,
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = p_dia_b)::bigint,
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = dia_r)::bigint
        FROM metrica_minuto_app m
        WHERE m.app = p_alvo
          AND ( (m.ts >= p_dia_a::timestamp AT TIME ZONE tz AND m.ts < (p_dia_a+1)::timestamp AT TIME ZONE tz)
             OR (m.ts >= p_dia_b::timestamp AT TIME ZONE tz AND m.ts < (p_dia_b+1)::timestamp AT TIME ZONE tz)
             OR (dia_r IS NOT NULL AND
                 m.ts >= dia_r::timestamp AT TIME ZONE tz AND m.ts < (dia_r+1)::timestamp AT TIME ZONE tz) )
        GROUP BY 1 ORDER BY 1;

    ELSIF p_nivel = 'serie' THEN
        RETURN QUERY
        -- NOTA: doc-fonte usa sum() direto, mas sum(bigint)=numeric quebra o tipo de
        -- retorno bigint da function; cast ::bigint adicionado sem mudar o valor.
        SELECT (m.ts AT TIME ZONE tz)::time,
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = p_dia_a)::bigint,
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = p_dia_b)::bigint,
               sum(m.valor) FILTER (WHERE (m.ts AT TIME ZONE tz)::date = dia_r)::bigint
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

CREATE OR REPLACE FUNCTION metricas.fn_perfil(p_nivel text, p_alvo text, p_dia date)
RETURNS TABLE (horario time, mediana numeric)
LANGUAGE sql STABLE SET search_path = metricas, pg_catalog AS $$
    SELECT p.horario, p.mediana
    FROM metricas.perfil_mediano_alvo p
    WHERE p.nivel = p_nivel AND p.alvo = p_alvo
      AND p.tipo_dia = CASE WHEN extract(isodow FROM p_dia) IN (6,7)
                            THEN 'fds' ELSE 'util' END
    ORDER BY p.horario;
$$;

-- 11. Partições iniciais (idempotente) ---------------------------------------
SELECT metricas.criar_particoes(4, 2);
