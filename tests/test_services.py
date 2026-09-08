"""
4. Serviços auxiliares — testes unitários:
   cache do clima (TTL/fallback), regras de negócio da simulação,
   estimativa de safra, zoneamento espectral e paletas.
"""
import pytest

from conftest import REPO_ROOT
from services import analytics_service, satellite_service, simulation_service, weather_service


# ---------------------------------------------------------------------------
# Cache do clima (NASA POWER)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _limpa_cache_clima():
    with weather_service._cache_lock:
        weather_service._cache.clear()
    yield
    with weather_service._cache_lock:
        weather_service._cache.clear()


def _payload(tag: str) -> dict:
    return {"summary": {"tag": tag}, "monthly": {}, "recent_applications": []}


class TestWeatherCache:
    def test_cache_hit_evita_segunda_chamada(self, monkeypatch):
        calls = []

        def fake(lat, lon):
            calls.append((lat, lon))
            return _payload("live")

        monkeypatch.setattr(weather_service, "_fetch_and_process", fake)
        p1 = weather_service.fetch_live_nasa_weather(-22.0, -55.0)
        p2 = weather_service.fetch_live_nasa_weather(-22.0, -55.0)
        assert len(calls) == 1, "a 2ª chamada deveria vir do cache"
        assert p1 == p2

    def test_cache_separado_por_coordenadas(self, monkeypatch):
        calls = []

        def fake(lat, lon):
            calls.append((lat, lon))
            return _payload(f"live-{lat}")

        monkeypatch.setattr(weather_service, "_fetch_and_process", fake)
        weather_service.fetch_live_nasa_weather(-22.0, -55.0)
        weather_service.fetch_live_nasa_weather(-10.0, -40.0)
        assert len(calls) == 2

    def test_ttl_expirado_refaz_chamada(self, monkeypatch):
        calls = []

        def fake(lat, lon):
            calls.append(1)
            return _payload("live")

        monkeypatch.setattr(weather_service, "_fetch_and_process", fake)
        weather_service.fetch_live_nasa_weather(-22.0, -55.0)
        assert len(calls) == 1

        # Envelhece a entrada do cache além do TTL configurado
        with weather_service._cache_lock:
            key = next(iter(weather_service._cache))
            ts, payload = weather_service._cache[key]
            from config import settings
            weather_service._cache[key] = (ts - settings.nasa_weather_cache_minutes * 60 - 1, payload)

        weather_service.fetch_live_nasa_weather(-22.0, -55.0)
        assert len(calls) == 2, "entrada expirada deveria refazer a chamada"

    def test_fallback_em_erro_de_rede(self, monkeypatch):
        def boom(lat, lon):
            raise ConnectionError("rede indisponível")

        monkeypatch.setattr(weather_service, "_fetch_and_process", boom)
        d = weather_service.fetch_live_nasa_weather(-22.0, -55.0)
        assert d["monthly"]["labels"][0] == "Jan"       # payload do fallback
        assert d["recent_applications"] == []

    def test_fallback_tambem_e_cacheado(self, monkeypatch):
        calls = []

        def boom(lat, lon):
            calls.append(1)
            raise ConnectionError("rede indisponível")

        monkeypatch.setattr(weather_service, "_fetch_and_process", boom)
        weather_service.fetch_live_nasa_weather(-22.0, -55.0)
        weather_service.fetch_live_nasa_weather(-22.0, -55.0)
        assert len(calls) == 1, "fallback também deve ficar em cache"


# ---------------------------------------------------------------------------
# Regras de negócio — simulação what-if
# ---------------------------------------------------------------------------
class TestSimulationRules:
    def test_crop_padrao_constantes(self):
        d = simulation_service.calculate_what_if_impact(0, 0, 0, 10.0)
        assert d["crop"] == "Soja / Milho Safrinha"
        assert d["bag_price_brl"] == 125.0
        assert d["delta_ndvi"] == 0
        assert d["total_production_sc"] == pytest.approx(65.0 * 10.0)

    def test_crop_desconhecido_usa_modelo_padrao(self):
        d = simulation_service.calculate_what_if_impact(50, 0, 0, 10.0, crop="Pé-de-soja-inexistente")
        ref = simulation_service.calculate_what_if_impact(50, 0, 0, 10.0)
        assert d["delta_ndvi"] == ref["delta_ndvi"]
        assert d["bag_price_brl"] == ref["bag_price_brl"]
        assert d["crop"] == "Pé-de-soja-inexistente"  # nome preservado na resposta

    def test_custo_apenas_com_inputs_positivos(self):
        # N negativo NÃO devolve ureia: custo zero → líquido == bruto
        d = simulation_service.calculate_what_if_impact(-50, -10, 0, 10.0)
        assert d["gross_financial_gain_brl"] == d["net_financial_gain_brl"]
        assert d["financial_impact_brl"] == d["net_financial_gain_brl"]

    def test_pressao_de_pragas_negativa_ndvi(self):
        d = simulation_service.calculate_what_if_impact(0, 0, 40, 10.0)
        assert d["delta_ndvi"] < 0
        assert d["delta_yield_sc_ha"] < 0

    def test_displacement_scale_tem_piso_1_0(self):
        d = simulation_service.calculate_what_if_impact(-200, -100, 100, 10.0)
        assert d["displacement_scale_3d"] == 1.0  # piso, nunca < 1

    def test_adubacao_positiva_aumenta_produtividade(self):
        base = simulation_service.calculate_what_if_impact(0, 0, 0, 42.54)
        fert = simulation_service.calculate_what_if_impact(100, 0, 0, 42.54)
        assert fert["delta_yield_sc_ha"] > base["delta_yield_sc_ha"]
        assert fert["total_production_sc"] > base["total_production_sc"]


