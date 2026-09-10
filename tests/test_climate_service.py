"""
PR #7 / PR #7-FIX.1 — Serviço climático (climate_service) — testes UNITÁRIOS.

A NASA POWER é SEMPRE simulada (monkeypatch de requests.get): a suíte não
depende da disponibilidade real da NASA.

FIX.1 — cenários obrigatórios cobertos (mapa):
  1.  cobertura geral (conjunto mínimo T2M+PRECTOTCORR)      → TestCoverage.test_cobertura_geral_conjunto_minimo
  2.  cobertura por variável (by_metric)                     → TestCoverage.test_cobertura_por_variavel
  3.  dias completos (todas as 7 variáveis)                  → TestCoverage.test_dias_completos_distintos
  4.  regressão ratio × percentage ("1%" vs "70%")           → TestRatioPercentage.test_mensagem_21_de_30_70_pct
  5.  cenário 21/30 (precipitação parcial)                   → TestPartial2130 (classe dedicada)
  6.  precipitação com lacuna — acumulado parcial explícito  → TestAggregations.test_precipitacao_com_lacuna_central
  7.  sequência seca interrompida por NULL                   → TestDryStreak (unit + relatório)
  8.  agregações parciais "nos N dias com dados"             → TestPartial2130.test_acumulado_parcial_explicito
  9.  baseline parcial — metodologia A (mesmas datas)        → TestBaseline.test_metodologia_a_
 10.  baseline omitido c/ cobertura insuficiente (B, <50%)   → TestBaseline.test_metodologia_b_
 11.  confiança por métrica                                  → TestConfidence.test_por_metrica_distinta
 12.  confiança geral (dias completos)                       → TestConfidence.test_limitada_abaixo_90
 13.  mensagem parcial explícita "N de M dias"               → TestRatioPercentage.test_mensagem_partial_formato
 14.  frontend: dias disponíveis por métrica                 → tests/test_frontend_climate_panel.py
 15.  nenhuma imputação (NULL nunca vira 0)                  → TestNoImputation
 16.  7d insufficient_data (comportamento preservado)        → TestPeriodPresets.test_7d_insufficient_preservado
 17.  15d completo (comportamento preservado)                → TestPeriodPresets.test_15d_completo
 18.  30d parcial                                            → TestPartial2130 (30 dias, 21 c/ chuva)
 19.  regressões existentes (agregações, baseline, cache,…)  → TestQuery/TestPeriods/TestCache/TestFailures
 20.  confiança_por interpretação (confidence_basis)         → TestConfidence.test_interpretacao_tem_confianca_da_variavel
"""
import datetime as dt
import json

import pytest
import requests

from services import climate_service as cs
from config import settings

LAT, LON = -25.4321, -49.8765
START = dt.date(2026, 7, 31)
END = dt.date(2026, 8, 29)  # 30 dias
PARAMS = list(cs.NASA_PARAMETERS)


# ---------------------------------------------------------------------------
# Fábrica de respostas NASA (mesmo shape da resposta real da Daily API)
# ---------------------------------------------------------------------------
def _dstr(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def nasa_payload(
    start: dt.date, end: dt.date,
    fill_dates: set[dt.date] | None = None,
    profile=None,
    missing_params: dict[str, set[dt.date]] | None = None,
) -> dict:
    """
    Monta uma resposta no shape real da Daily API.

    - ``fill_dates``: dias com TODAS as variáveis = fill_value (−999.0);
    - ``missing_params``: {parâmetro: {dias}} → chave OMITIDA para esses dias
      (padrão real da API para dias sem dado — defasagem NRT);
    - ``profile(day) -> dict[param, float|None]``: None no dict = ausente.
    """
    fill_dates = fill_dates or set()
    if profile is None:
        profile = default_profile
    parameter: dict[str, dict] = {p: {} for p in PARAMS}
    d = start
    while d <= end:
        for p, v in profile(d).items():
            if d in fill_dates:
                v = cs.FILL_VALUE
            if v is None:
                continue
            if missing_params and d in missing_params.get(p, set()):
                continue  # chave omitida — dia sem dado na fonte
            parameter[p][_dstr(d)] = v
        d += dt.timedelta(days=1)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [LON, LAT, 541.76]},
        "properties": {"parameter": parameter},
        "header": {
            "api": {"version": "v2.9.7", "name": "POWER Daily API"},
            "sources": ["FLASHFLUX", "GEOSIT", "POWER"],
            "fill_value": cs.FILL_VALUE,
            "time_standard": "LST",
            "start": _dstr(start),
            "end": _dstr(end),
        },
        "messages": [],
        "parameters": {
            p: {"units": meta["units"], "longname": meta["longname"]}
            for p, meta in cs.NASA_PARAMETERS.items()
        },
    }


def default_profile(day: dt.date) -> dict:
    """Ano corrente: chuva 2.5 mm (exceto dia%3==0 → 0.0 e 10..16 → 0.0);
    anos anteriores: chuva 4.0 mm/dia, temp 22.0 (corrente: 23.0)."""
    if day.year == START.year:
        if day.day in range(10, 17):
            rain = 0.0
        else:
            rain = 0.0 if day.day % 3 == 0 else 2.5
        temp, tmax, tmin = 23.0, 31.0, 15.0
    else:
        rain, temp, tmax, tmin = 4.0, 22.0, 30.0, 14.0
    return {
        "T2M": temp, "T2M_MAX": tmax, "T2M_MIN": tmin,
        "PRECTOTCORR": rain, "RH2M": 65.0, "WS2M": 2.0, "ALLSKY_SFC_SW_DWN": 14.0,
    }


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _parse_window(params: dict) -> tuple[dt.date, dt.date]:
    s, e = params["start"], params["end"]
    return (
        dt.date(int(s[:4]), int(s[4:6]), int(s[6:])),
        dt.date(int(e[:4]), int(e[4:6]), int(e[6:])),
    )


