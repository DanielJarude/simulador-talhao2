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
| `GET` | `/api/weather/farm/{farm_id}` | 🔑 | **Legado** — clima NASA POWER 12 meses + janela de pulverização (cache TTL de 60 min; fallback climatológico **rotulado** `is_real=false`) |
| `GET` | `/api/climate/farm/{farm_id}?preset=30d` · `?start=&end=` · `&baseline_years=` | 🔑 | **PR #7** — Clima & Condições Agronômicas: período 7d/15d/30d ou personalizado, baseline histórico, indicadores, interpretações conservadoras, confiança e proveniência (localização canônica da fazenda) |
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
- **Sem tile/sem CDSE** → `available=false` + `reason`; o 3D usa "Elevação aproximada" (plano) — nunca tela branca e nunca chama o relevo de "real".
- **PR #5e — relevo é GEOMETRIA, não truque de câmera**: o heightmap é amostrado uma única vez por fazenda (mesh 96×96, ≈9 m/quad para talhão de ~900 m — coerente com o DEM de 30 m), os **vértices** são deslocados no eixo Y (`applyDemToGeometry` + `computeVertexNormals`) na escala real `alívio (m) / (extensão do talhão em m / 50 u)`, e a iluminação direcional revela o relevo. A troca de data **troca só a textura** — o DEM nunca é reconstruído; Real e What-If compartilham a **mesma malha**.
- **PR #5g — terreno com VOLUME topográfico real**: escala **horizontal e vertical explícitas** (`terrainWorldUnitsPerMeter` = 50 u ÷ extensão em m; `demReliefWorldUnits`; a 1× o eixo vertical usa a **mesma proporção física** do campo — ex.: 23 m de relevo em 652 m ≈ 3,5% do lado — e o exagero multiplica **somente a diferença relativa de altitude**: `relativeHeight = (elev − minElevação)`, mín = 0); **superfície = contorno real do talhão** (máscara de grid pelo polígono KML/elipse — nunca um retângulo com a máscara "pintada"); **laterais + base** fecham o bloco (`baseY = minY − 8% da extensão`) acompanhando o DEM na borda; **hillshade multiplicativo** por vértice (encostas 0,8–1,2×, plano = 1,0 → cores dos índices agrícolas preservadas) + **sombras suaves** (PCFSoft, 1 luz, 1024²); grade discreta **abaixo da base**; `?debug3d=1` mostra wireframe/bbox/eixos/estatísticas (nunca por padrão).
- **Exagero vertical 1×/2×/3×/5×** (default 2×) é **apenas visual**: altera a escala do deslocamento da malha, nunca os valores reais de elevação — a UI segue mostrando "Elevação real: X–Y m • relevo relativo: 0–R m • exagero N×" (ex.: "592–615 m") vinda do backend. Trocar o exagero **não refaz STAC/DEM/textura** — só posições Y, normais, paredes e sombreamento. O log `[3D-DEM]` prova tecnicamente a deformação (vértices, min/max reais, alívio, exagero, min/max Y da malha, escala).
- **PR #5f — câmera ENQUADRADA no talhão**: `fitCameraToTerrain()` calcula o `BoundingBox` da malha (contorno real + DEM), usa o **centro como target** e posiciona a câmera em **vista oblíqua diagonal ~42° (35–55° acima do plano)** com distância proporcional à extensão — funciona para fazendas pequenas e grandes, nunca nasce "rasante/faixa no horizonte" nem pode passar sob o terreno (`maxPolarAngle = 0.44π`). O reenquadro roda após `applyTalhaoGeometry` e após o DEM; **"⟲ Resetar visão"** restaura a vista sem tocar na data/layer/exagero; grade discreta com opacidade reduzida abaixo do terreno e iluminação ambiente + hemisférica + direcional lateral (relevo com sombra, textura Sentinel não escurecida).
- **PR #5f — timeline de datas REAIS clicável**: abaixo do 3D, **um botão por cena STAC** ("30 AGO" / "2026", tooltip com data completa + nuvens, scroll horizontal + setas ‹ ›, 5/10/20+ cenas), com **Play/Pause**, velocidade e camada na mesma barra. **Ordem cronológica da interface: esquerda = mais antiga, direita = mais recente** (a API segue DESC internamente — `dates[0]` = mais recente para `latest_date`/cache — e a UI usa `timelineOrderDates` para apresentar ASC, sem nunca mutar o array da API). O destaque azul ("ATUAL") só muda no **COMMIT** da cena (preload nunca move a seleção); estados discretos por ponto (disponível/carregando/pronta/aplicada/erro); "⇥ Mais recente" resolve por `latest_date` (`latestSceneIndex`) e faz scroll automático até o chip; **Play percorre antiga → recente**, para (sem loop) na mais recente mantendo-a selecionada e, se acionado com a mais recente selecionada, reinicia explicitamente pela mais antiga; períodos 30d/60d/90d/6m/1a/Personalizado reconstroem os chips preservando o cache compatível. A timeline real continua construída **exclusivamente** de `GET /api/talhao/{farm_id}/dates` (`is_real=true`/`source=sentinel-cdse`).

