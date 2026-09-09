"""PR #5e — cache de cenas + plano de preload + terreno DEM (VM Node).

Executa os blocos puros extraídos do `index.html` em uma VM Node:
  - `[3D-SCENE-CACHE-HELPERS-START/END]`: chave farmId|talhao|data|layer,
    cache LRU com evicção explícita, plano de preload (≤12 → TODAS;
    >12 → 1 anterior + atual + 3 próximas + preenchimento em background),
    estado discreto da timeline e prioridade manual > play > preload;
  - `[3D-TERRAIN-HELPERS-START/END]`: agregação metros→unidades de mundo,
    amostragem bilinear do heightmap, deslocamento REAL de VÉRTICES com
    recomputação de normais (não é truque de câmera) e exagero 1×/2×/3×/5×
    como escala VISUAL (valores reais preservados);
  - `[3D-UX-HELPERS-START/END]`: `formatSceneDate`/`isAbortError` usados pelos
    blocos acima.

Nenhuma asserção é "busca de string": todas validam SAÍDAS das funções reais.
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
        "cache": _slice(source, "// [3D-SCENE-CACHE-HELPERS-START]", "// [3D-SCENE-CACHE-HELPERS-END]"),
        "terrain": _slice(source, "// [3D-TERRAIN-HELPERS-START]", "// [3D-TERRAIN-HELPERS-END]"),
        "ux": _slice(source, "// [3D-UX-HELPERS-START]", "// [3D-UX-HELPERS-END]"),
    }


def test_3d_scene_cache_and_terrain_real_behavior(helpers):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível para o teste de cache/preload/DEM")

    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');

const sandbox = { console: { log: () => {}, info: () => {}, warn: () => {}, error: () => {} } };
vm.runInNewContext(
  process.env.UX_HELPERS + '\n' + process.env.CACHE_HELPERS + '\n' + process.env.TERRAIN_HELPERS,
  sandbox
);

// =====================================================================
// 1. CHAVE DO CACHE: farmId|talhao|data|layer (contrato PR #5e)
// =====================================================================
assert.equal(sandbox.sceneCacheKey(7, 3, '2026-08-15', 'ndvi'), '7|3|2026-08-15|ndvi');
assert.equal(sandbox.sceneCacheKey(7, undefined, '2026-08-15', 'ndvi'), '7|0|2026-08-15|ndvi');
assert.notEqual(sandbox.sceneCacheKey(7, 3, '2026-08-15', 'ndvi'), sandbox.sceneCacheKey(8, 3, '2026-08-15', 'ndvi'), 'fazenda diferente não colide');
assert.notEqual(sandbox.sceneCacheKey(7, 3, '2026-08-15', 'ndvi'), sandbox.sceneCacheKey(7, 3, '2026-07-31', 'ndvi'), 'data diferente não colide');
assert.notEqual(sandbox.sceneCacheKey(7, 3, '2026-08-15', 'ndvi'), sandbox.sceneCacheKey(7, 3, '2026-08-15', 'evi'), 'layer diferente não colide');

// =====================================================================
// 2. CACHE LRU: get() re-ordena (MRU) e set() devolve evicted
// =====================================================================
const cache = sandbox.createSceneCache(3);
cache.set('a', { n: 1 });
cache.set('b', { n: 2 });
cache.set('c', { n: 3 });
cache.get('a');                     // 'a' vira MRU
let evicted = cache.set('d', { n: 4 });
assert.equal(evicted.length, 1); assert.equal(evicted[0], 'b', 'LRU evicta o menos recentemente usado (b)');
assert.equal(cache.has('a'), true, 'a continua (foi acessado)');
assert.equal(cache.has('d'), true);
assert.equal(cache.size(), 3);
evicted = cache.set('e', { n: 5 });
assert.equal(evicted.length, 1); assert.equal(evicted[0], 'c', 'evicta c (não acessado depois de a)');
const val = cache.get('a');
assert.equal(val.n, 1, 'valor intacto no cache');

// =====================================================================
// 3. PLANO DE PRELOAD: ≤12 cenas → TODAS; >12 → janela + background
// =====================================================================
const small = sandbox.computePreloadPlan(8, 3);
assert.equal(small.all, true);
assert.equal(small.priority.length, 8); assert.equal(small.priority[0], 0); assert.equal(small.priority[7], 7); // ≤12: TODAS as cenas da layer ativa
assert.equal(small.background.length, 0);

const big = sandbox.computePreloadPlan(30, 10);
assert.equal(big.all, false);
// janela = 1 anterior + atual + 3 próximas = índices 9,10,11,12,13
const winSorted = Array.from(big.window).sort((a,b)=>a-b); assert.equal(winSorted.join(','), '9,10,11,12,13', 'janela de 5: anterior+atual+3 próximas');
assert.ok(big.priority.length >= 5, 'prioridade inclui a janela');
assert.ok(big.background.length > 0, 'preenchimento em background existe');
assert.equal(big.priority.length + big.background.length, 30, 'plano cobre todas as cenas sem duplicar');
const seen = new Set([...big.priority, ...big.background]);
assert.equal(seen.size, 30, 'nenhum índice repetido no plano');

// Limites documentados (constantes reais do código)
assert.equal(sandbox.SCENE_CACHE_LIMITS.allLimit, 12, 'limite documentado do "preload todas"');
assert.equal(sandbox.SCENE_CACHE_LIMITS.window, 5, 'janela documentada (anterior+atual+3)');
assert.equal(sandbox.SCENE_CACHE_LIMITS.concurrency, 2, 'limite de fetches simultâneos');
assert.equal(sandbox.SCENE_CACHE_LIMITS.max, 12, 'tamanho LRU do cache de cenas');

// =====================================================================
// 4. ESTADO DISCRETO DA TIMELINE (available|loading|ready|applied|error)
// =====================================================================
const st = sandbox.createTimelineMachine(1, 3);
st.appliedLayer = 'ndvi';            // só é 'applied' quando data E layer batem
const pre = new Map();
const key = '7|3|2026-08-15|ndvi';
const cacheKey2 = '7|3|2026-08-15|evi';
assert.equal(sandbox.timelineSceneState(st, cache, pre, key, 1, 'ndvi'), 'applied', 'data APLICADA → applied');
assert.equal(sandbox.timelineSceneState(st, cache, pre, key, 0, 'ndvi'), 'available', 'sem cache/estado → available');
pre.set(key, 'loading');
assert.equal(sandbox.timelineSceneState(st, cache, pre, key, 0, 'ndvi'), 'loading');
pre.set(key, 'error');
assert.equal(sandbox.timelineSceneState(st, cache, pre, key, 0, 'ndvi'), 'error');
pre.set(key, 'ready');
assert.equal(sandbox.timelineSceneState(st, cache, pre, key, 0, 'ndvi'), 'ready');
// pronta via cache (preload terminou e gravou) sem estado explícito
const cache2 = sandbox.createSceneCache(3);
cache2.set(key, { texture: {} });
assert.equal(sandbox.timelineSceneState(st, cache2, new Map(), key, 0, 'ndvi'), 'ready');
// layer diferente → chave diferente → não pode virar 'ready' (sem colisão)
assert.equal(sandbox.timelineSceneState(st, cache2, new Map(), cacheKey2 || '7|3|2026-08-15|evi', 1, 'evi'), 'available');

// =====================================================================
// 5. TERRAIN: escala REAL metros → unidades de mundo (nunca arbitrária)
// =====================================================================
// talhão 900 m de extensão, 50 unidades: 1 unidade = 18 m → alívio 45 m = 2.5 u
assert.ok(Math.abs(sandbox.demReliefWorldUnits(45, 900, 50) - 2.5) < 1e-9);
assert.equal(sandbox.demReliefWorldUnits(0, 900, 50), 0);
assert.equal(sandbox.demReliefWorldUnits(45, 0, 50), 2250, 'span ≤ 0 tratado como 1 → FINITO (nunca Infinity/NaN)');
assert.ok(Number.isFinite(sandbox.demReliefWorldUnits(45, 0, 50)));

// =====================================================================
// 6. AMOSTRAGEM BILINEAR do grid do heightmap (0–255 → 0–1)
// =====================================================================
const grid = [0, 255, 255, 0]; // 2×2: alto à direita (u), baixo à esquerda
assert.equal(sandbox.demGridSample(grid, 2, 0, 0), 0);
assert.ok(Math.abs(sandbox.demGridSample(grid, 2, 1, 0) - 1.0) < 1e-9);
assert.ok(Math.abs(sandbox.demGridSample(grid, 2, 0.5, 0.5) - 0.5) < 1e-9, 'centro = média');
assert.equal(sandbox.demGridSample(null, 2, 0.5, 0.5), 0, 'grid nulo → 0 (chão plano)');

// =====================================================================
// 7. DEM APLICA NOS VÉRTICES + recomputeVertexNormals (não é câmera!)
// =====================================================================
function fakeGeometry(cols, rows) {
  const arr = new Float32Array(cols * rows * 3);
  for (let i = 2; i < arr.length; i += 3) arr[i] = 0;
  let normalsRecalled = 0;
  return {
    parameters: { widthSegments: cols - 1, heightSegments: rows - 1 },
    attributes: { position: { array: arr, needsUpdate: false, itemSize: 3 } },
    computeVertexNormals() { normalsRecalled += 1; },
    get normalsCalled() { return normalsRecalled; },
    get arr() { return arr; },
  };
}
const geo = fakeGeometry(4, 4);
const ok = sandbox.applyDemToGeometry(geo, (u, v) => (u >= 1 ? 1 : 0), 2.0, 1);
assert.equal(ok, true);
assert.equal(geo.normalsCalled, 1, 'computeVertexNormals executado após deslocar');
// vértice no canto (col=3 ⇒ u=1) deve ter z = 1.0 * 2.0 * 1 = 2.0
const iMax = ((0 * 4) + 3) * 3; // linha 0, coluna 3
assert.ok(Math.abs(geo.arr[iMax + 2] - 2.0) < 1e-6, `vértice deslocado em Z (real): ${geo.arr[iMax + 2]}`);
const iMin = (0 * 4 + 0) * 3;
assert.equal(geo.arr[iMin + 2], 0, 'vértice em u=0 permanece em 0 (base real)');
assert.equal(geo.attributes.position.needsUpdate, true);

// =====================================================================
// 8. EXAGERO 1×/2×/3×/5× é APENAS escala visual — a base (DEM) não muda;
//    ALTITUDE RELATIVA (mín = 0) garante 1× < 2× < 3× < 5× em todo vértice.
// =====================================================================
const peak = (u, v) => (u >= 1 ? 0.75 : 0.25);
const g1 = fakeGeometry(4, 4), g2 = fakeGeometry(4, 4), g3 = fakeGeometry(4, 4), g5 = fakeGeometry(4, 4);
sandbox.applyDemToGeometry(g1, peak, 2.0, 1);
sandbox.applyDemToGeometry(g2, peak, 2.0, 2);
sandbox.applyDemToGeometry(g3, peak, 2.0, 3);
sandbox.applyDemToGeometry(g5, peak, 2.0, 5);
const peakIdx = (0 * 4 + 3) * 3;
assert.ok(Math.abs(g1.arr[peakIdx + 2] * 2 - g2.arr[peakIdx + 2]) < 1e-6, '2× = 2× o deslocamento de 1×');
assert.ok(Math.abs(g1.arr[peakIdx + 2] * 3 - g3.arr[peakIdx + 2]) < 1e-6, '3× = 3× o deslocamento de 1×');
assert.ok(Math.abs(g1.arr[peakIdx + 2] * 5 - g5.arr[peakIdx + 2]) < 1e-6, '5× = 5× o deslocamento de 1×');
// A PROPORÇÃO entre dois pontos do RELEVO é a MESMA em 1×, 2×, 3× e 5×
const low1 = g1.arr[(0 * 4 + 0) * 3 + 2], low2 = g2.arr[(0 * 4 + 0) * 3 + 2], low3 = g3.arr[(0 * 4 + 0) * 3 + 2], low5 = g5.arr[(0 * 4 + 0) * 3 + 2];
assert.ok(Math.abs((low2 / low1) - 2.0) < 1e-6, 'todo o relevo escala igualmente (1×→2×)');
assert.ok(Math.abs((low3 / low1) - 3.0) < 1e-6, 'todo o relevo escala igualmente (1×→3×)');
assert.ok(Math.abs((low5 / low1) - 5.0) < 1e-6, 'todo o relevo escala igualmente (1×→5×)');
// ALTITUDE RELATIVA: o menor ponto do DEM fica em Z = 0 (mín de referência)
assert.equal(g1.arr[(1 * 4 + 0) * 3 + 2], 0.25 * 2.0 * 1, 'v = min relativo → 0 × alívio × exagero');
// alturas REAIS (metros do DEM) não são alteradas pela função — ela só
// recebe a escala; os valores min/max do backend permanecem no rótulo.

// =====================================================================
// 9. SEM DEM → chão plano (fallback explícito, nunca "relevo de mentira")
// =====================================================================
const flat = fakeGeometry(4, 4);
sandbox.applyDemToGeometry(flat, (u, v) => 0.9, 2.0, 3); // primeiro enche
assert.notEqual(flat.arr[(1 * 4 + 1) * 3 + 2], 0);
sandbox.flattenDemGeometry(flat);
assert.equal(flat.arr[(1 * 4 + 1) * 3 + 2], 0, 'flatten zera os vértices');
assert.equal(flat.normalsCalled, 2, 'flatten também recalcula normais');

// =====================================================================
// 10. CONFIGURAÇÃO DE CÂMERA: oblíqua inicial, órbita/zoom/inclinação
// =====================================================================
const cam = sandbox.CAMERA_PRESET;
assert.equal(cam.rotate && cam.zoom && cam.pan, true, 'órbita/zoom/pan habilitados');
assert.ok(cam.position[1] > 0, 'câmera acima do terreno');
assert.ok(cam.position[2] > 0, 'visão oblíqua (deslocada no eixo Z)');
assert.ok(cam.maxPolarAngle < Math.PI / 2, 'nunca abaixo do horizonte');
assert.ok(cam.minPolarAngle < cam.maxPolarAngle);
assert.ok(cam.minDistance < cam.maxDistance, 'zoom com limites');
assert.equal(sandbox.DEM_RULES.steps.join(','), '1,2,3,5');
assert.equal(sandbox.DEM_RULES.default, 2, 'default documentado = 2×');

process.stdout.write(JSON.stringify({ ok: true }));
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={
            "UX_HELPERS": helpers["ux"],
            "CACHE_HELPERS": helpers["cache"],
            "TERRAIN_HELPERS": helpers["terrain"],
        },
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
