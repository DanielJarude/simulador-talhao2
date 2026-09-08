"""
3. Endpoints de Fazendas e Simulações:
   contratos Pydantic (criação/resposta, What-If), 422 para inputs inválidos,
   geoprocessamento (texturas dinâmicas e heightmap/DEM via endpoint).
"""
import pytest

from conftest import REPO_ROOT, auth

FARM_BODY = {
    "name": "Fazenda Contrato",
    "city": "Bauru - SP",
    "total_area": 30.0,
    "talhao_name": "Talhão Contrato",
    "crop": "Milho Safrinha",
    "latitude": -22.3,
    "longitude": -50.5,
}

WHATIF_FIELDS = {
    "delta_ndvi", "delta_yield_sc_ha", "total_production_sc",
    "financial_impact_brl", "displacement_scale_3d",
    "gross_financial_gain_brl", "net_financial_gain_brl",
    "crop", "bag_price_brl",
}


# ---------------------------------------------------------------------------
# Fazendas — contratos
# ---------------------------------------------------------------------------
class TestFarms:
    def test_criar_contrato_e_geoprocessamento(self, client, admin_token):
        r = client.post("/api/farms", json=FARM_BODY, headers=auth(admin_token))
        assert r.status_code == 201
        d = r.json()

        # Contrato de resposta
        assert d["name"] == FARM_BODY["name"]
        assert d["city"] == FARM_BODY["city"]
        assert d["total_area"] == FARM_BODY["total_area"]
        assert len(d["talhoes"]) == 1
        talhao = d["talhoes"][0]
        assert talhao["name"] == FARM_BODY["talhao_name"]
        assert talhao["crop"] == FARM_BODY["crop"]
        assert talhao["area"] == FARM_BODY["total_area"]

        # Consistência GET
        got = client.get(f"/api/farms/{d['id']}", headers=auth(admin_token)).json()
        assert got["name"] == d["name"] and got["talhoes"][0]["id"] == talhao["id"]

        # Geoprocessamento: texturas espectrais geradas em disco
        folder = REPO_ROOT / "dynamic_talhoes" / f"farm_{d['id']}_talhao_{talhao['id']}"
        for layer in ("ndvi", "evi", "ndre", "ndmi"):
            assert (folder / f"{layer}_cloudless_min_max.png").exists(), f"{layer} não gerado"

    @pytest.mark.parametrize("bad_field,bad_value", [
        ("latitude", 999.0),
        ("longitude", -181.0),
        ("total_area", -5.0),
        ("total_area", 0),
        ("name", "X"),          # min_length=2
        ("city", ""),
    ])
    def test_criar_inputs_invalidos_422(self, client, admin_token, bad_field, bad_value):
        body = {**FARM_BODY, bad_field: bad_value}
        r = client.post("/api/farms", json=body, headers=auth(admin_token))
        assert r.status_code == 422

    def test_get_inexistente_404(self, client, admin_token):
        assert client.get("/api/farms/999999", headers=auth(admin_token)).status_code == 404

    def test_texture_dinamica_url_e_arquivo(self, client, admin_token):
        fid = client.post("/api/farms", json=FARM_BODY, headers=auth(admin_token)).json()["id"]
        r = client.get(f"/api/talhao/{fid}/texture?layer=ndvi", headers=auth(admin_token))
        assert r.status_code == 200
        d = r.json()
        assert d["type"] == "dynamic"
        # URL autenticada do PNG (PR #3); termina em .png (antes do query string).
        assert "/api/talhao/" in d["texture_url"] and ".png" in d["texture_url"]
        # URL autenticada servida por rota com checagem de ownership.
        assert client.get(d["texture_url"], headers=auth(admin_token)).status_code == 200

    @pytest.mark.parametrize("layer", ["xyz", "NDVI", ""])
    def test_texture_layer_invalida_422(self, client, admin_token, layer):
        assert client.get(f"/api/talhao/1/texture?layer={layer}", headers=auth(admin_token)).status_code == 422


# ---------------------------------------------------------------------------
# Datas Sentinel-2 (fonte única de config)
# ---------------------------------------------------------------------------
class TestDates:
    def test_dates_endpoint(self, client):
        d = client.get("/api/talhao/dates").json()
        assert len(d["dates"]) == 13
        assert d["dates"][0] == "2025-04-07"
        assert d["indices"] == ["ndvi", "evi", "ndre", "ndmi"]


