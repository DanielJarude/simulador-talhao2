"""PR #8 — Saúde & Evolução da Lavoura.

Serviços externos são simulados: nenhuma chamada real à Copernicus ou à NASA
é necessária para validar fórmulas, qualidade, tendência, zonas e contratos.
"""
from datetime import date

import numpy as np
import pytest

from conftest import auth, login, register_user, unique_email
from services import crop_health_service as health
from services.copernicus_service import SceneInfo, evi, gndvi, ndmi, ndre, ndvi, ndwi, savi


def scene(day: str, cloud: float = 3.0) -> SceneInfo:
    return SceneInfo(
        item_id=f"S2_{day}", acquisition_date=day, datetime=f"{day}T10:00:00Z",
        cloud_cover=cloud, platform="sentinel-2", constellation="sentinel-2",
        collection="sentinel-2-l2a", geometry=None, bbox=None, assets={},
    )


def test_formulas_health_and_division_by_zero():
    assert ndvi(0.8, 0.2) == pytest.approx(0.6)
    assert ndre(0.75, 0.25) == pytest.approx(0.5)
    assert savi(0.8, 0.2) == pytest.approx(((0.8 - 0.2) / (0.8 + 0.2 + 0.5)) * 1.5)
    assert evi(0.8, 0.2, 0.1) == pytest.approx(2.5 * 0.6 / (0.8 + 1.2 - 0.75 + 1))
    assert ndwi(0.7, 0.3) == pytest.approx(0.4)
    assert ndmi(0.7, 0.3) == pytest.approx(0.4)
    assert gndvi(0.7, 0.3) == pytest.approx(0.4)
    for value in (ndvi(0, 0), ndre(0, 0), savi(0, 0), evi(0, 0, 0), ndwi(0, 0), gndvi(0, 0)):
        assert np.isfinite(value)


def test_quality_uses_valid_pixels_and_clouds():
    assert health.classify_scene_quality(94, 3)["level"] == "alta"
    assert health.classify_scene_quality(61, 28)["level"] == "limitada"
    assert health.classify_scene_quality(0, 1)["level"] == "insuficiente"
    # Nuvem desconhecida não pode virar qualidade alta.
    assert health.classify_scene_quality(95, None)["level"] == "media"


def test_change_zones_use_real_pair_and_area():
    delta = np.array([[0.12, -0.11, 0.0, np.nan], [0.06, -0.02, 0.03, 0.2]], dtype=float)
    polygon = np.ones((2, 4), dtype=bool)
    valid = np.ones((2, 4), dtype=bool)
    result = health.compute_change_zones(delta, valid, polygon, area_ha=10, index="ndvi")
    assert result["improved"]["pixels"] >= 2
    assert result["declined"]["pixels"] >= 1
    assert result["unobserved"]["pixels"] == 1
    assert result["improved"]["ha"] + result["declined"]["ha"] + result["stable"]["ha"] + result["unobserved"]["ha"] == pytest.approx(10)
    assert "não extrapola" not in result["method"].lower()  # método é numérico, não diagnóstico


def test_trend_requires_three_good_scenes_and_absolute_delta():
    two = [
        {"date": "2026-08-01", "mean": 0.62, "quality": "alta"},
        {"date": "2026-08-16", "mean": 0.55, "quality": "alta"},
    ]
    assert health.classify_trend(two)["code"] == "dados_insuficientes"
    three = [
        {"date": "2026-08-01", "mean": 0.62, "quality": "alta"},
        {"date": "2026-08-16", "mean": 0.58, "quality": "media"},
        {"date": "2026-09-01", "mean": 0.54, "quality": "alta"},
    ]
    result = health.classify_trend(three)
    assert result["code"] == "queda"
    assert result["delta"] == pytest.approx(-0.08)
    assert "%" not in result["text"]


