"""
Agrometeorologia via NASA POWER (série diária de 12 meses).

Melhorias desta refatoração:
- Cache in-memory com TTL (thread-safe) — evita disparar a mesma
  requisição de 365 dias à NASA a cada abertura de página.
- `logging` estruturado no lugar de `print`.
- Timeout configurável via `config.settings`.
"""
import datetime
import logging
import threading
import time

import requests

from config import settings

logger = logging.getLogger("orion.weather")

# ---------------------------------------------------------------------------
# Cache in-memory: (lat, lon) -> (timestamp_monotonic, payload)
# ---------------------------------------------------------------------------
_cache: dict[tuple[float, float], tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_MAX_CACHE_ENTRIES = 64


def _cache_get(key: tuple[float, float]):
    with _cache_lock:
        item = _cache.get(key)
        if item is not None:
            age_s = time.monotonic() - item[0]
            if age_s < settings.nasa_weather_cache_minutes * 60:
                return item[1]
            _cache.pop(key, None)  # TTL expirado
    return None


def _cache_set(key: tuple[float, float], payload: dict) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), payload)
        if len(_cache) > _MAX_CACHE_ENTRIES:
            oldest_key = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest_key, None)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def fetch_live_nasa_weather(lat: float, lon: float) -> dict:
    """
    Busca a série agrometeorológica diária da NASA POWER considerando
    a defasagem de 3 dias de processamento e filtrando sentinelas -999.

    A resposta (dados reais OU fallback) fica em cache pelo TTL definido
    em `settings.nasa_weather_cache_minutes`, evitando requisições
    repetidas para as mesmas coordenadas.
    """
    key = (round(lat, 3), round(lon, 3))
    cached = _cache_get(key)
    if cached is not None:
        logger.info("NASA POWER: cache hit (%.3f, %.3f)", *key)
        return cached

    try:
        payload = _fetch_and_process(lat, lon)
        logger.info("NASA POWER: série ao vivo processada (%.3f, %.3f)", *key)
    except Exception:  # rede/timeout/payload malformado -> fallback conhecido
        logger.exception("NASA POWER indisponível (%.3f, %.3f); usando fallback.", *key)
        payload = generate_fallback_weather(lat, lon)

    _cache_set(key, payload)
    return payload


