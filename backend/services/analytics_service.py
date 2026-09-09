import os
import numpy as np
from PIL import Image

from config import settings

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def extract_layer_stats(image_path: str, total_area_ha: float = 42.54):
    if not os.path.exists(image_path):
        return None

    img = Image.open(image_path).convert("RGBA")
    arr = np.array(img)

    valid_mask = (arr[:, :, 0] > 30) | (arr[:, :, 1] > 30) | (arr[:, :, 2] > 30)
    total_valid_pixels = np.sum(valid_mask)
    if total_valid_pixels == 0:
        return None

    red_channel = arr[:, :, 0][valid_mask].astype(float)
    green_channel = arr[:, :, 1][valid_mask].astype(float)
    blue_channel = arr[:, :, 2][valid_mask].astype(float)

    # Classificação MUTUAMENTE EXCLUSIVA (cada pixel entra em exatamente uma
    # zona), na ordem de prioridade stress → medium → dense → good (catch-all).
    # Assim os pcts somam 100 e o índice médio fica preso a [0, 1].
    r = red_channel
    g = green_channel
    b = blue_channel
    is_stress = (r > 180) & (g < 100)
    is_mid = (~is_stress) & (r > 150) & (g > 100) & (b < 50)
    is_dense = (~is_stress) & (~is_mid) & (b > 150)
    is_good = ~(is_stress | is_mid | is_dense)   # resto do talhão

    stress_pixels = np.sum(is_stress)
    mid_pixels = np.sum(is_mid)
    dense_pixels = np.sum(is_dense)
    good_pixels = np.sum(is_good)

    pct_stress = round((stress_pixels / total_valid_pixels) * 100, 1)
    pct_mid = round((mid_pixels / total_valid_pixels) * 100, 1)
    pct_good = round((good_pixels / total_valid_pixels) * 100, 1)
    pct_dense = round((dense_pixels / total_valid_pixels) * 100, 1)

    mean_val = round(((pct_stress * 0.25) + (pct_mid * 0.48) + (pct_good * 0.68) + (pct_dense * 0.88)) / 100.0, 2)

    return {
        "mean_index": mean_val,
        "zones": {
            "stress": {"pct": pct_stress, "ha": round((pct_stress / 100.0) * total_area_ha, 2)},
            "medium": {"pct": pct_mid, "ha": round((pct_mid / 100.0) * total_area_ha, 2)},
            "good": {"pct": pct_good, "ha": round((pct_good / 100.0) * total_area_ha, 2)},
            "dense": {"pct": pct_dense, "ha": round((pct_dense / 100.0) * total_area_ha, 2)}
        }
    }


def estimate_seasonal_yield(timeline: list, area_ha: float = 42.54):
    """
    Identifica as safras do ano com base nos ciclos do NDVI e calcula a produtividade.
    """
    # 1ª Safra: Verão (Outubro a Janeiro) -> Soja
    summer_dates = ["2025-10-04", "2025-10-11", "2025-11-18", "2025-11-20", "2025-12-10", "2025-12-18", "2026-01-27"]
    # 2ª Safra: Safrinha (Fevereiro a Junho) -> Milho Safrinha
    safrinha_dates = ["2025-04-07", "2025-04-22", "2025-05-02", "2025-06-11", "2026-02-11", "2026-03-08"]

    summer_points = [t["mean"] for t in timeline if t["date"] in summer_dates]
    safrinha_points = [t["mean"] for t in timeline if t["date"] in safrinha_dates]

    summer_peak = max(summer_points) if summer_points else 0.88
    safrinha_peak = max(safrinha_points) if safrinha_points else 0.72

    # Modelagem empírica calibrada
    # Soja: ~66 sc/ha no pico 0.88-0.90
    soja_yield = round(20.0 + (summer_peak * 53.0), 1)
    soja_total_bags = int(soja_yield * area_ha)
    soja_gross = round(soja_total_bags * 130.0, 2)

    # Milho Safrinha: ~90 sc/ha no pico 0.72-0.75
    milho_yield = round(25.0 + (safrinha_peak * 90.0), 1)
    milho_total_bags = int(milho_yield * area_ha)
    milho_gross = round(milho_total_bags * 60.0, 2)

    return {
        "safra_verao": {
            "cultura": "Soja",
            "janela": "Outubro a Janeiro",
            "pico_ndvi": summer_peak,
            "produtividade_sc_ha": soja_yield,
            "total_sacas": soja_total_bags,
            "preco_saca_brl": 130.0,
            "faturamento_bruto_brl": soja_gross
        },
        "safrinha": {
            "cultura": "Milho Safrinha",
            "janela": "Fevereiro a Junho",
            "pico_ndvi": safrinha_peak,
            "produtividade_sc_ha": milho_yield,
            "total_sacas": milho_total_bags,
            "preco_saca_brl": 60.0,
            "faturamento_bruto_brl": milho_gross
        },
        "total_anual_faturamento_brl": round(soja_gross + milho_gross, 2)
    }


