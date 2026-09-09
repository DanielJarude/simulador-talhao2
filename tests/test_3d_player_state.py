"""PR #5d — testes da máquina de estados da timeline 3D (sincronização assíncrona).

Executa o bloco puro `[3D-UX-HELPERS-START/END]` extraído do `index.html` em
uma VM Node (mesma técnica de `test_frontend_3d_pipeline.py`). Nenhum teste é
"busca de string": todas as asserções validam TRANSIÇÕES reais da máquina e do
gate de concorrência:

  1. Play não avança durante loading (CARREGAR → APLICAR → INTERVALO → PRÓXIMA);
  2. data aplicada só muda após `playerFinishLoad(ok=true)`;
  3. generationId descarta resposta antiga (race condition);
  4. Pause durante loading cancela e NÃO retoma sozinho;
  5. troca manual de data invalida a carga anterior;
  6. layer swap preserva a intenção de Play e retoma após aplicar;
  7. falha → status error + PAUSA (recuperável, nunca pula cena);
  8. Real/What-If sincronizados (mesma data/layer da cena aplicada);
  9. metadados correspondem à textura aplicada;
 10. status real / fallback e proveniência compacta + "Detalhes".
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
def ux_helpers() -> str:
    source = INDEX_HTML.read_text(encoding="utf-8")
    return _slice(source, "// [3D-UX-HELPERS-START]", "// [3D-UX-HELPERS-END]")


def test_3d_player_state_machine_behaviour(ux_helpers):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível para o teste da máquina de estados 3D")

    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');

const sandbox = { console };
vm.runInNewContext(process.env.UX_HELPERS, sandbox);

// META de exemplo (contrato do backend /api/talhao/{id}/texture)
const realMeta = {
  data_origin: 'sentinel',
  real_data_status: 'ok',
  date: '2026-08-15',
  acquisition_date: '2026-08-15',
  cloud_cover: 2.04,
  platform: 'Sentinel-2B',
  constellation: 'S2',
  collection: 'sentinel-2-l2a',
  processing_level: 'L2A',
  product_id: 'S2B_MSIL2A_20260815T123456_N9999_R123_T22JGB',
  bands: ['B04', 'B08', 'B02'],
  valid_pixel_percentage: 97.8,
  selection_reason: 'least_cc',
  provider: 'Copernicus Data Space Ecosystem',
};

// ---- 1. Play NÃO avança durante loading ----
{
  const st = sandbox.createTimelineMachine(3, 7);
  sandbox.playerPlay(st);
  sandbox.playerStartLoad(st, 4, 'ndvi');
  assert.equal(st.status, 'loading');
  assert.equal(st.appliedIndex, 3, 'data aplicada intacta durante loading');
  assert.equal(sandbox.playerCanAdvance(st), false, 'loading bloqueia avanço');
  assert.equal(sandbox.playerNextIndex(st), 4, 'próxima = aplicada+1 (não a pendente enquanto não aplicou)');
}

// ---- 2. Data só muda APÓS aplicar (CARREGAR → APLICAR) ----
{
  const st = sandbox.createTimelineMachine(3, 7);
  sandbox.playerPlay(st);
  sandbox.playerStartLoad(st, 4, 'ndvi');
  sandbox.playerFinishLoad(st, 4, 'ndvi', true, null, realMeta);
  assert.equal(st.status, 'ready');
  assert.equal(st.appliedIndex, 4, 'aplicada só após conclusão');
  assert.equal(st.appliedLayer, 'ndvi');
  assert.notEqual(st.appliedMeta, null);
  assert.equal(sandbox.playerCanAdvance(st), true, 'READY + PLAYING → pode avançar');
}

// ---- 3. generationId descarta resposta ATRASADA (race) ----
{
  const gate = sandbox.createSceneLoadGate();
  assert.equal(gate.currentGeneration(), 0);
  const g1 = gate.beginLoad();
  const g2 = gate.beginLoad();
  assert.notEqual(g1, g2);
  assert.equal(gate.isCurrent(g1), false, 'resposta antiga NÃO é atual');
  assert.equal(gate.isCurrent(g2), true, 'resposta mais nova é a única aceita');
  gate.invalidate();
  assert.equal(gate.isCurrent(g2), false, 'invalidate descarta até a mais nova');
}

// ---- 4. Pause durante loading cancela e NÃO retoma sozinho ----
{
  const st = sandbox.createTimelineMachine(2, 7);
  sandbox.playerStartLoad(st, 3, 'evi');
  sandbox.playerPause(st);
  sandbox.playerCancelLoad(st);
  assert.equal(st.mode, 'paused');
  assert.equal(st.status, 'idle', 'sem cena aplicada → idle; nunca fica preso em loading');
  assert.equal(sandbox.playerCanAdvance(st), false);
  assert.equal(st.appliedIndex, 2, 'cena aplicada intacta após pause');
}

// ---- 5. Troca manual de data invalida a carga anterior ----
{
  const gate = sandbox.createSceneLoadGate();
  const oldGen = gate.beginLoad();
  const newGen = gate.beginLoad(); // usuário clicou outra data
  assert.equal(gate.isCurrent(oldGen), false);
  assert.equal(gate.isCurrent(newGen), true);
}

// ---- 6. Layer swap congelando + retomando Play ----
{
  const st = sandbox.createTimelineMachine(3, 7);
  sandbox.playerPlay(st);
  sandbox.playerBeginLayerSwap(st, 'ndmi');
  assert.equal(st.resumeAfterLayer, true, 'intenção de Play preservada');
  sandbox.playerStartLoad(st, 3, 'ndmi');
  assert.equal(sandbox.playerCanAdvance(st), false, 'troca congela o avanço');
  sandbox.playerFinishLoad(st, 3, 'ndmi', true, null, realMeta);
  assert.equal(st.appliedLayer, 'ndmi');
  assert.equal(st.resumeAfterLayer, false, 'aplicado → flag de retomada consumida');
  assert.equal(sandbox.playerCanAdvance(st), true, 'Play retomado após aplicar');
  assert.equal(sandbox.playerNextIndex(st), 4, 'avança a partir da data aplicada');
}

// ---- 7. Falha → error + PAUSA (recuperável, nunca pula) ----
{
  const st = sandbox.createTimelineMachine(1, 7);
  sandbox.playerPlay(st);
  sandbox.playerStartLoad(st, 2, 'ndvi');
  sandbox.playerFinishLoad(st, 2, 'ndvi', false, sandbox.sceneErrorLabel('2026-08-15'), null);
  assert.equal(st.status, 'error');
  assert.equal(st.mode, 'paused', 'erro PAUSA automaticamente');
  assert.equal(st.appliedIndex, 1, 'data principal NUNCA muda em falha');
  assert.equal(sandbox.playerCanAdvance(st), false);
  assert.ok(st.error.includes('15/08/2026'), 'erro contextualizado com a data');
  // retry manual → ok
  sandbox.playerPlay(st);
  sandbox.playerStartLoad(st, 2, 'ndvi');
  sandbox.playerFinishLoad(st, 2, 'ndvi', true, null, realMeta);
  assert.equal(st.status, 'ready');
  assert.equal(st.appliedIndex, 2);
  assert.equal(st.error, null);
}

// ---- 8. Real/What-If sincronizados (mesma data/layer da cena aplicada) ----
{
  const st = sandbox.createTimelineMachine(0, 7);
  sandbox.playerStartLoad(st, 5, 'ndre');
  sandbox.playerFinishLoad(st, 5, 'ndre', true, null, realMeta);
  const simBase = sandbox.buildSimBaseMeta(st.appliedMeta);
  assert.equal(simBase.date, '2026-08-15', 'What-If usa a MESMA data aplicada');
  assert.equal(simBase.data_origin, 'sentinel');
  assert.equal(simBase.real_data_status, 'ok');
}

// ---- 9. Metadados correspondem à textura aplicada ----
{
  const st = sandbox.createTimelineMachine(0, 7);
  sandbox.playerStartLoad(st, 2, 'rgb');
  sandbox.playerFinishLoad(st, 2, 'rgb', true, null, realMeta);
  assert.ok(st.appliedMeta, 'metadata do COMMIT é a da textura aplicada');
  const summary = sandbox.provenanceSummary(st.appliedMeta);
  assert.ok(summary.startsWith('Sentinel-2 L2A'), 'proveniência compacta REAL');
  assert.ok(summary.includes('15/08/2026'), 'data do item na proveniência');
  assert.ok(summary.includes('2,0%'), 'nuvens com vírgula pt-BR');
  const lines = sandbox.provenanceDetailLines(st.appliedMeta);
  assert.equal(lines.length, 9, 'Detalhes: 9 linhas');
  assert.ok(lines.some(l => l.startsWith('ID do item: S2B_MSIL2A')));
  assert.ok(lines.some(l => l.startsWith('Fornecedor: Copernicus')));
}

// ---- 10. Status real / fallback + formatação ----
{
  // (campos separados: objetos da VM têm prototype diferente do host — deepEqual estrito false-positivo)
  const real = sandbox.scenarioSubStatus('sentinel', 'ok');
  assert.equal(real.kind, 'real');
  assert.equal(real.text, 'Sentinel-2 L2A • dados reais');
  assert.equal(sandbox.scenarioSubStatus('procedural', 'not_configured').kind, 'no-config');
  assert.equal(sandbox.scenarioSubStatus('procedural', 'no_scene').kind, 'approx');
  assert.equal(sandbox.scenarioSubStatus('procedural', 'error').kind, 'approx');
  assert.equal(sandbox.scenarioSubStatus('procedural', null).kind, 'approx');

  assert.equal(sandbox.provenanceSummary(null), 'aguardando dados…');
  assert.ok(sandbox.provenanceSummary({ data_origin: 'procedural', real_data_status: 'not_configured' })
    .includes('NÃO CONFIGURADOS'));
  assert.ok(sandbox.provenanceSummary({ data_origin: 'procedural', real_data_status: 'no_scene' })
    .includes('aproximado'));
  assert.equal(sandbox.formatSceneDate('2026-08-15'), '15/08/2026');
  assert.equal(sandbox.isAbortError({ name: 'AbortError' }), true);
  assert.equal(sandbox.isAbortError(new Error('The operation was aborted')), true);
  assert.equal(sandbox.isAbortError(new Error('HTTP 500')), false);
  assert.equal(sandbox.playerNextIndex(sandbox.createTimelineMachine(6, 7)), 0, 'volta ao início');
}

process.stdout.write(JSON.stringify({ ok: true }));
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={"UX_HELPERS": ux_helpers},
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