# ---------------------------------------------------------------------------
# Consulta e processamento
# ---------------------------------------------------------------------------
def _fetch_and_process(lat: float, lon: float) -> dict:
    # A NASA POWER tem delay de 3 dias para consolidar os dados
    end_date = datetime.date.today() - datetime.timedelta(days=3)
    start_date = end_date - datetime.timedelta(days=365)

    url = (
        "https://power.larc.nasa.gov/api/temporal/daily/point"
        f"?parameters=T2M,T2M_MAX,T2M_MIN,PRECTOTCORR,RH2M,WS2M,ALLSKY_SFC_SW_DWN"
        f"&community=AG"
        f"&longitude={lon}&latitude={lat}"
        f"&start={start_date.strftime('%Y%m%d')}&end={end_date.strftime('%Y%m%d')}"
        f"&format=JSON"
    )

    response = requests.get(url, timeout=settings.nasa_power_timeout_s)
    if response.status_code != 200:
        raise RuntimeError(f"NASA POWER respondeu HTTP {response.status_code}")

    data = response.json().get("properties", {}).get("parameter", {})

    t2m = data.get("T2M", {})
    t2m_max = data.get("T2M_MAX", {})
    t2m_min = data.get("T2M_MIN", {})
    prec = data.get("PRECTOTCORR", {})
    rh = data.get("RH2M", {})
    ws = data.get("WS2M", {})

    daily_records = []
    monthly_rain = {}
    monthly_temp_max = {}
    monthly_temp_min = {}
    monthly_temp_avg = {}
    total_rain = 0.0
    valid_temps = []

    for date_key in sorted(t2m.keys()):
        temp = float(t2m[date_key])
        # Ignora dias com sentinela de erro da NASA
        if temp <= -900:
            continue

        t_max = float(t2m_max.get(date_key, temp))
        if t_max <= -900:
            t_max = temp

        t_min = float(t2m_min.get(date_key, temp))
        if t_min <= -900:
            t_min = temp

        p_val = float(prec.get(date_key, 0.0))
        rain = max(0.0, p_val) if p_val > -900 else 0.0

        rh_val = float(rh.get(date_key, 70.0))
        humidity = max(10.0, min(100.0, rh_val)) if rh_val > -900 else 70.0

        ws_val = float(ws.get(date_key, 2.0))
        wind_kmh = round(max(0.0, ws_val) * 3.6, 1) if ws_val > -900 else 6.0

        total_rain += rain
        valid_temps.append(temp)

        # Agrupamento Mensal
        month_label = f"{date_key[4:6]}/{date_key[2:4]}"
        monthly_rain[month_label] = monthly_rain.get(month_label, 0.0) + rain
        monthly_temp_max[month_label] = max(monthly_temp_max.get(month_label, -99.0), t_max)
        monthly_temp_min[month_label] = min(monthly_temp_min.get(month_label, 99.0), t_min)

        if month_label not in monthly_temp_avg:
            monthly_temp_avg[month_label] = []
        monthly_temp_avg[month_label].append(temp)

        # Avaliação de Pulverização
        if rain > 1.0:
            status, badge, reason = "INAPTO", "bad", f"Chuva acumulada de {rain:.1f} mm"
        elif wind_kmh > 10.0:
            status, badge, reason = "ATENÇÃO", "warn", f"Risco de deriva (Vento: {wind_kmh} km/h)"
        elif humidity < 50.0 or temp > 33.0:
            status, badge, reason = "ATENÇÃO", "warn", "Evaporação rápida de gotas"
        else:
            status, badge, reason = "FAVORÁVEL", "ok", "Condições ideais de aplicação"

        dt_formatted = f"{date_key[6:8]}/{date_key[4:6]}/{date_key[0:4]}"
        daily_records.append({
            "date": dt_formatted,
            "temp": f"{temp:.1f} °C",
            "rain": f"{rain:.1f} mm",
            "hum": f"{humidity:.1f} %",
            "wind": f"{wind_kmh:.1f} km/h",
            "status": badge,
            "label": status,
            "reason": reason
        })

    labels = list(monthly_rain.keys())
    rain_data = [round(monthly_rain[m], 1) for m in labels]
    temp_max_data = [round(monthly_temp_max[m], 1) for m in labels]
    temp_min_data = [round(monthly_temp_min[m], 1) for m in labels]
    temp_avg_data = [round(sum(monthly_temp_avg[m]) / len(monthly_temp_avg[m]), 1) for m in labels]

    return {
        "summary": {
            "total_rain_mm": round(total_rain, 1),
            "avg_temp_c": round(sum(valid_temps) / len(valid_temps), 1) if valid_temps else 23.0,
            "min_temp_c": round(min(valid_temps), 1) if valid_temps else 15.0,
            "max_temp_c": round(max(valid_temps), 1) if valid_temps else 35.0
        },
        "monthly": {
            "labels": labels,
            "rain": rain_data,
            "temp_max": temp_max_data,
            "temp_min": temp_min_data,
            "temp_avg": temp_avg_data
        },
        "recent_applications": daily_records[-14:]
    }


def generate_fallback_weather(lat: float, lon: float) -> dict:
    """Clima climatológico aproximado (região S. do PR) quando a NASA está fora."""
    months = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    return {
        "summary": {
            "total_rain_mm": 1580.0,
            "avg_temp_c": 25.2,
            "min_temp_c": 16.5,
            "max_temp_c": 36.0
        },
        "monthly": {
            "labels": months,
            "rain": [220.0, 180.0, 140.0, 85.0, 40.0, 25.0, 20.0, 30.0, 65.0, 140.0, 175.0, 230.0],
            "temp_max": [33.0, 33.5, 32.0, 30.0, 28.0, 26.5, 27.0, 29.5, 31.5, 32.0, 32.5, 33.0],
            "temp_min": [22.0, 22.0, 21.0, 19.0, 16.0, 14.0, 14.5, 16.0, 18.5, 20.0, 21.0, 21.5],
            "temp_avg": [27.0, 27.2, 26.0, 24.0, 21.5, 19.8, 20.2, 22.5, 24.5, 25.8, 26.5, 27.0]
        },
        "recent_applications": []
    }
