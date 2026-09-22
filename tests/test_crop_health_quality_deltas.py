"""PR #8 — Qualidade (cena × série), deltas referenciados e cena atual.

Pontos da auditoria cobertos aqui:

- A3: `delta_previous`, `delta_30d` e o delta da janela da tendência são
  conceitos distintos e cada um carrega a sua referência;
- A4: a qualidade da CENA e a qualidade agregada da SÉRIE são dimensões
  diferentes; uma cena ruim não rebaixa a série inteira para "Limitada",
  e "Limitada" deixou de ser fallback universal;
- A5: a última cena disponível pode não ser a última cena válida para a
  análise, e isso precisa ficar explícito em vez de escondido.
"""
from datetime import date, timedelta

import numpy as np
import pytest

from services import crop_health_service as health


def item(day: str, mean, quality="alta", valid=95.0, cloud=4.0):
    return {
        "date": day, "mean": mean, "quality": quality,
        "quality_label": {"alta": "Alta", "media": "Média",
                          "limitada": "Limitada", "insuficiente": "Insuficiente"}[quality],
        "valid_pixel_pct": valid, "cloud_cover": cloud,
        "delta_previous": None, "delta_previous_from": None, "delta_previous_days": None,
        "delta_30d": None, "delta_30d_from": None, "delta_30d_days": None,
    }


# ---------------------------------------------------------------------------
# delta_previous — cena anterior COM DADO, com data e intervalo
# ---------------------------------------------------------------------------
def test_delta_previous_traz_data_de_referencia_e_intervalo():
    timeline = [item("2026-08-01", 0.70), item("2026-08-11", 0.62), item("2026-08-21", 0.58)]
    health._enrich_deltas(timeline)
    assert timeline[0]["delta_previous"] is None
    assert timeline[1]["delta_previous"] == pytest.approx(-0.08)
    assert timeline[1]["delta_previous_from"] == "2026-08-01"
    assert timeline[1]["delta_previous_days"] == 10
    assert timeline[2]["delta_previous_from"] == "2026-08-11"


def test_delta_previous_pula_cena_sem_dado_em_vez_de_zerar():
    # A cena do meio falhou no processamento: o delta da terceira precisa
    # referenciar a última cena COM DADO, não virar None silenciosamente.
    timeline = [item("2026-08-01", 0.70), item("2026-08-11", None), item("2026-08-21", 0.60)]
    health._enrich_deltas(timeline)
    assert timeline[1]["delta_previous"] is None
    assert timeline[2]["delta_previous"] == pytest.approx(-0.10)
    assert timeline[2]["delta_previous_from"] == "2026-08-01"
    assert timeline[2]["delta_previous_days"] == 20


# ---------------------------------------------------------------------------
# delta_30d — alvo de 30 dias com tolerância explícita
# ---------------------------------------------------------------------------
def test_delta_30d_escolhe_a_cena_mais_proxima_de_30_dias():
    timeline = [
        item("2026-07-01", 0.50),   # 45 dias antes — mais longe do alvo
        item("2026-07-17", 0.55),   # 29 dias antes — vencedora
        item("2026-08-06", 0.60),   # 9 dias antes
        item("2026-08-15", 0.66),
    ]
    health._enrich_deltas(timeline)
    ultimo = timeline[-1]
    assert ultimo["delta_30d_from"] == "2026-07-17"
    assert ultimo["delta_30d_days"] == 29
    assert ultimo["delta_30d"] == pytest.approx(0.11)


def test_delta_30d_respeita_a_borda_da_tolerancia():
    tol = health.DELTA_30D_TOLERANCE_DAYS
    alvo = health.DELTA_30D_TARGET_DAYS
    fim = date(2026, 8, 30)

    def monta(dias_antes):
        return [item((fim - timedelta(days=dias_antes)).isoformat(), 0.50),
                item(fim.isoformat(), 0.60)]

    dentro = monta(alvo + tol)          # exatamente na borda → aceito
    health._enrich_deltas(dentro)
    assert dentro[-1]["delta_30d"] == pytest.approx(0.10)
    assert dentro[-1]["delta_30d_days"] == alvo + tol

    fora = monta(alvo + tol + 1)        # um dia além da borda → sem dado
    health._enrich_deltas(fora)
    assert fora[-1]["delta_30d"] is None
    assert fora[-1]["delta_30d_from"] is None


