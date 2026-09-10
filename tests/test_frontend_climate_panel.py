"""
PR #7 — Contratos do frontend do painel "Clima & Condições Agronômicas".

Mesmo padrão dos testes de frontend existentes: verificação estática do
HTML/JS real (o teste de bootstrap da VM Node já executa o script inline).

Garantias cobradas:
- painel INTEGRADO ao Dashboard (mesma tela FAZENDA → TALHÃO → LOCALIZAÇÃO);
- presets 7d/15d/30d + período personalizado;
- o frontend NUNCA consulta a NASA POWER diretamente (Fase 2);
- estados explícitos de erro/ausência (Fase 9) — sem dado inventado;
- separação de categorias: dado / calculado / interpretação (Fase 5);
- proveniência + confiança visíveis (Fase 8);
- dashboard.html legada rotulada como DADOS DEMONSTRATIVOS (Fase 9);
- ClimateService em app.js.
"""
from pathlib import Path

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