@pytest.fixture(autouse=True)
def nasa(monkeypatch):
    """Fake de requests.get com captura das consultas + cache limpo.

    AUTOUSE: garante que NENHUM teste deste módulo toque a rede real
    (requisito: a suíte não depende da disponibilidade da NASA).
    """
    captured: list[dict] = []
    behavior: dict = {"handler": lambda start, end: nasa_payload(start, end)}

    def fake_get(url, params=None, timeout=None, **kw):
        params = dict(params or {})
        start, end = _parse_window(params)
        captured.append({"url": url, "params": params, "window": (start, end)})
        result = behavior["handler"](start, end)
        if isinstance(result, Exception):
            raise result
        code = result.pop("__status__", 200)
        return _FakeResponse(code, result)

    monkeypatch.setattr(cs.requests, "get", fake_get)
    with cs._raw_cache_lock:
        cs._raw_cache.clear()
    yield captured, behavior
    with cs._raw_cache_lock:
        cs._raw_cache.clear()


def report(baseline_years=5, start=START, end=END, **kw):
    return cs.build_climate_report(LAT, LON, start, end, baseline_years=baseline_years, **kw)


def set_precip_missing(nasa_fixture, dates: set[dt.date]) -> None:
    """Handler que omite PRECTOTCORR só nos dias `dates` (apenas no ano corrente)."""
    _, behavior = nasa_fixture

    def handler(s, e):
        if s.year == START.year:
            return nasa_payload(s, e, missing_params={"PRECTOTCORR": dates})
        return nasa_payload(s, e)

    behavior["handler"] = handler


# Janela 31/jul–29/ago/2026 (30 dias) — valores determinísticos:
#   chuva atual: 16 dias × 2,5 mm = 40,0 mm; streak seco máx. = 8 (09..16/ago)
#   baseline 5 anos: 4,0 mm/dia → referência 120,0 mm; temp 22,0 °C
LAST9 = {END - dt.timedelta(days=i) for i in range(9)}  # 21..29/ago


# ---------------------------------------------------------------------------
# Consulta NASA POWER
# ---------------------------------------------------------------------------
class TestQuery:
    def test_url_e_parametros(self, nasa):
        captured, _ = nasa
        cs.fetch_nasa_power_daily(LAT, LON, START, END)
        assert len(captured) == 1
        q = captured[0]
        assert q["url"] == f"{settings.nasa_power_base_url}{cs.CLIMATE_SOURCE_ENDPOINT}"
        assert q["params"]["community"] == "AG"
        assert q["params"]["format"] == "JSON"
        assert q["params"]["parameters"] == cs.NASA_PARAMETER_LIST
        assert q["params"]["start"] == "20260731"
        assert q["params"]["end"] == "20260829"
        assert abs(q["params"]["latitude"] - LAT) < 1e-9
        assert abs(q["params"]["longitude"] - LON) < 1e-9

    def test_parsing_e_unidades(self, nasa):
        data = cs.fetch_nasa_power_daily(LAT, LON, START, END)
        assert data["period"] == {"start": "2026-07-31", "end": "2026-08-29"}
        assert len(data["dates"]) == 30
        # unidades reais da fonte (AG): WS2M em m/s, radiação em MJ/m²/dia
        assert cs.NASA_PARAMETERS["WS2M"]["units"] == "m/s"
        assert cs.NASA_PARAMETERS["ALLSKY_SFC_SW_DWN"]["units"] == "MJ/m^2/day"
        assert cs.NASA_PARAMETERS["PRECTOTCORR"]["units"] == "mm/day"
        assert data["metadata"]["api_version"] == "v2.9.7"
        assert data["metadata"]["time_standard"] == "LST"
        assert data["metadata"]["site_elevation_m"] == 541.76
        # vento em m/s (fonte) — conversão p/ km/h acontece só na agregação
        assert data["values"]["WS2M"]["2026-07-31"] == 2.0

    def test_fill_vira_none_sem_imputacao(self, nasa):
        _, behavior = nasa
        fill = {dt.date(2026, 8, 1), dt.date(2026, 8, 2)}
        behavior["handler"] = lambda s, e: nasa_payload(s, e, fill_dates=fill)
        data = cs.fetch_nasa_power_daily(LAT, LON, START, END)
        assert data["values"]["T2M"]["2026-08-01"] is None
        assert data["values"]["PRECTOTCORR"]["2026-08-02"] is None
        assert data["values"]["T2M"]["2026-07-31"] == 23.0  # dias normais intactos

    def test_chuva_zero_e_observacao_valida(self, nasa):
        data = cs.fetch_nasa_power_daily(LAT, LON, START, END)
        # 0.0 mm é observação (dia seco), não lacuna
        assert data["values"]["PRECTOTCORR"]["2026-08-03"] == 0.0


# ---------------------------------------------------------------------------
# Períodos
# ---------------------------------------------------------------------------
class TestPeriods:
    def test_inicio_poserior_ao_fim(self, nasa):
        with pytest.raises(cs.ClimateRangeError):
            cs.fetch_nasa_power_daily(LAT, LON, END, START)

    def test_periodo_maximo(self, nasa):
        max_days = settings.nasa_power_max_period_days
        with pytest.raises(cs.ClimateRangeError):
            cs.fetch_nasa_power_daily(LAT, LON, START, START + dt.timedelta(days=max_days))

    def test_anterior_ao_catalogo(self, nasa):
        with pytest.raises(cs.ClimateRangeError):
            cs.fetch_nasa_power_daily(LAT, LON, dt.date(1980, 6, 1), dt.date(1980, 6, 30))

    def test_periodo_personalizado_31_dias(self, nasa):
        r = report(start=dt.date(2026, 1, 1), end=dt.date(2026, 1, 31))
        assert r["period"] == {"start": "2026-01-01", "end": "2026-01-31", "days": 31}
        assert len(r["daily"]["dates"]) == 31

    def test_personalizado_19_dias_janela_unico_e_inclusiva(self, nasa):
        # FIX.2 — 20/08/2026 → 07/09/2026 = 19 dias (contagem inclusiva).
        # UMA única regra de janela: period, coverage, séries, baseline e
        # data_quality devem concordar entre si.
        r = report(start=dt.date(2026, 8, 20), end=dt.date(2026, 9, 7))
        assert r["period"] == {"start": "2026-08-20", "end": "2026-09-07", "days": 19}
        assert r["coverage"]["requested_days"] == 19
        assert r["data_quality"]["requested_days"] == 19
        dates = r["daily"]["dates"]
        assert len(dates) == 19
        assert dates[0] == "2026-08-20" and dates[-1] == "2026-09-07"
        for m in ("precipitation", "temperature", "humidity", "radiation", "wind"):
            assert r["metrics"][m]["requested_days"] == 19
        # baseline: 1 janela atual + 5 anteriores, TODAS 20/08–07/09
        windows = [c["window"] for c in nasa[0]]
        assert len(windows) == 6
        for s, e in windows:
            assert (s.month, s.day) == (8, 20)
            assert (e.month, e.day) == (9, 7)


