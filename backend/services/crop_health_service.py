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
#: Janela padrão do HISTÓRICO exibido na timeline/gráfico (não é a janela da
#: tendência — ver ``TREND_WINDOW_DAYS``).
DEFAULT_PERIOD_DAYS = 730
DEFAULT_LIMIT = 60

# ---------------------------------------------------------------------------
# Tendência temporal — janela, mínimos e limiares (PR #8, revisão da auditoria)
# ---------------------------------------------------------------------------
#: Janela OPERACIONAL da tendência, ancorada na última cena aceita. 120 dias
#: cobrem aproximadamente um ciclo de soja/milho safrinha (110–130 dias) e,
#: com a revisita de ~5 dias do par Sentinel-2, comportam até ~24 passagens
#: potenciais — o suficiente para uma regressão robusta. Chamar de "recente"
#: uma série de 730 dias era semanticamente errado; o histórico continua
#: disponível na timeline e na comparação A/B.
TREND_WINDOW_DAYS = 120
#: Extensão máxima admitida quando a janela de 120 dias não reúne o mínimo de
#: cenas aceitas (nuvem persistente). Acima disso a tendência é declarada
#: insuficiente em vez de misturar dois ciclos agrícolas.
TREND_WINDOW_MAX_DAYS = 240
#: Mínimo de cenas aceitas para qualquer classificação de tendência.
MIN_TREND_SCENES = 3
#: Intervalo temporal mínimo entre a primeira e a última cena da janela.
#: Abaixo disso duas passagens quase simultâneas produziriam "tendência".
MIN_TREND_INTERVAL_DAYS = 14
#: Concordância direcional mínima (tau de Kendall) para aceitar direção.
#: tau = S / C(n,2): com n=3, tau=1/3 corresponde a 2 de 3 pares concordantes,
#: ou seja, UMA oscilação isolada ainda preserva a tendência; com n≥12 exige
#: maioria clara de pares concordantes. 0,30 fica logo abaixo de 1/3 para não
#: descartar o caso n=3 por arredondamento.
TREND_TAU_MIN = 0.30
#: Alvo e tolerância do delta de ~30 dias (revisita real não cai em 30 exatos).
DELTA_30D_TARGET_DAYS = 30
DELTA_30D_TOLERANCE_DAYS = 15
PERSISTENCE_SCENES = 3

TREND_LABELS = {
    "melhoria": "Melhoria",
    "queda": "Queda",
    "estavel": "Estável",
    "sem_tendencia": "Sem tendência definida",
    "dados_insuficientes": "Dados insuficientes",
}

#: Critério textual da tendência — exibido na UI e replicado na documentação.
TREND_CRITERIA = (
    "Inclinação de Theil–Sen sobre as DATAS REAIS das cenas aceitas na janela; "
    f"direção confirmada por tau de Kendall (|tau| ≥ {TREND_TAU_MIN:.2f}) e por "
    "magnitude estimada no período (inclinação × dias da janela) maior ou igual "
    "ao piso do índice. Série sem magnitude relevante e com dispersão baixa é "
    "classificada como estável; magnitude relevante sem direção concordante, ou "
    "dispersão alta, é classificada como sem tendência definida. A direção só é "
    "aceita quando a mudança estimada é pelo menos tão grande quanto a dispersão "
    "residual em torno da reta (razão sinal/ruído ≥ 1)."
)

