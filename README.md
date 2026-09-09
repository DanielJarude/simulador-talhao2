# 🚜 Orion Agro — Simulador 3D de Talhões

Plataforma de monitoramento agronômico que combina **sensoriamento espectral** (índices NDVI, EVI, NDRE e NDMI derivados de passagens Sentinel-2), **agrometeorologia** (série diária de 12 meses da NASA POWER com avaliação de janela de pulverização) e **simulação what-if em 3D** (Three.js) para projetar o impacto de adubação nitrogenada, irrigação e pressão de pragas sobre o vigor, a produtividade e o financeiro do talhão.

## ✨ Funcionalidades

- **Dashboard agronômico** — KPIs de safra (soja de verão + milho safrinha), zoneamento espectral por área (ha), curva temporal do índice e série climática mensal.
- **Mapa 2D** — localização da propriedade e contorno do talhão (importação de `.kml` / `.geojson` / `.json`).
- **Simulador 3D split-view** — cena "cenário real" × "cenário simulado" lado a lado, com **relevo topográfico real (Copernicus DEM GL-30)** e sliders what-if em tempo real.
- **Auth JWT** — registro/login com senhas hasheadas (bcrypt) e tokens de acesso (HS256); mutações protegidas por `Authorization: Bearer`.
- **Laudo técnico em PDF** — relatório agronômico consolidando safras, zoneamento, clima e simulação.

## 🧱 Stack tecnológica

| Camada | Tecnologias |
|--------|-------------|
| **Backend** | FastAPI · Pydantic v2 · pydantic-settings · SQLAlchemy 2 · SQLite · requests · rasterio · ReportLab |
| **Frontend** | Three.js r128 (split-view 3D) · Leaflet 1.9 · Chart.js — HTML puro, sem build step |
| **Dados** | Sentinel-2 (13 passagens, `sentinel-21KXQ-*`), Copernicus DEM GL-30 (topografia), contornos KML/SHP, NASA POWER (tempo real) |

## 📁 Estrutura

```
.
├── index.html              # Dashboard + Mapa 2D + Simulador 3D (SPA multi-tela)
├── fazendas.html           # CRUD de propriedades + import de geometria
├── auth.html               # Login / cadastro
├── app.js                  # Cliente da API (Auth, Farms, Weather, Satellite, Simulation)
├── backend/
│   ├── main.py             # Rotas FastAPI (lifespan, CORS via settings)
│   ├── config.py           # Configuração centralizada (pydantic-settings + .env)
│   ├── database.py         # Engine/sessão SQLAlchemy
│   ├── models.py           # User, Farm, Talhao
│   ├── schemas.py          # Contratos Pydantic (validados)
│   ├── security.py         # Senhas (bcrypt) + tokens JWT + dependência get_current_user
│   ├── requirements.txt
│   ├── .env.example
│   ├── data/dem/           # Tiles GeoTIFF do Copernicus DEM GL-30 (fora do Git)
│   └── services/
│       ├── simulation_service.py   # Modelo agronômico what-if por cultura
│       ├── weather_service.py      # NASA POWER + cache TTL
│       ├── satellite_service.py    # Geração das texturas espectrais (KML → PNG)
│       ├── analytics_service.py    # Série temporal, zoneamento, estimativa de safra
│       ├── dem_service.py          # Pipeline topográfico Copernicus DEM GL-30 (heightmap)
│       └── pdf_service.py          # Laudo técnico em PDF (ReportLab)
├── sentinel-21KXQ-<data>/  # Texturas espectrais por passagem de satélite
└── dynamic_talhoes/        # Texturas + heightmaps gerados dinamicamente (runtime)
```

## ✅ Pré-requisitos

- **Python 3.11+**
- **Navegador** com WebGL (Chrome/Edge/Firefox recentes)
- Servidor HTTP estático para o frontend (ex.: Live Server do VS Code na porta 5501)

## 🚀 Instalação e execução

