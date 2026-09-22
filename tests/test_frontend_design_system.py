"""Contrato do design system interno (orion.css) e do rework visual.

Impede a volta do visual genérico: paleta solta, vidro/glow, emoji decorativo,
texto de marketing, página sem viewport e controles sem rótulo.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "orion.css").read_text(encoding="utf-8")
PAGES = {name: (ROOT / name).read_text(encoding="utf-8")
         for name in ("index.html", "fazendas.html", "auth.html", "dashboard.html")}

#: Paleta oficial — qualquer hex fora desta lista é regressão visual.
PALETTE = {
    # superfícies e linhas
    "#0b0f0e", "#121716", "#19201e", "#222b28", "#2a3431", "#3a4744",
    "#161d1b", "#141a19", "#131c1e", "#1c1a13", "#1a2220", "#3f6d86", "#7a6522",
    # texto
    "#e9efec", "#c3cfca", "#93a39d", "#7b8b84",
    # vegetação / aumento
    "#4e9a68", "#8ecfa0", "#3f7d52", "#59a874", "#6fae83", "#a3c2ae",
    # atenção
    "#b8862c", "#dfae55", "#b8720f", "#d9ad4e", "#c0a86a", "#a98b4e",
    "#b57a3e", "#e2cd86", "#96560f",
    # risco / queda
    "#b8473c", "#d98a7f", "#e0a196",
    # dado / água
    "#2f6f93", "#5a95bd", "#93b9cf", "#4a88ad", "#cfe3ee",
    # neutro / sem dado / branco puro
    "#58655f", "#ffffff", "#fff",
}


def _relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def test_design_system_define_os_tokens_essenciais():
    for token in (
        "--surface-0", "--surface-1", "--surface-2", "--line", "--line-strong",
        "--text-1", "--text-2", "--text-3",
        "--veg", "--warn", "--risk", "--data", "--neutral",
        "--font", "--fs-micro", "--fs-meta", "--fs-body", "--fs-value",
        "--sp-1", "--sp-2", "--sp-3", "--sp-4", "--r-sm", "--r-md", "--r-lg", "--focus",
    ):
        assert token in CSS, token


def test_todas_as_paginas_consomem_o_design_system():
    for name, html in PAGES.items():
        assert '<link rel="stylesheet" href="orion.css">' in html, name


def test_paleta_e_fechada_em_todas_as_paginas():
    fora = {}
    for name, html in {**PAGES, "orion.css": CSS}.items():
        for hex_color in re.findall(r"#[0-9a-fA-F]{3,6}\b", html):
            if hex_color.lower() not in PALETTE:
                fora.setdefault(name, set()).add(hex_color.lower())
    assert not fora, f"cores fora da paleta: {fora}"


@pytest.mark.parametrize("fg", ["#e9efec", "#c3cfca", "#93a39d", "#7b8b84",
                                "#8ecfa0", "#dfae55", "#d98a7f", "#93b9cf"])
@pytest.mark.parametrize("bg", ["#0b0f0e", "#121716", "#19201e"])
def test_contraste_de_texto_atende_wcag_aa(fg, bg):
    assert contrast(fg, bg) >= 4.5, f"{fg} sobre {bg} = {contrast(fg, bg):.2f}"


def test_cor_neutra_nunca_e_usada_como_texto():
    """#58655f é 'sem dado' — só serve para borda/legenda, não para ler."""
    assert contrast("#58655f", "#121716") < 4.5  # premissa do teste
    for name, html in {**PAGES, "orion.css": CSS}.items():
        # apenas a propriedade `color` (não background-color/border-*-color)
        for trecho in re.findall(r"(?<![-\w])color:\s*([^;\n}]+)", html):
            assert "#58655f" not in trecho, f"{name}: --neutral usado como cor de texto"
            assert "var(--neutral)" not in trecho, f"{name}: --neutral usado como cor de texto"


