/** Utilitários de data no fuso de negócio (America/Sao_Paulo). */

const TZ = 'America/Sao_Paulo';

/** Data de hoje (YYYY-MM-DD) no fuso de negócio. */
export function hojeNegocio(): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: TZ }).format(new Date());
}

/** Soma (ou subtrai) dias a uma data YYYY-MM-DD. */
export function addDias(ymd: string, n: number): string {
  const [y, m, d] = ymd.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + n);
  return dt.toISOString().slice(0, 10);
}

/** Formata YYYY-MM-DD como DD/MM/YYYY para exibição. */
export function formatarBr(ymd: string): string {
  const [y, m, d] = ymd.split('-');
  return `${d}/${m}/${y}`;
}

/** Converte YYYY-MM-DD -> Date (meia-noite LOCAL) para o DatePicker. */
export function ymdParaDate(ymd: string): Date {
  const [y, m, d] = ymd.split('-').map(Number);
  return new Date(y, m - 1, d);
}

/** Converte Date -> YYYY-MM-DD (usa componentes LOCAIS, alinhado ao DatePicker). */
export function dateParaYmd(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

/** Defaults: dia analisado = ontem; comparativo = mesmo dia da semana anterior. */
export function diasPadrao(): { dia: string; diaComp: string } {
  const dia = addDias(hojeNegocio(), -1);
  return { dia, diaComp: addDias(dia, -7) };
}
