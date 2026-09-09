"""PR #5f — câmera 3D ENQUADRADA + TIMELINE DE CENAS REAIS (VM Node).

Extratos REAIS do `index.html`:
  - `[3D-TERRAIN-HELPERS-START/END]`: `computeCameraFit` (enquadramento
    oblíquo ~35–55° por bounding box, puro e determinístico) + `DEM_RULES`;
  - `[3D-UX-HELPERS-START/END]`: `formatSceneDate` (tooltip);
  - `[3D-TIMELINE-HELPERS-START/END]`: `buildSceneChips`/`sceneChipTitle` —
    UM botão por cena STAC, sem datas intermediárias/fixas;
  - `[3D-SCENE-CACHE-HELPERS-START/END]`: `createSceneCache`/`timelineSceneState`;
  - `[3D-TIMELINE-STATES-START/END]` + `[3D-TIMELINE-DOM-START/END]`:
    render real dos chips + estados discretos (available/loading/ready/applied/error).

Garante §22 (câmera + timeline + cache): visão oblíqua elevada nunca rasante
nem abaixo do plano; distância proporcional ao tamanho do talhão; fit
determinístico (reset restaura a mesma vista); chips = exatamente as datas da
API, clicáveis, com tooltip data+nuvens; destaque "aplicada" só via estado do
player (preload não muda a seleção); reconstrução por período.

PR #5f (sentido cronológico, 2ª rodada): a API segue DESC internamente
(`dates[0]` = mais recente, contrato de `latest_date`/cache); a UI renderiza
ASC — esquerda = mais antiga, direita = mais recente — via `timelineOrderDates`
+ `buildSceneChips` (identidade por `idx` interno, nunca por posição visual);
Play direção antiga → recente, para na mais recente e reinicia pela mais
antiga quando acionado nela; "Mais recente" via `latestSceneIndex`.
"""
import json
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
def helpers() -> dict:
    source = INDEX_HTML.read_text(encoding="utf-8")
    return {
        "terrain": _slice(source, "// [3D-TERRAIN-HELPERS-START]", "// [3D-TERRAIN-HELPERS-END]"),
        "ux": _slice(source, "// [3D-UX-HELPERS-START]", "// [3D-UX-HELPERS-END]"),
        "chips": _slice(source, "// [3D-TIMELINE-HELPERS-START]", "// [3D-TIMELINE-HELPERS-END]"),
        "cache": _slice(source, "// [3D-SCENE-CACHE-HELPERS-START]", "// [3D-SCENE-CACHE-HELPERS-END]"),
        "states": _slice(source, "// [3D-TIMELINE-STATES-START]", "// [3D-TIMELINE-STATES-END]"),
        "dom": _slice(source, "// [3D-TIMELINE-DOM-START]", "// [3D-TIMELINE-DOM-END]"),
    }


def test_3d_camera_fit_pure(helpers):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível para o teste de enquadramento da câmera")

    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');

const sandbox = { console: { log: () => {}, info: () => {}, warn: () => {}, error: () => {} } };
vm.runInNewContext(process.env.TERRAIN_HELPERS, sandbox);

const { computeCameraFit, DEM_RULES } = sandbox;
const PI = Math.PI;

// ---- 1. Vista OBLÍQUA ELEVADA ~35–55°, target no centro, nunca rasante ----
const fit = computeCameraFit(50, 1.0);
assert.ok(Number.isFinite(fit.distance) && fit.distance > 0, 'distância finita');
assert.deepEqual(Array.from(fit.target), [0, 0, 0], 'target = centro do terreno');
assert.ok(fit.position[1] > 0, 'câmera ACIMA do plano');
assert.ok(Math.abs(fit.elevationDeg - 42) < 1e-9, 'elevação inicial = 42°');
assert.ok(fit.elevationDeg >= 35 && fit.elevationDeg <= 55, 'sempre 35–55°');
// polar (ângulo do eixo +Y) ≤ 0.44π → nunca abaixo do horizonte
const polar = Math.atan2(
  Math.hypot(fit.position[0], fit.position[2]), fit.position[1]
);
assert.ok(polar <= PI * 0.44 + 1e-9, `não abaixo do plano (polar=${polar.toFixed(3)}π)`);
assert.ok(polar < PI / 2, 'nunca rasante (polar < 90°)');
assert.ok(Math.abs(Math.hypot(...fit.position) - fit.distance) < 1e-6,
  '|posição| == distância (câmera na esfera do enquadramento)');
