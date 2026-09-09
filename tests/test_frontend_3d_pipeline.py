"""PR #4 — testes de comportamento real do pipeline 3D no frontend.

Executa os helpers extraídos do `index.html` em uma VM Node (mesma técnica do
`test_frontend_texture_urls.py`): sem DOM real, mas com comportamento real das
funções — URL relativa/absoluta, token no fetch, classificação de erro HTTP,
atribuição de textura ao material, transformação de pixel What-If, mapeamento
do polígono KML para o plano 3D e fallback visual em falha de asset.

Nenhum teste é "busca de string": todas as asserções validam a saída das
funções executadas (fetch/loader/canvas são mocked no mínimo necessário).
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
def helper_sources() -> dict:
    source = INDEX_HTML.read_text(encoding="utf-8")
    return {
        # Bloco PR #3 (isAuthenticatedAssetUrl + getCachedTexture) — intocado
        "loader": _slice(source, "function isAuthenticatedAssetUrl(url)", "\n\n    const matReal"),
        # Bloco PR #4 (helpers puros do pipeline 3D)
        "pipeline": _slice(source, "// [3D-PIPELINE-HELPERS-START]", "// [3D-PIPELINE-HELPERS-END]"),
    }


def test_frontend_3d_pipeline_real_behavior(helper_sources):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível para o teste do pipeline 3D")

    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');

const pipelineSource = process.env.PIPELINE_HELPERS;
const loaderSource = process.env.LOADER_HELPERS;

const fetchCalls = [];
const loadedUrls = [];
const revokedUrls = [];
const loggedOut = [];
const errors = [];
const warnings = [];

let fetchQueue = [];
let lastPutImageData = null;

function makeCtx() {
  return {
    fillStyle: '', strokeStyle: '', lineWidth: 0, font: '', textAlign: '',
    fillRect() {}, strokeRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
    closePath() {}, fill() {}, stroke() {}, setLineDash() {}, fillText() {},
    drawImage() {},
    getImageData(x, y, w, h) {
      const data = new Uint8ClampedArray(w * h * 4);
      for (let i = 0; i < data.length; i += 4) { data[i] = 100; data[i + 1] = 100; data[i + 2] = 100; data[i + 3] = 255; }
      return { data, width: w, height: h };
    },
    putImageData(imgData) { lastPutImageData = imgData; },
  };
}
function makeCanvas() { const c = { width: 0, height: 0, getContext: () => makeCtx() }; return c; }

class BrowserURL extends URL {}
BrowserURL.createObjectURL = () => `blob:mock-${Math.random()}`;
BrowserURL.revokeObjectURL = (url) => revokedUrls.push(url);

const sandbox = {
  URL: BrowserURL,
  Blob,
  API_URL: '/api',
  heightTexture: null,
  window: { location: { href: 'https://frontend.example/dashboard.html' } },
  document: { createElement: () => makeCanvas() },
  console: {
    warn: (...a) => warnings.push(a.join(' ')),
    error: (...a) => errors.push(a.join(' ')),
    info() {},
  },
  THREE: {
    LinearMipmapLinearFilter: 'mipmap',
    LinearFilter: 'linear',
    NoColorSpace: 'no-color-space',
    CanvasTexture: function (canvas) { this.image = canvas; this.isCanvasTexture = true; this.needsUpdate = false; },
  },
  MAX_CACHED_TEXTURES: 8,
  textureCache: new Map(),
  textureInFlight: new Map(),
  textureLoader: {
    load(url, onLoad) {
      loadedUrls.push(url);
      queueMicrotask(() => onLoad({ image: { width: 256, height: 256 } }));
    },
  },
  localStorage: { getItem: () => 'jwt-test-token' },
  AuthService: { logout: () => loggedOut.push(true) },
  fetch: async (url, options) => {
    fetchCalls.push({ url, options });
    const next = fetchQueue.shift() || { status: 200 };
    return {
      status: next.status,
      ok: next.status >= 200 && next.status < 300,
      blob: async () => new Blob(['png-bytes'], { type: 'image/png' }),
    };
  },
};
vm.runInNewContext(`${loaderSource}\n${pipelineSource}`, sandbox);

// ---- 1. Classificação de erro HTTP (estado de erro/fallback) ----
assert.equal(sandbox.classifyAssetHttpError(401), 'auth');
assert.equal(sandbox.classifyAssetHttpError(403), 'permission');
assert.equal(sandbox.classifyAssetHttpError(404), 'not_found');
assert.equal(sandbox.classifyAssetHttpError(500), 'http');

// ---- 2. URL relativa/absoluta autenticada (API relativa → same-origin) ----
assert.equal(
  sandbox.normalizeAssetUrl('http://localhost:8000/api/talhao/2/texture.png?layer=ndvi'),
  '/api/talhao/2/texture.png?layer=ndvi'
);
assert.equal(
  sandbox.normalizeAssetUrl('http://localhost:8000/api/talhao/2/heightmap.png?size=256'),
  '/api/talhao/2/heightmap.png?size=256'
);
sandbox.API_URL = 'http://localhost:8000/api';
assert.equal(
  sandbox.normalizeAssetUrl('http://localhost:8000/api/talhao/2/texture.png?layer=ndvi'),
  'http://localhost:8000/api/talhao/2/texture.png?layer=ndvi'
);
sandbox.API_URL = '/api';

// ---- 3. resolveTextureUrl: payload sem URL → null (nunca 404 silencioso) ----
assert.equal(
  sandbox.resolveTextureUrl({ type: 'native', path_pattern: 'sentinel-21KXQ-{date}/x.png' }, 'ndvi', '2025-04-07'),
  null
);
assert.equal(
  sandbox.resolveTextureUrl({ texture_url: '/api/talhao/2/texture.png?layer=ndvi' }, 'ndvi', '2025-04-07'),
  '/api/talhao/2/texture.png?layer=ndvi'
);

// ---- 4. Textura recebida é EFETIVAMENTE atribuída ao material ----
const mat = { map: null, displacementMap: null, needsUpdate: false };
const tex = { name: 'textura' };
assert.equal(sandbox.applyTextureToMaterial(mat, tex, tex), true);
assert.equal(mat.map, tex);
assert.equal(mat.needsUpdate, true);

// ---- 5. Pixel do cenário What-If (diferença visual real × simulado) ----
const positive = sandbox.computeSimPixel(100, 100, 100, 0.2);
const negative = sandbox.computeSimPixel(100, 100, 100, -0.2);
assert.ok(positive[1] > 100 && positive[0] < 100, 'N+/água → mais verde');
assert.ok(negative[0] > 100 && negative[1] < 100, 'pragas/seca → mais vermelho');
const neutral = sandbox.computeSimPixel(100, 100, 100, 0);
assert.equal(neutral[0], 100);
assert.equal(neutral[1], 100);
assert.equal(neutral[2], 100);

// ---- 6. buildSimulatedCanvasTexture aplica a transformação de verdade ----
const base = { image: { width: 256, height: 256 } };
const simTex = sandbox.buildSimulatedCanvasTexture(base, 0.2);
assert.ok(simTex && simTex.image, 'textura simulada gerada');
assert.ok(lastPutImageData, 'pixels transformados via putImageData');
const applied = lastPutImageData.data;
assert.ok(applied[1] > applied[0], 'verde > vermelho após simulação positiva');

// ---- 7. Fallback visual: falha de asset NUNCA deixa material sem map ----
const matFail = { map: null, displacementMap: null, needsUpdate: false };
fetchQueue = [{ status: 404 }];
(async () => {
  const result = await sandbox.loadTextureOrFallback('k', '/api/talhao/9/texture.png', matFail, matFail);
  assert.equal(result.ok, false);
  assert.ok(result.texture, 'fallback visual criado');
  assert.equal(matFail.map, result.texture, 'material recebeu a textura de fallback');
  assert.equal(matFail.needsUpdate, true);
  assert.ok(
    errors.some((e) => e.includes('falha ao carregar') && e.includes('Textura não encontrada')),
    'erro contextualizado no console'
  );

  // ---- 8. getCachedTexture usa Bearer ao buscar /api relativo (same-origin) ----
  fetchQueue = [{ status: 200 }];
  await sandbox.getCachedTexture('asset', '/api/talhao/2/texture.png?layer=ndvi');
  const lastCall = fetchCalls[fetchCalls.length - 1];
  assert.equal(lastCall.options.headers.Authorization, 'Bearer jwt-test-token');

  // ---- 9. Geometria: polígono real do talhão → plano 3D ----
  const kml = JSON.stringify([
    [-22.301, -50.501], [-22.300, -50.500], [-22.299, -50.501], [-22.300, -50.502],
  ]);
  const g = sandbox.talhaoWorldGeometry(kml, -22.3, -50.5, 30);
  assert.equal(g.points.length, 4);
  assert.ok(g.width > 0 && g.height > 0);
  const maxLatY = Math.max(...g.points.map((p) => p[1]));
  const maxLonX = Math.max(...g.points.map((p) => p[0]));
  assert.equal(g.points[2][1], maxLatY, 'norte (max lat) → +Y');
  assert.equal(g.points[1][0], maxLonX, 'leste (max lon) → +X');

  // Sem KML → elipse (mesma do rasterizador), nunca retângulo genérico.
  const fallback = sandbox.talhaoWorldGeometry(null, -22.3, -50.5, 30);
  assert.equal(fallback.points.length, 32);
  assert.equal(fallback.width, 50);
  assert.equal(fallback.height, 50);

  // KML inválido → null no parser (sem lançar exceção).
  assert.equal(sandbox.parseKmlPolygon('{nao-e-json'), null);

  // ---- 10. createFallbackTexture: objeto de textura válido ----
  const fb = sandbox.createFallbackTexture();
  assert.ok(fb && fb.image, 'textura de fallback criada sem exceção');

  process.stdout.write(JSON.stringify({ ok: true, errors, warnings }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={
            "PIPELINE_HELPERS": helper_sources["pipeline"],
            "LOADER_HELPERS": helper_sources["loader"],
        },
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