#: Critério textual da qualidade AGREGADA da série (distinta da cena).
SERIES_QUALITY_CRITERIA = (
    "Série: proporção de cenas úteis (qualidade alta ou média) sobre o total de "
    "aquisições do período. Alta: ≥80% úteis e ≥50% de qualidade alta; "
    "Média: ≥60% úteis; Limitada: ≥34% úteis e pelo menos 3 cenas úteis; "
    "Insuficiente: abaixo disso. Uma única cena ruim não rebaixa a série."
)

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
            "latest_scene": None,
            "latest_valid_scene": None,
            "current_is_valid_for_analysis": False,
            "current_scene_note": "Nenhuma aquisição Sentinel-2 disponível para o período.",
            "previous": None,
            "delta_previous": None,
            "delta_previous_detail": None,
            "delta_30d": None,
            "delta_30d_detail": None,
            "trend": "dados_insuficientes",
            "trend_label": TREND_LABELS["dados_insuficientes"],
            "trend_detail": {
                "code": "dados_insuficientes",
                "label": TREND_LABELS["dados_insuficientes"],
                "text": "Não há dados suficientes para avaliar tendência.",
                "reason": "nenhuma cena Sentinel-2 válida no período",
                "scenes_used": 0,
                "window": None,
                "criteria": TREND_CRITERIA,
            },
            "stability": None,
            "volatility": None,
            "volatility_scope": "janela da tendência",
            "persistence": {"status": "dados_insuficientes", "window": None},
            "internal_anomaly": {"status": "dados_insuficientes"},
        },
        "interpretation": {
            "trend": "Não há dados suficientes para avaliar tendência.",
            "attention": [],
            "scientific_note": "Índice espectral não é diagnóstico agronômico; os dados não permitem determinar a causa de uma eventual mudança.",
        },
        "quality": {
            "scope": "serie", "level": "insuficiente", "label": "Insuficiente",
            "scenes": 0, "usable_scenes": 0, "valid_scenes": 0,
            "usable_ratio_pct": 0.0, "high_ratio_pct": 0.0,
            "distribution": {"alta": 0, "media": 0, "limitada": 0, "insuficiente": 0},
            "criteria": SERIES_QUALITY_CRITERIA,
        },
        "scene_quality": None,
        "confidence": {
            "scope": "serie", "level": "insuficiente", "label": "Insuficiente",
            "alias_of": "quality",
            "note": "Campo mantido por compatibilidade: repete a qualidade agregada da série.",
            "reason": "sem cenas Sentinel-2 válidas",
        },
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
        "quality_scope": "cena",
        "accepted_for_trend": quality["level"] in ACCEPTABLE_QUALITIES,
        "source": "sentinel-cdse",
        "source_data": _scene_source(scene, stats, quality),
        # Deltas explicitamente referenciados (a UI nunca mostra "variação"
        # sem dizer em relação a quê e entre quais datas).
        "delta_previous": None,
        "delta_previous_from": None,
        "delta_previous_days": None,
        "delta_30d": None,
        "delta_30d_from": None,
        "delta_30d_days": None,
    }
    if error:
        item["status"] = "sem_dados"
        item["error"] = error
    else:
        item["status"] = "ok" if item["mean"] is not None else "sem_dados"
    return item


def _enrich_deltas(timeline: list[dict]) -> None:
    """Calcula os dois deltas automáticos, cada um com a sua referência.

    - ``delta_previous``: cena atual − cena ANTERIOR COM DADO (a cena anterior
      pode ter falhado; comparar contra ela produziria ``None`` silencioso);
    - ``delta_30d``: cena atual − cena mais próxima de ``DELTA_30D_TARGET_DAYS``
      dias antes, dentro de ``DELTA_30D_TOLERANCE_DAYS``.

    As datas de referência e o intervalo real em dias ficam gravados no item
    para que a interface nunca exiba uma variação sem dizer de onde ela vem.
    """
    ordered = sorted(timeline, key=lambda x: x["date"])
    for i, item in enumerate(ordered):
        if item.get("mean") is None:
            continue
        current_date = _safe_date(item["date"])
        previous = next((x for x in reversed(ordered[:i]) if x.get("mean") is not None), None)
        if previous is not None:
            item["delta_previous"] = _round_or_none(item["mean"] - previous["mean"])
            item["delta_previous_from"] = previous["date"]
            item["delta_previous_days"] = (current_date - _safe_date(previous["date"])).days
        target = current_date - timedelta(days=DELTA_30D_TARGET_DAYS)
        candidates = [
            x for x in ordered[:i]
            if x.get("mean") is not None
            and abs((_safe_date(x["date"]) - target).days) <= DELTA_30D_TOLERANCE_DAYS
        ]
        if candidates:
            base = min(candidates, key=lambda x: abs((_safe_date(x["date"]) - target).days))
            item["delta_30d"] = _round_or_none(item["mean"] - base["mean"])
            item["delta_30d_from"] = base["date"]
            item["delta_30d_days"] = (current_date - _safe_date(base["date"])).days


def _accepted_points(timeline: list[dict]) -> list[dict]:
    return [x for x in timeline if x.get("mean") is not None and x.get("quality") in ACCEPTABLE_QUALITIES]


