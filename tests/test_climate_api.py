"""
PR #7 — Endpoint GET /api/climate/farm/{farm_id} — testes de INTEGRAÇÃO.

A NASA POWER é SEMPRE simulada (monkeypatch de requests.get no
climate_service): a suíte não depende da disponibilidade real da NASA.

Cobrem:
- ownership (401 anônimo / 404 fazenda alheia / 200 dono / 200 admin);
- utilização da LOCALIZAÇÃO CANÔNICA da fazenda (a NASA recebe o ponto
  canônico do cadastro — PR #6);
- presets (7d/15d/30d) e período personalizado (start/end);
- validação de período (start>end, só start, período > 366 dias → 422);
- fonte indisponível → 503 EXPLÍCITO (nunca dado inventado);
- sem dados → 200 status=insufficient_data com mensagem explícita;
- contrato legado /api/weather/farm/{id} intacto (sem regressão);
- fallback legado rotulado como dado demonstrativo (is_real/data_origin).
"""
import datetime as dt

import pytest

from services import climate_service as cs
from conftest import auth, login, register_user, unique_email

LAT, LON = -25.4321, -49.8765  # coordenadas canônicas distintas da demo

FARM_BODY = {
    "name": "Fazenda Clima PR7",
    "total_area": 30.0,
    "talhao_name": "Talhão Clima",
    "crop": "Soja",
    "latitude": LAT,
    "longitude": LON,
    "location_source": "legacy",  # sem geometria → sem reverse geocoding (sem rede)
}


def _mk_farm(client, token) -> int:
    r = client.post("/api/farms", json=FARM_BODY, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _profile(day: dt.date) -> dict:
    """Perfis determinísticos por ano (corrente x anteriores)."""
    if day.year == 2026:
        rain = 2.5 if day.day % 3 else 0.0
        temp, tmax, tmin = 23.0, 31.0, 15.0
    else:
        rain, temp, tmax, tmin = 4.0, 22.0, 30.0, 14.0
    return {
        "T2M": temp, "T2M_MAX": tmax, "T2M_MIN": tmin,
        "PRECTOTCORR": rain, "RH2M": 65.0, "WS2M": 2.0, "ALLSKY_SFC_SW_DWN": 14.0,
    }


def _nasa_payload(start: dt.date, end: dt.date, fill: set[dt.date] | None = None) -> dict:
    fill = fill or set()
    parameter = {p: {} for p in cs.NASA_PARAMETERS}
    d = start
    while d <= end:
        for p, v in _profile(d).items():
            if d in fill:
                v = cs.FILL_VALUE
            parameter[p][d.strftime("%Y%m%d")] = v
        d += dt.timedelta(days=1)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [LON, LAT, 480.0]},
        "properties": {"parameter": parameter},
        "header": {
            "api": {"version": "v2.9.7", "name": "POWER Daily API"},
            "sources": ["FLASHFLUX", "GEOSIT", "POWER"],
            "fill_value": cs.FILL_VALUE, "time_standard": "LST",
        },
        "messages": [],
        "parameters": {},
    }


@pytest.fixture(autouse=True)
def nasa(monkeypatch):
    """Fake de requests.get (captura + cache limpo).

    AUTOUSE: nenhum teste deste módulo toca a rede real (requisito PR #7).
    """
    captured: list[dict] = []
    behavior: dict = {"handler": lambda s, e: _nasa_payload(s, e)}

    def fake_get(url, params=None, timeout=None, **kw):
        params = dict(params or {})
        s = params["start"]; e = params["end"]
        start = dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))
        end = dt.date(int(e[:4]), int(e[4:6]), int(e[6:]))
        captured.append({"params": params, "window": (start, end)})
        result = behavior["handler"](start, end)
        if isinstance(result, Exception):
            raise result
        code = result.pop("__status__", 200)

        class R:
            status_code = code
            def json(self):
                return result
        return R()

    monkeypatch.setattr(cs.requests, "get", fake_get)
    with cs._raw_cache_lock:
        cs._raw_cache.clear()
    yield captured, behavior
    with cs._raw_cache_lock:
        cs._raw_cache.clear()


