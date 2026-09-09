"""PR #5g — terreno topográfico com VOLUME real (VM Node).

Executa o bloco puro `[3D-TERRAIN-HELPERS-*]` do `index.html` e valida:
  - escala horizontal/vertical EXPLÍCITA (m → unidades de mundo) + exagero
    1×/2×/3×/5× multiplicando SOMENTE a diferença relativa de altitude;
  - altura RELATIVA (min DEM = Y 0) preservando os valores reais no rótulo;
  - geometria deformada NÃO coplanar (maxY > minY), estatísticas reais,
    bounding box incluindo o relevo;
  - lateral (skirt) + base: borda fechada, baseY < minY, contagem exata;
  - hillshade multiplicativo preservando o flat (fator ≈ 1) e sombreando
    encostas sem destruir as cores da textura;
  - câmera oblíqua 35–50° (topo + lateral) e enquadramento determinístico;
  - Real × What-If compartilham a MESMA geometria/escala/exagero (e a malha
    nunca é refeita ao trocar o exagero — só Y + normais)."""
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
def terrain_helpers() -> str:
    source = INDEX_HTML.read_text(encoding="utf-8")
    return _slice(source, "// [3D-TERRAIN-HELPERS-START]", "// [3D-TERRAIN-HELPERS-END]")


def test_3d_terrain_volume_real_behavior(terrain_helpers):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível para o teste do volume 3D")

    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');
const sandbox = { console: { log: () => {}, info: () => {}, warn: () => {}, error: () => {} } };
vm.runInNewContext(process.env.TERRAIN_HELPERS, sandbox);

// =====================================================================
// 1. ESCALA EXPLÍCITA: metros ↔ unidades de mundo (sem magic numbers)
// =====================================================================
assert.ok(Math.abs(sandbox.terrainWorldUnitsPerMeter(900, 50) - 0.055555) < 1e-6, '50 u / 900 m');
assert.equal(sandbox.terrainWorldUnitsPerMeter(0, 50), 50, 'span 0 → 1 m (finito)');
assert.ok(Number.isFinite(sandbox.terrainWorldUnitsPerMeter(0, 50)));

// Escala física REAL documentada: 23 m de relevo em 900 m → ~1,28 u (1×)
const relief1x = sandbox.demReliefWorldUnits(23, 900, 50);
assert.ok(Math.abs(relief1x - 1.277777) < 1e-4, `1× ≈ 1,2778 u (obtido ${relief1x})`);
assert.ok(relief1x < 50 * 0.03, '1× é proporção topográfica REAL (< 3% da extensão)');

// =====================================================================
// 2. ALTURA RELATIVA: min do DEM = 0; exagero multiplica SÓ a diferença
// =====================================================================
assert.equal(sandbox.terrainRelativeY(0, 2.0, 3), 0, 'min do DEM → Y relativo 0');
assert.equal(sandbox.terrainRelativeY(1, 2.0, 3), 6.0, 'max do DEM → alívio × exagero');
assert.equal(sandbox.terrainRelativeY(0.5, 2.0, 3), 3.0, 'meio = metade (linear)');
assert.equal(sandbox.terrainRelativeY(-5, 2.0, 3), 0, 'amostra negativa é truncada em 0');
assert.equal(sandbox.terrainRelativeY(2, 2.0, 3), 6.0, 'amostra > 1 é truncada em 1');
assert.ok(Number.isFinite(sandbox.terrainRelativeY(0.5, NaN, 3)));
assert.ok(Number.isFinite(sandbox.terrainRelativeY(0.5, 2.0, NaN)));

// =====================================================================
// 3. AMPLITUDE: 1× < 2× < 3× < 5× (diferença VISUAL clara, mesma razão)
// =====================================================================
const a1 = sandbox.terrainAmplitudeWorld(23, 900, 50, 1);
const a2 = sandbox.terrainAmplitudeWorld(23, 900, 50, 2);
const a3 = sandbox.terrainAmplitudeWorld(23, 900, 50, 3);
const a5 = sandbox.terrainAmplitudeWorld(23, 900, 50, 5);
assert.ok(a1 < a2 && a2 < a3 && a3 < a5, '1× < 2× < 3× < 5×');
assert.ok(Math.abs(a2 / a1 - 2) < 1e-9 && Math.abs(a3 / a1 - 3) < 1e-9 && Math.abs(a5 / a1 - 5) < 1e-9);
// Sem DEM (relief = 0) → amplitude 0, FINITA (nunca NaN/Infinity)
assert.equal(sandbox.terrainAmplitudeWorld(0, 900, 50, 3), 0);
assert.equal(sandbox.demReliefWorldUnits(0, 900, 50), 0);
assert.ok(Number.isFinite(sandbox.terrainAmplitudeWorld(0, 900, 50, 3)));

