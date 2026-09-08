"""
1. Autenticação e Segurança:
   registro, login (bcrypt), migração de hashes legados,
   emissão e validação de tokens JWT.
"""
import time

import pytest
from jose import jwt

import models
import security
from conftest import TEST_JWT_SECRET, auth, login, register_user, unique_email
from database import SessionLocal


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------
class TestRegistro:
    def test_registro_sucesso_nao_devolve_senha(self, client):
        email = unique_email("reg")
        r = client.post("/api/auth/register", json={
            "name": "Novo Produtor", "email": email,
            "password": "segredo123", "role": "Produtor Rural",
        })
        assert r.status_code == 201
        d = r.json()
        assert d["email"] == email
        assert d["name"] == "Novo Produtor"
        assert "password" not in d and "hashed_password" not in d

    def test_registro_duplicado_400(self, client):
        email = unique_email("dup")
        register_user(client, email)
        r = client.post("/api/auth/register", json={
            "name": "Duplicado", "email": email, "password": "abc12345",
        })
        assert r.status_code == 400

    @pytest.mark.parametrize("email", ["nao-eh-email", "a b@c.com", ""])
    def test_registro_email_invalido_422(self, client, email):
        r = client.post("/api/auth/register", json={
            "name": "Inválido", "email": email, "password": "abc12345",
        })
        assert r.status_code == 422

    @pytest.mark.parametrize("password", ["123", "12345"])
    def test_registro_senha_curta_422(self, client, password):
        r = client.post("/api/auth/register", json={
            "name": "Senha Curta", "email": unique_email("curta"), "password": password,
        })
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Registro — restrição de papel (anti auto-registro como admin)
# ---------------------------------------------------------------------------
class TestRegistroPapelForcado:
    """
    O auto-serviço NUNCA define privilégios: o campo `role` enviado no
    payload é ignorado e a conta recebe o papel padrão público
    (`main.DEFAULT_PUBLIC_ROLE`). Admin só via seed interno.
    """

    @pytest.mark.parametrize("role_enviado", ["admin", "Admin", "root", "user", "superuser"])
    def test_qualquer_role_enviado_e_ignorado(self, client, role_enviado):
        email = unique_email("priv")
        r = client.post("/api/auth/register", json={
            "name": "Tentativa Privilegio", "email": email,
            "password": "segredo123", "role": role_enviado,
        })
        assert r.status_code == 201
        assert r.json()["role"] == "Produtor Rural", (
            f"o papel enviado '{role_enviado}' foi aceito — auto-registro privilegiado!"
        )

    def test_tentativa_explicita_de_admin_nao_concede_prerrogativas(self, client):
        email = unique_email("evad")
        r = client.post("/api/auth/register", json={
            "name": "Evadidor", "email": email,
            "password": "segredo123", "role": "admin",
        })
        assert r.status_code == 201
        assert r.json()["role"] == "Produtor Rural"

        # No token JWT (e no payload do login) o papel continua o público:
        d = login(client, email, "segredo123")
        assert d["user"]["role"] == "Produtor Rural"
        token = d["access_token"]

        # ...e o RBAC de fato bloqueia as rotas administrativas (403, não 200):
        assert client.delete("/api/farms/1", headers=auth(token)).status_code == 403
        r = client.put("/api/farms/1", headers=auth(token), json={
            "name": "Ataque", "city": "X", "total_area": 1, "talhao_name": "T",
            "crop": "Soja", "latitude": -22.2, "longitude": -51.95,
        })
        assert r.status_code == 403

    def test_seed_admin_mantem_papel_administrativo(self, client):
        """Isolamento: a restrição do registro não afeta o admin semeado."""
        d = login(client, "admin@orion.com", "123456")
        assert d["user"]["role"] == "admin"


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
class TestLogin:
    def test_login_devolve_token_estruturado(self, client):
        d = login(client, "admin@orion.com", "123456")
        assert d["token_type"] == "bearer"
        assert len(d["access_token"].split(".")) == 3  # JWT: header.payload.signature
        assert d["expires_in"] == 720 * 60
        assert d["user"]["email"] == "admin@orion.com"
        assert d["user"]["role"] == "admin"

    def test_login_senha_errada_401(self, client):
        r = client.post("/api/auth/login", json={
            "email": "admin@orion.com", "password": "senha-errada",
        })
        assert r.status_code == 401

    def test_login_usuario_inexistente_401(self, client):
        r = client.post("/api/auth/login", json={
            "email": unique_email("fantasma"), "password": "abc12345",
        })
        assert r.status_code == 401

    def test_login_email_invalido_422(self, client):
        r = client.post("/api/auth/login", json={"email": "abc", "password": "x123456"})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Bcrypt
