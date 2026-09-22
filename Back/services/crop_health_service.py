"""Saúde & Evolução da Lavoura — análise temporal Sentinel-2 real.

Este serviço é deliberadamente separado do analytics legado. O contrato novo
não usa datas demonstrativas, texturas normalizadas ou valores sintéticos para
preencher uma série Sentinel. A resposta separa:

- ``source_data``: cenas, provedor, nuvens, máscara e cobertura;
- ``metrics``: estatísticas e deltas calculados dos pixels;
- ``interpretation``: tendência/atenção em linguagem conservadora.

Nenhum índice espectral é tratado como diagnóstico agronômico.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image

from config import settings
from services import copernicus_service as cdse
from services.climate_service import ClimateSourceError, build_climate_report

logger = logging.getLogger("orion.crop_health")

PROVIDER = "Copernicus Data Space Ecosystem"
COLLECTION = "sentinel-2-l2a"
PROCESSING_LEVEL = "L2A"
DEFAULT_PERIOD_DAYS = 730
DEFAULT_LIMIT = 60
MIN_TREND_SCENES = 3
MIN_TREND_INTERVAL_DAYS = 14
PERSISTENCE_SCENES = 3

# A resolução de B05/B11 é 20 m; o Process API entrega todos na grade pedida.
INDEX_CATALOG: dict[str, dict] = {
    "ndvi": {
        "label": "NDVI",
        "formula": "(B08 - B04) / (B08 + B04)",
        "bands": ["B08 (NIR)", "B04 (vermelho)"],
        "resolution": "10 m (B08/B04)",
        "interpretation": "vigor/atividade vegetativa relativa no talhão",
        "prohibited": "não diagnostica doença, praga, deficiência ou produtividade",
        "delta_floor": 0.05,
    },
    "ndre": {
        "label": "NDRE",
        "formula": "(B08 - B05) / (B08 + B05)",
        "bands": ["B08 (NIR)", "B05 (red edge)"],
        "resolution": "20 m (B05; reamostrado na grade do recorte)",
        "interpretation": "contraste espectral relativo associado à estrutura da vegetação",
        "prohibited": "não prova deficiência de nitrogênio ou clorofila",
        "delta_floor": 0.05,
    },
    "savi": {
        "label": "SAVI",
        "formula": "((B08 - B04) / (B08 + B04 + L)) × (1 + L), L = 0,5",
        "bands": ["B08 (NIR)", "B04 (vermelho)"],
        "resolution": "10 m (B08/B04)",
        "interpretation": "vigor relativo com redução da influência de solo exposto",
        "prohibited": "L=0,5 não é calibração universal e o índice não é diagnóstico",
        "delta_floor": 0.05,
    },
    "evi": {
        "label": "EVI",
        "formula": "2,5 × (B08 - B04) / (B08 + 6×B04 - 7,5×B02 + 1)",
        "bands": ["B08 (NIR)", "B04 (vermelho)", "B02 (azul)"],
        "resolution": "10 m (B02/B04/B08)",
        "interpretation": "vigor relativo com maior sensibilidade em vegetação densa",
        "prohibited": "não representa biomassa ou produtividade medida",
        "delta_floor": 0.05,
    },
    "ndwi": {
        "label": "NDWI (Gao)",
        "formula": "(B08 - B11) / (B08 + B11)",
        "bands": ["B08 (NIR)", "B11 (SWIR1)"],
        "resolution": "20 m (B11; reamostrado na grade do recorte)",
        "interpretation": "variação relativa compatível com conteúdo hídrico da vegetação",
        "prohibited": "não é NDWI de água superficial e não prova necessidade de irrigação",
        "delta_floor": 0.05,
    },
    "gndvi": {
        "label": "GNDVI",
        "formula": "(B08 - B03) / (B08 + B03)",
        "bands": ["B08 (NIR)", "B03 (verde)"],
        "resolution": "10 m (B03/B08)",
        "interpretation": "variação relativa do contraste NIR–verde",
        "prohibited": "não prova estado nutricional ou deficiência específica",
        "delta_floor": 0.05,
    },
    "ndmi": {
        "label": "NDMI",
        "formula": "(B08 - B11) / (B08 + B11)",
        "bands": ["B08 (NIR)", "B11 (SWIR1)"],
        "resolution": "20 m (B11; reamostrado na grade do recorte)",
        "interpretation": "variação relativa de umidade foliar/vegetação",
        "prohibited": "não prova estresse hídrico ou necessidade de irrigação",
        "delta_floor": 0.05,
    },
}
SUPPORTED_INDICES = tuple(INDEX_CATALOG)
ACCEPTABLE_QUALITIES = {"alta", "media"}

# Cache curto de respostas de metadados. Os rasters numéricos são mantidos pelo
# cache do CDSE; esta camada evita repetir a mesma sequência no dashboard.
_CACHE_TTL_SECONDS = max(0, int(settings.cdse_cache_hours * 3600))
_response_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_cache_lock = threading.RLock()
_RESPONSE_CACHE_MAX = 24


def clear_caches() -> None:
    with _cache_lock:
        _response_cache.clear()


def _cache_get(key: str) -> dict | None:
    with _cache_lock:
        entry = _response_cache.get(key)
        if not entry:
            return None
        expires, value = entry
        if _CACHE_TTL_SECONDS == 0 or time.monotonic() > expires:
            _response_cache.pop(key, None)
            return None
        _response_cache.move_to_end(key)
        return value


def _cache_set(key: str, value: dict) -> None:
    with _cache_lock:
        _response_cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)
        _response_cache.move_to_end(key)
        while len(_response_cache) > _RESPONSE_CACHE_MAX:
            _response_cache.popitem(last=False)


def _round_or_none(value, digits: int = 4):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, digits) if math.isfinite(value) else None


def _safe_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _geometry_cache_key(lat: float, lon: float, area_ha: float, kml_coordinates) -> str:
    return cdse.geometry_digest(lat, lon, area_ha, kml_coordinates)


def validate_index(index: str) -> str:
    normalized = str(index or "ndvi").lower().strip()
    if normalized not in SUPPORTED_INDICES:
        raise ValueError(f"Índice não suportado: {index}. Use: {', '.join(SUPPORTED_INDICES)}.")
    return normalized


def classify_scene_quality(valid_pixel_pct: float | None, cloud_cover: float | None) -> dict:
    """Critério objetivo de qualidade da aquisição no talhão.

    A cobertura válida é calculada após SCL/dataMask e geometria. Nuvem STAC
    é metadado da cena inteira. Quando uma das evidências falta, a qualidade
    nunca é promovida para alta.
    """
    valid = None if valid_pixel_pct is None else float(valid_pixel_pct)
    cloud = None if cloud_cover is None else float(cloud_cover)
    if valid is None or valid <= 0:
        level = "insuficiente"
    elif cloud is None:
        level = "media" if valid >= 75 else ("limitada" if valid >= 50 else "insuficiente")
    elif valid >= 90 and cloud <= 10:
        level = "alta"
    elif valid >= 75 and cloud <= 30:
        level = "media"
    elif valid >= 50 and cloud <= 60:
        level = "limitada"
    else:
        level = "insuficiente"
    labels = {"alta": "Alta", "media": "Média", "limitada": "Limitada", "insuficiente": "Insuficiente"}
    return {
        "level": level,
        "label": labels[level],
        "valid_pixel_pct": _round_or_none(valid, 1),
        "cloud_cover": _round_or_none(cloud, 2),
        "criteria": "Alta: cobertura válida ≥90% e nuvens ≤10%; Média: válida ≥75% e nuvens ≤30%; Limitada: válida ≥50% e nuvens ≤60%; caso contrário, insuficiente.",
    }


def _scene_from_calendar(item: dict) -> cdse.SceneInfo:
    return cdse.SceneInfo(
        item_id=str(item.get("product_id") or item.get("id") or ""),
        acquisition_date=str(item.get("date") or "")[:10],
        datetime=str(item.get("datetime") or item.get("date") or ""),
        cloud_cover=(float(item["cloud_cover"]) if item.get("cloud_cover") is not None else None),
        platform=item.get("platform"),
        constellation=item.get("constellation"),
        collection=str(item.get("collection") or COLLECTION),
        geometry=item.get("geometry"),
        bbox=item.get("bbox"),
        assets=item.get("assets") or {},
    )


def _empty_response(index: str, status: str, message: str, detail: dict | None = None) -> dict:
    catalog = INDEX_CATALOG[index]
    return {
        "status": status,
        "message": message,
        "index": index,
        "index_definition": catalog,
        "timeline": [],
        "source_data": {
            "provider": PROVIDER,
            "collection": COLLECTION,
            "processing_level": PROCESSING_LEVEL,
            "is_real": False,
            "calendar_source": "sentinel-cdse",
            "detail": detail,
        },
        "metrics": {
            "current": None,
            "previous": None,
            "delta_previous": None,
            "trend": "dados_insuficientes",
            "trend_label": "Dados insuficientes",
            "stability": None,
            "volatility": None,
            "persistence": {"status": "dados_insuficientes"},
            "internal_anomaly": {"status": "dados_insuficientes"},
        },
        "interpretation": {
            "trend": "Não há dados suficientes para avaliar tendência.",
            "attention": [],
            "scientific_note": "Índice espectral não é diagnóstico agronômico; os dados não permitem determinar a causa de uma eventual mudança.",
        },
        "quality": {"level": "insuficiente", "label": "Insuficiente", "scenes": 0},
        "confidence": {"level": "insuficiente", "label": "Insuficiente", "reason": "sem cenas Sentinel-2 válidas"},
    }


def _scene_source(scene: cdse.SceneInfo, stats: dict | None, quality: dict) -> dict:
    return {
        "provider": PROVIDER,
        "collection": scene.collection or COLLECTION,
        "processing_level": PROCESSING_LEVEL,
        "acquisition_date": scene.acquisition_date,
        "datetime": scene.datetime,
        "product_id": scene.item_id,
        "cloud_cover": quality.get("cloud_cover"),
        "valid_pixel_pct": quality.get("valid_pixel_pct"),
        "quality": quality.get("level"),
        "bands": list(cdse.BANDS),
        "mask": {"data_mask": True, "scl_invalid_classes": list(cdse.SCL_INVALID_CLASSES)},
    }


def _timeline_item(scene: cdse.SceneInfo, index: str, processed: dict | None, error: str | None = None) -> dict:
    stats = (processed or {}).get("stats") or {}
    valid_pct = stats.get("valid_pixel_pct")
    quality = classify_scene_quality(valid_pct, scene.cloud_cover)
    item = {
        "date": scene.acquisition_date,
        "index": index,
        "mean": _round_or_none(stats.get("mean")),
        "median": _round_or_none(stats.get("median")),
        "min": _round_or_none(stats.get("min")),
        "max": _round_or_none(stats.get("max")),
        "p25": _round_or_none(stats.get("p25")),
        "p75": _round_or_none(stats.get("p75")),
        "valid_pixel_pct": quality["valid_pixel_pct"],
        "cloud_cover": quality["cloud_cover"],
        "quality": quality["level"],
        "quality_label": quality["label"],
        "confidence": quality["level"],
        "source": "sentinel-cdse",
        "source_data": _scene_source(scene, stats, quality),
        "delta_previous": None,
        "delta_30d": None,
    }
    if error:
        item["status"] = "sem_dados"
        item["error"] = error
    else:
        item["status"] = "ok" if item["mean"] is not None else "sem_dados"
    return item


def _enrich_deltas(timeline: list[dict]) -> None:
    ordered = sorted(timeline, key=lambda x: x["date"])
    for i, item in enumerate(ordered):
        if i > 0 and item.get("mean") is not None and ordered[i - 1].get("mean") is not None:
            item["delta_previous"] = _round_or_none(item["mean"] - ordered[i - 1]["mean"])
        target = _safe_date(item["date"]) - timedelta(days=30)
        candidates = [
            x for x in ordered[:i]
            if x.get("mean") is not None and abs((_safe_date(x["date"]) - target).days) <= 15
        ]
        if candidates:
            base = min(candidates, key=lambda x: abs((_safe_date(x["date"]) - target).days))
            item["delta_30d"] = _round_or_none(item["mean"] - base["mean"])


def _accepted_points(timeline: list[dict]) -> list[dict]:
    return [x for x in timeline if x.get("mean") is not None and x.get("quality") in ACCEPTABLE_QUALITIES]


def classify_trend(timeline: list[dict], index: str = "ndvi") -> dict:
    """Classifica tendência somente com cenas de boa/média qualidade.

    São necessárias pelo menos três cenas, intervalo total de 14 dias e
    direção consistente dos intervalos. A magnitude é absoluta no índice;
    percentual do valor não é usado.
    """
    index = validate_index(index)
    points = sorted(_accepted_points(timeline), key=lambda x: x["date"])
    insufficient = {
        "code": "dados_insuficientes", "label": "Dados insuficientes",
        "text": "Não há dados suficientes para avaliar tendência.",
        "scenes_used": len(points), "delta": None, "slope_per_day": None,
    }
    if len(points) < MIN_TREND_SCENES:
        return insufficient
    span = (_safe_date(points[-1]["date"]) - _safe_date(points[0]["date"])).days
    if span < MIN_TREND_INTERVAL_DAYS:
        return {**insufficient, "reason": "intervalo temporal menor que 14 dias"}
    values = np.asarray([float(p["mean"]) for p in points], dtype=float)
    changes = np.diff(values)
    floor = float(INDEX_CATALOG[index]["delta_floor"])
    total_delta = float(values[-1] - values[0])
    # A direção consistente pode ser formada por passos menores que o piso;
    # o piso é aplicado à mudança acumulada, evitando perder uma tendência
    # gradual apenas porque cada passagem individual mudou pouco.
    signs = [1 if c > 0 else -1 if c < 0 else 0 for c in changes]
    nonzero = [s for s in signs if s]
    slope = float(np.polyfit(np.arange(len(values)), values, 1)[0] / max(span / max(len(values) - 1, 1), 1))
    if total_delta >= floor and len(nonzero) >= 2 and all(s == 1 for s in nonzero):
        code, label, text = "melhoria", "Melhoria", "Melhoria recente de vigor espectral."
    elif total_delta <= -floor and len(nonzero) >= 2 and all(s == -1 for s in nonzero):
        code, label, text = "queda", "Queda", "Queda recente de vigor espectral."
    elif abs(total_delta) < floor or len(nonzero) < 2 or (nonzero and len(set(nonzero)) > 1):
        code, label, text = "estavel", "Estável", "Vigor espectral sem mudança direcional consistente no período."
    else:
        code, label, text = "estavel", "Estável", "Variação espectral sem evidência suficiente de tendência direcional."
    return {
        "code": code, "label": label, "text": text,
        "scenes_used": len(points), "delta": _round_or_none(total_delta),
        "slope_per_day": _round_or_none(slope, 6), "span_days": span,
        "absolute_threshold": floor,
        "direction_sequence": signs,
    }


def _adaptive_change_threshold(delta_values: np.ndarray, index: str) -> float:
    floor = float(INDEX_CATALOG[validate_index(index)]["delta_floor"])
    if delta_values.size == 0:
        return floor
    center = float(np.median(delta_values))
    mad = float(np.median(np.abs(delta_values - center)))
    # Limite adaptativo robusto, com piso fixo documentado e teto para evitar
    # que uma cena muito heterogênea esconda toda mudança relevante.
    return round(max(floor, min(0.10, 2.0 * mad)), 4)


def compute_change_zones(
    delta: np.ndarray,
    valid_pair: np.ndarray,
    polygon_mask: np.ndarray,
    area_ha: float,
    index: str = "ndvi",
) -> dict:
    """Calcula zonas A/B nos pixels pareados dentro da geometria real."""
    index = validate_index(index)
    valid = np.asarray(valid_pair, dtype=bool) & np.asarray(polygon_mask, dtype=bool)
    values = np.asarray(delta, dtype="float32")
    valid &= np.isfinite(values)
    polygon_pixels = max(int(np.sum(np.asarray(polygon_mask, dtype=bool))), 1)
    pair_values = values[valid]
    threshold = _adaptive_change_threshold(pair_values, index)
    improved = valid & (values >= threshold)
    declined = valid & (values <= -threshold)
    stable = valid & ~(improved | declined)
    observed = int(np.sum(valid))
    unobserved = max(polygon_pixels - observed, 0)

    def bucket(mask: np.ndarray) -> dict:
        pixels = int(np.sum(mask))
        pct = 100.0 * pixels / polygon_pixels
        return {"pixels": pixels, "pct": round(pct, 2), "ha": round(area_ha * pct / 100.0, 4)}

    observed_pct = 100.0 * observed / polygon_pixels
    return {
        "threshold_absolute": threshold,
        "method": "delta = índice_B − índice_A; limiar = máximo entre piso do índice e 2×MAD dos deltas pareados, limitado a 0,10",
        "improved": bucket(improved),
        "stable": bucket(stable),
        "declined": bucket(declined),
        "unobserved": {"pixels": unobserved, "pct": round(100.0 * unobserved / polygon_pixels, 2), "ha": round(area_ha * unobserved / polygon_pixels, 4)},
        "observed_pair": {"pixels": observed, "pct": round(observed_pct, 2), "ha": round(area_ha * observed / polygon_pixels, 4)},
        "delta_min": _round_or_none(np.min(pair_values)) if pair_values.size else None,
        "delta_max": _round_or_none(np.max(pair_values)) if pair_values.size else None,
        "delta_mean": _round_or_none(np.mean(pair_values)) if pair_values.size else None,
        "delta_median": _round_or_none(np.median(pair_values)) if pair_values.size else None,
        "_masks": {"improved": improved, "stable": stable, "declined": declined, "valid": valid},
    }


def compute_internal_anomaly(values: np.ndarray, valid_mask: np.ndarray, area_ha: float, index: str = "ndvi") -> dict:
    """Identifica divergência relativa à própria distribuição da cena."""
    index = validate_index(index)
    array = np.asarray(values, dtype=float)
    valid = np.asarray(valid_mask, dtype=bool) & np.isfinite(array)
    selected = array[valid]
    if selected.size == 0:
        return {"status": "dados_insuficientes", "message": "Não há pixels válidos para baseline interno."}
    median = float(np.median(selected))
    mad = float(np.median(np.abs(selected - median)))
    threshold = max(float(INDEX_CATALOG[index]["delta_floor"]), min(0.12, 2.0 * mad))
    low = valid & (array <= median - threshold)
    high = valid & (array >= median + threshold)
    total = max(int(selected.size), 1)
    def area(mask):
        pixels = int(np.sum(mask)); pct = pixels * 100.0 / total
        return {"pixels": pixels, "pct": round(pct, 2), "ha": round(area_ha * pct / 100.0, 4)}
    low_gap = median - float(np.median(array[low])) if np.any(low) else None
    return {
        "status": "ok", "baseline": "mediana dos pixels válidos da própria cena",
        "talhao_median": _round_or_none(median), "threshold_absolute": round(threshold, 4),
        "below_internal_baseline": area(low), "above_internal_baseline": area(high),
        "low_zone_gap_to_median": _round_or_none(low_gap),
        "text": (
            f"Zona divergente abaixo da mediana interna por pelo menos {threshold:.2f} no {INDEX_CATALOG[index]['label']}."
            if np.any(low) else "Não foi identificada zona abaixo do baseline interno com o limiar adotado."
        ),
        "scientific_note": "A divergência é relativa ao próprio talhão e não determina a causa agronômica.",
    }


def compute_persistence(scene_results: list[dict], area_ha: float, index: str = "ndvi") -> dict:
    """Calcula mudança persistente em cenas consecutivas.

    ``scene_results`` contém resultados de ``process_farm_scene``. Três cenas
    fornecem duas transições; uma área só é persistente quando mantém a mesma
    direção nas duas transições acima do limiar.
    """
    index = validate_index(index)
    usable = [r for r in scene_results if r and r.get("stats") and r.get("valid_mask") is not None]
    usable = sorted(usable, key=lambda r: r["date"])
    if len(usable) < PERSISTENCE_SCENES:
        return {"status": "dados_insuficientes", "scenes_used": len(usable), "message": "São necessárias pelo menos três cenas válidas para avaliar persistência."}
    usable = usable[-PERSISTENCE_SCENES:]
    polygon = np.asarray(usable[0]["polygon_mask"], dtype=bool)
    valid = polygon.copy()
    transitions = []
    for prev, current in zip(usable[:-1], usable[1:]):
        pair = np.asarray(prev["valid_mask"], dtype=bool) & np.asarray(current["valid_mask"], dtype=bool) & polygon
        delta = np.asarray(current["values"]) - np.asarray(prev["values"])
        pair &= np.isfinite(delta)
        threshold = _adaptive_change_threshold(delta[pair], index)
        transitions.append((pair, delta, threshold))
        valid &= pair
    if not transitions or not np.any(valid):
        return {"status": "dados_insuficientes", "scenes_used": len(usable), "message": "Não há pixels pareados suficientes para avaliar persistência."}
    decline = valid.copy(); improve = valid.copy()
    for pair, delta, threshold in transitions:
        decline &= delta <= -threshold
        improve &= delta >= threshold
    polygon_pixels = max(int(np.sum(polygon)), 1)
    def area(mask):
        pixels = int(np.sum(mask)); pct = pixels * 100.0 / polygon_pixels
        return {"pixels": pixels, "pct": round(pct, 2), "ha": round(area_ha * pct / 100.0, 4)}
    return {
        "status": "ok", "scenes_used": len(usable),
        "dates": [r["date"] for r in usable],
        "declining": area(decline), "improving": area(improve),
        "thresholds_absolute": [round(t[2], 4) for t in transitions],
        "text": "Nível de atenção elevado por persistência espectral; isso não confirma problema agronômico.",
    }


def _climate_context(lat: float, lon: float, start: date, end: date) -> dict:
    try:
        report = build_climate_report(lat, lon, start, end)
        metrics = report.get("metrics", {})
        precip = metrics.get("precipitation", {})
        temp = metrics.get("temperature", {})
        rad = metrics.get("radiation", {})
        return {
            "status": "ok",
            "source": report.get("source"),
            "period": report.get("period"),
            "coverage": report.get("coverage"),
            "precipitation": {
                "accumulated_mm": precip.get("accumulated_mm"),
                "available_days": precip.get("available_days"),
                "baseline": precip.get("baseline"),
            },
            "temperature": {"mean_c": temp.get("mean_c"), "available_days": temp.get("available_days"), "baseline": temp.get("baseline")},
            "radiation": {"daily_mean_mj_m2": rad.get("daily_mean_mj_m2"), "available_days": rad.get("available_days"), "baseline": rad.get("baseline")},
            "interpretation": "As condições climáticas ocorreram no mesmo intervalo das cenas. Essa associação temporal não estabelece causalidade.",
        }
    except (ClimateSourceError, Exception) as exc:
        return {
            "status": "unavailable",
            "message": "Contexto climático indisponível; a análise espectral continua válida sem ele.",
            "error_type": type(exc).__name__,
        }


def _base_context(lat, lon, area_ha, kml_coordinates, index, start, end, limit) -> tuple[list[dict], str, dict | None]:
    return cdse.fetch_real_calendar(
        lat=lat, lon=lon, area_ha=area_ha, kml_coordinates=kml_coordinates,
        limit=limit, lookback_days=None, start_date=start, end_date=end,
    )


def get_health_timeline(
    farm_id: int,
    talhao_id: int,
    lat: float,
    lon: float,
    area_ha: float,
    kml_coordinates: str | None,
    index: str = "ndvi",
    period_days: int = DEFAULT_PERIOD_DAYS,
    start: date | None = None,
    end: date | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    index = validate_index(index)
    if start is None and end is None:
        end = date.today()
        start = end - timedelta(days=max(30, int(period_days)) - 1)
    elif start is None or end is None:
        raise ValueError("start e end devem ser informados juntos")
    if start > end:
        raise ValueError("start não pode ser posterior a end")
    cache_key = "timeline:" + hashlib.sha1(json.dumps({
        "farm": farm_id, "talhao": talhao_id, "geometry": _geometry_cache_key(lat, lon, area_ha, kml_coordinates),
        "index": index, "start": start.isoformat(), "end": end.isoformat(), "limit": limit,
    }, sort_keys=True).encode()).hexdigest()
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    calendar, calendar_status, detail = _base_context(lat, lon, area_ha, kml_coordinates, index, start, end, min(120, max(1, limit)))
    if not calendar:
        status = "unavailable" if calendar_status == "error" else "insufficient_data"
        message = "Dados Sentinel-2 indisponíveis no momento." if status == "unavailable" else "Não há dados suficientes para avaliar tendência."
        result = _empty_response(index, status, message, detail or {"calendar_status": calendar_status, "period": {"start": start.isoformat(), "end": end.isoformat()}})
        result["period"] = {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1}
        _cache_set(cache_key, result)
        return result

    records: list[dict] = []
    processed: list[dict] = []
    for item in sorted(calendar, key=lambda x: x.get("date", "")):
        scene = _scene_from_calendar(item)
        try:
            raster = cdse.process_farm_scene(
                farm_id=farm_id, talhao_id=talhao_id, lat=lat, lon=lon,
                area_ha=area_ha, kml_coordinates=kml_coordinates,
                scene=scene, index=index,
            )
            records.append(_timeline_item(scene, index, raster))
            processed.append(raster)
        except Exception as exc:
            logger.warning("Saúde & Evolução: cena %s indisponível (%s)", scene.acquisition_date, type(exc).__name__)
            records.append(_timeline_item(scene, index, None, f"Cena não processada ({type(exc).__name__})."))
    records.sort(key=lambda x: x["date"])
    _enrich_deltas(records)
    trend = classify_trend(records, index)
    valid_records = _accepted_points(records)
    means = np.asarray([r["mean"] for r in valid_records], dtype=float) if valid_records else np.asarray([])
    volatility = _round_or_none(np.std(means), 4) if means.size > 1 else None
    stability = "estavel" if volatility is not None and volatility < INDEX_CATALOG[index]["delta_floor"] else ("variavel" if volatility is not None else None)
    current_raster = processed[-1] if processed else None
    persistence = compute_persistence(processed, area_ha, index)
    anomaly = compute_internal_anomaly(current_raster["values"], current_raster["valid_mask"], area_ha, index) if current_raster else {"status": "dados_insuficientes"}
    current = records[-1] if records else None
    previous = records[-2] if len(records) > 1 else None
    quality_levels = [r["quality"] for r in records]
    quality_level = (
        "alta" if quality_levels and all(q == "alta" for q in quality_levels)
        else "media" if quality_levels and all(q in {"alta", "media"} for q in quality_levels)
        else "limitada"
    )
    result = {
        "status": "ok" if valid_records else "insufficient_data",
        "message": None if valid_records else "Não há pixels válidos suficientes para avaliar tendência.",
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1},
        "index": index,
        "index_definition": INDEX_CATALOG[index],
        "timeline": records,
        "source_data": {
            "provider": PROVIDER, "collection": COLLECTION, "processing_level": PROCESSING_LEVEL,
            "calendar_source": "sentinel-cdse", "calendar_status": calendar_status,
            "is_real": True, "scene_count": len(records), "valid_scene_count": len(valid_records),
        },
        "metrics": {
            "current": current, "previous": previous,
            "delta_previous": current.get("delta_previous") if current else None,
            "delta_30d": current.get("delta_30d") if current else None,
            "trend": trend["code"], "trend_label": trend["label"], "trend_detail": trend,
            "stability": stability, "volatility": volatility,
            "persistence": persistence, "internal_anomaly": anomaly,
        },
        "interpretation": {
            "trend": trend["text"],
            "attention": [
                *(["Área com queda espectral persistente."] if persistence.get("status") == "ok" and persistence.get("declining", {}).get("pixels", 0) else []),
                *(["Zona apresenta comportamento diferente do restante do talhão."] if anomaly.get("status") == "ok" and anomaly.get("below_internal_baseline", {}).get("pixels", 0) else []),
                *(["Há cenas com cobertura limitada; a interpretação deve ser feita com cautela."] if any(r.get("quality") == "limitada" for r in records) else []),
            ],
            "scientific_note": "Índice espectral não é diagnóstico agronômico. Os dados descrevem mudança relativa e não permitem determinar sua causa.",
        },
        "quality": {
            "level": quality_level, "label": {"alta": "Alta", "media": "Média", "limitada": "Limitada"}[quality_level],
            "scenes": len(records), "valid_scenes": len(valid_records),
            "criteria": classify_scene_quality(90, 10)["criteria"],
        },
        "confidence": {
            "level": quality_level,
            "label": {"alta": "Alta", "media": "Média", "limitada": "Limitada"}[quality_level],
            "basis": "cobertura válida pós-SCL/dataMask, cobertura de nuvens e qualidade individual das cenas",
        },
    }
    _cache_set(cache_key, result)
    return result


def _find_scene(calendar: list[dict], target: str) -> dict | None:
    return next((item for item in calendar if item.get("date") == target), None)


def _difference_png(path: str, delta: np.ndarray, valid: np.ndarray, polygon: np.ndarray, threshold: float) -> None:
    values = np.asarray(delta, dtype=float)
    valid = np.asarray(valid, dtype=bool) & np.asarray(polygon, dtype=bool) & np.isfinite(values)
    h, w = values.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[~np.asarray(polygon, dtype=bool)] = [13, 17, 23, 0]
    inside_invalid = np.asarray(polygon, dtype=bool) & ~valid
    rgba[inside_invalid] = [80, 80, 90, 210]
    scale = max(threshold * 2.0, float(np.percentile(np.abs(values[valid]), 98)) if np.any(valid) else threshold)
    norm = np.clip(values / max(scale, 1e-6), -1.0, 1.0)
    pos = valid & (values >= threshold)
    neg = valid & (values <= -threshold)
    stable = valid & ~(pos | neg)
    rgba[stable] = [156, 163, 175, 230]
    # verde = aumento; laranja/vermelho = queda; neutro = cinza.
    for mask, start, end in ((pos, np.array([200, 245, 210]), np.array([35, 160, 70])), (neg, np.array([255, 220, 180]), np.array([218, 54, 51]))):
        strength = np.clip(np.abs(norm[mask]), 0, 1)[:, None]
        rgba[mask, :3] = (start + (end - start) * strength).astype(np.uint8)
        rgba[mask, 3] = 240
    Image.fromarray(rgba, "RGBA").save(path, format="PNG")


def compare_dates(
    farm_id: int,
    talhao_id: int,
    lat: float,
    lon: float,
    area_ha: float,
    kml_coordinates: str | None,
    date_a: str,
    date_b: str,
    index: str = "ndvi",
    include_climate: bool = False,
) -> dict:
    index = validate_index(index)
    a = _safe_date(date_a); b = _safe_date(date_b)
    if a >= b:
        raise ValueError("date_a deve ser anterior a date_b")
    calendar, calendar_status, detail = cdse.fetch_real_calendar(
        lat=lat, lon=lon, area_ha=area_ha, kml_coordinates=kml_coordinates,
        limit=120, start_date=a, end_date=b,
    )
    scene_a_data = _find_scene(calendar, a.isoformat())
    scene_b_data = _find_scene(calendar, b.isoformat())
    if not scene_a_data or not scene_b_data:
        result = _empty_response(index, "insufficient_data", "As duas datas precisam corresponder a aquisições Sentinel-2 reais disponíveis no catálogo.", {"date_a": date_a, "date_b": date_b, "calendar_status": calendar_status, "detail": detail})
        result["comparison"] = {"date_a": date_a, "date_b": date_b, "status": "dados_insuficientes"}
        return result
    scene_a = _scene_from_calendar(scene_a_data); scene_b = _scene_from_calendar(scene_b_data)
    try:
        raster_a = cdse.process_farm_scene(farm_id, talhao_id, lat, lon, area_ha, kml_coordinates, scene_a, index)
        raster_b = cdse.process_farm_scene(farm_id, talhao_id, lat, lon, area_ha, kml_coordinates, scene_b, index)
    except Exception as exc:
        result = _empty_response(index, "unavailable", "Não foi possível processar as duas cenas Sentinel-2.", {"error_type": type(exc).__name__})
        result["comparison"] = {"date_a": date_a, "date_b": date_b, "status": "indisponivel"}
        return result
    pair = np.asarray(raster_a["valid_mask"], dtype=bool) & np.asarray(raster_b["valid_mask"], dtype=bool)
    polygon = np.asarray(raster_a["polygon_mask"], dtype=bool) & np.asarray(raster_b["polygon_mask"], dtype=bool)
    delta = np.asarray(raster_b["values"], dtype=float) - np.asarray(raster_a["values"], dtype=float)
    zones = compute_change_zones(delta, pair, polygon, area_ha, index)
    quality_a = classify_scene_quality(raster_a["stats"].get("valid_pixel_pct"), scene_a.cloud_cover)
    quality_b = classify_scene_quality(raster_b["stats"].get("valid_pixel_pct"), scene_b.cloud_cover)
    out_dir = Path(cdse.cdse_dir(farm_id, talhao_id, "crop_health"))
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{index}_{a.isoformat()}_{b.isoformat()}_delta.png"
    map_path = str(out_dir / safe_name)
    _difference_png(map_path, delta, zones["_masks"]["valid"], polygon, zones["threshold_absolute"])
    zones.pop("_masks", None)
    climate = _climate_context(lat, lon, a, b) if include_climate else {"status": "not_requested", "message": "Contexto climático não solicitado."}
    delta_mean = zones.get("delta_mean")
    direction = "melhoria" if (delta_mean or 0) > zones["threshold_absolute"] else "queda" if (delta_mean or 0) < -zones["threshold_absolute"] else "estável"
    return {
        "status": "ok",
        "comparison": {
            "date_a": a.isoformat(), "date_b": b.isoformat(), "index": index,
            "index_a": raster_a["stats"], "index_b": raster_b["stats"],
            "delta_mean": delta_mean, "delta_median": zones.get("delta_median"),
            "direction": direction, "zones": zones,
            "quality_a": quality_a, "quality_b": quality_b,
            "confidence_a": quality_a, "confidence_b": quality_b,
            "difference_map": {
                "url_path": f"/api/farms/{farm_id}/crop-health/difference.png?index={index}&date_a={a.isoformat()}&date_b={b.isoformat()}",
                "legend": {"positive": "aumento do índice", "zero": "estabilidade", "negative": "redução do índice"},
                "source": "Sentinel-2 / Copernicus",
            },
        },
        "source_data": {
            "provider": PROVIDER, "collection": COLLECTION, "processing_level": PROCESSING_LEVEL,
            "is_real": True, "bands": list(cdse.BANDS),
            "scene_a": _scene_source(scene_a, raster_a["stats"], quality_a),
            "scene_b": _scene_source(scene_b, raster_b["stats"], quality_b),
        },
        "metrics": {"zones": zones, "delta_absolute": delta_mean, "affected_area_ha": round(zones["declined"]["ha"] + zones["improved"]["ha"], 4)},
        "interpretation": {
            "text": f"Entre {a.strftime('%d/%m/%Y')} e {b.strftime('%d/%m/%Y')} houve {direction} no {INDEX_CATALOG[index]['label']} médio. A mudança espacial é descritiva e merece investigação quando persistente.",
            "attention": ["Área com redução relevante." if zones["declined"]["pixels"] else "Nenhuma área com redução acima do limiar na cobertura pareada.", "Os dados não permitem determinar a causa da mudança."],
            "scientific_note": "Índice espectral não é diagnóstico agronômico; coincidência temporal com clima não estabelece causalidade.",
        },
        "climate_context": climate,
        "_difference_path": map_path,
    }


def difference_path_for(farm_id: int, talhao_id: int, index: str, date_a: str, date_b: str) -> str:
    return os.path.join(cdse.cdse_dir(farm_id, talhao_id, "crop_health"), f"{index}_{date_a}_{date_b}_delta.png")
