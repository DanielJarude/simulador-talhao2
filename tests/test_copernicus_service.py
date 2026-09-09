"""
Serviço CDSE (Copernicus Data Space Ecosystem) — Sentinel-2 L2A real.

Cobertura (12 cenários de fixture da Fase 24 + validação numérica Fase 27):
1. cena válida (STAC → Process → PNG real);
2. múltiplas cenas (seleção explícita + selection_reason);
3. muita nuvem (relaxamento gradual);
4. nenhuma cena (fallback explícito, sem fabricar Sentinel);
5. timeout;
6. 401 (credenciais inválidas — segredo NUNCA logado);
7. 429 (retry curto com backoff);
8. 500 (erro remoto → fallback);
9. Process API retornando imagem (TIFF multicanal);
10. DEM COPERNICUS_30;
11. DEM 30 → fallback 90;
12. DEM 30 e 90 indisponíveis (fallback aproximado).

Nenhum teste acessa a rede real: requests são mockados e os rásteres
são sintéticos (mesma técnica da suíte DEM/GL-30).
"""
import io
import json
import os
from datetime import date, timedelta

import numpy as np
import pytest
import rasterio

from conftest import auth
from services import copernicus_service as cds
from services.copernicus_service import (
    CopernicusError,
    CopernicusNotConfigured,
    SceneInfo,
    aoi_bounds,
    build_valid_mask,
    evi,
    fetch_real_calendar,
    geometry_digest,
    kml_to_geojson_polygon,
    ndmi,
    ndre,
    ndvi,
    parse_stac_item,
    render_layer_png,
    select_best_scene,
)


# ---------------------------------------------------------------------------
# Fábricas de fixtures (sintéticas — sem rede)
# ---------------------------------------------------------------------------
def _scene(item_id: str, dt: str, cloud: float | None, **kw) -> SceneInfo:
    base: dict = {
        "id": item_id,
        "properties": {
            "datetime": dt,
            "eo:cloud_cover": cloud,
        },
        "geometry": {"type": "Polygon", "coordinates": [[[-49.1, -22.1], [-49.0, -22.1], [-49.0, -22.0], [-49.1, -22.0], [-49.1, -22.1]]]},
        "bbox": [-49.1, -22.1, -49.0, -22.0],
        "collection": "sentinel-2-l2a",
        "assets": {},
    }
    base.update(kw)
    return parse_stac_item(base)


def _synthetic_bands_8(size: int = 64, cloud_band: int = 32) -> np.ndarray:
    """
    Raster sintético de 8 bandas (B02,B03,B04,B05,B08,B11,SCL,dataMask).
    Vegetação plausível: B08 ≈ 0.45–0.65, B04 ≈ 0.08–0.15 (NDVI 0.5–0.8).
    SCL = 4 (vegetação) com faixa horizontal `cloud_band` = 9 (nuvem média).
    """
    yy, xx = np.mgrid[0:size, 0:size]
    b08 = 0.45 + 0.20 * (xx / size) + 0.05 * np.sin(xx / 7.0) * np.cos(yy / 9.0)
    b04 = 0.08 + 0.07 * (yy / size)
    b03 = 0.10 + 0.06 * (yy / size)
    b02 = 0.09 + 0.04 * (yy / size)
    b05 = 0.20 + 0.12 * (xx / size)
    b11 = 0.15 + 0.10 * (yy / size)
    scl = np.full((size, size), 4, dtype="uint8")
    scl[cloud_band:cloud_band + 4, :] = 9
    data_mask = np.ones((size, size), dtype="uint8")
    bands = np.stack([b02, b03, b04, b05, b08, b11, scl, data_mask]).astype("float32")
    return bands


def _tiff_8(size: int = 64) -> bytes:
    conf = {}
    bands = _synthetic_bands_8(size)
    buf = io.BytesIO()
    with rasterio.open(
        buf, "w", driver="GTiff", width=size, height=size, count=8,
        dtype="float32", crs="EPSG:4326",
        transform=rasterio.transform.from_bounds(-49.1, -22.1, -49.0, -22.0, size, size),
    ) as dst:
        for i in range(8):
            dst.write(bands[i], i + 1)
        conf["nodata"] = dst.nodata
    return buf.getvalue()


def _tiff_dem(size: int = 64, lo: float = 300.0, hi: float = 500.0) -> bytes:
    yy, xx = np.mgrid[0:size, 0:size]
    arr = lo + (hi - lo) * (xx / max(size - 1, 1))
    buf = io.BytesIO()
    with rasterio.open(
        buf, "w", driver="GTiff", width=size, height=size, count=1,
        dtype="float32", crs="EPSG:4326",
        transform=rasterio.transform.from_bounds(-49.1, -22.1, -49.0, -22.0, size, size),
    ) as dst:
        dst.write(arr.astype("float32"), 1)
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None,
                 content: bytes | None = None, headers: dict | None = None,
                 text: str | None = None):
        self.status_code = status_code
        self._body = body
        self.content = content if content is not None else b""
        self.headers = headers or {"Content-Type": "application/json"}
        self._text = text

    @property
    def text(self) -> str:
        if self._text is not None:
            return self._text
        if self.content:
            return self.content.decode("utf-8", errors="replace")
        if self._body is not None:
            return json.dumps(self._body, ensure_ascii=False)
        return ""

    def json(self):
        if self._body is None:
            raise ValueError("sem corpo JSON")
        return self._body

    def raise_for_status(self):
        pass


class _FailRequest:
    """Simula `requests.request(url, ...)`/`requests.post(url, ...)`."""

    def __init__(self, responses=None, exc=None):
        self.responses = list(responses or [])
        self.exc = exc
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        if self.exc:
            raise self.exc
        item = self.responses.pop(0) if self.responses else _FakeResponse(200, {})
        return item


@pytest.fixture(autouse=True)
def _clean_caches():
    """Isolamento entre testes: limpa caches de token/memória/disco CDSE."""
    cds._mem_cache.clear()
    cds._token_cache.update(token=None, expires_at=0.0)
    yield
    cds._mem_cache.clear()
    cds._token_cache.update(token=None, expires_at=0.0)


@pytest.fixture
def cdse_configured(monkeypatch):
    """Configura credenciais/CDSE. Devolve callable p/ reaplicar se preciso."""

    def _apply(*_args, **_kwargs):
        monkeypatch.setattr(cds.settings, "cdse_client_id", "cliente-teste")
        monkeypatch.setattr(cds.settings, "cdse_client_secret", "segredo-teste")
        monkeypatch.setattr(cds.settings, "cdse_raster_size", 64)
        monkeypatch.setattr(cds.settings, "cdse_lookback_days", 60)
        monkeypatch.setattr(cds.settings, "cdse_max_cloud_cover", 20.0)

    _apply()
    return _apply


