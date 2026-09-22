"""PR #8 — Tendência temporal: janela, robustez e casos da auditoria.

A auditoria mostrou que o classificador antigo exigia monotonicidade perfeita
(`all(s == 1)` / `all(s == -1)`): uma única oscilação intermediária transformava
uma queda de −0,32 NDVI em 23 cenas no rótulo "Estável".

Estes testes fixam o comportamento do classificador novo (Theil–Sen sobre datas
reais + tau de Kendall + razão sinal/ruído) e da janela operacional.
"""
from datetime import date, timedelta

import pytest

from services import crop_health_service as health


def serie(start: date, step_days, values, quality="alta"):
    """Constrói uma timeline aceitável a partir de datas reais."""
    if isinstance(step_days, int):
        step_days = [step_days] * (len(values) - 1)
    dates, cursor = [start], start
    for step in step_days:
        cursor = cursor + timedelta(days=step)
        dates.append(cursor)
    assert len(dates) == len(values)
    qualities = quality if isinstance(quality, (list, tuple)) else [quality] * len(values)
    return [
        {"date": d.isoformat(), "mean": v, "quality": q}
        for d, v, q in zip(dates, values, qualities)
    ]


# ---------------------------------------------------------------------------
# 1. Série praticamente horizontal continua "Estável"
# ---------------------------------------------------------------------------
def test_serie_horizontal_permanece_estavel():
    values = [0.601, 0.598, 0.603, 0.600, 0.599, 0.602, 0.601, 0.600]
    result = health.classify_trend(serie(date(2026, 5, 1), 12, values))
    assert result["code"] == "estavel"
    assert result["label"] == "Estável"
    assert abs(result["estimated_change"]) < 0.05
    # "Estável" precisa continuar distinto de "sem tendência" e de "sem dados".
    assert result["code"] not in {"sem_tendencia", "dados_insuficientes"}


# ---------------------------------------------------------------------------
# 2. Queda real com muitas cenas e oscilações — o caso que falhava
# ---------------------------------------------------------------------------
def test_queda_com_23_cenas_e_oscilacoes_nao_vira_estavel():
    # 23 cenas em ~110 dias, queda de ~0,32 com ruído de ±0,02 por passagem —
    # inclusive com DUAS subidas intermediárias que quebram a monotonicidade.
    base = [0.91 - (0.32 / 22) * i for i in range(23)]
    base[7] += 0.035     # oscilação para cima no meio da queda
    base[15] += 0.030    # segunda oscilação
    timeline = serie(date(2026, 5, 25), 5, [round(v, 4) for v in base])
    result = health.classify_trend(timeline)
    assert result["code"] == "queda", result
    assert result["scenes_used"] == 23
    assert result["delta"] < -0.25
    assert result["tau"] < -0.5
    # A oscilação existe na sequência de direções, mas não destrói a tendência.
    assert 1 in result["direction_sequence"]
    assert result["signal_to_noise"] >= 1.0


def test_queda_com_12_cenas_e_pequenas_oscilacoes():
    values = [0.78, 0.76, 0.77, 0.72, 0.70, 0.71, 0.66, 0.63, 0.64, 0.59, 0.56, 0.54]
    result = health.classify_trend(serie(date(2026, 6, 1), 8, values))
    assert result["code"] == "queda"
    assert result["slope_per_day"] < 0


# ---------------------------------------------------------------------------
# 3. Melhoria com oscilações
# ---------------------------------------------------------------------------
def test_melhoria_com_oscilacoes_nao_vira_estavel():
    values = [0.30, 0.33, 0.31, 0.38, 0.42, 0.40, 0.47, 0.52, 0.50, 0.58, 0.62, 0.66]
    result = health.classify_trend(serie(date(2026, 6, 1), 9, values))
    assert result["code"] == "melhoria"
    assert result["delta"] > 0.05
    assert result["tau"] > health.TREND_TAU_MIN


