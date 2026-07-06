import { Nivel } from './sqlSafe';

export interface AppJsonData {
  datasourceUid?: string;
}

export interface SerieItem {
  serie_id: number;
  produto: string;
  jornada: string;
  escopo: string;
  status: string;
}

export interface StatsRow {
  dia_recorde: string | null;
  total_dia_recorde: number | null;
  minuto_recorde_ts: number | string | null;
  minuto_recorde_valor: number | null;
  mediana_3m: number | null;
  atualizado_em: number | string | null;
}

export interface AnaliseParams {
  nivel: Nivel;
  alvo: string;
  dia: string;
  diaComp: string;
}
