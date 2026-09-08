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
        assert client.get(f"/api/farms/{fid}").status_code == 404

    def test_admin_pode_tambem_criar_e_simular(self, client, admin_token):
        assert _create_farm(client, admin_token) > 0
        r = client.post("/api/simulation/what-if", json=WHATIF_BODY, headers=auth(admin_token))
        assert r.status_code == 200


class TestRBACUsuarioComum:
    def test_usuario_excluir_403_e_dados_intactos(self, client, user_token):
        fid = _create_farm(client, user_token)          # uso padrão permitido
        r = client.delete(f"/api/farms/{fid}", headers=auth(user_token))
        assert r.status_code == 403
        assert client.get(f"/api/farms/{fid}").status_code == 200  # NADA foi excluído

    def test_usuario_atualizar_403(self, client, user_token):
        fid = _create_farm(client, user_token)
        r = client.put(f"/api/farms/{fid}", json=FARM_BODY, headers=auth(user_token))
        assert r.status_code == 403
        assert client.get(f"/api/farms/{fid}").status_code == 200  # NADA foi alterado

    def test_usuario_papel_generico_user_403(self, client):
        email = unique_email("generico")
        register_user(client, email, role="user")
        token = login(client, email, "abc12345")["access_token"]
        fid = _create_farm(client, token)
        assert client.delete(f"/api/farms/{fid}", headers=auth(token)).status_code == 403

    def test_usuario_mantem_uso_padrao_200(self, client, user_token):
        assert _create_farm(client, user_token) > 0
        r = client.post("/api/simulation/what-if", json=WHATIF_BODY, headers=auth(user_token))
        assert r.status_code == 200

    def test_mensagem_403_clara(self, client, user_token):
        fid = _create_farm(client, user_token)
        r = client.delete(f"/api/farms/{fid}", headers=auth(user_token))
        detail = r.json()["detail"]
        assert "admin" in detail
        assert "Produtor Rural" in detail


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


class TestLeiturasPublicas:
    @pytest.mark.parametrize("path", [
        "/api/farms",
        "/api/farms/1",
        "/api/talhao/dates",
        "/api/talhao/1/texture?layer=ndvi",
        "/api/analytics/farm/1",
        "/api/weather/farm/1",
        "/api/reports/farm/1/pdf",
    ])
    def test_get_publico_sem_token_200(self, client, path):
        assert client.get(path).status_code == 200