assert.ok(fit.position[0] !== 0 && fit.position[2] !== 0,
  'azimute diagonal → largura E profundidade visíveis');

// ---- 2. FIT RESPONDE À BBOX: distância cresce com o tamanho do talhão ----
const small = computeCameraFit(18, 1.0);   // fazenda pequena
const big = computeCameraFit(150, 1.0);    // fazenda grande
assert.ok(big.distance > small.distance * 7, 'distância proporcional à extensão');
assert.ok(big.minDistance > small.minDistance, 'limites de zoom proporcionais');
assert.ok(big.maxDistance >= big.distance, 'maxDistance nunca menor que a vista inicial');

// ---- 3. ASPECT: tela mais estreita → maior distância (cabe no quadro) ----
const narrow = computeCameraFit(50, 0.5);
assert.ok(narrow.distance > fit.distance, 'fov horizontal menor exige mais distância');

// ---- 4. ELEVAÇÃO É CLAMPADA 35–55° (nunca top-down exato nem rasante) ----
assert.equal(computeCameraFit(50, 1, { elevationDeg: 10 }).elevationDeg, 35);
assert.equal(computeCameraFit(50, 1, { elevationDeg: 80 }).elevationDeg, 55);

// ---- 5. DETERMINÍSTICO: resetar visão restaura EXATAMENTE a mesma vista ----
const again = computeCameraFit(50, 1.0);
assert.deepEqual(again.position, fit.position, 'reset idempotente (mesma posição)');
assert.deepEqual(again.target, fit.target);
assert.equal(again.distance, fit.distance);

// ---- 6. CASOS-LIMITE: extensão 1/nula e aspect inválido → finito, seguro ----
const tiny = computeCameraFit(1, 1.0);
assert.ok(Number.isFinite(tiny.distance) && tiny.position.every(Number.isFinite));
const nullExt = computeCameraFit(0, 0);
assert.ok(Number.isFinite(nullExt.distance), 'extent/aspect degenerados nunca NaN/Infinity');

// ---- 7. REGRAS DE CÂMERA EXPORTADAS (testáveis no harness) ----
const cam = DEM_RULES.camera;
assert.ok(cam.elevationDeg >= 35 && cam.elevationDeg <= 55);
assert.ok(cam.fitMargin > 1, 'folga de enquadramento > 1');
assert.ok(cam.maxPolarAngle > 0 && cam.maxPolarAngle < PI / 2, 'limite anti-chão');
assert.ok(cam.minPolarAngle < cam.maxPolarAngle);

process.stdout.write(JSON.stringify({ ok: true }));
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={"TERRAIN_HELPERS": helpers["terrain"]},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_3d_timeline_chips_pure(helpers):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível para o teste dos chips da timeline")

    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');

const sandbox = { console: { log: () => {}, info: () => {}, warn: () => {}, error: () => {} } };
vm.runInNewContext(
  process.env.UX_HELPERS + '\n' + process.env.CHIPS_HELPERS,
  sandbox
);

// ---- 1. UM BOTÃO POR CENA STAC — VISUAL ASC (antiga → recente) ----
// API continua DESC internamente: 0 = mais recente (30/08); a UI reverte.
const REAL = ['2026-08-30', '2026-08-27', '2026-08-15',
              '2026-07-31', '2026-07-26', '2026-07-18'];
const CAL = [
  { date: '2026-08-30', cloud_cover: 9.75 },
  { date: '2026-08-27', cloud_cover: 22.5 },
];
const chips = sandbox.buildSceneChips(REAL, CAL);
assert.equal(chips.length, REAL.length, 'exatamente um chip por cena');
assert.deepEqual(Array.from(chips.map(c => c.date)),
  ['2026-07-18', '2026-07-26', '2026-07-31', '2026-08-15', '2026-08-27', '2026-08-30'],
  'timeline VISUAL: mais antiga → mais recente (ASC)');
