"""Contrato de frontend do painel PR #8.

Os testes são estáticos para não depender de CDN/DOM/serviços externos; o
bootstrap completo do index.html continua coberto pela suíte 3D existente.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "app.js").read_text(encoding="utf-8")


def test_painel_saude_prioriza_estado_tendencia_mudanca_e_qualidade():
    for marker in (
        "Saúde &amp; Evolução da Lavoura",
        "crop-health-current",
        "crop-health-trend",
        "crop-health-change-summary",
        "crop-health-quality",
        "crop-health-provenance",
    ):
        assert marker in INDEX


def test_frontend_usa_endpoints_privados_e_bearer():
    assert "const CropHealthService" in APP
    assert "/crop-health/timeline" in APP
    assert "/crop-health/compare" in APP
    assert "/crop-health/difference.png" in APP
    assert "headers: { ...authHeaders() }" in APP
    assert "getDifferenceBlob" in APP


def test_frontend_nao_calcula_delta_de_png_ou_chama_copernicus_diretamente():
    # O navegador apenas pede os contratos da API; o delta/máscara/área são
    # calculados no backend. Não deve haver URL CDSE no código do painel.
    assert "stac.dataspace.copernicus.eu" not in INDEX
    assert "sh.dataspace.copernicus.eu" not in INDEX
    panel_start = INDEX.index("Saúde &amp; Evolução da Lavoura")
    panel_end = INDEX.index("<!-- Zoneamento da Data Escolhida -->")
    assert "/api/" not in INDEX[panel_start:panel_end]  # URLs ficam no serviço autenticado


def test_interface_explica_mapa_delta_e_cenas_reais():
    assert "Série temporal das aquisições reais" in INDEX
    assert "Comparar A/B" in INDEX
    assert "Comparar com cena anterior" in INDEX
    assert "Verde = aumento" in INDEX
    assert "sem par válido" in INDEX


def test_linguagem_do_painel_e_conservadora():
    assert "não estabelece causalidade" in INDEX
    assert "não permitem determinar a causa" in INDEX or "não estabelece causalidade" in INDEX
    assert "dados insuficientes" in INDEX
    start = INDEX.index("Saúde &amp; Evolução da Lavoura")
    end = INDEX.index("<!-- Zoneamento da Data Escolhida -->")
    assert "diagnóstico" in INDEX[start:end].lower() or "causalidade" in INDEX[start:end].lower()