**Como obter a tile (modo offline):** baixe `Copernicus_Dem_GLO30_<sN>_w<NNN>` (OpenTopography ou portal Copernicus) e coloque em `backend/data/dem/`; a API também tenta baixar automaticamente (best-effort, `DEM_DOWNLOAD_ENABLED`).

## 🎨 Interface do Simulador 3D — 3 zonas (PR #5h)

Polimento de UX/UI **sem tocar na geometria/pipeline 3D** (volume, DEM, câmera,
timeline cronológica, cache, What-If e backend ficam exatamente como no #5g):

- **3 zonas**: barra superior (navegação) · **viewport 3D dominante** (Real ×
  What-If, `flex:1`) · **barra própria de timeline/controles abaixo do 3D**
  (`clamp(120px,21vh,152px)`) — a timeline **nunca flutua/sobrepõe o terreno**.
- **Linha 1**: Período `[30d][60d][90d][6m][1a]` + Personalizado + "⇥ Mais
  recente" + setas ‹ › + **um botão por cena STAC** (scroll horizontal; visual
  antiga → recente). **Linha 2**: `▶ Play` · `1,6s` · camada RGB/NDVI/EVI/NDRE/NDMI ·
  Relevo `1×/2×/3×/5×` · `⟲ Resetar`.
- **Data atual**: um único chip azul (ex.: `30 AGO / 2026`) por cena aplicada +
  ponto discreto de estado — sem data duplicada nem badges simultâneos.
- **What-If recolhível**: painel compacto (Adubação N · Irrigação · Pragas +
  Resultado NDVI/Produtividade/Impacto) que vira a aba `[⚡ What-If]` ao
  recolher; os valores dos sliders são **preservados** e restaurados ao reabrir.
- **Proveniência/DEM compacta**: card translúcido no canto inferior esquerdo
  (`max-width: 320px`) com `Sentinel-2 L2A • 30/08/2026 • Nuvens 9,8% • DEM
  592–615 m`; `[Detalhes]` abre drawer técnico (product ID, provider, pixels
  válidos, DEM source, elevação real, relevo, escala u/m e exagero).
- **Cores neutras** (o NDVI já traz cor; azul/verde reservados a estado),
  hierarquia tipográfica título → secundário → técnico, labels discretos
  "OBSERVAÇÃO REAL" / "PROJEÇÃO WHAT-IF" e dica mínima "Arraste para orbitar ·
  Scroll para zoom".
- **Loading local** no chip/canto ("Carregando 27/08…") preservando a cena
  anterior até o commit; **erro local** "Falha ao carregar <data> · Tentar
  novamente" — nunca tela inteira.
- **Responsivo**: 1920×1080, 1600×900 e 1366×768 (timeline 112–152px; painel
  What-If encolhe; meta secundário oculto em telas < 1500px; cenas com scroll).

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
- `GET /api/talhao/{id}/dates?period_days=30..730&start=&end=&limit=` → **calendário real** com `source: "sentinel-cdse"`, `is_real: true`, `source_label`, `latest_date` (cena válida mais recente), `count` e `dates` = EXATAMENTE as cenas STAC (mais recente primeiro, filtro de nuvens); **fallback** apenas quando não há real: `source: "config_fallback"`, `is_real: false`, `source_label: "Calendário demonstrativo (datas de demonstração)"` e `fallback_reason` em pt-BR (nunca confundível com Sentinel real).
- `GET /api/talhao/dates` → público/legado demonstração (`source: "config_fallback"`, `is_real: false`); `visual_layers` inclui `rgb`.
- O `.env` do backend agora é resolvido contra a **pasta do módulo** (`backend/.env`) além do CWD — o servidor não perde mais as credenciais CDSE quando iniciado da raiz do repositório.

