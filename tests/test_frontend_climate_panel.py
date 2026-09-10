"""
PR #7 / PR #7-FIX.2 — Contratos do frontend do painel "Clima & Condições
Agronômicas".

Mesmo padrão dos testes de frontend existentes: verificação estática do
HTML/JS real + teste COMPORTAMENTAL do fluxo do período personalizado em
VM Node (o bloco real de controle do clima, extraído do index.html).

Garantias cobradas:
- painel INTEGRADO ao Dashboard (mesma tela FAZENDA → TALHÃO → LOCALIZAÇÃO);
- presets 7d/15d/30d + período personalizado;
- o frontend NUNCA consulta a NASA POWER diretamente (Fase 2);
- estados explícitos de erro/ausência (Fase 9) — sem dado inventado;
- separação de categorias: dado / calculado / interpretação (Fase 5);
- proveniência + confiança visíveis (Fase 8);
- dashboard.html legada rotulada como DADOS DEMONSTRATIVOS (Fase 9);
- ClimateService em app.js;
- FIX.2: período personalizado → URL com start/end (NUNCA preset),
  validação client-side, proteção contra race condition (resposta tardia
  de uma requisição anterior nunca sobrescreve a tela da mais recente).
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "app.js").read_text(encoding="utf-8")
DASH = (ROOT / "dashboard.html").read_text(encoding="utf-8")


def _inside_dashboard():
    start = INDEX.index('id="screen-dash"')
    end = INDEX.index('id="screen-map"', start)
    panel = INDEX.index('id="climate-panel"', start)
    assert start < panel < end, "painel de clima deve estar DENTRO do Dashboard"
    return panel


def test_painel_integrado_ao_dashboard():
    assert 'id="climate-panel"' in INDEX
    _inside_dashboard()
    # hierarquia do produto: FAZENDA → TALHÃO → LOCALIZAÇÃO
    ctx = INDEX[INDEX.index('id="climate-context"'):]
    assert "Fazenda" in ctx and "Talhão" in ctx and "Localização" in ctx


def test_presets_e_periodo_personalizado():
    for p in ("7d", "15d", "30d"):
        assert f"selectClimatePreset('{p}')" in INDEX
    assert "selectClimateCustom()" in INDEX
    assert "applyClimateCustomRange()" in INDEX
    assert 'id="climate-custom-start"' in INDEX and 'id="climate-custom-end"' in INDEX


def test_frontend_nunca_chama_nasa_diretamente():
    for src in (INDEX, APP, DASH):
        assert "power.larc.nasa.gov" not in src
    # sempre via endpoint interno (URL relativa à API)
    assert "/climate/farm/" in INDEX
    assert "/climate/farm/" in APP


def test_estados_explícitos_sem_dado_inventado():
    assert "temporariamente indisponíveis" in INDEX
    assert "Não há dados suficientes" in INDEX
    assert "Tentar novamente" in INDEX
    # fallback legado rotulado no KPI
    assert "DADOS DEMONSTRATIVOS" in INDEX
    assert "kpi-rain-sub" in INDEX


def test_categorias_separadas():
    # dado observado → "Condições do período"; derivado → "Indicadores
    # calculados"; texto → "Interpretação" (nunca misturados)
    assert "Condições do período" in INDEX
    assert "Indicadores calculados" in INDEX
    assert "Interpretação" in INDEX


def test_proveniencia_e_confianca_visiveis():
    assert "Fonte:" in INDEX and "NASA POWER" in INDEX
    assert "Período:" in INDEX
    assert "Confiança:" in INDEX
    assert "localização canônica" in INDEX
    assert "não é normal climatológica oficial" in INDEX  # nomenclatura correta do baseline
    assert "reanálise" in INDEX  # natureza do dado (não medição pontual)


def test_graficos_precipitacao_e_temperatura_com_baseline():
    assert 'id="climate-precip-chart"' in INDEX
    assert 'id="climate-temp-chart"' in INDEX
    assert "baseline_precip_mm" in INDEX and "baseline_temp_mean_c" in INDEX
    # unidades nos eixos
    assert "'mm'" in INDEX or "mm" in INDEX[INDEX.index("climate-precip-chart"):]


def test_dash_legacy_rotulada_como_demonstrativa():
    assert "DADOS DEMONSTRATIVOS" in DASH
    # o antigo rótulo enganoso "Ciclo Completo (NASA)" foi removido
    assert "Ciclo Completo (NASA)" not in DASH


# ---------------------------------------------------------------------------
# PR #7-FIX.1 — cobertura explícita no frontend
# ---------------------------------------------------------------------------
def test_painel_exibe_dados_do_periodo_por_variavel():
    # bloco "DADOS DO PERÍODO": dias solicitados × disponíveis por variável
    assert "Dados do período — disponibilidade por variável" in INDEX
    assert "Dias completos:" in INDEX
    # tags "X/Y dias" por métrica
    assert "dias com dados" in INDEX or "/ dias" in INDEX or " dias</span>" in INDEX


def test_banner_dados_parciais_sem_percentual_genérico():
    # banner "DADOS PARCIAIS" + dias por variável; a mensagem vem do backend
    assert "DADOS PARCIAIS" in INDEX
    assert "nenhum valor foi estimado ou preenchido" in INDEX


def test_delta_omitido_com_explicacao():
    # FIX.4 — sem comparação calculada não existe "desvio"; mostra o motivo
    assert "comparação omitida (cobertura insuficiente)" in INDEX


def test_indicador_nulo_exibe_motivo():
    # FIX.2 — indicador nulo (ex.: sequência seca com lacunas) mostra "—" + razão
    assert "OMITIDO: " in INDEX


def test_app_js_climate_service():
    assert "ClimateService" in APP
    assert "getFarmClimate" in APP


# ---------------------------------------------------------------------------
# PR #7-FIX.2 — Teste COMPORTAMENTAL do fluxo do período personalizado (VM Node)
#
# Extrai o BLOCO REAL de controle do clima do index.html (declarações let +
# functions selectClimatePreset/selectClimateCustom/applyClimateCustomRange/
# loadClimatePanel) e o executa num sandbox Node com stubs mínimos de
# document/fetch, reproduzindo o cenário exato do playtest:
#
#   1) load da página dispara preset=30d (requisição A, em voo);
#   2) usuário clica "Personalizado" (NÃO deve disparar requisição);
#   3) usuário aplica 2026-08-20 → 2026-09-07 (requisição B, start/end);
#   4) B responde (19 dias) → renderiza;
#   5) A responde TARDE (30 dias) → NÃO pode sobrescrever a tela;
#   6) validações client-side (start>end, campo vazio, >366 dias);
#   7) alternância 30d → custom → 15d → custom.
# ---------------------------------------------------------------------------
_NODE = shutil.which("node")

_CLIMATE_BLOCK_JS = r"""
const fs = require('fs');
const vm = require('vm');
const BLOCK = fs.readFileSync(process.argv[2], 'utf8');

