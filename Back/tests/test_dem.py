"""
Geoprocessamento — Copernicus DEM GL-30:
   unitários (nomenclatura SRTM, bounds, seleção de tile) e endpoint
   de heightmap (recorte → normalização → PNG servido estaticamente).

A tile GeoTIFF usada é SINTÉTICA (mesmo grid 30 m do GL-30), gerada em um
diretório temporário — a suíte não acessa a rede nem o repositório.
"""
import os
from pathlib import Path

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import from_bounds

from conftest import REPO_ROOT, auth
from services import dem_service
from services.dem_service import srtm_tile_name, talhao_bounds

DEM_DIR = Path(os.environ["DEM_DIR"])
TILE_NAME = "Copernicus_Dem_GLO30_s23_w056.tif"


@pytest.fixture(scope="session")
def dem_tile():
    """Tile 1°×1° (1200×1200 px @ 30 m) com relevo plausível do interior do PR."""
    out = DEM_DIR / TILE_NAME
    if not out.exists():
        w = h = 1200
        transform = from_bounds(-56.0, -23.0, -55.0, -22.0, w, h)
        rng = np.random.default_rng(42)
        yy, xx = np.mgrid[0:h, 0:w]
        arr = (480.0
               + 40.0 * (xx / w)
               + 15.0 * np.sin(xx / 90.0) * np.cos(yy / 130.0)
               + rng.normal(0, 2.5, (h, w))).astype("float32")
        with rasterio.open(out, "w", driver="GTiff", width=w, height=h, count=1,
                           dtype="float32", crs="EPSG:4326", transform=transform,
                           nodata=-32768.0) as dst:
            dst.write(arr, 1)
    return out


# ---------------------------------------------------------------------------
# Unitários
# ---------------------------------------------------------------------------
class TestDemUnits:
    @pytest.mark.parametrize("lat,lon,expected", [
        (-22.7182, -55.5421, "s23_w056"),   # Apucarana/PR (talhão demo)
        (10.5, -50.2, "n10_w051"),
        (-3.1, 39.9, "s04_e039"),
        (0.5, 0.5, "n00_e000"),
    ])
    def test_srtm_tile_name(self, lat, lon, expected):
        assert srtm_tile_name(lat, lon) == expected

    def test_bounds_sem_kml_usa_area(self):
        min_lat, max_lat, min_lon, max_lon = talhao_bounds(-22.7182, -55.5421, 42.54)
        # lado = √(42.54 ha em m²) ≈ 652 m → ~0.00586° em latitude
        assert (max_lat - min_lat) == pytest.approx(0.00586, rel=0.05)
        assert (min_lat + max_lat) / 2 == pytest.approx(-22.7182, abs=1e-9)
        assert (min_lon + max_lon) / 2 == pytest.approx(-55.5421, abs=1e-9)
        # longitude é maior que latitude a ~22°S (fator cos)
        assert (max_lon - min_lon) > (max_lat - min_lat)

    def test_bounds_com_kml_usa_poligono(self):
        kml = "[[-22.72,-55.55],[-22.71,-55.54],[-22.70,-55.53],[-22.71,-55.52],[-22.72,-55.55]]"
        min_lat, max_lat, min_lon, max_lon = talhao_bounds(-22.7182, -55.5421, 42.54, kml)
        assert (min_lat, max_lat, min_lon, max_lon) == (-22.72, -22.70, -55.55, -55.52)

    def test_bounds_kml_invalido_fallback_sem_excecao(self):
        b = talhao_bounds(-22.7182, -55.5421, 42.54, "json-quebrado{{{")
        assert b[1] > b[0] and b[3] > b[2]

class TestFindLocalTile:
    def test_prefere_tile_da_celula(self, monkeypatch, tmp_path):
        (tmp_path / "Aleatoria.tif").write_bytes(b"x")
        target = tmp_path / "Copernicus_Dem_GLO30_s23_w056.tif"
        target.write_bytes(b"x")
        monkeypatch.setattr(dem_service, "_dem_dir", lambda: str(tmp_path))
        assert dem_service._find_local_tile(-22.7182, -55.5421) == str(target)

    def test_unico_arquivo_e_usado(self, monkeypatch, tmp_path):
        only = tmp_path / "qualquer_dem.tif"
        only.write_bytes(b"x")
        monkeypatch.setattr(dem_service, "_dem_dir", lambda: str(tmp_path))
        assert dem_service._find_local_tile(-22.7182, -55.5421) == str(only)

    def test_dir_vazio_retorna_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dem_service, "_dem_dir", lambda: str(tmp_path))
        assert dem_service._find_local_tile(-22.7182, -55.5421) is None


# ---------------------------------------------------------------------------
# Endpoint /api/talhao/{farm_id}/heightmap
# ---------------------------------------------------------------------------
class TestHeightmapEndpoint:
    def test_endpoint_disponivel_e_png_servido(self, client, dem_tile, admin_token):
        d = client.get("/api/talhao/1/heightmap", headers=auth(admin_token)).json()
        assert d["available"] is True
        assert d["source"] == "copernicus_gl30"
        assert d["size"] == 256
        assert d["min_elevation_m"] < d["max_elevation_m"]
        # elevações plausíveis dentro da faixa da tile (480–520 m no recorte)
        assert 400 < d["min_elevation_m"] < d["max_elevation_m"] < 600
        assert len(d["bounds"]) == 4
        # PNG servido por rota autenticada (PR #3 — heightmap privado por fazenda)
        assert client.get(d["heightmap_url"], headers=auth(admin_token)).status_code == 200

    def test_png_256x256_escalonado(self, client, dem_tile, admin_token):
        client.get("/api/talhao/1/heightmap", headers=auth(admin_token))
        path = REPO_ROOT / "dynamic_talhoes" / "farm_1_talhao_1" / "heightmap.png"
        assert path.exists()
        img = Image.open(path)
        assert img.size == (256, 256)
        assert img.mode == "L"
        arr = np.asarray(img)
        assert arr.dtype == np.uint8
        assert arr.min() >= 0 and arr.max() <= 255
        # normalização efetiva: o recorte usa praticamente toda a escala 0–255
        # (o resize bilinear re-amostra os extremos, então o span encolhe um pouco)
        assert arr.max() - arr.min() >= 150

    def test_size_parametro_respeitado(self, client, dem_tile, admin_token):
        d = client.get("/api/talhao/1/heightmap?size=128", headers=auth(admin_token)).json()
        assert d["size"] == 128
        img = Image.open(REPO_ROOT / "dynamic_talhoes/farm_1_talhao_1/heightmap.png")
        assert img.size == (128, 128)

    @pytest.mark.parametrize("size", [16, 1000])
    def test_size_fora_da_faixa_422(self, client, size, admin_token):
        assert client.get(f"/api/talhao/1/heightmap?size={size}", headers=auth(admin_token)).status_code == 422

    def test_farm_inexistente_404(self, client, admin_token):
        assert client.get("/api/talhao/999999/heightmap", headers=auth(admin_token)).status_code == 404

    def test_indisponivel_sem_tile_retorna_disponibilidade_false(self, client, monkeypatch, admin_token):
        monkeypatch.setattr(dem_service, "_find_local_tile", lambda lat, lon: None)
        monkeypatch.setattr(dem_service, "_download_tile", lambda name: None)
        d = client.get("/api/talhao/1/heightmap", headers=auth(admin_token)).json()
        assert d["available"] is False
        assert d["source"] == "none"
        assert d["heightmap_url"] is None
        assert d["reason"] and "s23_w056" in d["reason"]  # orienta o usuário