**Validação de integração real (opcional, FORA da suíte):**
```
python scripts/test_cdse_connection.py --lat -22.7182 --lon -55.5421 --area 42.54 --demo
```
O script imprime o **contrato exato consumido pelo frontend**:
`[3D-DATES] source=... count=... latest=... dates=...` (e confirma se a lista fixa de
configuração foi usada ou não). Sem rede/credenciais apenas informa o status — nunca
imprime segredo/token.

**Sem credenciais** o app segue 100% funcional, mas a timeline exibe o rótulo explícito
**"Calendário demonstrativo (datas de demonstração)"** + motivo — nunca apresenta datas
fixas como se fossem aquisições Sentinel reais.

## 🧊 Simulador 3D — pipeline (PR #4 ↔ PR #5d)

O Split-View (CENÁRIO REAL × CENÁRIO SIMULADO) segue este fluxo, **sem depender de
arquivos fora do Git**:

```
fazenda → /api/talhao/{id}/texture → texture_url (+ data_origin + proveniência)
        → fetch autenticado (Bearer, AbortSignal) → Blob → TextureLoader → material.map
fazenda → /api/talhao/{id}/heightmap → heightmap_url (ou available=false + reason)
        → DEM amostrado 1× (grid 96×96) → VÉRTICES da malha deslocados em Y
          + computeVertexNormals → linha discreta "Elevação real: 592–615 m"
        → sem DEM → fallback explícito ("Elevação aproximada", plano)
what-if → /api/simulation/what-if → delta_ndvi
        → textura simulada via canvas (pixel a pixel) no lado direito
        → SEMPRE sobre a MESMA data/layer da cena real APLICADA
```

### Sincronização assíncrona (PR #5d)

**Sequência obrigatória: CARREGAR → APLICAR → ESPERAR → PRÓXIMA.** A timeline
NUNCA avança enquanto a próxima cena carrega; a data principal (chip azul da cena
aplicadaaplicada, slider e pílulas) é a da imagem **efetivamente aplicada**.

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

### Fila de cenas + pré-carregamento (PR #5e)

**PREPARAÇÃO é separada de REPRODUÇÃO**: abrir o simulador busca o calendário STAC
(`GET /api/talhao/{id}/dates`), aplica a cena atual e **pré-carrega em segundo
plano**; o Play só reproduz cena pronta e nunca avança para uma cena incompleta
(se chegar nela, aguarda apenas aquela). Não há tela grande de loading — só o
spinner discreto + "Preparando cenas: 3/7".

- **Fonte da timeline = STAC real** (nunca lista fixa); "Mais recente" = cena
  válida mais recente do catálogo (índice 0, reflete a política de nuvens);
  período 30/60/90/180/365 ou personalizado (`period_days` / `start` + `end` no
  endpoint — só metadados STAC; texturas processadas sob demanda). `maxDate` da
  UI = "Última aquisição disponível". Sem CDSE → grade oficial marcada como
  fallback (`source: config`).
- **Estratégia** (`computePreloadPlan`): calendário ≤ 12 cenas → pré-carrega
  **TODAS** da layer ativa; maior → janela de 5 (1 anterior + atual + 3
  próximas) em prioridade e o restante em background (mais próximo primeiro).
  Trocar de layer: carrega a cena atual da nova layer primeiro e agenda o
  preload da nova camada em background.
- **Cache por `farmId|talhao|date|layer`** (`sceneCacheKey`): textura +
  proveniência + estatísticas + `real_data_status` + data de aquisição +
  nuvens. LRU de 12 cenas (`SCENE_CACHE_MAX`) + LRU de 16 texturas de VRAM
  (`MAX_CACHED_TEXTURES`); se a VRAM descartar uma textura, a cena associada é
  invalidada (nunca fica "pronta" apontando para textura disposta). `blob:`
  URLs são revogadas somente depois do carregamento e nunca uma URL ainda em
  cache. Play → Pause → Play, voltar/avançar **não refazem** request de cena
  pronta.