# ---------------------------------------------------------------------------
# Analytics — estimativa de safra e zoneamento
# ---------------------------------------------------------------------------
#: Dataset Sentinel-2 (fora do git por higiene — candidatos a LFS).
#: Quando ausente, o teste abaixo é pulado com aviso (clone limpo/CI).
_SENTINEL_NDVI_PNG = REPO_ROOT / "sentinel-21KXQ-2025-04-07" / "ndvi_cloudless_min_max.png"


class TestAnalyticsUnits:
    def test_estimate_seasonal_yield_matematica(self):
        timeline = [
            {"date": "2025-10-04", "mean": 0.80},
            {"date": "2025-12-10", "mean": 0.88},   # pico da safra de verão
            {"date": "2026-03-08", "mean": 0.72},   # pico da safrinha
        ]
        r = analytics_service.estimate_seasonal_yield(timeline, area_ha=42.54)
        v, s = r["safra_verao"], r["safrinha"]
        assert v["pico_ndvi"] == 0.88
        assert v["produtividade_sc_ha"] == pytest.approx(round(20.0 + 0.88 * 53.0, 1))
        assert s["pico_ndvi"] == 0.72
        assert s["produtividade_sc_ha"] == pytest.approx(round(25.0 + 0.72 * 90.0, 1))
        assert v["total_sacas"] == int(v["produtividade_sc_ha"] * 42.54)
        assert r["total_anual_faturamento_brl"] == pytest.approx(
            v["total_sacas"] * 130.0 + s["total_sacas"] * 60.0)

    @pytest.mark.skipif(
        not _SENTINEL_NDVI_PNG.exists(),
        reason="Dataset Sentinel-2 não presente neste ambiente (fora do git — ver README)",
    )
    def test_extract_layer_stats_em_textura_real(self):
        png = _SENTINEL_NDVI_PNG
        assert png.exists()
        stats = analytics_service.extract_layer_stats(str(png), total_area_ha=42.54)
        assert stats is not None
        zones = stats["zones"]
        assert set(zones) == {"stress", "medium", "good", "dense"}
        # classificação mutuamente exclusiva: pcts somam 100, ha somam a área
        for name, z in zones.items():
            assert 0.0 <= z["pct"] <= 100.0
            assert z["ha"] == pytest.approx(z["pct"] / 100.0 * 42.54, abs=0.05)
        assert 99.5 <= sum(z["pct"] for z in zones.values()) <= 100.5
        assert sum(z["ha"] for z in zones.values()) == pytest.approx(42.54, abs=0.1)
        assert 0.0 < stats["mean_index"] < 1.0

    def test_extract_layer_stats_arquivo_inexistente_retorna_none(self):
        assert analytics_service.extract_layer_stats("/caminho/que/nao/existe.png") is None


# ---------------------------------------------------------------------------
# Satellite — paletas espectrais
# ---------------------------------------------------------------------------
class TestSatelliteUnits:
    @pytest.mark.parametrize("layer", ["ndvi", "evi", "ndre", "ndmi"])
    @pytest.mark.parametrize("v", [0.0, 0.2, 0.5, 0.8, 1.0])
    def test_paleta_devolve_rgba_valido(self, layer, v):
        c = satellite_service.get_spectral_palette(layer, v)
        assert isinstance(c, list) and len(c) == 4
        assert all(isinstance(x, int) and 0 <= x <= 255 for x in c)
        assert c[3] == 255  # sempre opaco

    def test_paleta_clampa_fora_do_intervalo(self):
        assert satellite_service.get_spectral_palette("ndvi", -5.0) == \
            satellite_service.get_spectral_palette("ndvi", 0.0)
        assert satellite_service.get_spectral_palette("ndvi", 5.0) == \
            satellite_service.get_spectral_palette("ndvi", 1.0)

    def test_paletas_diferentes_por_camada(self):
        # o mesmo valor produz cores distintas por índice espectral
        assert satellite_service.get_spectral_palette("ndvi", 0.4) != \
            satellite_service.get_spectral_palette("ndmi", 0.4)
