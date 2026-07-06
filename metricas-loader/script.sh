#!/usr/bin/env bash
#
# Seed de métricas de exemplo para testar a solução (plugin + banco).
#
# Gera dados sintéticos por minuto para as 13 séries bradesco, de
# (data atual - N meses) até ontem (últimos dias fechados), com sazonalidade
# diária e de fim de semana. Escreve na staging e usa as MESMAS procedures do
# fluxo real (processar_staging / atualizar_stats_alvo / atualizar_perfil_mediano)
# — nada de INSERT direto nas tabelas finais.
#
# Uso:
#   ./script.sh                # 2 meses, container metricas-postgres
#   MESES=3 ./script.sh        # 3 meses
#   PG_CONTAINER=metricas-postgres PG_USER=metricas PG_DB=metricas ./script.sh
#
set -euo pipefail

MESES="${MESES:-2}"
PG_CONTAINER="${PG_CONTAINER:-metricas-postgres}"
PG_USER="${PG_USER:-metricas}"
PG_DB="${PG_DB:-metricas}"

if ! docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  echo "ERRO: container '$PG_CONTAINER' não está rodando. Suba a stack (docker compose up -d) antes." >&2
  exit 1
fi

echo ">> Seed de ${MESES} mês(es) de métricas em ${PG_CONTAINER}/${PG_DB} (pode levar 1-3 min)..."
INICIO=$(date +%s)

docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -q -v ON_ERROR_STOP=1 -v meses="$MESES" <<'EOSQL'
SET myseed.meses = :'meses';
SET client_min_messages = notice;

DO $seed$
DECLARE
  tz     text := (SELECT valor FROM metricas.config WHERE chave = 'timezone_negocio');
  meses  int  := current_setting('myseed.meses')::int;
  d_ini  date := (current_date - (meses || ' months')::interval)::date;
  d_fim  date := current_date - 1;   -- até ontem (último dia fechado)
  d      date;
  pw     date;
  pm     date;
  nome   text;
BEGIN
  RAISE NOTICE 'Período: % a % (fuso %)', d_ini, d_fim, tz;

  -- 1) Partições históricas (criar_particoes só cobre presente->futuro).
  pw := date_trunc('week', d_ini)::date;
  WHILE pw <= d_fim LOOP
    nome := format('metrica_minuto_%s', to_char(pw, 'IYYY"w"IW'));
    IF to_regclass('metricas.' || nome) IS NULL THEN
      EXECUTE format('CREATE TABLE metricas.%I PARTITION OF metricas.metrica_minuto
                      FOR VALUES FROM (%L) TO (%L)', nome, pw, pw + 7);
    END IF;
    pw := pw + 7;
  END LOOP;

  pm := date_trunc('month', d_ini)::date;
  WHILE pm <= d_fim LOOP
    nome := format('metrica_minuto_app_%s', to_char(pm, 'YYYY_MM'));
    IF to_regclass('metricas.' || nome) IS NULL THEN
      EXECUTE format('CREATE TABLE metricas.%I PARTITION OF metricas.metrica_minuto_app
                      FOR VALUES FROM (%L) TO (%L)', nome, pm, (pm + interval '1 month')::date);
    END IF;
    pm := (pm + interval '1 month')::date;
  END LOOP;

  -- 2) Carga dia a dia (minuto a minuto), via procedures do fluxo real.
  FOR d IN SELECT generate_series(d_ini, d_fim, interval '1 day')::date LOOP
    INSERT INTO metricas.staging_metrica (ts, produto, app, jornada, escopo, status, valor)
    SELECT
      g.ts,
      s.produto, s.app, s.jornada, s.escopo, s.status,
      GREATEST(0, round(
          s.peso
        -- sazonalidade diária: pico ao meio-dia (hora local), baixa de madrugada
        * (0.10 + 0.90 * power(sin(pi() *
             (extract(epoch FROM (g.ts AT TIME ZONE tz)
                                 - date_trunc('day', g.ts AT TIME ZONE tz)) / 3600.0) / 24.0), 2))
        -- fim de semana mais fraco
        * (CASE WHEN extract(isodow FROM (g.ts AT TIME ZONE tz)) IN (6, 7) THEN 0.60 ELSE 1.00 END)
        -- ruído
        * (0.80 + random() * 0.40)
      ))::bigint AS valor
    FROM (VALUES
      ('mobilepf',     'mobilepf',     'extrato', 'consulta',              'sucesso', 40),
      ('mobilepf',     'mobilepf',     'extrato', 'consulta',              'falha',    3),
      ('mobilepf',     'mobilepf',     'saldo',   'consulta',              'sucesso', 45),
      ('mobilepf',     'mobilepf',     'saldo',   'consulta',              'falha',    3),
      ('mobilepf',     'mobilepf',     'login',   'obter-token',           'sucesso', 60),
      ('mobilepf',     'mobilepf',     'login',   'obter-token',           'falha',    4),
      ('mobilepf',     'mobilepf',     'login',   'validar-token',         'sucesso', 55),
      ('mobilepf',     'mobilepf',     'login',   'validar-token',         'falha',    4),
      ('mobilepf',     'mobilepf',     'login',   'confirma-autenticacao', 'sucesso', 50),
      ('mobilepf',     'mobilepf',     'login',   'confirma-autenticacao', 'falha',    4),
      ('pix-mobilepf', 'pix-mobilepf', 'pagamento','efetivacao',           'sucesso', 35),
      ('pix-mobilepf', 'pix-mobilepf', 'pagamento','efetivacao',           'falha',    3),
      ('pix-mobilepf', 'pix-mobilepf', 'adesao',  'adesao',                'falha',    6)
    ) AS s(produto, app, jornada, escopo, status, peso)
    CROSS JOIN generate_series(
      d::timestamp AT TIME ZONE tz,
      ((d + 1)::timestamp AT TIME ZONE tz) - interval '1 minute',
      interval '1 minute'
    ) AS g(ts);

    CALL metricas.processar_staging();
    CALL metricas.atualizar_stats_alvo(d);

    IF (d - d_ini) % 10 = 0 OR d = d_fim THEN
      RAISE NOTICE '  carregado ate %', d;
    END IF;
  END LOOP;

  -- 3) Perfil mediano (dia típico) para as curvas do plugin.
  CALL metricas.atualizar_perfil_mediano();
  RAISE NOTICE 'Seed concluido.';
END
$seed$;

-- Resumo
\echo '--- Resumo ---'
SELECT 'dim_serie'          AS tabela, count(*) FROM metricas.dim_serie
UNION ALL SELECT 'metrica_minuto',      count(*) FROM metricas.metrica_minuto
UNION ALL SELECT 'metrica_minuto_app',  count(*) FROM metricas.metrica_minuto_app
UNION ALL SELECT 'metrica_dia_alvo',    count(*) FROM metricas.metrica_dia_alvo
UNION ALL SELECT 'perfil_mediano_alvo', count(*) FROM metricas.perfil_mediano_alvo
UNION ALL SELECT 'stats_alvo',          count(*) FROM metricas.stats_alvo;

\echo '--- Dias recorde por app ---'
SELECT nivel, alvo, dia_recorde, total_dia_recorde, minuto_recorde_valor
FROM metricas.stats_alvo WHERE nivel = 'app' ORDER BY alvo;
EOSQL

FIM=$(date +%s)
echo ">> Seed finalizado em $((FIM - INICIO))s."
echo ">> Teste no plugin: Grafana -> Análise de Métricas (app mobilepf / pix-mobilepf)."