- **Fila própria do preload**: prioridade manual > Play > preload, 2 fetches
  simultâneos (`PRELOAD_CONCURRENCY`), dedupe por chave e `generationId`/
  `AbortController` do PR #5d preservados. Preload **nunca** altera a cena
  aplicada; layer swap, Pause e retry continuam os mesmos.
- **Estados discretos da timeline**: disponível/carregando/pronta/aplicada/erro
  (dot sob cada data) — nenhum estado é silencioso.
- **Play fluido**: intervalos entre cenas prontas **sem rede**; pausa entre
  datas configurável (1/1,6/3/5 s no `#play-interval`); crossfade de ~260 ms
  **apenas visual** (opacidade — nunca interpola índices/valores entre duas
  aquisições).
- **Câmera 3D real** (`CAMERA_PRESET`): perspectiva 45°, visão oblíqua aérea,
  órbita/zoom/pan com limites (nunca abaixo do horizonte), iluminação que
  revela o relevo; terreno no plano XZ (Y-up) com grade horizontal.
- **Exagero 1×/2×/3×**: muda só a escala visual do deslocamento da malha; a
  elevação real exibida ("Elevação real: 592–615 m" + alívio) vem do backend e
  nunca é falsificada. If DEM falha → "Elevação aproximada" (plano), nunca
  "real".

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
- **Clima**: NASA POWER Daily (comunidade AG) — reanálise/modelo (MERRA-2/FLASHFlux), grade ~0,5°, defasagem NRT ~3 dias. PR #7: análise por período (7d/15d/30d/personalizado) + baseline histórico + indicadores + interpretações conservadoras + confiança; PR #7-FIX.1: cobertura com três conceitos formais (geral/por variável/dias completos), agregações parciais honestas, sequência seca nula com lacunas, baseline "mesmas datas" com omissão abaixo de 50% e confiança por métrica; falha da fonte → 503 explícito (nunca dado inventado). O endpoint legado (12 meses + janela de pulverização) mantém o contrato e rotula o fallback como dado demonstrativo.

## 🌦️ Clima & Inteligência Agronômica (PR #7)

Camada climática consolidada — a base que os PRs seguintes (Saúde da Lavoura,
Bioinsumos) vão consumir. Auditoria completa em `AUDITORIA_CLIMA_PR7.md`.

- **Fonte real, só pelo backend**: `services/climate_service.py` consulta a
  NASA POWER Daily (`/api/temporal/daily/point`, comunidade **AG**) com
  parâmetros documentados e verificados: `T2M`, `T2M_MAX`, `T2M_MIN`,
  `PRECTOTCORR`, `RH2M`, `WS2M`, `ALLSKY_SFC_SW_DWN` (unidades: °C; °C; °C;
  mm/dia; %; m/s; MJ/m²/dia). **O frontend nunca chama a NASA diretamente.**