# ---------------------------------------------------------------------------
class TestBcrypt:
    def test_hash_e_verificacao(self):
        h = security.hash_password("segredo123")
        assert h.startswith("$2")            # formato bcrypt
        assert h != "segredo123"             # nunca texto puro
        assert security.verify_password("segredo123", h) is True
        assert security.verify_password("outra-senha", h) is False

    def test_hash_salteado_nao_deterministico(self):
        h1 = security.hash_password("mesma-senha")
        h2 = security.hash_password("mesma-senha")
        assert h1 != h2                       # salt aleatório
        assert security.verify_password("mesma-senha", h1)
        assert security.verify_password("mesma-senha", h2)

    def test_verificar_valor_legado_nao_hash_retorna_false(self):
        # Valor armazenado que NUNCA foi hash (legado em texto puro)
        assert security.verify_password("123456", "123456") is False

    def test_migracao_transparente_de_hash_legado(self, client):
        email = unique_email("legado")
        with SessionLocal() as db:
            db.add(models.User(name="Legado", email=email,
                               hashed_password="senha123", role="user"))
            db.commit()

        # 1º login: senha legada em texto puro → 200 + rehash
        assert login(client, email, "senha123")["access_token"]
        with SessionLocal() as db:
            stored = db.query(models.User).filter(models.User.email == email).first()
            assert stored.hashed_password.startswith("$2"), "senha legada não foi rehashed"

        # 2º login: agora validado via bcrypt
        assert client.post("/api/auth/login",
                           json={"email": email, "password": "senha123"}).status_code == 200
        assert client.post("/api/auth/login",
                           json={"email": email, "password": "errada"}).status_code == 401


# ---------------------------------------------------------------------------
# JWT — emissão e validação
# ---------------------------------------------------------------------------
class TestJWT:
    def test_emissao_inclui_claims(self, client):
        from database import SessionLocal
        with SessionLocal() as db:
            user = db.query(models.User).filter(models.User.email == "admin@orion.com").first()
        token = security.create_access_token(user)
        payload = jwt.decode(token, TEST_JWT_SECRET, algorithms=["HS256"])
        assert payload["sub"] == str(user.id)
        assert payload["role"] == user.role
        assert payload["email"] == user.email
        assert payload["exp"] > int(time.time())

    def test_token_valido_authentica(self, client, admin_token):
        r = client.post("/api/simulation/what-if", headers=auth(admin_token), json={
            "nitrogen_kg": 10, "water_mm": 0, "pest_pressure_pct": 0, "area_ha": 10,
        })
        assert r.status_code == 200

    def test_token_expirado_401(self, client):
        expired = jwt.encode(
            {"sub": "1", "exp": int(time.time()) - 300, "role": "admin"},
            TEST_JWT_SECRET, algorithm="HS256",
        )
        r = client.post("/api/simulation/what-if", headers=auth(expired), json={
            "nitrogen_kg": 10, "water_mm": 0, "pest_pressure_pct": 0, "area_ha": 10,
        })
        assert r.status_code == 401

    def test_token_assinatura_invalida_401(self, client):
        forged = jwt.encode(
            {"sub": "1", "exp": int(time.time()) + 3600, "role": "admin"},
            "chave-diferente-invalida", algorithm="HS256",
        )
        r = client.post("/api/simulation/what-if", headers=auth(forged), json={
            "nitrogen_kg": 10, "water_mm": 0, "pest_pressure_pct": 0, "area_ha": 10,
        })
        assert r.status_code == 401

    def test_token_sub_inexistente_401(self, client):
        ghost = jwt.encode(
            {"sub": "999999", "exp": int(time.time()) + 3600, "role": "admin"},
            TEST_JWT_SECRET, algorithm="HS256",
        )
        r = client.post("/api/simulation/what-if", headers=auth(ghost), json={
            "nitrogen_kg": 10, "water_mm": 0, "pest_pressure_pct": 0, "area_ha": 10,
        })
        assert r.status_code == 401
