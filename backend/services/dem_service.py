"""
Pipeline topográfico — Copernicus DEM GL-30.

Fluxo (para o talhão ativo):
1. Identifica a tile SRTM (1°×1°) que cobre o centroide da fazenda
   (nomenclatura: `Copernicus_Dem_GLO30_s23_w056`);
2. Busca a tile em `settings.dem_dir` (arquivo local) — se não existir
   e o download estiver habilitado, tenta os espelhos públicos de
   `settings.dem_download_urls` (best-effort, com log);
3. Recorta o raster aos bounds do talhão (polígono KML ou quadrado
   derivado da área), normaliza para 0-255 e resampleia para
   `size`×`size` (potência de 2 — ideal para mipmaps no Three.js);
4. Grava um PNG cinza (o canal VERMELHO é o lido pelo `displacementMap`
   do Three.js) em `dynamic_talhoes/farm_X_talhao_Y/heightmap.png` e
   devolve as estatísticas de elevação (mín/máx em metros).

Sem tile disponível, o endpoint responde `available=false` e o
frontend mantém o comportamento anterior (deslocamento via NDVI).
"""
import io
import json
import logging
import math
import os
import zipfile

import numpy as np
import rasterio
import requests
from PIL import Image

from config import settings

logger = logging.getLogger("orion.dem")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Limite abaixo do qual valores de elevação são considerados inválidos
#: (nodata padrão de DEMs SRTM/TanDEM-X)
_INVALID = -9000.0


# ---------------------------------------------------------------------------
# Geometria
# ---------------------------------------------------------------------------
def srtm_tile_name(lat: float, lon: float) -> str:
    """Nome da tile SRTM (1°×1°) que contém o ponto — ex.: `s23_w056`."""
    row = int(math.floor(lat))
    col = int(math.floor(lon))
    lat_part = f"{'s' if row < 0 else 'n'}{abs(row):02d}"
    lon_part = f"{'w' if col < 0 else 'e'}{abs(col):03d}"
    return f"{lat_part}_{lon_part}"


def talhao_bounds(
    lat: float,
    lon: float,
    area_ha: float,
    kml_coordinates: str | None = None,
) -> tuple[float, float, float, float]:
    """
    Devolve (min_lat, max_lat, min_lon, max_lon) do talhão.

    Usa o polígono KML quando disponível (pares [lat, lon]); caso
    contrário, um quadrado centrado nas coordenadas com lado igual ao
    do talhão (√área = 100·√area_ha metros) — suficiente para
    capturar o alívio local.
    """
    if kml_coordinates:
        try:
            coords = json.loads(kml_coordinates) if isinstance(kml_coordinates, str) else kml_coordinates
            if coords and len(coords) >= 3:
                lats = [pt[0] for pt in coords]
                lons = [pt[1] for pt in coords]
                return min(lats), max(lats), min(lons), max(lons)
        except Exception:
            logger.warning("KML inválido no talhão; usando bounds derivados da área.")
    side_m = math.sqrt(max(area_ha, 0.01)) * 100.0
    half_deg_lat = (side_m / 2.0) / 111_320.0
    half_deg_lon = (side_m / 2.0) / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return lat - half_deg_lat, lat + half_deg_lat, lon - half_deg_lon, lon + half_deg_lon


# ---------------------------------------------------------------------------
# Tile: localização local / download
# ---------------------------------------------------------------------------
def _dem_dir() -> str:
    return os.path.join(BASE_DIR, settings.dem_dir)


def _find_local_tile(lat: float, lon: float) -> str | None:
    """
    Localiza a melhor tile GeoTIFF disponível em `settings.dem_dir`:
    1ª opção — tile cujo nome contém a célula SRTM do ponto;
    2ª opção — qualquer tile presente (uso single-tile).
    """
    dem = _dem_dir()
    if not os.path.isdir(dem):
        return None
    candidates = [f for f in sorted(os.listdir(dem)) if f.lower().endswith((".tif", ".tiff"))]
    if not candidates:
        return None
    wanted = srtm_tile_name(lat, lon)
    for fname in candidates:
        if wanted in os.path.splitext(fname)[0].lower():
            return os.path.join(dem, fname)
    return os.path.join(dem, candidates[0])