function makeEl(id) {
  return { id: id, value: '', innerHTML: '', hidden: false,
    classList: { add() {}, remove() {} }, style: {} };
}
const els = {};
function getEl(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; }

const calls = [];
function fakeFetch(url, opts) {
  opts = opts || {};
  const rec = { url: String(url), signal: opts.signal || null, resolve: null, reject: null };
  rec.promise = new Promise((res, rej) => { rec.resolve = res; rec.reject = rej; });
  calls.push(rec);
  return rec.promise;
}

const rendered = [];
const sandbox = {
  document: { getElementById: getEl },
  fetch: fakeFetch,
  URLSearchParams: URLSearchParams,
  AbortController: AbortController,
  API_URL: '/api',
  authHeaders: () => ({ Authorization: 'Bearer test' }),
  activeFarm: { id: 1, name: 'Fazenda Orion', latitude: -22.7182,
                longitude: -55.5421, city: 'Ponta Porã', crop: 'Soja',
                location_source: 'legado', location_status: 'valida' },
  AuthService: { logout() {} },
  renderClimatePanel: (d) => rendered.push(d),
  console: console,
};
vm.createContext(sandbox);
vm.runInContext(BLOCK, sandbox);

const flush = (n) => { n = n || 20; const p = Promise.resolve();
  let q = p; for (let i = 0; i < n; i++) q = q.then(() => {}); return q; };
const resp = (period) => ({ status: 200, ok: true, json: async () => ({ period: period }) });

