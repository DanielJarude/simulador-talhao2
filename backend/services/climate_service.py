"""
Serviço climático consolidado — NASA POWER (PR #7 · Clima & Inteligência Agronômica).

Decisões de projeto (detalhamento em AUDITORIA_CLIMA_PR7.md):

- O BACKEND é o único componente que fala com a NASA POWER. O frontend
  consome somente ``/api/climate/farm/{farm_id}``.
- A localização é SEMPRE a canônica da fazenda (PR #6: geometria > ponto
  confirmado > legado). Este serviço recebe (lat, lon) já resolvidas e as
  valida; não existe "escolher cidade" nem coordenada fixa primária.
- Lacuna NUNCA é imputada: o fill value da fonte (-999.0) vira ``None`` e é
  contado na cobertura. DADO AUSENTE NÃO É ZERO e NÃO SIGNIFICA "SEM CHUVA".
- NÃO há fallback silencioso: falha da fonte (timeout/HTTP/parse) levanta
  ``ClimateSourceError`` e o endpoint devolve um estado explícito de
  indisponibilidade — nunca dado demonstrativo no lugar de dado real.
- Categorias de informação separadas e rotuladas (Fase 5):
  * ``dado_observado`` — valores da fonte (reanálise/modelo, NÃO medição);
  * ``dado_calculado`` — agregações/indicadores derivados (fórmula declarada
    e dias efetivamente utilizados declarados);
  * ``interpretacao`` — texto CONSERVADOR (nunca causalidade, nunca diagnóstico).
- Evapotranspiração (ET) NÃO é calculada: a Daily API AG não entrega ET e
  este PR não inventa metodologia (Fase 3).

============================================================================
DEFINIÇÃO FORMAL DE COBERTURA (FIX.1 — inequívoca, três conceitos distintos)
============================================================================
1. COBERTURA GERAL (``overall``): dias do período com o conjunto MÍNIMO de
   variáveis necessário para a análise geral — definido como
   TEMPERATURA MÉDIA (T2M) + PRECIPITAÇÃO (PRECTOTCORR), as variáveis que
   definem as "condições do período" (métricas de destaque e gráficos).
2. COBERTURA POR VARIÁVEL (``by_metric``): dias com dados de cada variável
   individualmente (precipitação, temperatura, umidade, radiação, vento).
3. DIAS COMPLETOS (``complete_days``): dias nos quais TODAS as 7 variáveis
   solicitadas estão simultaneamente disponíveis.

Os três conceitos são devolvidos EXPLICITAMENTE em ``coverage`` — nunca
sintetizados num único número genérico. Agregações declaram ``available_days``
(os dias efetivamente utilizados) e, quando o período tem lacunas, o
indicador/interpretação diz qual cálculo foi omitido e por quê.

============================================================================
BASELINE COM LACUNAS (metodologia A+B combinada — documentada)
============================================================================
- A comparação atual × referência histórica é feita APENAS sobre as datas em
  que a variável ATUAL possui observação (mesmas posições da janela no
  calendário, média dos anos do baseline) — nunca "período parcial × baseline
  completo" como se fossem equivalentes.
- Se a cobertura da variável for < 50% da janela, a comparação é OMITIDA
  (desvio = null + razão explícita): com menos da metade da janela, a
  comparação sobre os dias observados perde representatividade.
- Baseline em si: média dos N anos anteriores (padrão 5, máx. 10) para a
  mesma janela do calendário; anos inválidos são excluídos e contabilizados.
  Rótulo: "referência histórica" — explicitamente NÃO "normal climatológica
  oficial" (exige 30 anos por norma WMO).

============================================================================
CONFIANÇA (por métrica e geral — critérios objetivos)
============================================================================
- POR VARIÁVEL (usada nas interpretações que a utilizam):
    alta      = 100% dos dias do período com dados da variável;
    média     = 75–99,9%;
    limitada  = < 75%.
  Se a interpretação usa comparação com a referência histórica e o baseline
  tem < 3 anos válidos, o nível é rebaixado um degrau (alta→média→limitada).
- GERAL (escopo da análise completa): baseada nos DIAS COMPLETOS (todas as
  variáveis) + anos válidos do baseline:
    alta      = ≥ 98% de dias completos e ≥ 3 anos de referência;
    média     = 90–98% ou (≥ 98% com 1–2 anos de referência);
    limitada  = < 90% ou sem anos válidos de referência.
  A UI exibe a confiança GERAL e, em cada interpretação, a confiança da
  variável que sustenta aquele texto (e o porquê em ``confidence_basis``).

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

# ---------------------------------------------------------------------------
# Cobertura (FIX.1) — mapeamento variável canônica -> parâmetros da fonte.
# COBERTURA GERAL (overall): conjunto MÍNIMO p/ análise geral =
# temperatura média (T2M) + precipitação (PRECTOTCORR).
# ---------------------------------------------------------------------------
METRIC_VARIABLES: dict[str, list[str]] = {
    "precipitation": ["PRECTOTCORR"],
    "temperature": ["T2M"],
    "humidity": ["RH2M"],
    "radiation": ["ALLSKY_SFC_SW_DWN"],
    "wind": ["WS2M"],
}
CORE_VARIABLES: tuple[str, ...] = ("T2M", "PRECTOTCORR")

#: Abaixo desta fração de cobertura da variável, a comparação com o baseline
# é OMITIDA (metodologia B; acima, metodologia A — só dias com observação).
BASELINE_MIN_COVERAGE = 0.5

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
          "metadata": {...}, "coordinates": {...}, "period": {...},
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
# Estatísticas (nunca imputa: None sai da estatística e conta em cobertura)
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


def _count_valid(series: list[Optional[float]]) -> int:
    return sum(1 for v in series if v is not None)


def _longest_dry_streak(precip: list[Optional[float]], threshold_mm: float) -> int:
    """Maior sequência consecutiva de dias SEM precipitação relevante.

    DIA AUSENTE (None) INTERROMPE a contagem: dado ausente não é zero e não
    significa "sem chuva" — uma sequência só pode continuar através de dias
    cuja precipitação seja conhecida.
    """
    best = cur = 0
    for v in precip:
        if v is not None and v < threshold_mm:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


# ---------------------------------------------------------------------------
# COBERTURA (FIX.1) — três conceitos formais e distintos
# ---------------------------------------------------------------------------
def build_coverage(
    values: dict[str, dict[str, Optional[float]]], dates: list[str]
) -> dict:
    """
    Calcula os três conceitos de cobertura de forma EXPLÍCITA:

    - ``overall``: dias com o conjunto MÍNIMO para a análise geral
      (T2M + PRECTOTCORR — variáveis que definem as condições do período);
    - ``by_metric``: dias com dados de cada variável individual;
    - ``complete_days``: dias com TODAS as 7 variáveis simultaneamente.

    Nada aqui é "imputado": dia sem dado é simplesmente não contado.
    """
    total = len(dates)
    if total == 0:
        empty = {"available_days": 0, "pct": 0.0}
        return {
            "requested_days": 0,
            "overall_days": 0,
            "overall_pct": 0.0,
            "overall_definition": "dias com temperatura média (T2M) + precipitação (PRECTOTCORR)",
            "complete_days": 0,
            "complete_days_pct": 0.0,
            "by_metric": {k: dict(empty) for k in METRIC_VARIABLES},
        }

    def _days_with(params: list[str]) -> int:
        return sum(
            1 for d in dates if all(values[p].get(d) is not None for p in params)
        )

    overall_days = _days_with(list(CORE_VARIABLES))
    complete_days = _days_with(list(NASA_PARAMETERS))
    by_metric = {
        m: {
            "available_days": _days_with(params),
            "pct": round(_days_with(params) / total * 100.0, 1),
        }
        for m, params in METRIC_VARIABLES.items()
    }
    return {
        "requested_days": total,
        "overall_days": overall_days,
        "overall_pct": round(overall_days / total * 100.0, 1),
        "overall_definition": "dias com o conjunto mínimo para a análise geral: temperatura média (T2M) + precipitação (PRECTOTCORR)",
        "complete_days": complete_days,
        "complete_days_pct": round(complete_days / total * 100.0, 1),
        "by_metric": by_metric,
    }


def _availability(series: list[Optional[float]]) -> dict:
    n = len(series)
    avail = _count_valid(series)
    return {
        "available_days": avail,
        "requested_days": n,
        "complete": avail == n and n > 0,
    }


# ---------------------------------------------------------------------------
# Agregações por métrica (período atual) — SEMPRE declaram os dias usados
# ---------------------------------------------------------------------------
def _aggregate_precipitation(series: list[Optional[float]]) -> dict:
    valid = [v for v in series if v is not None]
    threshold = settings.nasa_climate_rainy_day_threshold_mm
    available = _count_valid(series)
    total = len(series)
    out: dict[str, Any] = {
        "category": CATEGORY_CALC,
        "unit": "mm",
        "accumulated_mm": round(sum(valid), 1) if valid else None,
        #: FIX.1 — o acumulado é SOMENTE dos dias com dados; a UI deve dizer
        #: "X mm nos N dias com dados disponíveis" quando N < total.
        "accumulated_over_days": available if valid else 0,
        "daily_mean_mm": round(sum(valid) / len(valid), 2) if valid else None,
        "rainy_days": sum(1 for v in valid if v >= threshold),
        "rainy_day_threshold_mm": threshold,
        **_availability(series),
    }
    # FIX.2 — sequência seca: com lacunas, NÃO pode ser afirmada sobre o
    # período (o valor calculado seria apenas um limite inferior).
    if available == total:
        out["longest_dry_streak_days"] = _longest_dry_streak(series, threshold)
        out["longest_dry_streak_reason"] = None
    else:
        out["longest_dry_streak_days"] = None
        out["longest_dry_streak_reason"] = (
            f"precipitação ausente em {total - available} de {total} dias — "
            "a maior sequência seca não pode ser determinada com segurança "
            "(dia sem dados não pode ser tratado como dia sem chuva)"
        )
    return out


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
        # disponibilidade da série principal (T2M); mín/máx/amplitude são
        # calculadas cada uma sobre os pares de dias disponíveis (null se não houver).
        **_availability(t),
    }


def _aggregate_radiation(series: list[Optional[float]]) -> dict:
    valid = [v for v in series if v is not None]
    return {
        "category": CATEGORY_CALC,
        "unit": "MJ/m^2/day",
        "daily_mean_mj_m2": round(sum(valid) / len(valid), 2) if valid else None,
        "accumulated_mj_m2": round(sum(valid), 1) if valid else None,
        **_availability(series),
        "note": "A unidade MJ/m²/dia da fonte AG já é o acumulado do dia; a média é a agregação primária.",
    }


def _aggregate_humidity(series: list[Optional[float]]) -> dict:
    return {
        "category": CATEGORY_CALC,
        "unit": "%",
        "mean_pct": _stat_mean(series),
        "min_pct": _stat_min(series),
        "max_pct": _stat_max(series),
        **_availability(series),
    }


def _aggregate_wind(series: list[Optional[float]]) -> dict:
    valid = [v for v in series if v is not None]
    return {
        "category": CATEGORY_CALC,
        "unit": "m/s (média diária da fonte); km/h = m/s × 3,6",
        "mean_ms": _stat_mean(series),
        "mean_kmh": round(sum(valid) / len(valid) * 3.6, 1) if valid else None,
        "max_daily_mean_kmh": round(max(valid) * 3.6, 1) if valid else None,
        **_availability(series),
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
                "radiation_mj_m2": [data["values"]["ALLSKY_SFC_SW_DWN"][d] for d in data["dates"]],
            }
        )
        valid_years += 1

    def _ref(metric: str) -> Optional[float]:
        vals = [y[metric] for y in years.values() if y[metric] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    # Perfil diário de referência = média elementar dos anos válidos
    # (mesma posição da janela em todos os anos — garantido por _baseline_window).
    profile: dict[str, list[Optional[float]]] = {
        "precip_mm": [], "temp_mean_c": [], "radiation_mj_m2": []
    }
    if profiles:
        for i in range(window_days):
            profile["precip_mm"].append(_stat_mean([p["precip_mm"][i] for p in profiles]))
            profile["temp_mean_c"].append(_stat_mean([p["temp_mean_c"][i] for p in profiles]))
            profile["radiation_mj_m2"].append(
                _stat_mean([p["radiation_mj_m2"][i] for p in profiles])
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
            "NÃO é 'normal climatológica oficial' (exige 30 anos por norma WMO). "
            "Quando o período atual tem lacunas, a comparação é feita apenas sobre "
            "as datas com observação atual (mesmas posições da janela); se a "
            "cobertura da variável for inferior a 50%, a comparação é omitida."
        ),
    }


def _classify(deviation_pct: Optional[float]) -> tuple[str, str]:
    band = settings.nasa_climate_baseline_band_pct
    if deviation_pct is None:
        return "proxima_da_referencia", "próxima da referência histórica"
    if deviation_pct <= -band:
        return "abaixo_da_referencia", "abaixo da referência histórica"
    if deviation_pct >= band:
        return "acima_da_referencia", "acima da referência histórica"
    return "proxima_da_referencia", "próxima da referência histórica"


def _metric_baseline_comparison(
    metric_key: str,
    current_series: list[Optional[float]],
    profile_series: Optional[list[Optional[float]]],
    baseline: Optional[dict],
) -> dict:
    """
    FIX.4 — comparação atual × referência histórica com lacunas (metodologia
    A+B, documentada no cabeçalho do módulo):

    A) compara APENAS sobre as datas em que a variável atual tem observação
       (a referência é a média dos anos do baseline nas mesmas posições);
    B) se a cobertura atual for < 50% da janela, OMITE o desvio (null + razão)
       — nunca "período parcial × baseline completo" como equivalentes.
    """
    total = len(current_series)
    known_idx = [i for i, v in enumerate(current_series) if v is not None]
    out: dict[str, Any] = {
        "compared": False,
        "compared_days": len(known_idx),
        "total_days": total,
        "current_value": None,
        "reference_value": None,
        "deviation": None,
        "deviation_pct": None,
        "classification": None,
        "classification_label": None,
        "reference_years": baseline["years"] if baseline else 0,
        "reason": None,
    }
    if metric_key not in ("precipitation", "temperature", "radiation"):
        out["reason"] = "sem referência histórica para esta variável nesta camada"
        return out
    if baseline is None or baseline.get("years", 0) < 1:
        out["reason"] = "sem anos válidos na referência histórica"
        return out
    if total == 0 or not known_idx:
        out["reason"] = "sem observações atuais da variável"
        return out
    if len(known_idx) / total < BASELINE_MIN_COVERAGE:
        out["reason"] = (
            f"cobertura atual insuficiente ({len(known_idx)} de {total} dias, "
            f"< {BASELINE_MIN_COVERAGE:.0%}) — comparação omitida para não "
            "tratar período parcial como equivalente ao baseline completo"
        )
        return out
    if profile_series is None or len(profile_series) != total:
        out["reason"] = "perfil diário de referência indisponível"
        return out

    if metric_key == "precipitation":
        # acumulado sobre os dias conhecidos (atual) vs soma da média histórica
        # nas mesmas posições
        current_value = round(sum(current_series[i] for i in known_idx), 1)
        ref_vals = [profile_series[i] for i in known_idx if profile_series[i] is not None]
        reference_value = round(sum(ref_vals), 1) if ref_vals else None
    else:
        # média sobre os dias conhecidos (atual) vs média da média histórica
        # nas mesmas posições
        current_value = round(
            sum(current_series[i] for i in known_idx) / len(known_idx), 2
        )
        ref_vals = [profile_series[i] for i in known_idx if profile_series[i] is not None]
        reference_value = round(sum(ref_vals) / len(ref_vals), 2) if ref_vals else None

    if reference_value is None:
        out["reason"] = "referência histórica sem dados nas posições comparadas"
        return out

    deviation = round(current_value - reference_value, 2)
    deviation_pct = (
        round((deviation / reference_value) * 100.0, 1) if reference_value else None
    )
    cls, label = _classify(deviation_pct)
    out.update(
        compared=True,
        current_value=current_value,
        reference_value=reference_value,
        deviation=deviation,
        deviation_pct=deviation_pct,
        classification=cls,
        classification_label=label,
        reason=None,
    )
    return out


# ---------------------------------------------------------------------------
# Confiância (Fase 8 / FIX.5) — critérios objetivos, documentados na resposta
# ---------------------------------------------------------------------------
def _metric_confidence_level(pct: float) -> str:
    """Confiança POR VARIÁVEL (usada nas interpretações que a utilizam)."""
    if pct >= 100.0:
        return "alta"
    if pct >= 75.0:
        return "media"
    return "limitada"


def _interp_confidence(
    metric_pct: float, uses_baseline: bool, valid_years: int
) -> tuple[str, str]:
    level = _metric_confidence_level(metric_pct)
    reasons = [f"cobertura da variável: {metric_pct:.1f}%"]
    if uses_baseline:
        if valid_years < 3:
            level = {"alta": "media", "media": "limitada", "limitada": "limitada"}[level]
            reasons.append(f"referência com apenas {valid_years} ano(s) válido(s)")
        else:
            reasons.append(f"referência com {valid_years} anos válidos")
    return level, "; ".join(reasons)


def _assess_confidence(
    coverage: dict, baseline: Optional[dict]
) -> dict:
    """Confiança GERAL (escopo da análise completa) — dias COMPLETOS + baseline."""
    complete_pct = coverage["complete_days_pct"]
    valid_years = baseline["years"] if baseline else None
    reasons: list[str] = []
    if complete_pct < 90.0:
        level = "limitada"
        reasons.append(f"apenas {coverage['complete_days']} de {coverage['requested_days']} dias com todas as variáveis ({complete_pct:.1f}%)")
    elif complete_pct < 98.0:
        level = "media"
        reasons.append(f"{coverage['complete_days']} de {coverage['requested_days']} dias completos ({complete_pct:.1f}%)")
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
        reasons.append("dias com todas as variáveis ≥ 98% e referência com ≥ 3 anos válidos")
    by_metric = {
        m: _metric_confidence_level(info["pct"])
        for m, info in coverage["by_metric"].items()
    }
    return {
        "scope": "analise_geral",
        "level": level,
        "complete_days_pct": complete_pct,
        "valid_baseline_years": valid_years,
        "by_metric": by_metric,
        "criteria": (
            "GERAL (escopo completo, dias com TODAS as variáveis): Alta = ≥98% de dias "
            "completos e referência com ≥3 anos válidos; Média = 90–98% ou 1–2 anos de "
            "referência; Limitada = <90% ou sem anos válidos. POR VARIÁVEL (cada "
            "interpretação): Alta = 100% dos dias da variável; Média = 75–99,9%; "
            "Limitada = <75%; se a interpretação usa a referência histórica com <3 "
            "anos válidos, o nível é rebaixado um degrau."
        ),
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Interpretações CONSERVADORAS (Fases 5/18) — nunca causalidade
# ---------------------------------------------------------------------------
def _interpretations(
    coverage: dict,
    metrics: dict,
    baseline: Optional[dict],
) -> list[dict]:
    out: list[dict] = []
    valid_years = baseline["years"] if baseline else 0

    def _pct(metric_key: str) -> float:
        return coverage["by_metric"][metric_key]["pct"]

    def _add(
        id_: str, topic: str, text: str,
        metric_key: Optional[str], uses_baseline: bool, basis: dict,
    ) -> None:
        if metric_key is None:
            level = "limitada"
            basis_text = "dados do período"
        else:
            level, basis_text = _interp_confidence(
                _pct(metric_key), uses_baseline, valid_years
            )
        out.append(
            {
                "id": id_,
                "topic": topic,
                "category": CATEGORY_INTERP,
                "text": text,
                "confidence": level,
                "confidence_basis": basis_text,
                "basis": basis,
            }
        )

    prec = metrics["precipitation"]
    temp = metrics["temperature"]
    rad = metrics["radiation"]
    hum = metrics["humidity"]
    wind = metrics["wind"]
    p_cov = prec["available_days"]
    p_total = prec["requested_days"]
    t_cov = temp["available_days"]
    t_total = temp["requested_days"]
    r_cov = rad["available_days"]
    r_total = rad["requested_days"]

    # ------------------------- PRECIPITAÇÃO -------------------------
    if prec["accumulated_mm"] is None:
        _add(
            "precip", "Precipitação",
            "Não há dados de precipitação para este período.",
            "precipitation", False, {"accumulated_mm": None},
        )
    else:
        b = prec["baseline"]
        partial = f"nos {p_cov} dias com dados disponíveis" if p_cov < p_total else "no período"
        if not b["compared"]:
            text = (
                f"Precipitação acumulada de {prec['accumulated_mm']} mm {partial} "
                f"({p_cov} de {p_total} dias). Comparação com a referência histórica "
                f"omitida: {b['reason']}. Não há evidência suficiente para classificar "
                "o período como mais seco ou mais úmido do que o histórico."
            )
        else:
            delta = b["deviation_pct"]
            sign = "+" if (delta or 0) > 0 else ""
            if b["classification"] == "abaixo_da_referencia":
                verdict = (
                    f"{sign}{delta}% em relação à média histórica dos mesmos dias "
                    f"({b['reference_value']} mm). Os dados indicam período mais seco "
                    "do que a referência. É compatível com atenção ao manejo hídrico, "
                    "mas não há evidência suficiente para afirmar estresse das "
                    "culturas sem outras fontes."
                )
            elif b["classification"] == "acima_da_referencia":
                verdict = (
                    f"{sign}{delta}% em relação à média histórica dos mesmos dias "
                    f"({b['reference_value']} mm). É compatível com condição mais "
                    "úmida do que a referência; merece acompanhamento (ex.: "
                    "logística de campo e aplicação)."
                )
            else:
                verdict = (
                    f"{sign}{delta}% em relação à média histórica dos mesmos dias "
                    f"({b['reference_value']} mm) — dentro da banda de ±"
                    f"{settings.nasa_climate_baseline_band_pct:g}%."
                )
            text = (
                f"Precipitação acumulada de {prec['accumulated_mm']} mm {partial} "
                f"— {verdict}"
            )
        _add(
            "precip", "Precipitação", text, "precipitation", b["compared"],
            {"accumulated_mm": prec["accumulated_mm"], "available_days": p_cov,
             "total_days": p_total, "reference_value": b["reference_value"],
             "deviation_pct": b["deviation_pct"]},
        )

    # ------------------------- TEMPERATURA -------------------------
    if temp["mean_c"] is not None:
        b = temp["baseline"]
        partial = f"nos {t_cov} dias com dados" if t_cov < t_total else "no período"
        if not b["compared"]:
            compare_txt = (
                f"Comparação com a referência omitida: {b['reason']}."
            )
        else:
            d = b["deviation"] or 0.0
            if d < 0:
                compare_txt = (
                    f"Média {abs(d)} °C abaixo da referência histórica dos mesmos "
                    f"dias ({b['reference_value']} °C). Os dados indicam período "
                    "mais frio do que a referência."
                )
            elif d > 0:
                compare_txt = (
                    f"Média {d} °C acima da referência histórica dos mesmos dias "
                    f"({b['reference_value']} °C). Os dados indicam período mais "
                    "quente do que a referência."
                )
            else:
                compare_txt = "Média alinhada à referência histórica dos mesmos dias."
        amp = (
            f" Amplitude térmica diária média de {temp['amplitude_mean_c']} °C."
            if temp["amplitude_mean_c"] is not None else ""
        )
        _add(
            "temp", "Temperatura",
            f"Temperatura média de {temp['mean_c']} °C {partial} (mín. "
            f"{temp['min_c']} °C / máx. {temp['max_c']} °C). {compare_txt}{amp}",
            "temperature", b["compared"],
            {"mean_c": temp["mean_c"], "available_days": t_cov, "total_days": t_total,
             "reference_value": b["reference_value"], "deviation": b["deviation"]},
        )

    # ------------------------- RADIAÇÃO -------------------------
    if rad["daily_mean_mj_m2"] is not None:
        b = rad["baseline"]
        partial = f"nos {r_cov} dias com dados" if r_cov < r_total else "no período"
        if b["compared"]:
            delta = b["deviation_pct"]
            sign = "+" if (delta or 0) > 0 else ""
            compare_txt = (
                f"Desvio de {sign}{delta}% em relação à média histórica dos mesmos "
                f"dias ({b['reference_value']} MJ/m²/dia). Os dados apenas descrevem "
                "disponibilidade de radiação; não permitem conclusão sobre "
                "fotossíntese sem outras evidências."
            )
        else:
            compare_txt = f"Comparação com a referência omitida: {b['reason']}."
        _add(
            "radiation", "Radiação",
            f"Radiação solar diária média de {rad['daily_mean_mj_m2']} MJ/m² {partial} "
            f"(acumulado de {rad['accumulated_mj_m2']} MJ/m² nos dias com dados). "
            f"{compare_txt}",
            "radiation", b["compared"],
            {"daily_mean_mj_m2": rad["daily_mean_mj_m2"], "available_days": r_cov,
             "total_days": r_total, "reference_value": b["reference_value"],
             "deviation_pct": b["deviation_pct"]},
        )

    # ------------------------- UMIDADE / VENTO -------------------------
    if hum["mean_pct"] is not None:
        partial = f"nos {hum['available_days']} dias com dados" if not hum["complete"] else "no período"
        _add(
            "humidity", "Umidade",
            f"Umidade relativa média de {hum['mean_pct']}% {partial} (extremos: "
            f"{hum['min_pct']}% a {hum['max_pct']}%). Dado descritivo: por si só, "
            "não sustenta conclusões agronômicas.",
            "humidity", False,
            {"mean_pct": hum["mean_pct"], "min_pct": hum["min_pct"],
             "max_pct": hum["max_pct"], "available_days": hum["available_days"]},
        )
    if wind["mean_kmh"] is not None:
        partial = f"nos {wind['available_days']} dias com dados" if not wind["complete"] else "no período"
        _add(
            "wind", "Vento",
            f"Vento médio de {wind['mean_kmh']} km/h {partial} (maior média diária: "
            f"{wind['max_daily_mean_kmh']} km/h). A fonte fornece médias diárias — "
            "não há rajadas no período analisado.",
            "wind", False,
            {"mean_kmh": wind["mean_kmh"], "max_daily_mean_kmh": wind["max_daily_mean_kmh"],
             "available_days": wind["available_days"]},
        )

    # ------------------------- SEQUÊNCIA SECA (FIX.2) -------------------------
    if prec["longest_dry_streak_days"] is not None:
        dry = prec["longest_dry_streak_days"]
        if dry >= 7:
            _add(
                "dry_streak", "Sequência seca",
                (
                    f"Os dados indicam a maior sequência sem precipitação relevante "
                    f"(≥{settings.nasa_climate_rainy_day_threshold_mm:g} mm/dia) de "
                    f"{dry} dias — período completo com dados de precipitação. É "
                    "compatível com período seco; o significado agronômico depende "
                    "de cultura, estádio e solo — não há dados suficientes aqui "
                    "para afirmar estresse."
                ),
                "precipitation", False,
                {"longest_dry_streak_days": dry,
                 "threshold_mm": settings.nasa_climate_rainy_day_threshold_mm},
            )
    elif prec["accumulated_mm"] is not None and p_cov < p_total:
        _add(
            "dry_streak", "Sequência seca",
            (
                f"Não é possível determinar a maior sequência seca com segurança: "
                f"precipitação ausente em {p_total - p_cov} de {p_total} dias. Dado "
                "ausente não é dia sem chuva — há evidência limitada para esse "
                "indicador neste período."
            ),
            "precipitation", False,
            {"available_days": p_cov, "total_days": p_total},
        )

    # ------------------------- QUALIDADE DOS DADOS (FIX.1/6) -------------------------
    if coverage["complete_days"] < coverage["requested_days"]:
        bm = coverage["by_metric"]
        _add(
            "data_quality", "Qualidade dos dados",
            (
                f"Cobertura do período: {coverage['complete_days']} de "
                f"{coverage['requested_days']} dias com TODAS as variáveis "
                f"({coverage['complete_days_pct']}%). Por variável — Precipitação: "
                f"{bm['precipitation']['available_days']}/{coverage['requested_days']}; "
                f"Temperatura: {bm['temperature']['available_days']}/{coverage['requested_days']}; "
                f"Umidade: {bm['humidity']['available_days']}/{coverage['requested_days']}; "
                f"Radiação: {bm['radiation']['available_days']}/{coverage['requested_days']}; "
                f"Vento: {bm['wind']['available_days']}/{coverage['requested_days']}. "
                "Todos os cálculos usam apenas os dias com dados (nenhuma estimativa "
                "ou imputação); comparações com a referência histórica usam as "
                "mesmas datas com observação, ou são omitidas se a cobertura for "
                "insuficiente. Últimos dias da fonte ainda podem não estar "
                "consolidados (defasagem NRT)."
            ),
            None, False,
            {"coverage": {k: v for k, v in coverage.items() if k != "overall_definition"}},
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
    Pipeline completo: série diária → cobertura (3 conceitos) → agregações
    (com dias efetivamente usados) → baseline (comparação A+B) → indicadores
    → interpretações (confiança por variável) → confiança geral → proveniência.

    ``lat/lon`` devem vir da localização CANÔNICA da fazenda (PR #6) — o
    endpoint valida e documenta isso. Não há imputação nem fallback:
    falha de fonte = ``ClimateSourceError``; dado ausente = ``None``.
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

    # --- Cobertura: três conceitos formais e distintos (FIX.1) ---
    coverage = build_coverage(values, dates)

    precip_series = [values["PRECTOTCORR"][d] for d in dates]
    temp_series = [values["T2M"][d] for d in dates]
    tmax_series = [values["T2M_MAX"][d] for d in dates]
    tmin_series = [values["T2M_MIN"][d] for d in dates]
    rad_series = [values["ALLSKY_SFC_SW_DWN"][d] for d in dates]
    hum_series = [values["RH2M"][d] for d in dates]
    wind_series = [values["WS2M"][d] for d in dates]

    # --- Agregações (declaram available_days; nunca imputam) ---
    metrics: dict[str, dict] = {
        "precipitation": _aggregate_precipitation(precip_series),
        "temperature": _aggregate_temperature(temp_series, tmax_series, tmin_series),
        "radiation": _aggregate_radiation(rad_series),
        "humidity": _aggregate_humidity(hum_series),
        "wind": _aggregate_wind(wind_series),
    }

    # --- Baseline por métrica (metodologia A+B — FIX.4) ---
    profile = (baseline or {}).get("daily_profile", {})
    metrics["precipitation"]["baseline"] = _metric_baseline_comparison(
        "precipitation", precip_series, profile.get("precip_mm"), baseline
    )
    metrics["temperature"]["baseline"] = _metric_baseline_comparison(
        "temperature", temp_series, profile.get("temp_mean_c"), baseline
    )
    metrics["radiation"]["baseline"] = _metric_baseline_comparison(
        "radiation", rad_series, profile.get("radiation_mj_m2"), baseline
    )
    for key in ("humidity", "wind"):
        metrics[key]["baseline"] = _metric_baseline_comparison(
            key, hum_series if key == "humidity" else wind_series, None, baseline
        )

    # --- Indicadores derivados (valor null + razão quando omitido) ---
    omitted: list[str] = []
    for m, b in metrics.items():
        bbase = b.get("baseline") or {}
        if not bbase.get("compared") and bbase.get("reason") and m in ("precipitation", "temperature", "radiation"):
            omitted.append(f"desvio de {m} vs. referência histórica — {bbase['reason']}")
    if metrics["precipitation"]["longest_dry_streak_days"] is None:
        omitted.append(f"sequência seca máxima — {metrics['precipitation']['longest_dry_streak_reason']}")

    def _ind_value(key: str, field: str):
        return metrics[key][field]

    indicators = [
        {
            "id": "chuva_acumulada", "name": "Chuva acumulada no período",
            "value": _ind_value("precipitation", "accumulated_mm"), "unit": "mm",
            "category": CATEGORY_CALC,
            "over_days": _ind_value("precipitation", "accumulated_over_days"),
            "total_days": _ind_value("precipitation", "requested_days"),
            "formula": "Soma dos acumulados diários (PRECTOTCORR, mm/dia) SOMENTE dos dias com dados (nenhum dia ausente é estimado).",
        },
        {
            "id": "dias_com_chuva", "name": "Dias com precipitação relevante",
            "value": _ind_value("precipitation", "rainy_days"), "unit": "dias",
            "category": CATEGORY_CALC,
            "over_days": _ind_value("precipitation", "available_days"),
            "total_days": _ind_value("precipitation", "requested_days"),
            "formula": f"Contagem de dias com chuva ≥ {settings.nasa_climate_rainy_day_threshold_mm:g} mm, entre os dias com dados.",
        },
        {
            "id": "sequencia_seca", "name": "Maior sequência sem chuva relevante",
            "value": _ind_value("precipitation", "longest_dry_streak_days"), "unit": "dias",
            "category": CATEGORY_CALC,
            "reason": _ind_value("precipitation", "longest_dry_streak_reason"),
            "formula": "Maior sequência consecutiva de dias CONHECIDOS com chuva < threshold; dia sem dados interrompe a contagem. Se o período tem lacunas, o indicador é omitido (null) — dado ausente não é dia sem chuva.",
        },
        {
            "id": "amplitude_termica", "name": "Amplitude térmica diária média",
            "value": _ind_value("temperature", "amplitude_mean_c"), "unit": "C",
            "category": CATEGORY_CALC,
            "formula": "Média de (T2M_MAX − T2M_MIN) nos dias com ambos os valores.",
        },
        {
            "id": "desvio_chuva", "name": "Desvio de precipitação vs. referência histórica",
            "value": metrics["precipitation"]["baseline"].get("deviation"), "unit": "mm",
            "category": CATEGORY_CALC,
            "compared_days": metrics["precipitation"]["baseline"].get("compared_days"),
            "reason": metrics["precipitation"]["baseline"].get("reason"),
            "formula": "Acumulado dos dias com observação atual − média histórica das mesmas datas (N anos anteriores). Omitido se cobertura < 50%.",
        },
        {
            "id": "desvio_temperatura", "name": "Desvio de temperatura média vs. referência histórica",
            "value": metrics["temperature"]["baseline"].get("deviation"), "unit": "C",
            "category": CATEGORY_CALC,
            "compared_days": metrics["temperature"]["baseline"].get("compared_days"),
            "reason": metrics["temperature"]["baseline"].get("reason"),
            "formula": "Média dos dias com observação atual − média histórica das mesmas datas (N anos anteriores). Omitido se cobertura < 50%.",
        },
    ]

    interpretations = _interpretations(coverage, metrics, baseline)
    confidence = _assess_confidence(coverage, baseline)

    # --- Estados explícitos (Fase 9 / FIX.1) ---
    total_days = coverage["requested_days"]
    complete_days = coverage["complete_days"]
    overall_days = coverage["overall_days"]
    if total_days > 0 and overall_days == 0:
        status = "insufficient_data"
        message = "Não há dados suficientes para este período."
    elif complete_days < total_days:
        status = "partial"
        # FIX.6 — contagem explícida + percentual correto (ratio × 100).
        message = (
            f"Dados parciais: {complete_days} de {total_days} dias com dados "
            f"completos na fonte ({coverage['complete_days_pct']}%). "
            "Algumas métricas ainda não possuem dados para todos os dias do "
            "período — veja a cobertura por variável."
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
        # FIX.1 — cobertura com os três conceitos formais e distintos.
        "coverage": coverage,
        "daily": {
            "dates": dates,
            "precip_mm": precip_series,
            "temp_mean_c": temp_series,
            "temp_max_c": tmax_series,
            "temp_min_c": tmin_series,
            "humidity_pct": hum_series,
            "wind_ms": wind_series,
            "radiation_mj_m2": rad_series,
            "baseline_precip_mm": profile.get("precip_mm", []),
            "baseline_temp_mean_c": profile.get("temp_mean_c", []),
        },
        "indicators": indicators,
        "interpretations": interpretations,
        "confidence": confidence,
        "data_quality": {
            "requested_days": total_days,
            "complete_days": complete_days,
            "complete_days_pct": coverage["complete_days_pct"],
            "by_metric_days": {
                m: info["available_days"] for m, info in coverage["by_metric"].items()
            },
            "missing_days": missing_days[:60],
            "missing_days_total": len(missing_days),
            "omitted_calculations": omitted,
            "fill_value": FILL_VALUE,
            "note": (
                "Valores ausentes na fonte (fill −999.0) aparecem como null e NÃO "
                "são imputados. 'Dias completos' = dias com TODAS as variáveis; "
                "cada métrica também declara seus próprios dias disponíveis."
            ),
        },
        "disclaimer": (
            "Dados de reanálise/modelo (não medição pontual). As interpretações são "
            "conservadoras e NÃO substituem avaliação agronômica em campo. Ausência "
            "de dados NUNCA é tratada como condição meteorológica (ex.: dia sem "
            "dados de chuva NÃO é 'dia sem chuva')."
        ),
        "fetched_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    return report
