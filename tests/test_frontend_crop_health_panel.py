"""PR #8 — Contrato do painel "Saúde & Evolução da Lavoura" no frontend.

Além da checagem estática de marcação, este módulo executa o código REAL do
painel (`index.html`, bloco [CROP-HEALTH-UI-*]) numa VM Node com um DOM
mínimo, e verifica o que a interface efetivamente escreve na tela:

- cada variação aparece com a UNIDADE do índice e a DATA de referência;
- "Estável" nunca convive com uma "variação absoluta" sem contexto;
- qualidade da CENA e cobertura da SÉRIE são campos distintos;
- ausência de dado é escrita como "Sem dado" — nunca como 0 ou "Estável";
- a última cena disponível é diferenciada da última cena válida.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "orion.css").read_text(encoding="utf-8")


def _slice(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start:source.index(end_marker, start)]


PANEL_HTML = _slice(INDEX, '<div class="chart-box" id="crop-health-panel">', "<!-- ======")
PANEL_JS = _slice(INDEX, "// [CROP-HEALTH-UI-START]", "// [CROP-HEALTH-UI-END]")


# ===========================================================================
# 1. Marcação — hierarquia e elementos essenciais após o rework
# ===========================================================================
def test_painel_expoe_a_hierarquia_de_leitura():
    """N1 estado/tendência · N2 deltas · N4 atenção · N5 série e A/B."""
    for marker in (
        "Saúde &amp; Evolução da Lavoura",
        'id="crop-health-current"',          # N1 — cena atual
        'id="crop-health-current-validity"',  # N1 — validade da cena atual
        'id="crop-health-trend"',            # N3 — tendência
        'id="crop-health-trend-window"',     # N3 — janela analisada
        'id="crop-health-delta-previous"',   # N2 — desde a cena anterior
        'id="crop-health-delta-30d"',        # N2 — ~30 dias
        'id="crop-health-delta-window"',     # N2 — período da tendência
        'id="crop-health-persistent"',       # N4 — persistência
        'id="crop-health-anomaly"',          # N4 — anomalia interna
        'id="crop-health-scene-quality"',    # N4 — qualidade da cena
        'id="crop-health-quality"',          # N4 — cobertura da série
        'id="cropHealthChart"',              # N5 — timeline
        'id="crop-health-change-summary"',   # N5 — comparação A/B
        'id="crop-health-provenance"',
    ):
        assert marker in PANEL_HTML, marker


def test_rotulos_dos_deltas_sao_semanticamente_explicitos():
    assert "Desde a cena anterior" in PANEL_HTML
    assert "Variação em ~30 dias" in PANEL_HTML
    assert "Variação no período da tendência" in PANEL_HTML
    assert "Comparação A → B" in PANEL_HTML
    # O rótulo antigo, ambíguo, não pode voltar.
    assert "Tendência recente</span>" not in PANEL_HTML
    assert "variação absoluta" not in PANEL_JS


def test_qualidade_da_cena_e_da_serie_sao_indicadores_distintos():
    assert "Qualidade da cena atual" in PANEL_HTML
    assert "Cobertura da série" in PANEL_HTML
    # `confidence` deixou de ser apresentado como dimensão independente.
    assert "confidence" not in PANEL_HTML
    assert "confidence" not in PANEL_JS


def test_controles_tem_label_e_estados_acessiveis():
    assert '<label for="crop-health-index">' in PANEL_HTML
    assert '<label for="crop-health-period">' in PANEL_HTML
    assert '<label for="crop-health-date-a">' in PANEL_HTML
    assert '<label for="crop-health-date-b">' in PANEL_HTML
    assert 'aria-live="polite"' in PANEL_HTML
    assert 'role="img"' in PANEL_HTML and "aria-label" in PANEL_HTML


def test_grafico_distingue_limitada_de_insuficiente():
    """No gráfico, "limitada" e "insuficiente" têm cores próprias."""
    assert "cena de qualidade alta" in PANEL_HTML
    assert "qualidade limitada" in PANEL_HTML
    assert "insuficiente — fora da tendência" in PANEL_HTML
    assert "limitada: '#d98a7f'" in PANEL_JS
    assert "insuficiente: '#58655f'" in PANEL_JS
    assert PANEL_JS.count("CH_QUALITY_COLOR") >= 2


def test_estados_vazio_carregando_e_erro_sao_distintos():
    assert "state-loading" in PANEL_HTML
    assert "state-empty" in PANEL_HTML
    # O painel troca a classe por estado; os quatro estados são usados.
    assert "`state state-${kind}`" in PANEL_JS
    for kind in ("'loading'", "'empty'", "'error'", "'ok'"):
        assert f"cropHealthSetState(" in PANEL_JS and kind in PANEL_JS, kind
    for classe in (".state-loading", ".state-empty", ".state-error", ".state-ok"):
        assert classe in CSS, classe


def test_frontend_usa_endpoints_privados_e_bearer():
    assert "const CropHealthService" in APP
    assert "/crop-health/timeline" in APP
    assert "/crop-health/compare" in APP
    assert "/crop-health/difference.png" in APP
    assert "headers: { ...authHeaders() }" in APP
    assert "getDifferenceBlob" in APP


def test_frontend_nao_calcula_delta_de_png_nem_fala_com_a_copernicus():
    assert "stac.dataspace.copernicus.eu" not in INDEX
    assert "sh.dataspace.copernicus.eu" not in INDEX
    assert "/api/" not in PANEL_HTML, "URLs ficam no serviço autenticado, não na marcação"


def test_linguagem_do_painel_e_conservadora():
    assert "não estabelece causalidade" in PANEL_HTML
    assert "não permite determinar a causa" in PANEL_HTML
    assert "dados insuficientes" in PANEL_HTML.lower()
    assert "não é diagnóstico agronômico" in PANEL_HTML
    # Nada de linguagem promocional no painel.
    for proibido in ("poderoso", "inteligente", "revolucion", "potencializ", "insights"):
        assert proibido not in PANEL_HTML.lower(), proibido


# ===========================================================================
# 2. Comportamento — o painel é executado com um payload real do backend
# ===========================================================================
DOM_STUB = r"""
const registry = new Map();
function makeEl(id) {
  const el = {
    id, className: '', innerText: '', _html: '', hidden: false, disabled: false,
    style: {}, children: [], _attrs: {},
    classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
    },
    appendChild(c) { el.children.push(c); return c; },
    addEventListener() {},
    setAttribute(k, v) { el._attrs[k] = v; },
    removeAttribute(k) { delete el._attrs[k]; },
    toggleAttribute() {},
    querySelector() { return { toggleAttribute() {} }; },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._html; },
    set(v) { el._html = String(v); el.children = []; },
  });
  Object.defineProperty(el, 'text', { get() { return el.innerText || el._html; } });
  registry.set(id, el);
  return el;
}
const document = {
  getElementById(id) { return registry.get(id) || makeEl(id); },
  createElement(tag) { const e = makeEl('__' + tag + '_' + registry.size); e.tag = tag; return e; },
  querySelectorAll() { return []; },
};
function readAll() {
  const out = {};
  for (const [id, el] of registry) out[id] = { text: el.innerText, html: el._html, className: el.className, hidden: el.hidden, disabled: el.disabled };
  return out;
}
"""


def _run_panel(payload: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível")
    script = (
        "const vm = require('vm');\n"
        "const sandbox = { console: { log(){}, info(){}, warn(){}, error(){} } };\n"
        "vm.createContext(sandbox);\n"
        "vm.runInContext(process.env.DOM, sandbox);\n"
        "vm.runInContext(process.env.PANEL, sandbox);\n"
        "sandbox.cropHealthIndex = JSON.parse(process.env.PAYLOAD).index;\n"
        "sandbox.renderCropHealthTimeline(JSON.parse(process.env.PAYLOAD));\n"
        "process.stdout.write(JSON.stringify(sandbox.readAll()));\n"
    )
    env = {
        "DOM": DOM_STUB,
        # o bloco real do painel, sem os `document.getElementById(...).addEventListener`
        # finais (não existem handlers no stub — mas o stub os aceita).
        "PANEL": "var cropHealthTimeline = [];\nvar cropHealthIndex = 'ndvi';\n"
                 "var cropHealthChartInstance = null;\nvar cropHealthAborter = null;\n"
                 "var cropHealthDifferenceObjectUrl = null;\n" + PANEL_JS,
        "PAYLOAD": json.dumps(payload),
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run([node, "-e", script], cwd=ROOT, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _scene(day, mean, quality="alta", valid=96.0, cloud=4.0, **extra):
    base = {
        "date": day, "mean": mean, "quality": quality,
        "quality_label": {"alta": "Alta", "media": "Média",
                          "limitada": "Limitada", "insuficiente": "Insuficiente"}[quality],
        "valid_pixel_pct": valid, "cloud_cover": cloud,
        "accepted_for_trend": quality in ("alta", "media"),
        "delta_previous": None, "delta_previous_from": None, "delta_previous_days": None,
        "delta_30d": None, "delta_30d_from": None, "delta_30d_days": None,
    }
    base.update(extra)
    return base


def _payload(**over):
    timeline = [
        _scene("2026-06-01", 0.91),
        _scene("2026-07-01", 0.78, delta_previous=-0.13, delta_previous_from="2026-06-01", delta_previous_days=30),
        _scene("2026-08-01", 0.66, delta_previous=-0.12, delta_previous_from="2026-07-01", delta_previous_days=31,
               delta_30d=-0.12, delta_30d_from="2026-07-01", delta_30d_days=31),
        _scene("2026-09-01", 0.59, delta_previous=-0.07, delta_previous_from="2026-08-01", delta_previous_days=31,
               delta_30d=-0.07, delta_30d_from="2026-08-01", delta_30d_days=31),
    ]
    current = timeline[-1]
    payload = {
        "status": "ok", "index": "ndvi",
        "index_definition": {"formula": "(B08 - B04) / (B08 + B04)", "label": "NDVI"},
        "period": {"start": "2025-09-22", "end": "2026-09-22", "days": 366},
        "timeline": timeline,
        "source_data": {"provider": "Copernicus Data Space Ecosystem",
                        "collection": "sentinel-2-l2a", "processing_level": "L2A"},
        "quality": {"scope": "serie", "level": "alta", "label": "Alta", "scenes": 4,
                    "usable_scenes": 4, "usable_ratio_pct": 100.0,
                    "criteria": "critério documentado da série"},
        "scene_quality": {"scope": "cena", "level": "alta", "label": "Alta",
                          "date": "2026-09-01", "valid_pixel_pct": 96.0, "cloud_cover": 4.0},
        "confidence": {"alias_of": "quality", "level": "alta"},
        "metrics": {
            "current": current,
            "latest_scene": current,
            "latest_valid_scene": current,
            "current_is_valid_for_analysis": True,
            "current_scene_note": "A última cena com dado também é a última cena válida para a análise de tendência.",
            "delta_previous_detail": {"value": -0.07, "from_date": "2026-08-01",
                                      "to_date": "2026-09-01", "days": 31,
                                      "reference": "cena anterior com dado", "unit": "NDVI"},
            "delta_30d_detail": {"value": -0.07, "from_date": "2026-08-01",
                                 "to_date": "2026-09-01", "days": 31,
                                 "reference": "cena mais próxima de 30 dias antes (±15 dias)", "unit": "NDVI"},
            "trend": "queda", "trend_label": "Queda",
            "trend_detail": {
                "code": "queda", "label": "Queda",
                "text": "Redução consistente do NDVI na janela analisada.",
                "scenes_used": 4, "delta": -0.32,
                "delta_from": "2026-06-01", "delta_to": "2026-09-01",
                "span_days": 92, "method": "Theil–Sen sobre datas reais",
                "criteria": "critério documentado da tendência",
                "window": {"days": 120, "mode": "janela_recente",
                           "first_observation": "2026-06-01", "last_observation": "2026-09-01"},
            },
            "persistence": {"status": "ok", "scenes_used": 3,
                            "declining": {"pixels": 400, "pct": 25.0, "ha": 2.5},
                            "window": {"start": "2026-07-01", "end": "2026-09-01",
                                       "days": 62, "scenes": 3, "transitions": 2}},
            "internal_anomaly": {"status": "ok", "talhao_median": 0.59,
                                 "below_internal_baseline": {"pixels": 120, "pct": 7.5, "ha": 0.75}},
        },
        "interpretation": {"attention": ["Queda espectral persistente em 2.50 ha entre 2026-07-01 e 2026-09-01."]},
    }
    payload.update(over)
    return payload


def test_painel_nunca_exibe_variacao_sem_referencia():
    dom = _run_panel(_payload())
    anterior = dom["crop-health-delta-previous"]["text"]
    ref = dom["crop-health-delta-previous-ref"]["text"]
    assert anterior == "−0,07 NDVI", anterior         # unidade explícita, nunca "%"
    assert "01/08/2026 → 01/09/2026" in ref and "31 dias" in ref

    janela = dom["crop-health-delta-window"]["text"]
    janela_ref = dom["crop-health-delta-window-ref"]["text"]
    assert janela == "−0,32 NDVI"
    assert "01/06/2026 → 01/09/2026" in janela_ref and "92 dias da janela" in janela_ref
    # Os três deltas são valores diferentes e cada um tem a sua referência.
    assert dom["crop-health-delta-30d-ref"]["text"] != janela_ref


def test_painel_declara_a_janela_da_tendencia():
    dom = _run_panel(_payload())
    assert dom["crop-health-trend"]["text"] == "Queda"
    assert dom["crop-health-trend-label"]["text"] == "Tendência · últimos 120 dias"
    janela = dom["crop-health-trend-window"]["text"]
    assert "4 cenas válidas" in janela
    assert "01/06/2026 a 01/09/2026" in janela


def test_nao_ha_mais_colisao_estavel_com_variacao_sem_contexto():
    """Regressão do caso da auditoria: −0,32 em 23 cenas era rotulado Estável."""
    dom = _run_panel(_payload())
    assert dom["crop-health-trend"]["text"] != "Estável"
    # E, quando a série for realmente estável, o delta continua referenciado.
    payload = _payload()
    payload["metrics"]["trend_detail"].update({
        "code": "estavel", "label": "Estável",
        "text": "Série praticamente horizontal: variação estimada abaixo de 0.05 NDVI.",
        "delta": 0.004,
    })
    payload["metrics"]["trend"] = "estavel"
    dom = _run_panel(payload)
    assert dom["crop-health-trend"]["text"] == "Estável"
    assert "abaixo de 0.05" in dom["crop-health-trend-detail"]["text"]
    assert "01/06/2026 → 01/09/2026" in dom["crop-health-delta-window-ref"]["text"]


def test_ausencia_de_dado_e_escrita_como_sem_dado():
    payload = _payload()
    payload["metrics"]["delta_30d_detail"] = None
    payload["metrics"]["persistence"] = {"status": "dados_insuficientes", "window": None,
                                         "message": "São necessárias pelo menos 3 cenas processadas."}
    payload["metrics"]["internal_anomaly"] = {"status": "dados_insuficientes",
                                              "message": "Não há pixels válidos para baseline interno."}
    dom = _run_panel(payload)
    assert dom["crop-health-delta-30d"]["text"] == "Sem dado"
    assert dom["crop-health-delta-30d"]["className"] == "value none"
    assert "nenhuma cena próxima de 30 dias" in dom["crop-health-delta-30d-ref"]["text"]
    assert dom["crop-health-persistent"]["text"] == "Sem dado"
    assert "pelo menos 3 cenas" in dom["crop-health-persistent-pct"]["text"]
    assert dom["crop-health-anomaly"]["text"] == "Sem dado"
    # "Sem dado" nunca é desenhado como 0 nem como "Estável".
    for campo in ("crop-health-delta-30d", "crop-health-persistent", "crop-health-anomaly"):
        assert dom[campo]["text"] not in ("0", "0,00", "Estável", "—")


def test_cena_disponivel_e_distinguida_da_cena_valida():
    payload = _payload()
    ruim = _scene("2026-09-15", 0.30, quality="insuficiente", valid=18.0, cloud=91.0)
    payload["timeline"] = payload["timeline"] + [ruim]
    payload["metrics"].update({
        "current": ruim,
        "latest_scene": ruim,
        "latest_valid_scene": payload["timeline"][3],
        "current_is_valid_for_analysis": False,
        "current_scene_note": "A última cena disponível (2026-09-15, qualidade insuficiente) "
                              "NÃO entra na tendência; a última cena válida para análise é 2026-09-01.",
    })
    payload["scene_quality"] = {"scope": "cena", "level": "insuficiente", "label": "Insuficiente",
                                "date": "2026-09-15", "valid_pixel_pct": 18.0, "cloud_cover": 91.0}
    dom = _run_panel(payload)
    assert "15/09/2026" in dom["crop-health-current-date"]["text"]
    validade = dom["crop-health-current-validity"]["text"]
    assert "NÃO entra na tendência" in validade and "2026-09-01" in validade
    assert dom["crop-health-current-card"]["className"] == "ch-headline is-warn"
    # Qualidade da cena "Insuficiente" convive com cobertura da série "Alta".
    assert dom["crop-health-scene-quality"]["text"] == "Insuficiente"
    assert dom["crop-health-quality"]["text"] == "Alta"


def test_qualidade_da_cena_e_da_serie_aparecem_com_escopos_proprios():
    dom = _run_panel(_payload())
    assert "Cena de 01/09/2026" in dom["crop-health-scene-quality-detail"]["text"]
    assert "cobertura válida 96,0%" in dom["crop-health-scene-quality-detail"]["text"]
    serie = dom["crop-health-quality-detail"]["text"]
    assert "4 de 4 aquisições úteis" in serie and "período exibido" in serie


def test_persistencia_e_anomalia_declaram_periodo_e_baseline():
    dom = _run_panel(_payload())
    persist = dom["crop-health-persistent-pct"]["text"]
    assert "01/07/2026" in persist and "01/09/2026" in persist and "62 dias" in persist
    assert dom["crop-health-persistent"]["text"] == "2,50 ha"
    anomalia = dom["crop-health-anomaly-ref"]["text"]
    assert "mediana do próprio talhão" in anomalia and "0,59 NDVI" in anomalia


def test_proveniencia_traz_fonte_formula_mascara_e_criterios():
    dom = _run_panel(_payload())
    prov = dom["crop-health-provenance"]["html"]
    assert "Copernicus Data Space Ecosystem" in prov
    assert "sentinel-2-l2a" in prov and "L2A" in prov
    assert "(B08 - B04) / (B08 + B04)" in prov
    assert "SCL + dataMask" in prov
    assert "critério documentado da tendência" in prov
    assert "critério documentado da série" in prov


def test_cabecalho_do_grafico_informa_periodo_unidade_e_cenas():
    dom = _run_panel(_payload())
    assert dom["crop-health-chart-title"]["text"] == "NDVI médio do talhão por aquisição"
    intervalo = dom["crop-health-chart-range"]["text"]
    assert "01/06/2026 a 01/09/2026" in intervalo
    assert "4 cenas com dado de 4 aquisições" in intervalo
    assert "janela de 366 dias" in intervalo
