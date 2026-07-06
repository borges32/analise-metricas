/**
 * Conversão dos resultados das functions (fn_grafico + fn_perfil) num conjunto
 * de 4 curvas alinhadas pelo horário do dia, com grade completa 00:00–23:59.
 *
 * Módulo PURO (sem dependências do Grafana) para ser testável em unidade.
 */

// Dia-base sintético para o eixo X: todas as curvas são projetadas neste dia
// para poderem se sobrepor por horário (o dia real vai na legenda).
export const BASE_DAY_UTC = Date.UTC(1970, 0, 2, 0, 0, 0); // 1970-01-02T00:00:00Z
const MINUTOS_DIA = 24 * 60;

export interface GraficoRow {
  horario: string;
  dia_analisado: number | null;
  dia_comparativo: number | null;
  dia_recorde: number | null;
}

export interface PerfilRow {
  horario: string;
  mediana: number | null;
}

export interface ChartSeries {
  time: number[];
  analisado: number[];
  comparativo: number[];
  recorde: number[] | null; // null quando o alvo não tem dia recorde
  tipico: number[];
}

export interface Agregados {
  totalAnalisado: number;
  totalComparativo: number;
  variacaoPct: number | null; // null quando comparativo == 0
}

export interface TransformResult {
  series: ChartSeries;
  agregados: Agregados;
  temRecorde: boolean;
}

/** Converte "HH:MM[:SS]" em índice de minuto do dia (0–1439), ou -1 se inválido. */
export function horarioParaMinuto(horario: string): number {
  const m = /^(\d{2}):(\d{2})(?::(\d{2}))?/.exec(String(horario).trim());
  if (!m) {
    return -1;
  }
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (h < 0 || h > 23 || min < 0 || min > 59) {
    return -1;
  }
  return h * 60 + min;
}

/** Timestamp sintético (ms) do minuto `idx` no dia-base. */
export function minutoParaTimestamp(idx: number): number {
  return BASE_DAY_UTC + idx * 60_000;
}

function novaGrade(): number[] {
  return new Array<number>(MINUTOS_DIA).fill(0);
}

export function transformar(grafico: GraficoRow[], perfil: PerfilRow[]): TransformResult {
  const time = novaGrade().map((_, i) => minutoParaTimestamp(i));
  const analisado = novaGrade();
  const comparativo = novaGrade();
  const recorde = novaGrade();
  const tipico = novaGrade();
  let temRecorde = false;

  for (const row of grafico) {
    const idx = horarioParaMinuto(row.horario);
    if (idx < 0) {
      continue;
    }
    analisado[idx] = row.dia_analisado ?? 0;
    comparativo[idx] = row.dia_comparativo ?? 0;
    if (row.dia_recorde != null) {
      temRecorde = true;
      recorde[idx] = row.dia_recorde;
    }
  }

  for (const row of perfil) {
    const idx = horarioParaMinuto(row.horario);
    if (idx < 0) {
      continue;
    }
    tipico[idx] = row.mediana ?? 0;
  }

  const totalAnalisado = analisado.reduce((a, b) => a + b, 0);
  const totalComparativo = comparativo.reduce((a, b) => a + b, 0);
  const variacaoPct =
    totalComparativo > 0
      ? ((totalAnalisado - totalComparativo) / totalComparativo) * 100
      : null;

  return {
    series: {
      time,
      analisado,
      comparativo,
      recorde: temRecorde ? recorde : null,
      tipico,
    },
    agregados: { totalAnalisado, totalComparativo, variacaoPct },
    temRecorde,
  };
}