def test_melhoria_com_20_cenas_e_ruido():
    base = [0.35 + (0.30 / 19) * i for i in range(20)]
    base[4] -= 0.03
    base[11] -= 0.025
    result = health.classify_trend(serie(date(2026, 6, 1), 5, [round(v, 4) for v in base]))
    assert result["code"] == "melhoria"
    assert result["scenes_used"] == 20


# ---------------------------------------------------------------------------
# 4. Série ruidosa sem tendência relevante — nem "melhoria" nem "estável"
# ---------------------------------------------------------------------------
def test_serie_ruidosa_sem_tendencia_relevante():
    # Oscilação de ~0,35 entre passagens, com arrasto residual de apenas 0,06:
    # o arrasto não pode ser promovido a tendência.
    values = [0.40, 0.75, 0.42, 0.78, 0.40, 0.72, 0.45, 0.80]
    result = health.classify_trend(serie(date(2026, 6, 1), 12, values))
    assert result["code"] == "sem_tendencia"
    assert result["label"] == "Sem tendência definida"
    assert result["signal_to_noise"] < 1.0
    # Distinto de "estável": a série NÃO é horizontal.
    assert result["residual_scatter"] > result["absolute_threshold"]


# ---------------------------------------------------------------------------
# 5. Proteções: cenas mínimas e intervalo temporal mínimo
# ---------------------------------------------------------------------------
def test_intervalo_menor_que_minimo_e_dados_insuficientes():
    result = health.classify_trend(serie(date(2026, 9, 1), 5, [0.62, 0.52, 0.41]))
    assert result["code"] == "dados_insuficientes"
    assert str(health.MIN_TREND_INTERVAL_DAYS) in result["reason"]


def test_menos_de_tres_cenas_aceitas_e_dados_insuficientes():
    result = health.classify_trend(serie(date(2026, 7, 1), 20, [0.62, 0.45]))
    assert result["code"] == "dados_insuficientes"
    assert result["scenes_used"] == 2


def test_cenas_de_qualidade_inadequada_nao_entram_na_tendencia():
    # Duas cenas boas + duas ruins: as ruins não podem "completar" o mínimo.
    timeline = serie(
        date(2026, 6, 1), 20, [0.70, 0.30, 0.68, 0.28],
        quality=["alta", "insuficiente", "alta", "limitada"],
    )
    result = health.classify_trend(timeline)
    assert result["code"] == "dados_insuficientes"
    assert result["scenes_used"] == 2


# ---------------------------------------------------------------------------
# 6. Datas irregulares — o eixo é o calendário real, não o índice ordinal
# ---------------------------------------------------------------------------
def test_datas_irregulares_usam_dias_reais_e_nao_posicao_ordinal():
    # Três passagens agrupadas + uma muito distante. Com índice ordinal a
    # inclinação seria a mesma de passos regulares; com datas reais, não.
    timeline = serie(date(2026, 6, 1), [2, 2, 88], [0.80, 0.79, 0.81, 0.40])
    result = health.classify_trend(timeline)
    assert result["code"] == "queda"
    assert result["span_days"] == 92
    # Inclinação por DIA: a queda de ~0,40 diluída em 92 dias.
    assert result["slope_per_day"] == pytest.approx(-0.0043, abs=5e-4)


def test_inclinacao_independe_do_espacamento_das_datas():
    regular = health.classify_trend(serie(date(2026, 6, 1), 10, [0.8, 0.7, 0.6, 0.5]))
    irregular = health.classify_trend(serie(date(2026, 6, 1), [5, 15, 10], [0.8, 0.75, 0.65, 0.5]))
    # Ambas caem 0,30 em 30 dias → mesma inclinação diária, apesar do
    # espaçamento diferente entre as passagens.
    assert regular["span_days"] == irregular["span_days"] == 30
    assert regular["slope_per_day"] == pytest.approx(-0.01, abs=1e-3)
    assert irregular["slope_per_day"] == pytest.approx(-0.01, abs=1e-3)


