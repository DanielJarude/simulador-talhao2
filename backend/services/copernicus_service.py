"""
Serviço Copernicus Data Space Ecosystem (CDSE) — dados REAIS do talhão.

Pipeline (confirmado na documentação oficial — documentation.dataspace.
copernicus.eu, 2026):

1. STAC search na coleção ``sentinel-2-l2a`` com ``intersects`` (polígono da
   fazenda) e janela temporal configurável;
2. seleção da MELHOR aquisição: filtro de cobertura de nuvens (máx.
   configurável) → mais recente → relaxamento gradual explícito;
3. Process API (``POST https://sh.dataspace.copernicus.eu/process/v1``):
   raster APENAS da área da fazenda com as bandas B02/B03/B04/B05/B08/B11 +
   SCL (Scene Classification Layer) + dataMask — nunca baixamos o produto
   inteiro (centenas de MB);
4. máscara de nuvens/sombra/nodata via SCL e dataMask;
5. índices NDVI/EVI/NDRE/NDMI e RGB true color calculados no backend
   (fórmulas testadas numericamente em tests/test_copernicus_service.py);
6. PNG (256×256, máscara pelo polígono) + estatísticas + proveniência,
   tudo cacheado (memória TTL + disco em dynamic_talhoes/..., fora do Git).

Resiliência:
- sem client_id/secret → ``not_configured`` (fallback procedural explícito);
- 401/403 → auth negada; 429 → 1 retry com backoff; 5xx/timeout/JSON ou
  imagem inválida → ``error`` com status/mensagem segura;
- NUNCA logs de client_secret/access_token/refresh_token;
- NUNCA agrupa credenciais em cache de dados.

Este módulo não faz import de ``main`` nem de rotas: todo HTTP do Copernicus
vive aqui.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
import rasterio
import requests
from PIL import Image, ImageDraw

from config import settings

logger = logging.getLogger("orion.copernicus")

# ---------------------------------------------------------------------------
# Constantes (confirmadas na documentação oficial)
# ---------------------------------------------------------------------------
COLLECTION = "sentinel-2-l2a"
PROVIDER = "Copernicus Data Space Ecosystem"
PROCESSING_LEVEL = "L2A"
#: Identificadores inequívocos da fonte do calendário (PR #5e — correção).
#: `sentinel-cdse` = catálogo STAC real; `config_fallback` = datas de
#: demonstração (NUNCA apresentadas como se fossem aquisições reais).
CALENDAR_REAL_SOURCE = "sentinel-cdse"
CALENDAR_FALLBACK_SOURCE = "config_fallback"
#: Bandas usadas no Process API (nomes oficiais do Sentinel-2 L2A no CDSE).
#: B05 e B11 são de 20 m — o Process API entrega na grade pedida (10 m).
BANDS = ["B02", "B03", "B04", "B05", "B08", "B11"]
SUPPORTED_LAYERS = ("rgb", "ndvi", "evi", "ndre", "ndmi")

#: Classes SCL tratadas como INVÁLIDAS para agricultura (nunca calcular índice
#: sobre elas): nodata(0), saturado/defeito(1), pixels escuros/sombra(2),
#: sombra de nuvem(3), nuvem média(8), nuvem alta(9), cirrus(10), neve(11).
SCL_INVALID_CLASSES = (0, 1, 2, 3, 8, 9, 10, 11)
#: Resto (4 vegetação, 5 não-vegetado, 6 água, 7 não classificado) é válido.
SCL_VALID_CLASSES = (4, 5, 6, 7)

_EPS = 1e-9

# ---------------------------------------------------------------------------
# Exceções + estado
# ---------------------------------------------------------------------------
class CopernicusNotConfigured(RuntimeError):
    """Credenciais CDSE ausentes — o sistema deve cair no fallback explícito."""


class CopernicusError(RuntimeError):
    """Erro operacional do CDSE (rede, auth, rate limit, dado inválido).

    Carrega diagnóstico SEGURO (nunca segredos/Authorization): status HTTP,
    endpoint, Content-Type e um trecho do corpo devolvido pelo provedor
    (limitado e sanitizado).
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        *,
        endpoint: str | None = None,
        content_type: str | None = None,
        body_snippet: str | None = None,
        stage: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint
        self.content_type = content_type
        self.body_snippet = body_snippet
        self.stage = stage

    def to_detail(self) -> dict:
        """Representação JSON-safe p/ UI/relatório (sem credenciais)."""
        return {
            "stage": self.stage,
            "endpoint": self.endpoint,
            "http_status": self.status,
            "content_type": self.content_type,
            "message": self.body_snippet or str(self),
        }


#: Limite do corpo de erro logado/exibido (evita logs gigantes)
_ERROR_BODY_LIMIT = 1200


def _sanitize_snippet(text: str | None, limit: int = _ERROR_BODY_LIMIT) -> str | None:
    """Trecho do corpo de resposta CDSE, truncado e sem credenciais."""
    if not text:
        return None
    out = str(text)[:limit]
    # Nunca vazar token/secret caso o provedor ecoe credenciais no corpo.
    out = re.sub(
        r"(?i)(access_token|refresh_token|client_secret|client_id|authorization)\s*[\"']?\s*[:=]\s*[\"']?\S+",
        r"\1=***",
        out,
    )
    return out