```bash
# 1. Clone e entre no repositório
git clone https://github.com/DanielJarude/simulador-talhao2.git
cd simulador-talhao2

# 2. Crie e ative o ambiente virtual
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

# 3. Instale as dependências do backend
pip install -r backend/requirements.txt

# 4. Configure o ambiente (recomendado)
cp backend/.env.example backend/.env
# Em produção, defina OBRIGATÓRIAMENTE JWT_SECRET_KEY (senão, um segredo
# efêmero é usado e os tokens expiram a cada restart do servidor).
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

# 5. Suba a API (porta 8000)
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
cd ..

# 6. Acesse (PR #4 — o frontend TAMBÉM é servido pela própria API)
#    http://localhost:8000/                → frontend completo (mesma origem)
#    http://localhost:8000/docs            → Swagger da API
#    (opcional) frontend separado em :5501 — outro terminal:
#      python -m http.server 5501
#      http://localhost:5501/fazendas.html
#      login (demo: admin@orion.com / 123456)
```

> **Nota (PR #4):** a URL base da API (`API_URL`, em `app.js`) é resolvida
> automaticamente: página servida pelo próprio backend → URL **relativa**;
> página em servidor estático local (`:5501`) → `http://localhost:8000/api`.
> Em deploy/preview, defina `window.ORION_API_URL = "<base>/api"` antes de
> carregar o `app.js` (ou monte `PUBLIC_BASE_URL` no `.env` do backend, que
> continua sendo a fonte das URLs de textura/heightmap devolvidas pela API).

## 🔌 Endpoints principais

| Método | Rota | Auth | Descrição |
|--------|------|:----:|-----------|
| `POST` | `/api/auth/register` | — | Cria usuário (bcrypt) — papel **fixo** `Produtor Rural`; `role` no payload é ignorado |
| `POST` | `/api/auth/login` | — | Valida bcrypt e **emite o token JWT** (`{access_token, token_type, expires_in, user}`) |
| `GET` | `/api/farms` · `/api/farms/{id}` | 🔑 | **Privado por usuário** — lista/busca só as próprias fazendas (admin vê todas); farm de demo (id=1) é compartilhada (legível por qualquer usuário autenticado) |
| `POST` | `/api/farms` | 🔑 | Cria fazenda **vinculada ao usuário logado** (gera texturas espectrais) |
| `PUT` | `/api/farms/{id}` | 🔑 | Atualiza a **própria** fazenda (ou qualquer uma, se `admin`) |
| `DELETE` | `/api/farms/{id}` | 🔑 | Remove a **própria** fazenda (ou qualquer uma, se `admin`) |
| `GET` | `/api/talhao/{farm_id}/texture?layer=ndvi&date=YYYY-MM-DD` | 🔑 | URL da textura (`texture_url` + `data_origin: sentinel\|procedural` — nativa p/ demo quando o dataset existe; fallback procedural autenticado em clone limpo) |
| `GET` | `/api/talhao/{farm_id}/heightmap?size=256` | 🔑 | **Heightmap Copernicus DEM GL-30** (relevo p/ Three.js; `available=false` + `reason` quando a tile não existe) |
| `GET` | `/api/talhao/dates` | — | Datas Sentinel-2 e índices disponíveis (catálogo — público) |
| `GET` | `/api/analytics/farm/{farm_id}` | 🔑 | Série temporal + zoneamento + estimativa de safras (da fazenda acessível) |
| `GET` | `/api/weather/farm/{farm_id}` | 🔑 | Clima NASA POWER (cache TTL de 60 min por padrão) |
| `POST` | `/api/simulation/what-if` | 🔑 | Impacto de N/água/pragas sobre NDVI, safra e financeiro |
| `GET` | `/api/reports/farm/{farm_id}/pdf` | 🔑 | Laudo técnico em PDF (da fazenda acessível) |

> 🔑 = exige `Authorization: Bearer <access_token>` (401 sem token). Leituras de fazenda
> NÃO são mais públicas (PR #3 — privacidade multiusuário).

### 🏡 Ownership de Fazendas (PR #3) — privacidade multiusuário

Cada fazenda pertence a um **dono** (`farms.owner_id` → `users.id`). Regras:

- **Usuário comum**: só enxerga/gere as **próprias** fazendas. `GET /api/farms` retorna apenas as
  suas; `POST` cria vinculada a ele (o `owner_id` **nunca** vem do payload — fail-closed); `PUT`/`DELETE`
  funcionam só na própria farm.
- **Admin** (`admin`): tem **acesso global** — lista/ler/edita/exclui qualquer fazenda.
- **Farm de demonstração (id=1)**: pertence ao admin demo, mas é marcada como **compartilhada**
  (`is_shared=true`) para funcionar como vitrine — **qualquer usuário autenticado** pode lê-la
  (analytics/clima/textura/heightmap/PDF), mas não editá-la/excluí-la.
- **Anônimo**: qualquer leitura de fazenda → **401** (autenticação precede autorização).
- **Fazenda de OUTRO usuário**: o backend responde **404** (e não 403), evitando expor a existência
  do recurso alheio.

> O usuário demo `admin@orion.com / 123456` possui role `admin` (seed) e é o dono da farm demo.

### 🛡️ RBAC — matriz de acesso

| Rota | Anônimo | Usuário comum (própria farm / demo) | Admin (`admin`) |
|------|:-------:|:---:|:---:|
| `GET /api/farms` · `/api/farms/{id}` | **401** | **200** (própria / demo) · **404** (alheia) | 200 (todas) |
| `POST /api/farms` | **401** | **201** (dono = usuário logado) | 201 |
| `GET` `/api/talhao/...`, `/api/analytics/...`, `/api/weather/...`, `/api/reports/.../pdf` | **401** | **200** (própria / demo) · **404** (alheia) | 200 |
| `PUT` / `DELETE /api/farms/{id}` | **401** | **200** (própria) · **404** (alheia/demo) | 200 (qualquer) |
| `POST /api/simulation/what-if` | 401 | 200 | 200 |

- A validação usa o claim `role` do **payload do token JWT** (snapshot do login), comparado à hierarquia `role_hierarchy` (`config.py`, sobrescrevível por `ROLE_HIERARCHY` no `.env`).
- O papel de nível máximo (`admin`) tem **acesso global**; papéis desconhecidos/ausentes caem no nível de `user` (**fail-closed**).
- Autenticação precede autorização: sem token (ou token inválido) o resultado é sempre **401**, nunca 403.

## 🗄️ Migrações (Alembic)

O esquema evolui via **Alembic** (`backend/alembic/`). A revisão
`0001_farm_ownership` mantém `farms.owner_id` como FK física para `users.id`,
adiciona `is_shared` e faz o **backfill seguro** de bancos legados (fazendas
órfãs → admin demo; farm demo id=1 → compartilhada).

Em SQLite, constraints não podem ser adicionadas por `ALTER TABLE ... ADD
CONSTRAINT`. Por isso, quando o banco legado ainda não tem a estrutura final, a
migration usa `batch_alter_table()` para reconstruir somente `farms` dentro da
transação do Alembic. O processo preserva dados, IDs, talhões, constraints e
índices existentes. `is_shared` usa `DEFAULT 0` apenas durante a cópia das
linhas legadas; uma segunda etapa remove o default físico, deixando o schema
final equivalente ao produzido por `Base.metadata.create_all()`.

```bash
cd backend
# 1. Gere o arquivo .env a partir do exemplo (se ainda não tiver)
cp .env.example .env
# 2. Aplique as migrações pendentes (idempotente)
alembic upgrade head
# 3. (Opcional) criar nova revisão após mudar models.py
alembic revision --autogenerate -m "descreva a mudança"
```

- O `DATABASE_URL` vem de `config.settings` (12-factor / `.env`) — não duplicado no `alembic.ini`.
- O `env.py` controla explicitamente a transação SQLite e registra
  `0001_farm_ownership` em `alembic_version`. Não há conexão AUTOCOMMIT paralela.
- Durante o batch SQLite, o enforcement da conexão de migration é desligado
  apenas para permitir a troca segura de uma tabela referenciada por
  `talhoes`; a operação ocorre na transação explícita e o enforcement é
  religado antes do fechamento da conexão. As conexões normais da aplicação
  habilitam `PRAGMA foreign_keys=ON` centralmente em `database.py`.
- O `lifespan` aplica `alembic upgrade head` no boot e interrompe a inicialização
  se a migration falhar, evitando iniciar com schema divergente.
- O teste dedicado `tests/test_schema_parity.py` compara banco novo e banco
  legado migrado, incluindo colunas, defaults, índices, FKs, dados preservados,
  `alembic_version` e idempotência.

## 🔐 Segurança

- **Senhas** nunca em texto puro: hash bcrypt via `passlib` (`security.py`). Bases legadas com senha crua são **rehashed automaticamente** no primeiro login bem-sucedido.
- **Sem auto-registro privilegiado**: `POST /api/auth/register` ignora qualquer `role` enviado no payload e atribui rigidamente o papel público (`DEFAULT_PUBLIC_ROLE = "Produtor Rural"` em `main.py`). O campo `role` nem faz parte do contrato `UserCreate`; contas `admin` existem apenas via seed interno (`seed_demo_data`) — impossibilitando escalada de privilégio pelo cadastro.
- **Ownership fail-closed**: `POST /api/farms` **ignora** qualquer `owner_id` no payload e sempre
  usa o usuário autenticado como dono. Fazendas de outros usuários são inacessíveis (404).
- **JWT (HS256)**: o login emite `access_token` + `token_type` + `expires_in`. Leituras de fazenda
  e criação de fazendas/talhões e simulação exigem `Authorization: Bearer <token>` via dependência
  `get_current_user` (401 se ausente/inválido/expirado).
- **RBAC e ownership**: `PUT`/`DELETE /api/farms` permitem o **dono da fazenda ou admin**; o claim `role` do token dá acesso global ao admin, enquanto a checagem de `owner_id` restringe usuários comuns às próprias fazendas. Recursos alheios retornam **404** consistente e seguro (matriz acima).
- **Segredo do token** vem de `JWT_SECRET_KEY` no `.env` (12-factor). Se ausente, um segredo **efêmero** é gerado no boot (adequado só p/ dev — tokens expiram no restart).
- **CORS** restrito às origens declaradas em `CORS_ORIGINS` (`.env`) — nunca `*`.
- **Validação** total com Pydantic (`Field` com faixas de lat/lon, áreas, parâmetros agronômicos e tamanho do heightmap).
- **Texturas/heightmap privados**: os PNGs por fazenda são servidos por rotas autenticadas
  (`/api/talhao/{id}/texture.png`, `/api/talhao/{id}/heightmap.png`) com checagem de ownership; o
  `TextureLoader` do Three.js baixa URLs relativas ou absolutas via `fetch` com Bearer token,
  converte a resposta para Blob e revoga o `blob:` URL após o carregamento. As texturas **nativas**
  de demonstração (`sentinel-21KXQ-*`, servidas via `StaticFiles`) permanecem públicas por serem
  asset de vitrine.

## ⛰️ Topografia — Copernicus DEM (CDSE real + GL-30 local)

O terreno 3D usa **relevo real** em vez de um plano com displacement genérico, com fonte reportada em `source`:

- **CDSE configurado** (`CDSE_CLIENT_ID/SECRET`) → `GET /api/talhao/{farm_id}/heightmap` tenta primeiro o **DEM COPERNICUS_30** (GLO-30, infill GLO-90) via Process API; se indisponível, **COPERNICUS_90** (GLO-90 global). `source` = `copernicus_30`/`copernicus_90`, sem baixar produto inteiro (raster só da área do talhão).
- **Sem CDSE** → recorte da tile local (`s23_w056`, etc.), normalização 0–255 e resample para `size`×`size`; `source` = `copernicus_gl30`/`local_geotiff`.
- **Sem tile/sem CDSE** → `available=false` + `reason`; o 3D mantém o fallback explícito (deslocamento via NDVI) — nunca tela branca.
- O frontend aplica o heightmap como `displacementMap` (canal lido pelo Three.js), mantendo as texturas de cor (NDVI/RGB) e o **mesmo cache LRU de VRAM** (1 heightmap por fazenda).

**Como obter a tile (modo offline):** baixe `Copernicus_Dem_GLO30_<sN>_w<NNN>` (OpenTopography ou portal Copernicus) e coloque em `backend/data/dem/`; a API também tenta baixar automaticamente (best-effort, `DEM_DOWNLOAD_ENABLED`).

## 🛰️ Dados REAIS Sentinel-2 — Copernicus Data Space Ecosystem (CDSE)

O backend busca observação real sempre que possível, com fallback procedural explícito quando não há dado (sem credenciais, sem cena, erro/rate-limit). Nada é marcado como Sentinel sem ser.

**Configuração (`backend/.env`, ver `.env.example`):**
```
CDSE_ENABLED=true
CDSE_CLIENT_ID=...        # OAuth Client do dataspace.copernicus.eu
CDSE_CLIENT_SECRET=...    # NUNCA versionado; nunca enviado ao frontend
CDSE_STAC_URL=https://stac.dataspace.copernicus.eu/v1
CDSE_PROCESS_URL=https://sh.dataspace.copernicus.eu/process/v1
CDSE_LOOKBACK_DAYS=60
CDSE_MAX_CLOUD_COVER=20
CDSE_TIMEOUT_S=45
CDSE_CACHE_HOURS=12
CDSE_RASTER_SIZE=256
```

**Fluxo (funções puras em `backend/services/copernicus_service.py`):**
```
polígono do talhão (KML ≥ 3 pts) → STAC search sentinel-2-l2a (intersects)
  → seleção explícita: mais recente ≤ 20% nuvem; relaxa (+15, +35, teto 100)
  → Process API (bounds da FAZENDA, evalscript B02/B03/B04/B05/B08/B11+SCL+dataMask)
  → máscara SCL (exclui nuvem/sombra/nodata) → RGB/NDVI/EVI/NDRE/NDMI reais
  → PNG 256×256 mascarado + estatísticas + proveniência, cache em disco
    (dynamic_talhoes/farm_X_talhao_Y/cdse/<data>/) e memória (TTL config).
```

**Fórmulas documentadas (validadas numericamente em `tests/test_copernicus_service.py`):**
- NDVI = (B08−B04)/(B08+B04) · NDRE = (B08−B05)/(B08+B05) · NDMI = (B08−B11)/(B08+B11)
- EVI = 2.5·(B08−B04)/(B08+6·B04−7.5·B02+1)
- RGB = [2.5·B04, 2.5·B03, 2.5·B02] clampado a [0,1] (padrão visual CDSE)

**Convenções de geometria (CRÍTICAS — corrigidas em PR #5c):**
- `aoi_bounds()` devolve **`(min_lon, min_lat, max_lon, max_lat)`** = [west, south, east, north] — ordem exigida pelo STAC (`bbox`) e pela Process API (`bounds.bbox`). A versão anterior devolvia (min_lat, max_lat, min_lon, max_lon) e enviava bbox invertida ao STAC (`west > east` → HTTP 400).
- GeoJSON (KML→`intersects`) usa **`[lon, lat]`**.
- A janela temporal é RFC3339: `YYYY-MM-DDThh:mm:ssZ/YYYY-MM-DDThh:mm:ssZ`.

**Diagnóstico de falhas (seguro):** qualquer 4xx/5xx do CDSE vira `real_data_error` com `stage` (`STAC_SEARCH`/`PROCESS_API`/`AUTH`), `http_status`, `endpoint`, `content_type` e um trecho truncado/sanitizado do corpo do provedor — nunca `Authorization`, `access_token`, `client_secret` ou `refresh_token`. `STAC OK SEM CENAS` (200 vazio) é distinto de `STAC ERRO` (4xx/5xx).

**Camadas novas na API (mantendo compatibilidade):**
- `GET /api/talhao/{id}/texture?layer=rgb|ndvi|evi|ndre|ndmi&date=YYYY-MM-DD` → `data_origin` (`sentinel`|`procedural`) + proveniência (`collection`, `product_id`, `acquisition_date`, `cloud_cover`, `processing_level`, `bands`, `valid_pixel_percentage`, `selection_reason`) quando real; `real_data_status` (`not_configured`|`no_scene`|`error`|`ok`) + `real_data_message` quando não.
- `GET /api/talhao/{id}/texture.png?layer=...&date=YYYY-MM-DD` → PNG (real cacheado ou procedural).
- `GET /api/talhao/{id}/dates` → calendário real (6–12 cenas úteis, `source: "sentinel-cdse"`) ou a grade oficial (`source: "config"`).
- `GET /api/talhao/dates` → `visual_layers` agora inclui `rgb`.

**Validação de integração real (opcional, FORA da suíte):**
```
python scripts/test_cdse_connection.py --lat -22.7182 --lon -55.5421 --area 42.54 --demo
```
Sem rede/credenciais este script apenas informa o status — nunca imprime segredo/token.

**Sem credenciais** o app segue 100% funcional (texturas procedurais com badge
`VISUALIZAÇÃO APROXIMADA` e mensagem **DADOS SATELITAIS REAIS NÃO CONFIGURADOS**).

## 🧊 Simulador 3D — pipeline (PR #4 ↔ PR #5d)

O Split-View (CENÁRIO REAL × CENÁRIO SIMULADO) segue este fluxo, **sem depender de
arquivos fora do Git**:

```
fazenda → /api/talhao/{id}/texture → texture_url (+ data_origin + proveniência)
        → fetch autenticado (Bearer, AbortSignal) → Blob → TextureLoader → material.map
fazenda → /api/talhao/{id}/heightmap → heightmap_url (ou available=false + reason)
        → displacementMap (relevo Copernicus GL-30) → linha discreta nos metadados
        → sem tile → fallback explícito ("relevo aproximado via índice")
what-if → /api/simulation/what-if → delta_ndvi
        → textura simulada via canvas (pixel a pixel) no lado direito
        → SEMPRE sobre a MESMA data/layer da cena real APLICADA
```

### Sincronização assíncrona (PR #5d)

**Sequência obrigatória: CARREGAR → APLICAR → ESPERAR → PRÓXIMA.** A timeline
NUNCA avança enquanto a próxima cena carrega; a data principal (`date-label`,
slider e pílulas) é a da imagem **efetivamente aplicada**.

- **Máquina de estados** (`createTimelineMachine`): `mode` = paused/playing,
  `status` = idle/loading/ready/error, com `appliedIndex`/`appliedLayer`/
  `appliedMeta` (fonte da verdade da cena aplicada) e `pending*` (carga em voo).
- **Race-safe**: `createSceneLoadGate` (generationId) + `AbortController` por
  carga; respostas atrasadas são descartadas e NUNCA substituem a cena atual;
  troca manual de data e Pause cancelam/invalidam a carga anterior.
- **Play**: intervalo que só dispara `playerCanAdvance()` (playing + ready);
  durante loading o botão permanece "⏸ Pause" visualmente ativo, mas nada avança.
- **Pausa**: cancela/invalida a carga, continua com a cena aplicada e NÃO retoma
  sozinho (revertendo a UI se uma troca de layer estava em voo).
- **Erro**: pausa automática + toast compacto com "Tentar novamente"; Play também
  funciona como retry. Nunca troca silenciosamente de data nem fica preso.
- **Layer swap** (RGB/NDVI/EVI/NDRE/NDMI): congela o avanço, carrega na data
  aplicada, atualiza proveniência e retoma o Play se já estava ativo.
- **What-If**: a base (`baseTextureForSim`) e o `lastSimBaseMeta` só mudam no
  COMMIT da cena — Real e Simulado nunca se misturam por assincronia.

### Hierarquia visual (do mais importante ao menos)

1. Terreno 3D (Split-View) — ocupa o máximo da tela;
2. label de cada cenário (REAL ▸ `Sentinel-2 L2A • dados reais` | SIMULADO ▸
   `Projeção What-If • base <data>`);
3. timeline (Play, spinner de carregamento, slider, data aplicada, layer);
4. painel What-If (parâmetros);
5. metadados compactos (1 linha de proveniência + "Detalhes" retrátil);
6. DEM → apenas uma linha discreta no rodapé dos metadados;
7. mensagens técnicas → `console`/toast compacto de erro.

- **Dado real vs aproximado**: a API devolve `data_origin` (`sentinel` = CDSE ou
  dataset nativo presente na máquina; `procedural` = textura espectral gerada) +
  `real_data_status`/`real_data_message` quando não há real. A proveniência mostra
  `Sentinel-2 L2A • 15/08/2026 • Nuvens 2,0%` (real) ou o motivo explícito do
  aproximado — nunca um terreno branco silencioso.
- **Proveniência**: 1 linha + botão "Detalhes" (drawer retrátil com Aquisição,
  Nuvens, Satélite, Produto, ID do item, Bandas, Pixels válidos, Seleção,
  Fornecedor).
- **Fallback visual**: se o load do asset falhar (rede/CORS/404) sem ser aborto,
  o 3D aplica uma textura de fallback claramente marcada; a malha, o relevo e os
  controles continuam funcionando e o erro pausa com retry.
- **Geometria real do talhão**: o terreno usa o contorno KML (projeção alinhada à
  rasterização) com contorno visível; sem KML, usa a elipse proporcional à área —
  nunca um retângulo genérico.
- **What-If visual**: N+/irrigação ⇒ mais verde; pragas ⇒ vermelho/castanho
  (aplicado por pixel na textura do lado direito, com `displacementScale` e
  valores financeiros atualizados em tempo real).
- **Clone limpo**: sem `sentinel-21KXQ-*` e sem tiles DEM, o simulador funciona
  em modo aproximado explícito; com os datasets presentes, volta ao dado real.

## 📊 Dados

- **Sentinel-2**: 13 passagens em `sentinel-21KXQ-<data>/` (PNGs coloridos por índice).
- **Topografia**: Copernicus DEM GL-30 — tiles GeoTIFF em `backend/data/dem/` (fora do Git).
- **Contornos**: `contorno_kml` / `contorno_shp` (talhão 01 de exemplo).
- **Clima**: NASA POWER (agregado, defasagem de 3 dias; fallback climatológico quando indisponível).

## 🧪 Teste rápido da API (fluxo JWT)

```bash
# 1. Login → captura o access_token
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@orion.com", "password": "123456"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

# 2. Simulação what-if (protegida — exige o token)
curl -X POST http://localhost:8000/api/simulation/what-if \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"nitrogen_kg": 50, "water_mm": 10, "pest_pressure_pct": 5, "area_ha": 42.54}'

# 3. Sem token → 401 Unauthorized
curl -i -X POST http://localhost:8000/api/simulation/what-if \
  -H "Content-Type: application/json" -d '{}' | head -1

# 4. Série temporal (farm de demo, leitura pública)
curl http://localhost:8000/api/analytics/farm/1

# 5. Heightmap topográfico (Copernicus DEM GL-30)
curl http://localhost:8000/api/talhao/1/heightmap

# 6. Clima (cache TTL — a 2ª chamada não consulta a NASA)
curl http://localhost:8000/api/weather/farm/1
```

## 🧪 Suíte de testes automatizados (pytest)

A suíte versionada em `tests/` cobre as 4 frentes exigidas + ownership/migrações/paridade de
schema, assets autenticados e o **pipeline 3D (PR #4 ↔ PR #5d)** — **255 testes** (1 skip por
dataset Sentinel-2 ausente fora do git):

| Módulo | Testes | Abrangência |
|---|---|---|
| `test_auth_security.py` | 27 | Registro/login (201/400/401/422), **papel forçado no registro** (qualquer `role` enviado — inclusive `"admin"` — é ignorado; sem prerrogativas administrativas; admin isolado ao seed), bcrypt (hash, salt, verificação), migração transparente de senha legada → bcrypt, emissão e validação de JWT (claims, expirado, assinatura inválida, `sub` inexistente) |
| `test_rbac.py` | 21 | Admin global 200 (PUT/DELETE/POST/what-if), leituras **privadas exigem token** (401 anônimo / 200 autenticado), usuário comum gerencia a **própria** farm (200), farm alheia → 404, sem token / token inválido 401 (autenticação precede autorização) |
| `test_farms_sim.py` | 28 | Contratos Pydantic (criação + resposta + What-If exatos a 9 campos), 422 parametrizados, 404, texturas dinâmicas em disco (rota autenticada), datas Sentinel-2, matemática da simulação (constantes por crop), analytics (série temporal, zoneamento, safras), laudo PDF |
| `test_dem.py` | 17 | Nomenclatura SRTM/Copernicus, bounds (KML e por área), seleção de tile, endpoint de heightmap: recorte → normalização → PNG 256×256 servido por rota autenticada, `size`, 422, 404, indisponível com orientação |
| `test_services.py` | 36 | Cache de clima (hit, por coordenadas, TTL, fallback em erro, fallback cacheado), regras de negócio do what-if, estimativa de safra, zoneamento espectral, paletas espectrais |
| `test_3d_pipeline.py` | 15 | **PR #4** — pipeline 3D: demo sem dataset Sentinel (metadados/PNG/analytics com `data_origin`), fazenda dinâmica autenticada, owner/admin/401/404, assets fora do mount público, `PUBLIC_BASE_URL`, heightmap `available=false` + motivo, contrato What-If, frontend servido pelo backend e higiene (`.env` não exposto) |
| `test_frontend_3d_pipeline.py` | 1 | **PR #4 ↔ #5d** — helpers do `index.html` executados em VM Node: classificação de erro HTTP, normalização de URL relativa/absoluta, token no fetch, atribuição real de textura ao material, pixel What-If, polígono KML → plano 3D e fallback de asset (material nunca sem `map`) |
| `test_3d_player_state.py` | 1 | **PR #5d** — máquina de estados da timeline executada em VM Node: Play não avança durante loading, data só muda após aplicar, generationId descarta resposta antiga, Pause durante loading não retoma, troca manual invalida carga anterior, layer swap congela e retoma, falha → error+pause+retry, Real/What-If sincronizados, metadados da textura aplicada, status real/fallback e proveniência compacta + Detalhes |
| `test_ownership.py` | 32 | **Ownership** (usuário cria/ler/edita/exclui a própria farm; `owner_id` correto; payload `owner_id` ignorado), **admin global**, **privacidade** (GETs exigem token → 401; analytics/clima/textura/heightmap/PDF não expõem farm alheia → 404), **migração Alembic** (adiciona `owner_id`/`is_shared` + backfill seguro em SQLite legado) |
| `test_schema_parity.py` | 3 | Paridade banco novo × legado migrado: colunas, tipos, nullable, defaults, PK, índices, FKs, `alembic_version`, idempotência, preservação de dados e enforcement SQLite |
| `test_frontend_texture_urls.py` | 1 | Fluxo frontend de texture.png/heightmap.png relativos e absolutos: classificação, Bearer no fetch, respostas 401/403/404 e revogação do Blob URL após o TextureLoader |

### Execução

```bash
python3 -m venv .venv-test && .venv-test/bin/pip install -r backend/requirements.txt
pytest            # a partir da RAIZ do repositório
```

### Isolamento (git nunca é poluído)

- **Banco SQLite efêmero** em diretório temporário do SO (`DATABASE_URL` via env,
  definido antes do import da aplicação) — removido ao fim da sessão;
- **NASA POWER simulada** no nível da aplicação (a rede nunca é acessada; o
  clima de fallback é testado em nível de serviço com o fetch monkeypatchado);
- **DEM**: tile GeoTIFF sintética (mesmo grid 30 m do GL-30) em temp +
  `DEM_DOWNLOAD_ENABLED=false`;
- Pastas novas criadas em `dynamic_talhoes/` pelos testes são **removidas na
  teardown** (snapshot/restore); `.pytest_cache` e `.venv*` são gitignored.
- O dataset pesado `sentinel-*/` vive **fora do git** (higiene): na ausência
  das texturas, 1 teste é pulado com aviso e a timeline do farm de demo usa
  o fallback documentado do serviço — o restante da suíte roda em qualquer
  clone limpo.

> A suíte também **detectou e corrigiu dois defeitos reais** durante o
> desenvolvimento: o quadrado de fallback de `talhao_bounds` saía 10× maior que
> o talhão (`√area·1000` em vez de `√area·100` metros) e a classificação
> espectral não era mutuamente exclusiva (pct das zonas passava de 100 e o
> índice médio podia ultrapassar o limite físico de 1,0).