def _download_tile(tile_name: str) -> str | None:
    """
    Best-effort: baixa a tile GeoTIFF (ou .zip contendo-a) dos espelhos
    públicos configurados. Falhas são logadas e devolvem None.
    """
    if not settings.dem_download_enabled:
        return None
    dem = _dem_dir()
    os.makedirs(dem, exist_ok=True)
    base_name = f"Copernicus_Dem_GLO30_{tile_name}"
    dest = os.path.join(dem, f"{base_name}.tif")

    for template in settings.dem_download_urls:
        url = template.replace("{tile}", base_name)
        try:
            logger.info("DEM: tentando baixar tile de %s", url)
            resp = requests.get(url, timeout=settings.dem_download_timeout_s)
            resp.raise_for_status()
            data = resp.content

            # Alguns espelhos servem .zip contendo o GeoTIFF
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
                tifs = [n for n in zf.namelist() if n.lower().endswith((".tif", ".tiff"))]
                if tifs:
                    with zf.open(tifs[0]) as fh:
                        data = fh.read()
            except zipfile.BadZipFile:
                pass  # conteúdo já é GeoTIFF bruto

            with open(dest, "wb") as fh:
                fh.write(data)
            logger.info("DEM: tile baixada para %s", dest)
            return dest
        except Exception as e:
            logger.warning("DEM: falha no espelho %s (%s)", url, e)
    return None


def _source_label(tif_path: str) -> str:
    stem = os.path.splitext(os.path.basename(tif_path))[0].lower()
    if any(k in stem for k in ("copernicus", "gl30", "glo30")):
        return "copernicus_gl30"
    return "local_geotiff"


