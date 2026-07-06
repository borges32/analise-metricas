import {
  alvoLiteral,
  quoteLiteral,
  sqlGrafico,
  sqlPerfil,
  sqlStats,
  sqlSeries,
  validarAlvoApp,
  validarData,
  validarNivel,
  validarSerieId,
} from '../sqlSafe';

describe('validarNivel', () => {
  it('aceita app e serie', () => {
    expect(validarNivel('app')).toBe('app');
    expect(validarNivel('serie')).toBe('serie');
  });
  it('rejeita valor fora da allowlist', () => {
    expect(() => validarNivel('drop')).toThrow(/inválido/);
    expect(() => validarNivel("app' OR '1'='1")).toThrow();
  });
});

describe('validarData', () => {
  it('aceita e re-serializa data válida', () => {
    expect(validarData('2026-07-04')).toBe('2026-07-04');
  });
  it('rejeita formato inválido', () => {
    expect(() => validarData('04/07/2026')).toThrow();
    expect(() => validarData("2026-07-04'; DROP TABLE")).toThrow();
  });
  it('rejeita data inexistente', () => {
    expect(() => validarData('2026-02-30')).toThrow(/inexistente/);
  });
});

describe('validarSerieId', () => {
  it('aceita inteiro', () => {
    expect(validarSerieId('42')).toBe(42);
    expect(validarSerieId(7)).toBe(7);
  });
  it('rejeita não-inteiro', () => {
    expect(() => validarSerieId('1; DROP')).toThrow();
    expect(() => validarSerieId('1.5')).toThrow();
  });
});

describe('quoteLiteral', () => {
  it('escapa aspas simples', () => {
    expect(quoteLiteral("a'b")).toBe("'a''b'");
  });
});

describe('validarAlvoApp', () => {
  it('exige app na allowlist', () => {
    expect(validarAlvoApp('mobilepf', ['mobilepf', 'pix'])).toBe('mobilepf');
    expect(() => validarAlvoApp('evil', ['mobilepf'])).toThrow(/allowlist/);
  });
});

describe('alvoLiteral', () => {
  it('serie exige inteiro e vira literal', () => {
    expect(alvoLiteral('serie', '13')).toBe("'13'");
    expect(() => alvoLiteral('serie', "13' OR 1=1")).toThrow();
  });
  it('app escapa aspas', () => {
    expect(alvoLiteral('app', "pix'--")).toBe("'pix''--'");
  });
});

describe('montagem de SQL', () => {
  it('sqlGrafico monta chamada válida', () => {
    const sql = sqlGrafico('app', 'mobilepf', '2026-07-04', '2026-06-27');
    expect(sql).toBe(
      "SELECT * FROM metricas.fn_grafico('app', 'mobilepf', '2026-07-04', '2026-06-27');"
    );
  });
  it('sqlGrafico com série valida inteiro', () => {
    const sql = sqlGrafico('serie', '13', '2026-07-04', '2026-06-27');
    expect(sql).toContain("fn_grafico('serie', '13', '2026-07-04', '2026-06-27')");
  });
  it('sqlGrafico bloqueia injeção via alvo de série', () => {
    expect(() => sqlGrafico('serie', "1); DROP TABLE x;--", '2026-07-04', '2026-06-27')).toThrow();
  });
  it('sqlPerfil e sqlStats montam corretamente', () => {
    expect(sqlPerfil('app', 'mobilepf', '2026-07-04')).toContain(
      "fn_perfil('app', 'mobilepf', '2026-07-04')"
    );
    expect(sqlStats('serie', '13')).toContain("nivel = 'serie' AND alvo = '13'");
  });
  it('sqlSeries escapa o app', () => {
    expect(sqlSeries("x'y")).toContain("WHERE app = 'x''y'");
  });
});
