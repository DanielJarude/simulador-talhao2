# Documentação — Orion Agro / Simulador de Talhões

Índice da documentação técnica do projeto. O **README da raiz** continua sendo
o ponto de entrada (visão geral, instalação, execução, endpoints, estrutura).

## Organização

| Pasta | Conteúdo |
|---|---|
| [`auditorias/`](auditorias/) | Auditorias técnicas por PR (escopo, evidências, testes e decisões de cada entrega) |

## Auditorias

| Documento | Escopo |
|---|---|
| [`AUDITORIA.md`](auditorias/AUDITORIA.md) | Auditoria geral do projeto e histórico consolidado dos PRs #1–#5 |
| [`AUDITORIA_LOCALIZACAO_PR6.md`](auditorias/AUDITORIA_LOCALIZACAO_PR6.md) | PR #6 — Localização canônica da propriedade |
| [`AUDITORIA_CLIMA_PR7.md`](auditorias/AUDITORIA_CLIMA_PR7.md) | PR #7 — Clima & Inteligência Agronômica |
| [`AUDITORIA_SAUDE_LAVOURA_PR8.md`](auditorias/AUDITORIA_SAUDE_LAVOURA_PR8.md) | PR #8 — Saúde e Evolução da Lavoura |

> **Os documentos de auditoria são registros históricos.** Eles descrevem o
> repositório como ele estava na data do respectivo PR e, por isso, citam os
> caminhos antigos (`backend/…`, `tests/…`, `scripts/…`, arquivos HTML na
> raiz). O conteúdo foi preservado sem edições na reorganização de pastas.
> A correspondência com a estrutura atual é:
>
> | Caminho antigo | Caminho atual |
> |---|---|
> | `backend/` (núcleo) | `Back/` |
> | `backend/services/{copernicus,climate,weather,dem,geolocation}_service.py` | `Api/services/` |
> | `backend/data/dem/` | `Api/data/dem/` |
> | `contorno_kml`, `contorno_shp/` | `Api/data/amostras/` |
> | `scripts/test_cdse_connection.py` | `Api/diagnostico/test_cdse_connection.py` |
> | `index.html`, `fazendas.html`, `auth.html`, `dashboard.html`, `app.js` | `Front/` |
> | `tools/playtest_dem.html` | `Front/tools/playtest_dem.html` |
> | `tests/` (API, serviços, ownership, migrações) | `Back/tests/` |
> | `tests/test_frontend_*.py` e `tests/test_3d_*.py` de análise estática/VM Node | `Front/tests/` |
> | `tests/test_3d_pipeline.py` (usa o `TestClient` da API) | `Back/tests/` |
> | `AUDITORIA*.md` | `Documentacao/auditorias/` |