# ---------------------------------------------------------------------------
# Estética: sem template genérico / "AI-generated"
# ---------------------------------------------------------------------------
def test_sem_vidro_glow_ou_sombra_decorativa():
    for name, html in PAGES.items():
        assert "backdrop-filter" not in html, f"{name}: glassmorphism"
        assert "box-shadow" not in html, f"{name}: sombra decorativa"
        assert "linear-gradient" not in html, f"{name}: gradiente decorativo"
        assert "radial-gradient" not in html, f"{name}: gradiente decorativo"
        assert "text-shadow" not in html, f"{name}: glow em texto"
    # No design system, a ÚNICA sombra permitida é o anel de foco (a11y).
    sombras = re.findall(r"box-shadow:\s*([^;\n]+)", CSS)
    assert sombras == ["var(--focus)"], sombras
    assert "backdrop-filter" not in CSS and "text-shadow" not in CSS


def test_raios_pequenos_de_instrumento():
    """Nada de bordas arredondadas exageradas (cartão de marketing)."""
    for name, html in {**PAGES, "orion.css": CSS}.items():
        for raio in re.findall(r"border-radius:\s*(\d+)px", html):
            assert int(raio) <= 8, f"{name}: border-radius {raio}px"


EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-⛿✀-➿️]")


def test_sem_emoji_decorativo_na_interface():
    for name, html in PAGES.items():
        encontrados = sorted(set(EMOJI.findall(html)))
        assert not encontrados, f"{name}: emoji {encontrados}"


def test_sem_microcopy_de_marketing():
    proibidos = ("poderoso", "revolucion", "potencializ", "descubra",
                 "inteligentes", "insights", "o melhor da", "solução completa")
    for name, html in PAGES.items():
        baixo = html.lower()
        for termo in proibidos:
            assert termo not in baixo, f"{name}: microcopy promocional '{termo}'"


# ---------------------------------------------------------------------------
# Responsividade e acessibilidade
# ---------------------------------------------------------------------------
def test_todas_as_paginas_tem_viewport_e_esquema_de_cor():
    for name, html in PAGES.items():
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html, name
        assert '<meta name="color-scheme" content="dark">' in html, name


def test_breakpoints_cobrem_desktop_tablet_e_celular():
    index = PAGES["index.html"]
    for breakpoint in ("max-width: 1500px", "max-width: 1100px",
                       "max-width: 900px", "max-width: 560px", "max-height: 800px"):
        assert f"@media ({breakpoint})" in index, breakpoint
    # Grades densas viram coluna única no celular.
    mobile = index[index.index("@media (max-width: 560px)"):]
    assert "grid-template-columns: 1fr" in mobile


def test_tabelas_e_graficos_nao_estouram_a_largura():
    index = PAGES["index.html"]
    assert ".table-wrap" in CSS and "overflow-x: auto" in CSS
    assert '<div class="table-wrap">' in index
    assert "max-width: 100%" in index, "canvas limitado à largura do painel"
    assert "minmax(0, 1fr)" in index, "colunas de grid podem encolher"


def test_acessibilidade_basica():
    index = PAGES["index.html"]
    assert ".skip-link" in CSS and 'class="skip-link"' in index
    assert ":focus-visible" in CSS and "--focus" in CSS
    assert ".visually-hidden" in CSS
    assert "prefers-reduced-motion" in CSS
    # Navegação e regiões nomeadas
    assert '<nav class="nav-tabs" aria-label=' in index
    assert 'role="status"' in index and 'aria-live="polite"' in index


def test_botoes_declaram_type_para_nao_submeterem_formulario():
    for name in ("index.html", "fazendas.html", "auth.html"):
        html = PAGES[name]
        for tag in re.findall(r"<button[^>]*>", html):
            assert "type=" in tag, f"{name}: <button> sem type — {tag[:80]}"


def test_estados_de_carregando_vazio_e_erro_sao_componentes_distintos():
    for classe in (".state-loading", ".state-empty", ".state-error", ".state-ok"):
        assert classe in CSS, classe
    index = PAGES["index.html"]
    assert "state state-loading" in index
    assert "state state-empty" in index
    # A distinção precisa ser visual, não só textual.
    empty_rule = CSS[CSS.index(".state-empty"):CSS.index(".state-error")]
    assert "border-style: dashed" in empty_rule