def get_farm_temporal_series(
    farm_id: int,
    layer: str = "ndvi",
    total_area_ha: float = 42.54,
    talhao_id: int | None = None,
):
    """
    Série temporal de índices espectrais da fazenda.

    `talhao_id` é o ID REAL do talhão (a antiga versão usava `farm_id`
    duas vezes no nome da pasta — `farm_{id}_talhao_{id}` — e, em
    fazendas multi-talhão, caía em fallback incorreto).
    As datas vêm de `config.settings.sentinel_dates` (fonte única).
    """
    if talhao_id is None:
        talhao_id = farm_id  # compatibilidade com chamadas antigas
    dates = settings.sentinel_dates

    series_data = []

    for d in dates:
        if farm_id == 1:
            img_path = os.path.join(BASE_DIR, f"sentinel-21KXQ-{d}", f"{layer}_cloudless_min_max.png")
            fallback_img_path = os.path.join(
                BASE_DIR, "dynamic_talhoes", f"farm_{farm_id}_talhao_{talhao_id}", f"{layer}_cloudless_min_max.png"
            )
        else:
            img_path = os.path.join(BASE_DIR, "dynamic_talhoes", f"farm_{farm_id}_talhao_{talhao_id}", f"{layer}_cloudless_min_max.png")
            fallback_img_path = None

        stats = extract_layer_stats(img_path, total_area_ha)

        # PR #4 — diagnóstica a origem dos dados (o 3D/dashboard precisa saber
        # se o valor é Sentinel real, textura procedural ou fallback numérico):
        #   sentinel  → dataset nativo sentinel-21KXQ-* presente;
        #   procedural→ textura espectrais geradas (fazenda dinâmica/demo);
        #   fallback  → nenhum asset disponível (valor sintético explícito).
        if stats:
            data_origin = "sentinel" if farm_id == 1 else "procedural"
        elif fallback_img_path:
            stats = extract_layer_stats(fallback_img_path, total_area_ha)
            data_origin = "procedural" if stats else None
        else:
            data_origin = None
        if stats is None:
            data_origin = "fallback"

        if stats:
            series_data.append({
                "date": d,
                "formatted_date": "/".join(d.split("-")[::-1]),
                "mean": stats["mean_index"],
                "zones": stats["zones"],
                "data_origin": data_origin,
            })
        else:
            series_data.append({
                "date": d,
                "formatted_date": "/".join(d.split("-")[::-1]),
                "mean": 0.68,
                "data_origin": "fallback",
                "zones": {
                    "stress": {"pct": 10.0, "ha": round(total_area_ha * 0.1, 2)},
                    "medium": {"pct": 25.0, "ha": round(total_area_ha * 0.25, 2)},
                    "good": {"pct": 45.0, "ha": round(total_area_ha * 0.45, 2)},
                    "dense": {"pct": 20.0, "ha": round(total_area_ha * 0.2, 2)}
                }
            })
            
    yield_predictions = estimate_seasonal_yield(series_data, total_area_ha)

    return {
        "farm_id": farm_id,
        "layer": layer,
        "timeline": series_data,
        "yield_predictions": yield_predictions
    }