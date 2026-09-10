"""
PR #7 / PR #7-FIX.1 / PR #7-FIX.2 / PR #7-FIX.3 — Contratos do frontend do
painel "Clima & Condições Agronômicas".

Mesmo padrão dos testes de frontend existentes: verificação estática do
HTML/JS real + teste COMPORTAMENTAL do fluxo completo em VM Node (o bloco
real de controle do clima + a função REAL switchScreen, extraídos do
index.html), reproduzindo o ciclo de vida exato do playtest:

- painel INTEGRADO ao Dashboard (mesma tela FAZENDA → TALHÃO → LOCALIZAÇÃO);
- presets 7d/15d/30d + período personalizado;
- o frontend NUNCA consulta a NASA POWER diretamente (Fase 2);
- estados explícitos de erro/ausência (Fase 9) — sem dado inventado;
- separação de categorias: dado / calculado / interpretação (Fase 5);
- proveniência + confiança visíveis (Fase 8);
- dashboard.html legada rotulada como DADOS DEMONSTRATIVOS (Fase 9);
- ClimateService em app.js;
- FIX.2: período personalizado → URL com start/end (NUNCA preset),
  validação client-side, proteção contra race condition;
- FIX.3:
  * estado ÚNICO e explícito (climateMode) — o par ambíguo
    climateCustomMode/climateCurrentPreset foi eliminado;
  * loadActiveFarm chama loadClimatePanel EXATAMENTE uma vez e nunca muda
    o modo; nenhuma rotina automática (timeline, analytics, weather,
    navegação) altera o modo;
  * proteção SEMÂNTICA: a resposta só é renderizada se o período coberto
    for exatamente o período pedido (2ª camada além do token seq);
  * comportamento de navegação DOCUMENTADO: o modo é PRESERVADO ao
    sair/voltar das telas; o estado visual das pílulas é restaurado ao
    voltar ao dashboard (nunca reset silencioso);
  * fluxo real completo: load (30d em voo) → Personalizado → Aplicar
    (20/08/2026→07/09/2026, 19 dias) → resposta custom → 30d responde
    TARDE → painel PERMANECE em 19 DIAS e NENHUM terceiro preset=30d é
    disparado.
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
# PR #7-FIX.3 — estado único explícito + rotinas que NUNCA alteram o modo
# ---------------------------------------------------------------------------
def _extract_function_body(name: str) -> str:
    """Extrai o corpo real de uma função (casamento de chaves)."""
    marker = f"function {name}("
    fstart = INDEX.index(marker)
    i = INDEX.index("{", fstart)
    depth = 0
    for j in range(i, len(INDEX)):
        if INDEX[j] == "{":
            depth += 1
        elif INDEX[j] == "}":
            depth -= 1
            if depth == 0:
                return INDEX[fstart:j + 1]
    raise AssertionError(f"fim de {name} não encontrado")


def test_estado_unico_explícito_climate_mode():
    # o par ambíguo foi eliminado: apenas climateMode decide o período
    assert "let climateMode" in INDEX
    assert "let climateCustomMode" not in INDEX
    assert "let climateCurrentPreset" not in INDEX
    # a carga do painel deriva o período EXCLUSIVAMENTE do estado explícito
    lc = _extract_function_body("loadClimatePanel")
    assert "climateMode.type === 'custom'" in lc
    assert "climateCustomMode" not in lc
    assert "climateCurrentPreset" not in lc


def test_load_active_farm_chama_clima_uma_vez_sem_mudar_modo():
    # carga inicial: loadActiveFarm dispara o clima EXATAMENTE uma vez e
    # nunca altera o modo (o 30d inicial vem do estado padrão de climateMode)
    body = _extract_function_body("loadActiveFarm")
    assert body.count("loadClimatePanel()") == 1
    assert "selectClimatePreset" not in body
    assert "selectClimateCustom" not in body
    assert "applyClimateCustomRange" not in body
    assert "climateMode" not in body  # a carga NÃO toca o modo


def test_nenhuma_outra_rotina_altera_o_modo_clima():
    # auditoria estática: apenas os controles do próprio painel (pílulas +
    # "Aplicar período do clima") escrevem em climateMode. A única outra
    # escrita permitida é a declaração inicial (let climateMode = {...}).
    writes = [m.start() for m in re.finditer(r"climateMode\s*=", INDEX)]
    assert writes, "nenhuma escrita em climateMode encontrada (estado inexistente?)"

    def body_span(fn):
        body = _extract_function_body(fn)
        start = INDEX.index(body)
        return start, start + len(body)

    allowed_spans = []
    for fn in ("selectClimatePreset", "selectClimateCustom", "applyClimateCustomRange"):
        s, e = body_span(fn)
        assert s != -1
        allowed_spans.append((s, e))

    decl = INDEX.index("let climateMode")
    decl_written = None
    for w in writes:
        if w >= decl and w < decl + 30:
            decl_written = w
            break

    offenders = []
    for w in writes:
        in_decl = (w == decl_written)
        in_ctl = any(s <= w < e for (s, e) in allowed_spans)
        if not (in_decl or in_ctl):
            offenders.append(w)
    assert not offenders, (
        f"climateMode está sendo escrito fora dos controles do painel: "
        f"posições {offenders}"
    )
    # cada controle escreve o modo exatamente uma vez
    for fn in ("selectClimatePreset", "selectClimateCustom", "applyClimateCustomRange"):
        s, e = body_span(fn)
        n = len(re.findall(r"climateMode\s*=", INDEX[s:e]))
        assert n == 1, f"{fn} deveria escrever climateMode 1×, escreveu {n}×"


def test_switch_screen_preserva_modo_e_restaura_pillulas():
    # navegação: switchScreen NÃO dispara/recarrega o clima e NUNCA escreve
    # em climateMode (reset silencioso proibido); ao voltar ao dashboard
    # restaura o estado visual das pílulas conforme o modo preservado
    body = _extract_function_body("switchScreen")
    assert "loadClimatePanel" not in body
    assert "selectClimatePreset" not in body
    assert "applyClimateCustomRange" not in body
    assert "climateMode =" not in body  # nunca zera/reescreve o modo
    # a restauração visual das pílulas ao voltar ao dashboard
    assert "climateSetPillActive" in body


def test_aplicars_desambiguados():
    # Causa raiz do FIX.3: o dashboard tinha DOIS "Personalizado + Aplicar"
    # idênticos (clima × timeline 3D). Agora cada um é explícito.
    assert "Aplicar período do clima" in INDEX          # painel de clima
    assert "Aplicar à timeline 3D" in INDEX             # timeline Sentinel
    assert "Período da análise climática:" in INDEX
    assert "altera apenas o painel de clima" in INDEX
    assert "Período da TIMELINE 3D" in INDEX            # tooltip da linha 3D


def test_log_diagnostico_climate_presente():
    # logs [CLIMATE] nos pontos-chave (DevTools → Console, filtro "[CLIMATE]")
    assert "console.debug('[CLIMATE]'" in INDEX
    for action in (
        "action: 'load_start'", "action: 'fetch'", "action: 'render'",
        "action: 'response_discarded_stale'", "action: 'response_discarded_mismatch'",
        "action: 'abort_previous'", "action: 'apply_custom'",
    ):
        assert action in INDEX


# ---------------------------------------------------------------------------
# PR #7-FIX.2 + FIX.3 — Teste COMPORTAMENTAL do fluxo COMPLETO em VM Node
#
# Extrai do index.html (a) o BLOCO REAL de controle do clima (declarações
# let + selectClimatePreset/selectClimateCustom/applyClimateCustomRange/
# loadClimatePanel + climateLog) e (b) a função REAL switchScreen, e executa
# os dois num sandbox Node com stubs mínimos de document/fetch, no MESMO
# contexto (mesma página), reproduzindo o ciclo de vida exato:
#
#   P1  carga da página (loadActiveFarm): 30d em voo (requisição A);
#   P2  usuário clica "Personalizado" (NÃO dispara) e aplica
#       2026-08-20 → 2026-09-07 (requisição B, start/end, 19 dias);
#   P3  B responde → renderiza 19 DIAS; A (30d) responde TARDE →
#       DESCARTADA; nenhum terceiro preset=30d é disparado;
#   P4  estado persiste: "Tentar novamente" reenvia o MESMO custom;
#       reabrir "Personalizado" reprefilcha as datas aplicadas;
#   P5  resposta INCOMPATÍVEL (período 30d para pedido custom) →
#       descartada pela validação semântica (2ª camada);
#   P6  validações client-side (start>end, vazio, >366 dias);
#   P7  custom → 15d (renderiza), 7d com resposta 30d → descartada,
#       15d → custom (renderiza);
#   P8  navegação: sair (map) e voltar (dash) — modo PRESERVADO, sem nova
#       requisição, pílulas restauradas (nunca reset silencioso);
#   P9  nenhuma chamada preset=30d depois do Aplicar custom.
# ---------------------------------------------------------------------------
_NODE = shutil.which("node")

_CLIMATE_BLOCK_JS = r"""
const fs = require('fs');
const vm = require('vm');
const BLOCK = fs.readFileSync(process.argv[2], 'utf8');
const SWITCH = fs.readFileSync(process.argv[3], 'utf8');