#: Cache de token OAuth2 (nunca logado, nunca em disco; guardado em memória)
_token_lock = threading.Lock()
_token_cache: dict = {"token": None, "expires_at": 0.0}

#: Cache em memória (STAC/metadados/raster já processados) — TTL em segundos
_mem_lock = threading.Lock()
_mem_cache: dict[str, tuple[float, object]] = {}


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
def is_configured() -> bool:
    """Só consideramos o CDSE configurado com client_id E client_secret."""
    return (
        settings.cdse_enabled
        and bool(settings.cdse_client_id.strip())
        and bool(settings.cdse_client_secret.strip())
    )


def _cache_ttl() -> float:
    return settings.cdse_cache_hours * 3600.0


def _mem_get(key: str):
    with _mem_lock:
        entry = _mem_cache.get(key)
        if not entry:
            return None
        expires, value = entry
        if time.monotonic() > expires:
            _mem_cache.pop(key, None)
            return None
        return value


def _mem_set(key: str, value) -> None:
    with _mem_lock:
        _mem_cache[key] = (time.monotonic() + _cache_ttl(), value)


# ---------------------------------------------------------------------------
# Autenticação OAuth2 (client_credentials) — nunca vaza segredo
# ---------------------------------------------------------------------------
def _get_token() -> str:
    if not is_configured():
        raise CopernicusNotConfigured("DADOS SATELITAIS REAIS NÃO CONFIGURADOS")
    now = time.monotonic()
    cached = _token_cache["token"]
    if cached and now < _token_cache["expires_at"] - 60:
        return cached

    try:
        resp = requests.post(
            settings.cdse_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.cdse_client_id,
                "client_secret": settings.cdse_client_secret,
            },
            timeout=settings.cdse_timeout_s,
        )
    except requests.RequestException as exc:
        raise CopernicusError(f"Falha de rede ao obter token CDSE ({type(exc).__name__})")

    if resp.status_code == 429:
        raise CopernicusError("CDSE rate limit ao obter token (429)", status=429)
    if resp.status_code in (401, 403):
        raise CopernicusError("CDSE credenciais inválidas ou sem permissão", status=resp.status_code)
    if resp.status_code >= 500:
        raise CopernicusError(f"CDSE erro remoto ao obter token (HTTP {resp.status_code})", status=resp.status_code)
    if resp.status_code != 200:
        raise CopernicusError(f"CDSE auth falhou (HTTP {resp.status_code})", status=resp.status_code)
    try:
        payload = resp.json()
        token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
    except Exception:
        raise CopernicusError("CDSE: resposta de token inválida")

    with _token_lock:
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + float(expires_in)
    logger.info("CDSE: token OAuth2 obtido (%ss de validade)", expires_in)
    return token


def _request(method: str, url: str, *, json_body: dict | None = None,
             headers: dict | None = None, stage: str = "HTTP"):
    """
    POST/GET com tratamento de 429 (1 retry com backoff), 401/403, 5xx etc.

    Em 4xx/5xx/nunca loga Authorization nem tokens: o corpo devolvido pelo
    CDSE é sanitizado/truncado e anexado ao erro (diagnóstico seguro).
    """
    attempts = 2 if settings.cdse_retry_on_429 else 1
    last: requests.Response | None = None
    for attempt in range(1, attempts + 1):
        try:
            last = requests.request(
                method, url, json=json_body, headers=headers or {},
                timeout=settings.cdse_timeout_s,
            )
        except requests.RequestException as exc:
            raise CopernicusError(
                f"Falha de rede CDSE ({type(exc).__name__}) — {stage}",
                endpoint=url, stage=stage,
            )
        snippet = _sanitize_snippet(getattr(last, "text", None) or "")
        ctype = None
        try:
            ctype = last.headers.get("Content-Type")
        except Exception:
            ctype = None

        if last.status_code == 429 and attempt < attempts:
            logger.warning("CDSE: 429 — retry com backoff de %.1fs", settings.cdse_retry_backoff_s)
            time.sleep(settings.cdse_retry_backoff_s)
            continue
        if last.status_code == 429:
            raise CopernicusError(
                f"CDSE rate limit (429) — {stage}", status=429,
                endpoint=url, content_type=ctype, body_snippet=snippet, stage=stage,
            )
        if last.status_code in (401, 403):
            # 401/403: NÃO expõe o corpo do provedor (pode ecoar credenciais).
            raise CopernicusError(
                f"CDSE autenticação negada (HTTP {last.status_code}) — {stage}",
                status=last.status_code, endpoint=url,
                content_type=ctype, stage=stage,
            )
        if last.status_code >= 500:
            raise CopernicusError(
                f"CDSE erro remoto (HTTP {last.status_code}) — {stage}",
                status=last.status_code, endpoint=url,
                content_type=ctype, body_snippet=snippet, stage=stage,
            )
        if last.status_code >= 400:
            raise CopernicusError(
                f"CDSE requisição recusada (HTTP {last.status_code}) — {stage}",
                status=last.status_code, endpoint=url,
                content_type=ctype, body_snippet=snippet, stage=stage,
            )
        return last
    raise CopernicusError("CDSE: sem resposta após retries", endpoint=url, stage=stage)