assert.deepEqual(Array.from(chips.map(c => c.idx)), [5, 4, 3, 2, 1, 0],
  'identidade = índice no array INTERNO (API DESC), não posição visual');
assert.deepEqual(Array.from(chips.map(c => c.visualIdx)), [0, 1, 2, 3, 4, 5]);
assert.ok(chips.every(c => c.date && c.day && c.month && c.year), 'dia/mês/ano preenchidos');
assert.equal(chips[0].label, '18 JUL', 'primeiro chip visual = mais antiga');
assert.equal(chips[0].subLabel, '2026');
assert.equal(chips[5].label, '30 AGO', 'último chip visual = mais recente');
assert.equal(chips[5].idx, 0, 'a cena mais recente aponta para dates[0] interno');
assert.equal(chips[0].idx, 5, 'a cena mais antiga aponta para o último índice interno');
assert.ok(!chips.some(c => c.date === '2025-04-07'), 'nenhuma data fixa/demo entra');

// ---- 1b. SEM PERDAS NEM DUPLICATAS + helper de ordem explícito ----
assert.equal(new Set(chips.map(c => c.date)).size, REAL.length, 'nenhuma cena duplicada');
assert.deepEqual(Array.from(sandbox.timelineOrderDates(REAL)),
  ['2026-07-18', '2026-07-26', '2026-07-31', '2026-08-15', '2026-08-27', '2026-08-30'],
  'timelineOrderDates = cópia ASC (não muta a API)');
assert.deepEqual(REAL, ['2026-08-30', '2026-08-27', '2026-08-15',
  '2026-07-31', '2026-07-26', '2026-07-18'], 'dates interno permanece DESC (contrato)');

// ---- 1c. "MAIS RECENTE" por latest_date (nunca pela ordem visual) ----
assert.equal(sandbox.latestSceneIndex(REAL, '2026-08-30'), 0, 'latest_date → dates[0]');
assert.equal(sandbox.latestSceneIndex(REAL, '2026-07-18'), 5, 'latest_date explícito vence');
assert.equal(sandbox.latestSceneIndex(REAL, '1999-01-01'), 0, 'não encontrado → contrato DISC (dates[0])');
assert.equal(sandbox.latestSceneIndex(REAL, null), 0);
assert.equal(sandbox.latestSceneIndex([], '2026-08-30'), -1, 'calendário vazio → -1');

// ---- 2. TOOLTIP: data completa + nuvens do catálogo (pt-BR, vírgula) ----
assert.ok(chips[5].title.includes('30/08/2026'), 'tooltip com a data completa');
assert.ok(chips[5].title.includes('Nuvens: 9,8%'), 'nuvens com vírgula decimal');
assert.equal(chips[5].cloud, 9.75, 'nuvens preservadas no chip');
assert.equal(chips[4].cloud, 22.5);
assert.equal(chips[2].cloud, null, 'sem metadado → cloud null');

// ---- 3. DATA SEM METADADO → nuvens '—' (nunca inventa) ----
assert.ok(sandbox.sceneChipTitle('2026-08-15', null).includes('Nuvens: —'));
assert.ok(sandbox.sceneChipTitle('2026-08-15', { cloud_cover: 5.56 }).includes('Nuvens: 5,6%'));

// ---- 4. VAZIO → VAZIO (a timeline nunca nasce com chips fixos) ----
assert.equal(Array.from(sandbox.buildSceneChips([], [])).length, 0);
assert.equal(Array.from(sandbox.buildSceneChips(undefined, [])).length, 0);

// ---- 5. 20+ CENAS: todos os chips gerados, navegáveis ----
const many = Array.from({ length: 24 }, (_, i) =>
  `2026-08-${String(30 - i).padStart(2, '0')}`);
const chipsMany = sandbox.buildSceneChips(many, []);
assert.equal(chipsMany.length, 24, '20+ cenas → 24 chips');
assert.equal(chipsMany[0].date, '2026-08-07', 'começa na mais antiga');
assert.equal(chipsMany.at(-1).date, '2026-08-30', 'termina na mais recente');
assert.equal(chipsMany[0].idx, 23, 'mais antiga = último índice interno');
assert.equal(chipsMany.at(-1).idx, 0, 'mais recente = dates[0] interno');

