"""
PR #4 — Pipeline do Simulador 3D (regressões do bug "terreno branco").

Cobre o fluxo completo exigido pelo PR:
  fazenda → textura (Sentinel nativa OU procedural) → PNG autenticado →
  heightmap (DEM GL-30) → fallback explícito → What-If → contrato.

Garantias específicas deste arquivo:
- a farm DEMO (id=1) não pode depender do dataset `sentinel-21KXQ-*`
  (fora do Git): em clone limpo ela cai no fallback EXPLÍCITO e o PNG
  continua sendo servido (malha nunca fica branca por 404);
- fazenda criada pelo usuário funciona no mesmo pipeline autenticado;
- `PUBLIC_BASE_URL` continua sendo respeitado nas URLs de asset;
- heightmap sem tile DEM responde `available=false` com motivo claro;
- usuário não autorizado/alheio continua bloqueado (401/404);
- What-If mantém o contrato de 9 campos.
"""
import io
import json

import pytest
from PIL import Image

from conftest import REPO_ROOT, auth

FARM_BODY = {
    "name": "Fazenda Pipeline 3D",
    "city": "Bauru - SP",
    "total_area": 30.0,
    "talhao_name": "Talhão Principal",
    "crop": "Soja / Milho Safrinha",
    "latitude": -22.3,
    "longitude": -50.5,
    # Polígono pequeno quadrado [lat, lon] (formato do gerador de texturas)
    "kml_coordinates": json.dumps([
        [-22.301, -50.501],
        [-22.300, -50.500],
        [-22.299, -50.501],
        [-22.300, -50.502],
    ]),
}

PUBLIC_BASE_URL = "http://testserver"  # definido em tests/conftest.py


def _assert_valid_png(content: bytes, min_bytes: int = 200) -> None:
    assert len(content) > min_bytes, "resposta vazia ou truncada"
    assert content[:8] == b"\x89PNG\r\n\x1a\n", "assinatura PNG ausente"
    img = Image.open(io.BytesIO(content))
    img.load()
    assert img.format == "PNG"


# ---------------------------------------------------------------------------
# 1. Farm DEMO (id=1) — clone limpo SEM dataset sentinel-21KXQ-*
# ---------------------------------------------------------------------------
class TestDemoFarmTexture:
    def test_clone_limpo_nao_tem_dataset_sentinel(self):
        """Pré-condição de higiene: o dataset não é versionado no Git."""
        assert not (REPO_ROOT / "sentinel-21KXQ-2025-04-07").exists()

    def test_demo_metadados_200_e_url_utilizavel(self, client, admin_token):
        r = client.get(
            "/api/talhao/1/texture?layer=ndvi&date=2025-04-07",
            headers=auth(admin_token),
        )
        assert r.status_code == 200
        d = r.json()
        # Nunca mais `path_pattern` sem URL: o frontend tem que receber algo
        # baixável (nativo OU procedural) — senão a malha fica branca.
        assert d["type"] in {"native", "dynamic"}
        assert d["data_origin"] in {"sentinel", "procedural"}
        assert d["texture_url"], "texture_url ausente — causa do terreno branco"
        assert ".png" in d["texture_url"]

    def test_demo_png_200_valido_sem_dataset(self, client, admin_token):
        """Sem sentinel nativo, o PNG deve ser gerado sob demanda (fallback)."""
        assert not (REPO_ROOT / "sentinel-21KXQ-2025-04-07").exists()
        r = client.get("/api/talhao/1/texture.png?layer=ndvi", headers=auth(admin_token))
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        _assert_valid_png(r.content)

    def test_demo_analytics_marca_origem_dos_dados(self, client, admin_token):
        # Gera o fallback procedural para a farm demo e verifica a origem
        # marcada na série temporal (diagnóstico real × aproximado).
        assert client.get("/api/talhao/1/texture.png?layer=ndvi", headers=auth(admin_token)).status_code == 200
        d = client.get("/api/analytics/farm/1?layer=ndvi", headers=auth(admin_token)).json()
        origins = {p.get("data_origin") for p in d["timeline"]}
        assert origins <= {"sentinel", "procedural", "fallback"}
        assert "procedural" in origins  # clone limpo → fonte explícita


