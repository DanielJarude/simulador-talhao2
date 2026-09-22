"""
Modelo agronômico calibrado por cultura (simulação what-if).

`base_yield_sc_ha` representa a produtividade de referência (sacas/ha)
da cultura sem manejo adicional — usada para estimar a produção total
na resposta da simulação.
"""
CROP_AGRONOMIC_MODELS = {
    "Soja / Milho Safrinha": {
        "price_per_bag": 125.0,
        "base_yield_sc_ha": 65.0,
        "n_sens": 0.0015,       # Resposta à adubação nitrogenada
        "water_sens": 0.0022,   # Resposta à irrigação
        "pest_sens": 0.0045,    # Sensibilidade a pragas
        "yield_conversion": 45.0  # Sacas/ha por unidade de delta NDVI
    },
    "Soja": {
        "price_per_bag": 130.0,
        "base_yield_sc_ha": 65.0,
        "n_sens": 0.0012,
        "water_sens": 0.0025,
        "pest_sens": 0.0040,
        "yield_conversion": 50.0
    },
    "Milho Safrinha": {
        "price_per_bag": 60.0,
        "base_yield_sc_ha": 90.0,
        "n_sens": 0.0028,
        "water_sens": 0.0020,
        "pest_sens": 0.0050,
        "yield_conversion": 85.0
    },
    "Algodão": {
        "price_per_bag": 180.0,
        "base_yield_sc_ha": 300.0,
        "n_sens": 0.0020,
        "water_sens": 0.0030,
        "pest_sens": 0.0060,
        "yield_conversion": 65.0
    },
    "Café": {
        "price_per_bag": 1150.0,
        "base_yield_sc_ha": 25.0,
        "n_sens": 0.0018,
        "water_sens": 0.0022,
        "pest_sens": 0.0055,
        "yield_conversion": 18.0
    }
}


def calculate_what_if_impact(
    n_kg: float,
    w_mm: float,
    pest_pct: float,
    area_ha: float = 42.54,
    crop: str = "Soja / Milho Safrinha",
) -> dict:
    """
    Calcula o impacto de N / irrigação / pragas sobre NDVI, produtividade
    e financeiro. O dicionário retornado espelha EXATAMENTE o schema
    `schemas.WhatIfResponse` (o mismatch antigo causava HTTP 500).
    """
    params = CROP_AGRONOMIC_MODELS.get(crop, CROP_AGRONOMIC_MODELS["Soja / Milho Safrinha"])

    delta_ndvi = (n_kg * params["n_sens"]) + (w_mm * params["water_sens"]) - (pest_pct * params["pest_sens"])
    delta_yield = round(delta_ndvi * params["yield_conversion"], 2)

    # Custo de insumos: Ureia (~R$ 3,80/kg N) e custo de bombeamento de água (~R$ 1,20/mm/ha)
    cost_n = max(0.0, n_kg * 3.80 * area_ha)
    cost_w = max(0.0, w_mm * 1.20 * area_ha)
    gross_gain = delta_yield * area_ha * params["price_per_bag"]
    net_finance = round(gross_gain - (cost_n + cost_w), 2)

    base_yield = params["base_yield_sc_ha"]

    return {
        "delta_ndvi": round(delta_ndvi, 4),
        "delta_yield_sc_ha": delta_yield,
        "total_production_sc": round((base_yield + delta_yield) * area_ha, 1),
        "financial_impact_brl": net_finance,
        "displacement_scale_3d": round(max(1.0, 4.0 + delta_ndvi * 8.0), 3),
        "gross_financial_gain_brl": round(gross_gain, 2),
        "net_financial_gain_brl": net_finance,
        "crop": crop,
        "bag_price_brl": params["price_per_bag"],
    }
