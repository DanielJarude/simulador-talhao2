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

# 6. Sirva o frontend (porta 5501) — em outro terminal
python -m http.server 5501
# (ou Live Server do VS Code)

# 7. Acesse
#    http://localhost:5501/fazendas.html   → cadastro/propriedades
#    http://localhost:5501/auth.html       → login (demo: admin@orion.com / 123456)
#    http://localhost:8000/docs            → Swagger da API
```

> **Nota:** a URL base da API usada pelo frontend está em `app.js` (`API_URL`).
> Ajuste-a se a API rodar em outra porta/host (o backend também monta URLs
> de texturas a partir de `PUBLIC_BASE_URL` no `.env`).

## 🔌 Endpoints principais

| Método | Rota | Auth | Descrição |
|--------|------|:----:|-----------|
| `POST` | `/api/auth/register` | — | Cria usuário (bcrypt) — papel **fixo** `Produtor Rural`; `role` no payload é ignorado |
| `POST` | `/api/auth/login` | — | Valida bcrypt e **emite o token JWT** (`{access_token, token_type, expires_in, user}`) |
| `GET` | `/api/farms` · `/api/farms/{id}` | 🔑 | **Privado por usuário** — lista/busca só as próprias fazendas (admin vê todas); farm de demo (id=1) é compartilhada (legível por qualquer usuário autenticado) |
| `POST` | `/api/farms` | 🔑 | Cria fazenda **vinculada ao usuário logado** (gera texturas espectrais) |
| `PUT` | `/api/farms/{id}` | 🔑 | Atualiza a **própria** fazenda (ou qualquer uma, se `admin`) |
| `DELETE` | `/api/farms/{id}` | 🔑 | Remove a **própria** fazenda (ou qualquer uma, se `admin`) |
| `GET` | `/api/talhao/{farm_id}/texture?layer=ndvi` | 🔑 | URL da textura espectral (nativa para farm demo, dinâmica autenticada p/ demais) |
| `GET` | `/api/talhao/{farm_id}/heightmap?size=256` | 🔑 | **Heightmap Copernicus DEM GL-30** (relevo p/ Three.js) |
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
- **RBAC**: `require_role("admin")` restringe rotas administrativas/destrutivas (`PUT`/`DELETE /api/farms`) validando o claim `role` do token — papéis insuficientes (e não-donos) recebem **404**
  consistente para recursos alheios (matriz acima).
- **Segredo do token** vem de `JWT_SECRET_KEY` no `.env` (12-factor). Se ausente, um segredo **efêmero** é gerado no boot (adequado só p/ dev — tokens expiram no restart).
- **CORS** restrito às origens declaradas em `CORS_ORIGINS` (`.env`) — nunca `*`.
- **Validação** total com Pydantic (`Field` com faixas de lat/lon, áreas, parâmetros agronômicos e tamanho do heightmap).
- **Texturas/heightmap privados**: os PNGs por fazenda são servidos por rotas autenticadas
  (`/api/talhao/{id}/texture.png`, `/api/talhao/{id}/heightmap.png`) com checagem de ownership; o
  `TextureLoader` do Three.js envia o Bearer token. As texturas **nativas** de demonstração
  (`sentinel-21KXQ-*`, servidas via `StaticFiles`) permanecem públicas por serem asset de vitrine.

## ⛰️ Topografia — Copernicus DEM GL-30

O terreno 3D usa **relevo real** a partir do Copernicus DEM GL-30 em vez de um plano com displacement genérico:

- `GET /api/talhao/{farm_id}/heightmap` recorta a tile SRTM que cobre a fazenda (ex.: `s23_w056`), normaliza a elevação para 0–255 e resampleia para `size`×`size` (potência de 2, ideal p/ mipmaps), servindo um PNG cinza em `/dynamic_talhoes/…/heightmap.png` + mín/máx em metros.
- O frontend aplica esse heightmap como `displacementMap` (o canal lido pelo Three.js), **mantendo as texturas NDVI como cor** e reutilizando o **mesmo cache LRU de VRAM** de antes (1 heightmap por fazenda — sem custo extra).
- **Sem tile disponível**, o endpoint responde `available=false` e o 3D mantém o fallback (deslocamento via NDVI).

**Como obter a tile:** baixe a tile `Copernicus_Dem_GLO30_<sN>_w<NNN>` correspondente à região (OpenTopography ou portal Copernicus) e coloque em `backend/data/dem/`. A API também tenta **baixar automaticamente** a tile dos espelhos públicos (best-effort, `DEM_DOWNLOAD_ENABLED`), cacheando o resultado localmente.

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
schema — **164 testes** (1 skip por dataset Sentinel-2 ausente fora do git):

| Módulo | Testes | Abrangência |
|---|---|---|
| `test_auth_security.py` | 27 | Registro/login (201/400/401/422), **papel forçado no registro** (qualquer `role` enviado — inclusive `"admin"` — é ignorado; sem prerrogativas administrativas; admin isolado ao seed), bcrypt (hash, salt, verificação), migração transparente de senha legada → bcrypt, emissão e validação de JWT (claims, expirado, assinatura inválida, `sub` inexistente) |
| `test_rbac.py` | 21 | Admin global 200 (PUT/DELETE/POST/what-if), leituras **privadas exigem token** (401 anônimo / 200 autenticado), usuário comum gerencia a **própria** farm (200), farm alheia → 404, sem token / token inválido 401 (autenticação precede autorização) |
| `test_farms_sim.py` | 28 | Contratos Pydantic (criação + resposta + What-If exatos a 9 campos), 422 parametrizados, 404, texturas dinâmicas em disco (rota autenticada), datas Sentinel-2, matemática da simulação (constantes por crop), analytics (série temporal, zoneamento, safras), laudo PDF |
| `test_dem.py` | 17 | Nomenclatura SRTM/Copernicus, bounds (KML e por área), seleção de tile, endpoint de heightmap: recorte → normalização → PNG 256×256 servido por rota autenticada, `size`, 422, 404, indisponível com orientação |
| `test_services.py` | 36 | Cache de clima (hit, por coordenadas, TTL, fallback em erro, fallback cacheado), regras de negócio do what-if, estimativa de safra, zoneamento espectral, paletas espectrais |
| `test_ownership.py` | 32 | **Ownership** (usuário cria/ler/edita/exclui a própria farm; `owner_id` correto; payload `owner_id` ignorado), **admin global**, **privacidade** (GETs exigem token → 401; analytics/clima/textura/heightmap/PDF não expõem farm alheia → 404), **migração Alembic** (adiciona `owner_id`/`is_shared` + backfill seguro em SQLite legado) |
| `test_schema_parity.py` | 3 | Paridade banco novo × legado migrado: colunas, tipos, nullable, defaults, PK, índices, FKs, `alembic_version`, idempotência, preservação de dados e enforcement SQLite |

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