# ---------------------------------------------------------------------------
# 2. Fazenda DINÂMICA (criada pelo usuário) — mesmo pipeline
# ---------------------------------------------------------------------------
class TestDynamicFarmTexture:
    def test_farm_dinamica_pipeline_completo(self, client, admin_token):
        fid = client.post("/api/farms", json=FARM_BODY, headers=auth(admin_token)).json()["id"]

        # Metadados: URL autenticada ABSOLUTA respeitando PUBLIC_BASE_URL
        meta = client.get(f"/api/talhao/{fid}/texture?layer=ndvi", headers=auth(admin_token))
        assert meta.status_code == 200
        d = meta.json()
        assert d["type"] == "dynamic"
        assert d["data_origin"] == "procedural"
        assert d["texture_url"].startswith(PUBLIC_BASE_URL + "/api/talhao/")
        assert d["texture_url"].endswith("?layer=ndvi")

        # PNG autenticado: 200, image/png, não vazio, válido
        png = client.get(d["texture_url"], headers=auth(admin_token))
        assert png.status_code == 200
        assert png.headers["content-type"] == "image/png"
        _assert_valid_png(png.content)

    def test_farm_dinamica_owner_acessa_propria_textura(self, client, user_token):
        """Usuário comum acessa a textura da PRÓPRIA fazenda (owner)."""
        fid = client.post("/api/farms", json=FARM_BODY, headers=auth(user_token)).json()["id"]
        r = client.get(f"/api/talhao/{fid}/texture.png?layer=ndvi", headers=auth(user_token))
        assert r.status_code == 200
        _assert_valid_png(r.content)

    def test_farm_alheia_continua_bloqueada_404(self, client, user_token, admin_token):
        fid = client.post("/api/farms", json=FARM_BODY, headers=auth(admin_token)).json()["id"]
        assert client.get(f"/api/talhao/{fid}/texture.png", headers=auth(user_token)).status_code == 404
        assert client.get(f"/api/talhao/{fid}/texture", headers=auth(user_token)).status_code == 404

    def test_assets_autenticados_exigem_token_401(self, client):
        assert client.get("/api/talhao/1/texture.png?layer=ndvi").status_code == 401
        assert client.get("/api/talhao/1/texture?layer=ndvi").status_code == 401
        assert client.get("/api/talhao/1/heightmap.png?size=256").status_code == 401

    def test_assets_nao_reacessiveis_via_static_publico(self, client, admin_token):
        """PR #4 — o mount público /dynamic_talhoes foi removido: os PNGs por
        fazenda só existem atrás das rotas autenticadas. Acesso direto estático
        deve ser negado mesmo para quem tem o nome exato do arquivo."""
        assert client.get("/api/talhao/1/texture.png?layer=ndvi", headers=auth(admin_token)).status_code == 200
        assert client.get("/dynamic_talhoes/farm_1_talhao_1/ndvi_cloudless_min_max.png").status_code == 404


# ---------------------------------------------------------------------------
# 3. HEIGHTMAP (Copernicus DEM GL-30) — fallback explícito em clone limpo
# ---------------------------------------------------------------------------
class TestHeightmapFallback:
    def test_heightmap_meta_200_sem_tile(self, client, admin_token):
        """Sem tile DEM (clone limpo), o contrato informa indisponibilidade."""
        r = client.get("/api/talhao/1/heightmap?size=256", headers=auth(admin_token))
        assert r.status_code == 200
        d = r.json()
        assert d["available"] is False
        assert d["source"] == "none"
        assert d["heightmap_url"] is None
        assert d["reason"] and "DEM" in d["reason"]

    def test_heightmap_png_404_explicito_sem_tile(self, client, admin_token):
        r = client.get("/api/talhao/1/heightmap.png?size=256", headers=auth(admin_token))
        assert r.status_code == 404
        assert "Heightmap indisponível" in r.json()["detail"]

    def test_heightmap_meta_user_nao_autorizado_401(self, client):
        assert client.get("/api/talhao/1/heightmap").status_code == 401


# ---------------------------------------------------------------------------
# 4. WHAT-IF — contrato do cenário simulado
# ---------------------------------------------------------------------------
class TestWhatIfContract:
    REQUIRED_FIELDS = {
        "delta_ndvi", "delta_yield_sc_ha", "total_production_sc",
        "financial_impact_brl", "displacement_scale_3d",
        "gross_financial_gain_brl", "net_financial_gain_brl",
        "crop", "bag_price_brl",
    }

    def test_retorna_contrato_esperado(self, client, admin_token):
        r = client.post("/api/simulation/what-if", headers=auth(admin_token), json={
            "nitrogen_kg": 50, "water_mm": 10, "pest_pressure_pct": 5, "area_ha": 42.54,
        })
        assert r.status_code == 200
        d = r.json()
        assert set(d.keys()) == self.REQUIRED_FIELDS
        assert d["delta_ndvi"] == pytest.approx(0.0745, abs=1e-3)
        assert d["displacement_scale_3d"] >= 1.0
        assert d["crop"] == "Soja / Milho Safrinha"


# ---------------------------------------------------------------------------
# 5. Frontend servido pelo backend (clone limpo: só uvicorn) — higiene/segurança
# ---------------------------------------------------------------------------
class TestFrontendSameOrigin:
    def test_raiz_serve_frontend(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "Simulador 3D" in r.text or "Orion Agro" in r.text

    def test_app_js_servido(self, client):
        r = client.get("/app.js")
        assert r.status_code == 200
        assert "application/javascript" in r.headers["content-type"]

    def test_arquivos_internos_do_backend_nao_expostos(self, client):
        # O catch-all nunca serve .env nem diretórios internos.
        assert client.get("/backend/.env").status_code == 404
        assert client.get("/backend/config.py").status_code == 404
        assert client.get("/config.py").status_code == 404

    def test_api_desconhecida_continua_404_json(self, client):
        r = client.get("/api/rota/inexistente")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# 6. Clone limpo: fluxo completo da fazenda dinâmica (Fase 13)
# ---------------------------------------------------------------------------
class TestCleanCloneEndToEnd:
    def test_fluxo_completo_farm_dinamica(self, client, admin_token):
        # Mesma sequência do frontend: metadados → PNG autenticado → heightmap.
        fid = client.post("/api/farms", json=FARM_BODY, headers=auth(admin_token)).json()["id"]
        meta = client.get(f"/api/talhao/{fid}/texture?layer=ndvi", headers=auth(admin_token)).json()
        assert client.get(meta["texture_url"], headers=auth(admin_token)).status_code == 200
        hm = client.get(f"/api/talhao/{fid}/heightmap", headers=auth(admin_token)).json()
        assert hm["available"] is False  # clone limpo sem tile → fallback explícito
        assert hm["reason"] is not None
        sim = client.post("/api/simulation/what-if", headers=auth(admin_token), json={
            "nitrogen_kg": 20, "water_mm": 0, "pest_pressure_pct": 0, "area_ha": 30.0,
        })
        assert sim.status_code == 200