def test_internal_anomaly_uses_own_talhao_baseline():
    values = np.array([[0.7, 0.7, 0.5, 0.7]], dtype=float)
    result = health.compute_internal_anomaly(values, np.ones_like(values, dtype=bool), 4.0)
    assert result["status"] == "ok"
    assert result["baseline"] == "mediana dos pixels válidos da própria cena"
    assert result["below_internal_baseline"]["pixels"] == 1
    assert result["low_zone_gap_to_median"] is not None


def test_persistence_needs_three_scenes_and_detects_direction():
    polygon = np.ones((2, 2), dtype=bool)
    records = []
    for day, values in (
        ("2026-08-01", np.full((2, 2), 0.70)),
        ("2026-08-16", np.full((2, 2), 0.62)),
        ("2026-09-01", np.full((2, 2), 0.54)),
    ):
        records.append({
            "date": day, "values": values, "valid_mask": polygon.copy(),
            "polygon_mask": polygon.copy(), "stats": {"mean": float(values.mean())},
        })
    result = health.compute_persistence(records, 4.0)
    assert result["status"] == "ok"
    assert result["declining"]["pixels"] == 4
    assert result["declining"]["ha"] == pytest.approx(4.0)
    assert "não confirma problema agronômico" in result["text"]


def test_timeline_uses_stac_dates_only_and_returns_quality(monkeypatch):
    health.clear_caches()
    calendar = [
        {"date": "2026-08-01", "datetime": "2026-08-01T10:00:00Z", "cloud_cover": 3.0, "product_id": "a"},
        {"date": "2026-08-16", "datetime": "2026-08-16T10:00:00Z", "cloud_cover": 4.0, "product_id": "b"},
        {"date": "2026-09-01", "datetime": "2026-09-01T10:00:00Z", "cloud_cover": 3.0, "product_id": "c"},
    ]
    monkeypatch.setattr(health.cdse, "fetch_real_calendar", lambda **kwargs: (calendar, "ok", None))

    def fake_process(*args, **kwargs):
        sc = kwargs["scene"]
        amount = {"2026-08-01": 0.70, "2026-08-16": 0.62, "2026-09-01": 0.54}[sc.acquisition_date]
        values = np.full((4, 4), amount, dtype=float)
        mask = np.ones((4, 4), dtype=bool)
        return {"date": sc.acquisition_date, "values": values, "valid_mask": mask, "polygon_mask": mask, "stats": {"mean": amount, "median": amount, "min": amount, "max": amount, "p25": amount, "p75": amount, "valid_pixel_pct": 100.0}, "scene": sc}

    monkeypatch.setattr(health.cdse, "process_farm_scene", fake_process)
    result = health.get_health_timeline(1, 1, -22.7, -51.2, 4.0, None, period_days=60, limit=10)
    assert result["status"] == "ok"
    assert [p["date"] for p in result["timeline"]] == ["2026-08-01", "2026-08-16", "2026-09-01"]
    assert result["metrics"]["trend"] == "queda"
    assert all(p["source"] == "sentinel-cdse" for p in result["timeline"])
    assert result["timeline"][0]["quality"] == "alta"
    assert result["timeline"][2]["delta_previous"] == pytest.approx(-0.08)


def test_timeline_cache_avoids_repeating_calendar(monkeypatch):
    health.clear_caches()
    calendar_calls = []
    calendar = [{"date": "2026-08-01", "datetime": "2026-08-01T10:00:00Z", "cloud_cover": 3.0, "product_id": "a"}]
    monkeypatch.setattr(health.cdse, "fetch_real_calendar", lambda **kwargs: (calendar_calls.append(1) or calendar, "ok", None))
    mask = np.ones((2, 2), dtype=bool)
    monkeypatch.setattr(health.cdse, "process_farm_scene", lambda *args, **kwargs: {"date": "2026-08-01", "values": np.full((2, 2), .6), "valid_mask": mask, "polygon_mask": mask, "stats": {"mean": .6, "median": .6, "min": .6, "max": .6, "p25": .6, "p75": .6, "valid_pixel_pct": 100}, "scene": kwargs["scene"]})
    health.get_health_timeline(2, 2, -22.7, -51.2, 4.0, None, period_days=60, limit=10)
    health.get_health_timeline(2, 2, -22.7, -51.2, 4.0, None, period_days=60, limit=10)
    assert len(calendar_calls) == 1


