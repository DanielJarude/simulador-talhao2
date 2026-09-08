"""
2. RBAC — Controle de Acesso Baseado em Papéis:
   200 para admin, 403 para papéis insuficientes, 401 para ausência/inválido
   de token (autenticação precede autorização).
"""
import pytest

from conftest import auth, login, register_user, unique_email

FARM_BODY = {
    "name": "Fazenda RBAC",
    "city": "Marília - SP",
    "total_area": 20.0,
    "talhao_name": "Talhão RBAC",
    "crop": "Soja",
    "latitude": -22.2,
    "longitude": -51.95,
}
WHATIF_BODY = {"nitrogen_kg": 10, "water_mm": 0, "pest_pressure_pct": 0, "area_ha": 10.0}


def _create_farm(client, token: str) -> int:
    r = client.post("/api/farms", json=FARM_BODY, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


class TestRBACAdmin:
    def test_admin_atualiza_200(self, client, admin_token):
        fid = _create_farm(client, admin_token)
        r = client.put(f"/api/farms/{fid}", json=FARM_BODY, headers=auth(admin_token))
        assert r.status_code == 200
        assert r.json()["id"] == fid

    def test_admin_exclui_200(self, client, admin_token):
        fid = _create_farm(client, admin_token)
        assert client.delete(f"/api/farms/{fid}", headers=auth(admin_token)).status_code == 200
        assert client.get(f"/api/farms/{fid}", headers=auth(admin_token)).status_code == 404

    def test_admin_pode_tambem_criar_e_simular(self, client, admin_token):
        assert _create_farm(client, admin_token) > 0
        r = client.post("/api/simulation/what-if", json=WHATIF_BODY, headers=auth(admin_token))
        assert r.status_code == 200


class TestRBACUsuarioComum:
    # PR #3 — usuário comum GERENCIA a PRÓPRIA fazenda (200), mas NÃO a de outro.
    def test_usuario_excluir_propria_200(self, client, user_token):
        fid = _create_farm(client, user_token)          # uso padrão permitido
        r = client.delete(f"/api/farms/{fid}", headers=auth(user_token))
        assert r.status_code == 200
        assert client.get(f"/api/farms/{fid}", headers=auth(user_token)).status_code == 404

    def test_usuario_atualizar_propria_200(self, client, user_token):
        fid = _create_farm(client, user_token)
        r = client.put(f"/api/farms/{fid}", json=FARM_BODY, headers=auth(user_token))
        assert r.status_code == 200
        assert client.get(f"/api/farms/{fid}", headers=auth(user_token)).status_code == 200

    def test_usuario_papel_generico_user_403_em_farm_alheia(self, client):
        # Farm de OUTRO usuário (criada por um 2º usuário) → 404 para um 3º
        # usuário comum (PR #3: não expõe a existência do recurso alheio).
        # A farm demo (id=1) é compartilhada e NÃO é tocada aqui.
        owner_email = unique_email("dono")
        register_user(client, owner_email)
        owner_token = login(client, owner_email, "abc12345")["access_token"]
        fid = _create_farm(client, owner_token)

        intruso_email = unique_email("intruso")
        register_user(client, intruso_email, role="user")
        intruso_token = login(client, intruso_email, "abc12345")["access_token"]
        assert client.delete(f"/api/farms/{fid}", headers=auth(intruso_token)).status_code == 404
        assert client.put(f"/api/farms/{fid}", json=FARM_BODY, headers=auth(intruso_token)).status_code == 404

    def test_usuario_mantem_uso_padrao_200(self, client, user_token):
        assert _create_farm(client, user_token) > 0
        r = client.post("/api/simulation/what-if", json=WHATIF_BODY, headers=auth(user_token))
        assert r.status_code == 200

    def test_usuario_nao_ve_farms_de_outros(self, client, user_token):
        # Usuário comum só enxerga as próprias fazendas na listagem.
        _create_farm(client, user_token)
        r = client.get("/api/farms", headers=auth(user_token))
        assert r.status_code == 200
        body = r.json()
        assert all(f["owner_id"] is not None for f in body)
        # A farm demo (id=1, do admin) NÃO aparece para o usuário comum.
        assert all(f["id"] != 1 for f in body)


class TestRBACSemToken:
    @pytest.mark.parametrize("method,path", [
        ("POST", "/api/farms"),
        ("PUT", "/api/farms/1"),
        ("DELETE", "/api/farms/1"),
        ("POST", "/api/simulation/what-if"),
    ])
    def test_anonimo_401(self, client, method, path):
        r = client.request(method, path, json=FARM_BODY if method in ("POST", "PUT") else WHATIF_BODY)
        assert r.status_code == 401

    def test_token_inválido_401(self, client):
        r = client.put("/api/farms/1", json=FARM_BODY, headers=auth("abc.def.ghi"))
        assert r.status_code == 401

    def test_token_nao_jwt_401(self, client):
        r = client.delete("/api/farms/1", headers=auth("token-qualquer"))
        assert r.status_code == 401


class TestLeiturasPrivadas:
    """PR #3 — leituras de fazenda exigem autenticação (401 anônimo).

    Para qualquer usuário autenticado (mesmo comum) a farm demo (id=1,
    compartilhada) é legível (200). Rotas de propriedade alheia retornam 404.
    """
    @pytest.mark.parametrize("path", [
        "/api/farms",
        "/api/farms/1",
        "/api/talhao/1/texture?layer=ndvi",
        "/api/analytics/farm/1",
        "/api/weather/farm/1",
        "/api/reports/farm/1/pdf",
    ])
    def test_get_sem_token_401(self, client, path):
        # Autenticação precede autorização: anônimo → sempre 401.
        assert client.get(path).status_code == 401

    @pytest.mark.parametrize("path", [
        "/api/farms",
        "/api/farms/1",
        "/api/talhao/1/texture?layer=ndvi",
        "/api/analytics/farm/1",
        "/api/weather/farm/1",
    ])
    def test_get_com_token_200_farm_compartilhada(self, client, user_token, path):
        # Qualquer usuário autenticado lê a farm demo compartilhada (200).
        assert client.get(path, headers=auth(user_token)).status_code == 200

    def test_farm_alheia_404_para_usuario_comum(self, client, user_token, admin_token):
        fid = _create_farm(client, admin_token)  # dono = admin
        # Usuário comum não enxerga a fazenda alheia (404, não expõe existência).
        assert client.get(f"/api/farms/{fid}", headers=auth(user_token)).status_code == 404
        # O dono (admin) enxerga normalmente.
        assert client.get(f"/api/farms/{fid}", headers=auth(admin_token)).status_code == 200

    def test_dates_segue_publico_200(self, client):
        # Endpoint de catálogo (sem dado de fazenda) continua público.
        assert client.get("/api/talhao/dates").status_code == 200