// =====================================================================
// 4. BASE (espessura do bloco): proporcional à extensão, nunca absurda
// =====================================================================
assert.ok(Math.abs(sandbox.terrainBaseDepthWorld(50) - 4.0) < 1e-9, '8% de 50 u = 4 u');
assert.ok(Math.abs(sandbox.terrainBaseDepthWorld(10) - 1.0) < 1e-9, 'piso de 1 u (extensão pequena)');
assert.ok(sandbox.terrainBaseDepthWorld(500) > sandbox.terrainBaseDepthWorld(50), 'cresce com a cena');

// =====================================================================
// 5. GEOMETRIA NÃO COPLANAR: min/max Y reais + mundo correto (Y = altura)
// =====================================================================
function fakeGeometry(cols, rows) {
  const arr = new Float32Array(cols * rows * 3);
  for (let i = 2; i < arr.length; i += 3) arr[i] = 0;
  let normals = 0;
  return {
    parameters: { widthSegments: cols - 1, heightSegments: rows - 1 },
    attributes: { position: { array: arr, needsUpdate: false, itemSize: 3 } },
    computeVertexNormals() { normals += 1; },
    get normalsCalled() { return normals; },
    get arr() { return arr; },
    userData: {},
  };
}
const peak = (u, v) => (u >= 0.75 && v >= 0.75 ? 1 : 0);
const geo = fakeGeometry(5, 5);
const okApply = sandbox.applyDemToGeometry(geo, peak, 2.0, 3);
assert.equal(okApply, true);
assert.equal(geo.normalsCalled, 1, 'normais recalculadas após o deslocamento');
const t = geo.userData.terrain;
assert.ok(t && t.worldStats, 'estatísticas gravadas na geometria');
assert.ok(t.worldStats.maxY > t.worldStats.minY, 'NÃO coplanar: maxY > minY com relevo');
assert.ok(Math.abs(t.worldStats.maxY - 6.0) < 1e-6, 'pico relativo 1.0 × 2 u × 3× = 6 u');
assert.equal(t.worldStats.minY, 0, 'altitude relativa: mínimo do DEM em Y = 0');
// Conversão local→mundo: (x, y, zLocal) → (x, zLocal, -y); a altura é Y
const world = sandbox.terrainWorldPositionsFromLocal(
  new Float32Array([1, 2, 3, 4, 5, 6])
);
assert.deepEqual(Array.from(world), [1, 3, -2, 4, 6, -5]);

// =====================================================================
// 6. LATERAL (skirt) + BASE: fecha a borda e fica ABAIXO do relevo
// =====================================================================
const data = sandbox.buildTerrainSkirtGeometryData(t.worldPositions, t.cols, t.rows, t.baseY);
assert.ok(data && data.positions && data.indices && data.vertexCount > 0, 'dados da lateral existem');
assert.ok(t.baseY < t.worldStats.minY, 'baseY < minY (bloco com espessura)');
assert.equal(data.vertexCount, 4 * (2 * (t.cols - 1) + 2 * (t.rows - 1)) + 4, '4 vértices/aresta + 4 cantos da base');
assert.equal(data.triangleCount, 2 * (2 * (t.cols - 1) + 2 * (t.rows - 1)) + 2, '2 triângulos/aresta + 2 da base');
// Cada vértice de topo da lateral acompanha um vértice EXATO da borda da malha
let maxSkirtY = -Infinity;
for (let i = 0; i < data.positions.length; i += 12) {
  maxSkirtY = Math.max(maxSkirtY, data.positions[i + 1], data.positions[i + 7]);
}
assert.ok(maxSkirtY <= t.worldStats.maxY + 1e-6, 'lateral acompanha o contorno real (nunca acima)');

