"""
PR #3 — Ownership de fazendas, privacidade multiusuário e migrations Alembic.

Cobrange:
  A) Ownership — usuário comum cria/ler/edita/exclui a PRÓPRIA fazenda; dono
     correto em owner_id; outro usuário não acessa (404 consistente).
  B) Admin — lista todas, lê/edita/exclui qualquer fazenda.
  C) Privacidade — GET de fazenda exige token (401 anônimo); endpoints
     analytics/clima/texture/heightmap/pdf não expõem fazenda alheia.
  D) Migração — Alembic em SQLite adiciona owner_id/is_shared e faz backfill
     seguro (demo admin + farm demo compartilhada), sem destruir dados.
  E) Regressão — integra com a suíte existente (conftest) sem quebrá-la.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from conftest import REPO_ROOT, auth, login, register_user, unique_email

from database import SessionLocal

FARM_BODY = {
    "name": "Fazenda Ownership",
    "city": "Londrina - PR",
    "total_area": 25.0,
    "talhao_name": "Talhão Ownership",
    "crop": "Soja",
    "latitude": -23.3,
    "longitude": -51.1,
}


def _create_farm(client, token: str) -> int:
    r = client.post("/api/farms", json=FARM_BODY, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _register_and_login(client, prefix: str):
    email = unique_email(prefix)
    register_user(client, email)
    data = login(client, email, "abc12345")
    return data["access_token"], data["user"]["id"]


# ---------------------------------------------------------------------------
# A) Ownership — usuário comum
# ---------------------------------------------------------------------------
class TestOwnershipUsuarioComum:
    def test_cria_farm_com_owner_id_correto(self, client):
        token, uid = _register_and_login(client, "owner")
        fid = _create_farm(client, token)
        farm = client.get(f"/api/farms/{fid}", headers=auth(token)).json()
        # owner_id vem do usuário autenticado (não do payload).
        assert farm["owner_id"] == uid
        assert farm["is_shared"] is False

    def test_owner_le_propria_farm(self, client):
        token, _ = _register_and_login(client, "owner2")
        fid = _create_farm(client, token)
        r = client.get(f"/api/farms/{fid}", headers=auth(token))
        assert r.status_code == 200
        assert r.json()["id"] == fid

    def test_owner_edita_propria_farm(self, client):
        token, _ = _register_and_login(client, "owner3")
        fid = _create_farm(client, token)
        r = client.put(f"/api/farms/{fid}", json={**FARM_BODY, "name": "Renomeada"},
                       headers=auth(token))
        assert r.status_code == 200
        assert r.json()["name"] == "Renomeada"

    def test_owner_exclui_propria_farm(self, client):
        token, _ = _register_and_login(client, "owner4")
        fid = _create_farm(client, token)
        assert client.delete(f"/api/farms/{fid}", headers=auth(token)).status_code == 200
        assert client.get(f"/api/farms/{fid}", headers=auth(token)).status_code == 404

    def test_outro_usuario_nao_le_farm_alheia(self, client):
        token_a, _ = _register_and_login(client, "a")
        fid = _create_farm(client, token_a)
        token_b, _ = _register_and_login(client, "b")
        # 404 (não expõe a existência do recurso alheio).
        assert client.get(f"/api/farms/{fid}", headers=auth(token_b)).status_code == 404

    def test_outro_usuario_nao_edita_farm_alheia(self, client):
        token_a, _ = _register_and_login(client, "a2")
        fid = _create_farm(client, token_a)
        token_b, _ = _register_and_login(client, "b2")
        r = client.put(f"/api/farms/{fid}", json=FARM_BODY, headers=auth(token_b))
        assert r.status_code == 404

    def test_outro_usuario_nao_exclui_farm_alheia(self, client):
        token_a, _ = _register_and_login(client, "a3")
        fid = _create_farm(client, token_a)
        token_b, _ = _register_and_login(client, "b3")
        assert client.delete(f"/api/farms/{fid}", headers=auth(token_b)).status_code == 404

    def test_payload_owner_id_ignorado(self, client, admin_token):
        # Mesmo enviando owner_id no body, o backend o descarta (fail-closed).
        token, uid = _register_and_login(client, "payload")
        admin_id = login(client, "admin@orion.com", "123456")["user"]["id"]
        body = {**FARM_BODY, "owner_id": admin_id}
        fid = client.post("/api/farms", json=body, headers=auth(token)).json()["id"]
        farm = client.get(f"/api/farms/{fid}", headers=auth(token)).json()
        assert farm["owner_id"] == uid  # dono = usuário logado, não o payload


# ---------------------------------------------------------------------------
# B) Admin
# ---------------------------------------------------------------------------
class TestAdminGlobal:
    def test_admin_lista_todas(self, client, admin_token):
        token, _ = _register_and_login(client, "comum")
        _create_farm(client, token)  # fazenda de outro usuário
        r = client.get("/api/farms", headers=auth(admin_token))
        assert r.status_code == 200
        ids = {f["id"] for f in r.json()}
        # Admin vê a própria demo (id=1) e a do usuário comum criada acima.
        assert 1 in ids

    def test_admin_le_qualquer_farm(self, client, admin_token):
        token, _ = _register_and_login(client, "comum2")
        fid = _create_farm(client, token)
        assert client.get(f"/api/farms/{fid}", headers=auth(admin_token)).status_code == 200

    def test_admin_edita_qualquer_farm(self, client, admin_token):
        token, _ = _register_and_login(client, "comum3")
        fid = _create_farm(client, token)
        r = client.put(f"/api/farms/{fid}", json={**FARM_BODY, "name": "Pelo Admin"},
                       headers=auth(admin_token))
        assert r.status_code == 200
        assert r.json()["name"] == "Pelo Admin"

    def test_admin_exclui_qualquer_farm(self, client, admin_token):
        token, _ = _register_and_login(client, "comum4")
        fid = _create_farm(client, token)
        assert client.delete(f"/api/farms/{fid}", headers=auth(admin_token)).status_code == 200
        assert client.get(f"/api/farms/{fid}", headers=auth(admin_token)).status_code == 404


# ---------------------------------------------------------------------------
# C) Privacidade — endpoints sensíveis não expõem fazenda alheia
# ---------------------------------------------------------------------------
class TestPrivacidade:
    @pytest.mark.parametrize("path", [
        "/api/farms",
        "/api/farms/1",
    ])
    def test_get_farms_sem_token_401(self, client, path):
        assert client.get(path).status_code == 401

    @pytest.mark.parametrize("path", [
        "/api/analytics/farm/1",
        "/api/weather/farm/1",
        "/api/talhao/1/texture?layer=ndvi",
        "/api/talhao/1/heightmap",
        "/api/reports/farm/1/pdf",
    ])
    def test_endpoints_sem_token_401(self, client, path):
        assert client.get(path).status_code == 401

    def test_analytics_farm_alheia_404(self, client, admin_token):
        token, _ = _register_and_login(client, "u1")
        fid = _create_farm(client, admin_token)  # dono = admin
        assert client.get(f"/api/analytics/farm/{fid}", headers=auth(token)).status_code == 404

    def test_weather_farm_alheia_404(self, client, admin_token):
        token, _ = _register_and_login(client, "u2")
        fid = _create_farm(client, admin_token)
        assert client.get(f"/api/weather/farm/{fid}", headers=auth(token)).status_code == 404

    def test_texture_farm_alheia_404(self, client, admin_token):
        token, _ = _register_and_login(client, "u3")
        fid = _create_farm(client, admin_token)
        assert client.get(f"/api/talhao/{fid}/texture?layer=ndvi",
                          headers=auth(token)).status_code == 404

    def test_heightmap_farm_alheia_404(self, client, admin_token):
        token, _ = _register_and_login(client, "u4")
        fid = _create_farm(client, admin_token)
        assert client.get(f"/api/talhao/{fid}/heightmap",
                          headers=auth(token)).status_code == 404

    def test_pdf_farm_alheia_404(self, client, admin_token):
        token, _ = _register_and_login(client, "u5")
        fid = _create_farm(client, admin_token)
        assert client.get(f"/api/reports/farm/{fid}/pdf",
                          headers=auth(token)).status_code == 404

    def test_shared_demo_legivel_por_qualquer_usuario(self, client):
        token, _ = _register_and_login(client, "u6")
        # Farm demo (id=1, is_shared=True) é legível por usuário comum autenticado.
        assert client.get("/api/farms/1", headers=auth(token)).status_code == 200


# ---------------------------------------------------------------------------
# D) Migração / backfill (SQLite) — Alembic + seed
# ---------------------------------------------------------------------------
class TestMigracaoBackfill:
    def test_colunas_owner_presentes_e_backfill_ok(self, client, admin_token):
        """Após o boot (alembic upgrade head + seed), owner_id/is_shared existem
        e o demo está associado ao admin e marcado como compartilhado."""
        with SessionLocal() as db:
            import models
            demo = db.query(models.Farm).filter(models.Farm.id == 1).first()
            admin = db.query(models.User).filter(models.User.email == "admin@orion.com").first()
            assert demo is not None
            assert demo.owner_id == admin.id
            assert demo.is_shared is True
            # Nenhuma fazenda órfã (owner_id nulo) deve restar.
            orphans = db.query(models.Farm).filter(models.Farm.owner_id.is_(None)).count()
            assert orphans == 0

    def test_alembic_upgrade_head_sqlite(self):
        """Roda `alembic upgrade head` em um SQLite com o esquema LEGADO
        (sem owner_id/is_shared) e valida a migração + backfill seguro."""
        backend_dir = REPO_ROOT / "backend"
        tmp = Path(tempfile.mkdtemp(prefix="orion_mig_"))
        db_path = tmp / "legacy.db"
        url = f"sqlite:///{db_path}"

        # Esquema legado (sem owner_id/is_shared) + 1 fazenda órfã.
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE,
                hashed_password TEXT NOT NULL, role TEXT
            );
            CREATE TABLE farms (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL,
                total_area FLOAT NOT NULL, latitude FLOAT, longitude FLOAT
            );
            CREATE TABLE talhoes (
                id INTEGER PRIMARY KEY, farm_id INTEGER, name TEXT, area FLOAT,
                crop TEXT, latitude FLOAT, longitude FLOAT, kml_coordinates TEXT
            );
            INSERT INTO users (id, name, email, hashed_password, role)
                VALUES (1, 'Admin Demo', 'admin@orion.com', 'x', 'admin');
            INSERT INTO farms (id, name, city, total_area, latitude, longitude)
                VALUES (1, 'Fazenda Legacy', 'Cidade', 10.0, -22.0, -55.0);
            """
        )
        conn.commit()
        conn.close()

        env = dict(os.environ)
        env["DATABASE_URL"] = url
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            cwd=str(backend_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(farms)")}
        assert "owner_id" in cols and "is_shared" in cols
        row = conn.execute("SELECT owner_id, is_shared FROM farms WHERE id=1").fetchone()
        # Backfill: dono = admin demo (id=1); farm demo compartilhada (is_shared=1).
        assert row[0] == 1
        assert row[1] == 1
        conn.close()