# ---------------------------------------------------------------------------
# Geometria / AOI
# ---------------------------------------------------------------------------
def kml_to_geojson_polygon(kml_coordinates) -> dict | None:
    """Converte [[lat, lon], ...] (formato do projeto) para GeoJSON Polygon."""
    if not kml_coordinates:
        return None
    try:
        coords = json.loads(kml_coordinates) if isinstance(kml_coordinates, str) else kml_coordinates
        if not coords or len(coords) < 3:
            return None
        ring = [[float(p[1]), float(p[0])] for p in coords]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        return {"type": "Polygon", "coordinates": [ring]}
    except Exception:
        logger.warning("CDSE: KML inválido — usando bbox/centroide.")
        return None


def aoi_bounds(
    lat: float,
    lon: float,
    area_ha: float,
    kml_coordinates=None,
) -> tuple[float, float, float, float]:
    """
    Bounding box do talhão na ORDEM GEOGRÁFICA das APIs CDSE:

        (min_lon, min_lat, max_lon, max_lat)  ==  [west, south, east, north]

    Mesma ordem exigida por STAC (`bbox`) e Process API (`bounds.bbox`).
    Corrige o bug anterior que devolvia (min_lat, max_lat, min_lon, max_lon)
    e era enviado ao STAC invertido (west > east → HTTP 400).
    """
    if kml_coordinates:
        try:
            coords = json.loads(kml_coordinates) if isinstance(kml_coordinates, str) else kml_coordinates
            if coords and len(coords) >= 3:
                lats = [float(p[0]) for p in coords]
                lons = [float(p[1]) for p in coords]
                return min(lons), min(lats), max(lons), max(lats)
        except Exception:
            logger.warning("CDSE: KML inválido nos bounds; usando área.")
    side_m = math.sqrt(max(area_ha, 0.01)) * 100.0
    half_lat = (side_m / 2.0) / 111_320.0
    half_lon = (side_m / 2.0) / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return lon - half_lon, lat - half_lat, lon + half_lon, lat + half_lat