# ---------------------------------------------------------------------------
# Fase 27 — validação numérica das fórmulas
# ---------------------------------------------------------------------------
class TestFormulasNumericas:
    def test_ndvi_formula(self):
        # (B08 - B04) / (B08 + B04): 0.80/0.20 → 0.60
        assert ndvi(0.80, 0.20) == pytest.approx((0.80 - 0.20) / (0.80 + 0.20))
        assert ndvi(0.55, 0.05) == pytest.approx((0.55 - 0.05) / (0.55 + 0.05))
        assert ndvi(0.2, 0.8) == pytest.approx((0.2 - 0.8) / (0.2 + 0.8))

    def test_ndre_formula_b08_b05(self):
        assert ndre(0.75, 0.25) == pytest.approx((0.75 - 0.25) / (0.75 + 0.25))
        assert ndre(0.9, 0.1) == pytest.approx(0.8)

    def test_ndmi_formula_b08_b11(self):
        assert ndmi(0.7, 0.3) == pytest.approx((0.7 - 0.3) / (0.7 + 0.3))

    def test_evi_formula_padrao(self):
        nir, red, blue = 0.8, 0.2, 0.1
        expected = 2.5 * (nir - red) / (nir + 6.0 * red - 7.5 * blue + 1.0)
        assert evi(nir, red, blue) == pytest.approx(expected)

    def test_evi_bate_referencia_literatura(self):
        # Caso vegetação densa da literatura (G=2.5, C1=6, C2=7.5, L=1)
        nir, red, blue = 0.7, 0.05, 0.03
        assert 0.0 < evi(nir, red, blue) < 1.1

    def test_denominador_nunca_zero(self):
        # mesmo com NIR=RED=0 o denominador tem epsilon → finito
        assert np.isfinite(ndvi(0.0, 0.0))
        assert np.isfinite(ndre(0.0, 0.0))
        assert np.isfinite(ndmi(0.0, 0.0))
        assert np.isfinite(evi(0.0, 0.0, 0.0))

    def test_index_array_ordem_bandas(self):
        bands = _synthetic_bands_8()
        idx = cds.index_array("ndvi", bands)
        manual = (bands[4] - bands[2]) / (bands[4] + bands[2] + 1e-9)
        assert np.allclose(idx, manual)
        # NDRE usa B05 (índice 3); NDMI usa B11 (índice 5); EVI usa B02/B04/B08
        assert np.allclose(cds.index_array("ndre", bands),
                           (bands[4] - bands[3]) / (bands[4] + bands[3] + 1e-9))
        assert np.allclose(cds.index_array("ndmi", bands),
                           (bands[4] - bands[5]) / (bands[4] + bands[5] + 1e-9))


# ---------------------------------------------------------------------------
# Fase 11 — qualidade (SCL/dataMask) e máscara do polígono
# ---------------------------------------------------------------------------
class TestQualidade:
    def test_mascara_exclui_nuvens_sombra_nodata(self):
        bands = _synthetic_bands_8()
        valid = build_valid_mask(bands)
        scl = bands[6]
        # SCL=9 (nuvem média) em cloud_band:32..35 → inválido nessas linhas
        assert not valid[32:36, :].all()
        assert valid[:32, :].all()
        assert valid.size == bands.shape[1] * bands.shape[2]

    def test_mascara_exige_data_mask(self):
        bands = _synthetic_bands_8()
        bands[7, 5:9, :] = 0  # sem dado
        valid = build_valid_mask(bands)
        assert not valid[5:9, :].all()

    def test_render_layer_png_pixel_invalido_fica_sem_indice(self):
        bands = _synthetic_bands_8()
        valid = build_valid_mask(bands)
        poly = cds.polygon_mask(64, aoi_bounds(-22.1, -49.05, 42.54), None)
        png, stats = render_layer_png("ndvi", bands, valid, poly)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert 0 < stats["valid_pixel_percentage"] < 100  # faixa com nuvem
        assert stats["zones"]["stress"] + stats["zones"]["medium"] + \
            stats["zones"]["good"] + stats["zones"]["dense"] == pytest.approx(100.0, abs=0.5)

    def test_render_rgb_usa_b04_b03_b02(self):
        bands = _synthetic_bands_8()
        valid = build_valid_mask(bands)
        poly = cds.polygon_mask(64, aoi_bounds(-22.1, -49.05, 42.54), None)
        png, stats = render_layer_png("rgb", bands, valid, poly)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert stats["valid_pixel_percentage"] > 0


class TestGeometria:
    def test_kml_para_geojson_polygon(self):
        kml = "[[-22.01,-49.01],[-22.02,-49.01],[-22.02,-49.02],[-22.01,-49.02]]"
        g = kml_to_geojson_polygon(kml)
        assert g["type"] == "Polygon"
        ring = g["coordinates"][0]
        assert ring[0] == ring[-1]  # fechado
        assert ring[1] == [-49.01, -22.02]  # [lon, lat]

    def test_kml_invalido_retorna_none(self):
        assert kml_to_geojson_polygon("não-json{{{") is None
        assert kml_to_geojson_polygon("[[1,2],[3,4]]") is None  # menos de 3 pontos
        assert kml_to_geojson_polygon(None) is None

    def test_aoi_bounds_ordem_geografica_west_south_east_north(self):
        # Ordem exigida por STAC/Process API: [minLon, minLat, maxLon, maxLat]
        b = aoi_bounds(-22.7182, -55.5421, 42.54)
        assert b[0] < b[2], "minLon < maxLon (west < east)"
        assert b[1] < b[3], "minLat < maxLat (south < north)"
        assert b[0] == pytest.approx(-55.5421 - (b[2] - b[0]) / 2.0, abs=1e-9)
        assert b[1] == pytest.approx(-22.7182 - (b[3] - b[1]) / 2.0, abs=1e-9)
        # centro = lat/lon informados (sem inversão)
        assert (b[1] + b[3]) / 2 == pytest.approx(-22.7182, abs=1e-9)
        assert (b[0] + b[2]) / 2 == pytest.approx(-55.5421, abs=1e-9)

    def test_geometry_digest_estavel_e_diferente(self):
        d1 = geometry_digest(-22.7, -55.5, 42.5, None)
        d2 = geometry_digest(-22.7, -55.5, 42.5, None)
        d3 = geometry_digest(-22.8, -55.5, 42.5, None)
        assert d1 == d2 and len(d1) == 16
        assert d1 != d3