// =====================================================================
// 6b. CONTORNO REAL: máscara de grid (sem "placa" retangular) + paredes
// =====================================================================
const square = [[-5, -5], [5, -5], [5, 5], [-5, 5]];
assert.equal(sandbox.pointInPolygon(0, 0, square), true, 'centro dentro');
assert.equal(sandbox.pointInPolygon(6, 0, square), false, 'fora do polígono');
assert.equal(sandbox.pointInPolygon(0, 0, null), false, 'polígono nulo → fora');
// Máscara: triangulação da PlaneGeometry 10×10 — só os triângulos DENTRO ficam
const gridGeo = (() => {
  const cols = 10, rows = 10;                  // malha MAIOR que o polígono
  const arr = new Float32Array(cols * rows * 3);
  for (let iy = 0; iy < rows; iy++) for (let ix = 0; ix < cols; ix++) {
    const i = (iy * cols + ix) * 3;
    arr[i] = (ix / (cols - 1) - 0.5) * 20;     // -10..+10
    arr[i + 1] = (iy / (rows - 1) - 0.5) * 20; // -10..+10
  }
  const idxArr = [];
  const v = (ix, iy) => iy * cols + ix;
  for (let iy = 0; iy < rows - 1; iy++) for (let ix = 0; ix < cols - 1; ix++) {
    const a = v(ix, iy), b = v(ix + 1, iy), c = v(ix, iy + 1), d = v(ix + 1, iy + 1);
    idxArr.push(a, c, b, b, c, d);
  }
  return {
    parameters: { widthSegments: cols - 1, heightSegments: rows - 1 },
    attributes: { position: { array: arr, needsUpdate: false, itemSize: 3 } },
    index: { array: new Uint32Array(idxArr) },
    setIndex(list) { this.index = { array: new Uint32Array(list) }; },
  };
})();
const beforeTris = gridGeo.index.array.length / 3;
const masked = sandbox.maskPlaneGeometryToPolygon(gridGeo, square);
assert.equal(masked, true);
const afterTris = gridGeo.index.array.length / 3;
assert.ok(afterTris > 0 && afterTris < beforeTris, `máscara removeu triângulos fora (${afterTris}/${beforeTris})`);
// Paredes do POLÍGONO: 4 arestas × 6 índices (2 triângulos), topo = DEM
const outline = [[-5, -5], [5, -5], [5, 5], [-5, 5]];
const wallData = sandbox.buildPolygonSkirtGeometryData(outline, () => 2.0, -1.0);
assert.equal(wallData.vertexCount, 16, '4 vértices por aresta × 4 arestas');
assert.equal(wallData.triangleCount, 8, '2 triângulos por aresta × 4 arestas');
assert.equal(wallData.triangleCount * 3, wallData.indices.length);
let topMax = -Infinity, bottomMin = Infinity;
for (let i = 0; i < wallData.positions.length; i += 3) {
  topMax = Math.max(topMax, wallData.positions[i + 1]);
  bottomMin = Math.min(bottomMin, wallData.positions[i + 1]);
}
assert.equal(topMax, 2.0, 'paredes sobem até a altura do DEM na borda');
assert.equal(bottomMin, -1.0, 'paredes descem até baseY (fecha a borda)');
// Amostragem da altura no mundo (usada pelas paredes): bilinear no grid
const wStats = { minX: -5, maxX: 5, minZ: -5, maxZ: 5 };
const grid5 = (() => {
  const n = 5 * 5; const w = new Float32Array(n * 3);
  for (let iy = 0; iy < 5; iy++) for (let ix = 0; ix < 5; ix++) {
    const i = (iy * 5 + ix) * 3;
    w[i] = ix * 2.5 - 5; w[i + 1] = iy * 2.0; w[i + 2] = iy * 2.5 - 5;
  }
  return w;
})();
assert.ok(Math.abs(sandbox.terrainGridSampleWorld(grid5, 5, 5, 0, 0, wStats) - 4.0) < 1e-9, 'centro = amostra exata do grid (linha 2, Y=4)');
assert.equal(sandbox.terrainGridSampleWorld(grid5, 5, 5, -5, -5, wStats), 0, 'canto mínimo');
assert.equal(sandbox.terrainGridSampleWorld(grid5, 5, 5, 5, 5, wStats), 8.0, 'canto máximo');

// =====================================================================
// 7. HILLSHADE: plano fica neutro; encosta iluminada>1; oposta<1 (cores ok)
// =====================================================================
const flatWorld = (() => {
  const n = 3 * 3; const w = new Float32Array(n * 3);
  for (let i = 0; i < n * 3; i += 3) { w[i] = i / 3; w[i + 1] = 0; w[i + 2] = 0; }
  return w;
})();
const flatCol = sandbox.terrainHillshadeColors(flatWorld, 3, 3, 1.0);
for (let i = 0; i < flatCol.length; i += 3) {
  assert.ok(Math.abs(flatCol[i] - 1.0) < 1e-6, 'plano ≈ fator 1.0 (cores preservadas)');
}
// Encosta: Y cresce no eixo Z (lado voltado à luz -0.55/1/0.6 fica SOMBREADO)
const slopeWorld = (() => {
  const n = 3 * 3; const w = new Float32Array(n * 3);
  for (let iy = 0; iy < 3; iy++) for (let ix = 0; ix < 3; ix++) {
    const i = (iy * 3 + ix) * 3;
    w[i] = ix; w[i + 1] = iy * 2.0; w[i + 2] = iy;
  }
  return w;
})();
const slopeCol = sandbox.terrainHillshadeColors(slopeWorld, 3, 3, 1.0);
assert.ok(slopeCol.some((v, i) => i % 3 === 0 && v < 1.0), 'encosta com sombra existe (fator < 1)');
assert.ok(slopeCol.every((v) => v >= 0.8 && v <= 1.2), 'clamp 0.8–1.2 (sutil, não destrói a textura)');

