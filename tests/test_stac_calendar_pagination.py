"""PR #8 — STAC: ordenação determinística e paginação da janela (item A7).

A auditoria encontrou `fetch_real_calendar` consultando o STAC sem ordenação
explícita e sem paginação. Com `limit=60` e `period_days=730`, o servidor
podia devolver 60 itens ARBITRÁRIOS da janela e o calendário passaria a
representar um recorte enviesado do período.

Aqui nada vai à rede: `cds._request` é substituído por um servidor STAC
falso que devolve páginas com `links[rel=next]`.
"""
import json
from datetime import date

import pytest

from services import copernicus_service as cds


class FakeResponse:
    def __init__(self, body):
        self._body = body
        self.status_code = 200
        self.headers = {"Content-Type": "application/json"}
        self.text = json.dumps(body)

    def json(self):
        return self._body


def feature(item_id: str, dt: str, cloud: float) -> dict:
    return {
        "id": item_id,
        "collection": "sentinel-2-l2a",
        "properties": {"datetime": dt, "eo:cloud_cover": cloud,
                       "platform": "sentinel-2a", "constellation": "sentinel-2"},
        "geometry": None, "bbox": None, "assets": {},
    }


@pytest.fixture
def cdse_on(monkeypatch):
    monkeypatch.setattr(cds.settings, "cdse_client_id", "id-falso")
    monkeypatch.setattr(cds.settings, "cdse_client_secret", "segredo-falso")
    monkeypatch.setattr(cds, "_get_token", lambda: "token-falso")
    cds._mem_cache.clear()


# ---------------------------------------------------------------------------
# Ordenação
# ---------------------------------------------------------------------------
def test_stac_pede_ordenacao_temporal_determinista(monkeypatch, cdse_on):
    seen = {}

    def fake_request(method, url, *, json_body=None, headers=None, stage="HTTP"):
        seen["body"] = json_body
        return FakeResponse({"features": [], "links": []})

    monkeypatch.setattr(cds, "_request", fake_request)
    cds.stac_search(None, [-49.1, -22.1, -49.0, -22.0], date(2025, 1, 1), date(2026, 1, 1), limit=30)
    assert seen["body"]["sortby"] == [{"field": "properties.datetime", "direction": "desc"}]
    assert seen["body"]["limit"] == 30


def test_stac_ordena_localmente_mesmo_com_resposta_fora_de_ordem(monkeypatch, cdse_on):
    fora_de_ordem = [
        feature("b", "2025-06-15T10:00:00Z", 5.0),
        feature("d", "2025-12-02T10:00:00Z", 5.0),
        feature("a", "2025-02-01T10:00:00Z", 5.0),
        feature("c", "2025-09-08T10:00:00Z", 5.0),
    ]
    monkeypatch.setattr(cds, "_request",
                        lambda *a, **k: FakeResponse({"features": fora_de_ordem, "links": []}))
    scenes = cds.stac_search(None, [-49.1, -22.1, -49.0, -22.0], date(2025, 1, 1), date(2026, 1, 1), limit=10)
    assert [s.item_id for s in scenes] == ["d", "c", "b", "a"]


def test_stac_refaz_consulta_sem_sortby_quando_servidor_recusa(monkeypatch, cdse_on):
    tentativas = []

    def fake_request(method, url, *, json_body=None, headers=None, stage="HTTP"):
        tentativas.append(dict(json_body or {}))
        if "sortby" in (json_body or {}):
            raise cds.CopernicusError("STAC recusou sortby", status=400,
                                      endpoint=url, stage="STAC_SEARCH")
        return FakeResponse({"features": [feature("x", "2025-05-01T10:00:00Z", 2.0)], "links": []})

    monkeypatch.setattr(cds, "_request", fake_request)
    scenes = cds.stac_search(None, [-49.1, -22.1, -49.0, -22.0], date(2025, 1, 1), date(2026, 1, 1), limit=10)
    assert len(tentativas) == 2
    assert "sortby" in tentativas[0] and "sortby" not in tentativas[1]
    assert [s.item_id for s in scenes] == ["x"]


def test_stac_propaga_erro_que_nao_e_sortby(monkeypatch, cdse_on):
    def boom(*a, **k):
        raise cds.CopernicusError("bbox inválida", status=400, endpoint="x", stage="STAC_SEARCH")

    monkeypatch.setattr(cds, "_request", boom)
    with pytest.raises(cds.CopernicusError):
        cds.stac_search(None, [-49.1, -22.1, -49.0, -22.0],
                        date(2025, 1, 1), date(2026, 1, 1), limit=10, sortby=False)