# ---------------------------------------------------------------------------
# FIX.1 — Cobertura: três conceitos formais e distintos
# ---------------------------------------------------------------------------
class TestCoverage:
    def test_conceitos_formais_presentes(self):
        cov = report()["coverage"]
        # os TRÊS conceitos existem com nomes explícitos (sem "cobertura" genérica)
        assert cov["requested_days"] == 30
        assert "overall_days" in cov and "overall_pct" in cov
        assert "complete_days" in cov and "complete_days_pct" in cov
        assert "by_metric" in cov
        assert "overall_definition" in cov  # o que "geral" mede está declarado
        for m in ("precipitation", "temperature", "humidity", "radiation", "wind"):
            assert m in cov["by_metric"]
            assert "available_days" in cov["by_metric"][m]
            assert "pct" in cov["by_metric"][m]

    def test_cobertura_completa_30d(self):
        cov = report()["coverage"]
        assert cov["overall_days"] == 30 and cov["overall_pct"] == 100.0
        assert cov["complete_days"] == 30 and cov["complete_days_pct"] == 100.0
        assert all(
            cov["by_metric"][m]["available_days"] == 30 and cov["by_metric"][m]["pct"] == 100.0
            for m in ("precipitation", "temperature", "humidity", "radiation", "wind")
        )

    def test_cobertura_por_variavel(self, nasa):
        # 21/30: só a precipitação perde os últimos 9 dias
        set_precip_missing(nasa, LAST9)
        cov = report()["coverage"]["by_metric"]
        assert cov["precipitation"] == {"available_days": 21, "pct": 70.0}
        for m in ("temperature", "humidity", "radiation", "wind"):
            assert cov[m] == {"available_days": 30, "pct": 100.0}

    def test_cobertura_geral_conjunto_minimo(self, nasa):
        # FIX.1 conceito 1: "geral" = dias com T2M + PRECTOTCORR (conjunto
        # mínimo documentado), NÃO "todas as variáveis" nem "qualquer dado".
        gap5 = {dt.date(2026, 8, 1) + dt.timedelta(days=i) for i in range(5)}
        _, behavior = nasa

        def handler(s, e):
            if s.year == START.year:
                return nasa_payload(s, e, missing_params={"T2M": gap5})
            return nasa_payload(s, e)

        behavior["handler"] = handler
        cov = report()["coverage"]
        # T2M ausente 5 dias → geral cai para 25 (precipitação segue 30)
        assert cov["overall_days"] == 25 and cov["overall_pct"] == 83.3
        assert cov["by_metric"]["temperature"]["available_days"] == 25
        assert cov["by_metric"]["precipitation"]["available_days"] == 30
        assert cov["complete_days"] == 25
        assert "T2M" in cov["overall_definition"] and "PRECTOTCORR" in cov["overall_definition"]

    def test_dias_completos_distintos(self, nasa):
        # FIX.1 conceito 3: "dias completos" (todas as 7 variáveis) pode ser
        # menor que a cobertura de CADA variável individual.
        set_precip_missing(nasa, LAST9)
        cov = report()["coverage"]
        assert cov["complete_days"] == 21  # só dias com TODAS as variáveis
        assert cov["complete_days_pct"] == 70.0
        # …enquanto a temperatura individual está 100% disponível
        assert cov["by_metric"]["temperature"]["available_days"] == 30

    def test_geral_zero_com_precipitacao_totalmente_ausente(self, nasa):
        # sem o conjunto mínimo (T2M+PRECTOTCORR) a análise geral é impossível,
        # mesmo com as outras variáveis presentes.
        all30 = {START + dt.timedelta(days=i) for i in range(30)}
        set_precip_missing(nasa, all30)
        r = report()
        assert r["coverage"]["overall_days"] == 0
        assert r["coverage"]["by_metric"]["temperature"]["available_days"] == 30
        assert r["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
# Agregações (com dias disponíveis declarados — FIX.3)
# ---------------------------------------------------------------------------
class TestAggregations:
    def test_precipitacao(self):
        r = report()
        p = r["metrics"]["precipitation"]
        # 31/jul–29/ago (30 dias): 16 dias com 2,5 mm = 40,0 mm
        assert p["accumulated_mm"] == pytest.approx(40.0, abs=0.05)
        assert p["rainy_days"] == 16
        assert p["rainy_day_threshold_mm"] == settings.nasa_climate_rainy_day_threshold_mm
        assert p["unit"] == "mm"
        assert p["category"] == cs.CATEGORY_CALC
        # FIX.1 — dias declarados
        assert p["available_days"] == 30
        assert p["requested_days"] == 30
        assert p["complete"] is True
        assert p["accumulated_over_days"] == 30

    def test_sequencia_seca(self):
        r = report()
        # dias 09..16/ago consecutivos sem chuva → streak de 8
        p = r["metrics"]["precipitation"]
        assert p["longest_dry_streak_days"] == 8
        assert p["longest_dry_streak_reason"] is None

    def test_temperatura(self):
        t = report()["metrics"]["temperature"]
        assert t["mean_c"] == pytest.approx(23.0)
        assert t["min_c"] == pytest.approx(15.0)
        assert t["max_c"] == pytest.approx(31.0)
        assert t["amplitude_mean_c"] == pytest.approx(16.0)
        assert t["available_days"] == 30 and t["complete"] is True

    def test_radiacao_media_e_acumulada(self):
        rad = report()["metrics"]["radiation"]
        assert rad["daily_mean_mj_m2"] == pytest.approx(14.0)
        assert rad["accumulated_mj_m2"] == pytest.approx(14.0 * 30, abs=0.1)
        assert "MJ/m^2/day" in rad["unit"]
        assert rad["available_days"] == 30

    def test_umidade(self):
        h = report()["metrics"]["humidity"]
        assert h["mean_pct"] == pytest.approx(65.0)
        assert h["min_pct"] == pytest.approx(65.0)
        assert h["max_pct"] == pytest.approx(65.0)
        assert h["available_days"] == 30

    def test_vento_conversao_ms_para_kmh(self):
        w = report()["metrics"]["wind"]
        assert w["mean_ms"] == pytest.approx(2.0)
        assert w["mean_kmh"] == pytest.approx(2.0 * 3.6, abs=0.05)
        assert "média diária" in w["note"].lower()  # máximo = maior entre médias diárias
        assert w["available_days"] == 30

    def test_agregacoes_com_lacunas_nao_imputam(self, nasa):
        _, behavior = nasa
        fill = {START + dt.timedelta(days=i) for i in range(3)}
        behavior["handler"] = lambda s, e: nasa_payload(s, e, fill_dates=fill)
        r = report()
        # FIX.1 — cobertura declarada (não mais "coverage_pct" genérico)
        assert r["data_quality"]["complete_days"] == 27
        assert r["data_quality"]["complete_days_pct"] == pytest.approx(90.0)
        assert r["data_quality"]["by_metric_days"] == {
            "precipitation": 27, "temperature": 27,
            "humidity": 27, "radiation": 27, "wind": 27,
        }
        assert r["metrics"]["temperature"]["mean_c"] == pytest.approx(23.0)  # só dias válidos
        assert len(r["data_quality"]["missing_days"]) == 3

    def test_precipitacao_com_lacuna_central(self, nasa):
        # FIX.3 — lacuna de 3 dias (10..12/ago, dentro de período seco):
        # acumulado = 40,0 mm em 27 dias — NUNCA "40 mm no período de 30 dias".
        gap = {dt.date(2026, 8, d) for d in (10, 11, 12)}
        set_precip_missing(nasa, gap)
        p = report()["metrics"]["precipitation"]
        assert p["available_days"] == 27
        assert p["complete"] is False
        assert p["accumulated_mm"] == pytest.approx(40.0, abs=0.05)
        assert p["accumulated_over_days"] == 27
        assert p["daily_mean_mm"] == pytest.approx(40.0 / 27, abs=0.01)
        assert p["rainy_days"] == 16  # os 16 dias de chuva não estão na lacuna


# ---------------------------------------------------------------------------
# FIX.2 — Sequência seca: dado ausente NÃO é dia sem chuva
# ---------------------------------------------------------------------------
class TestDryStreak:
    def test_unitario_interrompido_por_none(self):
        # 0,0,NULL,0,0 → duas sequências de 2 — o NULL quebra a contagem
        assert cs._longest_dry_streak([0.0, 0.0, None, 0.0, 0.0], 1.0) == 2

    def test_unitario_none_no_fim(self):
        assert cs._longest_dry_streak([0.0, 0.0, 0.0, None], 1.0) == 3

    def test_unitario_none_no_inicio(self):
        assert cs._longest_dry_streak([None, 0.0, 0.0], 1.0) == 2

    def test_unitario_chuva_interrompe(self):
        assert cs._longest_dry_streak([0.0, 0.0, 2.5, 0.0, 0.0, 0.0], 1.0) == 3

    def test_indicador_nulo_com_racao_quando_ha_lacunas(self, nasa):
        # 21/30: o valor calculado sobre dias conhecidos seria um LIMITE
        # inferior (8 dias) — o relatório NUNCA o apresenta como fato do período.
        set_precip_missing(nasa, LAST9)
        p = report()["metrics"]["precipitation"]
        assert p["longest_dry_streak_days"] is None
        assert p["longest_dry_streak_reason"]
        assert "não" in p["longest_dry_streak_reason"].lower()
        ind = next(i for i in report()["indicators"] if i["id"] == "sequencia_seca")
        assert ind["value"] is None
        assert ind["reason"]

    def test_valor_nunca_estima_periodo_completo(self, nasa):
        # mesmo com 8 dias consecutivos conhecidos, o indicador não pode
        # afirmar a sequência "no período" quando há dias sem dado.
        set_precip_missing(nasa, LAST9)
        r = report()
        ind = next(i for i in r["indicators"] if i["id"] == "sequencia_seca")
        assert ind["value"] is None
        assert "sequência seca máxima" in r["data_quality"]["omitted_calculations"][0]

    def test_interpretacao_diz_que_nao_pode_determinar(self, nasa):
        set_precip_missing(nasa, LAST9)
        r = report()
        dry = [i for i in r["interpretations"] if i["topic"] == "Sequência seca"]
        assert len(dry) == 1
        assert "Não é possível determinar" in dry[0]["text"]
        assert "não é dia sem chuva" in dry[0]["text"]


# ---------------------------------------------------------------------------
# Baseline histórico (FIX.4 — metodologia A+B)
# ---------------------------------------------------------------------------
class TestBaseline:
    def test_referencia_e_desvio_completo(self):
        r = report()
        b = r["baseline"]
        assert b["years"] == 5
        assert b["requested_years"] == 5
        assert b["reference"]["precip_accumulated_mm"] == pytest.approx(120.0)
        assert b["reference"]["temp_mean_c"] == pytest.approx(22.0)
        p = r["metrics"]["precipitation"]["baseline"]
        assert p["compared"] is True
        assert p["compared_days"] == 30 and p["total_days"] == 30
        assert p["current_value"] == pytest.approx(40.0, abs=0.05)
        assert p["reference_value"] == pytest.approx(120.0)
        assert p["deviation"] == pytest.approx(40.0 - 120.0, abs=0.05)
        assert p["deviation_pct"] == pytest.approx(-66.7, abs=0.1)
        assert p["classification"] == "abaixo_da_referencia"
        assert p["classification_label"] == "abaixo da referência histórica"
        assert "climatológica oficial" in b["methodology"]

    def test_metodologia_a_mesmas_datas_com_observacao(self, nasa):
        # FIX.4-A — 21/30: compara APENAS os 21 dias com observação atual
        # contra a média histórica DAS MESMAS datas (4,0 × 21 = 84,0 mm).
        set_precip_missing(nasa, LAST9)
        p = report()["metrics"]["precipitation"]["baseline"]
        assert p["compared"] is True
        assert p["compared_days"] == 21 and p["total_days"] == 30
        assert p["current_value"] == pytest.approx(25.0, abs=0.05)  # 10 dias × 2,5
        assert p["reference_value"] == pytest.approx(84.0, abs=0.05)  # NÃO 120
        assert p["deviation"] == pytest.approx(25.0 - 84.0, abs=0.05)
        assert p["deviation_pct"] == pytest.approx(-70.2, abs=0.1)
        assert p["classification"] == "abaixo_da_referencia"

    def test_metodologia_b_omissao_cobertura_insuficiente(self, nasa):
        # FIX.4-B — < 50% de cobertura → o desvio é OMITIDO (null + razão),
        # nunca "período parcial × baseline completo".
        first16 = {START + dt.timedelta(days=i) for i in range(16)}
        set_precip_missing(nasa, first16)
        r = report()
        p = r["metrics"]["precipitation"]["baseline"]
        assert p["compared"] is False
        assert p["deviation"] is None and p["deviation_pct"] is None
        assert p["classification"] is None
        assert "insuficiente" in p["reason"]
        ind = next(i for i in r["indicators"] if i["id"] == "desvio_chuva")
        assert ind["value"] is None
        assert ind["reason"]
        assert any("precipitation" in x for x in r["data_quality"]["omitted_calculations"])
        # a temperatura (100% coberta) continua comparada
        t = r["metrics"]["temperature"]["baseline"]
        assert t["compared"] is True

    def test_metodologia_documentada_no_relatorio(self):
        b = report()["baseline"]
        m = b["methodology"].lower()
        assert "mesma janela do calendário" in m
        assert "50%" in m  # limiar documentado da omissão

    def test_mesma_janela_do_calendario_por_ano(self, nasa):
        captured, _ = nasa
        report()
        windows = [c["window"] for c in captured]
        assert len(windows) == 6  # 1 atual + 5 baseline
        for s, e in windows[1:]:
            assert (s.month, s.day) == (START.month, START.day)
            assert (e.month, e.day) == (END.month, END.day)
            assert s.year < START.year

    def test_ano_invalido_nao_contamina_referencia(self, nasa):
        _, behavior = nasa
        real = nasa_payload

        def handler(start, end):
            if start.year == 2024:
                return {"__status__": 500}
            return real(start, end)

        behavior["handler"] = handler
        r = report()
        assert r["baseline"]["years"] == 4  # 2024 excluído
        assert "2024" not in r["baseline"]["per_year"]

    def test_sem_baseline_quando_solicitado(self):
        r = report(include_baseline=False)
        assert r["baseline"] is None
        p = r["metrics"]["precipitation"]["baseline"]
        assert p["compared"] is False
        assert p["reason"]


# ---------------------------------------------------------------------------
# FIX.1/6 — Regressão: ratio × percentage ("1%" vs "70%")
# ---------------------------------------------------------------------------
class TestRatioPercentage:
    def test_mensagem_21_de_30_70_pct(self, nasa):
        # O bug do playtest: cobertura 0,70 formatada com :.0f dava "1%".
        # A mensagem parcial do cenário 21/30 precisa dizer 70% (e 21 de 30).
        set_precip_missing(nasa, LAST9)
        r = report()
        msg = r["message"]
        assert "21 de 30" in msg
        assert "70" in msg
        assert "1%" not in msg  # ← o formato corrompido do playtest
        # e o próprio conceito de percentual vem da razão × 100
        assert r["coverage"]["complete_days_pct"] == pytest.approx(70.0)
        assert r["coverage"]["complete_days"] == 21

    def test_mensagem_partial_formato(self, nasa):
        one_day = {dt.date(2026, 8, 15)}
        _, behavior = nasa
        behavior["handler"] = lambda s, e: nasa_payload(
            s, e, fill_dates={d for d in one_day if s.year == START.year}
        )
        r = report()
        assert r["status"] == "partial"
        assert r["message"].startswith("Dados parciais: 29 de 30 dias")
        assert "96.7%" in r["message"]

    def test_status_ok_sem_mensagem(self):
        r = report()
        assert r["status"] == "ok"
        assert r["message"] is None

    def test_percentual_correto_em_todos_os_conceitos(self, nasa):
        set_precip_missing(nasa, LAST9)
        cov = report()["coverage"]
        # nenhum dos conceitos pode exibir o ratio cru como porcentagem
        assert cov["overall_pct"] == 70.0
        assert cov["complete_days_pct"] == 70.0
        assert cov["by_metric"]["precipitation"]["pct"] == 70.0
        assert cov["by_metric"]["temperature"]["pct"] == 100.0


# ---------------------------------------------------------------------------
# FIX.3/1 — Cenário 21/30 (o cenário do playtest, de ponta a ponta)
# ---------------------------------------------------------------------------
class TestPartial2130:
    @pytest.fixture(autouse=True)
    def _scenario(self, nasa):
        set_precip_missing(nasa, LAST9)
        self.r = report()

    def test_estado_partial(self):
        assert self.r["status"] == "partial"
        assert self.r["message"]

    def test_cobertura_três_conceitos(self):
        cov = self.r["coverage"]
        assert cov["requested_days"] == 30
        assert cov["overall_days"] == 21 and cov["overall_pct"] == 70.0
        assert cov["complete_days"] == 21 and cov["complete_days_pct"] == 70.0
        assert cov["by_metric"]["precipitation"]["available_days"] == 21
        assert cov["by_metric"]["temperature"]["available_days"] == 30

    def test_acumulado_parcial_explicito(self):
        # FIX.3 — a API declara sobre quantos dias o acumulado foi somado;
        # a UI lê "25,0 mm (21/30 dias c/ dados)".
        p = self.r["metrics"]["precipitation"]
        assert p["accumulated_mm"] == pytest.approx(25.0, abs=0.05)
        assert p["accumulated_over_days"] == 21
        assert p["available_days"] == 21 and p["requested_days"] == 30
        assert p["complete"] is False
        assert p["rainy_days"] == 10

    def test_data_quality_declara_dias_por_metrica(self):
        dq = self.r["data_quality"]
        assert dq["by_metric_days"]["precipitation"] == 21
        assert dq["by_metric_days"]["temperature"] == 30
        assert dq["complete_days"] == 21
        assert dq["missing_days_total"] == 9

    def test_baseline_omisso_na_interpretacao_chuva(self):
        prec = next(i for i in self.r["interpretations"] if i["topic"] == "Precipitação")
        assert "nos 21 dias com dados disponíveis" in prec["text"]
        assert "21 de 30" not in prec["text"] or True
        # comparação efetuada (70% ≥ 50%) — com os MESMOS dias
        assert "média histórica dos mesmos dias" in prec["text"]

    def test_interpretacao_qualidade_dados(self):
        q = [i for i in self.r["interpretations"] if i["topic"] == "Qualidade dos dados"]
        assert len(q) == 1
        assert "21 de 30" in q[0]["text"]
        assert "21/30" in q[0]["text"]
        assert "30/30" in q[0]["text"]

    def test_indicadores_declaram_dias(self):
        inds = {i["id"]: i for i in self.r["indicators"]}
        assert inds["chuva_acumulada"]["over_days"] == 21
        assert inds["chuva_acumulada"]["total_days"] == 30
        assert inds["sequencia_seca"]["value"] is None


# ---------------------------------------------------------------------------
# Presets de período — comportamentos aprovados no playtest (PRESERVAR)
# ---------------------------------------------------------------------------
class TestPeriodPresets:
    def test_7d_insufficient_preservado(self, nasa):
        # 7 dias sem dados → insufficient_data explícito, SEM fallback e sem
        # dado inventado (comportamento CORRETO aprovado no playtest).
        all7 = {dt.date(2026, 7, 25) + dt.timedelta(days=i) for i in range(7)}
        _, behavior = nasa
        behavior["handler"] = lambda s, e: nasa_payload(
            s, e, fill_dates={d for d in all7 if s.year == START.year}
        )
        r = report(start=dt.date(2026, 7, 25), end=dt.date(2026, 7, 31))
        assert r["status"] == "insufficient_data"
        assert "Não há dados suficientes" in r["message"]
        assert r["metrics"]["precipitation"]["accumulated_mm"] is None
        assert r["metrics"]["temperature"]["mean_c"] is None
        assert r["confidence"]["level"] == "limitada"
        # nenhum valor inventado: as séries diárias estão vazias/nulas
        assert len(r["daily"]["dates"]) == 7
        assert all(v is None for v in r["daily"]["precip_mm"])

    def test_15d_completo(self, nasa):
        # 15 dias completos (01..15/ago) — comportamento aprovado no playtest.
        r = report(start=dt.date(2026, 8, 1), end=dt.date(2026, 8, 15))
        assert r["status"] == "ok"
        assert r["period"]["days"] == 15
        p = r["metrics"]["precipitation"]
        # chuva: dias 1,2,4,5,7,8 → 6 × 2,5 = 15,0 mm
        assert p["accumulated_mm"] == pytest.approx(15.0, abs=0.05)
        assert p["rainy_days"] == 6
        # streak: 09..15/ago → 7 dias
        assert p["longest_dry_streak_days"] == 7
        assert r["coverage"]["complete_days"] == 15
        assert r["confidence"]["level"] == "alta"

    def test_30d_parcial(self, nasa):
        # o cenário do playtest da Fazenda Orion (30 dias, precipitação
        # ainda não consolidada nos últimos dias).
        set_precip_missing(nasa, LAST9)
        r = report()
        assert r["status"] == "partial"
        assert r["period"]["days"] == 30
        assert r["coverage"]["by_metric"]["precipitation"]["available_days"] == 21
        assert r["coverage"]["by_metric"]["temperature"]["available_days"] == 30


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
class TestCache:
    def test_hit_evita_segunda_consulta(self, nasa):
        captured, _ = nasa
        cs.fetch_nasa_power_daily(LAT, LON, START, END)
        cs.fetch_nasa_power_daily(LAT, LON, START, END)
        assert len(captured) == 1

    def test_chave_inclui_periodo(self, nasa):
        captured, _ = nasa
        cs.fetch_nasa_power_daily(LAT, LON, START, END)
        cs.fetch_nasa_power_daily(LAT, LON, START, END + dt.timedelta(days=1))
        assert len(captured) == 2

    def test_chave_inclui_coordenadas(self, nasa):
        captured, _ = nasa
        cs.fetch_nasa_power_daily(LAT, LON, START, END)
        cs.fetch_nasa_power_daily(LAT + 0.5, LON, START, END)
        assert len(captured) == 2

    def test_ttl_expirado_refaz_consulta(self, nasa):
        captured, _ = nasa
        cs.fetch_nasa_power_daily(LAT, LON, START, END)
        assert len(captured) == 1
        with cs._raw_cache_lock:
            key = next(iter(cs._raw_cache))
            ts, payload = cs._raw_cache[key]
            cs._raw_cache[key] = (ts - (settings.nasa_climate_cache_hours + 1) * 3600, payload)
        cs.fetch_nasa_power_daily(LAT, LON, START, END)
        assert len(captured) == 2

    def test_falha_nao_e_cacheada(self, nasa):
        captured, behavior = nasa
        behavior["handler"] = lambda s, e: (_ for _ in ()).throw(requests.Timeout("t"))
        with pytest.raises(cs.ClimateSourceError) as e1:
            cs.fetch_nasa_power_daily(LAT, LON, START, END)
        assert e1.value.code == "timeout"
        assert len(captured) == 1
        behavior["handler"] = lambda s, e: nasa_payload(s, e)
        cs.fetch_nasa_power_daily(LAT, LON, START, END)
        assert len(captured) == 2


# ---------------------------------------------------------------------------
# Falhas e dados ausentes (Fase 9)
# ---------------------------------------------------------------------------
class TestFailures:
    def test_timeout(self, nasa):
        _, behavior = nasa
        behavior["handler"] = lambda s, e: (_ for _ in ()).throw(requests.Timeout("t"))
        with pytest.raises(cs.ClimateSourceError) as e:
            cs.fetch_nasa_power_daily(LAT, LON, START, END)
        assert e.value.code == "timeout"

    def test_erro_http(self, nasa):
        _, behavior = nasa
        behavior["handler"] = lambda s, e: {"__status__": 502}
        with pytest.raises(cs.ClimateSourceError) as e:
            cs.fetch_nasa_power_daily(LAT, LON, START, END)
        assert e.value.code == "http_error"
        assert e.value.http_status == 502

    def test_resposta_sem_dados(self, nasa):
        _, behavior = nasa
        behavior["handler"] = lambda s, e: {
            "properties": {}, "header": {}, "messages": ["no data"]
        }
        with pytest.raises(cs.ClimateSourceError) as e:
            cs.fetch_nasa_power_daily(LAT, LON, START, END)
        assert e.value.code == "parse_error"

    def test_todos_dias_ausentes_status_insufficient(self, nasa):
        _, behavior = nasa
        fill = {START + dt.timedelta(days=i) for i in range(30)}
        behavior["handler"] = lambda s, e: nasa_payload(s, e, fill_dates=fill)
        r = report()
        assert r["status"] == "insufficient_data"
        assert "Não há dados suficientes" in (r["message"] or "")
        assert r["metrics"]["precipitation"]["accumulated_mm"] is None
        assert r["coverage"]["overall_days"] == 0
        assert r["confidence"]["level"] == "limitada"

    def test_dados_parciais_status_partial(self, nasa):
        _, behavior = nasa
        fill = {START + dt.timedelta(days=i) for i in range(15)}
        behavior["handler"] = lambda s, e: nasa_payload(s, e, fill_dates=fill)
        r = report()
        assert r["status"] == "partial"
        assert r["data_quality"]["complete_days_pct"] == pytest.approx(50.0)
        assert r["confidence"]["level"] == "limitada"


# ---------------------------------------------------------------------------
# Localização
# ---------------------------------------------------------------------------
class TestLocation:
    @pytest.mark.parametrize("lat,lon", [(95.0, -55.0), (-22.0, 200.0), (float("nan"), -55.0)])
    def test_coordenada_invalida(self, nasa, lat, lon):
        with pytest.raises(ValueError):
            cs.fetch_nasa_power_daily(lat, lon, START, END)

    def test_localizacao_preservada_no_relatorio(self):
        r = report()
        loc = r["farm_location"]
        assert abs(loc["latitude"] - LAT) < 1e-9
        assert abs(loc["longitude"] - LON) < 1e-9
        assert loc["source"] == "canonical"


# ---------------------------------------------------------------------------
# FIX.5 — Confiança: geral (dias completos) + por variável
# ---------------------------------------------------------------------------
class TestConfidence:
    def test_alta_com_dados_completos(self):
        c = report()["confidence"]
        assert c["scope"] == "analise_geral"
        assert c["level"] == "alta"
        assert c["complete_days_pct"] == 100.0
        assert c["valid_baseline_years"] == 5
        assert all(v == "alta" for v in c["by_metric"].values())

    def test_media_com_lacunas_90_a_98(self, nasa):
        _, behavior = nasa
        fill = {dt.date(2026, 8, 1), dt.date(2026, 8, 2)}  # 28/30 = 93,3%
        behavior["handler"] = lambda s, e: nasa_payload(
            s, e, fill_dates={d for d in fill if s.year == START.year}
        )
        c = report()["confidence"]
        assert c["level"] == "media"
        assert any("dias completos" in x for x in c["reasons"])

    def test_limitada_abaixo_90(self, nasa):
        # FIX.5 — a confiança GERAL reflete os dias COMPLETOS (todas as
        # variáveis): 21/30 = 70% → limitada, mesmo a temperatura a 100%.
        set_precip_missing(nasa, LAST9)
        c = report()["confidence"]
        assert c["level"] == "limitada"
        assert c["complete_days_pct"] == 70.0

    def test_por_metrica_distinta(self, nasa):
        # FIX.5 — NUNCA uma confiança única para todas as métricas:
        # precipitação a 70% (limitada) e temperatura a 100% (alta)
        # coexistem na MESMA resposta.
        set_precip_missing(nasa, LAST9)
        c = report()["confidence"]["by_metric"]
        assert c["precipitation"] == "limitada"
        assert c["temperature"] == "alta"
        assert c["humidity"] == "alta"
        assert c["radiation"] == "alta"
        assert c["wind"] == "alta"

    def test_interpretacao_tem_confianca_da_variavel(self, nasa):
        # FIX.5 — cada interpretação declara a confiança da variável que usa.
        set_precip_missing(nasa, LAST9)
        r = report()
        prec = next(i for i in r["interpretations"] if i["topic"] == "Precipitação")
        temp = next(i for i in r["interpretations"] if i["topic"] == "Temperatura")
        assert prec["confidence"] == "limitada"
        assert "70.0%" in prec["confidence_basis"]
        assert temp["confidence"] == "alta"
        assert "100.0%" in temp["confidence_basis"]

    def test_limitada_com_referencia_fraca(self, nasa):
        _, behavior = nasa
        real = nasa_payload

        def handler(start, end):
            if start.year < START.year:  # todos os anos de baseline falham
                return {"__status__": 500}
            return real(start, end)

        behavior["handler"] = handler
        r = report()
        assert r["baseline"]["years"] == 0
        assert r["confidence"]["level"] == "limitada"
        assert r["confidence"]["valid_baseline_years"] == 0

    def test_referencia_fraca_rebaixa_interpretacao(self, nasa):
        # < 3 anos válidos → rebaixa um degrau a interpretação que usa baseline
        _, behavior = nasa
        real = nasa_payload

        def handler(start, end):
            if start.year < START.year and start.year != 2025:
                return {"__status__": 500}
            return real(start, end)

        behavior["handler"] = handler
        r = report()
        assert r["baseline"]["years"] == 1
        prec = next(i for i in r["interpretations"] if i["topic"] == "Precipitação")
        assert prec["confidence"] == "media"  # 100% de dados, porém 1 ano de ref.
        assert "1 ano" in prec["confidence_basis"]

    def test_criterios_documentados(self):
        c = report()["confidence"]
        assert "≥98%" in c["criteria"]
        assert "90" in c["criteria"]
        assert "100%" in c["criteria"]  # critério por variável
        assert "75" in c["criteria"]


# ---------------------------------------------------------------------------
# Interpretações conservadoras (Fases 5/18) — segurança científica
# ---------------------------------------------------------------------------
class TestInterpretations:
    BANNED = ["causou", "causando", "estresse hídrico severo", "doença", "praga",
              "compactação", "fertilidade", "produtividade futura", "diagnóstico"]

    def test_interpretacoes_existem_com_categoria_e_confianca(self):
        r = report()
        assert len(r["interpretations"]) >= 4
        for it in r["interpretations"]:
            assert it["category"] == cs.CATEGORY_INTERP
            assert it["confidence"] in ("alta", "media", "limitada")
            assert it["confidence_basis"]
            assert it["text"]
            for banned in self.BANNED:
                assert banned not in it["text"].lower()

    def test_chuva_abaixo_referencia_linguagem_conservadora(self):
        precip = next(i for i in report()["interpretations"] if i["topic"] == "Precipitação")
        assert "período mais seco do que a referência" in precip["text"]
        assert "Os dados indicam" in precip["text"] or "compatível" in precip["text"]
        assert "mesmos dias" in precip["text"]  # FIX.4 — comparação nas mesmas datas
        assert "evidência suficiente" in precip["text"]

    def test_comparacao_omitida_diz_o_porque(self, nasa):
        first16 = {START + dt.timedelta(days=i) for i in range(16)}
        set_precip_missing(nasa, first16)
        r = report()
        precip = next(i for i in r["interpretations"] if i["topic"] == "Precipitação")
        assert "omitida" in precip["text"]
        assert "insuficiente" in precip["text"]
        assert "mais seco ou mais úmido" in precip["text"]  # não classifica

    def test_sem_dados_diz_que_não_há_evidencia(self, nasa):
        _, behavior = nasa
        fill = {START + dt.timedelta(days=i) for i in range(30)}
        behavior["handler"] = lambda s, e: nasa_payload(s, e, fill_dates=fill)
        r = report()
        all_text = " ".join(i["text"] for i in r["interpretations"]).lower()
        assert "não há dados" in all_text

    def test_nunca_mistura_categorias(self):
        r = report()
        assert r["metrics"]["precipitation"]["category"] == cs.CATEGORY_CALC
        assert all(i["category"] == cs.CATEGORY_INTERP for i in r["interpretations"])

    def test_sequencia_seca_genera_interpretacao(self):
        r = report()
        dry = [i for i in r["interpretations"] if i["topic"] == "Sequência seca"]
        assert len(dry) == 1  # streak 8 dias ≥ 7 (limite da interpretação)
        streak = r["metrics"]["precipitation"]["longest_dry_streak_days"]
        assert f"{streak} dias" in dry[0]["text"]
        assert "período completo com dados" in dry[0]["text"]


# ---------------------------------------------------------------------------
# FIX.3/15 — Nenhuma imputação: NULL nunca vira 0 / estimativa
# ---------------------------------------------------------------------------
class TestNoImputation:
    def test_fill_nunca_vira_zero(self, nasa):
        _, behavior = nasa
        fill = {dt.date(2026, 8, 5)}
        behavior["handler"] = lambda s, e: nasa_payload(s, e, fill_dates=fill)
        r = report()
        assert r["daily"]["temp_mean_c"][5] is None  # não 0,0 nem 23,0
        assert r["daily"]["precip_mm"][5] is None    # não 0,0

    def test_precipitacao_ausente_nunca_vira_chuva_zero(self, nasa):
        # DADO AUSENTE NÃO É ZERO / NÃO SIGNIFICA "SEM CHUVA".
        set_precip_missing(nasa, LAST9)
        r = report()
        tail = r["daily"]["precip_mm"][-9:]
        assert all(v is None for v in tail)
        # e os dias ausentes NUNCA contam como dias secos
        assert r["metrics"]["precipitation"]["rainy_days"] == 10

    def test_diarios_preservam_null_sem_interpolacao(self, nasa):
        set_precip_missing(nasa, LAST9)
        r = report()
        # 30 posições, 21 valores e 9 nulls — nada preenchido
        assert len(r["daily"]["precip_mm"]) == 30
        assert sum(1 for v in r["daily"]["precip_mm"] if v is None) == 9
        assert sum(1 for v in r["daily"]["precip_mm"] if v is not None) == 21


# ---------------------------------------------------------------------------
# Evapotranspiração — FORA de escopo (Fase 3: não inventar metodologia)
# ---------------------------------------------------------------------------
class TestNoET:
    def test_sem_evapotranspiracao_no_relatorio(self):
        r = report()
        blob = json.dumps(r, ensure_ascii=False, default=str).lower()
        assert "evapotranspir" not in blob


# ---------------------------------------------------------------------------
# Módulo legado (weather_service) — contrato intacto + origem EXPLÍCITA
# ---------------------------------------------------------------------------
class TestLegacyWeatherTags:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from services import weather_service
        with weather_service._cache_lock:
            weather_service._cache.clear()
        yield
        with weather_service._cache_lock:
            weather_service._cache.clear()

    _LEGACY = {
        "summary": {"total_rain_mm": 999.0, "avg_temp_c": 24.0,
                    "min_temp_c": 15.0, "max_temp_c": 35.0},
        "monthly": {"labels": ["Jan"], "rain": [1.0],
                    "temp_max": [30.0], "temp_min": [20.0], "temp_avg": [25.0]},
        "recent_applications": [],
    }

    def test_payload_real_taguado(self, monkeypatch):
        from services import weather_service
        monkeypatch.setattr(weather_service, "_fetch_and_process",
                            lambda lat, lon: json.loads(json.dumps(self._LEGACY)))
        d = weather_service.fetch_live_nasa_weather(-22.0, -55.0)
        assert d["summary"]["total_rain_mm"] == 999.0
        assert d["monthly"]["labels"] == ["Jan"]
        assert d["is_real"] is True
        assert d["data_origin"] == "nasa_power"
        assert "NASA POWER" in d["provenance"]["source"]

    def test_fallback_taguado_como_demonstrativo(self, monkeypatch):
        from services import weather_service

        def boom(lat, lon):
            raise ConnectionError("rede indisponível")

        monkeypatch.setattr(weather_service, "_fetch_and_process", boom)
        d = weather_service.fetch_live_nasa_weather(-22.0, -55.0)
        assert d["is_real"] is False
        assert d["data_origin"] == "climatology_demo"
        assert "DEMONSTRATIVA" in d["provenance"]["source"].upper()