def test_compare_ab_returns_delta_zones_and_real_map(monkeypatch):
    health.clear_caches()
    calendar = [
        {"date": "2026-08-01", "datetime": "2026-08-01T10:00:00Z", "cloud_cover": 3.0, "product_id": "a"},
        {"date": "2026-08-16", "datetime": "2026-08-16T10:00:00Z", "cloud_cover": 3.0, "product_id": "b"},
    ]
    monkeypatch.setattr(health.cdse, "fetch_real_calendar", lambda **kwargs: (calendar, "ok", None))
    mask = np.ones((4, 4), dtype=bool)
    def fake_process(*args, **kwargs):
        scene_arg = kwargs.get("scene") or args[6]
        amount = .5 if scene_arg.acquisition_date == "2026-08-01" else .62
        values = np.full((4, 4), amount)
        return {"date": scene_arg.acquisition_date, "values": values, "valid_mask": mask, "polygon_mask": mask, "stats": {"mean": amount, "median": amount, "min": amount, "max": amount, "p25": amount, "p75": amount, "valid_pixel_pct": 100}, "scene": scene_arg}
    monkeypatch.setattr(health.cdse, "process_farm_scene", fake_process)
    result = health.compare_dates(8, 8, -22.7, -51.2, 4.0, None, "2026-08-01", "2026-08-16", include_climate=False)
    assert result["status"] == "ok"
    assert result["comparison"]["delta_mean"] == pytest.approx(.12)
    assert result["comparison"]["zones"]["improved"]["ha"] == pytest.approx(4.0)
    assert result["climate_context"]["status"] == "not_requested"
    assert result["_difference_path"]


def test_climate_unavailable_does_not_break_spectral_context(monkeypatch):
    def unavailable(*args, **kwargs):
        raise health.ClimateSourceError("NASA fora")
    monkeypatch.setattr(health, "build_climate_report", unavailable)
    context = health._climate_context(-22.7, -51.2, date(2026, 8, 1), date(2026, 8, 16))
    assert context["status"] == "unavailable"
    assert "continua" in context["message"]


def test_crop_health_endpoint_requires_authentication(client):
    response = client.get("/api/farms/1/crop-health/timeline")
    assert response.status_code == 401


def test_crop_health_endpoint_respects_ownership(client):
    owner_email = unique_email("health-owner")
    other_email = unique_email("health-other")
    register_user(client, owner_email)
    register_user(client, other_email)
    owner_token = login(client, owner_email, "abc12345")["access_token"]
    other_token = login(client, other_email, "abc12345")["access_token"]
    farm = client.post("/api/farms", json={
        "name": "Fazenda Saúde Privada", "city": "Marília - SP", "total_area": 4.0,
        "talhao_name": "Talhão privado", "crop": "Soja", "latitude": -22.2, "longitude": -49.9,
    }, headers=auth(owner_token))
    assert farm.status_code == 201
    farm_id = farm.json()["id"]
    response = client.get(f"/api/farms/{farm_id}/crop-health/timeline", headers=auth(other_token))
    assert response.status_code == 404


def test_crop_health_endpoint_is_explicit_when_cdse_unavailable(client, admin_token):
    response = client.get("/api/farms/1/crop-health/timeline", headers=auth(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"insufficient_data", "unavailable"}
    assert "Não há dados suficientes" in (body.get("message") or "") or "indisponíveis" in (body.get("message") or "")
    assert body["timeline"] == []
    assert body["metrics"]["trend"] == "dados_insuficientes"