- **Localização canônica (PR #6)**: o serviço recebe o ponto da fazenda
  (`farm.latitude/longitude`) validado — sem coordenada fixa primária nem
  "escolher cidade".
- **Períodos**: `preset=7d|15d|30d` (fim = hoje − defasagem NRT de 3 dias) ou
  `start`/`end` personalizado (máx. 366 dias; catálogo desde 1981).
- **Baseline histórico**: média dos N anos anteriores (padrão 5, máx. 10) para a
  **mesma janela do calendário** — rotulado como "referência histórica",
  explicitamente **não** "normal climatológica oficial" (exige 30 anos/WMO).
- **Agregações coerentes**: chuva acumulada + dias com chuva (≥1,0 mm,
  configurável) + maior sequência seca; temperatura média/mín/máx + amplitude
  térmica; radiação média e acumulada (MJ/m²); umidade média/extremos; vento
  médio (m/s→km/h; "máximo" = maior entre médias diárias, documentado).
- **Indicadores derivados** (fórmula declarada em cada item): chuva acumulada,
  dias com chuva, sequência seca, amplitude térmica, desvio de chuva e de
  temperatura vs. referência.
- **Interpretação conservadora** (Fase 5/18): frases rotuladas por categoria
  (`dado_observado` / `dado_calculado` / `interpretacao`), nunca causalidade
  nem diagnóstico ("os dados indicam", "é compatível com", "não há dados
  suficientes"). **Evapotranspiração não é calculada** (a fonte não entrega;
  sem metodologia inventada).
- **Confiança objetiva (FIX.5)**: **duas camadas** — (a) *geral* (escopo da
  análise, baseada nos dias COMPLETOS, com TODAS as variáveis): Alta ≥98% e ≥3
  anos de referência / Média 90–98% ou 1–2 anos / Limitada <90% ou sem anos;
  (b) *por variável*: Alta 100% dos dias / Média 75–99,9% / Limitada <75%
  (rebaixada 1 degrau se a referência tem <3 anos) — cada interpretação declara
  a confiança da variável que usa (`confidence` + `confidence_basis`), nunca
  uma confiança única para todas as métricas. Critérios na própria resposta.
- **Falhas explícitas**: timeout/HTTP/parse → **503** "Dados climáticos
  temporariamente indisponíveis."; sem dados na fonte → `status=
  "insufficient_data"` "Não há dados suficientes para este período."; lacunas
  viram `null` + `coverage` (três conceitos formais, abaixo) (nunca imputação).
- **Cache**: série diária por `(lat, lon, start, end, parâmetros)`, TTL
  configurável (`NASA_CLIMATE_CACHE_HOURS`, padrão 6 h), LRU 128; falha não
  é cacheada.
- **Frontend**: painel "Clima & Condições Agronômicas" integrado ao Dashboard
  (mesmo fluxo FAZENDA → TALHÃO → LOCALIZAÇÃO → SATÉLITE → RELEVO → CLIMA):
  presets de período, métricas com desvio vs. referência, indicadores,
  interpretação com chip de confiança, 2 gráficos diários (chuva e temperatura
  × referência histórica) e rodapé de proveniência. Sem nova dependência
  (Chart.js já presente).
- **Preparação PR #8/#9**: séries diárias com datas ISO-8601 absolutas e
  período `start/end` → junção **temporal** (não causal) futura com o
  calendário Sentinel-2 e com eventos de manejo/bioinsumos.

### PR #7-FIX.1 — cobertura, lacunas e confiabilidade

Correção dos 6 defeitos do playtest (Fazenda Orion, presets 7d/15d/30d).
Regra central: **ausência de dado NUNCA é lida como condição meteorológica**
(dia sem dado de chuva ≠ dia sem chuva; nunca imputar, interpolar ou zerar).

- **Cobertura com três conceitos formais e distintos** (campo `coverage`):
  1. `overall_days`/`overall_pct` — **cobertura geral**: dias com o conjunto
     mínimo para a análise geral (T2M + PRECTOTCORR — declarado em
     `overall_definition`);
  2. `by_metric{precipitation,temperature,humidity,radiation,wind}` —
     **cobertura por variável** (`available_days` + `pct`);
  3. `complete_days`/`complete_days_pct` — **dias completos**: TODAS as 7
     variáveis simultâneas.
  Nunca existe "cobertura" genérica não explicada (causa do bug "1% × 70%":
  uma razão 0,70 formatada com `:.0f` virava "1%").
- **Estados**: `insufficient_data` (0 dias do conjunto mínimo — preservado,
  ex.: 7d sem dados consolidados), `partial` (qualquer dia incompleto, com
  mensagem de contagem explícita "N de M dias … (pct%)"), `ok`.
- **Agregações parciais honestas** (FIX.3): toda métrica declara
  `available_days`/`requested_days`/`complete`; a UI lê "25,0 mm (21/30 dias c/
  dados)". O acumulado é **somente** dos dias com dados (`accumulated_over_days`).
- **Sequência seca com lacunas** (FIX.2): o valor só é afirmado com 100% de
  cobertura de precipitação; com lacunas → `null` + `reason` ("dia sem dados
  não pode ser tratado como dia sem chuva") e o cálculo vai a
  `data_quality.omitted_calculations`.
- **Baseline com lacunas** (FIX.4 — metodologia A+B, documentada em
  `baseline.methodology`): (A) a comparação atual × referência histórica é feita
  **apenas sobre as datas com observação atual** (mesmas posições da janela;
  referência = média dos anos nas mesmas posições); (B) se a cobertura da
  variável for < 50%, o desvio é **omitido** (`compared=false` + `reason`) —
  nunca "período parcial × baseline completo". Cada métrica tem seu próprio
  bloco `metrics[<m>].baseline` com `compared_days/total_days` e
  `current_value/reference_value/deviation(_pct)/classification`.
- **Confiança por métrica + geral** (FIX.5): ver bullet acima; `confidence`
  ganha `scope`, `complete_days_pct`, `by_metric` e `criteria`.
- **`data_quality`**: `by_metric_days`, `missing_days(_total)`,
  `complete_days(_pct)` e `omitted_calculations` (lista de cálculos omitidos e
  por quê).
- **Frontend**: banner **DADOS PARCIAIS** (dias por variável; "nenhum valor foi
  estimado ou preenchido"); bloco **DADOS DO PERÍODO — disponibilidade por
  variável** (X/Y badges + dias completos); cards de métrica com "X/Y dias";
  acumulado de chuva "(21/30 dias c/ dados)"; chip de desvio vira
  "comparação omitida (cobertura insuficiente)" quando não há comparação
  calculada; indicadores nulos exibem "—" + motivo (tooltip "OMITIDO: …");
  proveniência com confiança geral + por métrica.

### Endpoints do PR #7

```bash
# Login
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@orion.com","password":"123456"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

# Últimos 30 dias (consolidados) da farm de demo, com baseline de 5 anos
curl -s "http://localhost:8000/api/climate/farm/1?preset=30d" \
  -H "Authorization: Bearer $TOKEN"

# Período personalizado + baseline de 3 anos
curl -s "http://localhost:8000/api/climate/farm/1?start=2026-07-01&end=2026-07-31&baseline_years=3" \
  -H "Authorization: Bearer $TOKEN"
```

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

# 6. Clima legado (cache TTL — a 2ª chamada não consulta a NASA)
curl http://localhost:8000/api/weather/farm/1

# 7. PR #7 — Clima & Condições Agronômicas por período (baseline + interpretações)
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/climate/farm/1?preset=30d"
```

## 🧪 Suíte de testes automatizados (pytest)

A suíte versionada em `tests/` cobre as 4 frentes exigidas + ownership/migrações/paridade de
schema, assets autenticados, o **pipeline 3D (PR #4 ↔ PR #5h)** e a **camada climática
consolidada (PR #7 / PR #7-FIX.1)** — **424 testes** (423 passing + 1 skip por dataset Sentinel-2 ausente fora do git):

| Módulo | Testes | Abrangência |
|---|---|---|
| `test_auth_security.py` | 27 | Registro/login (201/400/401/422), **papel forçado no registro** (qualquer `role` enviado — inclusive `"admin"` — é ignorado; sem prerrogativas administrativas; admin isolado ao seed), bcrypt (hash, salt, verificação), migração transparente de senha legada → bcrypt, emissão e validação de JWT (claims, expirado, assinatura inválida, `sub` inexistente) |
| `test_rbac.py` | 21 | Admin global 200 (PUT/DELETE/POST/what-if), leituras **privadas exigem token** (401 anônimo / 200 autenticado), usuário comum gerencia a **própria** farm (200), farm alheia → 404, sem token / token inválido 401 (autenticação precede autorização) |
| `test_farms_sim.py` | 28 | Contratos Pydantic (criação + resposta + What-If exatos a 9 campos), 422 parametrizados, 404, texturas dinâmicas em disco (rota autenticada), datas Sentinel-2, matemática da simulação (constantes por crop), analytics (série temporal, zoneamento, safras), laudo PDF |
| `test_dem.py` | 17 | Nomenclatura SRTM/Copernicus, bounds (KML e por área), seleção de tile, endpoint de heightmap: recorte → normalização → PNG 256×256 servido por rota autenticada, `size`, 422, 404, indisponível com orientação |
| `test_services.py` | 36 | Cache de clima (hit, por coordenadas, TTL, fallback em erro, fallback cacheado), regras de negócio do what-if, estimativa de safra, zoneamento espectral, paletas espectrais |
| `test_climate_service.py` | 82 | **PR #7 + FIX.1** — serviço climático: consulta NASA POWER (URL/parâmetros/comunidade AG/período/coordenadas), parsing (fill −999 → null, unidades m/s e MJ/m²/dia), períodos inválidos/máx./pré-1981, **cobertura com 3 conceitos formais** (geral T2M+PREC / por variável / dias completos), agregações com `available_days` declarados (chuva/sequência seca/temperatura/radiação/umidade/vento), **sequência seca nula com lacunas** (NULL interrompe a contagem; "dia sem dados ≠ dia sem chuva"), **baseline metodologia A+B** (mesmas datas com observação; omissão < 50%; ano inválido excluído), **regressão ratio × percentage** ("21 de 30 … 70%" e nunca "1%"), cenário 21/30 de ponta a ponta, presets 7d (insufficient preservado) / 15d (ok) / 30d parcial, confiança **geral + por métrica** + `confidence_basis`, interpretações conservadoras (sem causalidade/diagnóstico), **sem imputação** (NULL nunca vira 0), ausência de ET, cache, timeout/HTTP 502/parse_error, coordenada inválida, tags `is_real`/`data_origin` do legado |
| `test_climate_api.py` | 16 | **PR #7** — endpoint `/api/climate/farm/{id}`: ownership (401 anônimo / 404 alheia / 200 dono / 200 admin), **localização canônica** enviada à NASA, presets 7d/15d/30d + personalizado, 422 (start>end, só start, >366 dias), **503 explícito** com fonte fora (sem dado fake), `insufficient_data` explícito, cache em nível de API, contrato legado `/api/weather/farm/{id}` intacto |
| `test_frontend_climate_panel.py` | 13 | **PR #7 + FIX.1** — painel integrado ao Dashboard (FAZENDA→TALHÃO→LOCALIZAÇÃO), presets 7d/15d/30d + personalizado, **frontend nunca chama a NASA diretamente**, estados explícitos (indisponível/sem dados/Tentar novamente), separação dado/calculado/interpretação, proveniência + confiança visíveis, baseline rotulado "não é normal climatológica oficial", dashboard legada como DADOS DEMONSTRATIVOS, `ClimateService` no app.js, **FIX.1**: bloco "Dados do período — disponibilidade por variável" + "Dias completos", banner **DADOS PARCIAIS** ("nenhum valor foi estimado"), chip "comparação omitida (cobertura insuficiente)", indicador nulo com motivo "OMITIDO:" |
| `test_3d_pipeline.py` | 15 | **PR #4** — pipeline 3D: demo sem dataset Sentinel (metadados/PNG/analytics com `data_origin`), fazenda dinâmica autenticada, owner/admin/401/404, assets fora do mount público, `PUBLIC_BASE_URL`, heightmap `available=false` + motivo, contrato What-If, frontend servido pelo backend e higiene (`.env` não exposto) |
| `test_frontend_3d_pipeline.py` | 1 | **PR #4 ↔ #5d** — helpers do `index.html` executados em VM Node: classificação de erro HTTP, normalização de URL relativa/absoluta, token no fetch, atribuição real de textura ao material, pixel What-If, polígono KML → plano 3D e fallback de asset (material nunca sem `map`) |
| `test_3d_player_state.py` | 1 | **PR #5d** — máquina de estados da timeline executada em VM Node: Play não avança durante loading, data só muda após aplicar, generationId descarta resposta antiga, Pause durante loading não retoma, troca manual invalida carga anterior, layer swap congela e retoma, falha → error+pause+retry, Real/What-If sincronizados, metadados da textura aplicada, status real/fallback e proveniência compacta + Detalhes |
| `test_3d_load_scene_wiring.py` | 1 | **PR #5d ↔ #5e** — wiring real do `loadScene` em VM Node: commit só após o fetch, race de resposta atrasada, Pause, falha+retry, layer swap e, no PR #5e: cena pronta no cache aplica **sem nenhum fetch**, preload em background **não altera** a cena aplicada, Play avança para cena pré-carregada sem request e textura descartada da VRAM invalida a cena |
| `test_3d_scene_cache_preload.py` | 1 | **PR #5e/#5g** — cache/preload/terreno puros em VM Node: chave `farmId|talhao|data|layer` (sem colisão), LRU com evicção, plano ≤ 12 → todas / > 12 → janela 5 + background, limites documentados, estados discreto da timeline, escala real metros→unidades, amostragem bilinear, DEM desloca vértices + normais, 1×/2×/3×/5× só visual, altitude relativa (mín = 0), fallback plano e `CAMERA_PRESET` |
| `test_3d_calendar_timeline.py` | 2 | **PR #5e (correção)** — calendário STAC real × demonstração em VM Node: decisão pura (real só com `source=sentinel-cdse` + cenas; datas STAC diferentes da config; `latest_date`; `no_scene`/`not_configured`/HTTP 500/rede/401 → demo rotulada com motivo) e wiring do `refreshRealTimeline` (resposta real substitui completamente a timeline; fallback identificado "Calendário demonstrativo"; erro de endpoint nunca apresenta demo como real; 'Buscando datas Sentinel-2…' + seleção da mais recente) |
| `test_3d_bootstrap_order.py` | 2 | **PR #5g (P0)** — ordem REAL de inicialização do bootstrap em VM Node: executa o script inline INTEIRO na ordem do navegador (stubs DOM/THREE/Leaflet/Chart) e exige que a avaliação chegue ao último statement, sem ReferenceError/TypeError/SyntaxError (pega o TDZ `Cannot access 'terrainGeometry' before initialization' que abortava Dashboard/clima/gráficos) e valida `updateGroundGrid` no estado inicial (fallback seguro) e com o bloco pronto (grade abaixo da base) |
| `test_3d_terrain_volume.py` | 3 | **PR #5g — VOLUME 3D** em VM Node: escala explícita m↔unidades (1× = proporção física real;
  `relativeHeight` mín = 0; 1×<2×<3×<5×; finito com relief=0/DEM faltante), geometria NÃO coplanar
  (maxY>minY, estatísticas/bbox com relevo), lateral+base (baseY<minY, contagens exatas, contorno
  acompanha o DEM), máscara do polígono real (triângulos fora removidos), paredes do contorno,
  hillshade (plano = 1,0; encostas 0,8–1,2), câmera 35–50°, fit determinístico, Real×What-If
  compartilham os MESMOS helpers e o contrato de código (textura no mesh, exagero sem fetch,
  `?debug3d=1` nunca por padrão, grade abaixo da base) |
| `test_3d_camera_chips.py` | 3 | **PR #5f** — VM Node: (1) **câmera** `computeCameraFit` pura (vista oblíqua 35–55° nunca rasante nem abaixo do plano, target = centro, distância proporcional à bbox pequena/grande, aspect, clamp, determinística → reset restaura) e (2) **chips da timeline** (exatamente 1 botão por data STAC, ordem desc, sem datas fixas/intermediárias, tooltip data completa + nuvens, 24 cenas) e (3) **DOM real** `renderDatePills` + estados discretos (aplicada só na cena comitada — preload vira `ready`, nunca seleção; clique → `selectDateIndex`; troca de período reconstrói sem sobras; loading/error) |
| `test_3d_ui_polish.py` | 15 | **PR #5h — INTERFACE/UX** (VM Node + contrato de fonte): timeline é faixa PRÓPRIA fora do viewport 3D (irmã do `#viewport-3d`, nunca flutua sobre o terreno), viewport dominante (flex 1) × timeline `clamp(120px,21vh,152px)`, linhas da barra (status fina + Período/cenas + controles), alturas simuladas 1920×1080/1600×900/1366×768 (3D 60–90% do espaço), What-If recolhível que **preserva valores** (`setWhatIfCollapsed` só troca display, VM Node), proveniência compacta `Sentinel-2 L2A • data • nuvens • DEM m` + drawer com 13 linhas (product ID/provider/pixels/relevo/escala/exagero), **um único chip azul** da data aplicada (sem data duplicada/badge extra), controles presentes e ligados (▶ Play · 1,6s · camada · Relevo 1×/2×/3×/5× · Resetar · períodos · setas) e contratos 3D intactos (OrbitControls, volume, hillshade, sombras, cache por identidade, marcadores `[3D-*]`) |
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
- **NASA POWER simulada** em nível de aplicação **e** de HTTP (PR #7: o
  `requests.get` do `climate_service` é monkeypatchado — a rede NUNCA é
  acessada; fallback legado, erros, parse e baseline são testados em serviço e
  em API com payloads no formato real da resposta);
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