# ---------------------------------------------------------------------------
# 7. Janela operacional da tendência (A2)
# ---------------------------------------------------------------------------
def test_tendencia_usa_janela_recente_e_nao_o_historico_inteiro():
    # Histórico de 2 anos subindo + últimos ~100 dias caindo. O indicador
    # "recente" precisa responder à queda atual, não à subida de 2024.
    antigo = serie(date(2024, 10, 1), 15, [round(0.50 + 0.01 * i, 4) for i in range(20)])
    recente = serie(date(2026, 6, 1), 10, [round(0.80 - 0.03 * i, 4) for i in range(11)])
    result = health.classify_trend(antigo + recente)
    assert result["code"] == "queda"
    assert result["scenes_used"] == 11
    assert result["scenes_accepted_total"] == 31
    window = result["window"]
    assert window["mode"] == "janela_recente"
    assert window["days"] == health.TREND_WINDOW_DAYS
    assert window["first_observation"] == "2026-06-01"
    assert window["last_observation"] == "2026-09-09"
    assert window["end"] == window["anchor_date"] == "2026-09-09"


def test_janela_estende_quando_falta_cobertura_e_declara_o_modo():
    # Só 3 cenas aceitas, espalhadas por ~200 dias (nuvem no meio): a janela
    # é estendida UMA vez e o modo fica explícito na resposta.
    timeline = serie(date(2026, 2, 1), [100, 95], [0.70, 0.60, 0.48])
    result = health.classify_trend(timeline)
    assert result["window"]["mode"] == "janela_estendida"
    assert result["window"]["days"] == health.TREND_WINDOW_MAX_DAYS
    assert "estendida" in result["window"]["mode_label"].lower()
    assert result["scenes_used"] == 3


def test_janela_nao_mistura_dois_ciclos_agricolas():
    # Cenas antigas além de TREND_WINDOW_MAX_DAYS ficam de fora mesmo quando
    # a janela precisa ser estendida.
    antigo = serie(date(2025, 1, 1), 15, [0.4, 0.45, 0.5])
    recente = serie(date(2026, 6, 1), 30, [0.80, 0.70, 0.60])
    result = health.classify_trend(antigo + recente)
    assert result["scenes_used"] == 3
    assert result["delta_from"] == "2026-06-01"


# ---------------------------------------------------------------------------
# 8. Contrato da resposta: sem número mágico sem explicação
# ---------------------------------------------------------------------------
def test_resposta_da_tendencia_documenta_criterio_e_limiares():
    result = health.classify_trend(serie(date(2026, 6, 1), 10, [0.80, 0.70, 0.60, 0.52]))
    assert result["criteria"] == health.TREND_CRITERIA
    assert result["method"].startswith("Theil–Sen")
    assert result["absolute_threshold"] == 0.05
    assert result["tau_threshold"] == health.TREND_TAU_MIN
    assert result["mann_kendall"]["pairs"] == 6
    # Delta do período é ponta-a-ponta E vem com as duas datas de referência.
    assert result["delta_from"] == "2026-06-01"
    assert result["delta_to"] == "2026-07-01"
    # Nunca em percentual: o índice é limitado e a variação é absoluta.
    assert "%" not in result["text"]


def test_estimadores_theil_sen_e_mann_kendall_isolados():
    import numpy as np

    x = np.asarray([0.0, 10.0, 20.0, 30.0])
    y = np.asarray([0.80, 0.70, 0.60, 0.50])
    assert health._theil_sen_slope(x, y) == pytest.approx(-0.01)
    mk = health._mann_kendall(x, y)
    assert mk["s"] == -6 and mk["tau"] == pytest.approx(-1.0)
    # Uma cena discrepante não derruba a mediana das inclinações par a par.
    y_outlier = np.asarray([0.80, 0.70, 0.95, 0.50])
    assert health._theil_sen_slope(x, y_outlier) < 0