process.stdout.write(JSON.stringify({ ok: true }));
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={
            "UX_HELPERS": helpers["ux"],
            "CHIPS_HELPERS": helpers["chips"],
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_3d_timeline_dom_states(helpers):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível para o teste DOM dos chips")

    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');

// ---------- MINI-DOM (registro + classes + querySelectorAll [id^=] ) ----------
const all = [];
const registry = new Map();
function classOf(el) {
  const has = (c) => el.className.split(/\s+/).filter(Boolean).includes(c);
  return {
    add: (...cs) => { cs.forEach(c => { if (!has(c)) el.className = (el.className + ' ' + c).trim(); }); },
    remove: (...cs) => {
      cs.forEach(c => {
        el.className = el.className.split(/\s+/).filter(x => x && x !== c).join(' ');
      });
    },
    toggle: (c, force) => {
      const on = force === undefined ? !has(c) : !!force;
      if (on) el.classList.add(c); else el.classList.remove(c);
      return on;
    },
    contains: has,
  };
}
function makeEl(id, tag) {
  const el = {
    id: id || '', tag: tag || 'div', children: [], parentNode: null,
    className: '', innerText: '', title: '', type: '', value: '', max: '0',
    disabled: false, hidden: false, onclick: null, _removed: false,
    appendChild(c) { el.children.push(c); c.parentNode = el; return c; },
    addEventListener() {},
    setAttribute() {},
    scrollIntoView() {},
    // Elementos-reais têm querySelectorAll; o mini-DOM delega ao documento
    // (necessário para o guard de `updateTimelinePillStates`).
    querySelectorAll(sel) { return documentShim.querySelectorAll(sel); },
  };
  el.classList = classOf(el);
  Object.defineProperty(el, 'innerHTML', {
    get() { return el.children.map(c => c.outerHTML || '').join(''); },
    set(v) {
      const clear = (node) => {
        node.children.forEach(clear);
        node._removed = true;
        if (node.id && registry.get(node.id) === node) registry.delete(node.id);
      };
      el.children.forEach(clear);
      el.children.length = 0;
    },
  });
  all.push(el);
  return el;
}
const documentShim = {
  getElementById(id) {
    if (!registry.has(id)) { const e = makeEl(id); registry.set(id, e); }
    return registry.get(id);
  },
  createElement(tag) { return makeEl('', tag); },
  querySelectorAll(sel) {
    const m = /^\[id\^="(.+)"\]$/.exec(sel);
    if (!m) return [];
    return all.filter(e => !e._removed && e.id && e.id.startsWith(m[1]));
  },
};

const calls = { select: [] };
const sandbox = {
  console: { log: () => {}, info: () => {}, warn: () => {}, error: () => {} },
  document: documentShim,
  dates: ['2026-08-30', '2026-08-27', '2026-08-15'],
  calendarMeta: { calendar: [{ date: '2026-08-30', cloud_cover: 9.75 }] },
  currentLayer: 'ndvi',
  currentIndex: 0,
  activeFarm: { id: 7 },
  activeTalhaoId: () => 3,
  selectDateIndex: (i) => { calls.select.push(i); },
};
vm.runInNewContext(
  process.env.UX_HELPERS + '\n' + process.env.CHIPS_HELPERS + '\n' +
  process.env.CACHE_HELPERS + '\n' + process.env.STATES_HELPERS + '\n' +
  process.env.DOM_HELPERS,
  sandbox
);

// player com cena INTERNA 2 (15/08/2026) APLICADA (layer ndvi) — única fonte do destaque.
// Visual ASC: chip[0]=15/08(idx2) · chip[1]=27/08(idx1) · chip[2]=30/08(idx0).
sandbox.player = sandbox.createTimelineMachine(2, 3);
sandbox.player.appliedLayer = 'ndvi';
sandbox.preloadStates = new Map();
sandbox.sceneCache = sandbox.createSceneCache(12);
// cena interna 0 (30/08 = mais recente) já pré-carregada — NUNCA pode virar seleção
sandbox.preloadStates.set('7|3|2026-08-30|ndvi', 'ready');

sandbox.renderDatePills();

// ---- 1. UM CHIP POR DATA DA API, VISUAL ASC (antiga → recente), tooltip real ----
const chips = documentShim.querySelectorAll('[id^="scene-chip-"]');
assert.equal(chips.length, 3, 'um botão por cena real');
assert.deepEqual(Array.from(chips.map(c => c.id)),
  ['scene-chip-2', 'scene-chip-1', 'scene-chip-0'],
  'id = identidade INTERNA (API DESC); DOM renderiza antiga → recente');
assert.equal(chips[0].children[0].innerText, '15 AGO', 'primeiro chip = mais antiga');
assert.equal(chips[2].children[0].innerText, '30 AGO', 'último chip = mais recente');
assert.equal(chips[2].children[1].innerText, '2026');
assert.ok(chips[2].title.includes('30/08/2026') && chips[2].title.includes('9,8%'),
  'tooltip: data completa + nuvens');

// ---- 2. ESTADOS DISCRETOS por IDENTIDADE de cena (não por posição visual) ----
assert.ok(chips[0].classList.contains('applied'), 'cena comitada (15/08) → applied');
assert.ok(chips[0].classList.contains('active'), 'aplicada recebe destaque inequívoco');
assert.ok(chips[2].classList.contains('ready'), 'preload da mais recente (30/08) → ready');
assert.ok(!chips[2].classList.contains('active'), 'PRELOAD NÃO muda a seleção');
assert.ok(chips[1].classList.contains('available'), 'sem estado → available');

// ---- 3. CLIQUE → selectDateIndex da CENA correspondente (índice interno) ----
chips[1].onclick();                       // 27/08 (idx interno 1)
assert.deepEqual(calls.select, [1], 'clique no chip chama a cena certa (por data)');

// ---- 4. TROCA DE PERÍODO → reconstrói do zero, SEMPRE antiga → recente ----
sandbox.dates = ['2026-08-30', '2026-08-27', '2026-08-15', '2026-07-31', '2026-07-26'];
sandbox.renderDatePills();
const rebuilt = documentShim.querySelectorAll('[id^="scene-chip-"]');
assert.equal(rebuilt.length, 5, 'período novo → 5 chips, sem sobras');
assert.deepEqual(Array.from(rebuilt.map(c => c.id)),
  ['scene-chip-4', 'scene-chip-3', 'scene-chip-2', 'scene-chip-1', 'scene-chip-0'],
  'visual ASC: 26/07 → 31/07 → 15/08 → 27/08 → 30/08');
// aplicada (15/08, idx interno 2) continua destacada após o rebuild
assert.ok(rebuilt[2].classList.contains('applied') && rebuilt[2].classList.contains('active'));
assert.equal(rebuilt[2].id, 'scene-chip-2');

// ---- 5. CARREGANDO → loading; erro em OUTRA cena → error (ponto discreto);
//         'error' na cena APLICADA não vence o estado 'applied' ----
sandbox.preloadStates.set('7|3|2026-08-27|ndvi', 'loading');
sandbox.preloadStates.set('7|3|2026-08-15|ndvi', 'error');   // aplicada: vence applied
sandbox.preloadStates.set('7|3|2026-07-26|ndvi', 'error');   // outra cena: error
sandbox.updateTimelinePillStates();
const after = documentShim.querySelectorAll('[id^="scene-chip-"]');
assert.ok(after[3].classList.contains('loading'), '27/08 (visual posição 3) em loading');
assert.ok(after[2].classList.contains('applied'), 'aplicada prevalece sobre erro no mesmo key');
assert.ok(after[2].classList.contains('active'));
assert.ok(!after[2].classList.contains('error'));
assert.ok(after[0].classList.contains('error'), 'erro na 26/07 (mais antiga, visual 0) correto');

process.stdout.write(JSON.stringify({ ok: true }));
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={
            "UX_HELPERS": helpers["ux"],
            "CHIPS_HELPERS": helpers["chips"],
            "CACHE_HELPERS": helpers["cache"],
            "STATES_HELPERS": helpers["states"],
            "DOM_HELPERS": helpers["dom"],
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
