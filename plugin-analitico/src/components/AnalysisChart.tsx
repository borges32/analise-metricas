import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  AbsoluteTimeRange,
  DataFrame,
  FieldType,
  GrafanaTheme2,
  LoadingState,
  PanelData,
  TimeRange,
  dateTime,
  toDataFrame,
} from '@grafana/data';
import { PanelRenderer } from '@grafana/runtime';
import { Button, VizLegend, useStyles2 } from '@grafana/ui';
import { css } from '@emotion/css';

import { BASE_DAY_UTC, ChartSeries } from '../transform';
import { formatarBr } from '../dates';

interface Props {
  series: ChartSeries;
  diaAnalisado: string;
  diaComparativo: string;
  diaRecorde: string | null;
}

const ALTURA = 380;
const UM_DIA_MS = 24 * 60 * 60 * 1000;

interface SerieDef {
  key: string;
  label: string;
  color: string;
  values: number[];
  custom: Record<string, unknown>;
}

function construirDefs(props: Props): SerieDef[] {
  const { series } = props;
  const defs: SerieDef[] = [
    {
      key: 'analisado',
      label: `Analisado — ${formatarBr(props.diaAnalisado)}`,
      color: '#73BF69',
      values: series.analisado,
      custom: { lineWidth: 2, lineStyle: { fill: 'solid' } },
    },
    {
      key: 'comparativo',
      label: `Comparativo — ${formatarBr(props.diaComparativo)}`,
      color: '#FF9830',
      values: series.comparativo,
      custom: { lineWidth: 1, lineStyle: { fill: 'solid' } },
    },
  ];
  if (series.recorde) {
    defs.push({
      key: 'recorde',
      label: props.diaRecorde ? `Recorde — ${formatarBr(props.diaRecorde)}` : 'Recorde',
      color: '#5794F2',
      values: series.recorde,
      custom: { lineWidth: 1, lineStyle: { fill: 'dash', dash: [8, 6] } },
    });
  }
  defs.push({
    key: 'tipico',
    label: 'Dia típico (mediana 90d)',
    color: '#B877D9',
    values: series.tipico,
    custom: { lineWidth: 1, fillOpacity: 0, lineStyle: { fill: 'dot' } },
  });
  return defs;
}

function construirFrame(defs: SerieDef[], time: number[], isolado: string | null): DataFrame {
  const fields: Array<Record<string, unknown>> = [
    // unit 'time:HH:mm' formata eixo e tooltip só com a hora (o dia-base é sintético
    // — as curvas são de dias diferentes sobrepostas por horário).
    { name: 'Horário', type: FieldType.time, values: time, config: { unit: 'time:HH:mm' } },
  ];
  for (const def of defs) {
    const oculto = isolado !== null && isolado !== def.key;
    fields.push({
      name: def.label,
      type: FieldType.number,
      values: def.values,
      config: {
        color: { mode: 'fixed', fixedColor: def.color },
        custom: { ...def.custom, hideFrom: { viz: oculto, legend: false, tooltip: oculto } },
      },
    });
  }
  return toDataFrame({ fields });
}

function useLargura(): [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [largura, setLargura] = useState(800);
  useEffect(() => {
    if (!ref.current) {
      return;
    }
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) {
        setLargura(Math.floor(w));
      }
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, largura];
}

export function AnalysisChart(props: Props) {
  const styles = useStyles2(getStyles);
  const [ref, largura] = useLargura();
  const [isolado, setIsolado] = useState<string | null>(null);
  const [zoom, setZoom] = useState<TimeRange | null>(null);

  const defs = useMemo(() => construirDefs(props), [props]);
  const frame = useMemo(
    () => construirFrame(defs, props.series.time, isolado),
    [defs, props.series.time, isolado]
  );

  const baseRange: TimeRange = useMemo(() => {
    const from = dateTime(BASE_DAY_UTC);
    const to = dateTime(BASE_DAY_UTC + UM_DIA_MS);
    return { from, to, raw: { from, to } };
  }, []);

  const range = zoom ?? baseRange;
  const data: PanelData = { state: LoadingState.Done, series: [frame], timeRange: range };

  const items = defs.map((def) => ({
    label: def.label,
    color: def.color,
    yAxis: 1,
    disabled: isolado !== null && isolado !== def.key,
  }));

  const onLabelClick = (item: { label: string }) => {
    const def = defs.find((d) => d.label === item.label);
    if (def) {
      setIsolado((prev) => (prev === def.key ? null : def.key));
    }
  };

  return (
    <div ref={ref} className={styles.wrap}>
      {zoom && (
        <div className={styles.toolbar}>
          <Button size="sm" variant="secondary" icon="search-minus" onClick={() => setZoom(null)}>
            Resetar zoom
          </Button>
        </div>
      )}
      <PanelRenderer
        pluginId="timeseries"
        title=""
        width={largura}
        height={ALTURA}
        data={data}
        // O horario já é o horário LOCAL de negócio, codificado como UTC no dia-base;
        // renderizar em UTC evita o painel reconverter para o fuso do browser (-3h).
        timeZone="utc"
        onChangeTimeRange={(abs: AbsoluteTimeRange) => {
          const from = dateTime(abs.from);
          const to = dateTime(abs.to);
          setZoom({ from, to, raw: { from, to } });
        }}
        options={{
          legend: { showLegend: false },
          tooltip: { mode: 'multi', sort: 'none' },
        }}
        fieldConfig={{ defaults: { custom: { spanNulls: true } }, overrides: [] }}
      />
      <div className={styles.legend}>
        <VizLegend
          placement="bottom"
          displayMode={'list' as never}
          items={items as never}
          onLabelClick={onLabelClick as never}
        />
      </div>
    </div>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  wrap: css({ width: '100%', position: 'relative' }),
  toolbar: css({ display: 'flex', justifyContent: 'flex-end', marginBottom: theme.spacing(0.5) }),
  legend: css({ marginTop: theme.spacing(1) }),
});
