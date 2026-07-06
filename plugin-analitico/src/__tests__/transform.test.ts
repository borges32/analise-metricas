import {
  BASE_DAY_UTC,
  GraficoRow,
  horarioParaMinuto,
  minutoParaTimestamp,
  transformar,
} from '../transform';

describe('horarioParaMinuto', () => {
  it('converte HH:MM:SS', () => {
    expect(horarioParaMinuto('00:00:00')).toBe(0);
    expect(horarioParaMinuto('01:30:00')).toBe(90);
    expect(horarioParaMinuto('23:59')).toBe(1439);
  });
  it('rejeita inválido', () => {
    expect(horarioParaMinuto('abc')).toBe(-1);
    expect(horarioParaMinuto('99:99')).toBe(-1);
  });
});

describe('minutoParaTimestamp', () => {
  it('projeta no dia-base', () => {
    expect(minutoParaTimestamp(0)).toBe(BASE_DAY_UTC);
    expect(minutoParaTimestamp(90)).toBe(BASE_DAY_UTC + 90 * 60_000);
  });
});

describe('transformar', () => {
  it('preenche grade completa de 1440 minutos', () => {
    const r = transformar([], []);
    expect(r.series.time).toHaveLength(1440);
    expect(r.series.analisado).toHaveLength(1440);
    // buracos viram 0
    expect(r.series.analisado.every((v) => v === 0)).toBe(true);
  });

  it('dia completo: soma e variação %', () => {
    const grafico: GraficoRow[] = [
      { horario: '00:00:00', dia_analisado: 100, dia_comparativo: 80, dia_recorde: 200 },
      { horario: '00:01:00', dia_analisado: 50, dia_comparativo: 20, dia_recorde: 100 },
    ];
    const r = transformar(grafico, [{ horario: '00:00:00', mediana: 90 }]);
    expect(r.agregados.totalAnalisado).toBe(150);
    expect(r.agregados.totalComparativo).toBe(100);
    expect(r.agregados.variacaoPct).toBeCloseTo(50); // (150-100)/100
    expect(r.temRecorde).toBe(true);
    expect(r.series.recorde).not.toBeNull();
    expect(r.series.recorde![0]).toBe(200);
    expect(r.series.tipico[0]).toBe(90);
  });

  it('dia parcial: minutos ausentes ficam 0', () => {
    const r = transformar(
      [{ horario: '12:00:00', dia_analisado: 10, dia_comparativo: 5, dia_recorde: null }],
      []
    );
    expect(r.series.analisado[12 * 60]).toBe(10);
    expect(r.series.analisado[0]).toBe(0);
    expect(r.agregados.totalAnalisado).toBe(10);
  });

  it('alvo sem recorde: série de recorde é null', () => {
    const grafico: GraficoRow[] = [
      { horario: '00:00:00', dia_analisado: 10, dia_comparativo: 8, dia_recorde: null },
    ];
    const r = transformar(grafico, []);
    expect(r.temRecorde).toBe(false);
    expect(r.series.recorde).toBeNull();
  });

  it('série/dia sem dados: variação null quando comparativo é 0', () => {
    const r = transformar(
      [{ horario: '00:00:00', dia_analisado: 10, dia_comparativo: 0, dia_recorde: null }],
      []
    );
    expect(r.agregados.variacaoPct).toBeNull();
  });
});