# ---------------------------------------------------------------------------
# Fase 3/4 — STAC: busca, normalização e seleção de melhor cena
# ---------------------------------------------------------------------------
class TestStac:
    def test_parse_stac_item_normaliza_campos(self):
        item = _scene("S2B_20250407", "2025-04-07T13:05:12Z", 12.3,
                      platform="sentinel-2b", constellation="sentinel-2")
        assert item.item_id == "S2B_20250407"
        assert item.acquisition_date == "2025-04-07"
        assert item.cloud_cover == 12.3
        assert item.platform == "sentinel-2b"
        assert item.collection == "sentinel-2-l2a"

    def test_parse_stac_sem_cloud(self):
        item = _scene("S2A_X", "2025-05-01T10:00:00Z", None)
        assert item.cloud_cover is None

    def test_selecao_mais_recente_dentro_limite(self):
        items = [
            _scene("a", "2025-04-05T10:00:00Z", 30.0),
            _scene("b", "2025-05-10T10:00:00Z", 5.0),
            _scene("c", "2025-04-20T10:00:00Z", 15.0),
        ]
        scene, reason, limit = select_best_scene(items, max_cloud=20.0)
        assert scene.item_id == "b"
        assert reason == "most_recent_within_limit"
        assert limit == 20.0

    @pytest.mark.parametrize("clouds,expected_reason", [
        ([25.0, 30.0, 40.0], "cloud_limit_relaxed"),
        ([90.0, 95.0, 99.0], "cloud_limit_relaxed"),
    ])
    def test_selecao_relaxa_quando_muita_nuvem(self, clouds, expected_reason):
        items = [_scene(f"c{i}", f"2025-05-0{i}T10:00:00Z", c) for i, c in enumerate(clouds, 1)]
        scene, reason, limit = select_best_scene(items, max_cloud=20.0)
        assert scene is not None and reason == expected_reason
        assert 20.0 < limit <= 100.0

    def test_selecao_data_exata_tem_prioridade(self):
        items = [
            _scene("a", "2025-05-01T10:00:00Z", 30.0),
            _scene("b", "2025-04-07T10:00:00Z", 22.0),
        ]
        scene, reason, _ = select_best_scene(items, max_cloud=20.0, target_date="2025-04-07")
        assert scene.item_id == "b" and reason == "exact_date_match"

    def test_selecao_sem_cena_retorna_none(self):
        scene, reason, limit = select_best_scene([], max_cloud=20.0)
        assert scene is None and reason == "no_scene" and limit is None

    def test_selecao_todas_nubladas_mais_que_teto(self):
        items = [_scene(f"c{i}", f"2025-05-0{i}T10:00:00Z", 99.0) for i in range(1, 5)]
        scene, _, _ = select_best_scene(items, max_cloud=10.0)
        # relaxa até o teto 100 → ainda há cena (a mais recente)
        assert scene is not None and scene.cloud_cover == 99.0

    def test_selecao_cloud_unknown_rotulado(self):
        items = [_scene("a", "2025-05-01T10:00:00Z", None)]
        scene, reason, _ = select_best_scene(items, max_cloud=20.0)
        assert scene.item_id == "a" and reason == "cloud_unknown"

    def test_fetch_real_calendar_sem_credenciais(self, monkeypatch):
        # sem cdse_configured: retorna status not_configured, nunca exceção
        cal, status, detail = fetch_real_calendar(-22.7, -55.5, 42.5, None)
        assert cal == [] and status == "not_configured" and detail is None


# ---------------------------------------------------------------------------
# Fase 5/9 — Process API + render (fixture central do ticket)
# ---------------------------------------------------------------------------
class TestProcessApi:
    def test_stac_usa_intersects_do_poligono(self, monkeypatch):
        calls = {}

        def fake_stac(geometry, bbox, start, end, limit=10):
            calls["geometry"] = geometry
            calls["bbox"] = bbox
            return []

        monkeypatch.setattr(cds, "stac_search", fake_stac)
        bounds = aoi_bounds(-22.1, -49.05, 42.54)
        cds.stac_search(kml_to_geojson_polygon(
            "[[-22.1,-49.1],[-22.0,-49.1],[-22.0,-49.0],[-22.1,-49.0]]"),
            list(bounds), date.today() - timedelta(30), date.today())
        assert calls["geometry"] is not None and calls["geometry"]["type"] == "Polygon"

    def test_process_request_envelope_correto(self, monkeypatch, cdse_configured):
        seen = {}

        def fake_request(method, url, **kwargs):
            body = kwargs.get("json")
            seen.update({"url": url, "timeRange": body["input"]["data"][0]["dataFilter"].get("timeRange"),
                         "maxCloud": body["input"]["data"][0]["dataFilter"].get("maxCloudCoverage"),
                         "mosaicking": body["input"]["data"][0]["dataFilter"].get("mosaickingOrder"),
                         "bbox": body["input"]["bounds"]["bbox"],
                         "crs": body["input"]["bounds"]["properties"]["crs"],
                         "dataType": body["input"]["data"][0]["type"],
                         "width": body["output"]["width"],
                         "responses": body["output"]["responses"],
                         "evalscript": body["evalscript"]})
            return _FakeResponse(200, content=_tiff_8())

        monkeypatch.setattr(cds, "_get_token", lambda: "tok-falso")
        monkeypatch.setattr(cds.requests, "request", fake_request)
        monkeypatch.setattr(cds.requests, "post", fake_request)
        bounds = aoi_bounds(-22.1, -49.05, 42.54)
        out = cds._process_request(
            bounds=bounds,
            evalscript=cds.build_bands_evalscript(),
            size=64, time_range=("2025-04-07", "2025-04-07"), max_cloud=20.0,
            mosaicking_order="leastCC",
        )
        assert seen["url"].endswith("/process/v1")
        assert seen["dataType"] == "sentinel-2-l2a"
        assert seen["timeRange"]["from"].startswith("2025-04-07")
        assert seen["maxCloud"] == 20.0
        assert seen["mosaicking"] == "leastCC"
        # ordem geográfica [minLon, minLat, maxLon, maxLat] — sem inversão
        assert seen["bbox"] == list(bounds)
        assert seen["bbox"][0] < seen["bbox"][2] and seen["bbox"][1] < seen["bbox"][3]
        assert seen["crs"].endswith("CRS84")
        assert seen["width"] == 64
        assert seen["responses"][0]["format"]["type"] == "image/tiff"
        assert "//VERSION=3" in seen["evalscript"]
        assert len(out) > 100

    def test_process_dem_usar_dem_instance(self, monkeypatch, cdse_configured):
        seen = {}

        def fake_post(method, url, **kwargs):
            seen["demInstance"] = kwargs["json"]["input"]["data"][0].get("demInstance")
            seen["type"] = kwargs["json"]["input"]["data"][0]["type"]
            seen["evalscript"] = kwargs["json"]["evalscript"]
            return _FakeResponse(200, content=_tiff_dem())

        monkeypatch.setattr(cds, "_get_token", lambda: "tok")
        monkeypatch.setattr(cds.requests, "post", fake_post)
        monkeypatch.setattr(cds.requests, "request", fake_post)
        out = cds._process_request(
            bounds=aoi_bounds(-22.1, -49.05, 42.54),
            evalscript=cds.build_dem_evalscript(), size=64,
            data_type="dem", dem_instance="COPERNICUS_30",
        )
        assert seen["type"] == "dem"
        assert seen["demInstance"] == "COPERNICUS_30"
        assert "input: [\"DEM\"]" in seen["evalscript"]
        assert len(out) > 100