def _theil_sen_slope(x: np.ndarray, y: np.ndarray) -> float | None:
    """Inclinação de Theil–Sen: mediana das inclinações par a par.

    Estimador robusto — uma cena discrepante altera no máximo uma fração das
    inclinações par a par e praticamente não desloca a mediana, ao contrário
    dos mínimos quadrados. Usa as DATAS REAIS (``x`` em dias), portanto
    intervalos irregulares entre passagens não distorcem o resultado.
    """
    n = int(x.size)
    if n < 2:
        return None
    slopes = []
    for i in range(n - 1):
        dx = x[i + 1:] - x[i]
        valid = dx > 0
        if np.any(valid):
            slopes.append((y[i + 1:][valid] - y[i]) / dx[valid])
    if not slopes:
        return None
    return float(np.median(np.concatenate(slopes)))


def _mann_kendall(x: np.ndarray, y: np.ndarray) -> dict:
    """Estatística S de Mann–Kendall, tau de Kendall e aproximação normal.

    ``S`` soma o sinal de cada par ordenado no tempo; ``tau = S / C(n,2)`` é a
    concordância direcional (1 = monotônica crescente, -1 = decrescente).
    A aproximação normal (com correção de empates) devolve ``z``/``p`` como
    INFORMAÇÃO: com n=3 o teste não tem poder algum, por isso a decisão usa
    tau + magnitude e o p-valor é apenas reportado.
    """
    n = int(x.size)
    pairs = n * (n - 1) // 2
    if pairs == 0:
        return {"s": 0, "pairs": 0, "tau": None, "z": None, "p_value": None}
    s_stat = 0
    for i in range(n - 1):
        dt = np.sign(x[i + 1:] - x[i])
        dv = np.sign(y[i + 1:] - y[i])
        s_stat += int(np.sum(dt * dv))
    tau = s_stat / pairs
    # Variância com correção de empates nos valores do índice.
    _, counts = np.unique(y, return_counts=True)
    ties = float(np.sum(counts * (counts - 1) * (2 * counts + 5)))
    variance = (n * (n - 1) * (2 * n + 5) - ties) / 18.0
    if variance <= 0:
        return {"s": s_stat, "pairs": pairs, "tau": round(tau, 4), "z": None, "p_value": None}
    if s_stat > 0:
        z = (s_stat - 1) / math.sqrt(variance)
    elif s_stat < 0:
        z = (s_stat + 1) / math.sqrt(variance)
    else:
        z = 0.0
    p_value = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0))))
    return {"s": s_stat, "pairs": pairs, "tau": round(tau, 4),
            "z": round(z, 4), "p_value": round(p_value, 4)}


def select_trend_window(
    points: list[dict],
    window_days: int = TREND_WINDOW_DAYS,
    max_window_days: int = TREND_WINDOW_MAX_DAYS,
) -> tuple[list[dict], dict]:
    """Recorta a janela OPERACIONAL da tendência a partir da última cena aceita.

    A timeline mostra o histórico completo pedido; a tendência responde a
    "o que está acontecendo agora" e por isso olha apenas os últimos
    ``window_days``. Quando a nuvem impede reunir o mínimo de cenas nessa
    janela, ela é estendida uma única vez até ``max_window_days`` e o modo é
    declarado explicitamente na resposta.
    """
    if not points:
        return [], {"days": window_days, "mode": "sem_cenas", "requested_days": window_days}
    anchor = _safe_date(points[-1]["date"])

    def slice_from(days: int) -> list[dict]:
        floor_date = anchor - timedelta(days=days - 1)
        return [p for p in points if _safe_date(p["date"]) >= floor_date]

    selected = slice_from(window_days)
    mode = "janela_recente"
    if len(selected) < MIN_TREND_SCENES or _span_days(selected) < MIN_TREND_INTERVAL_DAYS:
        extended = slice_from(max_window_days)
        if len(extended) > len(selected):
            selected, mode = extended, "janela_estendida"
    used_days = window_days if mode == "janela_recente" else max_window_days
    window = {
        "requested_days": window_days,
        "days": used_days,
        "mode": mode,
        "mode_label": ("Últimos %d dias" % used_days) if mode == "janela_recente"
        else ("Janela estendida para %d dias (cobertura insuficiente em %d dias)" % (used_days, window_days)),
        "anchor_date": anchor.isoformat(),
        "start": (anchor - timedelta(days=used_days - 1)).isoformat(),
        "end": anchor.isoformat(),
    }
    if selected:
        window["first_observation"] = selected[0]["date"]
        window["last_observation"] = selected[-1]["date"]
        window["observed_span_days"] = _span_days(selected)
    return selected, window


