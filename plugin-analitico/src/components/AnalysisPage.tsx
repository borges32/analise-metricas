import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { GrafanaTheme2, SelectableValue } from '@grafana/data';
import { PluginPage, locationService } from '@grafana/runtime';
import {
  Alert,
  Button,
  Field,
  InlineField,
  LoadingPlaceholder,
  RadioButtonGroup,
  Select,
  useStyles2,
} from '@grafana/ui';
import { css } from '@emotion/css';

import { getApps, getGrafico, getPerfil, getSeries, getStats } from '../api';
import { transformar, TransformResult } from '../transform';
import { Nivel, validarAlvoApp } from '../sqlSafe';
import { SerieItem, StatsRow } from '../types';
import { diasPadrao, formatarBr } from '../dates';
import { CONFIG_URL } from '../constants';
import { SummaryCards } from './SummaryCards';
import { AnalysisChart } from './AnalysisChart';
import { DateFieldBr } from './DateFieldBr';

interface Props {
  datasourceUid?: string;
}

const NIVEL_OPCOES: Array<SelectableValue<Nivel>> = [
  { label: 'APP', value: 'app' },
  { label: 'Série', value: 'serie' },
];

const labelSerie = (s: SerieItem) => `${s.jornada} / ${s.escopo} / ${s.status}`;

