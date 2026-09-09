"""PR #5e (correção) — calendário real STAC × fallback demonstrativo (VM Node).

Extratos REAIS do `index.html`:
  - `[3D-CALENDAR-HELPERS-START/END]`: decisão PURA da fonte da timeline
    (STAC real vs. demonstração) — datas reais SÓ quando `source==='sentinel-cdse'`
    e há cenas; qualquer outro caso vira demo rotulada com motivo;
  - `[3D-CALENDAR-WIRING-START/END]`: `refreshRealTimeline` real — limpa a
    timeline, consulta a API, reconstrói pílulas do zero, seleciona a mais
    recente e só então carrega cena/preload (ou aplica demo rotulada).

Garante os requisitos E–H: resposta real substitui completamente a timeline
anterior; fallback só em cenário permitido e identificado; erro de endpoint
NUNCA apresenta datas demo como se fossem Sentinel reais.
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
        "calendar": _slice(
            source, "// [3D-CALENDAR-HELPERS-START]", "// [3D-CALENDAR-HELPERS-END]"
        ),
        # PR #5f — `latestSceneIndex`/`timelineOrderDates` (semântica visual
        # ASC, identidade interna DESC) usados pelo wiring abaixo.
        "chips": _slice(
            source, "// [3D-TIMELINE-HELPERS-START]", "// [3D-TIMELINE-HELPERS-END]"
        ),
        "wiring": _slice(
            source, "// [3D-CALENDAR-WIRING-START]", "// [3D-CALENDAR-WIRING-END]"
        ),
    }


def test_3d_calendar_decision_pure(helpers):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível para o teste de decisão do calendário")

    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');

const sandbox = { console: { log: () => {}, info: () => {}, warn: () => {}, error: () => {} } };
vm.runInNewContext(process.env.CALENDAR_HELPERS, sandbox);

const DEMO = ['2025-04-07', '2026-03-08', '2025-10-04'];

// ---- A/B/C: STAC com cenas (diferentes da config) → SÓ essas datas ----
const REAL_DATES = ['2026-08-30', '2026-08-27', '2026-07-31'];
const real = sandbox.classifyCalendarResponse(
  { source: 'sentinel-cdse', dates: REAL_DATES, count: REAL_DATES.length,
    latest_date: '2026-08-30', status: 'ok' },
  DEMO, null, null
);
assert.equal(real.kind, 'real');
assert.equal(real.source, 'sentinel-cdse');
assert.deepEqual(Array.from(real.dates), REAL_DATES, 'datas EXATAMENTE as do STAC');
assert.ok(real.dates.every(d => !DEMO.includes(d)), 'nenhuma data da config fixa entra');
assert.equal(real.latest, '2026-08-30', 'latest_date = cena real mais recente');
assert.equal(real.count, 3);
assert.ok(/STAC/i.test(real.source_label));

// ---- no_scene (STAC OK, sem cena útil) → demo com motivo, nunca "real" ----
const none = sandbox.classifyCalendarResponse(
  { source: 'sentinel-cdse', dates: [], status: 'no_scene' }, DEMO, null, null
);
assert.equal(none.kind, 'demo');
assert.equal(none.source, 'config_fallback');
assert.ok(/Sem cena/i.test(none.fallback_reason));
assert.ok(/demonstrativo/i.test(none.source_label));
assert.ok(none.dates.every(d => !REAL_DATES.includes(d)));

// ---- not_configured (sem credenciais) → demo + motivo explícito ----
const nc = sandbox.classifyCalendarResponse(
  { source: 'config_fallback', status: 'not_configured', dates: DEMO }, DEMO, null, null
);
assert.equal(nc.kind, 'demo');
assert.ok(/não configurado/i.test(nc.fallback_reason));

// ---- erro HTTP 500 → demo (nunca são apresentadas como reais) ----
const http = sandbox.classifyCalendarResponse(null, DEMO, 500, null);
assert.equal(http.kind, 'demo');
assert.ok(/Erro HTTP 500/i.test(http.fallback_reason));

// ---- falha de rede → demo + motivo ----
const net = sandbox.classifyCalendarResponse(null, DEMO, null, new TypeError('fetch failed'));
assert.equal(net.kind, 'demo');
assert.ok(/Falha de rede/i.test(net.fallback_reason));

// ---- 401 (sessão) → demo + motivo ----
const auth = sandbox.classifyCalendarResponse(null, DEMO, 401, null);
assert.equal(auth.kind, 'demo');
assert.ok(/Sessão/i.test(auth.fallback_reason));

// ---- resposta vazia/inesperada → NUNCA kind='real' ----
assert.equal(sandbox.classifyCalendarResponse(undefined, DEMO, null, null).kind, 'demo');
assert.equal(sandbox.classifyCalendarResponse({ source: 'config' }, DEMO, null, null).kind, 'demo');
assert.equal(sandbox.classifyCalendarResponse({ source: 'sentinel-cdse', dates: [] }, DEMO, null, null).kind, 'demo');

process.stdout.write(JSON.stringify({ ok: true }));
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={"CALENDAR_HELPERS": helpers["calendar"]},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_3d_calendar_timeline_wiring(helpers):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível para o teste de wiring da timeline")

    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');

// ---------- DOM mínimo (registra texto/imagem exibidos) ----------
const els = {};
function makeEl(id) {
  const e = { id, innerText: '', value: '', max: '0', disabled: false,
              innerHTML: '', hidden: false };
  e.classList = { add(){}, remove(){}, toggle(){}, contains(){ return false; } };
  e.addEventListener = () => {};
  e.setAttribute = () => {};
  return e;
}
function el(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; }
const calls = { fetch: [], logout: 0, pills: 0, select: [], updateTextures: 0, updateTimelineUI: 0 };

const sandbox = {
  console: { log: () => {}, info: () => {}, warn: () => {}, error: () => {} },
  document: { getElementById: el, querySelectorAll: () => [] },
  URLSearchParams,
  API_URL: '/api',
  authHeaders: () => ({ Authorization: 'Bearer t' }),
  AuthService: { logout: () => { calls.logout += 1; } },
  activeFarm: { id: 7 },
  DEMO_DATES: ['2025-04-07', '2026-03-08', '2025-10-04'],
  dates: [],
  calendarMeta: null,
  currentPeriod: 60,
  player: { appliedIndex: 0, appliedMeta: null },
  preloadStates: new Map(),
  preloadQueue: [],
  preloadQueuedKeys: new Set(),
  createTimelineMachine: (i, n) => ({ appliedIndex: i, count: n, appliedMeta: null }),
  renderDatePills: () => { calls.pills += 1; },
  selectDateIndex: (i) => { calls.select.push(i); },
  updateTimelineUI: (i) => { calls.updateTimelineUI += 1; },
  updateTextures: () => { calls.updateTextures += 1; },
  formatSceneDate: (d) => (d ? d.split('-').reverse().join('/') : '—'),
  fetch: async (url, options) => {
    calls.fetch.push({ url, options });
    const mode = sandbox.__fetchMode;
    if (mode === 'real') {
      return { status: 200, ok: true, json: async () => ({
        source: 'sentinel-cdse', is_real: true,
        source_label: 'Copernicus Sentinel-2 — catálogo STAC real',
        dates: ['2026-08-30', '2026-08-27', '2026-07-31'],
        latest_date: '2026-08-30', count: 3, status: 'ok',
      }) };
    }
    if (mode === 'config') {
      return { status: 200, ok: true, json: async () => ({
        source: 'config_fallback', is_real: false,
        source_label: 'Calendário demonstrativo (datas de demonstração)',
        dates: ['2026-03-08', '2025-10-04', '2025-04-07'],
        status: 'not_configured', latest_date: null, count: 0,
      }) };
    }
    if (mode === 'http500') {
      return { status: 500, ok: false, json: async () => ({ detail: 'boom' }) };
    }
    if (mode === 'unauth') {
      return { status: 401, ok: false, json: async () => ({ detail: 'x' }) };
    }
    throw new TypeError('fetch failed');
  },
};

const calendarSource = process.env.CALENDAR_HELPERS;
const chipsSource = process.env.CALENDAR_CHIPS_HELPERS;
const wiringSource = process.env.CALENDAR_WIRING_HELPERS;
vm.runInNewContext(calendarSource + '\n' + chipsSource + '\n' + wiringSource, sandbox);

(async () => {
  // =====================================================================
  // E. RESPOSTA REAL SUBSTITUI COMPLETAMENTE A TIMELINE ANTERIOR
  // =====================================================================
  sandbox.__fetchMode = 'real';
  await sandbox.refreshRealTimeline(60);
  assert.deepEqual(Array.from(sandbox.dates), ['2026-08-30', '2026-08-27', '2026-07-31'],
    'timeline = exatamente as datas do STAC');
  assert.equal(sandbox.calendarMeta.is_real, true);
  assert.equal(sandbox.calendarMeta.latest_date, '2026-08-30');
  assert.equal(sandbox.calendarMeta.count, 3);
  assert.equal(calls.fetch.length, 1);
  assert.ok(calls.fetch[0].url.includes('/talhao/7/dates?period_days=60&limit=24'),
    'consulta o endpoint correto com período/limite');
  assert.ok(el('timeline-meta').innerText.includes('Última aquisição disponível: 30/08/2026'),
    'rodapé mostra a última aquisição real');
  assert.ok(el('timeline-meta').innerText.includes('3 cenas'));
  assert.deepEqual(calls.select, [0], 'cena MAIS RECENTE selecionada automaticamente');
  assert.equal(calls.pills, 1, 'pílulas reconstruídas do zero');
  assert.equal(calls.updateTextures, 0, 'no real, a cena é carregada via selectDateIndex');
  assert.equal(el('btn-latest').disabled, false, '"Mais recente" habilitado com catálogo real');
  assert.equal(el('timeline-slider').max, '2');

  // F/G. FALLBACK SÓ EM CENÁRIO PERMITIDO E IDENTIFICADO (nunca "real")
  calls.fetch.length = 0; calls.pills = 0; calls.select = []; calls.updateTextures = 0;
  sandbox.dates = ['2026-08-30'];  // estado anterior (real) a ser substituído
  sandbox.calendarMeta = { is_real: true, latest_date: '2026-08-30' };
  sandbox.__fetchMode = 'config';
  await sandbox.refreshRealTimeline(60);
  assert.deepEqual(Array.from(sandbox.dates), ['2026-03-08', '2025-10-04', '2025-04-07'],
    'fallback usa APENAS datas de demonstração (desc)');
  assert.equal(sandbox.calendarMeta.is_real, false);
  assert.equal(sandbox.calendarMeta.source, 'config_fallback');
  assert.ok(sandbox.calendarMeta.fallback_reason.includes('não configurado'));
  const metaText = el('timeline-meta').innerText;
  assert.ok(metaText.includes('Calendário demonstrativo'), 'rótulo explícito de demo');
  assert.ok(!metaText.includes('Última aquisição disponível'), 'nunca parece real');
  assert.equal(calls.updateTextures, 1, 'cena demo carregada após a linha do tempo');
  assert.deepEqual(calls.select, [], 'demo não passa por seleção de cena real');
  assert.equal(el('btn-latest').disabled, false); // demo desc: dates[0] = mais recente da demo

  // H. ERRO DE ENDPOINT → DEMO IDENTIFICADA (nunca "real")
  calls.updateTextures = 0;
  sandbox.__fetchMode = 'http500';
  await sandbox.refreshRealTimeline(60);
  assert.equal(sandbox.calendarMeta.is_real, false);
  assert.ok(sandbox.calendarMeta.fallback_reason.includes('Erro HTTP 500'));
  assert.ok(el('timeline-meta').innerText.includes('Calendário demonstrativo'));
  assert.ok(!el('timeline-meta').innerText.includes('Última aquisição disponível'));
  assert.equal(calls.updateTextures, 1);

  // rede fora + 401 → demo rotulada + logout (nunca datas reais)
  sandbox.__fetchMode = 'network';
  await sandbox.refreshRealTimeline(60);
  assert.equal(sandbox.calendarMeta.is_real, false);
  assert.ok(sandbox.calendarMeta.fallback_reason.includes('Falha de rede'));

  sandbox.__fetchMode = 'unauth';
  await sandbox.refreshRealTimeline(60);
  assert.equal(sandbox.calendarMeta.is_real, false);
  assert.ok(sandbox.calendarMeta.fallback_reason.includes('Sessão'));
  assert.equal(calls.logout, 1, '401 mantém o fluxo de logout existente');

  // "Buscando datas Sentinel-2…" aparece enquanto consulta (estado visual)
  sandbox.__fetchMode = 'real';
  const p = sandbox.refreshRealTimeline(60);
  assert.ok(el('timeline-meta').innerText.includes('Buscando datas Sentinel-2…'),
    'estado de busca exibido imediatamente');
  await p;

  process.stdout.write(JSON.stringify({ ok: true }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={
            "CALENDAR_HELPERS": helpers["calendar"],
            "CALENDAR_CHIPS_HELPERS": helpers["chips"],
            "CALENDAR_WIRING_HELPERS": helpers["wiring"],
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
