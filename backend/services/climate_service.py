"""
Serviço climático consolidado — NASA POWER (PR #7 · Clima & Inteligência Agronômica).

Decisões de projeto (detalhamento em AUDITORIA_CLIMA_PR7.md):

- O BACKEND é o único componente que fala com a NASA POWER. O frontend
  consome somente ``/api/climate/farm/{farm_id}``.
- A localização é SEMPRE a canônica da fazenda (PR #6: geometria > ponto
  confirmado > legado). Este serviço recebe (lat, lon) já resolvidas e as
  valida; não existe "escolher cidade" nem coordenada fixa primária.
- Lacuna NUNCA é imputada: o fill value da fonte (-999.0) vira ``None`` e é
  contado em ``data_quality.coverage_pct``. Nenhuma métrica é inventada.
- NÃO há fallback silencioso: falha da fonte (timeout/HTTP/parse) levanta
  ``ClimateSourceError`` e o endpoint devolve um estado explícito de
  indisponibilidade — nunca dado demonstrativo no lugar de dado real.
- Categorias de informação separadas e rotuladas (Fase 5):
  * ``dado_observado`` — valores da fonte (reanálise/modelo, NÃO medição);
  * ``dado_calculado`` — agregações/indicadores derivados (fórmula declarada);
  * ``interpretacao`` — texto CONSERVADOR (nunca causalidade, nunca diagnóstico).
- Evapotranspiração (ET) NÃO é calculada: a Daily API AG não entrega ET e
  este PR não inventa metodologia (Fase 3).
- Baseline histórico: média das N safras anteriores (padrão 5 anos, máx. 10)
  sobre a MESMA janela do calendário (mes/dia idênticos). Rótulo: "referência
  histórica" — explicitamente NÃO é "normal climatológica oficial" (que
  exigiria 30 anos por norma WMO).
- Preparação para PR #8/#9: séries com datas ISO-8601 absolutas e período
  ``start/end`` permitem junção TEMPORAL (não causal) com o calendário
  Sentinel-2 e, futuramente, com eventos de manejo/bioinsumos.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from typing import Any, Optional

import requests

from config import settings
from services.geolocation_service import validate_coordinates

logger = logging.getLogger("orion.climate")

# ---------------------------------------------------------------------------
# Fonte — parâmetros REALMENTE suportados pela NASA POWER Daily API (AG)
# Unidades conforme o bloco `parameters` da resposta da API (verificado
# ao vivo em 09/09/2026). NADA aqui é inventado: se a fonte não entrega,
# não entra no payload.
# ---------------------------------------------------------------------------
CLIMATE_SOURCE_ID = "nasa-power"
CLIMATE_SOURCE_NAME = "NASA POWER (Prediction Of Worldwide Energy Resources)"
CLIMATE_SOURCE_ENDPOINT = "/api/temporal/daily/point"

#: Comunidade AG = Agroclimatação (documentação oficial: daily time series).
NASA_COMMUNITY = "AG"

#: Ordem relevante (limite da API: até 20 parâmetros por point request).
NASA_PARAMETERS: dict[str, dict[str, str]] = {
    "T2M": {
        "longname": "Temperature at 2 Meters",
        "units": "C",
        "nature": "reanálise/modelo (MERRA-2) — média diária",
    },
    "T2M_MAX": {
        "longname": "Temperature at 2 Meters Maximum",
        "units": "C",
        "nature": "reanálise/modelo (MERRA-2) — máxima do dia",
    },
    "T2M_MIN": {
        "longname": "Temperature at 2 Meters Minimum",
        "units": "C",
        "nature": "reanálise/modelo (MERRA-2) — mínima do dia",
    },
    "PRECTOTCORR": {
        "longname": "Precipitation Corrected",
        "units": "mm/day",
        "nature": "reanálise/modelo (MERRA-2) — acumulado do dia",
    },
    "RH2M": {
        "longname": "Relative Humidity at 2 Meters",
        "units": "%",
        "nature": "reanálise/modelo (MERRA-2) — média diária",
    },
    "WS2M": {
        "longname": "Wind Speed at 2 Meters",
        "units": "m/s",
        "nature": "reanálise/modelo (MERRA-2) — média diária",
    },
    "ALLSKY_SFC_SW_DWN": {
        "longname": "All Sky Surface Shortwave Downward Irradiance",
        "units": "MJ/m^2/day",
        "nature": (
            "reanálise/modelo (FLASHFlux/CERES); na comunidade AG o valor "
            "diário é o ACUMULADO do dia (MJ/m²)"
        ),
    },
}
NASA_PARAMETER_LIST = ",".join(NASA_PARAMETERS)

#: Fill value oficial da API (header.fill_value): marca dado ausente.
FILL_VALUE = -999.0

#: Categorias de informação (Fase 5 — nunca misturar).
CATEGORY_DATA = "dado_observado"
CATEGORY_CALC = "dado_calculado"
CATEGORY_INTERP = "interpretacao"

#: Início do catálogo Daily (documentação oficial: 1981-01-01 → NRT).
_CATALOG_START = _dt.date.fromisoformat(settings.nasa_power_catalog_start)


class ClimateRangeError(ValueError):
    """Período de consulta inválido (incoerente ou fora do catálogo)."""


class ClimateSourceError(Exception):
    """Falha ao consultar a fonte (timeout/HTTP/parse). Nunca vira dado fake."""

    def __init__(self, code: str, message: str, http_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Cache raw (série diária) — mesmo padrão do projeto: in-memory, thread-safe,
# TTL + LRU por idade. A chave considera localização, PERÍODO E parâmetros
# (Fase 10). Falhas NÃO são cacheadas (toda tentativa é refeita).
# ---------------------------------------------------------------------------
_raw_cache: dict[tuple, tuple[float, dict]] = {}
_raw_cache_lock = threading.Lock()


def _raw_cache_get(key: tuple):
    with _raw_cache_lock:
        item = _raw_cache.get(key)
        if item is not None:
            age_h = (time.monotonic() - item[0]) / 3600.0
            if age_h < settings.nasa_climate_cache_hours:
                return item[1]
            _raw_cache.pop(key, None)
    return None


def _raw_cache_set(key: tuple, payload: dict) -> None:
    with _raw_cache_lock:
        _raw_cache[key] = (time.monotonic(), payload)
        if len(_raw_cache) > settings.nasa_climate_cache_max_entries:
            oldest = min(_raw_cache, key=lambda k: _raw_cache[k][0])
            _raw_cache.pop(oldest, None)


def _cache_key(lat: float, lon: float, start: _dt.date, end: _dt.date) -> tuple:
    # Arredondamento a 3 casas (~111 m): mesmo nível do cache legado — evita
    # entradas distintas para a mesma localização canônica regravada.
    return (
        round(float(lat), 3),
        round(float(lon), 3),
        start.isoformat(),
        end.isoformat(),
        NASA_PARAMETER_LIST,  # parâmetros fazem parte da chave (Fase 10)
    )


# ---------------------------------------------------------------------------
# Consulta à fonte
# ---------------------------------------------------------------------------
def _validate_range(start: _dt.date, end: _dt.date) -> None:
    if not isinstance(start, _dt.date) or not isinstance(end, _dt.date):
        raise ClimateRangeError("start/end devem ser datas (YYYY-MM-DD).")
    if start > end:
        raise ClimateRangeError("start não pode ser posterior a end.")
    if start < _CATALOG_START:
        raise ClimateRangeError(
            f"A NASA POWER Daily tem dados a partir de {_CATALOG_START.isoformat()}."
        )
    max_days = settings.nasa_power_max_period_days
    if (end - start).days + 1 > max_days:
        raise ClimateRangeError(
            f"Período máximo de {max_days} dias por consulta "
            f"(recebido: {(end - start).days + 1})."
        )


def fetch_nasa_power_daily(
    lat: float, lon: float, start: _dt.date, end: _dt.date
) -> dict:
    """
    Busca a série diária normalizada da NASA POWER para [start, end].

    Retorna::

        {
          "dates": ["2026-08-01", ...],           # todos os dias do período
          "values": {
            "T2M": {"2026-08-01": 25.34, ..., "2026-08-03": None},
            ...                                    # fill -999.0 -> None
          },
          "metadata": {
            "api_version": "v2.9.7", "time_standard": "LST",
            "sources": ["FLASHFLUX", "GEOSIT", "POWER"],
            "fill_value": -999.0, "site_elevation_m": 541.76,
          },
          "coordinates": {"latitude": ..., "longitude": ...},
          "period": {"start": ..., "end": ...},
        }

    Erros: ``ClimateRangeError`` (período inválido), ``ClimateSourceError``
    (timeout/http_error/parse_error/network_error) ou ``ValueError``
    (coordenada inválida, via ``validate_coordinates``).
    """
    lat, lon = validate_coordinates(lat, lon)
    _validate_range(start, end)

    key = _cache_key(lat, lon, start, end)
    cached = _raw_cache_get(key)
    if cached is not None:
        logger.info(
            "NASA POWER: cache hit (%.3f, %.3f) %s→%s", lat, lon,
            start.isoformat(), end.isoformat(),
        )
        return cached

    url = f"{settings.nasa_power_base_url}{CLIMATE_SOURCE_ENDPOINT}"
    params = {
        "parameters": NASA_PARAMETER_LIST,
        "community": NASA_COMMUNITY,
        "longitude": lon,
        "latitude": lat,
        "start": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "format": "JSON",
    }
    try:
        response = requests.get(url, params=params, timeout=settings.nasa_power_timeout_s)
    except requests.Timeout as exc:
        raise ClimateSourceError(
            "timeout",
            f"Timeout de {settings.nasa_power_timeout_s:g}s ao consultar a NASA POWER.",
        ) from exc
    except requests.RequestException as exc:
        raise ClimateSourceError("network_error", f"Falha de rede ao consultar a NASA POWER: {exc}") from exc

    if response.status_code != 200:
        raise ClimateSourceError(
            "http_error",
            f"NASA POWER respondeu HTTP {response.status_code}.",
            http_status=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ClimateSourceError("parse_error", "Resposta da NASA POWER não é JSON válido.") from exc

    parameter = (payload.get("properties") or {}).get("parameter") or {}
    if not parameter:
        raise ClimateSourceError(
            "parse_error", "Resposta da NASA POWER sem bloco 'properties.parameter'."
        )

    header = payload.get("header") or {}
    geometry = payload.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []

    dates = list(_iter_dates(start, end))
    values: dict[str, dict[str, Optional[float]]] = {}
    for param in NASA_PARAMETERS:
        series = parameter.get(param) or {}
        values[param] = {}
        for d in dates:
            raw = series.get(d.strftime("%Y%m%d"))
            values[param][d.isoformat()] = (
                None if (raw is None or float(raw) <= FILL_VALUE) else float(raw)
            )

    result = {
        "dates": [d.isoformat() for d in dates],
        "values": values,
        "metadata": {
            "api_version": (header.get("api") or {}).get("version"),
            "time_standard": header.get("time_standard"),
            "sources": header.get("sources"),
            "fill_value": header.get("fill_value", FILL_VALUE),
            "site_elevation_m": (
                round(float(coordinates[2]), 2) if len(coordinates) > 2 and coordinates[2] is not None else None
            ),
        },
        "coordinates": {"latitude": lat, "longitude": lon},
        "period": {"start": start.isoformat(), "end": end.isoformat()},
    }
    _raw_cache_set(key, result)
    logger.info(
        "NASA POWER: série diária processada (%.3f, %.3f) %s→%s (%d dias)",
        lat, lon, start.isoformat(), end.isoformat(), len(dates),
    )
    return result


def _iter_dates(start: _dt.date, end: _dt.date):
    d = start
    one = _dt.timedelta(days=1)
    while d <= end:
        yield d
        d += one


def _shift_year(d: _dt.date, year: int) -> Optional[_dt.date]:
    """Move a data para outro ano preservando dia/mês (29/fev -> 28/fev)."""
    try:
        return d.replace(year=year)
    except ValueError:
        return d.replace(year=year, day=28)


# ---------------------------------------------------------------------------
# Estatísticas (nunca imputa: None sai da estatística e conta em 'missing')
# ---------------------------------------------------------------------------
def _stat_mean(series: list[Optional[float]]) -> Optional[float]:
    valid = [v for v in series if v is not None]
    return round(sum(valid) / len(valid), 2) if valid else None


def _stat_min(series: list[Optional[float]]) -> Optional[float]:
    valid = [v for v in series if v is not None]
    return round(min(valid), 2) if valid else None


def _stat_max(series: list[Optional[float]]) -> Optional[float]:
    valid = [v for v in series if v is not None]
    return round(max(valid), 2) if valid else None


def _longest_dry_streak(precip: list[Optional[float]], threshold_mm: float) -> int:
    """Maior sequência consecutiva de dias SEM precipitação relevante.

    Dia "seco" = valor presente e < threshold. Dia ausente (None) INTERROMPE
    a contagem (não se sabe — conservador).
    """
    best = cur = 0
    for v in precip:
        if v is not None and v < threshold_mm:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _coverage(values: dict[str, dict[str, Optional[float]]], dates: list[str]) -> float:
    """Fração de dias com TODOS os parâmetros presentes."""
    if not dates:
        return 0.0
    complete = 0
    for d in dates:
        if all(values[p].get(d) is not None for p in NASA_PARAMETERS):
            complete += 1
    return complete / len(dates)


# ---------------------------------------------------------------------------
# Agregações por métrica (período atual)
# ---------------------------------------------------------------------------
def _aggregate_precipitation(series: list[Optional[float]]) -> dict:
    valid = [v for v in series if v is not None]
    threshold = settings.nasa_climate_rainy_day_threshold_mm
    return {
        "category": CATEGORY_CALC,
        "unit": "mm",
        "accumulated_mm": round(sum(valid), 1) if valid else None,
        "daily_mean_mm": round(sum(valid) / len(valid), 2) if valid else None,
        "rainy_days": sum(1 for v in valid if v >= threshold),
        "rainy_day_threshold_mm": threshold,
        "longest_dry_streak_days": _longest_dry_streak(series, threshold),
    }


def _aggregate_temperature(
    t: list[Optional[float]],
    tmax: list[Optional[float]],
    tmin: list[Optional[float]],
) -> dict:
    amplitudes = [
        round(m - n, 2) for m, n in zip(tmax, tmin) if m is not None and n is not None
    ]
    return {
        "category": CATEGORY_CALC,
        "unit": "C",
        "mean_c": _stat_mean(t),
        "min_c": _stat_min(tmin),
        "max_c": _stat_max(tmax),
        "amplitude_mean_c": round(sum(amplitudes) / len(amplitudes), 2) if amplitudes else None,
    }


def _aggregate_radiation(series: list[Optional[float]]) -> dict:
    valid = [v for v in series if v is not None]
    return {
        "category": CATEGORY_CALC,
        "unit": "MJ/m^2/day",
        "daily_mean_mj_m2": round(sum(valid) / len(valid), 2) if valid else None,
        "accumulated_mj_m2": round(sum(valid), 1) if valid else None,
        "note": "A unidade MJ/m²/dia da fonte AG já é o acumulado do dia; a média é a agregação primária.",
    }


def _aggregate_humidity(series: list[Optional[float]]) -> dict:
    return {
        "category": CATEGORY_CALC,
        "unit": "%",
        "mean_pct": _stat_mean(series),
        "min_pct": _stat_min(series),
        "max_pct": _stat_max(series),
    }


def _aggregate_wind(series: list[Optional[float]]) -> dict:
    valid = [v for v in series if v is not None]
    return {
        "category": CATEGORY_CALC,
        "unit": "m/s (média diária da fonte); km/h = m/s × 3,6",
        "mean_ms": _stat_mean(series),
        "mean_kmh": round(sum(valid) / len(valid) * 3.6, 1) if valid else None,
        "max_daily_mean_kmh": round(max(valid) * 3.6, 1) if valid else None,
        "note": "A fonte entrega MÉDIA diária do vento; 'máximo' é o maior entre médias diárias (não é rajada).",
    }


# ---------------------------------------------------------------------------
# Baseline histórico (mesma janela do calendário, N anos anteriores)
# ---------------------------------------------------------------------------
def _baseline_window(
    start: _dt.date, end: _dt.date, year: int
) -> Optional[tuple[_dt.date, _dt.date]]:
    s, e = _shift_year(start, year), _shift_year(end, year)
    if s is None or e is None or s > e:
        return None
    if (e - s).days + 1 != (end - start).days + 1:
        # Janela de anos bissextos não casa dia a dia -> ano excluído
        # (documentado; caso raro: janela que atravessa 29/02).
        return None
    if s < _CATALOG_START:
        return None
    return s, e


def _baseline_metric(series: list[Optional[float]]) -> Optional[float]:
    valid = [v for v in series if v is not None]
    return sum(valid) / len(valid) if valid else None


def _build_baseline(
    lat: float,
    lon: float,
    start: _dt.date,
    end: _dt.date,
    baseline_years: int,
) -> dict:
    """
    Referência histórica = média dos `baseline_years` anos ANTERIORES à data
    `end`, sobre a MESMA janela do calendário (mes/dia idênticos).

    Cada ano é uma consulta pequena (mesmo comprimento do período pedido) e
    cacheada — nenhuma consulta monolítica multi-anual.
    """
    window_days = (end - start).days + 1
    years: dict[str, dict[str, Optional[float]]] = {}
    profiles: list[dict[str, list[Optional[float]]]] = []
    valid_years = 0

    for year in range(end.year - 1, end.year - 1 - baseline_years, -1):
        window = _baseline_window(start, end, year)
        if window is None:
            continue
        try:
            data = fetch_nasa_power_daily(lat, lon, window[0], window[1])
        except (ClimateSourceError, ClimateRangeError, ValueError):
            # Ano ausente/inacessível: não contamina a referência.
            continue
        years[str(year)] = {
            "start": window[0].isoformat(),
            "end": window[1].isoformat(),
            "precip_accumulated_mm": _stat_mean([
                sum(v for v in data["values"]["PRECTOTCORR"].values() if v is not None)
            ]),
            "temp_mean_c": _stat_mean([data["values"]["T2M"][d] for d in data["dates"]]),
            "radiation_daily_mean_mj_m2": _stat_mean(
                [data["values"]["ALLSKY_SFC_SW_DWN"][d] for d in data["dates"]]
            ),
        }
        profiles.append(
            {
                "precip_mm": [data["values"]["PRECTOTCORR"][d] for d in data["dates"]],
                "temp_mean_c": [data["values"]["T2M"][d] for d in data["dates"]],
            }
        )
        valid_years += 1

    def _ref(metric: str) -> Optional[float]:
        vals = [y[metric] for y in years.values() if y[metric] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    # Perfil diário de referência = média elementar dos anos válidos
    # (mesma posição da janela em todos os anos — garantido por _baseline_window).
    profile: dict[str, list[Optional[float]]] = {"precip_mm": [], "temp_mean_c": []}
    if profiles:
        for i in range(window_days):
            profile["precip_mm"].append(
                _stat_mean([p["precip_mm"][i] for p in profiles])
            )
            profile["temp_mean_c"].append(
                _stat_mean([p["temp_mean_c"][i] for p in profiles])
            )

    return {
        "label": "referência histórica (mesma janela do calendário)",
        "years": valid_years,
        "requested_years": baseline_years,
        "per_year": years,
        "reference": {
            "precip_accumulated_mm": _ref("precip_accumulated_mm"),
            "temp_mean_c": _ref("temp_mean_c"),
            "radiation_daily_mean_mj_m2": _ref("radiation_daily_mean_mj_m2"),
        },
        "daily_profile": profile,
        "methodology": (
            f"Média de {valid_years} ano(s) anterior(es) a {end.year} para a mesma "
            f"janela do calendário ({start.strftime('%d/%m')}–{end.strftime('%d/%m')}). "
            "NÃO é 'normal climatológica oficial' (exige 30 anos por norma WMO)."
        ),
    }


def _deviation(current: Optional[float], reference: Optional[float]) -> dict:
    if current is None or reference is None:
        return {
            "deviation": None,
            "deviation_pct": None,
            "classification": None,
            "classification_label": None,
        }
    delta = round(current - reference, 2)
    pct = round((delta / reference) * 100.0, 1) if reference else None
    band = settings.nasa_climate_baseline_band_pct
    if pct is None:
        cls, label = "proxima_da_referencia", "próxima da referência histórica"
    elif pct <= -band:
        cls, label = "abaixo_da_referencia", "abaixo da referência histórica"
    elif pct >= band:
        cls, label = "acima_da_referencia", "acima da referência histórica"
    else:
        cls, label = "proxima_da_referencia", "próxima da referência histórica"
    return {
        "deviation": delta,
        "deviation_pct": pct,
        "classification": cls,
        "classification_label": label,
    }


# ---------------------------------------------------------------------------
# Confiança (Fase 8) — critérios OBJETIVOS, documentados na resposta
# ---------------------------------------------------------------------------
def _assess_confidence(
    coverage: float, baseline: Optional[dict]
) -> dict:
    reasons: list[str] = []
    valid_years = baseline["years"] if baseline else None
    if coverage < 0.90:
        level = "limitada"
        reasons.append(f"cobertura de dados de {coverage:.0%} (abaixo de 90%)")
    elif coverage < 0.98:
        level = "media"
        reasons.append(f"cobertura de dados de {coverage:.0%} (90–98%)")
    else:
        level = "alta"
    if baseline is not None and valid_years is not None:
        if valid_years == 0:
            level = "limitada"
            reasons.append("nenhum ano válido na referência histórica")
        elif valid_years < 3:
            if level != "limitada":
                level = "media"
            reasons.append(f"apenas {valid_years} ano(s) válido(s) na referência histórica")
    if level == "alta" and not reasons:
        reasons.append("dados completos para praticamente todo o período e referência com ≥3 anos")
    return {
        "level": level,
        "coverage_pct": round(coverage * 100.0, 1),
        "valid_baseline_years": valid_years,
        "criteria": (
            "Alta: cobertura ≥98% dos dias e referência com ≥3 anos válidos. "
            "Média: cobertura 90–98% OU referência com 1–2 anos válidos. "
            "Limitada: cobertura <90% OU referência sem anos válidos."
        ),
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Interpretações CONSERVADORAS (Fases 5/18) — nunca causalidade
# ---------------------------------------------------------------------------
def _interpretations(
    coverage: float,
    metrics: dict,
    baseline: Optional[dict],
) -> list[dict]:
    out: list[dict] = []
    band = settings.nasa_climate_baseline_band_pct

    def _add(id_: str, topic: str, text: str, confidence: str, basis: dict) -> None:
        out.append(
            {
                "id": id_,
                "topic": topic,
                "category": CATEGORY_INTERP,
                "text": text,
                "confidence": confidence,
                "basis": basis,
            }
        )

    prec = metrics["precipitation"]
    t = metrics["temperature"]
    rad = metrics["radiation"]
    hum = metrics["humidity"]
    wind = metrics["wind"]
    ref = (baseline or {}).get("reference", {})

    if prec["accumulated_mm"] is None:
        _add(
            "precip", "Precipitação",
            "Não há dados suficientes de precipitação para este período.",
            "limitada", {"accumulated_mm": None},
        )
    else:
        d = _deviation(prec["accumulated_mm"], ref.get("precip_accumulated_mm"))
        basis = {
            "accumulated_mm": prec["accumulated_mm"],
            "reference_mm": ref.get("precip_accumulated_mm"),
            "deviation_pct": d["deviation_pct"],
        }
        if d["classification"] is None:
            _add(
                "precip", "Precipitação",
                f"Precipitação acumulada de {prec['accumulated_mm']} mm no período; "
                "não há referência histórica suficiente para comparação.",
                "media", basis,
            )
        elif d["classification"] == "abaixo_da_referencia":
            _add(
                "precip", "Precipitação",
                f"Precipitação acumulada ({prec['accumulated_mm']} mm) abaixo da "
                f"referência histórica ({ref['precip_accumulated_mm']} mm, "
                f"{d['deviation_pct']}%). Os dados indicam período mais seco do que a "
                "média histórica para esta janela. É compatível com atenção ao "
                "manejo hídrico, mas não há evidência suficiente para afirmar "
                "estresse das culturas sem outras fontes.",
                "media", basis,
            )
        elif d["classification"] == "acima_da_referencia":
            _add(
                "precip", "Precipitação",
                f"Precipitação acumulada ({prec['accumulated_mm']} mm) acima da "
                f"referência histórica ({ref['precip_accumulated_mm']} mm, "
                f"+{d['deviation_pct']}%). É compatível com condição mais úmida "
                "do que a média histórica; merece acompanhamento (ex.: logística "
                "de campo e aplicação).",
                "media", basis,
            )
        else:
            _add(
                "precip", "Precipitação",
                f"Precipitação acumulada ({prec['accumulated_mm']} mm) próxima da "
                f"referência histórica ({ref['precip_accumulated_mm']} mm, dentro de "
                f"±{band:g}%).",
                "alta", basis,
            )

    if t["mean_c"] is not None:
        d = _deviation(t["mean_c"], ref.get("temp_mean_c"))
        basis = {
            "mean_c": t["mean_c"],
            "reference_c": ref.get("temp_mean_c"),
            "deviation_c": d["deviation"],
        }
        if d["deviation"] is None:
            text = (
                f"Temperatura média de {t['mean_c']} °C no período (mín. "
                f"{t['min_c']} °C / máx. {t['max_c']} °C); sem referência "
                "histórica suficiente para comparação."
            )
        elif d["deviation"] < 0:
            text = (
                f"Temperatura média de {t['mean_c']} °C, {abs(d['deviation'])} °C "
                f"abaixo da referência histórica ({ref['temp_mean_c']} °C). "
                "Os dados indicam período mais frio do que a média histórica."
            )
        elif d["deviation"] > 0:
            text = (
                f"Temperatura média de {t['mean_c']} °C, {d['deviation']} °C acima "
                f"da referência histórica ({ref['temp_mean_c']} °C). Os dados "
                "indicam período mais quente do que a média histórica."
            )
        else:
            text = f"Temperatura média de {t['mean_c']} °C, alinhada à referência histórica."
        if t["amplitude_mean_c"] is not None:
            text += f" Amplitude térmica diária média de {t['amplitude_mean_c']} °C."
        _add("temp", "Temperatura", text, "alta" if d["deviation"] is not None else "media", basis)

    if rad["daily_mean_mj_m2"] is not None:
        d = _deviation(rad["daily_mean_mj_m2"], ref.get("radiation_daily_mean_mj_m2"))
        _add(
            "radiation", "Radiação",
            (
                f"Radiação solar diária média de {rad['daily_mean_mj_m2']} MJ/m² "
                f"(acumulado do período: {rad['accumulated_mj_m2']} MJ/m²). "
                + (
                    f"Desvio de {d['deviation_pct']}% em relação à referência histórica "
                    f"({ref['radiation_daily_mean_mj_m2']} MJ/m²/dia). Os dados "
                    "apenas descrevem disponibilidade de radiação; não permitem "
                    "conclusão sobre fotossíntese sem outras evidências."
                    if d["deviation_pct"] is not None
                    else "Sem referência histórica suficiente para comparação."
                )
            ),
            "media",
            {"daily_mean_mj_m2": rad["daily_mean_mj_m2"],
             "reference_mj_m2": ref.get("radiation_daily_mean_mj_m2"),
             "deviation_pct": d["deviation_pct"]},
        )

    if hum["mean_pct"] is not None:
        _add(
            "humidity", "Umidade",
            (
                f"Umidade relativa média de {hum['mean_pct']}% (extremos: "
                f"{hum['min_pct']}% a {hum['max_pct']}%). Dado descritivo: por "
                "si só, não sustenta conclusões agronômicas."
            ),
            "media",
            {"mean_pct": hum["mean_pct"], "min_pct": hum["min_pct"], "max_pct": hum["max_pct"]},
        )

    if wind["mean_kmh"] is not None:
        _add(
            "wind", "Vento",
            (
                f"Vento médio de {wind['mean_kmh']} km/h (maior média diária: "
                f"{wind['max_daily_mean_kmh']} km/h). A fonte fornece médias "
                "diárias — não há rajadas no período analisado."
            ),
            "media",
            {"mean_kmh": wind["mean_kmh"], "max_daily_mean_kmh": wind["max_daily_mean_kmh"]},
        )

    dry = prec["longest_dry_streak_days"]
    if prec["accumulated_mm"] is not None and dry >= 7:
        _add(
            "dry_streak", "Sequência seca",
            (
                f"Os dados indicam a maior sequência sem precipitação relevante "
                f"(≥{settings.nasa_climate_rainy_day_threshold_mm:g} mm/dia) de "
                f"{dry} dias dentro do período. É compatível com período seco; "
                "o significado agronômico depende de cultura, estádio e solo — "
                "não há dados suficientes aqui para afirmar estresse."
            ),
            "media",
            {"longest_dry_streak_days": dry,
             "threshold_mm": settings.nasa_climate_rainy_day_threshold_mm},
        )

    if coverage < 0.90:
        _add(
            "data_quality", "Qualidade dos dados",
            (
                f"Não há dados suficientes para o período completo: cobertura de "
                f"{coverage:.0%}. As análises têm confiança limitada e devem ser "
                "refeitas quando a fonte consolidar os dias restantes."
            ),
            "limitada",
            {"coverage_pct": round(coverage * 100, 1)},
        )

    return out


# ---------------------------------------------------------------------------
# Relatório completo (API pública do serviço)
# ---------------------------------------------------------------------------
def build_climate_report(
    lat: float,
    lon: float,
    start: _dt.date,
    end: _dt.date,
    baseline_years: Optional[int] = None,
    include_baseline: bool = True,
) -> dict:
    """
    Pipeline completo: série diária → agregações → baseline → indicadores →
    interpretações → confiança → proveniência.

    ``lat/lon`` devem vir da localização CANÔNICA da fazenda (PR #6) — o
    endpoint valida e documenta isso. Não há imputação nem fallback:
    falha de fonte = ``ClimateSourceError``.
    """
    lat, lon = validate_coordinates(lat, lon)
    _validate_range(start, end)
    years = settings.nasa_climate_baseline_years if baseline_years is None else baseline_years

    current = fetch_nasa_power_daily(lat, lon, start, end)
    dates: list[str] = current["dates"]
    values = current["values"]

    baseline: Optional[dict] = None
    if include_baseline and years >= 1:
        baseline = _build_baseline(lat, lon, start, end, years)

    coverage = _coverage(values, dates)

    precip_series = [values["PRECTOTCORR"][d] for d in dates]
    temp_series = [values["T2M"][d] for d in dates]
    tmax_series = [values["T2M_MAX"][d] for d in dates]
    tmin_series = [values["T2M_MIN"][d] for d in dates]
    rad_series = [values["ALLSKY_SFC_SW_DWN"][d] for d in dates]
    hum_series = [values["RH2M"][d] for d in dates]
    wind_series = [values["WS2M"][d] for d in dates]

    metrics = {
        "precipitation": _aggregate_precipitation(precip_series),
        "temperature": _aggregate_temperature(temp_series, tmax_series, tmin_series),
        "radiation": _aggregate_radiation(rad_series),
        "humidity": _aggregate_humidity(hum_series),
        "wind": _aggregate_wind(wind_series),
    }

    # Baseline anexado por métrica (desvio + classificação)
    ref = (baseline or {}).get("reference", {})
    metrics["precipitation"]["baseline"] = _deviation(
        metrics["precipitation"]["accumulated_mm"], ref.get("precip_accumulated_mm")
    )
    metrics["temperature"]["baseline"] = _deviation(
        metrics["temperature"]["mean_c"], ref.get("temp_mean_c")
    )
    metrics["radiation"]["baseline"] = _deviation(
        metrics["radiation"]["daily_mean_mj_m2"], ref.get("radiation_daily_mean_mj_m2")
    )
    if baseline is not None:
        for m in metrics.values():
            m.setdefault("baseline", {"deviation": None, "deviation_pct": None,
                                      "classification": None, "classification_label": None})

    indicators = [
        {
            "id": "chuva_acumulada", "name": "Chuva acumulada no período",
            "value": metrics["precipitation"]["accumulated_mm"], "unit": "mm",
            "category": CATEGORY_CALC,
            "formula": "Soma dos acumulados diários (PRECTOTCORR, mm/dia) dos dias com dados.",
        },
        {
            "id": "dias_com_chuva", "name": "Dias com precipitação relevante",
            "value": metrics["precipitation"]["rainy_days"], "unit": "dias",
            "category": CATEGORY_CALC,
            "formula": f"Contagem de dias com chuva ≥ {settings.nasa_climate_rainy_day_threshold_mm:g} mm (threshold configurável).",
        },
        {
            "id": "sequencia_seca", "name": "Maior sequência sem chuva relevante",
            "value": metrics["precipitation"]["longest_dry_streak_days"], "unit": "dias",
            "category": CATEGORY_CALC,
            "formula": "Maior sequência consecutiva de dias com chuva < threshold (dia sem dados interrompe a contagem).",
        },
        {
            "id": "amplitude_termica", "name": "Amplitude térmica diária média",
            "value": metrics["temperature"]["amplitude_mean_c"], "unit": "C",
            "category": CATEGORY_CALC,
            "formula": "Média de (T2M_MAX − T2M_MIN) nos dias com ambos os valores.",
        },
        {
            "id": "desvio_chuva", "name": "Desvio de precipitação vs. referência histórica",
            "value": metrics["precipitation"]["baseline"].get("deviation"), "unit": "mm",
            "category": CATEGORY_CALC,
            "formula": "Acumulado do período − média da referência histórica (mesma janela, N anos anteriores).",
        },
        {
            "id": "desvio_temperatura", "name": "Desvio de temperatura média vs. referência histórica",
            "value": metrics["temperature"]["baseline"].get("deviation"), "unit": "C",
            "category": CATEGORY_CALC,
            "formula": "Média do período − média da referência histórica (mesma janela, N anos anteriores).",
        },
    ]

    interpretations = _interpretations(coverage, metrics, baseline)
    confidence = _assess_confidence(coverage, baseline)

    # Estados explícitos (Fase 9) — nunca dado inventado
    if coverage == 0.0:
        status, message = "insufficient_data", "Não há dados suficientes para este período."
    elif coverage < 0.75:
        status, message = "partial", (
            f"Dados parciais: apenas {coverage:.0f}% dos dias do período possuem "
            "dados completos na fonte."
        )
    else:
        status, message = "ok", None

    missing_days = [
        d for d in dates
        if any(values[p].get(d) is None for p in NASA_PARAMETERS)
    ]

    report = {
        "status": status,
        "message": message,
        "farm_location": {
            "latitude": current["coordinates"]["latitude"],
            "longitude": current["coordinates"]["longitude"],
            "source": "canonical",
            "note": "Coordenadas canônicas da fazenda (PR #6): geometria > ponto confirmado > legado.",
        },
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": (end - start).days + 1,
        },
        "source": {
            "id": CLIMATE_SOURCE_ID,
            "name": CLIMATE_SOURCE_NAME,
            "endpoint": CLIMATE_SOURCE_ENDPOINT,
            "community": NASA_COMMUNITY,
            "api_version": current["metadata"].get("api_version"),
            "time_standard": current["metadata"].get("time_standard") or "LST",
            "data_nature": (
                "Reanálise/modelo (MERRA-2 para meteorologia; SRB/CERES/FLASHFlux "
                "para radiação) em grade global ~0,5°. NÃO é medição pontual do talhão."
            ),
            "nrt_lag_days": settings.nasa_power_nrt_lag_days,
            "sources": current["metadata"].get("sources"),
            "fill_value": current["metadata"].get("fill_value"),
            "site_elevation_m": current["metadata"].get("site_elevation_m"),
            "parameters": {
                p: {"longname": meta["longname"], "units": meta["units"], "nature": meta["nature"]}
                for p, meta in NASA_PARAMETERS.items()
            },
        },
        "metrics": metrics,
        "baseline": baseline,
        "daily": {
            "dates": dates,
            "precip_mm": precip_series,
            "temp_mean_c": [values["T2M"][d] for d in dates],
            "temp_max_c": tmax_series,
            "temp_min_c": tmin_series,
            "humidity_pct": hum_series,
            "wind_ms": wind_series,
            "radiation_mj_m2": rad_series,
            "baseline_precip_mm": (baseline or {}).get("daily_profile", {}).get("precip_mm", []),
            "baseline_temp_mean_c": (baseline or {}).get("daily_profile", {}).get("temp_mean_c", []),
        },
        "indicators": indicators,
        "interpretations": interpretations,
        "confidence": confidence,
        "data_quality": {
            "coverage_pct": confidence["coverage_pct"],
            "missing_days": missing_days[:60],
            "missing_days_total": len(missing_days),
            "fill_value": FILL_VALUE,
            "note": "Valores ausentes na fonte (fill −999.0) aparecem como null e NÃO são imputados.",
        },
        "disclaimer": (
            "Dados de reanálise/modelo (não medição pontual). As interpretações são "
            "conservadoras e NÃO substituem avaliação agronômica em campo. Ausência "
            "de correlação temporal com outros dados (ex.: Sentinel) não é analisada "
            "nesta camada."
        ),
        "fetched_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    return report