def _span_days(points: list[dict]) -> int:
    if len(points) < 2:
        return 0
    return (_safe_date(points[-1]["date"]) - _safe_date(points[0]["date"])).days


def classify_trend(
    timeline: list[dict],
    index: str = "ndvi",
    window_days: int = TREND_WINDOW_DAYS,
) -> dict:
    """Classifica a tendência da janela operacional usando as datas reais.

    Algoritmo (substitui a regra de monotonicidade perfeita da versão
    anterior, que transformava qualquer oscilação isolada em "Estável"):

    1. só entram cenas com média válida e qualidade alta/média;
    2. a janela é recortada por ``select_trend_window`` (últimos
       ``TREND_WINDOW_DAYS`` dias, estendida até ``TREND_WINDOW_MAX_DAYS``
       quando necessário);
    3. exige ``MIN_TREND_SCENES`` cenas e ``MIN_TREND_INTERVAL_DAYS`` de
       intervalo observado;
    4. a inclinação é estimada por Theil–Sen sobre dias corridos reais;
    5. a magnitude estimada no período é ``inclinação × dias observados`` e
       precisa alcançar o piso do índice (``delta_floor``);
    6. a direção precisa de concordância ``|tau| ≥ TREND_TAU_MIN``;
    7. sem magnitude relevante e com dispersão residual baixa → "estável";
       demais casos → "sem tendência definida" (explicitamente distinto de
       "estável" e de "dados insuficientes").
    """
    index = validate_index(index)
    floor = float(INDEX_CATALOG[index]["delta_floor"])
    accepted = sorted(_accepted_points(timeline), key=lambda x: x["date"])
    points, window = select_trend_window(accepted, window_days)

    def insufficient(reason: str) -> dict:
        return {
            "code": "dados_insuficientes",
            "label": TREND_LABELS["dados_insuficientes"],
            "text": "Não há dados suficientes para avaliar tendência.",
            "reason": reason,
            "scenes_used": len(points),
            "scenes_accepted_total": len(accepted),
            "window": window,
            "delta": None,
            "delta_from": points[0]["date"] if points else None,
            "delta_to": points[-1]["date"] if points else None,
            "estimated_change": None,
            "slope_per_day": None,
            "span_days": _span_days(points),
            "absolute_threshold": floor,
            "tau": None,
            "criteria": TREND_CRITERIA,
        }

    if len(points) < MIN_TREND_SCENES:
        return insufficient(
            f"menos de {MIN_TREND_SCENES} cenas de qualidade alta/média na janela de "
            f"{window.get('days', window_days)} dias"
        )
    span = _span_days(points)
    if span < MIN_TREND_INTERVAL_DAYS:
        return insufficient(f"intervalo observado menor que {MIN_TREND_INTERVAL_DAYS} dias")

    dates = [_safe_date(p["date"]) for p in points]
    x = np.asarray([(d - dates[0]).days for d in dates], dtype=float)
    y = np.asarray([float(p["mean"]) for p in points], dtype=float)
    slope = _theil_sen_slope(x, y)
    if slope is None:
        return insufficient("não foi possível estimar a inclinação temporal")
    estimated_change = float(slope) * span
    endpoint_delta = float(y[-1] - y[0])
    mk = _mann_kendall(x, y)
    tau = mk["tau"] if mk["tau"] is not None else 0.0
    residuals = y - (y[0] + slope * (x - x[0]))
    # Dispersão robusta em torno da reta de Theil–Sen (MAD × 1,4826 ≈ desvio
    # padrão de uma normal, sem sofrer com uma cena discrepante).
    residual_scatter = float(1.4826 * np.median(np.abs(residuals - np.median(residuals))))

    magnitude_ok = abs(estimated_change) >= floor
    direction_ok = abs(tau) >= TREND_TAU_MIN
    # Razão sinal/ruído: a mudança estimada ao longo da janela precisa ser pelo
    # menos tão grande quanto a dispersão típica das cenas em torno da reta.
    # Sem isso, uma série que oscila 0,35 de uma passagem para a outra seria
    # declarada "melhoria" por um arrasto de 0,06 nas pontas.
    signal_to_noise = (abs(estimated_change) / residual_scatter) if residual_scatter > 0 else None
    noise_ok = residual_scatter <= abs(estimated_change)
    if magnitude_ok and direction_ok and noise_ok and tau > 0:
        code = "melhoria"
        text = (
            f"Aumento consistente do {INDEX_CATALOG[index]['label']} na janela analisada "
            f"({window.get('first_observation')} a {window.get('last_observation')})."
        )
    elif magnitude_ok and direction_ok and noise_ok and tau < 0:
        code = "queda"
        text = (
            f"Redução consistente do {INDEX_CATALOG[index]['label']} na janela analisada "
            f"({window.get('first_observation')} a {window.get('last_observation')})."
        )
    elif not magnitude_ok and residual_scatter < floor:
        code = "estavel"
        text = (
            f"Série praticamente horizontal: variação estimada abaixo de {floor:.2f} "
            f"{INDEX_CATALOG[index]['label']} e dispersão baixa entre as cenas."
        )
    else:
        code = "sem_tendencia"
        text = (
            "As cenas oscilam sem direção predominante; não há evidência suficiente "
            "para declarar melhoria ou queda no período."
        )
    return {
        "code": code,
        "label": TREND_LABELS[code],
        "text": text,
        "scenes_used": len(points),
        "scenes_accepted_total": len(accepted),
        "window": window,
        # Delta ponta-a-ponta DA JANELA (primeira → última cena aceita dela).
        "delta": _round_or_none(endpoint_delta),
        "delta_from": points[0]["date"],
        "delta_to": points[-1]["date"],
        # Mudança estimada pela reta robusta ao longo do intervalo observado.
        "estimated_change": _round_or_none(estimated_change),
        "slope_per_day": _round_or_none(slope, 6),
        "span_days": span,
        "absolute_threshold": floor,
        "tau": mk["tau"],
        "tau_threshold": TREND_TAU_MIN,
        "mann_kendall": mk,
        "residual_scatter": _round_or_none(residual_scatter),
        "signal_to_noise": _round_or_none(signal_to_noise, 2),
        "direction_sequence": [1 if c > 0 else -1 if c < 0 else 0 for c in np.diff(y)],
        "criteria": TREND_CRITERIA,
        "method": "Theil–Sen sobre datas reais + tau de Kendall",
    }


