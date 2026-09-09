"""PR #5g — REGRESSÃO P0: ordem REAL de inicialização do bootstrap da página.

O bug (commit cb913a4): `updateGroundGrid(70)` é chamada no TOP-LEVEL do script
inline (antes de `let terrainGeometry` ser executado). Como `updateGroundGrid`
passou a ler `terrainGeometry` (PR #5g), o acesso a uma binding `let` em Temporal
Dead Zone lançou `ReferenceError: Cannot access 'terrainGeometry' before
initialization` e abortou TODA a avaliação do script — Dashboard/clima/gráficos
ficaram em "Carregando…".

Este teste NÃO é grep: ele executa o script inline REAL na ordem do navegador em
uma VM Node com stubs de DOM/THREE/Leaflet/Chart, com estes objetivos:
  1. nenhuma função executada no bootstrap acessa `let/const` antes da
     inicialização (a avaliação completa do script não pode lançar);
  2. `updateGroundGrid` pode ser chamada no estado inicial (terrainGeometry =
     null) sem lançar e usa o fallback de altura;
  3. o bootstrap chega ao ÚLTIMO statement (loadActiveFarm()) — sem isso o
     Dashboard não inicializa;
  4. nenhum ReferenceError/TypeError/SyntaxError é registrado durante a
     avaliação e nas microtasks seguintes (fluxos async do loadActiveFarm).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO_ROOT / "index.html"

_BOOTSTRAP_OK_MARKER = "\n;globalThis.__BOOTSTRAP_OK = true;\n"


def _inline_script(source: str) -> str:
    scripts = re.findall(r"<script>(.*?)</script>", source, re.S)
    return max(scripts, key=len)


def _extract_function(source: str, name: str) -> str:
    """Extrai a função pelo nome (casamento de chaves balanceadas)."""
    start = source.index(f"function {name}(")
    i = source.index("{", start)
    depth = 0
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[start:j + 1]
    raise AssertionError(f"função {name} não encontrada")


@pytest.fixture(scope="module")
def inline_script() -> str:
    return _inline_script(INDEX_HTML.read_text(encoding="utf-8"))


# =============================================================================
# Stubs mínimos de ambiente (DOM, THREE, Leaflet, Chart) para executar o script
# inline inteiro na ordem do navegador. O objetivo é detectar TDZ/erros de
# ORDEM de inicialização — não reproduzir o render WebGL.
# =============================================================================
_STUB_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('node:assert/strict');

function makeEl(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    className: '', innerText: '', innerHTML: '', textContent: '',
    id: '', value: '', max: '', title: '', hidden: false, disabled: false,
    type: '', onclick: null, clientWidth: undefined, clientHeight: undefined,
    width: 0, height: 0, style: { cssText: '', setProperty() {} },
    classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { return c; }, removeChild() {}, replaceChild() {}, remove() {},
    cloneNode() { return makeEl(tag); },
    setAttribute() {}, getAttribute() { return null; },
    scrollIntoView() {}, scrollBy() {},
    parentNode: { replaceChild() {} },
    querySelectorAll() { return []; },
    getContext() { return null; },
    toDataURL() { return 'data:image/png;base64,'; },
  };
  return el;
}
const els = new Map();
const documentStub = {
  getElementById(id) {
    if (!els.has(id)) els.set(id, makeEl('div'));
    return els.get(id);
  },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl(tag); },
  body: { appendChild() {} },
  addEventListener() {},
};

function vec3() {
  return {
    x: 0, y: 0, z: 0,
    set(x, y, z) { this.x = x; this.y = y; this.z = z; return this; },
    normalize() { return this; },
    multiplyScalar() { return this; },
    toArray() { return [this.x, this.y, this.z]; },
  };
}

function fakePlane(w, h, segX, segY) {
  const cols = segX + 1, rows = segY + 1;
  const pos = new Float32Array(cols * rows * 3);
  const idx = [];
  const v = (ix, iy) => iy * cols + ix;
  for (let iy = 0; iy < rows; iy++) {
    for (let ix = 0; ix < cols; ix++) {
      const i = (iy * cols + ix) * 3;
      pos[i] = (ix / (cols - 1) - 0.5) * w;
      pos[i + 1] = (iy / (rows - 1) - 0.5) * h;
      pos[i + 2] = 0;
    }
  }
  for (let iy = 0; iy < segY; iy++) {
    for (let ix = 0; ix < segX; ix++) {
      const a = v(ix, iy), b = v(ix + 1, iy), c = v(ix, iy + 1), d = v(ix + 1, iy + 1);
      idx.push(a, c, b, b, c, d);
    }
  }
  return {
    parameters: { widthSegments: segX, heightSegments: segY },
    attributes: {
      position: { array: pos, needsUpdate: false, itemSize: 3 },
      color: { array: null, needsUpdate: false, itemSize: 3 },
    },
    index: { array: new Uint32Array(idx) },
    userData: {},
    setIndex(list) { this.index = { array: new Uint32Array(list) }; },
    setAttribute(name, attr) { this.attributes[name] = attr; },
    computeVertexNormals() {},
    computeBoundingBox() {},
    dispose() {},
  };
}

function meshLike(geo, mat) {
  return {
    geometry: geo, material: mat,
    rotation: { x: 0 }, position: vec3(),
    visible: true, renderOrder: 0, castShadow: false, receiveShadow: false,
    frustumCulled: true,
  };
}

function lightLike() {
  return {
    position: vec3(), castShadow: false,
    shadow: { mapSize: { set() {} }, camera: {}, bias: 0, radius: 0 },
  };
}

function THREEStub() {
  const THREE = {};
  THREE.DoubleSide = 2;
  THREE.PCFSoftShadowMap = 2;
  THREE.LinearFilter = 1006;
  THREE.LinearMipmapLinearFilter = 1008;
  THREE.NoColorSpace = '';
  THREE.WebGLRenderer = function () {
    return {
      domElement: makeEl('canvas'),
      shadowMap: { enabled: false, type: 0 },
      info: { render: { calls: 0, triangles: 0 } },
      setPixelRatio() {}, setSize() {}, autoClear: true,
      render() {}, clear() {}, setViewport() {}, setScissor() {}, setScissorTest() {},
    };
  };
  THREE.Color = function () { return {}; };
  THREE.Scene = function () { return { background: null, add() {}, remove() {} }; };
  THREE.PerspectiveCamera = function () { return { position: vec3(), aspect: 1, updateProjectionMatrix() {} }; };
  THREE.OrbitControls = function (cam, dom) {
    return {
      target: vec3(), enableDamping: false, enableRotate: false, enableZoom: false,
      enablePan: false, minDistance: 0, maxDistance: 0, minPolarAngle: 0,
      maxPolarAngle: 0, rotateSpeed: 0, zoomSpeed: 0, panSpeed: 0, update() {},
    };
  };
  THREE.AmbientLight = lightLike;
  THREE.HemisphereLight = lightLike;
  THREE.DirectionalLight = lightLike;
  THREE.GridHelper = function () {
    return { position: vec3(), geometry: { dispose() {} }, material: [{ transparent: false, opacity: 1, depthWrite: true }] };
  };
  THREE.PlaneGeometry = fakePlane;
  THREE.TextureLoader = function () { return { setCrossOrigin() {}, load() {} }; };
  THREE.CanvasTexture = function () { return {}; };
  THREE.MeshStandardMaterial = function (opts) {
    return Object.assign({ map: null, needsUpdate: false, transparent: false, opacity: 1, depthWrite: true, color: { setRGB() {} }, dispose() {} }, opts);
  };
  THREE.MeshLambertMaterial = function (opts) { return Object.assign({ dispose() {} }, opts); };
  THREE.LineBasicMaterial = function (opts) { return Object.assign({ dispose() {} }, opts); };
  THREE.Mesh = meshLike;
  THREE.Line = meshLike;
  THREE.LineSegments = meshLike;
  THREE.BufferGeometry = function () {
    return {
      attributes: {},
      setAttribute(n, a) { this.attributes[n] = a; },
      setIndex(i) { this.index = { array: i.array || i }; },
      computeVertexNormals() {},
      setFromPoints() { return this; },
      dispose() {},
    };
  };
  THREE.BufferAttribute = function (array, itemSize) { return { array, itemSize, needsUpdate: false }; };
  THREE.Vector3 = function (x, y, z) { return vec3().set(x || 0, y || 0, z || 0); };
  THREE.Box3 = function () {
    const b = { _min: null, _max: null };
    b.setFromObject = function (obj) {
      const t = obj && obj.geometry && obj.geometry.userData && obj.geometry.userData.terrain;
      const s = t && t.worldStats;
      if (s) {
        b._min = { x: s.minX, y: t.baseY, z: s.minZ };
        b._max = { x: s.maxX, y: s.maxY, z: s.maxZ };
      } else {
        b._min = { x: -25, y: 0, z: -25 };
        b._max = { x: 25, y: 0, z: 25 };
      }
      return b;
    };
    b.getSize = function (v) {
      return v.set(
        this._max.x - this._min.x,
        this._max.y - this._min.y,
        this._max.z - this._min.z
      );
    };
    b.getCenter = function (v) {
      return v.set(
        (this._max.x + this._min.x) / 2,
        (this._max.y + this._min.y) / 2,
        (this._max.z + this._min.z) / 2
      );
    };
    return b;
  };
  THREE.AxesHelper = function () { return { position: vec3() }; };
  THREE.Box3Helper = function () { return {}; };
  THREE.WireframeGeometry = function () { return { dispose() {} }; };
  return THREE;
}

function makeSandbox(records) {
  const windowStub = {
    location: { search: '', href: 'http://localhost/' },
    innerWidth: 1200, innerHeight: 800, devicePixelRatio: 1,
    addEventListener() {}, requestAnimationFrame(cb) { return 0; },
  };
  const sandbox = {
    console: {
      log() {}, info() {},
      warn(msg) { records.push('warn:' + String(msg)); },
      error(msg, extra) {
        records.push('error:' + String(msg) + (extra ? ' :: ' + (extra && extra.message || extra) : ''));
      },
    },
    document: documentStub,
    window: windowStub,
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    navigator: { userAgent: 'node-test' },
    fetch: async () => { const e = new Error('stub fetch'); e.name = 'Error'; throw e; },
    setTimeout, clearTimeout, setInterval, clearInterval,
    AbortController, URLSearchParams, URL, Blob: function () {},
    THREE: THREEStub(),
    L: {
      map() { return { setView() { return this; }, invalidateSize() {}, removeLayer() {}, fitBounds() {} }; },
      tileLayer() { return { addTo() { return this; } }; },
      marker() { return { addTo() { return this; }, setLatLng() { return this; } }; },
      polygon() { return { addTo() { return this; }, getBounds() { return {}; } }; },
    },
    Chart: function () {},
    FarmService: { getFarmById: async () => null },
    SatelliteService: { getTalhaoHeightmap: async () => null },
    SimulationService: { calculateWhatIf: async () => ({ delta_ndvi: 0 }) },
    ReportService: { downloadFarmPdf: async () => {} },
    AuthService: { logout() {} },
    API_URL: '/api',
    authHeaders: () => ({}),
  };
  return sandbox;
}

function fatalErrorInRecords(records) {
  for (const r of records) {
    if (/ReferenceError|TypeError|SyntaxError|before initialization/.test(r)) return r;
  }
  return null;
}

async function main(mode, extra) {
  const records = [];
  const sandbox = makeSandbox(records);
  if (mode === 'full') {
    const code = fs.readFileSync(process.env.INLINE_FILE, 'utf8');
    vm.runInNewContext(code + process.env.BOOTSTRAP_OK_MARKER, sandbox);
    // deixa as microtasks/macrotasks do loadActiveFarm (fetch stub etc.) fluírem
    await new Promise((r) => setTimeout(r, 80));
    assert.equal(sandbox.__BOOTSTRAP_OK, true,
      'bootstrap NÃO chegou ao final do script inline (avaliação abortada)');
    assert.equal(fatalErrorInRecords(records), null,
      'erro fatal de inicialização registrado: ' + fatalErrorInRecords(records));
    return { ok: true, records: records.slice(0, 6) };
  }
  if (mode === 'groundgrid') {
    // Mini-ordem idêntica ao bootstrap: terrainGeometry declarada ANTES da
    // chamada inicial de updateGroundGrid; estado inicial deve ser seguro.
    vm.runInNewContext(extra + '\nupdateGroundGrid(60);', sandbox);
    assert.ok(sandbox.__GG_POS !== undefined, 'grade inicial executou');
    return { ok: true, pos: sandbox.__GG_POS };
  }
  throw new Error('modo desconhecido');
}

main(process.env.MODE, process.env.EXTRA_CODE || '')
  .then((r) => process.stdout.write(JSON.stringify(r)))
  .catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_3d_bootstrap_order_full_inline_script_no_tdz(inline_script, tmp_path):
    """1/3/4 — o script inline inteiro executa na ordem do browser até o final
    sem ReferenceError/TypeError/SyntaxError (TDZ de terrainGeometry abortava
    tudo — Dashboard incluído)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível")
    script_file = tmp_path / "inline_bootstrap.js"
    script_file.write_text(inline_script, encoding="utf-8")
    result = subprocess.run(
        [node, "-e", _STUB_HARNESS],
        cwd=REPO_ROOT,
        env={
            "MODE": "full",
            "INLINE_FILE": str(script_file),
            "BOOTSTRAP_OK_MARKER": _BOOTSTRAP_OK_MARKER,
            "EXTRA_CODE": "",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bootstrap abortou:\n{result.stdout}\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


def test_3d_bootstrap_ground_grid_initial_state_safe():
    """2 — updateGroundGrid chamada no ESTADO INICIAL (terrainGeometry = null)
    não lança e usa o fallback de altura; com geometria pronta, desce para a
    base do bloco em vez de ficar no meio da superfície."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível")
    source = INDEX_HTML.read_text(encoding="utf-8")
    fn = _extract_function(source, "updateGroundGrid")
    # Ordem exata do browser corrigida: a declaração acontece ANTES da chamada.
    prelude = r"""
const sceneReal = { add() {}, remove() {} };
const sceneSim = { add() {}, remove() {} };
let groundGridReal = null, groundGridSim = null;
const THREE_G = { GridHelper: function(){ return { position:{ x:0,y:0,z:0 }, geometry:{ dispose(){} }, material:[{ transparent:false, opacity:1, depthWrite:true }] }; } };
const THREE = THREE_G;
let terrainGeometry = null;
__GG_POS = null;
""" + fn + r"""
updateGroundGrid(60);
__GG_POS = groundGridReal.position.y;
"""
    # 1ª chamada no estado inicial (fallback -0.06, sem TDZ)
    result = subprocess.run(
        [node, "-e", _STUB_HARNESS],
        cwd=REPO_ROOT,
        env={"MODE": "groundgrid", "INLINE_FILE": "", "BOOTSTRAP_OK_MARKER": "", "EXTRA_CODE": prelude},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"updateGroundGrid inicial lançou:\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    # fallback: plano em -0.06 menos a folga mínima (0.15) → -0.21 (abaixo)
    assert abs(payload["pos"] - (-0.21)) < 1e-9, "estado inicial usa fallback abaixo do plano"

    # 2ª chamada com bloco topográfico pronto: grade desce para baixo da base
    prelude2 = prelude.replace(
        "let terrainGeometry = null;",
        "let terrainGeometry = { userData: { terrain: { worldStats: { minY: 0, maxY: 2 }, baseY: -3.2 } } };",
    )
    result2 = subprocess.run(
        [node, "-e", _STUB_HARNESS],
        cwd=REPO_ROOT,
        env={"MODE": "groundgrid", "INLINE_FILE": "", "BOOTSTRAP_OK_MARKER": "", "EXTRA_CODE": prelude2},
        capture_output=True,
        text=True,
    )
    assert result2.returncode == 0, f"updateGroundGrid (bloco pronto) lançou:\n{result2.stderr}"
    payload2 = json.loads(result2.stdout)
    # gap = max(0.15, |baseY| * 0.02) = 0.15 → -3.35 (abaixo da base, não no meio)
    assert abs(payload2["pos"] - (-3.35)) < 1e-9, "grade segue ABAIXO da base do bloco"