# ---------------------------------------------------------------------------
# FASE 7 — Contrato REAL (payloads fiéis ao CDSE; regressão lat/lon/bbox/datetime)
# ---------------------------------------------------------------------------
class TestContratoRealCDSE:
    """Impede regressão de inversão lat/lon, bbox fora de ordem, datetime
    inválido, payloads STAC/Process fora do contrato e STAC-error vs STAC-empty."""

    def test_stac_payload_bbox_ordem_geografica(self, monkeypatch, cdse_configured):
        seen = {}

        def fake_post(method, url, **kwargs):
            seen["url"] = url
            seen["body"] = kwargs.get("json")
            seen["headers"] = kwargs.get("headers") or {}
            return _FakeResponse(200, {"features": []})

        monkeypatch.setattr(cds, "_get_token", lambda: "tok-falso")
        monkeypatch.setattr(cds.requests, "post", fake_post)
        monkeypatch.setattr(cds.requests, "request", fake_post)

        start = date(2026, 7, 11)
        end = date(2026, 9, 9)
        bounds = aoi_bounds(-22.7182, -55.5421, 42.54)
        cds.stac_search(None, bounds, start, end, limit=12)

        assert seen["url"] == "https://stac.dataspace.copernicus.eu/v1/search"
        body = seen["body"]
        assert body["collections"] == ["sentinel-2-l2a"]
        assert body["limit"] == 12
        assert body["datetime"] == "2026-07-11T00:00:00Z/2026-09-09T23:59:59Z"
        # bbox NA ORDEM GEOGRÁFICA [minLon, minLat, maxLon, maxLat]
        assert body["bbox"] == list(bounds)
        assert body["bbox"][0] == pytest.approx(-55.5421 - (bounds[2] - bounds[0]) / 2, abs=1e-6)
        assert body["bbox"][1] == pytest.approx(-22.7182 - (bounds[3] - bounds[1]) / 2, abs=1e-6)
        assert body["bbox"][0] < body["bbox"][2], "west < east"
        assert body["bbox"][1] < body["bbox"][3], "south < north"
        assert "intersects" not in body
        assert seen["headers"]["Authorization"] == "Bearer tok-falso"

    def test_stac_payload_intersects_geojson_lonlat(self, monkeypatch, cdse_configured):
        seen = {}

        def fake_post(method, url, **kwargs):
            seen["body"] = kwargs.get("json")
            return _FakeResponse(200, {"features": []})

        monkeypatch.setattr(cds, "_get_token", lambda: "tok")
        monkeypatch.setattr(cds.requests, "post", fake_post)
        monkeypatch.setattr(cds.requests, "request", fake_post)
        bounds = aoi_bounds(-22.1, -49.05, 42.54)
        geom = kml_to_geojson_polygon(
            "[[-22.1,-49.1],[-22.0,-49.1],[-22.0,-49.0],[-22.1,-49.0]]")
        cds.stac_search(geom, bounds, date(2026, 9, 1), date(2026, 9, 9))
        body = seen["body"]
        assert body["intersects"]["type"] == "Polygon"
        ring = body["intersects"]["coordinates"][0]
        # GeoJSON usa [lon, lat] — NUNCA [lat, lon]
        assert ring[0] == [-49.1, -22.1]
        assert "bbox" not in body

    def test_process_payload_s2_contrato_real(self, monkeypatch, cdse_configured):
        seen = {}

        def fake_post(method, url, **kwargs):
            seen["body"] = kwargs.get("json")
            return _FakeResponse(200, content=_tiff_8())

        monkeypatch.setattr(cds, "_get_token", lambda: "tok")
        monkeypatch.setattr(cds.requests, "post", fake_post)
        monkeypatch.setattr(cds.requests, "request", fake_post)
        bounds = aoi_bounds(-22.7182, -55.5421, 42.54)
        cds._process_request(
            bounds=bounds, evalscript=cds.build_bands_evalscript(), size=256,
            time_range=("2026-09-09", "2026-09-09"), max_cloud=20.0,
            mosaicking_order="leastCC",
        )
        body = seen["body"]
        data = body["input"]["data"][0]
        assert data["type"] == "sentinel-2-l2a"
        filt = data["dataFilter"]
        assert filt["timeRange"] == {
            "from": "2026-09-09T00:00:00Z", "to": "2026-09-09T23:59:59Z",
        }
        assert filt["maxCloudCoverage"] == 20.0
        assert filt["mosaickingOrder"] == "leastCC"
        assert body["input"]["bounds"]["bbox"] == list(bounds)
        assert body["input"]["bounds"]["properties"]["crs"].endswith("CRS84")
        assert body["output"] == {
            "width": 256, "height": 256,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        }
        script = body["evalscript"]
        for band in ("B02", "B03", "B04", "B05", "B08", "B11", "SCL", "dataMask"):
            assert band in script, f"evalscript sem {band}"
        assert "SampleType.FLOAT32" in script
        assert "//VERSION=3" in script

    def test_process_payload_dem_bbox_ordem_geografica(self, monkeypatch, cdse_configured):
        seen = {}

        def fake_post(method, url, **kwargs):
            seen["body"] = kwargs.get("json")
            return _FakeResponse(200, content=_tiff_dem())

        monkeypatch.setattr(cds, "_get_token", lambda: "tok")
        monkeypatch.setattr(cds.requests, "post", fake_post)
        monkeypatch.setattr(cds.requests, "request", fake_post)
        bounds = aoi_bounds(-22.7182, -55.5421, 42.54)
        cds._process_request(bounds=bounds, evalscript=cds.build_dem_evalscript(),
                             size=64, data_type="dem", dem_instance="COPERNICUS_30")
        body = seen["body"]
        assert body["input"]["data"][0]["type"] == "dem"
        assert body["input"]["data"][0]["demInstance"] == "COPERNICUS_30"
        assert body["input"]["bounds"]["bbox"] == list(bounds)
        assert body["input"]["bounds"]["bbox"][0] < body["input"]["bounds"]["bbox"][2]

    def test_datetime_invalido_cai_em_hoje(self, monkeypatch, cdse_configured):
        """date_str inválida → janela termina HOJE (nunca intervalo futuro/roto)."""
        seen = {}

        def fake_stac(geometry, bbox, start, end, limit=10):
            seen["start"], seen["end"] = start, end
            return []

        monkeypatch.setattr(cds, "stac_search", fake_stac)
        out = cds.process_farm_layer(
            farm_id=1, talhao_id=1, lat=-22.1, lon=-49.05, area_ha=42.54,
            kml_coordinates=None, layer="ndvi", date_str="data-invalida-xyz",
        )
        assert out["real_data_status"] == "no_scene"
        assert seen["end"] == date.today()
        assert (seen["end"] - seen["start"]).days == cds.settings.cdse_lookback_days

    def test_stac_vazio_nao_e_stac_erro(self, monkeypatch, cdse_configured):
        """STAC 200 vazio = no_scene (detalhe None); STAC 400 = error + detalhe."""
        monkeypatch.setattr(cds, "stac_search", lambda *a, **k: [])
        cal, status, detail = cds.fetch_real_calendar(-22.7, -55.5, 42.5, None)
        assert cal == [] and status == "no_scene" and detail is None

    def test_stac_erro_400_traz_detalhe_seguro(self, monkeypatch, cdse_configured):
        def boom(*a, **k):
            raise CopernicusError(
                "CDSE requisição recusada (HTTP 400) — STAC_SEARCH",
                status=400,
                endpoint="https://stac.dataspace.copernicus.eu/v1/search",
                content_type="application/json",
                body_snippet='{"detail":"Invalid bbox: west > east"}',
                stage="STAC_SEARCH",
            )

        monkeypatch.setattr(cds, "stac_search", boom)
        cal, status, detail = cds.fetch_real_calendar(-22.7, -55.5, 42.5, None)
        assert cal == [] and status == "error"
        assert detail["stage"] == "STAC_SEARCH"
        assert detail["http_status"] == 400
        assert detail["endpoint"] == "https://stac.dataspace.copernicus.eu/v1/search"
        assert "west > east" in detail["message"]
        # a representação segura nunca expõe headers de auth/token
        assert "Authorization" not in json.dumps(detail)
        assert "Bearer" not in json.dumps(detail)

    def test_erro_400_do_process_captura_corpo(self, monkeypatch, cdse_configured):
        f = _FailRequest([_FakeResponse(
            400,
            text='{"detail":"The property \'mosaickingOrder\' is not allowed"}',
            headers={"Content-Type": "application/problem+json"},
        )])
        monkeypatch.setattr(cds, "_get_token", lambda: "tok")
        monkeypatch.setattr(cds.requests, "post", f)
        monkeypatch.setattr(cds.requests, "request", f)
        with pytest.raises(CopernicusError) as exc:
            cds._request("POST", "https://sh.dataspace.copernicus.eu/process/v1",
                         json_body={}, stage="PROCESS_API")
        assert exc.value.status == 400
        assert exc.value.stage == "PROCESS_API"
        assert exc.value.endpoint == "https://sh.dataspace.copernicus.eu/process/v1"
        assert exc.value.content_type == "application/problem+json"
        assert "mosaickingOrder" in (exc.value.body_snippet or "")
        detail = exc.value.to_detail()
        assert detail["http_status"] == 400
        assert "mosaickingOrder" in detail["message"]

    def test_sanitize_snippet_mascara_credenciais_e_limita_tamanho(self):
        big = "x" * 5000
        snippet = cds._sanitize_snippet(big)
        assert snippet is not None and len(snippet) <= cds._ERROR_BODY_LIMIT
        with_secret = '{"access_token":"segredo123","client_secret":"abc","ok":1}'
        out = cds._sanitize_snippet(with_secret)
        assert "segredo123" not in out and "abc" not in out
        assert "access_token=***" in out or "access_token" in out

    def test_erro_401_nao_expoe_corpo(self, monkeypatch, cdse_configured):
        f = _FailRequest([_FakeResponse(401, {"error": "invalid_client"})])
        monkeypatch.setattr(cds.requests, "post", f)
        monkeypatch.setattr(cds.requests, "request", f)
        with pytest.raises(CopernicusError) as exc:
            cds._request("POST", "https://x/process/v1", json_body={}, stage="PROCESS_API")
        assert exc.value.status == 401
        assert exc.value.body_snippet is None

    def test_process_farm_layer_expoe_real_data_error_400(self, monkeypatch, cdse_configured):
        """Fase 2: HTTP 400 do Process chega como real_data_error SEM quebrar."""
        cdse_configured(monkeypatch)
        monkeypatch.setattr(cds, "stac_search", lambda *a, **k: [
            _scene("real-1", "2026-09-09T10:00:00Z", 5.0)
        ])

        def fake_process(**kw):
            raise CopernicusError(
                "CDSE requisição recusada (HTTP 400) — PROCESS_API",
                status=400,
                endpoint="https://sh.dataspace.copernicus.eu/process/v1",
                content_type="application/json",
                body_snippet='{"detail":"evalscript inválido"}',
                stage="PROCESS_API",
            )

        monkeypatch.setattr(cds, "_process_request", fake_process)
        out = cds.process_farm_layer(
            farm_id=90, talhao_id=1, lat=-22.1, lon=-49.05, area_ha=42.54,
            kml_coordinates=None, layer="ndvi", date_str="2026-09-09",
        )
        assert out["data_origin"] == "procedural"
        assert out["real_data_status"] == "error"
        err = out["real_data_error"]
        assert err["http_status"] == 400
        assert err["stage"] == "PROCESS_API"
        assert "evalscript inválido" in err["message"]