# ---------------------------------------------------------------------------
# Simulação What-If — contrato e regras
# ---------------------------------------------------------------------------
class TestWhatIf:
    def test_contrato_exatamente_9_campos(self, client, admin_token):
        r = client.post("/api/simulation/what-if", headers=auth(admin_token), json={
            "nitrogen_kg": 50, "water_mm": 10, "pest_pressure_pct": 5, "area_ha": 42.54,
        })
        assert r.status_code == 200
        d = r.json()
        assert set(d.keys()) == WHATIF_FIELDS
        for f in ("delta_ndvi", "delta_yield_sc_ha", "total_production_sc",
                  "financial_impact_brl", "displacement_scale_3d",
                  "gross_financial_gain_brl", "net_financial_gain_brl", "bag_price_brl"):
            assert isinstance(d[f], (int, float)), f
        assert d["crop"] == "Soja / Milho Safrinha"

    def test_matematica_crop_padrao(self, client, admin_token):
        d = client.post("/api/simulation/what-if", headers=auth(admin_token), json={
            "nitrogen_kg": 50, "water_mm": 10, "pest_pressure_pct": 5, "area_ha": 42.54,
        }).json()
        # Constantes de "Soja / Milho Safrinha": n=0.0015, w=0.0022, pest=0.0045, conv=45
        expected_ndvi = 50 * 0.0015 + 10 * 0.0022 - 5 * 0.0045   # 0.0745
        assert d["delta_ndvi"] == pytest.approx(round(expected_ndvi, 4))
        assert d["delta_yield_sc_ha"] == pytest.approx(round(expected_ndvi * 45, 2), abs=0.01)
        assert d["total_production_sc"] == pytest.approx((65.0 + d["delta_yield_sc_ha"]) * 42.54, abs=0.5)
        assert d["displacement_scale_3d"] == pytest.approx(max(1.0, 4.0 + d["delta_ndvi"] * 8.0), abs=1e-3)
        cost = 50 * 3.80 * 42.54 + 10 * 1.20 * 42.54
        assert d["net_financial_gain_brl"] == pytest.approx(
            d["delta_yield_sc_ha"] * 42.54 * 125.0 - cost, abs=0.05)

    def test_crop_especifico_usa_suas_constantes(self, client, admin_token):
        d = client.post("/api/simulation/what-if", headers=auth(admin_token), json={
            "nitrogen_kg": 30, "water_mm": 0, "pest_pressure_pct": 0, "area_ha": 100,
            "crop": "Milho Safrinha",
        }).json()
        assert d["crop"] == "Milho Safrinha"
        assert d["bag_price_brl"] == 60.0
        assert d["delta_ndvi"] == pytest.approx(round(30 * 0.0028, 4))

    @pytest.mark.parametrize("body", [
        {"nitrogen_kg": 9999, "water_mm": 0, "pest_pressure_pct": 0, "area_ha": 10},
        {"nitrogen_kg": -999, "water_mm": 0, "pest_pressure_pct": 0, "area_ha": 10},
        {"nitrogen_kg": 0, "water_mm": 999, "pest_pressure_pct": 0, "area_ha": 10},
        {"nitrogen_kg": 0, "water_mm": 0, "pest_pressure_pct": -1, "area_ha": 10},
        {"nitrogen_kg": 0, "water_mm": 0, "pest_pressure_pct": 101, "area_ha": 10},
        {"nitrogen_kg": 0, "water_mm": 0, "pest_pressure_pct": 0, "area_ha": 0},
        {"nitrogen_kg": "abc", "water_mm": 0, "pest_pressure_pct": 0, "area_ha": 10},
    ])
    def test_inputs_invalidos_422(self, client, admin_token, body):
        r = client.post("/api/simulation/what-if", json=body, headers=auth(admin_token))
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Analytics (série temporal / zoneamento / safras)
# ---------------------------------------------------------------------------
class TestAnalytics:
    def test_serie_temporal_farm_demo(self, client, admin_token):
        d = client.get("/api/analytics/farm/1?layer=ndvi", headers=auth(admin_token)).json()
        assert len(d["timeline"]) == 13
        for point in d["timeline"]:
            zones = point["zones"]
            assert set(zones) == {"stress", "medium", "good", "dense"}
            for name, z in zones.items():
                assert 0.0 <= z["pct"] <= 100.0
                assert z["ha"] == pytest.approx(z["pct"] / 100.0 * 42.54, abs=0.05)
            # zonas mutuamente exclusivas → somam 100% (margem de arredondamento)
            assert 99.5 <= sum(z["pct"] for z in zones.values()) <= 100.5
            assert 0.0 < point["mean"] < 1.0  # índice espectral fisicamente válido
        yp = d["yield_predictions"]
        assert yp["safra_verao"]["cultura"] == "Soja"
        assert yp["safrinha"]["cultura"] == "Milho Safrinha"
        assert yp["total_anual_faturamento_brl"] > 0

    def test_farm_inexistente_404(self, client, admin_token):
        assert client.get("/api/analytics/farm/999999", headers=auth(admin_token)).status_code == 404

    def test_layer_invalida_422(self, client, admin_token):
        assert client.get("/api/analytics/farm/1?layer=xyz", headers=auth(admin_token)).status_code == 422


# ---------------------------------------------------------------------------
# Laudo PDF
# ---------------------------------------------------------------------------
class TestPDF:
    def test_pdf_200(self, client, admin_token):
        r = client.get("/api/reports/farm/1/pdf?layer=ndvi&date_index=0&n_kg=50", headers=auth(admin_token))
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content[:4] == b"%PDF"
        assert len(r.content) > 1000

    def test_pdf_date_index_fora_da_faixa_422(self, client, admin_token):
        assert client.get("/api/reports/farm/1/pdf?date_index=99", headers=auth(admin_token)).status_code == 422