# ---------------------------------------------------------------------------
# Paginação
# ---------------------------------------------------------------------------
def _paged_server(paginas):
    """Devolve um `_request` falso que percorre `paginas` via rel=next."""
    estado = {"i": 0, "chamadas": []}

    def fake_request(method, url, *, json_body=None, headers=None, stage="HTTP"):
        i = estado["i"]
        estado["chamadas"].append({"method": method, "url": url, "body": json_body})
        estado["i"] += 1
        features = paginas[i]
        links = []
        if i + 1 < len(paginas):
            links = [{"rel": "next", "href": url, "method": "POST",
                      "merge": True, "body": {"token": f"pagina-{i + 1}"}}]
        return FakeResponse({"features": features, "links": links})

    return fake_request, estado


def test_stac_sem_max_items_mantem_uma_unica_pagina(monkeypatch, cdse_on):
    paginas = [[feature(f"p0-{i}", f"2025-12-{1 + i:02d}T10:00:00Z", 3.0) for i in range(5)],
               [feature("p1-0", "2025-11-01T10:00:00Z", 3.0)]]
    fake, estado = _paged_server(paginas)
    monkeypatch.setattr(cds, "_request", fake)
    scenes = cds.stac_search(None, [-49.1, -22.1, -49.0, -22.0],
                             date(2025, 1, 1), date(2026, 1, 1), limit=10)
    assert len(estado["chamadas"]) == 1, "sem max_items o comportamento histórico é preservado"
    assert len(scenes) == 5


def test_stac_pagina_ate_cobrir_a_janela(monkeypatch, cdse_on):
    paginas = [
        [feature(f"a{i}", f"2025-12-{1 + i:02d}T10:00:00Z", 3.0) for i in range(10)],
        [feature(f"b{i}", f"2025-11-{1 + i:02d}T10:00:00Z", 3.0) for i in range(10)],
        [feature(f"c{i}", f"2025-10-{1 + i:02d}T10:00:00Z", 3.0) for i in range(10)],
    ]
    fake, estado = _paged_server(paginas)
    monkeypatch.setattr(cds, "_request", fake)
    scenes = cds.stac_search(None, [-49.1, -22.1, -49.0, -22.0],
                             date(2025, 1, 1), date(2026, 1, 1), limit=10, max_items=100)
    assert len(estado["chamadas"]) == 3
    assert len(scenes) == 30
    # O token da página seguinte é levado adiante (merge do link rel=next).
    assert estado["chamadas"][1]["body"]["token"] == "pagina-1"
    assert estado["chamadas"][1]["body"]["collections"] == ["sentinel-2-l2a"]
    # Resultado global ordenado, não "ordenado por página".
    assert [s.acquisition_date for s in scenes] == sorted(
        (s.acquisition_date for s in scenes), reverse=True)


def test_stac_respeita_o_teto_de_itens(monkeypatch, cdse_on):
    paginas = [[feature(f"p{p}-{i}", f"2025-{12 - p:02d}-{1 + i:02d}T10:00:00Z", 3.0)
                for i in range(10)] for p in range(6)]
    fake, estado = _paged_server(paginas)
    monkeypatch.setattr(cds, "_request", fake)
    scenes = cds.stac_search(None, [-49.1, -22.1, -49.0, -22.0],
                             date(2025, 1, 1), date(2026, 1, 1), limit=10, max_items=25)
    assert len(scenes) == 25
    assert len(estado["chamadas"]) == 3, "para assim que o orçamento de itens é atingido"


def test_stac_dedupe_itens_repetidos_entre_paginas(monkeypatch, cdse_on):
    repetido = feature("dup", "2025-12-01T10:00:00Z", 3.0)
    paginas = [[repetido, feature("x", "2025-11-20T10:00:00Z", 3.0)],
               [repetido, feature("y", "2025-11-10T10:00:00Z", 3.0)]]
    fake, _ = _paged_server(paginas)
    monkeypatch.setattr(cds, "_request", fake)
    scenes = cds.stac_search(None, [-49.1, -22.1, -49.0, -22.0],
                             date(2025, 1, 1), date(2026, 1, 1), limit=10, max_items=50)
    assert [s.item_id for s in scenes] == ["dup", "x", "y"]


