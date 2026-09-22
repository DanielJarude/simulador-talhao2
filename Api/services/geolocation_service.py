"""Fonte geográfica canônica: geometria, ponto representativo e geocodificação.

A persistência histórica usa JSON de pares ``[latitude, longitude]``. Este
módulo também aceita GeoJSON Polygon/MultiPolygon sem alterar a geometria.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import unicodedata
from dataclasses import dataclass

import requests
from config import settings

logger = logging.getLogger("orion.geolocation")
_cache: dict[tuple[float, float], tuple[float, dict]] = {}
_lock = threading.Lock()


class GeometryError(ValueError):
    pass


def validate_coordinates(lat: float, lon: float) -> tuple[float, float]:
    lat, lon = float(lat), float(lon)
    if not math.isfinite(lat) or not -90 <= lat <= 90:
        raise ValueError("Latitude inválida: informe um valor entre -90 e 90.")
    if not math.isfinite(lon) or not -180 <= lon <= 180:
        raise ValueError("Longitude inválida: informe um valor entre -180 e 180.")
    return lat, lon


def _ring_latlon(ring, geojson=False):
    out = []
    for p in ring:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise GeometryError("Coordenada inválida na geometria.")
        lat, lon = (p[1], p[0]) if geojson else (p[0], p[1])
        out.append(validate_coordinates(lat, lon))
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    if len(out) < 3 or len(set(out)) < 3:
        raise GeometryError("Polígono vazio ou com menos de três pontos distintos.")
    return out


def parse_geometry(value) -> list[list[tuple[float, float]]]:
    """Retorna polígonos (anel externo) como pares lat/lon."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception as exc:
            raise GeometryError("KML/GeoJSON inválido.") from exc
    if not value:
        raise GeometryError("Polígono vazio.")
    if isinstance(value, dict):
        if value.get("type") == "Feature": value = value.get("geometry")
        if value.get("type") == "FeatureCollection":
            features = value.get("features") or []
            if not features: raise GeometryError("GeoJSON sem feições.")
            value = features[0].get("geometry")
        typ, coords = value.get("type"), value.get("coordinates")
        if typ == "Polygon": return [_ring_latlon(coords[0], True)]
        if typ == "MultiPolygon": return [_ring_latlon(poly[0], True) for poly in coords]
        raise GeometryError("GeoJSON deve conter Polygon ou MultiPolygon.")
    # legado: [[lat,lon], ...], ou multipolígono legado
    if isinstance(value, list) and value and isinstance(value[0], (list, tuple)):
        if value[0] and isinstance(value[0][0], (int, float)):
            return [_ring_latlon(value)]
        return [_ring_latlon(ring) for ring in value]
    raise GeometryError("Formato de geometria não suportado.")


def _signed_area_centroid(ring):
    # Projeção equiretangular local evita tratar graus lat/lon como cartesianos.
    lat0 = math.radians(sum(p[0] for p in ring) / len(ring))
    pts = [(math.radians(lon) * math.cos(lat0), math.radians(lat)) for lat, lon in ring]
    cross_sum = cx = cy = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        c = x1*y2 - x2*y1; cross_sum += c; cx += (x1+x2)*c; cy += (y1+y2)*c
    if abs(cross_sum) < 1e-15: raise GeometryError("Polígono sem área válida.")
    x, y = cx/(3*cross_sum), cy/(3*cross_sum)
    return abs(cross_sum/2), (math.degrees(y), math.degrees(x/math.cos(lat0)))


def _inside(point, ring):
    y, x = point; inside = False
    for i, (y1, x1) in enumerate(ring):
        y2, x2 = ring[(i+1) % len(ring)]
        if ((y1 > y) != (y2 > y)) and x < (x2-x1)*(y-y1)/(y2-y1)+x1: inside = not inside
    return inside


def representative_point(value) -> tuple[float, float]:
    """Centroide ponderado por área; em geometria côncava usa ponto interior.

    O fallback interior é o centro do segmento interno mais largo em linhas
    horizontais candidatas (point-on-surface), melhor que média de vértices.
    """
    polygons = parse_geometry(value)
    parts = [_signed_area_centroid(r) for r in polygons]
    total = sum(a for a, _ in parts)
    candidate = (sum(a*p[0] for a,p in parts)/total, sum(a*p[1] for a,p in parts)/total)
    if any(_inside(candidate, r) for r in polygons): return validate_coordinates(*candidate)
    # escolha o maior componente e obtenha um ponto garantidamente interior
    ring = polygons[max(range(len(parts)), key=lambda i: parts[i][0])]
    ys = sorted({p[0] for p in ring} | {sum(p[0] for p in ring)/len(ring)})
    best = None
    for y in ys + [(min(ys)+max(ys))/2]:
        xs=[]
        for i,(y1,x1) in enumerate(ring):
            y2,x2=ring[(i+1)%len(ring)]
            if (y1 <= y < y2) or (y2 <= y < y1): xs.append(x1+(y-y1)*(x2-x1)/(y2-y1))
        xs.sort()
        for a,b in zip(xs[::2], xs[1::2]):
            if best is None or b-a > best[0]: best=(b-a,(y,(a+b)/2))
    if not best: raise GeometryError("Não foi possível obter ponto interior.")
    return validate_coordinates(*best[1])


def normalize_text(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower().split())


def location_matches(city_a, state_a, city_b, state_b) -> bool:
    states = {"parana":"pr", "sao paulo":"sp", "mato grosso do sul":"ms", "minas gerais":"mg"}
    norm_state=lambda x: states.get(normalize_text(x), normalize_text(x).replace("br-", ""))
    return normalize_text(city_a) == normalize_text(city_b) and norm_state(state_a) == norm_state(state_b)


def distance_km(a, b):
    lat1,lon1=map(math.radians,a); lat2,lon2=map(math.radians,b)
    h=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371.0088*2*math.asin(math.sqrt(h))


def reverse_geocode(lat: float, lon: float) -> dict:
    lat, lon = validate_coordinates(lat, lon); key=(round(lat,5),round(lon,5))
    with _lock:
        cached=_cache.get(key)
        if cached and time.monotonic()-cached[0] < settings.geocoder_cache_hours*3600: return {**cached[1], "cached": True}
    try:
        response=requests.get(settings.geocoder_url, params={"lat":lat,"lon":lon,"format":"jsonv2","addressdetails":1,"accept-language":"pt-BR"}, headers={"User-Agent":settings.geocoder_user_agent}, timeout=settings.geocoder_timeout_s)
        response.raise_for_status(); data=response.json(); address=data.get("address", {})
        city=address.get("city") or address.get("town") or address.get("municipality") or address.get("village")
        state=address.get("state"); code=(address.get("ISO3166-2-lvl4") or "").split("-")[-1] or None
        result={"latitude":lat,"longitude":lon,"city":city,"state":state,"state_code":code,"country":address.get("country") or "Brasil","country_code":str(address.get("country_code") or "BR").upper(),"source":"nominatim","confidence":None,"status":"ok" if city else "municipality_not_found"}
    except requests.Timeout:
        result={"latitude":lat,"longitude":lon,"city":None,"state":None,"state_code":None,"country":"Brasil","country_code":"BR","source":"nominatim","confidence":None,"status":"timeout"}
    except Exception:
        logger.warning("Reverse geocoder indisponível", exc_info=True)
        result={"latitude":lat,"longitude":lon,"city":None,"state":None,"state_code":None,"country":"Brasil","country_code":"BR","source":"nominatim","confidence":None,"status":"unavailable"}
    if result["status"] in ("ok","municipality_not_found"):
        with _lock: _cache[key]=(time.monotonic(),result)
    return result
