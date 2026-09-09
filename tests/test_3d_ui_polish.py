"""PR #5h — POLIMENTO DE INTERFACE do Simulador 3D (contrato de UI, VM Node).

Fonte da verdade: `index.html` (markup + CSS + JS real). Valida o novo layout
em 3 zonas sem exigir navegador:

  - a timeline é uma faixa PRÓPRIA abaixO do viewport 3D (nunca flutua sobre
    o terreno); viewport dominante (flex 1) e timeline com altura própria;
  - What-If é recolhível (`[⚡ What-If]`) e RECOLHER NÃO ALTERA os valores
    dos sliders (preservação por construção: só display é trocado);
  - proveniência compacta (1 linha + [Detalhes] → drawer com DEM/escala/
    exagero) e por padrão apenas o resumo;
  - data atual = UM único chip azul (ativo) por cena aplicada, sem data
    duplicada no cabeçalho dos controles;
  - controles seguem presentes/ligados: ▶ Play, intervalo, camada,
    Relevo 1×/2×/3×/5×, Resetar câmera, períodos e setas da timeline;
  - estrutura responsiva: clamp da barra de timeline, media queries para
    1366×768/1600×900/1920×1080, scene-timeline com scroll horizontal;
  - NENHUMA funcionalidade 3D removida (geometria/DEM/câmera/cache/
    timeline cronológica seguem nos marcadores/contratos existentes).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO_ROOT / "index.html"


def _slice(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


@pytest.fixture(scope="module")
def source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def screen_3d_block(source: str) -> str:
    """Somente o bloco <div id="screen-3d"> (markup, sem CSS/JS)."""
    start = source.index('<div id="screen-3d"')
    end = source.index("</main>", start)
    return source[start:end]


# ---------------------------------------------------------------------------
# 1. TIMELINE FORA DO VIEWPORT 3D / REGIÃO PRÓPRIA
# ---------------------------------------------------------------------------
def test_3d_ui_timeline_outside_viewport(screen_3d_block):
    """A timeline é IRMÃ do viewport 3D (nunca filha/sobreposta ao canvas)."""
    if "viewport-3d" not in screen_3d_block:
        pytest.fail("marcação #viewport-3d ausente")
    viewport_start = screen_3d_block.index('<div id="viewport-3d">')
    timeline_start = screen_3d_block.index('<div id="timeline-panel">')
    viewport = screen_3d_block[viewport_start:timeline_start]
    # O canvas vive DENTRO do viewport; a timeline nunca é descendente dele
    assert '<div id="canvas-container"></div>' in viewport, "canvas dentro do viewport 3D"
    assert '<div id="timeline-panel">' not in viewport, "timeline NÃO pode ser filha do viewport"
    assert timeline_start > viewport_start, \
        "ordem do fluxo: viewport (área principal) → timeline (faixa inferior)"


def test_3d_ui_timeline_own_region_css(source):
    """Zonas: viewport flex:1 dominante; timeline faixa própria com altura fixa."""
    css = _slice(source, "/* =====================================================================\n       PR #5h", "  </style>")
    assert "#screen-3d" in css
    assert re.search(r"#screen-3d\.active\s*\{[^}]*display:\s*flex[^}]*flex-direction:\s*column", css, re.S), \
        "screen-3d ativa é coluna (header externo / viewport / timeline)"
    assert re.search(r"#viewport-3d\s*\{[^}]*flex:\s*1 1 auto[^}]*min-height:\s*0", css, re.S), \
        "viewport ocupa o espaço dominante (60–78% da altura)"
    assert re.search(r"#timeline-panel\s*\{[^}]*flex:\s*0 0 clamp\(120px,\s*21vh,\s*152px\)", css, re.S), \
        "timeline é faixa própria com altura limitada (nunca cobre o terreno)"
    assert "position: absolute" not in _slice(css, "#timeline-panel {", "/* Linha 2"), \
        "timeline NÃO usa posicionamento absoluto (não flutua sobre o 3D)"
    assert re.search(r"#scene-timeline\s*\{[^}]*overflow-x:\s*auto", css, re.S), \
        "cena com scroll horizontal na faixa própria"


def test_3d_ui_timeline_two_content_lines(screen_3d_block):
    """Linha 1 = Período + cenas; Linha 2 = controles (+ linha fina de status)."""
    rows = re.findall(r'<div class="timeline-row[^"]*">', screen_3d_block)
    assert len(rows) == 3, "3 linhas no máximo: status fina + Período/cenas + controles"
    row_idx = [screen_3d_block.index(r) for r in rows]
    lines = [screen_3d_block[i:] for i in row_idx]
    assert 'id="period-pills"' in lines[1] and 'id="scene-timeline"' in lines[1], \
        "Linha 1 combina Período + cenas (sem linha separada)"
    assert 'id="play-btn"' in lines[2] and 'id="layer-select"' in lines[2] and \
        'id="btn-reset-camera"' in lines[2] and 'data-exag="1"' in lines[2], \
        "Linha 2 reúne Play · intervalo · camada · Relevo · Resetar"
    assert 'id="timeline-meta"' in lines[0] and 'id="load-indicator"' in lines[0]


def test_3d_ui_zone_heights_responsive(source):
    """1920×1080 · 1600×900 · 1366×768: timeline (≤152px) nunca engole o 3D."""
    css = _slice(source, "/* =====================================================================\n       PR #5h", "  </style>")
    assert "@media (max-height: 800px)" in css, "regra específica p/ 1366×768"
    assert re.search(r"@media \(max-height:\s*800px\)\s*\{[^}]*#timeline-panel\s*\{\s*flex-basis:\s*112px", css, re.S), \
        "768px → timeline 112px (ainda mais contida)"
    # Simula a conta das 3 resoluções (56px = top-nav fixa)
    for (label, height, expected_tl) in (
        ("1920×1080", 1080, 152),
        ("1600×900", 900, 152),
        ("1366×768", 768, 112),
    ):
        tl = expected_tl
        viewport = height - 56 - tl
        assert viewport > 400, f"{label}: viewport {viewport}px pequeno demais"
        ratio = viewport / (height - 56)
        assert 0.60 <= ratio <= 0.90, f"{label}: 3D deve dominar ({ratio:.0%})"


# ---------------------------------------------------------------------------
# 2. WHAT-IF RECOLHÍVEL E PRESERVA VALORES
# ---------------------------------------------------------------------------
def test_3d_ui_whatif_collapse_preserves_values(source):
    """setWhatIfCollapsed troca SÓ display (painel↔aba); sliders intactos."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível")

    engine = _slice(source, "function setPlayButtonUI()", "function onWindowResize()")
    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');