function makeClassList() {
  const set = new Set();
  return {
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    toggle: (c, force) => { const on = (force === undefined) ? !set.has(c) : !!force; if (on) set.add(c); else set.delete(c); return on; },
    contains: (c) => set.has(c),
  };
}
function makeEl(id) {
  return { id: id, value: '', innerHTML: '', hidden: false, classList: makeClassList(), style: {} };
}
const els = {};
function getEl(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; }
// pílulas do clima + abas de navegação como .tab-btn (switchScreen as varre)
const tabBtnEls = ['climate-preset-7d', 'climate-preset-15d', 'climate-preset-30d',
                   'climate-preset-custom', 'tab-dash', 'tab-map', 'tab-3d']
                   .map(getEl);
const screenEls = ['screen-dash', 'screen-map', 'screen-3d'].map(getEl);

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
  document: {
    getElementById: getEl,
    querySelectorAll: (sel) => {
      if (sel === '.tab-btn') return tabBtnEls;
      if (sel === '.screen-view') return screenEls;
      return [];
    },
  },
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
  // stubs usados apenas pela função REAL switchScreen (navegação)
  stopRenderLoop: () => {},
  startRenderLoop: () => {},
  onWindowResize: () => {},
  map: { invalidateSize() {} },
  setTimeout: (fn) => 0, // timers de navegação não precisam disparar no teste
  console: console,
};
vm.createContext(sandbox);
vm.runInContext(BLOCK, sandbox);      // estado + controles reais do clima
vm.runInContext(
  "var currentScreen = 'dash';\n" + SWITCH + "\n", sandbox); // switchScreen REAL