export function AnalysisPage({ datasourceUid }: Props) {
  const styles = useStyles2(getStyles);
  const padrao = useMemo(() => diasPadrao(), []);
  // Estado inicial a partir da query string (URL compartilhável), sem react-router.
  const q = useMemo(() => {
    const obj = locationService.getSearchObject();
    const get = (k: string) => (obj[k] == null ? undefined : String(obj[k]));
    return { nivel: get('nivel'), alvo: get('alvo'), dia: get('dia'), dia_comp: get('dia_comp') };
  }, []);

  const [nivel, setNivel] = useState<Nivel>((q.nivel as Nivel) || 'app');
  const [app, setApp] = useState<string | undefined>(
    q.nivel === 'serie' ? undefined : q.alvo
  );
  const [serieId, setSerieId] = useState<string | undefined>(
    q.nivel === 'serie' ? q.alvo : undefined
  );
  const [dia, setDia] = useState<string>(q.dia || padrao.dia);
  const [diaComp, setDiaComp] = useState<string>(q.dia_comp || padrao.diaComp);

  const [apps, setApps] = useState<string[]>([]);
  const [series, setSeries] = useState<SerieItem[]>([]);
  const [carregandoMeta, setCarregandoMeta] = useState(false);

  const [loading, setLoading] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [resultado, setResultado] = useState<TransformResult | null>(null);
  const [stats, setStats] = useState<StatsRow | null>(null);
  const [appSelecionadoParaSerie, setAppSerie] = useState<string | undefined>(undefined);

  // ---- carrega lista de apps ----
  useEffect(() => {
    if (!datasourceUid) {
      return;
    }
    setCarregandoMeta(true);
    getApps(datasourceUid)
      .then(setApps)
      .catch((e) => setErro(String(e.message ?? e)))
      .finally(() => setCarregandoMeta(false));
  }, [datasourceUid]);

  // ---- carrega séries quando um app é escolhido (nível série) ----
  const carregarSeries = useCallback(
    (appNome: string) => {
      if (!datasourceUid) {
        return;
      }
      setAppSerie(appNome);
      getSeries(datasourceUid, appNome).then(setSeries).catch((e) => setErro(String(e.message ?? e)));
    },
    [datasourceUid]
  );

  const alvo = nivel === 'app' ? app : serieId;

  const analisar = useCallback(async () => {
    if (!datasourceUid || !alvo) {
      return;
    }
    setLoading(true);
    setErro(null);
    try {
      if (nivel === 'app') {
        validarAlvoApp(alvo, apps); // defesa: alvo deve existir na allowlist
      }
      // URL compartilhável
      locationService.partial({ nivel, alvo, dia, dia_comp: diaComp }, true);

      const [grafico, perfil, st] = await Promise.all([
        getGrafico(datasourceUid, nivel, alvo, dia, diaComp),
        getPerfil(datasourceUid, nivel, alvo, dia),
        getStats(datasourceUid, nivel, alvo),
      ]);
      setResultado(transformar(grafico, perfil));
      setStats(st);
    } catch (e: any) {
      setErro(String(e.message ?? e));
      setResultado(null);
    } finally {
      setLoading(false);
    }
  }, [datasourceUid, nivel, alvo, dia, diaComp, apps]);

  // auto-run no load quando a URL já traz alvo
  useEffect(() => {
    if (datasourceUid && alvo && apps.length && !resultado && !loading && !erro) {
      if (nivel === 'serie' && !series.length && appSelecionadoParaSerie === undefined) {
        return; // aguarda séries carregarem
      }
      analisar();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apps]);

  if (!datasourceUid) {
    return (
      <PluginPage>
        <Alert title="Datasource não configurado" severity="info">
          Configure o datasource PostgreSQL na{' '}
          <a className={styles.link} href={CONFIG_URL}>
            página de configuração do app
          </a>
          .
        </Alert>
      </PluginPage>
    );
  }

  const semDados =
    resultado &&
    resultado.agregados.totalAnalisado === 0 &&
    resultado.agregados.totalComparativo === 0 &&
    !resultado.temRecorde;

  return (
    <PluginPage>
      <div className={styles.barra}>
        <InlineField label="Nível">
          <RadioButtonGroup
            options={NIVEL_OPCOES}
            value={nivel}
            onChange={(v) => {
              setNivel(v!);
              setResultado(null);
            }}
          />
        </InlineField>

        <InlineField label="App">
          <Select
            width={26}
            placeholder={carregandoMeta ? 'carregando...' : 'selecione o app'}
            options={apps.map((a) => ({ label: a, value: a }))}
            value={app ? { label: app, value: app } : null}
            onChange={(v) => {
              setApp(v?.value);
              if (nivel === 'serie' && v?.value) {
                carregarSeries(v.value);
                setSerieId(undefined);
              }
            }}
          />
        </InlineField>

        {nivel === 'serie' && (
          <InlineField label="Série">
            <Select
              width={34}
              placeholder="selecione a série"
              options={series.map((s) => ({ label: labelSerie(s), value: String(s.serie_id) }))}
              value={
                serieId
                  ? {
                      label:
                        series.find((s) => String(s.serie_id) === serieId)
                          ? labelSerie(series.find((s) => String(s.serie_id) === serieId)!)
                          : serieId,
                      value: serieId,
                    }
                  : null
              }
              onChange={(v) => setSerieId(v?.value)}
            />
          </InlineField>
        )}

        <Field label="Dia analisado" className={styles.dateField}>
          <DateFieldBr value={dia} onChange={setDia} />
        </Field>
        <Field label="Dia comparativo" className={styles.dateField}>
          <DateFieldBr value={diaComp} onChange={setDiaComp} />
        </Field>

        <Button onClick={analisar} disabled={!alvo || loading} icon={loading ? 'spinner' : 'search'}>
          Analisar
        </Button>
      </div>

      {erro && (
        <Alert title="Erro ao consultar" severity="error" onRemove={() => setErro(null)}>
          {erro}
          <div>
            <Button variant="secondary" size="sm" onClick={analisar} className={styles.retry}>
              Tentar novamente
            </Button>
          </div>
        </Alert>
      )}

      {loading && <LoadingPlaceholder text="Carregando análise..." />}

      {!loading && resultado && !semDados && (
        <>
          {!resultado.temRecorde && (
            <Alert title="Sem dia recorde para este alvo" severity="info">
              A curva de recorde foi omitida (alvo novo ou stats ainda não materializadas).
            </Alert>
          )}
          <AnalysisChart
            series={resultado.series}
            diaAnalisado={dia}
            diaComparativo={diaComp}
            diaRecorde={stats?.dia_recorde ?? null}
          />
          <SummaryCards stats={stats} agregados={resultado.agregados} />
          {stats?.atualizado_em && (
            <div className={styles.rodape}>
              Estatísticas atualizadas em {new Date(stats.atualizado_em).toLocaleString('pt-BR')}
            </div>
          )}
        </>
      )}

      {!loading && semDados && (
        <Alert title="Sem volumetria no período" severity="info">
          Não há dados para {formatarBr(dia)} nem {formatarBr(diaComp)} neste alvo.
        </Alert>
      )}
    </PluginPage>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  barra: css({
    display: 'flex',
    flexWrap: 'wrap',
    alignItems: 'flex-end',
    gap: theme.spacing(1),
    marginBottom: theme.spacing(2),
  }),
  dateField: css({ marginBottom: 0 }),
  rodape: css({
    marginTop: theme.spacing(1),
    color: theme.colors.text.secondary,
    fontSize: theme.typography.bodySmall.fontSize,
  }),
  retry: css({ marginTop: theme.spacing(1) }),
  link: css({ color: theme.colors.text.link, textDecoration: 'underline' }),
});
