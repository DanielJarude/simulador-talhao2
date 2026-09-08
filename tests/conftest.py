"""
Suíte de testes — Orion Agro API (pytest).

Isolamento e limpeza:
- Banco SQLite EFÊMERO em diretório temporário do SO (fora do repositório),
  removido ao fim da sessão — nada de `*.db` poluindo o git;
- `JWT_SECRET_KEY` fixa e determinística para os testes;
- `DEM_DIR` apontando para o temp (com tile sintética criada sob demanda) e
  download de tiles DESLIGADO — a suíte NUNCA acessa a rede;
- `fetch_live_nasa_weather` é simulado no nível da aplicação (a NASA POWER
  jamais é consultada); fallback/cache são testados em nível de serviço;
- pastas novas criadas em `dynamic_talhoes/` pelos testes são removidas ao
  fim da sessão.

Execução (raiz do repositório):
    pytest
"""
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"

# ---------------------------------------------------------------------------
# Ambiente de teste — DEVE ser definido antes de importar a aplicação
# ---------------------------------------------------------------------------
_TEST_TMP = Path(tempfile.mkdtemp(prefix="orion_pytest_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_TMP / 'test.db'}"
os.environ["JWT_SECRET_KEY"] = "pytest-secret-fixo-nao-usar-em-producao-0123456789"
os.environ["JWT_EXPIRE_MINUTES"] = "720"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["DEM_DIR"] = str(_TEST_TMP / "dem")
os.environ["DEM_DOWNLOAD_ENABLED"] = "false"
os.environ["NASA_WEATHER_CACHE_MINUTES"] = "60"
os.makedirs(os.environ["DEM_DIR"], exist_ok=True)

sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402

#: Segredo JWT usado pelos testes (espelha os env acima)
TEST_JWT_SECRET = os.environ["JWT_SECRET_KEY"]

#: Clima determinístico injetado no lugar da NASA POWER (mesma forma do payload real)
FAKE_WEATHER = {
    "summary": {
        "total_rain_mm": 1500.0,
        "avg_temp_c": 24.5,
        "min_temp_c": 15.0,
        "max_temp_c": 34.0,
    },
    "monthly": {
        "labels": ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
                   "Jul", "Ago", "Set", "Out", "Nov", "Dez"],
        "rain": [100.0] * 12,
        "temp_max": [30.0] * 12,
        "temp_min": [18.0] * 12,
        "temp_avg": [24.0] * 12,
    },
    "recent_applications": [],
}


def _fake_nasa_weather(lat: float, lon: float) -> dict:
    return FAKE_WEATHER


# Snapshot das pastas existentes em dynamic_talhoes ANTES de qualquer teste
_DYNAMIC_DIR = REPO_ROOT / "dynamic_talhoes"
_DYNAMIC_BEFORE = {p.name for p in _DYNAMIC_DIR.iterdir()} if _DYNAMIC_DIR.exists() else set()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def client():
    """TestClient com lifespan (seed) e NASA POWER simulada."""
    from fastapi.testclient import TestClient

    with patch.object(main, "fetch_live_nasa_weather", side_effect=_fake_nasa_weather):
        with TestClient(main.app) as c:
            yield c


def _teardown_cleanup():
    """Remove o temp e as pastas novas criadas em dynamic_talhoes pela suíte."""
    if _DYNAMIC_DIR.exists():
        for entry in _DYNAMIC_DIR.iterdir():
            if entry.name not in _DYNAMIC_BEFORE:
                shutil.rmtree(entry, ignore_errors=True)
    shutil.rmtree(_TEST_TMP, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_session():
    """Teardown no fim da sessão de testes (caso normal)."""
    yield
    _teardown_cleanup()


def pytest_sessionfinish(session, exitstatus):
    """Rede de segurança idempotente: garante a limpeza mesmo em
    `--collect-only`, coleção abortada ou finalização antecipada
    (quando as fixtures de sessão não chegam a rodar)."""
    _teardown_cleanup()


# ---------------------------------------------------------------------------
# Helpers de autenticação
# ---------------------------------------------------------------------------
def register_user(client, email: str, role: str = "Produtor Rural",
                  password: str = "abc12345", name: str | None = None) -> str:
    r = client.post("/api/auth/register", json={
        "name": name or "Testador " + email.split("@")[0][-8:],
        "email": email,
        "password": password,
        "role": role,
    })
    assert r.status_code == 201, f"Registro falhou: {r.text}"
    return password


def login(client, email: str, password: str) -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"Login falhou: {r.text}"
    return r.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@orion-test.com"


@pytest.fixture()
def admin_token(client) -> str:
    """Token do admin semeado (role 'admin')."""
    return login(client, "admin@orion.com", "123456")["access_token"]


@pytest.fixture()
def user_token(client) -> str:
    """Token de usuário comum (role 'Produtor Rural'), único por teste."""
    email = unique_email("produtor")
    register_user(client, email, role="Produtor Rural")
    return login(client, email, "abc12345")["access_token"]