def test_stac_segue_link_next_do_tipo_get(monkeypatch, cdse_on):
    chamadas = []

    def fake_request(method, url, *, json_body=None, headers=None, stage="HTTP"):
        chamadas.append((method, url))
        if method == "POST":
            return FakeResponse({
                "features": [feature("g1", "2025-12-01T10:00:00Z", 3.0)],
                "links": [{"rel": "next", "method": "GET", "href": "https://stac/x?page=2"}],
            })
        return FakeResponse({"features": [feature("g2", "2025-11-01T10:00:00Z", 3.0)], "links": []})

    monkeypatch.setattr(cds, "_request", fake_request)
    scenes = cds.stac_search(None, [-49.1, -22.1, -49.0, -22.0],
                             date(2025, 1, 1), date(2026, 1, 1), limit=10, max_items=50)
    assert chamadas[1] == ("GET", "https://stac/x?page=2")
    assert [s.item_id for s in scenes] == ["g1", "g2"]


# ---------------------------------------------------------------------------
# Calendário real — a janela de 730 dias não pode ser truncada arbitrariamente
# ---------------------------------------------------------------------------
def test_calendario_de_730_dias_devolve_as_cenas_mais_recentes_da_janela(monkeypatch, cdse_on):
    # 3 páginas cobrindo 2 anos, da mais recente para a mais antiga.
    paginas = [
        [feature(f"r{i}", f"2026-{9 - i // 3:02d}-{1 + (i % 3) * 10:02d}T10:00:00Z", 4.0) for i in range(9)],
        [feature(f"m{i}", f"2026-0{1 + i // 3}-{1 + (i % 3) * 10:02d}T10:00:00Z", 4.0) for i in range(9)],
        [feature(f"v{i}", f"2025-0{1 + i // 3}-{1 + (i % 3) * 10:02d}T10:00:00Z", 4.0) for i in range(9)],
    ]
    fake, estado = _paged_server(paginas)
    monkeypatch.setattr(cds, "_request", fake)
    calendar, status, detail = cds.fetch_real_calendar(
        -22.7, -51.2, 42.5, None, limit=5,
        start_date=date(2024, 10, 1), end_date=date(2026, 9, 22),
    )
    assert status == "ok"
    assert len(estado["chamadas"]) == 3, "a janela inteira é paginada, não só a 1ª página"
    assert len(calendar) == 5
    datas = [c["date"] for c in calendar]
    assert datas == sorted(datas, reverse=True)
    assert datas[0].startswith("2026-09"), "as 5 devolvidas são as MAIS RECENTES da janela"
    # Diagnóstico de cobertura acompanha a resposta (sem segredos).
    assert detail["stac_scenes"] == 27
    assert detail["usable_scenes"] == 27
    assert detail["returned"] == 5
    assert detail["truncated_by_limit"] is True
    assert detail["sorted_by"] == "properties.datetime desc"
    assert "Authorization" not in json.dumps(detail)


def test_calendario_sem_cena_continua_sem_detalhe(monkeypatch, cdse_on):
    monkeypatch.setattr(cds, "_request", lambda *a, **k: FakeResponse({"features": [], "links": []}))
    calendar, status, detail = cds.fetch_real_calendar(-22.7, -51.2, 42.5, None, limit=5)
    assert calendar == [] and status == "no_scene" and detail is None


def test_calendario_filtra_nuvem_antes_de_truncar(monkeypatch, cdse_on):
    monkeypatch.setattr(cds.settings, "cdse_max_cloud_cover", 20.0)
    features = [feature("nublada", "2026-09-20T10:00:00Z", 95.0),
                feature("boa1", "2026-09-10T10:00:00Z", 4.0),
                feature("boa2", "2026-09-01T10:00:00Z", 6.0)]
    monkeypatch.setattr(cds, "_request", lambda *a, **k: FakeResponse({"features": features, "links": []}))
    calendar, status, detail = cds.fetch_real_calendar(-22.7, -51.2, 42.5, None, limit=2)
    assert [c["product_id"] for c in calendar] == ["boa1", "boa2"]
    assert detail["stac_scenes"] == 3 and detail["usable_scenes"] == 2
    assert detail["truncated_by_limit"] is False
