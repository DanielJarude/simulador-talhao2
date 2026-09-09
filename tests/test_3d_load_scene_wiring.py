"""PR #5d — wiring do carregamento assíncrono do Simulador 3D (VM Node).

Extrato REAL do `index.html`: bloco puro `[3D-UX-HELPERS-START/END]` (máquina +
gate) + funções do motor (`selectDateIndex`, `setSpectralLayer`,
`setPlayButtonUI`, `updateTimelineUI`, `schedulePlayNext`, `advanceToNext`,
`pauseTimelinePlay`, `updateTextures`, `loadScene`), executadas com
fetch/AbortController controlados e DOM mínimo mockado.

Cobre (além do teste da máquina, agora no CÓDIGO REAL de wiring):
  - commit da data SOMENTE após a conclusão do fetch (nunca antes);
  - resposta atrasada (gen antiga) NÃO sobrescreve a data mais nova;
  - Pause durante loading cancela e não aplica a resposta tardia;
  - troca de layer congela e retoma o Play;
  - falha HTTP → pausa + toast com retry (retry recupera);
  - What-If atualizado a partir do meta do COMMIT.
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
def sources() -> dict:
    source = INDEX_HTML.read_text(encoding="utf-8")
    return {
        "ux": _slice(source, "// [3D-UX-HELPERS-START]", "// [3D-UX-HELPERS-END]"),
        # seleção manual + troca de layer (antes do downloadPDFReport)
        "selection": _slice(source, "function selectDateIndex(idx)", "function downloadPDFReport()"),
        # motor de carregamento (do setPlayButtonUI até onWindowResize)
        "engine": _slice(source, "function setPlayButtonUI()", "function onWindowResize()"),
        # PR #5e — cache de cenas/plano de preload (o motor usa esses helpers)
        "scene_cache": _slice(source, "// [3D-SCENE-CACHE-HELPERS-START]", "// [3D-SCENE-CACHE-HELPERS-END]"),
    }


def test_3d_load_scene_wiring_real_behavior(sources):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível para o teste de wiring do loadScene")

    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');

// ---------- DOM mínimo ----------
function makeEl(id) {
  const state = { id, innerText: '', value: '' };
  state.classList = {
    add() {}, remove() {}, toggle() {}, contains() { return false; },
  };
  state.addEventListener = () => {};
  state.setAttribute = () => {};
  state.getAttribute = () => null;
  state.replaceChild = () => {};
  return state;
}
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}
const document = {
  getElementById: (id) => el(id),
  querySelectorAll: () => [],
};

// ---------- spies de UI ----------
const ui = {
  loading: [], overlay: [], provSummary: [], provDetails: [],
  sceneStatus: [], simStatus: [], toast: [], retryFn: null,
  simRendered: 0, timelineUpdated: [], appliedToMaterial: [],
};
function spy(name, fn) { return (...args) => { if (fn) fn(...args); return ui[name] && ui[name].push ? ui[name].push(args) : ui[name]; }; }

// ---------- fetch controlado ----------
let fetchQueue = [];
let abortSignals = [];
const fetchCalls = [];
const fetchMock = (url, options = {}) => {
  fetchCalls.push({ url, options });
  abortSignals.push(options.signal);
  const next = fetchQueue.shift() || { status: 500, json: async () => ({}) };
  if (next.delayController) return next.delayController.promise; // promessa manual
  if (options.signal && next.abortOnSignal !== false && next.status >= 200 && next.status < 300) {
    return (async () => {
      await new Promise((resolve) => setTimeout(resolve, 1));
      return { status: next.status, ok: true, json: async () => next.json };
    })();
  }
  return Promise.resolve({ status: next.status, ok: next.status >= 200 && next.status < 300, json: async () => next.json });
};

const applyCalls = [];
const sandbox = {
  // info() é noop: o código da página loga no console e o processo escreve o
  // JSON de resultado no STDOUT — nada além dele pode ir para stdout.
  console: { log: () => {}, info: () => {}, warn: () => {}, error: () => {} },
  document,
  AbortController,
  setTimeout, clearTimeout,
  API_URL: '/api',
  authHeaders: () => ({ Authorization: 'Bearer t' }),
  AuthService: { logout: () => {} },
  resolveTextureUrl: (payload) => (payload && payload.texture_url) || null,
  loadTextureOrFallback: async (key, url, matA, matB, signal) => {
    const tex = { name: 'tex:' + url, url };
    applyCalls.push({ matA, tex });
    return { ok: true, texture: tex };
  },
  createFallbackTexture: () => ({ name: 'fallback' }),
  applyTextureToMaterial: () => {},
  matReal: { name: 'matReal' }, matSim: { name: 'matSim' }, heightTexture: null,
  renderSimulatedTexture: () => { ui.simRendered += 1; },
  calculateImpact: () => {},
  updateZoneCards: () => {},
  loadAnalyticsData: () => {},   // setSpectralLayer dispara analytics (fora do foco aqui)
  fetch: fetchMock,
  PLAY_INTERVAL_MS: 1600,

  setLoadingIndicator: spy('loading'),
  setSceneOverlay: spy('overlay'),
  renderProvenanceSummary: spy('provSummary'),
  renderProvenanceDetails: spy('provDetails'),
  setScenarioStatus: spy('sceneStatus'),
  setSimScenarioStatus: spy('simStatus'),
  showErrorToast: (msg, retryFn) => { ui.toast.push(msg); ui.retryFn = retryFn; },
  hideErrorToast: () => {},
  updateTimelineUI: spy('timelineUpdated'),

  dates: ['2026-08-15', '2026-07-31'],
  currentLayer: 'ndvi',
  activeFarm: { id: 7 },
};
// variáveis de estado do motor (no script real são let do escopo)
sandbox.playTimer = null;
sandbox.activeTextureAbort = null;
sandbox.lastSimBaseMeta = null;
sandbox.lastFailedScene = null;
sandbox.baseTextureForSim = null;
sandbox.sceneLoadGate = null;
sandbox.player = null;
// PR #5e — estado de cache/preload (criado após avaliar os helpers puros)
sandbox.sceneCache = null;
sandbox.sceneFetchInFlight = null;
sandbox.preloadQueue = null;
sandbox.preloadQueuedKeys = null;
sandbox.preloadStates = null;
sandbox.preloadRunning = false;
sandbox.preloadActiveCount = 0;
sandbox.calendarMeta = null; // sem calendário real no harness → sem preload
sandbox.matRealFade = null;  // sem meshes de fade no harness → aplicação direta
sandbox.meshRealFade = null;
sandbox.matSimFade = null;
sandbox.meshSimFade = null;

const uxSource = process.env.UX_HELPERS;
const cacheSource = process.env.SCENE_CACHE_HELPERS;
const selectionSource = process.env.SELECTION_HELPERS;
const engineSource = process.env.ENGINE_HELPERS;
vm.runInNewContext(`${uxSource}\n${cacheSource}\n${selectionSource}\n${engineSource}`, sandbox);
sandbox.sceneLoadGate = sandbox.createSceneLoadGate();
sandbox.player = sandbox.createTimelineMachine(0, sandbox.dates.length);
sandbox.sceneCache = sandbox.createSceneCache(12);
sandbox.sceneFetchInFlight = new Map();
sandbox.preloadQueue = [];
sandbox.preloadQueuedKeys = new Set();
sandbox.preloadStates = new Map();
sandbox.getCachedTexture = async () => ({ name: 'tex-cached' });

const TX_META = {
  data_origin: 'sentinel', real_data_status: 'ok',
  date: '2026-08-15', acquisition_date: '2026-08-15',
  cloud_cover: 2.04, product_id: 'S2B_...', bands: ['B04','B08','B02'],
};

// ============ 1. data só muda APÓS a conclusão (CARREGAR → COMMIT) ============
(async () => {
  let release;
  const gate = { promise: new Promise((res) => { release = res; }) };
  fetchQueue = [{ delayController: gate, status: 200, json: TX_META }];

  const p = sandbox.loadScene(0, 'ndvi', { fromUser: true });
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(sandbox.player.status, 'loading', 'status loading enquanto o fetch não resolve');
  assert.equal(sandbox.player.appliedIndex, 0);
  assert.ok(ui.loading.some(([v]) => v === true), 'spinner de carregamento visível durante o loading');
  assert.ok(ui.overlay.some(([v]) => v === true), 'overlay discreto visível durante o loading');

  release({ status: 200, ok: true, json: async () => TX_META });
  const out = await p;
  assert.equal(out.ok, true, 'committed com sucesso');
  assert.equal(sandbox.player.status, 'ready');
  assert.equal(sandbox.player.appliedIndex, 0);
  assert.ok(ui.loading.some(([v]) => v === false), 'spinner escondido após aplicar');
  assert.ok(ui.provSummary.length >= 1, 'proveniência resumida renderizada do meta do COMMIT');
  assert.ok(ui.sceneStatus.length >= 1, 'status principal do cenário atualizado');
  assert.ok(ui.simStatus.length >= 1, 'status do cenário simulado atualizado');
  assert.equal(sandbox.lastSimBaseMeta.date, '2026-08-15', 'What-If usa a data do COMMIT');
  assert.equal(sandbox.lastSimBaseMeta.layer, 'ndvi');
  assert.equal(sandbox.player.appliedMeta.product_id, 'S2B_...');

  // ============ 2. resposta ATRASADA não sobrescreve a mais nova ============
  // (PR #5e: cena já pronta viraria cache-hit; aqui o cache é limpo para
  //  forçar o cenário de RACE real entre duas requisições em voo.)
  sandbox.sceneCache = sandbox.createSceneCache(12);
  sandbox.preloadStates.clear();
  let releaseOld;
  const oldGate = { promise: new Promise((res) => { releaseOld = res; }) };
  fetchQueue = [
    { delayController: oldGate, status: 200, json: TX_META },          // load A (lento)
    { status: 200, json: { ...TX_META, date: '2026-07-31', acquisition_date: '2026-07-31' } }, // B
  ];
  const pA = sandbox.loadScene(0, 'ndvi');           // gen A
  const pB = sandbox.loadScene(1, 'ndvi');           // gen B (mais nova, cancela A)
  const outB = await pB;
  assert.equal(outB.ok, true);
  assert.equal(sandbox.player.appliedIndex, 1, 'data nova aplicada');
  assert.equal(sandbox.player.appliedMeta.date, '2026-07-31');
  releaseOld({ status: 200, ok: true, json: async () => ({ ...TX_META, date: '2026-08-15' }) });
  const outA = await pA;
  assert.equal(outA.cancelled, true, 'resposta antiga é descartada');
  assert.equal(sandbox.player.appliedIndex, 1, 'resposta atrasada NUNCA substitui a data atual');
  assert.equal(sandbox.player.appliedMeta.date, '2026-07-31', 'metadados continuam os da textura aplicada');
  assert.equal(sandbox.lastSimBaseMeta.date, '2026-07-31', 'What-If permanece na data aplicada');

  // ============ 3. Pause durante loading cancela e não aplica a tardia ============
  let releasePause;
  const pauseGate = { promise: new Promise((res) => { releasePause = res; }) };
  fetchQueue = [{ delayController: pauseGate, status: 200, json: { ...TX_META, date: '2026-08-15' } }];
  const pPause = sandbox.loadScene(0, 'ndvi');
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(sandbox.player.status, 'loading');
  sandbox.pauseTimelinePlay();
  assert.equal(sandbox.player.mode, 'paused');
  assert.equal(sandbox.player.status, 'ready', 'após pause com cena aplicada → ready (nunca preso)');
  assert.equal(sandbox.player.appliedIndex, 1, 'cena aplicada intacta');
  releasePause({ status: 200, ok: true, json: async () => ({ ...TX_META, date: '2026-08-15' }) });
  const outPause = await pPause;
  assert.equal(outPause.cancelled, true, 'resposta pós-pause é descartada');
  assert.equal(sandbox.player.appliedIndex, 1, 'pausa não aplica a carga cancelada');
  assert.equal(sandbox.player.mode, 'paused', 'pausa NÃO retoma sozinho');

  // ============ 4. falha HTTP → pausa + toast com retry recuperável ============
  fetchQueue = [
    { status: 500, json: async () => ({ detail: 'boom' }) },
    { status: 200, json: { ...TX_META, date: '2026-08-15', acquisition_date: '2026-08-15' } },
  ];
  const pFail = sandbox.loadScene(0, 'ndvi');
  const outFail = await pFail;
  assert.equal(outFail.ok, false, 'falha reconhecida (nunca silenciosa)');
  assert.equal(sandbox.player.status, 'error');
  assert.equal(sandbox.player.mode, 'paused', 'erro pausa automaticamente');
  assert.equal(sandbox.player.appliedIndex, 1, 'falha nunca muda a data principal');
  assert.ok(ui.toast.length >= 1, 'toast compacto de erro exibido');
  assert.ok(ui.toast[ui.toast.length - 1].includes('15/08/2026'), 'erro contextualizado com a data');
  assert.equal(typeof ui.retryFn, 'function', 'retry disponível');
  ui.retryFn();                                  // "Tentar novamente"
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(sandbox.player.status, 'ready', 'retry recupera o estado');
  assert.equal(sandbox.player.appliedIndex, 0, 'retry aplica a cena da falha');
  assert.equal(sandbox.player.error, null);

  // ============ 5. layer swap congela e retoma o Play ============
  // PR #5f (cronológico): o Play só tem para onde avançar quando a cena
  // aplicada NÃO é a mais recente. Aplica a cena ANTIGA (31/07, idx 1) antes.
  fetchQueue = [{ status: 200, json: { ...TX_META, date: '2026-07-31', acquisition_date: '2026-07-31' } }];
  const pOldBase = await sandbox.loadScene(1, 'ndvi');
  assert.equal(pOldBase.ok, true);
  assert.equal(sandbox.player.appliedIndex, 1, 'cena antiga (31/07) aplicada');
  sandbox.playerPlay(sandbox.player);
  fetchQueue = [{ status: 200, json: { ...TX_META, date: '2026-07-31' } }];
  sandbox.setSpectralLayer('evi');  // troca manual (Play ativo)
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(sandbox.player.appliedLayer, 'evi', 'layer trocada e aplicada');
  assert.equal(sandbox.player.status, 'ready');
  assert.equal(sandbox.player.mode, 'playing', 'Play preservado através da troca');
  assert.equal(sandbox.playerCanAdvance(sandbox.player), true, 'após aplicar, o Play pode avançar');
  assert.equal(sandbox.playerNextIndex(sandbox.player), 0,
    'após a troca, o Play avança na direção temporal (31/07 → 15/08)');
  // Determinismo p/ os cenários de cache/preload: zera timers e estado.
  sandbox.pauseTimelinePlay();
  sandbox.sceneCache = sandbox.createSceneCache(12);
  sandbox.sceneFetchInFlight = new Map();
  sandbox.preloadStates.clear();
  sandbox.preloadQueue = [];
  sandbox.preloadQueuedKeys.clear();
  sandbox.preloadActiveCount = 0;
  sandbox.calendarMeta = null;

  // ============ 6. cena PRONTA no cache → aplica SEM nenhum fetch ============
  const cachedTexture = { name: 'tex-cache-hit', url: '/api/talhao/7/texture.png?layer=ndvi' };
  const key0 = sandbox.sceneCacheKey(7, 0, '2026-08-15', 'ndvi');
  sandbox.sceneCache.set(key0, {
    key: key0, farmId: 7, talhaoId: 0, date: '2026-08-15', layer: 'ndvi',
    texture: cachedTexture, meta: { ...TX_META, date: '2026-08-15', acquisition_date: '2026-08-15' },
    dataOrigin: 'sentinel', realDataStatus: 'ok', acquisitionDate: '2026-08-15',
    cloudCover: 2.04, preparedAt: 1,
  });
  fetchCalls.length = 0;
  const pCached = sandbox.loadScene(0, 'ndvi');
  const outCached = await pCached;
  assert.equal(outCached.ok, true);
  assert.equal(outCached.fromCache, true, 'cena vinda do cache identificada');
  assert.equal(fetchCalls.length, 0, 'NENHUM request de rede para cena pronta');
  assert.equal(sandbox.player.appliedIndex, 0);
  assert.equal(sandbox.player.appliedLayer, 'ndvi');
  assert.equal(sandbox.player.status, 'ready');
  assert.equal(sandbox.lastSimBaseMeta.date, '2026-08-15', 'What-If usa a cena em cache');

  // ============ 7. PRELOAD em background NÃO altera a cena aplicada ============
  sandbox.calendarMeta = { source: 'sentinel-cdse' };  // habilita preload pós-commit
  fetchQueue = [{ status: 200, json: { ...TX_META, date: '2026-07-31', acquisition_date: '2026-07-31' } }];
  const preKey = sandbox.sceneCacheKey(7, 0, '2026-07-31', 'ndvi');
  sandbox.enqueuePreload(1, 'ndvi', 'preload');
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(sandbox.player.appliedIndex, 0, 'preload NUNCA aplica cena');
  assert.equal(sandbox.player.status, 'ready', 'estado da cena principal intacto');
  assert.equal(sandbox.preloadStates.get(preKey), 'ready', 'preload concluiu e marcou pronta');
  assert.equal(sandbox.sceneCache.has(preKey), true, 'cena pré-carregada no cache');

  // ============ 8. Play avança ANTIGA → RECENTE (cronológico) sem request ============
  // `dates` interno é DESC (contrato: 0 = 15/08 = mais recente; 1 = 31/07).
  // Aplica a cena ANTIGA (1) do cache (pronta pela seção 7) e avança p/ 0.
  fetchCalls.length = 0;
  const pOld = await sandbox.loadScene(1, 'ndvi');           // 31/07 aplicada
  assert.equal(pOld.fromCache, true, 'cena antiga vem do cache (sem refetch)');
  assert.equal(sandbox.player.appliedIndex, 1);
  assert.equal(fetchCalls.length, 0, 'load da cena pronta NÃO refaz request');
  sandbox.playerPlay(sandbox.player);
  await sandbox.advanceToNext();
  assert.equal(sandbox.player.appliedIndex, 0, 'Play avança para a cena MAIS RECENTE (15/08)');
  assert.equal(sandbox.player.appliedLayer, 'ndvi');
  assert.equal(sandbox.player.status, 'ready');
  assert.equal(fetchCalls.length, 0, 'Play entre cenas prontas NÃO refaz request');
  // Chegou à mais recente → PÁRA (sem loop) e o botão volta a "▶ Play".
  sandbox.playerPlay(sandbox.player);   // ainda playing → testa o fim da linha
  await sandbox.advanceToNext();
  assert.equal(sandbox.player.appliedIndex, 0, 'mais recente mantida (não volta à antiga)');
  assert.equal(sandbox.player.status, 'ready');
  assert.equal(sandbox.player.mode, 'paused', 'Play PAROU ao chegar na mais recente (sem loop)');
  sandbox.pauseTimelinePlay();          // limpa timers antes de sair

  // ============ 9. textura descartada (VRAM LRU) invalida a cena ============
  sandbox.sceneCache = sandbox.createSceneCache(12);
  const vramKey = '7|ndvi|http://t/tex.png';
  sandbox.sceneCache.set(key0, {
    key: key0, farmId: 7, talhaoId: 0, date: '2026-08-15', layer: 'ndvi',
    texture: cachedTexture, textureKey: vramKey, meta: { ...TX_META },
  });
  const removed = sandbox.evictSceneEntriesForTexture(vramKey);
  assert.equal(removed, 1, 'o cache de cenas detecta a textura disposta');
  assert.equal(sandbox.sceneCache.has(key0), false,
    'cena NUNCA fica "pronta" apontando para textura sem VRAM');

  process.stdout.write(JSON.stringify({ ok: true }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={
            "UX_HELPERS": sources["ux"],
            "SCENE_CACHE_HELPERS": sources["scene_cache"],
            "SELECTION_HELPERS": sources["selection"],
            "ENGINE_HELPERS": sources["engine"],
        },
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