// let/const do BLOCO vivem no escopo léxico do contexto (não no objeto
// sandbox) — leitura via runInContext (mesma "página").
const getMode = () => vm.runInContext("climateMode ? { ...climateMode } : null", sandbox);

const flush = (n) => { n = n || 20; const p = Promise.resolve();
  let q = p; for (let i = 0; i < n; i++) q = q.then(() => {}); return q; };
const resp = (period) => ({ status: 200, ok: true, json: async () => ({ period: period }) });
const customPeriod = () => ({ start: '2026-08-20', end: '2026-09-07', days: 19, preset: null });
const p30Period = () => ({ start: '2026-08-09', end: '2026-09-07', days: 30, preset: '30d' });

(async () => {
  const out = { renderCounts: [] };
  const state = () => getEl('climate-state').innerHTML;
  const pillCustom = getEl('climate-preset-custom');

  // P1 — carga da página (loadActiveFarm chama loadClimatePanel() 1×):
  // 30d padrão em voo (requisição A)
  sandbox.loadClimatePanel();
  out.callA = calls[0] ? calls[0].url : null;

  // P2 — "Personalizado" NÃO dispara; Aplicar dispara B (start/end, 19 dias)
  sandbox.selectClimateCustom();
  out.callsAfterCustomClick = calls.length;
  getEl('climate-custom-start').value = '2026-08-20';
  getEl('climate-custom-end').value = '2026-09-07';
  sandbox.applyClimateCustomRange();
  out.callB = calls[1] ? calls[1].url : null;
  out.aAborted = (calls[0] && calls[0].signal) ? calls[0].signal.aborted : null;
  out.modeAfterApply = getMode();

  // P3 — B responde (19d) → renderiza; A (30d) responde TARDE → descarta
  if (calls[1]) { calls[1].resolve(resp(customPeriod())); }
  await flush();
  out.renderCounts.push(rendered.length);           // após custom: 1
  if (calls[0]) { calls[0].resolve(resp(p30Period())); }
  await flush();
  out.renderCounts.push(rendered.length);           // após 30d tardia: continua 1
  out.callsAfterLate30 = calls.length;              // nenhum 3º disparo

  // P4 — persistência do estado: "Tentar novamente" reenvia o MESMO custom
  sandbox.loadClimatePanel();
  out.callRetry = calls[calls.length - 1] ? calls[calls.length - 1].url : null;
  calls[calls.length - 1].resolve(resp(customPeriod()));
  await flush();
  out.renderCounts.push(rendered.length);           // renderiza o custom de novo
  // reabrir "Personalizado" reprefilcha as datas aplicadas (estado único)
  getEl('climate-custom-start').value = '';
  getEl('climate-custom-end').value = '';
  sandbox.selectClimateCustom();
  out.prefillStart = getEl('climate-custom-start').value;
  out.prefillEnd = getEl('climate-custom-end').value;

  // P5 — resposta INCOMPATÍVEL (30d) para pedido custom → validação semântica
  sandbox.loadClimatePanel();
  calls[calls.length - 1].resolve(resp(p30Period())); // período != pedido
  await flush();
  out.renderCounts.push(rendered.length);           // NADA de novo renderizado

  // P6 — validações client-side — nenhuma deve disparar requisição
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

  // P7 — custom → 15d (compatível renderiza); 7d com resposta 30d → descarta
  sandbox.selectClimatePreset('15d');
  out.call15 = calls[calls.length - 1] ? calls[calls.length - 1].url : null;
  out.modeAfter15d = getMode();
  calls[calls.length - 1].resolve(resp({ start: '2026-08-24', end: '2026-09-07', days: 15, preset: '15d' }));
  await flush();
  out.renderCounts.push(rendered.length);           // 15d renderizado
  sandbox.selectClimatePreset('7d');
  out.call7 = calls[calls.length - 1] ? calls[calls.length - 1].url : null;
  calls[calls.length - 1].resolve(resp(p30Period())); // preset errado → descarta
  await flush();
  out.renderCounts.push(rendered.length);           // continua igual
  // 15d/7d → custom (fluxo real de volta ao personalizado)
  sandbox.selectClimateCustom();
  getEl('climate-custom-start').value = '2026-08-20';
  getEl('climate-custom-end').value = '2026-09-07';
  sandbox.applyClimateCustomRange();
  out.callCustom2 = calls[calls.length - 1] ? calls[calls.length - 1].url : null;
  calls[calls.length - 1].resolve(resp(customPeriod()));
  await flush();
  out.renderCounts.push(rendered.length);           // custom renderizado

  // P8 — NAVEGAÇÃO (função REAL switchScreen): sair → voltar
  out.navCallsBefore = calls.length;
  out.modeBeforeNav = getMode();
  sandbox.switchScreen('map');
  out.pillActiveAfterMap = pillCustom.classList.contains('active');
  sandbox.switchScreen('dash');
  out.pillActiveAfterDash = pillCustom.classList.contains('active');
  out.modeAfterNav = getMode();
  out.navCallsAfter = calls.length;                 // navegação NÃO dispara clima
  out.renderCounts.push(rendered.length);           // nada novo renderizado

  // P9 — nenhuma chamada preset=30d depois do Aplicar custom (índice 1)
  out.no30dAfterCustom = calls.slice(2).every((c) => c.url.indexOf('preset=30d') === -1);
  out.totalCalls = calls.length;
  out.rendered = rendered.map((d) => d.period);
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


def _extract_switch_screen() -> str:
    """Extrai a função REAL switchScreen (comportamento de navegação)."""
    marker = "function switchScreen("
    fstart = INDEX.index(marker)
    i = INDEX.index("{", fstart)
    depth = 0
    for j in range(i, len(INDEX)):
        if INDEX[j] == "{":
            depth += 1
        elif INDEX[j] == "}":
            depth -= 1
            if depth == 0:
                return INDEX[fstart:j + 1]
    raise AssertionError("fim de switchScreen não encontrado")


def _run_vm(harness_extra: str = "") -> dict:
    block = _extract_climate_block()
    switch = _extract_switch_screen()
    with tempfile.TemporaryDirectory() as td:
        block_path = Path(td) / "climate_block.js"
        block_path.write_text(block, encoding="utf-8")
        switch_path = Path(td) / "switch_screen.js"
        switch_path.write_text(switch, encoding="utf-8")
        harness_path = Path(td) / "harness.js"
        harness_path.write_text(_CLIMATE_BLOCK_JS, encoding="utf-8")
        proc = subprocess.run(
            [_NODE, str(harness_path), str(block_path), str(switch_path)],
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
    return out


@pytest.mark.skipif(_NODE is None, reason="node indisponível no ambiente")
def test_fluxo_completo_custom_no_playtest_real():
    """Ciclo de vida COMPLETO do playtest (não duas promises isoladas):
    carga 30d em voo → Personalizado → Aplicar 20/08→07/09 → custom responde
    → 30d responde TARDIO → o painel PERMANECE em 19 DIAS e nenhuma rotina
    dispara um terceiro preset=30d."""
    out = _run_vm()

    # P1 — carga da página → preset 30d, SEM start/end
    assert out["callA"] is not None
    assert "preset=30d" in out["callA"]
    assert "start=" not in out["callA"] and "end=" not in out["callA"]

    # P2 — "Personalizado" NÃO dispara requisição; Aplicar usa start/end
    assert out["callsAfterCustomClick"] == 1
    assert out["callB"] is not None
    assert "start=2026-08-20" in out["callB"]
    assert "end=2026-09-07" in out["callB"]
    assert "preset=" not in out["callB"]
    assert out["aAborted"] is True  # 30d em voo foi cancelada
    # estado único explícito depois do Aplicar
    assert out["modeAfterApply"] == {
        "type": "custom", "preset": None,
        "start": "2026-08-20", "end": "2026-09-07",
    }

    # P3 — SÓ a resposta custom é renderizada (19 dias INCLUSIVOS);
    # a 30d tardia é descartada; nenhum terceiro disparo.
    assert out["renderCounts"][0] == 1
    assert out["renderCounts"][1] == 1
    assert out["rendered"][0]["start"] == "2026-08-20"
    assert out["rendered"][0]["end"] == "2026-09-07"
    assert out["rendered"][0]["days"] == 19
    assert all(p["days"] != 30 for p in out["rendered"])
    assert out["callsAfterLate30"] == 2

    # P4 — estado persiste: retry reenvia o MESMO custom (modo preservado)
    assert out["callRetry"] is not None
    assert "start=2026-08-20" in out["callRetry"]
    assert "end=2026-09-07" in out["callRetry"]
    assert "preset=" not in out["callRetry"]
    assert out["renderCounts"][2] == 2
    # reabrir "Personalizado" reprefilcha as datas aplicadas
    assert out["prefillStart"] == "2026-08-20"
    assert out["prefillEnd"] == "2026-09-07"

    # P5 — resposta incompatível (30d para pedido custom) → DESCARTADA
    assert out["renderCounts"][3] == out["renderCounts"][2]

    # P6 — validações client-side não disparam requisição e explicam o motivo
    assert out["noCallForInvalid"] is True
    assert out["callsAfterStartGtEnd"] == out["callsAfterEmpty"] == out["callsAfterTooLong"]
    assert "início" in out["msgStartGtEnd"].lower() or "fim" in out["msgStartGtEnd"].lower()
    assert "informe" in out["msgEmpty"].lower()
    assert "366" in out["msgTooLong"]

    # P7 — alternância: 15d usa preset (renderiza); 7d c/ resposta 30d descarta;
    # 15d → custom volta a start/end (renderiza)
    assert "preset=15d" in out["call15"]
    assert "start=" not in out["call15"] and "end=" not in out["call15"]
    assert out["modeAfter15d"] == {"type": "preset", "preset": "15d", "start": None, "end": None}
    assert out["renderCounts"][4] == out["renderCounts"][3] + 1
    assert out["rendered"][-2]["days"] == 15
    assert "preset=7d" in out["call7"]
    assert out["renderCounts"][5] == out["renderCounts"][4]  # 30d p/ pedido 7d → descarta
    assert "start=2026-08-20" in out["callCustom2"]
    assert "end=2026-09-07" in out["callCustom2"]
    assert "preset=" not in out["callCustom2"]
    assert out["renderCounts"][6] == out["renderCounts"][5] + 1
    assert out["rendered"][-1]["days"] == 19

    # P8 — NAVEGAÇÃO: modo preservado, sem nova requisição, pílulas restauradas
    assert out["modeBeforeNav"] == {"type": "custom", "preset": None,
                                    "start": "2026-08-20", "end": "2026-09-07"}
    assert out["navCallsBefore"] == out["navCallsAfter"]  # nada disparado
    assert out["renderCounts"][7] == out["renderCounts"][6]  # nada renderizado
    assert out["modeAfterNav"] == out["modeBeforeNav"]  # PRESERVADO (sem reset)
    assert out["pillActiveAfterMap"] is False  # fora do dashboard, sem destaque
    assert out["pillActiveAfterDash"] is True  # ao voltar, pílula custom restaurada

    # P9 — NENHUM terceiro preset=30d depois do Aplicar custom
    assert out["no30dAfterCustom"] is True