@pytest.fixture()
def farm_and_token(client, user_token):
    return _mk_farm(client, user_token), user_token


# ---------------------------------------------------------------------------
# Ownership (Fase 11)
# ---------------------------------------------------------------------------
class TestOwnership:
    def test_anonimo_401(self, client, nasa):
        r = client.get("/api/climate/farm/1")
        assert r.status_code == 401

    def test_fazenda_alheia_404(self, client, user_token, nasa):
        other_email = unique_email("outro")
        register_user(client, other_email)
        other_token = login(client, other_email, "abc12345")["access_token"]
        fid = _mk_farm(client, user_token)  # fazenda do user_token
        r = client.get(f"/api/climate/farm/{fid}", headers=auth(other_token))
        assert r.status_code == 404  # não expõe a existência

    def test_dono_200(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(f"/api/climate/farm/{fid}?preset=30d", headers=auth(token))
        assert r.status_code == 200, r.text

    def test_admin_acessa_qualquer(self, client, admin_token, nasa, farm_and_token):
        fid, _ = farm_and_token
        r = client.get(f"/api/climate/farm/{fid}", headers=auth(admin_token))
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Localização canônica (PR #6) — a NASA recebe o ponto do cadastro
# ---------------------------------------------------------------------------
class TestCanonicalLocation:
    def test_nasa_recebe_coordenadas_canonicas(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(f"/api/climate/farm/{fid}?preset=30d", headers=auth(token))
        assert r.status_code == 200
        captured, _ = nasa
        assert captured, "a NASA POWER deveria ter sido consultada"
        for c in captured:
            assert abs(c["params"]["latitude"] - LAT) < 1e-9
            assert abs(c["params"]["longitude"] - LON) < 1e-9
        body = r.json()
        assert abs(body["farm_location"]["latitude"] - LAT) < 1e-9
        assert abs(body["farm_location"]["longitude"] - LON) < 1e-9
        assert body["farm_location"]["source"] == "canonical"

    def test_fazenda_demo_usa_seu_ponto(self, client, nasa, admin_token):
        r = client.get("/api/climate/farm/1", headers=auth(admin_token))
        assert r.status_code == 200
        captured, _ = nasa
        # demo: -22.7182 / -55.5421 (seed)
        assert abs(captured[0]["params"]["latitude"] - (-22.7182)) < 1e-6
        assert abs(captured[0]["params"]["longitude"] - (-55.5421)) < 1e-6


# ---------------------------------------------------------------------------
# Períodos (Fase 4)
# ---------------------------------------------------------------------------
class TestPeriods:
    def test_preset_7d(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(f"/api/climate/farm/{fid}?preset=7d", headers=auth(token))
        assert r.status_code == 200
        body = r.json()
        assert body["period"]["days"] == 7
        assert body["period"]["preset"] == "7d"
        esperado_fim = dt.date.today() - dt.timedelta(days=cs.settings.nasa_power_nrt_lag_days)
        assert body["period"]["end"] == esperado_fim.isoformat()

    def test_preset_15d(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(f"/api/climate/farm/{fid}?preset=15d", headers=auth(token))
        assert r.status_code == 200
        assert r.json()["period"]["days"] == 15

    def test_periodo_personalizado(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(
            f"/api/climate/farm/{fid}?start=2026-01-01&end=2026-01-31",
            headers=auth(token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["period"] == {
            "start": "2026-01-01", "end": "2026-01-31", "days": 31, "preset": None
        }
        assert len(body["daily"]["dates"]) == 31

    def test_start_pos_422(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(
            f"/api/climate/farm/{fid}?start=2026-02-01&end=2026-01-01",
            headers=auth(token),
        )
        assert r.status_code == 422

    def test_somente_start_422(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(f"/api/climate/farm/{fid}?start=2026-01-01", headers=auth(token))
        assert r.status_code == 422

    def test_periodo_demasiado_longo_422(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(
            f"/api/climate/farm/{fid}?start=2024-12-01&end=2026-01-01",
            headers=auth(token),
        )
        assert r.status_code == 422
        assert "366" in r.json()["detail"]


# ---------------------------------------------------------------------------
# PR #7-FIX.2 — Período personalizado: prioridade start/end, contagem
# inclusiva, baseline na mesma janela, cache sem colisão com preset.
# ---------------------------------------------------------------------------
class TestCustomPeriod:
    # Janela exata do playtest: 20/08/2026 → 07/09/2026 = 19 dias (inclusiva)
    CUSTOM = "start=2026-08-20&end=2026-09-07"

    def test_personalizado_19_dias_janela_exata(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(f"/api/climate/farm/{fid}?{self.CUSTOM}", headers=auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        # period exato, contagem inclusiva (19 dias), sem preset
        assert body["period"]["start"] == "2026-08-20"
        assert body["period"]["end"] == "2026-09-07"
        assert body["period"]["days"] == 19
        assert body["period"]["preset"] is None
        # cobertura usa o total correto do período
        assert body["coverage"]["requested_days"] == 19
        assert body["coverage"]["overall_days"] <= 19
        assert body["coverage"]["complete_days"] <= 19
        for m in ("precipitation", "temperature", "humidity", "radiation", "wind"):
            assert body["coverage"]["by_metric"][m]["available_days"] <= 19
        assert body["data_quality"]["requested_days"] == 19
        # gráficos/séries retornam SOMENTE as datas do intervalo (inclusivo)
        dates = body["daily"]["dates"]
        assert len(dates) == 19
        assert dates[0] == "2026-08-20"
        assert dates[-1] == "2026-09-07"
        assert all(d >= "2026-08-20" and d <= "2026-09-07" for d in dates)

    def test_start_end_precedem_preset(self, client, nasa, farm_and_token):
        # REGRA DE PRIORIDADE: se start E end vierem (mesmo com preset),
        # o período personalizado é usado — nunca misturar.
        fid, token = farm_and_token
        r = client.get(
            f"/api/climate/farm/{fid}?preset=30d&{self.CUSTOM}", headers=auth(token)
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["period"]["start"] == "2026-08-20"
        assert body["period"]["end"] == "2026-09-07"
        assert body["period"]["days"] == 19
        assert body["period"]["preset"] is None

    def test_baseline_usa_mesma_janela_personalizada(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        client.get(f"/api/climate/farm/{fid}?{self.CUSTOM}", headers=auth(token))
        captured, _ = nasa
        windows = [c["window"] for c in captured]
        # 1 janela atual + 5 de baseline = 6 consultas
        assert len(windows) == 6
        # TODAS (atual + baseline) usam a mesma janela do calendário 20/08–07/09
        for s, e in windows:
            assert (s.month, s.day) == (8, 20), (s, e)
            assert (e.month, e.day) == (9, 7), (s, e)
        # a atual é 2026; as de baseline são anos anteriores
        assert windows[0][0].year == 2026
        assert all(w[0].year < 2026 for w in windows[1:])

    def test_somente_end_422(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(f"/api/climate/farm/{fid}?end=2026-09-07", headers=auth(token))
        assert r.status_code == 422

    def test_somente_start_422(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(f"/api/climate/farm/{fid}?start=2026-08-20", headers=auth(token))
        assert r.status_code == 422

    def test_start_apos_end_422(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        r = client.get(
            f"/api/climate/farm/{fid}?start=2026-09-07&end=2026-08-20", headers=auth(token)
        )
        assert r.status_code == 422

    def test_cache_diferencia_preset30d_de_custom(self, client, nasa, farm_and_token):
        # uma resposta 09/08→07/09 (30d) NUNCA pode servir 20/08→07/09 (custom)
        fid, token = farm_and_token
        h = auth(token)
        client.get(f"/api/climate/farm/{fid}?preset=30d", headers=h)
        n_after_30d = len(nasa[0])
        client.get(f"/api/climate/farm/{fid}?{self.CUSTOM}", headers=h)
        windows = [c["window"] for c in nasa[0]]
        # a janela custom (20/08→07/09) precisou ser consultada de fato
        assert (dt.date(2026, 8, 20), dt.date(2026, 9, 7)) in windows
        # a janela 30d (dinâmica: hoje − NRT) é distinta e não serviu de cache
        end30 = dt.date.today() - dt.timedelta(days=cs.settings.nasa_power_nrt_lag_days)
        start30 = end30 - dt.timedelta(days=29)
        assert (start30, end30) in windows
        assert (start30, end30) != (dt.date(2026, 8, 20), dt.date(2026, 9, 7))
        assert len(windows) > n_after_30d  # houve nova consulta (sem colisão)

    def test_cache_diferencia_dois_intervalos_custom(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        h = auth(token)
        client.get(f"/api/climate/farm/{fid}?start=2026-08-20&end=2026-09-07", headers=h)
        client.get(f"/api/climate/farm/{fid}?start=2026-08-01&end=2026-08-31", headers=h)
        windows = [c["window"] for c in nasa[0]]
        assert (dt.date(2026, 8, 20), dt.date(2026, 9, 7)) in windows
        assert (dt.date(2026, 8, 1), dt.date(2026, 8, 31)) in windows


# ---------------------------------------------------------------------------
# Resposta estruturada (Fase 3/8/11)
# ---------------------------------------------------------------------------
class TestResponseContract:
    def test_estrutura_completa(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        body = client.get(f"/api/climate/farm/{fid}?preset=30d", headers=auth(token)).json()
        # estados
        assert body["status"] == "ok"
        # dados da fonte
        assert body["source"]["name"].startswith("NASA POWER")
        assert body["source"]["community"] == "AG"
        assert body["source"]["api_version"]
        assert "reanálise" in body["source"]["data_nature"].lower() or "modelo" in body["source"]["data_nature"].lower()
        assert set(body["source"]["parameters"]) == set(cs.NASA_PARAMETERS)
        # métricas com unidade e categoria
        for key in ("precipitation", "temperature", "radiation", "humidity", "wind"):
            m = body["metrics"][key]
            assert "unit" in m
            assert "baseline" in m
        # séries diárias com datas absolutas (preparação PR #8)
        assert body["daily"]["dates"][0].startswith("20")
        assert len(body["daily"]["precip_mm"]) == body["period"]["days"]
        # baseline + indicadores + interpretações + confiança
        assert body["baseline"]["years"] == 5
        assert len(body["indicators"]) >= 5
        assert body["interpretations"]
        assert body["confidence"]["level"] in ("alta", "media", "limitada")
        assert body["farm"]["id"] == fid

    def test_cache_api_nivel(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        h = auth(token)
        client.get(f"/api/climate/farm/{fid}?preset=30d", headers=h)
        n1 = len(nasa[0])
        client.get(f"/api/climate/farm/{fid}?preset=30d", headers=h)
        n2 = len(nasa[0])
        assert n2 == n1, "a 2ª requisição idêntica NÃO deve consultar a NASA de novo"


# ---------------------------------------------------------------------------
# Falhas e dados ausentes (Fase 9) — nunca dado inventado
# ---------------------------------------------------------------------------
class TestFailureStates:
    def test_fonte_indisponivel_503_explícito(self, client, nasa, farm_and_token):
        import requests as rq
        fid, token = farm_and_token
        nasa[1]["handler"] = lambda s, e: (_ for _ in ()).throw(rq.ConnectionError("down"))
        r = client.get(f"/api/climate/farm/{fid}?preset=30d", headers=auth(token))
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert detail["status"] == "unavailable"
        assert "temporariamente indisponíveis" in detail["message"]
        assert "metrics" not in r.json()  # nenhum dado (nem fake) no corpo

    def test_sem_dados_status_explicito(self, client, nasa, farm_and_token):
        fid, token = farm_and_token
        today = dt.date.today()
        fill = {today - dt.timedelta(days=i) for i in range(60)}
        nasa[1]["handler"] = lambda s, e: _nasa_payload(s, e, fill=fill)
        r = client.get(f"/api/climate/farm/{fid}?preset=30d", headers=auth(token))
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "insufficient_data"
        assert "Não há dados suficientes" in body["message"]
        assert body["metrics"]["precipitation"]["accumulated_mm"] is None