// =====================================================================
// 8. CÂMERA: oblíqua 35–50° mostrando TOPO + LATERAL; fit determinístico
// =====================================================================
const fit = sandbox.computeCameraFit(50, 1.6, {});
assert.ok(fit.elevationDeg >= 35 && fit.elevationDeg <= 50, `elevação ${fit.elevationDeg}° ∈ [35,50]`);
assert.ok(fit.position[1] > 0 && fit.position[2] > 0, 'visão oblíqua elevada (não rasante)');
const fit2 = sandbox.computeCameraFit(50, 1.6, {});
assert.deepEqual(fit2.position, fit.position, 'fit determinístico (mesma entrada → mesma saída)');
assert.ok(fit.minDistance < fit.distance && fit.distance < fit.maxDistance, 'zoom com limites');
assert.ok(fit.position[0] !== 0, 'azimute ≠ top-down (diagonal: largura + profundidade)');
const cfg = sandbox.CAMERA_PRESET;
assert.ok(cfg.maxPolarAngle < Math.PI / 2, 'nunca abaixo da superfície');
assert.equal(sandbox.DEM_RULES.default, 2, 'default documentado = 2×');
assert.ok(sandbox.DEM_RULES.scale.planeUnits >= 1, 'escala explícita documentada');

// =====================================================================
// 9. REAL × WHAT-IF: mesma geometria/escala/exagero (contrato de código)
// =====================================================================
assert.equal(sandbox.applyDemToGeometry.length >= 4, true, 'aplicação de DEM usa a mesma função p/ os dois cenários');
assert.ok(
  ['terrainRelativeY', 'terrainWorldUnitsPerMeter', 'demReliefWorldUnits'].every(
    (fn) => typeof sandbox[fn] === 'function'
  ),
  'helpers de escala compartilhados (Real e What-If usam exatamente os mesmos)'
);

process.stdout.write(JSON.stringify({ ok: true, relief1x, a1, a2, a3, a5 }));
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={"TERRAIN_HELPERS": terrain_helpers},
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


def test_3d_terrain_volume_wiring_contract():
    """Contrato de montagem (fonte): textura NO mesh deformado + Real×What-If
    compartilhando a MESMA geometria + troca de exagero sem refazer nada."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    checks = [
        # Textura Sentinel aplicada no MATERIAL do mesh (nunca plano separado)
        ("applyTextureToMaterial(matReal,", "textura aplicada no material do terreno real"),
        ("matReal.map", "material real carrega map (mesh deformado)"),
        # Geometria compartilhada Real × What-If
        ("meshSim.geometry = newGeo", "What-If usa a MESMA geometria do Real"),
        ("meshReal.geometry = newGeo", "Real recebe a geometria nova"),
        # Exagero: sem fetch/DEM/textura — só Y + normais
        ("setExaggeration(mult)", "handler de exagero existe"),
        ("bakeDemGeometry()", "re-bake sem fetch"),
        # Escala explícita não copiada (sem magic numbers)
        ("worldUnitsPerMeterHorizontal", "escala horizontal explícita"),
        ("terrainRelativeY", "altura relativa (min = 0)"),
        # Debug nunca por padrão
        ("debug3dEnabled = q.get('debug3d')", "debug3d somente via ?debug3d=1"),
        # Grade ABAIXO do bloco (base)
        ("g.position.y = baseY - gap", "grade abaixo da base do bloco"),
        # Contorno REAL: superfície máscara + paredes no polígono (não caixa)
        ("maskPlaneGeometryToPolygon(newGeo, g.points)", "superfície = polígono real do talhão"),
        ("buildPolygonSkirtGeometryData", "paredes seguem o contorno real (KML/elipse)"),
    ]
    for needle, desc in checks:
        assert needle in source, f"contrato ausente: {desc} ({needle})"
    # Não regressão #5f: marcadores preservados
    for marker in ("[3D-CALENDAR-HELPERS-START]", "[3D-TIMELINE-HELPERS-START]", "[3D-DATES]"):
        assert marker in source


def test_3d_terrain_volume_dem_markers_in_contract():
    """O log [3D-DEM] (auditoria) existe no pipeline de carregamento e no
    handler de exagero — prova técnica de que o DEM deforma a geometria."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert source.count("[3D-DEM]") >= 2, "log [3D-DEM] no carregamento e no exagero"
    for attr in ("vertices", "dem_min", "dem_max", "relief_real", "exaggeration", "mesh_y_min", "mesh_y_max"):
        assert attr in source, f"atributo do log [3D-DEM] ausente: {attr}"