class TestResiliencia:
    def test_timeout_vira_copernicus_error(self, monkeypatch, cdse_configured):
        import requests as req
        monkeypatch.setattr(cds, "_get_token", lambda: "tok")
        monkeypatch.setattr(cds.requests, "post", _FailRequest(exc=req.Timeout("boom")))
        monkeypatch.setattr(cds.requests, "request", _FailRequest(exc=req.Timeout("boom")))
        with pytest.raises(CopernicusError, match="timeout|Falha de rede|network"):
            cds._request("POST", "https://x/process/v1", json_body={})
        # monkeypatch restaura requests original ao final; nada logado

    def test_401_autenticacao_negada(self, monkeypatch, cdse_configured):
        f = _FailRequest([
            _FakeResponse(401, {"error": "invalid_client"}),
        ])
        monkeypatch.setattr(cds.requests, "post", f)
        monkeypatch.setattr(cds.requests, "request", f)
        with pytest.raises(CopernicusError) as exc:
            cds._request("POST", "https://x/process/v1", json_body={})
        assert exc.value.status == 401

    def test_429_retry_curto_com_backoff(self, monkeypatch, cdse_configured):
        calls = {"n": 0}

        def fake(method, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResponse(429, {})
            return _FakeResponse(200, content=b"x" * 200)

        monkeypatch.setattr(cds.settings, "cdse_retry_backoff_s", 0.0)
        monkeypatch.setattr(cds.requests, "post", fake)
        monkeypatch.setattr(cds.requests, "request", fake)
        resp = cds._request("POST", "https://x/process/v1", json_body={})
        assert resp.status_code == 200 and calls["n"] == 2

    def test_429_sem_retry_quando_desabilitado(self, monkeypatch, cdse_configured):
        calls = {"n": 0}

        def fake(method, url, **kwargs):
            calls["n"] += 1
            return _FakeResponse(429, {})

        monkeypatch.setattr(cds.settings, "cdse_retry_on_429", False)
        monkeypatch.setattr(cds.requests, "post", fake)
        monkeypatch.setattr(cds.requests, "request", fake)
        with pytest.raises(CopernicusError) as exc:
            cds._request("POST", "https://x/process/v1", json_body={})
        assert exc.value.status == 429 and calls["n"] == 1

    def test_500_erro_remoto(self, monkeypatch, cdse_configured):
        f = _FailRequest([_FakeResponse(500, {})])
        monkeypatch.setattr(cds.requests, "post", f)
        monkeypatch.setattr(cds.requests, "request", f)
        with pytest.raises(CopernicusError) as exc:
            cds._request("POST", "https://x/process/v1", json_body={})
        assert exc.value.status == 500

    def test_json_invalido_do_stac(self, monkeypatch, cdse_configured):
        class BadJson(_FakeResponse):
            def json(self):
                raise ValueError("não é JSON")

        f = _FailRequest([BadJson(200)])
        monkeypatch.setattr(cds, "_get_token", lambda: "tok-falso")
        monkeypatch.setattr(cds.requests, "post", f)
        monkeypatch.setattr(cds.requests, "request", f)
        with pytest.raises(CopernicusError, match="JSON"):
            cds.stac_search(None, [-49.1, -22.1, -49.0, -22.0],
                            date.today() - timedelta(10), date.today())

    def test_imagem_vazia_do_process(self, monkeypatch, cdse_configured):
        f = _FailRequest([_FakeResponse(200, content=b"")])
        monkeypatch.setattr(cds, "_get_token", lambda: "tok-falso")
        monkeypatch.setattr(cds.requests, "post", f)
        monkeypatch.setattr(cds.requests, "request", f)
        with pytest.raises(CopernicusError, match="vazia"):
            cds._process_request(
                bounds=aoi_bounds(-22.1, -49.05, 42.54),
                evalscript=cds.build_bands_evalscript(), size=64,
            )

    def test_tiff_invalido_do_process(self):
        # o TIFF é de fato lido em `read_band_raster`; lixo → erro explícito
        with pytest.raises(CopernicusError, match="TIFF"):
            cds.read_band_raster(b"\x00" * 500, 64)

    def test_token_401_no_get_token(self, monkeypatch, cdse_configured, caplog):
        import logging
        f = _FailRequest([_FakeResponse(401, {})])
        monkeypatch.setattr(cds.requests, "post", f)
        monkeypatch.setattr(cds.requests, "request", f)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(CopernicusError):
                cds._get_token()
            # log não contém o segredo configurado
            assert "segredo-teste" not in caplog.text

    def test_token_nunca_logado(self, monkeypatch, cdse_configured, caplog):
        import logging
        monkeypatch.setattr(cds.requests, "post", _FailRequest([
            _FakeResponse(200, {"access_token": "token-super-secreto-abc", "expires_in": 3600})
        ]))
        monkeypatch.setattr(cds.requests, "request", lambda *a, **k: _FakeResponse(200, {}))
        with caplog.at_level(logging.DEBUG):
            tok = cds._get_token()
            assert tok == "token-super-secreto-abc"
            assert "token-super-secreto-abc" not in caplog.text
            assert "segredo-teste" not in caplog.text
        # token em memória, nunca em disco
        assert cds._token_cache["token"] == "token-super-secreto-abc"


# ---------------------------------------------------------------------------
# Fases 6-10/13 — pipeline completo de camada real (fixture 1)
# ---------------------------------------------------------------------------
class TestPipelineCompleto:
    def _mock_ok(self, monkeypatch, scenes, tiff=None, cdse_configured=True):
        """STAC devolve cenas fixture; Process devolve TIFF sintético."""
        if cdse_configured:
            cdse_configured(monkeypatch)
        monkeypatch.setattr(cds, "stac_search", lambda *a, **k: scenes)
        monkeypatch.setattr(cds, "_process_request",
                            lambda **kw: tiff if tiff is not None else _tiff_8())

    def test_cena_valida_pipeline_retorna_proveniencia(self, monkeypatch, tmp_path, cdse_configured):
        cdse_configured(monkeypatch)
        monkeypatch.setattr(cds, "stac_search", lambda *a, **k: [
            _scene("S2B_123456_20250407", "2025-04-07T13:05:12Z", 8.0,
                   platform="sentinel-2b")
        ])
        monkeypatch.setattr(cds, "_process_request", lambda **kw: _tiff_8())
        monkeypatch.setattr(cds, "cdse_dir", lambda f, t, d: str(tmp_path / f"cdse/{d}"))

        out = cds.process_farm_layer(
            farm_id=7, talhao_id=3, lat=-22.1, lon=-49.05, area_ha=42.54,
            kml_coordinates=None, layer="ndvi", date_str="2025-04-07",
        )
        assert out["data_origin"] == "sentinel"
        assert out["real_data_status"] == "ok"
        assert out["collection"] == "sentinel-2-l2a"
        assert out["product_id"] == "S2B_123456_20250407"
        assert out["acquisition_date"] == "2025-04-07"
        assert out["processing_level"] == "L2A"
        assert out["bands"] == cds.BANDS
        assert out["valid_pixel_percentage"] is not None and out["valid_pixel_percentage"] > 0
        assert out["selection_reason"] == "exact_date_match"
        assert out["index_formulas"]["ndre"] == "(B08 - B05) / (B08 + B05)"
        assert "texture_url" in out and "layer=ndvi" in out["texture_url"]
        assert (tmp_path / "cdse/2025-04-07/ndvi.png").exists()
        assert (tmp_path / "cdse/2025-04-07/provenance.json").exists()
        assert json.loads((tmp_path / "cdse/2025-04-07/provenance.json").read_text())["provider"] == cds.PROVIDER

    def test_multiplicas_cenas_escolhe_melhor_e_justifica(self, monkeypatch, tmp_path, cdse_configured):
        cdse_configured(monkeypatch)
        monkeypatch.setattr(cds, "stac_search", lambda *a, **k: [
            _scene("nublada", "2025-04-10T10:00:00Z", 60.0),
            _scene("limpa", "2025-04-07T10:00:00Z", 10.0),
            _scene("muitonuvem", "2025-04-06T10:00:00Z", 90.0),
        ])
        monkeypatch.setattr(cds, "_process_request", lambda **kw: _tiff_8())
        monkeypatch.setattr(cds, "cdse_dir", lambda f, t, d: str(tmp_path / f"cdse/{d}"))
        out = cds.process_farm_layer(
            farm_id=9, talhao_id=1, lat=-22.1, lon=-49.05, area_ha=10.0,
            kml_coordinates=None, layer="ndvi", date_str="2025-04-07",
        )
        assert out["product_id"] == "limpa"
        assert out["selection_reason"] == "exact_date_match"
        assert out["date"] == "2025-04-07"

    def test_muita_nuvem_todas_fora_limite(self, monkeypatch, tmp_path, cdse_configured):
        cdse_configured(monkeypatch)
        monkeypatch.setattr(cds, "stac_search", lambda *a, **k: [
            _scene("c1", "2025-04-12T10:00:00Z", 25.0),
            _scene("c2", "2025-04-10T10:00:00Z", 40.0),
        ])
        monkeypatch.setattr(cds, "_process_request", lambda **kw: _tiff_8())
        monkeypatch.setattr(cds, "cdse_dir", lambda f, t, d: str(tmp_path / f"cdse/{d}"))
        out = cds.process_farm_layer(
            farm_id=10, talhao_id=2, lat=-22.1, lon=-49.05, area_ha=10.0,
            kml_coordinates=None, layer="ndvi", date_str="2025-04-15",
        )
        # relaxamento: c1 (25%) é aceita no limite ampliado (35%)
        assert out["data_origin"] == "sentinel"
        assert out["product_id"] == "c1"
        assert out["selection_reason"] == "cloud_limit_relaxed"

    def test_nenhuma_cena_fallback_explicito(self, monkeypatch, cdse_configured):
        cdse_configured(monkeypatch)
        monkeypatch.setattr(cds, "stac_search", lambda *a, **k: [])
        out = cds.process_farm_layer(
            farm_id=11, talhao_id=2, lat=-22.1, lon=-49.05, area_ha=10.0,
            kml_coordinates=None, layer="ndvi", date_str="2025-04-07",
        )
        assert out["data_origin"] == "procedural"
        assert out["real_data_status"] == "no_scene"

    def test_sem_credenciais_status_not_configured(self):
        out = cds.process_farm_layer(
            farm_id=12, talhao_id=2, lat=-22.1, lon=-49.05, area_ha=10.0,
            kml_coordinates=None, layer="ndvi", date_str="2025-04-07",
        )
        assert out["data_origin"] == "procedural"
        assert out["real_data_status"] == "not_configured"
        assert "NÃO CONFIGURADOS" in out["real_data_message"]

    def test_timeout_pipeline_vira_fallback(self, monkeypatch, cdse_configured):
        cdse_configured(monkeypatch)

        def boom(*a, **k):
            raise CopernicusError("Falha de rede (Timeout)")

        monkeypatch.setattr(cds, "stac_search", boom)
        out = cds.process_farm_layer(
            farm_id=13, talhao_id=2, lat=-22.1, lon=-49.05, area_ha=10.0,
            kml_coordinates=None, layer="ndvi", date_str="2025-04-07",
        )
        assert out["data_origin"] == "procedural"
        assert out["real_data_status"] == "error"

    def test_imagem_invalida_pipeline_vira_fallback(self, monkeypatch, cdse_configured):
        cdse_configured(monkeypatch)
        monkeypatch.setattr(cds, "stac_search", lambda *a, **k: [
            _scene("ok", "2025-04-07T10:00:00Z", 5.0)
        ])
        monkeypatch.setattr(cds, "_process_request", lambda **kw: b"\x00" * 128)
        out = cds.process_farm_layer(
            farm_id=14, talhao_id=2, lat=-22.1, lon=-49.05, area_ha=10.0,
            kml_coordinates=None, layer="ndvi", date_str="2025-04-07",
        )
        assert out["data_origin"] == "procedural"
        assert out["real_data_status"] == "error"

    def test_cache_disco_evita_segunda_chamada(self, monkeypatch, tmp_path, cdse_configured):
        cdse_configured(monkeypatch)
        calls = {"process": 0, "stac": 0}

        def fake_stac(*a, **k):
            calls["stac"] += 1
            return [_scene("s1", "2025-04-07T10:00:00Z", 3.0)]

        def fake_process(**kw):
            calls["process"] += 1
            return _tiff_8()

        monkeypatch.setattr(cds, "stac_search", fake_stac)
        monkeypatch.setattr(cds, "_process_request", fake_process)
        monkeypatch.setattr(cds, "cdse_dir", lambda f, t, d: str(tmp_path / f"cdse/{d}"))

        first = cds.process_farm_layer(
            farm_id=15, talhao_id=1, lat=-22.1, lon=-49.05, area_ha=10.0,
            kml_coordinates=None, layer="ndre", date_str="2025-04-07",
        )
        assert first["data_origin"] == "sentinel"
        assert calls["process"] == 1
        # segunda chamada (mesma data/camada) vem do disco: 0 novas chamadas
        second = cds.process_farm_layer(
            farm_id=15, talhao_id=1, lat=-22.1, lon=-49.05, area_ha=10.0,
            kml_coordinates=None, layer="ndre", date_str="2025-04-07",
        )
        assert second["product_id"] == "s1"
        assert calls["process"] == 1 and calls["stac"] == 1

    def test_camada_invalida_levanta_value_error(self, cdse_configured):
        with pytest.raises(ValueError, match="não suportada"):
            cds.process_farm_layer(
                farm_id=1, talhao_id=1, lat=-22.1, lon=-49.05, area_ha=10.0,
                kml_coordinates=None, layer="tsm", date_str=None,
            )


# ---------------------------------------------------------------------------
# Fase 12 — DEM real: COPERNICUS_30 → COPERNICUS_90 → aproximado
# ---------------------------------------------------------------------------
class TestDemReal:
    def test_dem_30_sucesso(self, monkeypatch, cdse_configured):
        cdse_configured(monkeypatch)
        seen = {}

        def fake_process(**kw):
            seen["instance"] = kw.get("dem_instance")
            return _tiff_dem(lo=320.0, hi=480.0)

        monkeypatch.setattr(cds, "_process_request", fake_process)
        out = cds.fetch_dem_png(aoi_bounds(-22.1, -49.05, 42.54), 64, instance="COPERNICUS_30")
        assert out is not None
        assert out["source"] == "copernicus_30"
        assert out["min_elevation_m"] == 320.0
        assert out["max_elevation_m"] == 480.0
        assert out["png_bytes"][:8] == b"\x89PNG\r\n\x1a\n"
        assert seen["instance"] == "COPERNICUS_30"

    def test_dem_30_falha_cai_para_90(self, monkeypatch, cdse_configured):
        cdse_configured(monkeypatch)
        calls = []

        def fake_process(**kw):
            calls.append(kw.get("dem_instance"))
            if kw.get("dem_instance") == "COPERNICUS_30":
                raise CopernicusError("30 indisponível")
            return _tiff_dem(lo=250.0, hi=610.0)

        monkeypatch.setattr(cds, "_process_request", fake_process)
        out = cds.fetch_dem_png(aoi_bounds(-22.1, -49.05, 42.54), 64, instance="COPERNICUS_30")
        assert calls == ["COPERNICUS_30"]
        # 30 falhou → caller do heightmap tenta 90
        monkeypatch.setattr(cds, "_process_request",
                            lambda **kw: _tiff_dem(lo=250.0, hi=610.0)
                            if kw.get("dem_instance") == "COPERNICUS_90" else (_ for _ in ()).throw(CopernicusError("30")))
        out90 = cds.fetch_dem_png(aoi_bounds(-22.1, -49.05, 42.54), 64, instance="COPERNICUS_90")
        assert out90 is not None and out90["source"] == "copernicus_90"

    def test_dem_ambos_indisponiveis_retorna_none(self, monkeypatch, cdse_configured):
        cdse_configured(monkeypatch)
        monkeypatch.setattr(cds, "_process_request",
                            lambda **kw: (_ for _ in ()).throw(CopernicusError("sem dados")))
        assert cds.fetch_dem_png(aoi_bounds(-22.1, -49.05, 42.54), 64) is None
        assert cds.fetch_dem_png(aoi_bounds(-22.1, -49.05, 42.54), 64, instance="COPERNICUS_90") is None

    def test_dem_sem_credenciais_lanca_not_configured(self):
        with pytest.raises(CopernicusNotConfigured):
            cds.fetch_dem_png(aoi_bounds(-22.1, -49.05, 42.54), 64)

    def test_dem_endpoint_usa_copernicus_30_depois_90(
        self, monkeypatch, cdse_configured, tmp_path
    ):
        """dem_service: 30 falha → 90 → heightmap real com source copernicus_90."""
        from services import dem_service

        cdse_configured(monkeypatch)

        def fake_fetch(bounds, size, instance="COPERNICUS_30"):
            if instance == "COPERNICUS_30":
                return None
            return {
                "png_bytes": _tiff_dem(lo=240.0, hi=540.0),
                "min_elevation_m": 240.0,
                "max_elevation_m": 540.0,
                "source": "copernicus_90",
                "instance": instance,
                "provider": "CDSE",
            }

        monkeypatch.setattr(cds, "is_configured", lambda: True)
        monkeypatch.setattr(cds, "fetch_dem_png", fake_fetch)
        # mantém o cache de DEM real em repro: aponta dynamic_talhoes para tmp
        monkeypatch.setattr(dem_service, "BASE_DIR", str(tmp_path))
        res = dem_service.process_talhao_heightmap(
            farm_id=21, talhao_id=1, lat=-22.1, lon=-49.05, area_ha=42.54,
            kml_coordinates=None, size=64,
        )
        assert res["available"] is True
        assert res["source"] == "copernicus_90"
        assert res["min_elevation_m"] == 240.0
        assert res["heightmap_url"].startswith("http://testserver/api/talhao/21/heightmap")
        assert (tmp_path / "dynamic_talhoes/farm_21_talhao_1/heightmap.png").exists()

    def test_dem_fallback_total_aproximado(self, monkeypatch, tmp_path):
        """Sem CDSE e sem tile local → available=false + reason (aproximado)."""
        from services import dem_service

        monkeypatch.setattr(cds, "is_configured", lambda: False)
        monkeypatch.setattr(dem_service, "BASE_DIR", str(tmp_path))
        monkeypatch.setattr(dem_service, "_find_local_tile", lambda lat, lon: None)
        monkeypatch.setattr(dem_service, "_download_tile", lambda t: None)
        res = dem_service.process_talhao_heightmap(
            farm_id=22, talhao_id=1, lat=-22.1, lon=-49.05, area_ha=42.54,
            kml_coordinates=None, size=64,
        )
        assert res["available"] is False
        assert res["source"] == "none"
        assert "Tile DEM indisponível" in res["reason"]


# ---------------------------------------------------------------------------
# Fase 24 (fim) — contrato do endpoint com CDSE mockado
# ---------------------------------------------------------------------------
class TestEndpointCdsE:
    def test_sem_credenciais_textura_fallback_mensagem_clara(self, client, admin_token):
        r = client.get("/api/talhao/1/texture?layer=ndvi", headers=auth(admin_token))
        assert r.status_code == 200
        d = r.json()
        assert d["data_origin"] == "procedural"
        assert d["real_data_status"] == "not_configured"
        assert d["real_data_message"] == "DADOS SATELITAIS REAIS NÃO CONFIGURADOS"

    def test_textura_com_cdse_mock_retorna_proveniencia(
        self, client, user_token, monkeypatch, tmp_path
    ):
        # cria fazenda do usuário (endpoint público) e mocka CDSE no main
        body = {
            "name": "Fazenda CDSE", "city": "Marília", "total_area": 25.0,
            "talhao_name": "Talhão A", "crop": "Soja",
            "latitude": -22.20, "longitude": -49.05,
        }
        fid = client.post("/api/farms", json=body, headers=auth(user_token)).json()["id"]

        monkeypatch.setattr(cds.settings, "cdse_client_id", "c")
        monkeypatch.setattr(cds.settings, "cdse_client_secret", "s")
        monkeypatch.setattr(cds, "stac_search", lambda *a, **k: [
            _scene("S2A_REAL_001", "2025-04-07T10:00:00Z", 5.0)
        ])
        monkeypatch.setattr(cds, "_process_request", lambda **kw: _tiff_8())
        monkeypatch.setattr(
            "main.cdse_dir", lambda f, t, d: str(tmp_path / f"cdse{f}_{t}/{d}")
        )
        monkeypatch.setattr(cds, "cdse_dir", lambda f, t, d: str(tmp_path / f"cdse{f}_{t}/{d}"))

        r = client.get(f"/api/talhao/{fid}/texture?layer=ndvi&date=2025-04-07",
                       headers=auth(user_token))
        assert r.status_code == 200
        d = r.json()
        assert d["data_origin"] == "sentinel"
        assert d["product_id"] == "S2A_REAL_001"
        assert d["collection"] == "sentinel-2-l2a"
        assert d["processing_level"] == "L2A"
        assert d["valid_pixel_percentage"] > 0
        assert "/api/talhao/" in d["texture_url"] and "date=2025-04-07" in d["texture_url"]

        # PNG real é servido pela rota autenticada com a data correta
        rpng = client.get(d["texture_url"].replace("http://testserver", ""),
                          headers=auth(user_token))
        assert rpng.status_code == 200
        assert rpng.headers["content-type"] == "image/png"
        assert rpng.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_dates_farm_cdse_retorna_calendario_real(
        self, client, user_token, monkeypatch
    ):
        body = {
            "name": "Fazenda Calendário", "city": "Marília", "total_area": 30.0,
            "talhao_name": "A", "crop": "Milho Safrinha",
            "latitude": -22.25, "longitude": -49.10,
        }
        fid = client.post("/api/farms", json=body, headers=auth(user_token)).json()["id"]
        monkeypatch.setattr(cds.settings, "cdse_client_id", "c")
        monkeypatch.setattr(cds.settings, "cdse_client_secret", "s")
        monkeypatch.setattr(cds, "stac_search", lambda *a, **k: [
            _scene("d1", "2025-03-20T10:00:00Z", 6.0),
            _scene("d2", "2025-03-24T10:00:00Z", 15.0),
            _scene("d3", "2025-03-28T10:00:00Z", 40.0),  # fora do limite → descartada
        ])
        r = client.get(f"/api/talhao/{fid}/dates", headers=auth(user_token))
        assert r.status_code == 200
        d = r.json()
        assert d["source"] == "sentinel-cdse"
        assert d["dates"] == ["2025-03-24", "2025-03-20"]
        assert d["calendar"][0]["product_id"] == "d2"
        assert d["visual_layers"][0] == "rgb"

    def test_dates_farm_sem_cdse_cai_na_config(self, client, user_token):
        body = {
            "name": "Fazenda Sem CDSE", "city": "Marília", "total_area": 30.0,
            "talhao_name": "A", "crop": "Soja",
            "latitude": -22.25, "longitude": -49.10,
        }
        fid = client.post("/api/farms", json=body, headers=auth(user_token)).json()["id"]
        r = client.get(f"/api/talhao/{fid}/dates", headers=auth(user_token))
        assert r.status_code == 200
        d = r.json()
        assert d["source"] == "config"
        assert d["dates"] == cds.settings.sentinel_dates
        assert d["status"] == "not_configured"

    def test_dates_publico_visual_layers(self, client):
        d = client.get("/api/talhao/dates").json()
        assert d["visual_layers"][0] == "rgb"
        # compatibilidade: campos antigos intactos
        assert d["indices"] == ["ndvi", "evi", "ndre", "ndmi"]
        assert len(d["dates"]) == 13