def test_delta_30d_ausente_nao_vira_zero():
    timeline = [item("2026-08-25", 0.55), item("2026-08-30", 0.60)]
    health._enrich_deltas(timeline)
    assert timeline[-1]["delta_30d"] is None          # "sem dado", nunca 0
    assert timeline[-1]["delta_previous"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Qualidade da CENA × qualidade da SÉRIE
# ---------------------------------------------------------------------------
def test_qualidade_da_cena_continua_objetiva_e_isolada():
    assert health.classify_scene_quality(94, 3)["level"] == "alta"
    assert health.classify_scene_quality(78, 22)["level"] == "media"
    assert health.classify_scene_quality(61, 28)["level"] == "limitada"
    assert health.classify_scene_quality(10, 80)["level"] == "insuficiente"
    assert health.classify_scene_quality(95, None)["level"] == "media"


def test_serie_majoritariamente_util_nao_e_rebaixada_por_uma_cena_ruim():
    # 9 cenas altas + 1 insuficiente: o classificador antigo devolvia
    # "limitada" para a análise inteira.
    timeline = [item(f"2026-0{1 + i // 3}-{10 + i:02d}", 0.6) for i in range(9)]
    timeline.append(item("2026-09-28", None, quality="insuficiente"))
    result = health.classify_series_quality(timeline)
    assert result["level"] == "alta"
    assert result["usable_scenes"] == 9
    assert result["scenes"] == 10
    assert result["usable_ratio_pct"] == 90.0
    assert result["scope"] == "serie"


def test_serie_mista_alta_media_limitada_insuficiente():
    timeline = (
        [item("2026-06-01", 0.6, "alta"), item("2026-06-11", 0.6, "alta")]
        + [item("2026-06-21", 0.6, "media"), item("2026-07-01", 0.6, "media"), item("2026-07-11", 0.6, "media")]
        + [item("2026-07-21", 0.6, "limitada"), item("2026-08-01", 0.6, "limitada")]
        + [item("2026-08-11", None, "insuficiente")]
    )
    result = health.classify_series_quality(timeline)
    # 5 úteis de 8 (62,5%) e apenas 25% altas → "média", não "limitada".
    assert result["level"] == "media"
    assert result["distribution"] == {"alta": 2, "media": 3, "limitada": 2, "insuficiente": 1}
    assert result["usable_ratio_pct"] == 62.5


def test_serie_majoritariamente_insuficiente_e_declarada_insuficiente():
    timeline = [item("2026-06-01", 0.6, "alta")] + [
        item(f"2026-07-{10 + i:02d}", None, "insuficiente") for i in range(9)
    ]
    result = health.classify_series_quality(timeline)
    assert result["level"] == "insuficiente"
    assert result["usable_scenes"] == 1


def test_serie_com_cobertura_intermediaria_e_limitada():
    timeline = (
        [item(f"2026-06-{1 + i:02d}", 0.6, "media") for i in range(4)]
        + [item(f"2026-07-{1 + i:02d}", 0.6, "limitada") for i in range(5)]
    )
    result = health.classify_series_quality(timeline)
    # 4 úteis de 9 (44%) → limitada, mas nunca insuficiente por fallback.
    assert result["level"] == "limitada"


def test_limitada_nao_e_mais_fallback_universal():
    todas_altas = [item(f"2026-06-{1 + i:02d}", 0.6, "alta") for i in range(6)]
    assert health.classify_series_quality(todas_altas)["level"] == "alta"
    assert health.classify_series_quality([])["level"] == "insuficiente"


def test_criterio_da_qualidade_agregada_e_documentado():
    result = health.classify_series_quality([item("2026-06-01", 0.6)])
    assert result["criteria"] == health.SERIES_QUALITY_CRITERIA
    assert "80%" in result["criteria"] and "60%" in result["criteria"]


# ---------------------------------------------------------------------------
# Cena atual × última cena válida (A5) e contrato da resposta completa
# ---------------------------------------------------------------------------
def _stub_timeline(monkeypatch, calendar, means, qualities, valids=None, clouds=None):
    """Monta um get_health_timeline determinístico, sem rede."""
    health.clear_caches()
    valids = valids or {d: 96.0 for d in means}
    clouds = clouds or {d: 4.0 for d in means}
    monkeypatch.setattr(health.cdse, "fetch_real_calendar", lambda **kw: (calendar, "ok", None))

    def fake_process(*args, **kwargs):
        sc = kwargs.get("scene") or args[6]
        day = sc.acquisition_date
        value = means[day]
        if value is None:
            raise RuntimeError("cena sem pixels válidos")
        values = np.full((4, 4), value, dtype=float)
        mask = np.ones((4, 4), dtype=bool)
        return {
            "date": day, "values": values, "valid_mask": mask, "polygon_mask": mask,
            "stats": {"mean": value, "median": value, "min": value, "max": value,
                      "p25": value, "p75": value, "valid_pixel_pct": valids[day]},
            "scene": sc,
        }

    monkeypatch.setattr(health.cdse, "process_farm_scene", fake_process)
    return health.get_health_timeline(
        99, 99, -22.7, -51.2, 10.0, None, period_days=365, limit=30,
    )


def _cal(days, clouds):
    return [{"date": d, "datetime": f"{d}T10:00:00Z", "cloud_cover": c, "product_id": d}
            for d, c in zip(days, clouds)]


def test_cena_atual_distinta_da_ultima_cena_valida(monkeypatch):
    # A última passagem tem 88% de nuvem: aparece na timeline, mas NÃO entra
    # na análise. A resposta precisa dizer isso, não esconder.
    days = ["2026-06-01", "2026-06-21", "2026-07-11", "2026-07-31", "2026-08-20"]
    clouds = [3.0, 4.0, 5.0, 6.0, 88.0]
    means = dict(zip(days, [0.80, 0.74, 0.68, 0.62, 0.30]))
    valids = {d: 96.0 for d in days}
    valids["2026-08-20"] = 20.0
    result = _stub_timeline(monkeypatch, _cal(days, clouds), means, None, valids)

    metrics = result["metrics"]
    assert metrics["latest_scene"]["date"] == "2026-08-20"
    assert metrics["current"]["date"] == "2026-08-20"           # última COM dado
    assert metrics["latest_valid_scene"]["date"] == "2026-07-31"  # última ACEITA
    assert metrics["current_is_valid_for_analysis"] is False
    assert "não entra na tendência" in metrics["current_scene_note"].lower() \
        or "NÃO entra na tendência" in metrics["current_scene_note"]
    assert "2026-07-31" in metrics["current_scene_note"]
    # A cena ruim não puxa a tendência.
    assert metrics["trend_detail"]["scenes_used"] == 4
    assert result["status"] == "ok"


def test_cena_atual_valida_quando_a_ultima_passagem_e_boa(monkeypatch):
    days = ["2026-06-01", "2026-06-21", "2026-07-11", "2026-07-31"]
    means = dict(zip(days, [0.80, 0.74, 0.68, 0.62]))
    result = _stub_timeline(monkeypatch, _cal(days, [3.0] * 4), means, None)
    metrics = result["metrics"]
    assert metrics["current_is_valid_for_analysis"] is True
    assert metrics["latest_valid_scene"]["date"] == metrics["current"]["date"] == "2026-07-31"


def test_resposta_separa_qualidade_da_cena_e_da_serie(monkeypatch):
    days = ["2026-06-01", "2026-06-21", "2026-07-11", "2026-07-31"]
    means = dict(zip(days, [0.80, 0.74, 0.68, 0.62]))
    valids = {d: 96.0 for d in days}
    valids["2026-07-31"] = 70.0            # última cena um pouco pior
    clouds = [3.0, 3.0, 3.0, 25.0]
    result = _stub_timeline(monkeypatch, _cal(days, clouds), means, None, valids)

    assert result["quality"]["scope"] == "serie"
    assert result["scene_quality"]["scope"] == "cena"
    assert result["scene_quality"]["date"] == "2026-07-31"
    # A cena atual é "limitada" enquanto a série continua "alta": os dois
    # números convivem porque são indicadores diferentes e rotulados como tal.
    assert result["scene_quality"]["level"] == "limitada"
    assert result["quality"]["level"] in {"alta", "media"}
    # `confidence` deixou de ser uma dimensão independente.
    assert result["confidence"]["alias_of"] == "quality"
    assert result["confidence"]["level"] == result["quality"]["level"]


def test_resposta_traz_deltas_com_referencia_explicita(monkeypatch):
    days = ["2026-06-01", "2026-07-02", "2026-08-01"]
    means = dict(zip(days, [0.80, 0.70, 0.62]))
    result = _stub_timeline(monkeypatch, _cal(days, [3.0] * 3), means, None)
    metrics = result["metrics"]

    dp = metrics["delta_previous_detail"]
    assert dp["from_date"] == "2026-07-02" and dp["to_date"] == "2026-08-01"
    assert dp["days"] == 30 and dp["value"] == pytest.approx(-0.08)
    assert dp["unit"] == "NDVI" and "anterior" in dp["reference"]

    d30 = metrics["delta_30d_detail"]
    assert d30["from_date"] == "2026-07-02" and d30["days"] == 30
    assert "30 dias" in d30["reference"]

    trend = metrics["trend_detail"]
    assert trend["delta_from"] == "2026-06-01" and trend["delta_to"] == "2026-08-01"
    # O delta da janela e o delta da cena anterior NÃO são o mesmo número.
    assert trend["delta"] != dp["value"]


def test_persistencia_declara_a_janela_avaliada(monkeypatch):
    days = ["2026-06-01", "2026-06-21", "2026-07-11", "2026-07-31"]
    means = dict(zip(days, [0.80, 0.74, 0.68, 0.62]))
    result = _stub_timeline(monkeypatch, _cal(days, [3.0] * 4), means, None)
    persistence = result["metrics"]["persistence"]
    assert persistence["status"] == "ok"
    window = persistence["window"]
    assert window["start"] == "2026-06-21" and window["end"] == "2026-07-31"
    assert window["days"] == 40
    assert window["scenes"] == health.PERSISTENCE_SCENES
    assert window["transitions"] == health.PERSISTENCE_SCENES - 1
    assert "2026-06-21" in persistence["text"] and "40 dias" in persistence["text"]
