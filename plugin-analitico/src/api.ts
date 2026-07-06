/**
 * Camada de acesso a dados: executa SQL EXCLUSIVAMENTE via datasource
 * PostgreSQL do Grafana (getDataSourceSrv). Toda SQL vem de sqlSafe.ts.
 */

import { DataFrame, Field, getDefaultTimeRange } from '@grafana/data';
import { getDataSourceSrv } from '@grafana/runtime';
import { lastValueFrom } from 'rxjs';

import {
  Nivel,
  SQL_APPS,
  sqlGrafico,
  sqlPerfil,
  sqlSeries,
  sqlStats,
} from './sqlSafe';
import { GraficoRow, PerfilRow } from './transform';
import { SerieItem, StatsRow } from './types';

let _seq = 0;

function valoresDoField(field: Field): unknown[] {
  const v = field.values as unknown as { toArray?: () => unknown[] } | unknown[];
  if (Array.isArray(v)) {
    return v;
  }
  return v.toArray ? v.toArray() : (v as unknown[]);
}

/** Converte um DataFrame em array de objetos { coluna: valor }. */
export function frameParaLinhas(frame: DataFrame): Record<string, unknown>[] {
  const linhas: Record<string, unknown>[] = [];
  const colunas = frame.fields.map((f) => ({ nome: f.name, valores: valoresDoField(f) }));
  for (let i = 0; i < frame.length; i++) {
    const linha: Record<string, unknown> = {};
    for (const c of colunas) {
      linha[c.nome] = c.valores[i];
    }
    linhas.push(linha);
  }
  return linhas;
}

async function executar(uid: string, rawSql: string): Promise<DataFrame[]> {
  const ds = await getDataSourceSrv().get(uid);
  const req = {
    requestId: `analitico-${_seq++}`,
    interval: '1m',
    intervalMs: 60_000,
    range: getDefaultTimeRange(),
    scopedVars: {},
    timezone: 'browser',
    app: 'plugin-analitico',
    startTime: Date.now(),
    targets: [{ refId: 'A', rawSql, format: 'table', datasource: { uid } }],
  } as any;
  const resp = await lastValueFrom(ds.query(req));
  if (resp.error) {
    throw new Error(resp.error.message || 'erro ao consultar o datasource');
  }
  return (resp.data as DataFrame[]) ?? [];
}

async function linhas(uid: string, rawSql: string): Promise<Record<string, unknown>[]> {
  const frames = await executar(uid, rawSql);
  return frames.length ? frameParaLinhas(frames[0]) : [];
}

const num = (v: unknown): number | null =>
  v == null || v === '' ? null : Number(v);

// Colunas `date`/`timestamp` chegam do datasource como epoch em ms (número).
// Normaliza para YYYY-MM-DD (usa UTC: uma `date` vem como meia-noite UTC).
function toYmd(v: unknown): string | null {
  if (v == null || v === '') {
    return null;
  }
  if (typeof v === 'number') {
    return new Date(v).toISOString().slice(0, 10);
  }
  const s = String(v);
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) {
    return s.slice(0, 10);
  }
  if (/^\d+$/.test(s)) {
    return new Date(Number(s)).toISOString().slice(0, 10);
  }
  const d = new Date(s);
  return isNaN(d.getTime()) ? s : d.toISOString().slice(0, 10);
}

export async function getApps(uid: string): Promise<string[]> {
  const rows = await linhas(uid, SQL_APPS);
  return rows.map((r) => String(r.app)).filter(Boolean);
}

export async function getSeries(uid: string, app: string): Promise<SerieItem[]> {
  const rows = await linhas(uid, sqlSeries(app));
  return rows.map((r) => ({
    serie_id: Number(r.serie_id),
    produto: String(r.produto ?? ''),
    jornada: String(r.jornada ?? ''),
    escopo: String(r.escopo ?? ''),
    status: String(r.status ?? ''),
  }));
}

export async function getGrafico(
  uid: string,
  nivel: Nivel,
  alvo: string,
  diaA: string,
  diaB: string
): Promise<GraficoRow[]> {
  const rows = await linhas(uid, sqlGrafico(nivel, alvo, diaA, diaB));
  return rows.map((r) => ({
    horario: String(r.horario ?? ''),
    dia_analisado: num(r.dia_analisado),
    dia_comparativo: num(r.dia_comparativo),
    dia_recorde: num(r.dia_recorde),
  }));
}

export async function getPerfil(
  uid: string,
  nivel: Nivel,
  alvo: string,
  dia: string
): Promise<PerfilRow[]> {
  const rows = await linhas(uid, sqlPerfil(nivel, alvo, dia));
  return rows.map((r) => ({ horario: String(r.horario ?? ''), mediana: num(r.mediana) }));
}

export async function getStats(
  uid: string,
  nivel: Nivel,
  alvo: string
): Promise<StatsRow | null> {
  const rows = await linhas(uid, sqlStats(nivel, alvo));
  if (!rows.length) {
    return null;
  }
  const r = rows[0];
  return {
    dia_recorde: toYmd(r.dia_recorde),
    total_dia_recorde: num(r.total_dia_recorde),
    // ts/atualizado_em podem vir como epoch(ms) ou string ISO — mantém cru e formata na UI.
    minuto_recorde_ts: (r.minuto_recorde_ts as number | string) ?? null,
    minuto_recorde_valor: num(r.minuto_recorde_valor),
    mediana_3m: num(r.mediana_3m),
    atualizado_em: (r.atualizado_em as number | string) ?? null,
  };
}