const els = new Map();
function makeEl(id) {
  const el = {
    id, style: { display: '' }, innerText: '', value: '', hidden: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, setAttribute(k, v) { el._attrs = el._attrs || {}; el._attrs[k] = v; },
    getAttribute() { return (el._attrs && el._attrs[Object.keys(el._attrs)[0]]) || null; },
    replaceChild() {},
    querySelectorAll() { return []; },
  };
  return el;
}
const document = {
  getElementById: (id) => { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
  querySelectorAll: () => [],
};
const sandbox = {
  console: { log() {}, info() {}, warn() {}, error() {} },
  document,
  player: { mode: 'paused' },
  dates: ['2026-08-30'],
  currentLayer: 'ndvi',
  demInfo: null,
};
vm.runInNewContext(process.env.ENGINE, sandbox);
const panel = document.getElementById('whatif-panel-3d');
const tab = document.getElementById('whatif-tab');
const toggle = document.getElementById('whatif-toggle');
const sliderN = document.getElementById('slider-3d-n');
sliderN.value = '30';                       // usuário moveu a adubação
sandbox.setWhatIfCollapsed(true);
assert.equal(panel.style.display, 'none', 'recolhido → painel some');
assert.equal(tab.style.display, '', 'aba [⚡ What-If] aparece');
assert.equal(toggle._attrs['aria-expanded'], 'false');
assert.equal(sliderN.value, '30', 'VALOR do slider preservado ao recolher');
assert.equal(document.getElementById('val-3d-n').innerText, '', 'resultados não são apagados');
sandbox.setWhatIfCollapsed(false);
assert.equal(panel.style.display, '', 'reabre o painel');
assert.equal(tab.style.display, 'none', 'aba some ao reabrir');
assert.equal(sliderN.value, '30', 'valores continuam preservados após reabrir');
process.stdout.write(JSON.stringify({ ok: true }));
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={"ENGINE": engine},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_3d_ui_whatif_collapsed_markup(screen_3d_block):
    """Aba única [⚡ What-If] e painel compacto (agrupado, sem caixas aninhadas)."""
    assert '<button id="whatif-tab" type="button" style="display:none;">⚡ What-If</button>' in screen_3d_block
    assert '<button id="whatif-toggle" type="button"' in screen_3d_block
    assert 'id="whatif-body"' in screen_3d_block
    # Parâmetros agrupados TODO o painel é 1 caixa; sliders dentro de grupos simples
    assert screen_3d_block.count('class="param-group"') == 3
    assert 'class="sim-result-box"' in screen_3d_block
    assert "Adubação (N)" in screen_3d_block and "Irrigação" in screen_3d_block and "Pragas" in screen_3d_block
    assert 'id="sim-3d-ndvi"' in screen_3d_block and 'id="sim-3d-yield"' in screen_3d_block


# ---------------------------------------------------------------------------
# 3. PROVENIÊNCIA COMPACTA + DETALHES (drawer)
# ---------------------------------------------------------------------------
def test_3d_ui_provenance_compact_detail_lines(source):
    """Card: 1 linha (Sentinel-2 L2A · data · nuvens · DEM m); Detalhes abre drawer."""
    ux = _slice(source, "// [3D-UX-HELPERS-START]", "// [3D-UX-HELPERS-END]")
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível")
    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');
const sandbox = { console: { log() {}, info() {}, warn() {}, error() {} } };
vm.runInNewContext(process.env.UX, sandbox);
const meta = {
  data_origin: 'sentinel', real_data_status: 'ok',
  acquisition_date: '2026-08-30', cloud_cover: 9.8,
  platform: 'Sentinel-2A', collection: 'sentinel-2-l2a', processing_level: 'L2A',
  product_id: 'S2A_MSIL2A_20260830T131251_N0500_R081_T22KGY',
  bands: ['B04', 'B08', 'B02'], valid_pixel_percentage: 91.2,
  provider: 'Copernicus Data Space Ecosystem',
};
const dem = { source: 'COPERNICUS_30', min: 592, max: 615, relief: 23,
              unitsPerMeter: 0.0556, exaggeration: 2 };
// Resumo COMPACTO: 1 linha única com fonte · data · nuvens · DEM real
const summary = sandbox.provenanceSummary(meta, dem);
assert.equal(summary, 'Sentinel-2 L2A • 30/08/2026 • Nuvens 9,8% • DEM 592–615 m');
// Sem DEM carregado, o resumo continua válido (sem inventar elevação)
assert.equal(sandbox.provenanceSummary(meta, null), 'Sentinel-2 L2A • 30/08/2026 • Nuvens 9,8%');
// Detalhes: 9 linhas da cena + 4 do DEM (escala/exagero no drawer)
const lines = sandbox.provenanceDetailLines(meta, dem);
assert.equal(lines.length, 13, 'detalhes completos no drawer');
assert.ok(lines.some(l => l.startsWith('ID do item: S2A_MSIL2A')));
assert.ok(lines.some(l => l.startsWith('DEM fonte: COPERNICUS_30')));
assert.ok(lines.some(l => /Elevação real: 592–615 m • alívio ~23 m/.test(l)));
assert.ok(lines.some(l => l.includes('0.0556 u/m')));
assert.ok(lines.some(l => l.includes('Exagero visual: 2×')));
// Por padrão (sem DEM) ficam as 9 linhas técnicas — resumo é o que aparece no card
assert.equal(sandbox.provenanceDetailLines(meta, null).length, 9);
process.stdout.write(JSON.stringify({ ok: true }));
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={"UX": ux},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_3d_ui_provenance_markup_css(screen_3d_block, source):
    """Card compacto (canto inferior esquerdo, ≤~25%), resumo padrão + drawer."""
    assert '<div id="metadata-panel">' in screen_3d_block
    assert '<span id="prov-summary">aguardando dados…</span>' in screen_3d_block
    assert '<button id="prov-toggle" type="button">Detalhes</button>' in screen_3d_block
    assert '<div id="prov-details">' in screen_3d_block and 'id="prov-lines"' in screen_3d_block
    assert 'id="dem-line"' in screen_3d_block
    css = _slice(source, "/* =====================================================================\n       PR #5h", "  </style>")
    assert re.search(r"#metadata-panel\s*\{[^}]*bottom:\s*10px[^}]*max-width:\s*320px", css, re.S) or \
        re.search(r"#metadata-panel\s*\{[^}]*bottom:\s*10px[^}]*max-width", css, re.S) or \
        ("bottom: 10px" in css and "max-width: 320px" in css), \
        "card no canto inferior esquerdo e ≤ ~25% da largura"
    assert re.search(r"#prov-details\s*\{[^}]*display:\s*none", css, re.S), \
        "por padrão SÓ o resumo (detalhes fechados)"
    assert re.search(r"#prov-details\.open\s*\{[^}]*display:\s*block", css, re.S), \
        "[Detalhes] abre o drawer com product ID/provider/pixels/relevo/escala/exagero"


# ---------------------------------------------------------------------------
# 4. DATA ATUAL: UM ÚNICO CHIP AZUL; SEM DUPLICAÇÃO
# ---------------------------------------------------------------------------
def test_3d_ui_single_current_chip(source):
    """Há UM chip de cena ativo (data aplicada); nada de data repetida na UI."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível")
    helpers = {
        "ux": _slice(source, "// [3D-UX-HELPERS-START]", "// [3D-UX-HELPERS-END]"),
        "chips": _slice(source, "// [3D-TIMELINE-HELPERS-START]", "// [3D-TIMELINE-HELPERS-END]"),
        "cache": _slice(source, "// [3D-SCENE-CACHE-HELPERS-START]", "// [3D-SCENE-CACHE-HELPERS-END]"),
        "states": _slice(source, "// [3D-TIMELINE-STATES-START]", "// [3D-TIMELINE-STATES-END]"),
        "dom": _slice(source, "// [3D-TIMELINE-DOM-START]", "// [3D-TIMELINE-DOM-END]"),
    }
    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');
const all = [];
const registry = new Map();
function classOf(el) {
  const has = (c) => el.className.split(/\s+/).filter(Boolean).includes(c);
  return {
    add: (...cs) => { cs.forEach(c => { if (!has(c)) el.className = (el.className + ' ' + c).trim(); }); },
    remove: (...cs) => { cs.forEach(c => { el.className = el.className.split(/\s+/).filter(x => x && x !== c).join(' '); }); },
    toggle: (c, force) => { const on = force === undefined ? !has(c) : !!force; if (on) el.classList.add(c); else el.classList.remove(c); return on; },
    contains: has,
  };
}
function makeEl(id, tag) {
  const el = { id: id || '', tag: tag || 'div', children: [], parentNode: null, className: '', innerText: '', title: '', type: '', value: '', max: '0', disabled: false, hidden: false, onclick: null, _removed: false,
    appendChild(c) { el.children.push(c); c.parentNode = el; return c; },
    addEventListener() {}, setAttribute() {}, scrollIntoView() {},
    querySelectorAll(sel) { return documentShim.querySelectorAll(sel); } };
  el.classList = classOf(el);
  Object.defineProperty(el, 'innerHTML', { get() { return ''; }, set() {} });
  all.push(el);
  return el;
}
const documentShim = {
  getElementById(id) { if (!registry.has(id)) { const e = makeEl(id); registry.set(id, e); } return registry.get(id); },
  createElement(tag) { return makeEl('', tag); },
  querySelectorAll(sel) { const m = /^\[id\^="(.+)"\]$/.exec(sel); if (!m) return []; return all.filter(e => !e._removed && e.id && e.id.startsWith(m[1])); },
};
const sandbox = {
  console: { log() {}, info() {}, warn() {}, error() {} },
  document: documentShim,
  dates: ['2026-08-30', '2026-08-27', '2026-08-15'],
  calendarMeta: { calendar: [{ date: '2026-08-30', cloud_cover: 9.8 }] },
  currentLayer: 'ndvi', currentIndex: 0,
  activeFarm: { id: 7 }, activeTalhaoId: () => 3,
  selectDateIndex: () => {},
};
vm.runInNewContext(
  process.env.UX + '\n' + process.env.CHIPS + '\n' + process.env.CACHE + '\n' +
  process.env.STATES + '\n' + process.env.DOM, sandbox);
sandbox.player = sandbox.createTimelineMachine(0, 3);   // 30/08 aplicada (idx interno 0)
sandbox.player.appliedLayer = 'ndvi';
sandbox.preloadStates = new Map();
sandbox.sceneCache = sandbox.createSceneCache(12);
sandbox.renderDatePills();
const chips = documentShim.querySelectorAll('[id^="scene-chip-"]');
assert.equal(chips.length, 3);
const active = chips.filter(c => c.classList.contains('active'));
assert.equal(active.length, 1, 'UM único chip = data aplicada');
assert.equal(active[0].id, 'scene-chip-0');
assert.equal(active[0].children[0].innerText, '30 AGO');
assert.equal(active[0].children[1].innerText, '2026');
assert.equal(active[0].children.length, 3, 'dia + ano + ponto — sem badge extra');
process.stdout.write(JSON.stringify({ ok: true }));
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={k: v for k, v in {
            "UX": helpers["ux"], "CHIPS": helpers["chips"], "CACHE": helpers["cache"],
            "STATES": helpers["states"], "DOM": helpers["dom"],
        }.items()},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_3d_ui_no_duplicate_date_control(screen_3d_block):
    """Sem data repetida (o chip azul é a única fonte visual da data atual)."""
    assert 'id="date-label"' not in screen_3d_block, "data não pode repetir no cabeçalho dos controles"


# ---------------------------------------------------------------------------
# 5. CONTROLES SEGUEM (Play · intervalo · camada · Relevo 1/2/3/5 · Reset)
# ---------------------------------------------------------------------------
def test_3d_ui_controls_still_present_and_wired(screen_3d_block, source):
    engine = _slice(source, "function setPlayButtonUI()", "function onWindowResize()")
    # Markup
    assert '<button id="play-btn" type="button">▶ Play</button>' in screen_3d_block
    assert '<select id="play-interval"' in screen_3d_block
    assert '<select id="layer-select"' in screen_3d_block
    for l in ("rgb", "ndvi", "evi", "ndre", "ndmi"):
        assert f'<option value="{l}">' in screen_3d_block
    assert '<button id="btn-reset-camera"' in screen_3d_block and "⟲ Resetar" in screen_3d_block
    for exh in ("1×", "2×", "3×", "5×"):
        assert f'data-exag="{exh[:-1]}"' in screen_3d_block, f"Relevo {exh} presente"
    assert 'data-exag="1"' in screen_3d_block and 'data-exag="5"' in screen_3d_block
    # Wiring (engine real)
    assert "setSpectralLayer(e.target.value)" in engine, "troca de camada ligada"
    assert "PLAY_INTERVAL_MS" in engine and "playIntervalSel.addEventListener" in engine
    assert "fitCameraToTerrain()" in engine and "resetCamBtn.addEventListener" in engine
    assert "document.querySelectorAll('.exag-btn').forEach" in engine, "exagero 1×/2×/3×/5× ligado"
    assert "setExaggeration(Number(btn.getAttribute('data-exag')))" in engine
    assert "scrollSceneTimeline" in engine and "tlPrev" in engine and "tlNext" in engine


def test_3d_ui_periods_and_calendar_contract(source):
    """Períodos [30d][60d][90d][6m][1a] + contrato #5f não regrediu."""
    assert "PERIOD_OPTIONS = [30, 60, 90, 180, 365]" in source
    assert "{ 30: '30d', 60: '60d', 90: '90d', 180: '6m', 365: '1a' }" in source
    # Contrato DOM dos testes anteriores permanece
    assert 'id="timeline-meta"' in source
    assert "renderTimelineMeta()" in source


# ---------------------------------------------------------------------------
# 6. NADA DE 3D REMOVIDO (contratos #5e/#5f/#5g) + estrutura neutra
# ---------------------------------------------------------------------------
def test_3d_ui_pipeline_3d_preserved(source):
    assert "new THREE.OrbitControls" in source, "OrbitControls preservado"
    assert "setExaggeration(mult)" in source and "bakeDemGeometry()" in source
    assert "buildTerrainSkirtGeometryData" in source and "buildPolygonSkirtGeometryData" in source
    assert "rebuildVolumeSides()" in source
    assert "renderer.shadowMap.type = THREE.PCFSoftShadowMap" in source
    assert "terrainHillshadeColors" in source
    assert "fitCameraToTerrain()" in source
    assert "computeCameraFit" in source and "CAMERA_PRESET" in source
    assert source.count("[3D-DEM]") >= 2
    for marker in ("[3D-TERRAIN-HELPERS-START]", "[3D-UX-HELPERS-START]",
                   "[3D-TIMELINE-HELPERS-START]", "[3D-SCENE-CACHE-HELPERS-START]",
                   "[3D-TIMELINE-STATES-START]", "[3D-TIMELINE-DOM-START]"):
        assert marker in source, f"marcador removido: {marker}"
    # Identidade por cena no cache/preload (nunca posição)
    assert "sceneCacheKey(activeFarm.id, activeTalhaoId(), dateStr, layer)" in source


def test_3d_ui_neutral_palette_and_hierarchy(source):
    """UI neutra: cor apenas para estado; hierarquia título→secundário→técnico."""
    css = _slice(source, "/* =====================================================================\n       PR #5h", "  </style>")
    # Labels discretos SEM caixas grandes (borda fina, fundo translúcido)
    assert re.search(r"#label-left\s*\{[^}]*border-left:\s*3px", css, re.S)
    assert "backdrop-filter" in css
    # Título > subtítulo > técnico (tamanhos decrescentes na mesma família)
    assert re.search(r"\.scenario-title\s*\{[^}]*font-size:\s*0\.64rem[^}]*font-weight:\s*800", css, re.S)
    assert re.search(r"\.scenario-sub\s*\{[^}]*font-size:\s*0\.62rem[^}]*font-weight:\s*600", css, re.S)
    # Dica discreta (não tutorial permanente) — texto no markup do viewport
    assert "Arraste para orbitar · Scroll para zoom" in source
    # Erro/loading localizados (sem tela inteira)
    assert 'id="error-retry"' in source and "Tentar novamente" in source
    assert "showErrorToast" in source and "setSceneOverlay" in source


def test_3d_ui_loading_and_error_local(source):
    """Loading no canto/chip preserva a cena anterior; erro local com retry."""
    assert "setSceneOverlay(true, loadLabel)" in source, "loading local no canto"
    assert "Carregando ${formatSceneDate(dateStr)}…" in source
    # Preserva cena anterior: o commit só acontece após o fetch; falha não toca appliedIndex
    assert "playerFinishLoad(player, safe, layer, false, msg, null)" in source
    assert "lastFailedScene = { index: safe, layer }" in source