(async () => {
  const out = { rendered: [] };
  const state = () => getEl('climate-state').innerHTML;

  // (1) load da página: preset 30d (requisição A) em voo
  sandbox.selectClimatePreset('30d');
  out.callA = calls[0] ? calls[0].url : null;

  // (2) clica "Personalizado" — NÃO pode disparar requisição
  sandbox.selectClimateCustom();
  out.callsAfterCustomClick = calls.length;

  // (3) aplica o intervalo do playtest — só start/end, nunca preset
  getEl('climate-custom-start').value = '2026-08-20';
  getEl('climate-custom-end').value = '2026-09-07';
  sandbox.applyClimateCustomRange();
  out.callB = calls[1] ? calls[1].url : null;
  out.aAborted = (calls[0] && calls[0].signal) ? calls[0].signal.aborted : null;

  // (4) B responde (19 dias) → renderiza
  if (calls[1]) { calls[1].resolve(resp({ start: '2026-08-20', end: '2026-09-07', days: 19 })); }
  await flush();

  // (5) A responde TARDE (30 dias) — NÃO pode sobrescrever
  if (calls[0]) { calls[0].resolve(resp({ start: '2026-08-09', end: '2026-09-07', days: 30 })); }
  await flush();
  out.rendered = rendered.map((d) => d.period);

  // (6) validações client-side — nenhuma deve disparar requisição
  const before = calls.length;
  getEl('climate-custom-start').value = '2026-09-05';
  getEl('climate-custom-end').value = '2026-08-20';
  sandbox.applyClimateCustomRange();
  out.callsAfterStartGtEnd = calls.length;
  out.msgStartGtEnd = state();
  getEl('climate-custom-start').value = '';
  getEl('climate-custom-end').value = '2026-09-07';
  sandbox.applyClimateCustomRange();
  out.callsAfterEmpty = calls.length;
  out.msgEmpty = state();
  getEl('climate-custom-start').value = '2025-01-01';
  getEl('climate-custom-end').value = '2026-09-07';
  sandbox.applyClimateCustomRange();
  out.callsAfterTooLong = calls.length;
  out.msgTooLong = state();
  out.noCallForInvalid = (calls.length === before);

  // (7) alternância: custom → 15d → custom
  sandbox.selectClimatePreset('15d');
  out.call15 = calls[calls.length - 1] ? calls[calls.length - 1].url : null;
  if (calls[calls.length - 1]) { calls[calls.length - 1].resolve(resp({ start: '2026-08-24', end: '2026-09-07', days: 15 })); }
  await flush();
  // fluxo real: clicar "Personalizado" de novo (modo custom) e só então Aplicar
  sandbox.selectClimateCustom();
  getEl('climate-custom-start').value = '2026-08-20';
  getEl('climate-custom-end').value = '2026-09-07';
  sandbox.applyClimateCustomRange();
  out.callCustom2 = calls[calls.length - 1] ? calls[calls.length - 1].url : null;
  out.totalCalls = calls.length;
  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ fatal: String((e && e.stack) || e) })); });
"""


def _extract_climate_block() -> str:
    """Extrai o bloco real de controle do clima (lets + functions) do index.html."""
    src = INDEX
    start = src.index("let climateChartPrecip = null;")
    fstart = src.index("function loadClimatePanel(", start)
    i = src.index("{", fstart)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("fim de loadClimatePanel não encontrado")


@pytest.mark.skipif(_NODE is None, reason="node indisponível no ambiente")
def test_custom_period_flow_em_vm_node():
    """Cenário exato do playtest: a resposta TARDIA de 30d NÃO sobrescreve o
    período personalizado aplicado depois (race condition — causa raiz do FIX.2)."""
    block = _extract_climate_block()
    with tempfile.TemporaryDirectory() as td:
        block_path = Path(td) / "climate_block.js"
        block_path.write_text(block, encoding="utf-8")
        harness_path = Path(td) / "harness.js"
        harness_path.write_text(_CLIMATE_BLOCK_JS, encoding="utf-8")
        proc = subprocess.run(
            [_NODE, str(harness_path), str(block_path)],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, f"node falhou:\n{proc.stderr}"
        out = None
        for line in reversed(proc.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    out = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        assert out is not None, f"sem JSON no stdout do node:\n{proc.stdout}"
    assert "fatal" not in out, out.get("fatal")

    # (1) load da página → preset 30d, SEM start/end
    assert out["callA"] is not None
    assert "preset=30d" in out["callA"]
    assert "start=" not in out["callA"] and "end=" not in out["callA"]

    # (2) clicar "Personalizado" NÃO dispara requisição
    assert out["callsAfterCustomClick"] == 1

    # (3) Aplicar → URL com start/end e NUNCA preset
    assert out["callB"] is not None
    assert "start=2026-08-20" in out["callB"]
    assert "end=2026-09-07" in out["callB"]
    assert "preset=" not in out["callB"]
    # (3b) a requisição anterior (30d) foi abortada
    assert out["aAborted"] is True

    # (4)+(5) SÓ a resposta do período personalizado é renderizada;
    # a resposta tardia de 30d NÃO sobrescreve a tela.
    periods = out["rendered"]
    assert len(periods) == 1, f"esperava 1 render, veio {periods}"
    assert periods[0]["start"] == "2026-08-20"
    assert periods[0]["end"] == "2026-09-07"
    assert periods[0]["days"] == 19
    assert all(p["days"] != 30 for p in periods)

    # (6) validações client-side não disparam requisição e explicam o motivo
    assert out["noCallForInvalid"] is True
    assert out["callsAfterStartGtEnd"] == out["callsAfterEmpty"] == out["callsAfterTooLong"]
    assert "início" in out["msgStartGtEnd"].lower() or "fim" in out["msgStartGtEnd"].lower()
    assert "informe" in out["msgEmpty"].lower()
    assert "366" in out["msgTooLong"]

    # (7) alternância de modos: 15d usa preset (sem start/end); custom volta a start/end
    assert "preset=15d" in out["call15"]
    assert "start=" not in out["call15"] and "end=" not in out["call15"]
    assert "start=2026-08-20" in out["callCustom2"]
    assert "end=2026-09-07" in out["callCustom2"]
    assert "preset=" not in out["callCustom2"]
