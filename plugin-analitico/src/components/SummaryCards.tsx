import React from 'react';
import { GrafanaTheme2 } from '@grafana/data';
import { useStyles2 } from '@grafana/ui';
import { css } from '@emotion/css';

import { Agregados } from '../transform';
import { StatsRow } from '../types';
import { formatarBr } from '../dates';

interface Props {
  stats: StatsRow | null;
  agregados: Agregados | null;
}

const fmtNum = (v: number | null | undefined): string =>
  v == null ? '—' : new Intl.NumberFormat('pt-BR').format(Math.round(v));

// Aceita epoch(ms) numérico, string numérica ou ISO.
const paraDate = (v: number | string | null): Date | null => {
  if (v == null || v === '') {
    return null;
  }
  const ms = typeof v === 'number' ? v : /^\d+$/.test(v) ? Number(v) : v;
  const d = new Date(ms as number | string);
  return isNaN(d.getTime()) ? null : d;
};

const fmtTs = (ts: number | string | null): string => {
  const d = paraDate(ts);
  return d
    ? new Intl.DateTimeFormat('pt-BR', {
        dateStyle: 'short',
        timeStyle: 'short',
        timeZone: 'America/Sao_Paulo',
      }).format(d)
    : '—';
};

function Card({ titulo, valor, sub }: { titulo: string; valor: string; sub?: string }) {
  const s = useStyles2(getStyles);
  return (
    <div className={s.card}>
      <div className={s.titulo}>{titulo}</div>
      <div className={s.valor}>{valor}</div>
      {sub && <div className={s.sub}>{sub}</div>}
    </div>
  );
}

export function SummaryCards({ stats, agregados }: Props) {
  const s = useStyles2(getStyles);
  const variacao =
    agregados?.variacaoPct == null
      ? '—'
      : `${agregados.variacaoPct >= 0 ? '+' : ''}${agregados.variacaoPct.toFixed(1)}% vs. comparativo`;

  return (
    <div className={s.grid}>
      <Card
        titulo="Dia recorde"
        valor={fmtNum(stats?.total_dia_recorde)}
        sub={stats?.dia_recorde ? formatarBr(stats.dia_recorde) : 'sem recorde'}
      />
      <Card
        titulo="Minuto recorde"
        valor={fmtNum(stats?.minuto_recorde_valor)}
        sub={fmtTs(stats?.minuto_recorde_ts ?? null)}
      />
      <Card titulo="Mediana 3m (vol/min)" valor={fmtNum(stats?.mediana_3m)} />
      <Card
        titulo="Total do dia analisado"
        valor={fmtNum(agregados?.totalAnalisado ?? null)}
        sub={variacao}
      />
    </div>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  grid: css({
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
    gap: theme.spacing(2),
    marginTop: theme.spacing(2),
  }),
  card: css({
    background: theme.colors.background.secondary,
    border: `1px solid ${theme.colors.border.weak}`,
    borderRadius: theme.shape.radius.default,
    padding: theme.spacing(2),
  }),
  titulo: css({ color: theme.colors.text.secondary, fontSize: theme.typography.bodySmall.fontSize }),
  valor: css({ fontSize: theme.typography.h2.fontSize, fontWeight: 600, marginTop: theme.spacing(0.5) }),
  sub: css({ color: theme.colors.text.secondary, fontSize: theme.typography.bodySmall.fontSize }),
});