def classify_series_quality(timeline: list[dict]) -> dict:
    """Qualidade AGREGADA da série — distinta da qualidade de cada cena.

    A versão anterior rebaixava a análise inteira para "Limitada" assim que
    UMA cena não fosse alta/média. Aqui a classificação é proporcional: o que
    importa é quantas aquisições do período sustentam a análise.
    """
    total = len(timeline)
    counts = {"alta": 0, "media": 0, "limitada": 0, "insuficiente": 0}
    for item in timeline:
        level = item.get("quality")
        if level in counts:
            counts[level] += 1
        else:
            counts["insuficiente"] += 1
    usable = counts["alta"] + counts["media"]
    usable_ratio = (usable / total) if total else 0.0
    high_ratio = (counts["alta"] / total) if total else 0.0
    if total == 0 or usable == 0 or usable < MIN_TREND_SCENES or usable_ratio < 0.34:
        level = "insuficiente"
    elif usable_ratio < 0.60:
        level = "limitada"
    elif usable_ratio < 0.80 or high_ratio < 0.50:
        level = "media"
    else:
        level = "alta"
    labels = {"alta": "Alta", "media": "Média", "limitada": "Limitada", "insuficiente": "Insuficiente"}
    return {
        "scope": "serie",
        "level": level,
        "label": labels[level],
        "scenes": total,
        "usable_scenes": usable,
        "valid_scenes": usable,
        "usable_ratio_pct": round(100.0 * usable_ratio, 1),
        "high_ratio_pct": round(100.0 * high_ratio, 1),
        "distribution": counts,
        "criteria": SERIES_QUALITY_CRITERIA,
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
        return {
            "status": "dados_insuficientes", "scenes_used": len(usable), "window": None,
            "message": f"São necessárias pelo menos {PERSISTENCE_SCENES} cenas processadas para avaliar persistência.",
        }
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
        return {
            "status": "dados_insuficientes", "scenes_used": len(usable), "window": None,
            "message": "Não há pixels pareados suficientes para avaliar persistência.",
        }
    decline = valid.copy(); improve = valid.copy()
    for pair, delta, threshold in transitions:
        decline &= delta <= -threshold
        improve &= delta >= threshold
    polygon_pixels = max(int(np.sum(polygon)), 1)
    def area(mask):
        pixels = int(np.sum(mask)); pct = pixels * 100.0 / polygon_pixels
        return {"pixels": pixels, "pct": round(pct, 2), "ha": round(area_ha * pct / 100.0, 4)}
    dates = [r["date"] for r in usable]
    window = {
        "start": dates[0],
        "end": dates[-1],
        "days": (_safe_date(dates[-1]) - _safe_date(dates[0])).days,
        "scenes": len(usable),
        "transitions": len(transitions),
    }
    return {
        "status": "ok", "scenes_used": len(usable),
        "dates": dates,
        "window": window,
        "declining": area(decline), "improving": area(improve),
        "thresholds_absolute": [round(t[2], 4) for t in transitions],
        "criteria": (
            f"Persistência exige a MESMA direção nas {len(transitions)} transições entre as "
            f"{len(usable)} últimas cenas com dado pareado, cada uma acima do limiar adaptativo "
            "da transição."
        ),
        "text": (
            f"Persistência avaliada entre {dates[0]} e {dates[-1]} ({window['days']} dias, "
            f"{len(usable)} cenas). Persistência espectral eleva o nível de atenção, mas não "
            "confirma problema agronômico."
        ),
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
        # A causa da ausência é declarada, não uniformizada: fonte não
        # configurada, falha da fonte e janela sem passagem são situações
        # diferentes e o operador precisa saber qual delas ocorreu.
        status, message = {
            "not_configured": ("unavailable",
                               "Fonte Sentinel-2 não configurada neste ambiente: nenhuma cena pode ser processada."),
            "error": ("unavailable",
                      "Dados Sentinel-2 indisponíveis no momento (falha ao consultar o catálogo)."),
        }.get(calendar_status, ("insufficient_data",
                                "Nenhuma aquisição Sentinel-2 utilizável no período selecionado."))
        result = _empty_response(index, status, message, detail or {"calendar_status": calendar_status, "period": {"start": start.isoformat(), "end": end.isoformat()}})
        result["source_data"]["calendar_status"] = calendar_status
        result["source_data"]["scene_count"] = 0
        result["source_data"]["scene_with_data_count"] = 0
        result["source_data"]["valid_scene_count"] = 0
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
    accepted_records = _accepted_points(records)
    with_data = [r for r in records if r.get("mean") is not None]

    # Volatilidade/estabilidade descrevem a DISPERSÃO DA JANELA DA TENDÊNCIA,
    # não do histórico inteiro — misturar dois ciclos agrícolas inflaria o
    # desvio e tornaria a métrica ilegível.
    window_points, _ = select_trend_window(accepted_records)
    window_means = np.asarray([r["mean"] for r in window_points], dtype=float) if window_points else np.asarray([])
    volatility = _round_or_none(np.std(window_means), 4) if window_means.size > 1 else None
    floor = float(INDEX_CATALOG[index]["delta_floor"])
    stability = None
    if volatility is not None:
        stability = "estavel" if volatility < floor else "variavel"

    current_raster = processed[-1] if processed else None
    persistence = compute_persistence(processed, area_ha, index)
    anomaly = (
        compute_internal_anomaly(current_raster["values"], current_raster["valid_mask"], area_ha, index)
        if current_raster else {"status": "dados_insuficientes"}
    )

    # ------------------------------------------------------------------
    # Cena atual — a interface precisa distinguir explicitamente:
    #   latest_scene        = última aquisição do catálogo (pode ter falhado)
    #   current             = última aquisição COM valor calculado
    #   latest_valid_scene  = última aquisição ACEITA pela tendência
    # ------------------------------------------------------------------
    latest_scene = records[-1] if records else None
    current = with_data[-1] if with_data else None
    latest_valid = accepted_records[-1] if accepted_records else None
    previous = with_data[-2] if len(with_data) > 1 else None
    current_is_valid = bool(current and current.get("quality") in ACCEPTABLE_QUALITIES)
    if current is None:
        current_note = "Nenhuma aquisição do período produziu valor de índice."
    elif current_is_valid:
        current_note = "A última cena com dado também é a última cena válida para a análise de tendência."
    elif latest_valid is not None:
        current_note = (
            f"A última cena disponível ({current['date']}, qualidade "
            f"{current.get('quality_label', '—').lower()}) NÃO entra na tendência; a última cena válida "
            f"para análise é {latest_valid['date']}."
        )
    else:
        current_note = (
            f"A última cena disponível ({current['date']}) tem qualidade "
            f"{current.get('quality_label', '—').lower()} e nenhuma cena do período é válida para análise."
        )

    series_quality = classify_series_quality(records)
    scene_quality = None
    if current is not None:
        scene_quality = {
            "scope": "cena",
            "level": current.get("quality"),
            "label": current.get("quality_label"),
            "date": current.get("date"),
            "valid_pixel_pct": current.get("valid_pixel_pct"),
            "cloud_cover": current.get("cloud_cover"),
            "criteria": classify_scene_quality(90, 10)["criteria"],
        }

    attention: list[str] = []
    if persistence.get("status") == "ok" and persistence.get("declining", {}).get("pixels", 0):
        window = persistence.get("window") or {}
        attention.append(
            f"Queda espectral persistente em {persistence['declining']['ha']:.2f} ha entre "
            f"{window.get('start', '—')} e {window.get('end', '—')}."
        )
    if anomaly.get("status") == "ok" and anomaly.get("below_internal_baseline", {}).get("pixels", 0):
        attention.append("Há zona abaixo do baseline interno do próprio talhão na cena mais recente processada.")
    if not current_is_valid and current is not None:
        attention.append(current_note)
    if series_quality["level"] in {"limitada", "insuficiente"}:
        attention.append(
            f"Cobertura da série {series_quality['label'].lower()}: "
            f"{series_quality['usable_scenes']} de {series_quality['scenes']} cenas são úteis."
        )

    result = {
        "status": "ok" if accepted_records else "insufficient_data",
        "message": None if accepted_records else "Não há cenas com qualidade suficiente para avaliar tendência no período.",
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1,
                   "scope": "histórico exibido na timeline"},
        "index": index,
        "index_definition": INDEX_CATALOG[index],
        "timeline": records,
        "source_data": {
            "provider": PROVIDER, "collection": COLLECTION, "processing_level": PROCESSING_LEVEL,
            "calendar_source": "sentinel-cdse", "calendar_status": calendar_status,
            "calendar_coverage": detail,
            "is_real": True, "scene_count": len(records),
            "scene_with_data_count": len(with_data),
            "valid_scene_count": len(accepted_records),
        },
        "metrics": {
            "current": current,
            "latest_scene": latest_scene,
            "latest_valid_scene": latest_valid,
            "current_is_valid_for_analysis": current_is_valid,
            "current_scene_note": current_note,
            "previous": previous,
            "delta_previous": current.get("delta_previous") if current else None,
            "delta_previous_detail": {
                "value": current.get("delta_previous"),
                "from_date": current.get("delta_previous_from"),
                "to_date": current.get("date"),
                "days": current.get("delta_previous_days"),
                "reference": "cena anterior com dado",
                "unit": INDEX_CATALOG[index]["label"],
            } if current and current.get("delta_previous") is not None else None,
            "delta_30d": current.get("delta_30d") if current else None,
            "delta_30d_detail": {
                "value": current.get("delta_30d"),
                "from_date": current.get("delta_30d_from"),
                "to_date": current.get("date"),
                "days": current.get("delta_30d_days"),
                "reference": f"cena mais próxima de {DELTA_30D_TARGET_DAYS} dias antes (±{DELTA_30D_TOLERANCE_DAYS} dias)",
                "unit": INDEX_CATALOG[index]["label"],
            } if current and current.get("delta_30d") is not None else None,
            "trend": trend["code"], "trend_label": trend["label"], "trend_detail": trend,
            "stability": stability, "volatility": volatility,
            "volatility_scope": "janela da tendência",
            "persistence": persistence, "internal_anomaly": anomaly,
        },
        "interpretation": {
            "trend": trend["text"],
            "attention": attention,
            "scientific_note": "Índice espectral não é diagnóstico agronômico. Os dados descrevem mudança relativa e não permitem determinar sua causa.",
        },
        "quality": series_quality,
        "scene_quality": scene_quality,
        "confidence": {
            **series_quality,
            "alias_of": "quality",
            "note": "Campo mantido por compatibilidade: repete literalmente a qualidade agregada da série. Não é uma dimensão independente e não é exibido como tal na interface.",
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
