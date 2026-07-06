/**
 * Construção segura de SQL para o datasource PostgreSQL do Grafana.
 *
 * O `rawSql` do datasource NÃO suporta bind parameters, então todo valor
 * interpolado é validado por allowlist e/ou escapado antes de entrar na string.
 * Este módulo é a ÚNICA fonte de SQL do plugin (contrato do README do projeto).
 */

export type Nivel = 'app' | 'serie';

const NIVEIS: readonly Nivel[] = ['app', 'serie'];
const RE_DATA = /^(\d{4})-(\d{2})-(\d{2})$/;

/** Escapa e envelopa um texto como literal SQL seguro (`'ab''c'`). */
export function quoteLiteral(valor: string): string {
  return `'${String(valor).replace(/'/g, "''")}'`;
}

/** Valida o nível contra a allowlist. */
export function validarNivel(nivel: string): Nivel {
  if ((NIVEIS as readonly string[]).includes(nivel)) {
    return nivel as Nivel;
  }
  throw new Error(`nível inválido: ${nivel} (esperado app|serie)`);
}

/**
 * Parse estrito de data `YYYY-MM-DD`. Rejeita datas impossíveis e RE-SERIALIZA
 * a partir do objeto Date (nunca ecoa a string do input).
 */
export function validarData(entrada: string): string {
  const m = RE_DATA.exec(String(entrada).trim());
  if (!m) {
    throw new Error(`data inválida (esperado YYYY-MM-DD): ${entrada}`);
  }
  const [, ay, am, ad] = m;
  const ano = Number(ay);
  const mes = Number(am);
  const dia = Number(ad);
  const d = new Date(Date.UTC(ano, mes - 1, dia));
  // Round-trip: rejeita ex. 2026-02-30 (normalizado pelo Date).
  if (d.getUTCFullYear() !== ano || d.getUTCMonth() !== mes - 1 || d.getUTCDate() !== dia) {
    throw new Error(`data inexistente: ${entrada}`);
  }
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(d.getUTCDate()).padStart(2, '0');
  return `${d.getUTCFullYear()}-${mm}-${dd}`;
}

/** Valida um serie_id como inteiro estrito. */
export function validarSerieId(valor: string | number): number {
  const n = typeof valor === 'number' ? valor : Number(valor);
  if (!Number.isInteger(n) || n < 0) {
    throw new Error(`serie_id inválido: ${valor}`);
  }
  return n;
}

/** Garante que o app existe na allowlist retornada pela query de apps. */
export function validarAlvoApp(alvo: string, appsPermitidos: readonly string[]): string {
  if (!appsPermitidos.includes(alvo)) {
    throw new Error(`app fora da allowlist: ${alvo}`);
  }
  return alvo;
}

/** Normaliza e valida o `alvo` conforme o nível (retorna literal SQL seguro). */
export function alvoLiteral(nivel: Nivel, alvo: string): string {
  if (nivel === 'serie') {
    return quoteLiteral(String(validarSerieId(alvo)));
  }
  // nível app: allowlist é aplicada na camada de API (contra a lista de apps);
  // aqui aplicamos o escaping defensivo.
  return quoteLiteral(alvo);
}

// --------------------------------------------------------------------------
// Queries permitidas (as mesmas do contrato da API no README do projeto).
// --------------------------------------------------------------------------

export const SQL_APPS = 'SELECT DISTINCT app FROM metricas.dim_serie ORDER BY app;';

export function sqlSeries(app: string): string {
  return (
    'SELECT serie_id, produto, jornada, escopo, status FROM metricas.dim_serie ' +
    `WHERE app = ${quoteLiteral(app)} ORDER BY jornada, escopo, status;`
  );
}

export function sqlGrafico(nivel: string, alvo: string, diaA: string, diaB: string): string {
  const n = validarNivel(nivel);
  const a = alvoLiteral(n, alvo);
  const da = quoteLiteral(validarData(diaA));
  const db = quoteLiteral(validarData(diaB));
  return `SELECT * FROM metricas.fn_grafico(${quoteLiteral(n)}, ${a}, ${da}, ${db});`;
}

export function sqlPerfil(nivel: string, alvo: string, dia: string): string {
  const n = validarNivel(nivel);
  const a = alvoLiteral(n, alvo);
  const d = quoteLiteral(validarData(dia));
  return `SELECT * FROM metricas.fn_perfil(${quoteLiteral(n)}, ${a}, ${d});`;
}

export function sqlStats(nivel: string, alvo: string): string {
  const n = validarNivel(nivel);
  const a = alvoLiteral(n, alvo);
  return (
    'SELECT nivel, alvo, dia_recorde, total_dia_recorde, minuto_recorde_ts, ' +
    'minuto_recorde_valor, mediana_3m, atualizado_em ' +
    `FROM metricas.stats_alvo WHERE nivel = ${quoteLiteral(n)} AND alvo = ${a};`
  );
}