# ---------------------------------------------------------------------------
# Heightmap
# ---------------------------------------------------------------------------
def process_talhao_heightmap(
    farm_id: int,
    talhao_id: int,
    lat: float,
    lon: float,
    area_ha: float,
    kml_coordinates: str | None = None,
    size: int = 256,
) -> dict:
    """
    Processa o recorte DEM do talhão e devolve o payload do endpoint
    (schema `schemas.HeightmapResponse`).
    """
    unavailable: dict = {
        "available": False,
        "farm_id": farm_id,
        "talhao_id": talhao_id,
        "source": "none",
    }

    # --- PR #4b — DEM REAL via CDSE (Process API): COPERNICUS_30 → 90 ---
    # Só quando configurado; sem credenciais a tentativa é pulada (fallback).
    try:
        from services.copernicus_service import (
            fetch_dem_png,
            is_configured,
        )

        if is_configured():
            # talhao_bounds devolve (min_lat, max_lat, min_lon, max_lon);
            # o CDSE espera [minLon, minLat, maxLon, maxLat] — converte aqui.
            latlon = talhao_bounds(lat, lon, area_ha, kml_coordinates)
            bounds = (latlon[2], latlon[0], latlon[3], latlon[1])
            for instance, source in (
                ("COPERNICUS_30", "copernicus_30"),
                ("COPERNICUS_90", "copernicus_90"),
            ):
                dem = fetch_dem_png(bounds, size, instance=instance)
                if dem:
                    folder = os.path.join(
                        BASE_DIR, "dynamic_talhoes", f"farm_{farm_id}_talhao_{talhao_id}"
                    )
                    os.makedirs(folder, exist_ok=True)
                    out_path = os.path.join(folder, "heightmap.png")
                    with open(out_path, "wb") as fh:
                        fh.write(dem["png_bytes"])
                    logger.info(
                        "DEM: heightmap REAL %s×%s via CDSE %s (%.2f–%.2f m)",
                        size, size, source, dem["min_elevation_m"], dem["max_elevation_m"],
                    )
                    return {
                        "available": True,
                        "farm_id": farm_id,
                        "talhao_id": talhao_id,
                        "size": size,
                        "min_elevation_m": dem["min_elevation_m"],
                        "max_elevation_m": dem["max_elevation_m"],
                        "bounds": [round(bounds[0], 6), round(bounds[1], 6),
                                   round(bounds[2], 6), round(bounds[3], 6)],
                        "source": source,
                        "heightmap_url": (
                            f"{settings.public_base_url}/api/talhao/{farm_id}/heightmap.png?size={size}"
                        ),
                        "reason": None,
                    }
    except Exception:
        logger.exception("DEM: falha inesperada na tentativa CDSE — usando fallback.")

    tif_path = _find_local_tile(lat, lon) or _download_tile(srtm_tile_name(lat, lon))
    if not tif_path:
        unavailable["reason"] = (
            f"Tile DEM indisponível. Baixe o Copernicus DEM GL-30 da tile "
            f"{srtm_tile_name(lat, lon)} (OpenTopography / portal Copernicus) e "
            f"coloque em '{settings.dem_dir}/' — veja o README."
        )
        return unavailable

    # --- Recorte e normalização ---
    try:
        min_lat, max_lat, min_lon, max_lon = talhao_bounds(lat, lon, area_ha, kml_coordinates)
        with rasterio.open(tif_path) as src:
            window = rasterio.windows.from_bounds(min_lon, min_lat, max_lon, max_lat, src.transform)
            arr = src.read(1, window=window, boundless=True, fill_value=-9999.0).astype("float32")
            nodata = src.nodata
            if nodata is not None:
                arr[arr == nodata] = -9999.0
    except Exception:
        logger.exception("DEM: falha ao ler a tile %s", tif_path)
        unavailable["reason"] = "Falha ao ler a tile DEM (arquivo inválido?)."
        return unavailable

    valid = arr[arr > _INVALID]
    if valid.size == 0:
        unavailable["reason"] = "Sem dados de elevação válidos na área (oceano/sem cobertura?)."
        return unavailable

    lo, hi = float(valid.min()), float(valid.max())
    if (hi - lo) < 0.05:
        unavailable["reason"] = "Alívio insuficiente no recorte para gerar heightmap."
        return unavailable

    norm = np.clip((arr - lo) / (hi - lo + 1e-9), 0.0, 1.0)
    norm[arr <= _INVALID] = 0.0
    out = (norm * 255).astype("uint8")
    img = Image.fromarray(out, mode="L").resize((size, size), Image.BILINEAR)

    # --- Persistência (servida estaticamente em /dynamic_talhoes) ---
    folder = os.path.join(BASE_DIR, "dynamic_talhoes", f"farm_{farm_id}_talhao_{talhao_id}")
    os.makedirs(folder, exist_ok=True)
    out_path = os.path.join(folder, "heightmap.png")
    img.save(out_path)
    logger.info(
        "DEM: heightmap %s×%s gerado para farm_%s_talhao_%s (alívio %.1f m, %.2f–%.2f m)",
        size, size, farm_id, talhao_id, hi - lo, lo, hi,
    )

    return {
        "available": True,
        "farm_id": farm_id,
        "talhao_id": talhao_id,
        "size": size,
        "min_elevation_m": round(lo, 2),
        "max_elevation_m": round(hi, 2),
        "bounds": [round(min_lon, 6), round(min_lat, 6), round(max_lon, 6), round(max_lat, 6)],
        "source": _source_label(tif_path),
        "heightmap_url": (
            f"{settings.public_base_url}/dynamic_talhoes/farm_{farm_id}_talhao_{talhao_id}/heightmap.png"
        ),
        "reason": None,
    }