def geometry_digest(lat: float, lon: float, area_ha: float, kml_coordinates) -> str:
    payload = json.dumps(
        {"lat": round(lat, 6), "lon": round(lon, 6), "area": round(area_ha, 2), "kml": kml_coordinates},
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def cdse_dir(farm_id: int, talhao_id: int, acquisition_date: str) -> str:
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(
        base, "dynamic_talhoes",
        f"farm_{farm_id}_talhao_{talhao_id}", "cdse", acquisition_date,
    )


# ---------------------------------------------------------------------------
# STAC — busca e normalização
# ---------------------------------------------------------------------------
@dataclass
class SceneInfo:
    item_id: str
    acquisition_date: str          # YYYY-MM-DD
    datetime: str
    cloud_cover: float | None
    platform: str | None
    constellation: str | None
    collection: str
    geometry: dict | None
    bbox: list[float] | None
    assets: dict


def parse_stac_item(feature: dict) -> SceneInfo:
    props = feature.get("properties") or {}
    # Tolerante a mocks/fixtures: aceita campos em properties (STAC oficial)
    # ou no topo do item (formas alternativas de catalogar).
    raw_dt = props.get("datetime") or feature.get("datetime") or ""
    cloud = props.get("eo:cloud_cover", feature.get("eo:cloud_cover"))
    return SceneInfo(
        item_id=str(feature.get("id", "")),
        acquisition_date=str(raw_dt)[:10],
        datetime=str(raw_dt),
        cloud_cover=round(float(cloud), 2) if isinstance(cloud, (int, float)) and not isinstance(cloud, bool) else None,
        platform=props.get("platform", feature.get("platform")),
        constellation=props.get("constellation", feature.get("constellation")),
        collection=str(feature.get("collection") or COLLECTION),
        geometry=feature.get("geometry"),
        bbox=feature.get("bbox"),
        assets=feature.get("assets") or {},
    )


def stac_search(
    geometry: dict | None,
    bbox: list[float],
    start: date,
    end: date,
    limit: int = 10,
) -> list[SceneInfo]:
    """
    POST https://stac.dataspace.copernicus.eu/v1/search (STAC 1.1.0).

    `bbox` deve estar na ordem geográfica [minLon, minLat, maxLon, maxLat]
    (ver `aoi_bounds`). `geometry`, quando presente, é GeoJSON [lon, lat] —
    o CDSE prioriza `intersects` (interseção real do polígono).
    """
    url = settings.cdse_stac_url.rstrip("/") + "/search"
    body: dict = {
        "collections": [COLLECTION],          # sentinel-2-l2a
        "limit": limit,
        # Intervalo RFC3339 aceito pelo STAC: inicio/fim com offset UTC.
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
    }
    if geometry:
        body["intersects"] = geometry            # prioriza interseção REAL do polígono
    else:
        body["bbox"] = list(bbox)                # [minLon, minLat, maxLon, maxLat]

    headers = {"Content-Type": "application/json"}
    if is_configured():
        headers["Authorization"] = f"Bearer {_get_token()}"
    resp = _request("POST", url, json_body=body, headers=headers, stage="STAC_SEARCH")
    try:
        data = resp.json()
    except Exception:
        raise CopernicusError(
            "CDSE STAC: JSON inválido na resposta",
            endpoint=url, content_type="application/json", stage="STAC_SEARCH",
        )
    features = data.get("features") or []
    return [parse_stac_item(f) for f in features]


def fetch_real_calendar(
    lat: float,
    lon: float,
    area_ha: float,
    kml_coordinates: str | None,
    limit: int = 12,
    lookback_days: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> tuple[list[dict], str, dict | None]:
    """
    Calendário real de passagens Sentinel-2 (STAC) — até `limit` cenas
    úteis (<= max_cloud_cover da config), da mais recente para a mais antiga.

    Janela: `start_date`/`end_date` explícitos (período personalizado) ou
    `lookback_days` a partir de hoje (períodos 30/60/90/180/365). A consulta
    é SOMENTE metadados STAC — nenhum asset é processado aqui.

    Devolve (cenas, status, detail) onde status ∈ {"ok", "not_configured",
    "error", "no_scene"} e `detail` é um dict de diagnóstico SEGURO quando
    status="error" (HTTP, endpoint, etapa, corpo truncado — nunca segredos).
    NUNCA levanta exceção: o chamador decide o fallback.
    """
    if not is_configured():
        return [], "not_configured", None
    end = end_date or date.today()
    if start_date:
        start = start_date
    else:
        start = end - timedelta(days=lookback_days or settings.cdse_lookback_days)
    if start > end:  # tolerância amigável: inverte, nunca falha silenciosamente
        start, end = end, start
    try:
        bounds = aoi_bounds(lat, lon, area_ha, kml_coordinates)
        geometry = kml_to_geojson_polygon(kml_coordinates)
        scenes = stac_search(geometry, bounds, start, end, limit=max(limit, 20))
        usable = [s for s in scenes if s.cloud_cover is not None and s.cloud_cover <= settings.cdse_max_cloud_cover]
        usable.sort(key=lambda s: (s.datetime or "", s.item_id or ""), reverse=True)
        calendar = [
            {
                "date": s.acquisition_date,
                "datetime": s.datetime,
                "cloud_cover": s.cloud_cover,
                "product_id": s.item_id,
                "collection": s.collection,
            }
            for s in usable[:limit]
        ]
        # Log diagnóstico SEM segredos: fonte, contagem e última aquisição.
        if calendar:
            logger.info(
                "[3D-DATES] source=%s stac_scenes=%d usable=%d count=%d latest=%s window=%s..%s",
                CALENDAR_REAL_SOURCE, len(scenes), len(usable), len(calendar),
                calendar[0]["date"], start.isoformat(), end.isoformat(),
            )
        else:
            logger.warning(
                "[3D-DATES] source=%s reason=%s stac_scenes=%d window=%s..%s",
                CALENDAR_FALLBACK_SOURCE, "no_scene", len(scenes),
                start.isoformat(), end.isoformat(),
            )
        return calendar, ("ok" if calendar else "no_scene"), None
    except (CopernicusNotConfigured, CopernicusError) as exc:
        stage = getattr(exc, "stage", None) or type(exc).__name__
        logger.warning(
            "[3D-DATES] source=%s reason=%s stage=%s",
            CALENDAR_FALLBACK_SOURCE, "error", stage,
        )
        return [], "error", exc.to_detail() if isinstance(exc, CopernicusError) else None
        return [], "error", exc.to_detail() if isinstance(exc, CopernicusError) else None


def select_best_scene(
    items: list[SceneInfo],
    max_cloud: float | None = None,
    target_date: str | None = None,
) -> tuple[SceneInfo | None, str, float | None]:
    """
    Regra explícita de seleção:
    1. ordena por data/hora (mais recente primeiro);
    2. aceita cenas com cobertura <= limite (config);
    3. se nenhuma: relaxa gradualmente (+15, +35 p.p., teto 100);
    4. cenas sem `eo:cloud_cover` só entram no estágio final (cloud_unknown).
    Devolve (scene, selection_reason, cloud_limit_used).
    """
    if not items:
        return None, "no_scene", None

    ordered = sorted(
        items,
        key=lambda s: (s.datetime or "", s.item_id or ""),
        reverse=True,
    )
    base = settings.cdse_max_cloud_cover if max_cloud is None else float(max_cloud)
    limits = [base, min(100.0, base + 15.0), min(100.0, base + 35.0), 100.0]

    # Passo 0: data-alvo exata (quando pedida) tem prioridade.
    if target_date:
        for scene in ordered:
            if scene.acquisition_date == target_date:
                return scene, "exact_date_match", base

    for i, limit in enumerate(limits):
        for scene in ordered:
            if scene.cloud_cover is None:
                continue
            if scene.cloud_cover <= limit:
                reason = "most_recent_within_limit" if i == 0 else "cloud_limit_relaxed"
                return scene, reason, limit

    # Último recurso: cena sem metadado de nuvem (comunicar explicitamente).
    for scene in ordered:
        if scene.cloud_cover is None:
            return scene, "cloud_unknown", 100.0
    return None, "no_scene", None


# ---------------------------------------------------------------------------
# Process API — bandas reais da área da fazenda (sem produto inteiro)
# ---------------------------------------------------------------------------
def build_bands_evalscript() -> str:
    """Evalscript VERSION=3: bandas + SCL + dataMask em 1 request (8 bandas)."""
    return """//VERSION=3
function setup() {
  return {
    input: ["B02", "B03", "B04", "B05", "B08", "B11", "SCL", "dataMask"],
    output: { id: "default", bands: 8, sampleType: SampleType.FLOAT32 }
  };
}
function evaluatePixel(sample) {
  return [
    sample.B02, sample.B03, sample.B04, sample.B05, sample.B08, sample.B11,
    sample.SCL, sample.dataMask
  ];
}"""


def build_dem_evalscript() -> str:
    """Evalscript VERSION=3 para DEM em metros (INT16)."""
    return """//VERSION=3
function setup() {
  return {
    input: ["DEM"],
    output: { id: "default", bands: 1, sampleType: SampleType.INT16 }
  };
}
function evaluatePixel(sample) {
  return [sample.DEM];
}"""


def _process_request(
    bounds: tuple[float, float, float, float],
    evalscript: str,
    size: int,
    data_type: str = "sentinel-2-l2a",
    time_range: tuple[str, str] | None = None,
    max_cloud: float | None = None,
    dem_instance: str | None = None,
    mosaicking_order: str | None = None,
) -> bytes:
    # bounds na ordem geográfica [west, south, east, north] (aoi_bounds)
    west, south, east, north = bounds
    data_filter: dict = {}
    if time_range:
        data_filter["timeRange"] = {
            "from": f"{time_range[0]}T00:00:00Z",
            "to": f"{time_range[1]}T23:59:59Z",
        }
    if max_cloud is not None:
        data_filter["maxCloudCoverage"] = float(max_cloud)
    if mosaicking_order:
        # determinismo quando há mais de uma cena na janela (ex.: 2 órbitas
        # no mesmo dia) — "leastCC" = menor cobertura de nuvens por tile.
        data_filter["mosaickingOrder"] = mosaicking_order

    data_entry: dict = {"type": data_type, "dataFilter": data_filter}
    if dem_instance:
        # DEM: `demInstance` fica no `input.data[0]` (não em dataFilter) —
        # confirmado em documentation.dataspace.copernicus.eu/APIs/SentinelHub/Data/DEM.html
        data_entry["demInstance"] = dem_instance

    body = {
        "input": {
            "bounds": {
                "bbox": [west, south, east, north],  # [minLon, minLat, maxLon, maxLat]
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
            },
            "data": [data_entry],
        },
        "output": {
            "width": int(size),
            "height": int(size),
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": evalscript,
    }
    headers = {"Content-Type": "application/json"}
    if is_configured():
        headers["Authorization"] = f"Bearer {_get_token()}"
    resp = _request("POST", settings.cdse_process_url, json_body=body,
                    headers=headers, stage="PROCESS_API")
    if not resp.content or len(resp.content) < 100:
        raise CopernicusError(
            "CDSE Process: resposta de imagem vazia/inválida",
            endpoint=settings.cdse_process_url, stage="PROCESS_API",
        )
    return resp.content


# ---------------------------------------------------------------------------
# Bandas → máscaras → índices → PNG + estatísticas (funções puras, testáveis)
# ---------------------------------------------------------------------------
def ndvi(nir, red):
    return (nir - red) / (nir + red + _EPS)


def ndre(nir, red_edge):
    return (nir - red_edge) / (nir + red_edge + _EPS)


def ndmi(nir, swir):
    return (nir - swir) / (nir + swir + _EPS)


def evi(nir, red, blue):
    # EVI padrão: G=2.5, C1=6, C2=7.5, L=1 (reflectância 0–1)
    return 2.5 * (nir - red) / (nir + 6.0 * red - 7.5 * blue + 1.0 + _EPS)


def index_array(layer: str, bands: np.ndarray) -> np.ndarray:
    """bands: (8, H, W) na ordem B02,B03,B04,B05,B08,B11,SCL,dataMask."""
    b02, b03, b04, b05, b08, b11 = bands[0], bands[1], bands[2], bands[3], bands[4], bands[5]
    if layer == "ndvi":
        return ndvi(b08, b04)
    if layer == "ndre":
        return ndre(b08, b05)
    if layer == "ndmi":
        return ndmi(b08, b11)
    if layer == "evi":
        return evi(b08, b04, b02)
    raise ValueError(f"Camada sem índice: {layer}")


def build_valid_mask(bands: np.ndarray) -> np.ndarray:
    """Máscara de qualidade: dataMask e classes SCL válidas (sem nuvem/sombra)."""
    scl = bands[6].astype("float32")
    data_mask = bands[7]
    valid = np.isfinite(scl) & np.isfinite(data_mask) & (data_mask > 0)
    valid &= ~np.isin(scl.astype("int32"), SCL_INVALID_CLASSES)
    return valid


def polygon_mask(size: int, bounds, kml_coordinates) -> np.ndarray:
    """
    Máscara do polígono do talhão no raster (mesma projeção do rasterizador
    procedural: margem 12%, drawable 76% → contorno 3D alinhado).

    `bounds` segue a ordem geográfica (min_lon, min_lat, max_lon, max_lat).
    """
    min_lon, min_lat, max_lon, max_lat = bounds
    mask_img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask_img)
    margin = 0.12 * size
    drawable = size - (2 * margin)
    if kml_coordinates:
        try:
            coords = json.loads(kml_coordinates) if isinstance(kml_coordinates, str) else kml_coordinates
            if coords and len(coords) >= 3:
                lats = [float(p[0]) for p in coords]
                lons = [float(p[1]) for p in coords]
                span_lat = max(max(lats) - min(lats), 1e-6)
                span_lon = max(max(lons) - min(lons), 1e-6)
                pts = []
                for lat, lon in zip(lats, lons):
                    px = margin + ((lon - min_lon) / span_lon) * drawable
                    py = margin + ((max_lat - lat) / span_lat) * drawable
                    pts.append((px, py))
                draw.polygon(pts, fill=255)
                return np.array(mask_img) > 128
        except Exception:
            logger.warning("CDSE: KML inválido na máscara; usando elipse.")
    span_lat = max(max_lat - min_lat, 1e-6)
    span_lon = max(max_lon - min_lon, 1e-6)
    # elipse equivalente (bbox 15%–85%) — igual ao fallback procedural
    cx, cy = size / 2.0, size / 2.0
    rx = drawable / 2.0
    ry = drawable / 2.0
    draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    return np.array(mask_img) > 128


#: Limites de zona por camada — MESMOS thresholds da paleta procedural
ZONE_THRESHOLDS = {
    "ndvi": (0.35, 0.55, 0.75),
    "evi": (0.30, 0.60, 0.95),
    "ndre": (0.35, 0.65, 0.95),
    "ndmi": (0.35, 0.65, 0.95),
}


def _zone_stats(values: np.ndarray, layer: str) -> dict:
    t1, t2, t3 = ZONE_THRESHOLDS.get(layer, ZONE_THRESHOLDS["ndvi"])
    n = values.size
    if n == 0:
        return {"stress": 0.0, "medium": 0.0, "good": 0.0, "dense": 0.0}
    stress = float(np.mean(values < t1)) * 100.0
    medium = float(np.mean((values >= t1) & (values < t2))) * 100.0
    good = float(np.mean((values >= t2) & (values < t3))) * 100.0
    dense = 100.0 - stress - medium - good
    return {
        "stress": round(stress, 1),
        "medium": round(medium, 1),
        "good": round(good, 1),
        "dense": round(max(0.0, dense), 1),
    }


def render_layer_png(
    layer: str,
    bands: np.ndarray,
    valid: np.ndarray,
    poly: np.ndarray,
) -> tuple[bytes, dict]:
    """Gera PNG (RGBA 256) + estatísticas reais para a camada pedida."""
    from services.satellite_service import get_spectral_palette  # paleta comum

    height, width = valid.shape
    size = max(height, width)

    if layer == "rgb":
        b02, b03, b04 = bands[0], bands[1], bands[2]
        # True color: reflectância ×2.5 (visualização padrão CDSE), clamp 0–1
        img = np.stack(
            [np.clip(b04 * 2.5, 0, 1), np.clip(b03 * 2.5, 0, 1), np.clip(b02 * 2.5, 0, 1)],
            axis=-1,
        )
        stats = {"mean_index": None, "min": None, "max": None}
    else:
        idx = index_array(layer, bands).astype("float32")
        values = idx[valid & poly]
        if values.size == 0:
            raise CopernicusError("CDSE: nenhum pixel válido no recorte do talhão")
        lo, hi = float(np.percentile(values, 2)), float(np.percentile(values, 98))
        span = max(hi - lo, 1e-6)
        norm = np.clip((idx - lo) / span, 0.0, 1.0)

        color_map = np.zeros((height, width, 3), dtype="uint8")
        inside = poly
        valid_inside = inside & valid
        color_map[inside] = [80, 80, 90]  # pixels dentro do talhão sem dado (nuvem)
        # colormap paleta (mesma classe visual do fallback procedural)
        v = norm[valid_inside]
        if v.size:
            color_map[valid_inside] = np.array(
                [get_spectral_palette(layer, float(x))[:3] for x in v],
                dtype="uint8",
            ).reshape(-1, 3)
        img = color_map.astype("float32") / 255.0

        stats = {
            "mean_index": round(float(np.mean(values)), 4),
            "min": round(float(np.min(values)), 4),
            "max": round(float(np.max(values)), 4),
            "p2": round(lo, 4),
            "p98": round(hi, 4),
            "zones": _zone_stats(values, layer),
        }

    # Alpha: talhão (com ou sem dado) visível; fora do talhão → fundo padrão
    rgba = np.zeros((height, width, 4), dtype="uint8")
    rgba[..., :3] = (img * 255.0).astype("uint8")
    rgba[..., 3] = 255
    rgba[~poly] = [13, 17, 23, 255]

    out = Image.fromarray(rgba, "RGBA").resize((size, size), Image.BILINEAR)
    buf = io.BytesIO()
    out.save(buf, format="PNG")

    valid_total = int(np.sum(valid & poly))
    poly_total = max(int(np.sum(poly)), 1)
    stats["valid_pixels"] = valid_total
    stats["polygon_pixels"] = poly_total
    stats["valid_pixel_percentage"] = round(100.0 * valid_total / poly_total, 1)
    return buf.getvalue(), stats


def read_band_raster(content: bytes, size: int) -> np.ndarray:
    """image/tiff do Process API → (8, size, size)."""
    try:
        with rasterio.open(io.BytesIO(content)) as src:
            arr = src.read()
    except Exception as exc:
        raise CopernicusError(f"CDSE: TIFF inválido ({type(exc).__name__})")
    if arr.ndim != 3 or arr.shape[0] != 8:
        raise CopernicusError(f"CDSE: raster inesperado (shape {arr.shape})")
    if arr.shape[1:] != (size, size):
        resized = np.stack(
            [np.asarray(Image.fromarray(arr[i]).resize((size, size), Image.BILINEAR)) for i in range(arr.shape[0])]
        )
        arr = resized
    return arr.astype("float32")


# ---------------------------------------------------------------------------
# Pipeline de alto nível — camada do talhão (com cache e proveniência)
# ---------------------------------------------------------------------------
def _result_not_configured(layer: str, requested_date: str | None) -> dict:
    return {
        "data_origin": "procedural",
        "real_data_status": "not_configured",
        "real_data_message": "DADOS SATELITAIS REAIS NÃO CONFIGURADOS",
        "real_data_error": None,
        "layer": layer,
        "date": requested_date,
    }


def _result_no_scene(layer: str, requested_date: str | None) -> dict:
    return {
        "data_origin": "procedural",
        "real_data_status": "no_scene",
        "real_data_message": "NENHUMA CENA SENTINEL-2 DISPONÍVEL NA JANELA",
        "real_data_error": None,
        "layer": layer,
        "date": requested_date,
    }


def _result_error(layer: str, requested_date: str | None, exc: Exception) -> dict:
    detail = exc.to_detail() if isinstance(exc, CopernicusError) else None
    logger.warning(
        "CDSE: falha ao obter dado real — etapa=%s status=%s endpoint=%s motivo=%s corpo=%s",
        getattr(exc, "stage", None), getattr(exc, "status", None),
        getattr(exc, "endpoint", None), exc, getattr(exc, "body_snippet", None),
    )
    return {
        "data_origin": "procedural",
        "real_data_status": "error",
        "real_data_message": f"DADOS REAIS INDISPONÍVEIS ({type(exc).__name__})",
        "real_data_error": detail,
        "layer": layer,
        "date": requested_date,
    }


def process_farm_layer(
    farm_id: int,
    talhao_id: int,
    lat: float,
    lon: float,
    area_ha: float,
    kml_coordinates: str | None,
    layer: str,
    date_str: str | None = None,
    size: int | None = None,
) -> dict:
    """
    Busca/gera a textura REAL (ou um status explícito de indisponibilidade).

    Retorna dict com `data_origin="sentinel"` + proveniência quando há dado
    real; caso contrário, dict com `data_origin="procedural"` e
    `real_data_status` (not_configured | no_scene | error) para o frontend
    comunicar com clareza.
    """
    if layer not in SUPPORTED_LAYERS:
        raise ValueError(f"Camada não suportada pelo CDSE: {layer}")
    if not is_configured():
        return _result_not_configured(layer, date_str)

    size = int(size or settings.cdse_raster_size)
    digest = geometry_digest(lat, lon, area_ha, kml_coordinates)
    cache_key = f"layer:{digest}:{date_str or 'auto'}:{layer}:{size}"
    cached = _mem_get(cache_key)
    if cached:
        return cached

    bounds = aoi_bounds(lat, lon, area_ha, kml_coordinates)
    geometry = kml_to_geojson_polygon(kml_coordinates)

    # Janela temporal: até `lookback` dias antes da data pedida (ou hoje)
    end = date.today()
    if date_str:
        try:
            end = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            end = date.today()
    start = end - timedelta(days=settings.cdse_lookback_days)

    try:
        stac_key = f"stac:{digest}:{start.isoformat()}:{end.isoformat()}"
        scenes = _mem_get(stac_key)
        if scenes is None:
            scenes = stac_search(geometry, bounds, start, end, limit=12)
            _mem_set(stac_key, scenes)
        scene, reason, used_limit = select_best_scene(scenes, target_date=date_str)
    except (CopernicusNotConfigured, CopernicusError) as exc:
        return _result_error(layer, date_str, exc)

    if scene is None:
        return _result_no_scene(layer, date_str)

    acq = scene.acquisition_date
    folder = cdse_dir(farm_id, talhao_id, acq)
    os.makedirs(folder, exist_ok=True)
    png_path = os.path.join(folder, f"{layer}.png")
    stats_path = os.path.join(folder, f"{layer}_stats.json")
    prov_path = os.path.join(folder, "provenance.json")

    # Cache em disco: reutiliza o raster já processado da MESMA data+camada.
    if os.path.exists(png_path):
        stats = json.loads(open(stats_path).read()) if os.path.exists(stats_path) else {}
        provenance = json.loads(open(prov_path).read()) if os.path.exists(prov_path) else {}
        return _layer_result(farm_id=farm_id, layer=layer, acq=acq, scene=scene,
                             selection_reason=reason, stats=stats, provenance=provenance,
                             cache_key=cache_key)

    try:
        # Raster só da área do talhão (uma requisição, 8 bandas)
        process_key = f"process:{digest}:{acq}:{layer}:{size}"
        content = _mem_get(process_key)
        if content is None:
            content = _process_request(
                bounds=bounds,
                evalscript=build_bands_evalscript(),
                size=size,
                time_range=(acq, acq),
                max_cloud=used_limit,
                # mosaico determinístico: menor cobertura de nuvens por tile
                # (comportamento documentado para sentinel-2 no Process API)
                mosaicking_order="leastCC",
            )
            _mem_set(process_key, content)
        bands = read_band_raster(content, size)
        valid = build_valid_mask(bands)
        poly = polygon_mask(size, bounds, kml_coordinates)
        png_bytes, stats = render_layer_png(layer, bands, valid, poly)
        with open(png_path, "wb") as fh:
            fh.write(png_bytes)
        with open(stats_path, "w", encoding="utf-8") as fh:
            json.dump(stats, fh)
        provenance = {
            "provider": PROVIDER,
            "collection": scene.collection or COLLECTION,
            "product_id": scene.item_id,
            "acquisition_date": acq,
            "datetime": scene.datetime,
            "cloud_cover": scene.cloud_cover,
            "processing_level": PROCESSING_LEVEL,
            "bands": BANDS,
            "selection_reason": reason,
            "cloud_limit_used": used_limit,
            "platform": scene.platform,
            "constellation": scene.constellation,
            "scl_invalid_classes": list(SCL_INVALID_CLASSES),
            "index_formulas": {
                "ndvi": "(B08 - B04) / (B08 + B04)",
                "ndre": "(B08 - B05) / (B08 + B05)",
                "ndmi": "(B08 - B11) / (B08 + B11)",
                "evi": "2.5*(B08 - B04) / (B08 + 6*B04 - 7.5*B02 + 1)",
            },
        }
        with open(prov_path, "w", encoding="utf-8") as fh:
            json.dump(provenance, fh, ensure_ascii=False)
    except (CopernicusError, rasterio.errors.RasterioError) as exc:
        return _result_error(layer, date_str, exc)

    result = _layer_result(farm_id=farm_id, layer=layer, acq=acq, scene=scene,
                           selection_reason=reason, stats=stats, provenance=provenance,
                           cache_key=cache_key)
    return result


def _layer_result(farm_id, layer, acq, scene, selection_reason, stats, provenance, cache_key):
    result = {
        **provenance,
        "type": "dynamic",
        "data_origin": "sentinel",
        "real_data_status": "ok",
        "real_data_message": None,
        "layer": layer,
        "date": acq,
        "selection_reason": selection_reason,
        "valid_pixel_percentage": stats.get("valid_pixel_percentage"),
        "stats": stats,
        "texture_url": (
            f"{settings.public_base_url}/api/talhao/{farm_id}/texture.png?layer={layer}&date={acq}"
        ),
    }
    _mem_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# DEM real via CDSE (COPERNICUS_30 → COPERNICUS_90)
# ---------------------------------------------------------------------------
def fetch_dem_png(
    bounds: tuple[float, float, float, float],
    size: int,
    instance: str = "COPERNICUS_30",
) -> dict | None:
    """Dados DEM (metros) via Process API; normaliza 0–255 como heightmap."""
    if not is_configured():
        raise CopernicusNotConfigured("DEM real do CDSE não configurado")
    source = "copernicus_30" if instance == "COPERNICUS_30" else "copernicus_90"
    try:
        content = _process_request(
            bounds=bounds,
            evalscript=build_dem_evalscript(),
            size=int(size),
            data_type="dem",
            time_range=None,
            max_cloud=None,
            dem_instance=instance,
        )
        with rasterio.open(io.BytesIO(content)) as src:
            arr = src.read(1).astype("float32")
        valid = np.isfinite(arr) & (arr > -9000.0)
        if valid.sum() == 0:
            raise CopernicusError(f"DEM {instance}: sem elevações válidas")
        lo, hi = float(arr[valid].min()), float(arr[valid].max())
        if hi - lo < 0.05:
            raise CopernicusError(f"DEM {instance}: alívio insuficiente")
        norm = np.clip((arr - lo) / (hi - lo + 1e-9), 0.0, 1.0)
        norm[~valid] = 0.0
        out = (norm * 255).astype("uint8")
        img = Image.fromarray(out, mode="L").resize((size, size), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {
            "png_bytes": buf.getvalue(),
            "min_elevation_m": round(lo, 2),
            "max_elevation_m": round(hi, 2),
            "source": source,
            "instance": instance,
            "provider": PROVIDER,
        }
    except (CopernicusNotConfigured, CopernicusError) as exc:
        logger.warning("CDSE DEM %s indisponível (%s)", instance, exc)
        return None
    except Exception as exc:  # TIFF inválido etc.
        logger.warning("CDSE DEM %s: erro inesperado (%s)", instance, type(exc).__name__)
        return None


def load_cached_real_stats(farm_id: int, talhao_id: int, date_str: str, layer: str) -> dict | None:
    """Lê estatísticas REAIS cacheadas (disco) p/ analytics/3D — sem rede."""
    path = os.path.join(cdse_dir(farm_id, talhao_id, date_str), f"{layer}_stats.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        return None
    return None


def load_cached_provenance(farm_id: int, talhao_id: int, date_str: str) -> dict | None:
    """Proveniência REAL cacheadada em disco (sem chamada de rede)."""
    path = os.path.join(cdse_dir(farm_id, talhao_id, date_str), "provenance.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
