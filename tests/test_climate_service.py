"""
PR #7 — Serviço climático consolidado (climate_service) — testes UNITÁRIOS.

A NASA POWER é SEMPRE simulada (monkeypatch de requests.get): a suíte não
depende da disponibilidade real da NASA. Cobrem:

- consulta NASA POWER (URL, parâmetros, comunidade AG, período, coordenadas);
- parsing da resposta real (fill -999.0 → None; unidades preservadas);
- períodos (inválido, máx. 366 dias, anterior a 1981);
- agregações (chuva, temperatura, radiação, umidade, vento + conversão m/s→km/h);
- baseline histórico (mesma janela, N anos; desvio; classificação conservadora);
- cache (chave por localização+período+parâmetros; TTL; falha não cacheada);
- timeout / erro HTTP / resposta sem dados / dados parcialmente ausentes;
- localização inválida; confiança (critérios objetivos);
- interpretações conservadoras (sem causalidade/diagnóstico);
- ausência de ET (fora de escopo — não se inventa metodologia).
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


def nasa_payload(start: dt.date, end: dt.date, fill_dates: set[dt.date] | None = None,
                 profile=None) -> dict:
    """profile(day) -> dict[param, float|None]. None no dict = ausente (sem chave)."""
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
        captured, behavior = nasa
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


# ---------------------------------------------------------------------------
# Agregações
# ---------------------------------------------------------------------------
class TestAggregations:
    def test_precipitacao(self):
        r = report()
        p = r["metrics"]["precipitation"]
        # Janela 31/jul–29/ago (30 dias): dias secos = {3,6,9,10,11,12,13,14,15,16,18,21,24,27/ago}
        # (dia%3==0 ∪ dias 10–16, interseção 12/15) → 16 dias com 2,5 mm = 40,0 mm
        assert p["accumulated_mm"] == pytest.approx(40.0, abs=0.05)
        assert p["rainy_days"] == 16
        assert p["rainy_day_threshold_mm"] == settings.nasa_climate_rainy_day_threshold_mm
        assert p["unit"] == "mm"
        assert p["category"] == cs.CATEGORY_CALC

    def test_sequencia_seca(self):
        r = report()
        # dias 09..16/ago consecutivos sem chuva (09/3 + 10..16) → streak de 8
        assert r["metrics"]["precipitation"]["longest_dry_streak_days"] == 8

    def test_temperatura(self):
        r = report()
        t = r["metrics"]["temperature"]
        assert t["mean_c"] == pytest.approx(23.0)
        assert t["min_c"] == pytest.approx(15.0)
        assert t["max_c"] == pytest.approx(31.0)
        assert t["amplitude_mean_c"] == pytest.approx(16.0)

    def test_radiação_media_e_acumulada(self):
        r = report()
        rad = r["metrics"]["radiation"]
        assert rad["daily_mean_mj_m2"] == pytest.approx(14.0)
        assert rad["accumulated_mj_m2"] == pytest.approx(14.0 * 30, abs=0.1)
        assert "MJ/m^2/day" in rad["unit"]

    def test_umidade(self):
        r = report()
        h = r["metrics"]["humidity"]
        assert h["mean_pct"] == pytest.approx(65.0)
        assert h["min_pct"] == pytest.approx(65.0)
        assert h["max_pct"] == pytest.approx(65.0)

    def test_vento_conversao_ms_para_kmh(self):
        r = report()
        w = r["metrics"]["wind"]
        assert w["mean_ms"] == pytest.approx(2.0)
        assert w["mean_kmh"] == pytest.approx(2.0 * 3.6, abs=0.05)
        assert "média diária" in w["note"].lower()  # máximo = maior entre médias diárias

    def test_agregacoes_com_lacunas_nao_imputam(self, nasa):
        _, behavior = nasa
        fill = {d for d in [START + dt.timedelta(days=i) for i in range(3)]}
        behavior["handler"] = lambda s, e: nasa_payload(s, e, fill_dates=fill)
        r = report()
        assert r["data_quality"]["coverage_pct"] == pytest.approx(90.0)
        assert r["metrics"]["temperature"]["mean_c"] == pytest.approx(23.0)  # só dias válidos
        assert 3 == len(r["data_quality"]["missing_days"])


# ---------------------------------------------------------------------------
# Baseline histórico
# ---------------------------------------------------------------------------
class TestBaseline:
    def test_referencia_e_desvio(self):
        r = report()
        b = r["baseline"]
        assert b["years"] == 5
        assert b["requested_years"] == 5
        # referência = 4,0 mm/dia x 30 = 120,0 mm
        assert b["reference"]["precip_accumulated_mm"] == pytest.approx(120.0)
        assert b["reference"]["temp_mean_c"] == pytest.approx(22.0)
        p = r["metrics"]["precipitation"]["baseline"]
        assert p["deviation"] == pytest.approx(40.0 - 120.0, abs=0.05)
        assert p["deviation_pct"] < -20
        assert p["classification"] == "abaixo_da_referencia"
        assert p["classification_label"] == "abaixo da referência histórica"
        assert "NÃO" in b["methodology"].upper() or "não" in b["methodology"]
        assert "climatológica oficial" in b["methodology"]

    def test_mesma_janela_do_calendario_por_ano(self, nasa):
        captured, _ = nasa
        report()
        windows = [c["window"] for c in captured]
        # 1 (período atual) + 5 (baseline)
        assert len(windows) == 6
        for s, e in windows[1:]:
            assert (s.month, s.day) == (START.month, START.day)
            assert (e.month, e.day) == (END.month, END.day)
            assert s.year < START.year

    def test_ano_invalido_nao_contamina_referencia(self, nasa):
        captured, behavior = nasa
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
        assert r["metrics"]["precipitation"]["baseline"]["classification"] is None


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
        # recupera: a próxima tentativa consulta de novo (nada em cache)
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
        assert r["confidence"]["level"] == "limitada"

    def test_dados_parciais_status_partial(self, nasa):
        _, behavior = nasa
        fill = {START + dt.timedelta(days=i) for i in range(15)}
        behavior["handler"] = lambda s, e: nasa_payload(s, e, fill_dates=fill)
        r = report()
        assert r["status"] == "partial"
        assert r["data_quality"]["coverage_pct"] == pytest.approx(50.0)
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
# Confiança (Fase 8)
# ---------------------------------------------------------------------------
class TestConfidence:
    def test_alta_com_dados_completos(self):
        r = report()
        assert r["confidence"]["level"] == "alta"
        assert r["confidence"]["coverage_pct"] == 100.0
        assert r["confidence"]["valid_baseline_years"] == 5

    def test_media_com_lacunas_90_a_98(self, nasa):
        _, behavior = nasa
        fill = {dt.date(2026, 8, 1), dt.date(2026, 8, 2)}  # 93,3%
        behavior["handler"] = lambda s, e: nasa_payload(s, e, fill_dates=fill)
        r = report()
        assert r["confidence"]["level"] == "media"
        assert any("cobertura" in x for x in r["confidence"]["reasons"])

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

    def test_critérios_documentados(self):
        r = report()
        assert "≥98%" in r["confidence"]["criteria"] or "98%" in r["confidence"]["criteria"]
        assert "90" in r["confidence"]["criteria"]


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
            assert it["text"]
            for banned in self.BANNED:
                assert banned not in it["text"].lower()

    def test_chuva_abaixo_referencia_linguagem_conservadora(self):
        r = report()
        precip = next(i for i in r["interpretations"] if i["topic"] == "Precipitação")
        assert "abaixo da referência histórica" in precip["text"]
        assert "Os dados indicam" in precip["text"] or "compatível" in precip["text"]
        assert "evidência suficiente" in precip["text"]  # não afirma estresse

    def test_sem_dados_diz_que_não_há_evidencia(self, nasa):
        _, behavior = nasa
        fill = {START + dt.timedelta(days=i) for i in range(30)}
        behavior["handler"] = lambda s, e: nasa_payload(s, e, fill_dates=fill)
        r = report()
        all_text = " ".join(i["text"] for i in r["interpretations"]).lower()
        assert "não há dados suficientes" in all_text

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
        # contrato legado preservado
        assert d["summary"]["total_rain_mm"] == 999.0
        assert d["monthly"]["labels"] == ["Jan"]
        # PR #7 — origem explícita
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
