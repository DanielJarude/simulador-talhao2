# 📋 Auditoria de Código — Simulador Talhão 2 (Orion Agro)

| | |
|---|---|
| **Data** | 08/09/2026 |
| **Escopo** | `main` @ `8555781` — frontend (HTML/JS/Three.js r128 + Leaflet + Chart.js) e backend (FastAPI + SQLAlchemy/SQLite) |
| **Metodologia** | Leitura estática completa dos ~3.200 linhas de código + **execução real da API em ambiente limpo** (venv criado do `requirements.txt`) com `uvicorn` para validar os bugs críticos listados |

---

## 0. Resumo Executivo

### 🔴 Críticos (quebram o sistema / segurança)

| # | Problema | Local | Evidência |
|---|----------|-------|-----------|
| C1 | `POST /api/simulation/what-if` **retorna HTTP 500** — o contrato `WhatIfResponse` não casa com o dicionário devolvido pelo serviço | `backend/schemas.py` × `services/simulation_service.py` | **Executado:** `ResponseValidationError: 3 validation errors: total_production_sc, financial_impact_brl, displacement_scale_3d (missing)` |
| C2 | `reportlab` **não está no `requirements.txt`** — em instalação limpa o backend **não sobe** (ImportError no `main.py`) | `backend/requirements.txt` | **Executado:** `ModuleNotFoundError: No module named 'reportlab'` ao importar `main` |
| C3 | **Senhas em texto puro**: registro salva `hashed_password = senha crua`; login compara string; seed do admin é `"123456"`; `auth.html` ainda pré-preenche a senha no formulário | `main.py`, `models.py`, `auth.html:63` | Confirmed por leitura |
| C4 | Sem token de sessão (JWT) — `python-jose`/`passlib` estão no requirements mas **são dependências mortas**; CORS `*` + `allow_credentials=True` (configuração contraditória) | `main.py:24-29` | Confirmed por leitura |

### 🟠 Altos (gargalos reais / bugs latentes)

| # | Problema | Local |
|---|----------|-------|
| A1 | **Não há DEM real**: o "relevo" 3D é `PlaneGeometry(50,50,64,64)` com a **textura NDVI usada como `displacementMap`** (paleta de 4 cores → relevo em degraus). Copernicus DEM GL-30 não é integrado em lugar nenhum | `index.html:805, 887-896` |
| A2 | Texturas **nunca são `dispose()`-adas**: a cada troca de data/camada um novo `THREE.Texture` é criado (13 datas × 4 índices = 52 texturas) sem liberar as anteriores → vazamento progressivo de memória GPU | `index.html:869-902` |
| A3 | `GET /api/analytics/farm/{id}` usa a pasta `farm_{farm_id}_talhao_{farm_id}` (ID duplicado!) — o gerador cria `farm_{farm_id}_talhao_{talhao_id}`. Para fazendas com `talhao_id ≠ farm_id` a série temporal **cai no fallback falso** silenciosamente | `analytics_service.py:112` × `satellite_service.py:52` |
| A4 | **Sem cache na NASA POWER**: cada chamada busca 365 dias de dados; sem `httpx`/async, sem retry, sem validação de esquema; erros engolidos com `print()` | `weather_service.py` |
| A5 | `http://localhost:8000` **hardcoded** em 5 pontos do frontend e na resposta `texture_url` da API → app quebra fora da máquina local (ex.: preview, deploy, HTTPS) | `app.js:2`, `index.html:580,681,877,898`, `main.py:238` |
| A6 | `schemas.py`: classe `FarmCreate` **definida duas vezes** (a primeira é silenciosamente sombreada); zero validação (`Field`) em e-mail, senha, lat/lon, áreas, parâmetros what-if (aceita `nitrogen_kg=-99999`) | `schemas.py` |
| A7 | `satellite_service.py`: loop Python duplo 256×256 na colorização (≈65k iterações × 4 camadas) e `np.random.seed()` **global** (não thread-safe: duas fazendas em paralelo corrompem o seed) | `satellite_service.py:128-140` |
| A8 | `@app.on_event("startup")` (API deprecada no FastAPI ≥0.109); SQLAlchemy import deprecado `sqlalchemy.ext.declarative.declarative_base` | `main.py:41`, `database.py:2` |
| A9 | Geração das 4 texturas espectrais roda **síncrona dentro do `POST/PUT /api/farms`** (usuário fica esperando o processamento) | `main.py:113-121` |

### 🟡 Médios (qualidade / boas práticas)

| # | Problema | Local |
|---|----------|-------|
| M1 | **`README.md` contém letra de música** ("Ela partiu…") em vez de documentação do projeto | `README.md` |
| M2 | **Sem `.gitignore`**: 1.085 PNGs + 159 TIFFs + `agro_orion.db` + 9 `.pyc` **comprometidos no Git** (~14 MB de binários versionados) | `git ls-files` |
| M3 | Sem `.env.example`, sem gerenciamento de configuração (tudo hardcoded: DB URL, lat/lon default, cores, listas de datas) | — |
| M4 | `requirements.txt` com pins frouxos (`>=`) e dependências mortas: `pandas`, `shapely`, `python-multipart` (importadas zero vezes) | `backend/requirements.txt` |
| M5 | `dashboard.html` é **código morto** (nenhuma página o referencia; o dashboard real vive em `index.html`) | `dashboard.html` |
| M6 | Lógica what-if **duplicada**: frontend calcula localmente (`calculateImpact()`, `index.html:819-846`) com constantes duplicadas do `CROP_AGRONOMIC_MODELS` do backend; `SimulationService.calculateWhatIf` (o cliente da API) **nunca é chamado** | `app.js` × `index.html` |
| M7 | Listas de datas Sentinel-2 **duplicadas em 3 lugares** (`main.py:241-252`, `analytics_service.py:118-122`, `index.html:459-462`) — nova passagem de satélite = editar 3 arquivos | — |
| M8 | `index.html` monolito de 964 linhas (HTML + CSS + JS inline); Three.js **r128 de 2021** via CDN global (build não-modulear, descontinuado) | `index.html` |
| M9 | Sem testes, sem linter/formatter configurado, sem CI | — |
| M10 | Tiles de Google Maps via URL não oficial `https://{s}.google.com/vt/...` — violação de ToS do Google | `index.html:544` |
| M11 | `extract_layer_stats` classifica zonas **lendo cores RGB de um PNG renderizado** (depende dos valores exatos da paleta do `satellite_service` — os dois arquivos precisam evoluir em lockstep) | `analytics_service.py:13-27` |

---

## 1. Arquitetura Geral

### 1.1 Estrutura atual

```
simulador-talhao2/
├── index.html          # 964 linhas: dashboard + mapa 2D + 3D + todo o JS inline
├── fazendas.html       # CRUD de fazendas + parser KML/GeoJSON (JS inline)
├── auth.html           # login/cadastro
├── dashboard.html      # ⚠️ código morto (não referenciado)
├── app.js              # serviços de API (Auth/Farm/Weather/Satellite/Simulation)
├── backend/
│   ├── main.py         # 348 linhas: app, rotas, seed, montagem de estáticos
│   ├── database.py     # engine + session
│   ├── models.py       # User, Farm, Talhao
│   ├── schemas.py      # Pydantic (com classe duplicada)
│   ├── requirements.txt
│   ├── agro_orion.db   # ⚠️ banco binário no Git
│   └── services/       # analytics, pdf, satellite, simulation, weather
├── sentinel-21KXQ-*/   # 13 datas × (PNGs + TIFs) — 1.244 arquivos no Git
├── dynamic_talhoes/    # texturas geradas em runtime
└── contorno_kml / contorno_shp/  # geometria de exemplo
```

**Pontos fortes:** a separação `routes (main.py) → services/` já existe e é o caminho certo; os serviços têm responsabilidade coesa e funções pequenas.

### 1.2 Problemas estruturais

1. **Frontend monolítico por página.** Cada `.html` carrega ~300-500 linhas de JS inline + CSS inline. A mesma lógica de UI (sliders what-if, badges, formatação BRL) está duplicada em `index.html` (versões 3D e dashboard) e em `dashboard.html`.
2. **Contrato de dados duplicado** (datas Sentinel, constantes agronômicas, cores de zona) entre frontend e backend.
3. **Sem front estático servido pelo backend.** O usuário precisa de *dois* servidores (Live Server 5501 + uvicorn 8000) e URLs absolutas entre eles — fragilidade que o item A5 explora.
4. **Estrutura proposta (baixo esforço, alto ganho):**

```
simulador-talhao2/
├── frontend/
│   ├── index.html          # só markup + <script type="module">
│   ├── auth.html
│   ├── fazendas.html
│   └── js/
│       ├── config.js       # const API_URL = import.meta.env?.VITE_API_URL ?? ""
│       ├── api.js          # (hoje app.js) — um único cliente fetch com base relativa
│       ├── dashboard.js    # charts, KPIs, zoneamento
│       ├── three/
│       │   ├── viewer3d.js # cena, câmera, split-view, resize
│       │   ├── textures.js # cache LRU + dispose (seção 6.6)
│       │   └── dem.js      # heightmap Copernicus (seção 2.4)
│       └── style.css
├── backend/
│   ├── app/
│   │   ├── main.py         # só FastAPI() + include_router + middleware
│   │   ├── config.py       # pydantic-settings (seção 6.4)
│   │   ├── database.py
│   │   ├── models.py
│   │   ├── schemas.py
│   │   ├── security.py     # hashing + JWT (seção 6.3)
│   │   ├── exceptions.py   # handlers globais
│   │   ├── api/            # routers: auth, farms, analytics, weather, simulation, reports
│   │   └── services/       # (mantidos, com tipo + tests)
│   └── data/               # .gitignore; DEM/TIFs fora do Git (LFS ou CDN)
├── .env.example
├── .gitignore
└── README.md
```

Passos concretos (em ordem): ① extrair JS/CSS do `index.html` para `frontend/js/*` com import map; ② mover backend para `backend/app/` com routers por domínio; ③ `app.mount("/", StaticFiles(directory="frontend", html=True))` para servir as duas camadas na mesma origem (elimina o `localhost:8000` hardcoded); ④ apagar `dashboard.html`; ⑤ mover as listas de datas para **uma** fonte (`/api/talhao/dates`) e o frontend consome.

---

## 2. Performance Gráfica e 3D

### 2.1 O que o código realmente faz hoje

- Cena split-view com **dois `Scene`** compartilhando uma única `PlaneGeometry(50, 50, 64, 64)` (4.356 vértices — trivial).
- A textura **categorizada** de NDVI (PNG 256×256 com 4 cores chapadas + fundo) é usada ao mesmo tempo como `map` e `displacementMap` → o "relevo" é uma superfície em **4 degraus de altura**, não topografia.
- **Copernicus DEM GL-30 não existe no código** — nenhuma referência a DEM/GeoTIFF/elevação em nenhum arquivo. Se o objetivo é o GL-30, ele precisa ser integrado (padrão na seção 2.4).

### 2.2 Gargalos de FPS/memória encontrados (em ordem de impacto)

| # | Gargalo | Por que importa |
|---|---------|----------------|
| G1 | `updateTextures()` cria `new Texture` a cada troca de data/camada **sem `dispose()` da anterior**. No modo *Play* (1.4 s/dia) o navegador acumula texturas e `ImageBitmap` internos | Vazamento contínuo de VRAM; queda progressiva de FPS em sessões longas |
| G2 | `renderer.setPixelRatio(window.devicePixelRatio)` sem teto. Em 4K/Retina (dpr 2-3) o custo de fill-rate dobra/triplica numa cena que já renderiza 2 cenas por frame | FPS inconsistente por dispositivo, sem relação com a complexidade da cena |
| G3 | Three.js **r128 (fev/2021)** via `<script>` global. Builds `examples/js` não-moduleares foram removidos da distribuição oficial do Three.js; o r128 também antecede a correção de `colorSpace`, `renderer.capabilities`, e otimizações do material system | Sem correções de performance de ~5 anos de versões; risco de CDN break |
| G4 | `MeshStandardMaterial` com `side: THREE.DoubleSide` em planos infinitos vistos de cima — fragment shader renderiza as duas faces à toa (≈ +50% fill-rate nos meshes) | Custo sem benefício visual |
| G5 | Loop `animate()` roda `requestAnimationFrame` **para sempre** (mesmo com a aba Dashboard ativa) e a cena 3D é construída imediatamente no load, mesmo que o usuário nunca abra a aba 3D | CPU/GPU ociosa; o ideal é lazy-init ao primeiro `switchScreen('3d')` e pausar o RAF quando `currentScreen !== '3d'` (hoje o RAF roda e só retorna cedo) |
| G6 | Sem `anisotropy` na textura → moiré/aliasing em vistas rasas (obriga o usuário a aproximar, ou a cena "piora" ao afastar) | Percepção de qualidade |
| G7 | PNGs RGBA de ~30-90 KB/diretório baixados a cada troca sem cache HTTP explícito nem preflight das próximas datas | Latência no Play |

### 2.3 Refatoração prioritária — cache de texturas com lifecycle correto

`frontend/js/three/textures.js` (novo):

```js
// Cache LRU de texturas com dispose explícito (corrige G1)
const MAX_CACHED = 8; // 8 texturas 256×256 RGBA ≈ 2,6 MB de VRAM

class TextureCache {
  constructor(renderer) {
    this._map = new Map(); // key -> THREE.Texture (ordem de acesso = LRU)
    this._loader = new THREE.TextureLoader();
    this._loader.setCrossOrigin("anonymous");
    this._maxAniso = Math.min(4, renderer.capabilities.getMaxAnisotropy());
  }

  key(farmId, layer, dateStr) { return `${farmId}|${layer}|${dateStr}`; }

  async load(farmId, layer, dateStr, url) {
    const k = this.key(farmId, layer, dateStr);
    const hit = this._map.get(k);
    if (hit) { this._map.delete(k); this._map.set(k, hit); return hit; } // MRU

    const tex = await new Promise((res, rej) =>
      this._loader.load(url, res, undefined, rej));
    tex.colorSpace = THREE.SRGBColorSpace;      // cores corretas no r152+
    tex.anisotropy = this._maxAniso;            // corrige G6
    tex.generateMipmaps = true;
    tex.minFilter = THREE.LinearMipmapLinearFilter;
    tex.magFilter = THREE.LinearFilter;
    this._map.set(k, tex);

    while (this._map.size > MAX_CACHED) {       // evicção + liberação de VRAM
      const oldest = this._map.keys().next().value;
      this._map.get(oldest).dispose();
      this._map.delete(oldest);
    }
    return tex;
  }

  disposeAll() { for (const t of this._map.values()) t.dispose(); this._map.clear(); }
}
```

Uso no `viewer3d.js`:

```js
const texCache = new TextureCache(renderer);

async function updateTextures() {
  const dateStr = dates[currentIndex];
  const url = textureUrlFor(activeFarm, currentLayer, dateStr); // relativo, sem localhost
  const [colorTex, heightTex] = await Promise.all([
    texCache.load(activeFarm.id, currentLayer, dateStr, url),
    heightTexFor(activeFarm, dateStr), // DEM — seção 2.4 (ou reuso do cache)
  ]);
  matReal.map = colorTex;             matReal.displacementMap = heightTex;
  matSim.map  = colorTex;             matSim.displacementMap  = heightTex;
  matReal.needsUpdate = matSim.needsUpdate = true;
}
```

Outros ajustes diretos no `viewer3d.js` (corrige G2, G4, G5):

```js
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2)); // G2
// materiais:
const matReal = new THREE.MeshStandardMaterial({
  roughness: 0.85, displacementScale: 4.0,
}); // G4: remover THREE.DoubleSide
// lazy init: a cena 3D só é criada no primeiro switchScreen('3d');
// no loop:
function animate() {
  if (currentScreen !== "3d") { running = false; return; } // G5: parar de fato
  requestAnimationFrame(animate);
  controls.update();
  // ... scissor split-view (mantido)
}
```

### 2.4 Integração do Copernicus DEM GL-30 (padrão recomendado: heightmap server-side)

O GL-30 tem resolução nativa de 30 m — para um talhão de ~42 ha o grid nativo é grosseiro; a receita é **recortar + resampling bilinear para 256–512 px e normalizar por talhão**, e depois *combinar* com o micro-relevo do NDVI:

`backend/services/dem_service.py` (novo, depende de `rasterio`):

```python
import numpy as np
import rasterio
from PIL import Image

def build_heightmap(dem_tif: str, min_lat: float, max_lat: float,
                    min_lon: float, max_lon: float, size: int = 256) -> np.ndarray:
    """Recorta o Copernicus DEM GL-30 aos bounds do talhão e devolve uint8 0..255."""
    with rasterio.open(dem_tif) as src:
        win = rasterio.windows.from_bounds(min_lon, min_lat, max_lon, max_lat, src.transform)
        arr = src.read(1, window=win, boundless=True, fill_value=rasterio.nan)

    arr = np.nan_to_num(arr, nan=-9999.0)
    valid = arr[arr > -9000]
    if valid.size == 0:
        return np.zeros((size, size), dtype="uint8")
    lo, hi = float(valid.min()), float(valid.max())
    norm = np.clip((arr - lo) / (hi - lo + 1e-6), 0.0, 1.0)   # normaliza POR talhão
    out = (norm * 255).astype("uint8")
    return np.array(Image.fromarray(out).resize((size, size), Image.BILINEAR))
```

No endpoint de textura, devolver as duas URLs (cor + altura) e no frontend:

```js
matReal.displacementMap = heightTex;   // DEM → topografia real (suave)
matReal.map = ndviTex;                 // NDVI → apenas cor
// se quiser manter o "respiro" da biomassa no relevo:
// displacementScale = 3.2, e gerar heightTex combinado: 0.85*h_dem + 0.15*h_ndvi (no backend, uint8)
```

Alternativa 100% client-side (sem backend) com `geotiff.js` (CDN, ES module):

```js
const buf = await (await fetch("data/copernicus_gl30_cut.tif")).arrayBuffer();
const tiff = await fromArrayBuffer(buf);
const { width, height, data } = await tiff.getImage().render({ width: 512, height: 512 });
const flat = new Float32Array(data);
let lo = Infinity, hi = -Infinity;
for (const v of flat) { if (v > -9000) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
const norm = new Uint8Array(flat.length);
for (let i = 0; i < flat.length; i++) norm[i] = ((flat[i] - lo) / (hi - lo + 1e-6) * 255) | 0;
const tex = new THREE.DataTexture(norm, width, height, THREE.RedFormat, THREE.UnsignedByteType);
tex.needsUpdate = true;
```

> **Nota:** o recorte do TIF (crop + 512²) deve ser feito **uma vez** e o PNG/TIF resultante versionado em `backend/data/` (fora do Git) — nunca processar o DEM dentro do request HTTP.

### 2.5 Checklist rápido de FPS

- [ ] Cache LRU + `dispose()` (G1) — **prioridade máxima**
- [ ] `setPixelRatio(min(dpr, 2))` (G2)
- [ ] Migrar para Three.js ≥ r160 via **import map** (substitui G3 e o `OrbitControls` global)
- [ ] Remover `DoubleSide`; lazy-init da cena 3D; RAF só na aba ativa (G4, G5)
- [ ] `anisotropy` (G6)
- [ ] `prefetch` das datas vizinhas no Play (G7) — `new Image().src = próximaUrl`

---

## 3. Backend e API (FastAPI)

### 3.1 Bugs confirmados por execução

**C1 — `POST /api/simulation/what-if` → 500.** Executado contra o `main` com venv limpo:

```
fastapi.exceptions.ResponseValidationError: 3 validation errors:
  {'type': 'missing', 'loc': ('response', 'total_production_sc'), 'msg': 'Field required', ...}
  {'type': 'missing', 'loc': ('response', 'financial_impact_brl'), 'msg': 'Field required', ...}
  {'type': 'missing', 'loc': ('response', 'displacement_scale_3d'), 'msg': 'Field required', ...}
  input: {'delta_ndvi': 0.0745, 'delta_yield_sc_ha': 3.35,
          'gross_financial_gain_brl': 17813.62, 'net_financial_gain_brl': 9220.55, ...}
```

O schema declara campos que o serviço nunca produz (e o serviço produz campos que o schema não declara). Detalhe agravante: o cliente `SimulationService.calculateWhatIf` existe em `app.js` mas **nunca é chamado** — a UI calcula localmente, então o bug passou despercebido. Correção na seção 6.1.

**C2 — `reportlab` ausente do requirements** → `ModuleNotFoundError` no boot (evidência na seção 0).

### 3.2 Segurança

- `register` grava a senha crua: `hashed_password=user_in.password`. A coluna se chama `hashed_password` mas **nunca há hash**.
- `login` faz match exato em string (timing attack + senha visível no banco).
- Seed cria admin com `"123456"` e `auth.html` pré-preenche essa senha no DOM (qualquer visitante lê no inspector).
- CORS `allow_origins=["*"]` com `allow_credentials=True` — configuração que o próprio Fetch spec rejeita em requests credenciados; deve vir de `settings.cors_origins` explícito.
- `python-jose` e `passlib` já estão instalados (mortos) — a correção (6.3) é barata.

### 3.3 Validação com Pydantic (estado atual → alvo)

| Modelo | Hoje | Falta |
|--------|------|-------|
| `UserCreate` | `email: str` (qualquer string), `password: str` (1 char ok) | `EmailStr`, `Field(min_length=6, max_length=72)`, regex opcional |
| `WhatIfRequest` | sem limites | `nitrogen_kg: ge=-200, le=500`; `pest_pressure_pct: ge=0, le=100`; `area_ha: gt=0` |
| `FarmCreate` | **definido 2× no mesmo arquivo** (a 2ª sombra a 1ª); `latitude` pode ser 999 | `Field(ge=-90, le=90)` / `ge=-180, le=180`; remover duplicata |
| `date_index` (query do PDF) | `int` sem validação | `ge=0, le=12` (hoje `timeline[-1]` "funciona" por index negativo — bug sutil) |
| `layer` (query de textura/analytics) | `str` livre | `Literal["ndvi","evi","ndre","ndmi"]` |

### 3.4 Eficiência — clima NASA POWER e simulação

- **Sem cache**: `GET /api/weather/farm/{id}` dispara 1 HTTP p/ NASA com 365 dias por requisição; abrir a página duas vezes = duas chamadas idênticas. Solução: cache in-memory com TTL por `(lat, lon)` arredondado (6.5).
- **Síncrono com `requests`**: endpoint `def` roda em thread-pool (não derruba o loop, ok), mas o thread-pool padrão do AnyIO tem teto (~40) e a chamada bloqueia a thread por até 10 s cada. Migrar para `httpx.AsyncClient` + `async def` (6.5).
- **Exceções**: `except Exception: print(...); return fallback` engole *tudo* (JSON malformado, key error, timeout) e esconde falhas reais; usar `logging` com `logger.exception`, diferenciar *timeout/falha de rede* (fallback legítimo) de *parse error* (deve propagar 502).
- **Simulação**: `CROP_AGRONOMIC_MODELS.get(crop, default)` aceita qualquer string de cultura e cai no default sem aviso; melhor retornar `crop_supported: bool` no payload.
- **Geração espectral síncrona** em `POST/PUT /api/farms` (A9): mover para `BackgroundTasks` e responder 202, ou pre-calcular.
- **Tipagem**: funções dos services têm hints parciais; respostas de clima/analytics voltam `dict` anônimo — definir `WeatherResponse`, `TemporalSeriesResponse` em `schemas.py` (documentação Swagger + contrato).
- **`@app.on_event("startup")`** → `lifespan` (6.7); **`declarative_base`** de `sqlalchemy.ext.declarative` → `sqlalchemy.orm.declarative_base`.

---

## 4. Qualidade e Boas Práticas

### 4.1 Arquivos essenciais

| Arquivo | Estado | Ação |
|---------|--------|------|
| `README.md` | ❌ contém letra de música ("Ela partiu…") | Reescrever (modelo na seção 6.8) |
| `.env.example` | ❌ inexistente | Criar (seção 6.4) |
| `.gitignore` | ❌ inexistente — por isso 1.244 imagens, o `.db` e 9 `.pyc` estão no Git | Criar + `git rm -r --cached` (seção 6.9) |
| `requirements.txt` | ⚠️ sem `reportlab` (quebra o boot), pins frouxos, deps mortas (`pandas`, `shapely`, `python-multipart`) | Corrigir (seção 6.2) |
| Testes / CI / linter | ❌ ausentes | Mínimo: `pytest` com 3 testes (what-if, schemas, textura) + ruff + pre-commit |

### 4.2 Depreciações e versões

| Item | Versão/estado | Risco |
|------|---------------|-------|
| Three.js | r128 (2021), builds `examples/js` removidos da distribuição | CDN pode quebrar; sem correções de 5 anos |
| `fastapi>=0.100.0` | pin flutuante | `on_event` deprecado no 0.109+ |
| `sqlalchemy>=2.0.0` + import `ext.declarative` | DeprecationWarning em 2.x | Migrar import |
| `passlib 1.7.4` | Incompatível com `bcrypt>=4.1` (traceback famoso) | Fixar `bcrypt==4.0.1` |
| Chart.js/Leaflet via CDN sem hash | `chart.js` sem versão no jsDelivr (build muda) | Fixar versões |

### 4.3 Convenções de dados

- `kml_coordinates` gravado como `String` com pares **`[lat, lon]`** (não-GeoJSON, que usa `[lon, lat]`) — funciona porque todo o pipeline é próprio, mas o contrato deve ser documentado (ou convertido para WKT/GeoJSON).
- Listas de datas duplicadas em 3 arquivos (M7).
- Cores de zona: fonte de verdade em `satellite_service.get_spectral_palette` e *cópias implícitas* nos limiares RGB de `analytics_service.extract_layer_stats` (M11).

---

## 5. Roadmap de Refatoração (priorizado)

| Prioridade | Item | Esforço | Efeito |
|-----------|------|---------|--------|
| **P0** (sprint 0) | 6.1 Fix do what-if (500) | 30 min | API funcional de novo |
| **P0** | 6.2 `requirements.txt` com `reportlab` + pins | 15 min | Backend sobe em ambiente limpo |
| **P0** | 6.3 Senhas hasheadas + JWT + CORS p/ config | 2-3 h | Fecha risco de segurança real |
| **P0** | 6.4 `config.py` + `.env.example` + logging | 1 h | Fim do `localhost:8000` hardcoded e de magic numbers |
| **P1** | 6.5 NASA POWER: `httpx` async + cache TTL | 2 h | Latência e custo de API caem drasticamente |
| **P1** | 6.6 Frontend: `TextureCache` + pixelRatio cap + lazy 3D | 2 h | Estabilidade de FPS/memória |
| **P1** | 6.10 Fix da pasta `talhao_id` no analytics | 10 min | Dados corretos p/ multi-talhão |
| **P1** | 6.11 Validações Pydantic | 1 h | API defensável |
| **P2** | 2.4 DEM Copernicus GL-30 (heightmap) | 1 dia | Topografia real |
| **P2** | 6.8 README real + 6.9 `.gitignore`/LFS | 2 h | Onboarding e repo limpo |
| **P2** | 1.2 Modularização do frontend (modules) | 1-2 dias | Manutenibilidade |
| **P2** | 6.7 `lifespan` + vectorização do `satellite_service` + testes/CI | 1 dia | Higiene |

---

## 6. Refatorações Prioritárias (código pronto)

### 6.1 P0 — Corrigir o contrato what-if (`schemas.py` + `simulation_service.py`)

```python
# backend/schemas.py
class WhatIfRequest(BaseModel):
    nitrogen_kg: float = Field(ge=-200, le=500, description="kg de N por hectare")
    water_mm: float = Field(ge=-100, le=300, description="lâmina de irrigação em mm")
    pest_pressure_pct: float = Field(ge=0, le=100, description="pressão de pragas em %")
    area_ha: float = Field(default=42.54, gt=0, le=100_000)

class WhatIfResponse(BaseModel):
    # campos que o serviço REALMENTE produz:
    delta_ndvi: float
    delta_yield_sc_ha: float
    gross_financial_gain_brl: float
    net_financial_gain_brl: float
    crop: str
    bag_price_brl: float
    # campos extras que o front 3D pode consumir:
    total_production_sc: float | None = None
    displacement_scale_3d: float | None = None
```

```python
# backend/services/simulation_service.py (fim da função)
    base_yield = params.get("base_yield_sc_ha", 60.0)  # adicionar ao CROP_AGRONOMIC_MODELS
    return {
        "delta_ndvi": round(delta_ndvi, 4),
        "delta_yield_sc_ha": delta_yield,
        "total_production_sc": round((base_yield + delta_yield) * area_ha, 1),
        "financial_impact_brl": net_finance,
        "displacement_scale_3d": round(max(1.0, 4.0 + delta_ndvi * 8.0), 3),
        "gross_financial_gain_brl": round(gross_gain, 2),
        "net_financial_gain_brl": net_finance,
        "crop": crop,
        "bag_price_brl": params["price_per_bag"],
    }
```

E no frontend, `calculateImpact()` (index.html:819) deve chamar a API corrigida em vez de duplicar as constantes:

```js
async function calculateImpact() {
  const res = await SimulationService.calculateWhatIf(factorNitro, factorWater, factorPest, activeFarm.total_area);
  if (!res) return; // offline: mantém últimos valores
  document.getElementById("sim-3d-ndvi").innerText = `NDVI ${res.delta_ndvi >= 0 ? "+" : ""}${res.delta_ndvi.toFixed(2)}`;
  matSim.displacementScale = res.displacement_scale_3d; // fonte única de verdade
}
```

### 6.2 P0 — `backend/requirements.txt` corrigido

```txt
# API
fastapi==0.115.6
uvicorn[standard]==0.34.0
pydantic==2.10.4
pydantic-settings==2.7.0
email-validator==2.2.0

# Banco
sqlalchemy==2.0.36

# Auth
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4
bcrypt==4.0.1          # fixado: passlib 1.7.4 quebra com bcrypt>=4.1

# HTTP / Processamento
httpx==0.28.1
Pillow==11.0.0
numpy==2.2.0
rasterio==1.43.0       # novo: recorte do Copernicus DEM (seção 2.4)
reportlab==4.2.5       # FALTAVA — sem ela o backend não inicia

# Removidas (não importadas em lugar nenhum):
# pandas, shapely, python-multipart, requests (substituído por httpx)
```

### 6.3 P0 — Autenticação real (`backend/security.py` novo + rotas ajustadas)

```python
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from config import settings
from database import get_db
import models

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/token")

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_access_token(user: models.User) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode({"sub": str(user.id), "exp": exp},
                      settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

def get_current_user(token: str = Depends(oauth2), db: Session = Depends(get_db)) -> models.User:
    err = HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciais inválidas.",
                        headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        raise err
    user = db.get(models.User, int(payload["sub"]))
    if not user:
        raise err
    return user
```

```python
# main.py — login devolve token, e os endpoints sensíveis passam a exigir o usuário:
@app.post("/api/auth/login", response_model=schemas.UserResponse)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == form.username).first()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(401, "E-mail ou senha incorretos.")
    return schemas.UserResponse(id=user.id, name=user.name, email=user.email, role=user.role)
```

E no seed: `hashed_password=hash_password("TrocarNoPrimeiroLogin!")` — nunca mais senha crua, e o `auth.html` perde o `value="123456"`.

### 6.4 P0 — Configuração centralizada (`backend/config.py` + `.env.example`)

```python
# backend/config.py
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Orion Agro API"
    database_url: str = "sqlite:///./agro_orion.db"
    public_base_url: str = "http://localhost:8000"        # usado em texture_url
    cors_origins: list[str] = ["http://localhost:5501"]   # fim do CORS "*"
    jwt_secret_key: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720
    nasa_power_timeout_s: float = 15.0
    nasa_weather_cache_minutes: int = 60
    sentinel_dates: list[str] = [                            # fonte única (M7)
        "2025-04-07", "2025-04-22", "2025-05-02", "2025-06-11",
        "2025-10-04", "2025-10-11", "2025-11-18", "2025-11-20",
        "2025-12-10", "2025-12-18", "2026-01-27", "2026-02-11", "2026-03-08",
    ]

settings = Settings()
```

```bash
# .env.example
# Orion Agro API
DATABASE_URL=sqlite:///./agro_orion.db
PUBLIC_BASE_URL=http://localhost:8000
CORS_ORIGINS=["http://localhost:5501","http://localhost:8000"]

# Gerar com: python -c "import secrets; print(secrets.token_urlsafe(48))"
JWT_SECRET_KEY=troque-por-um-segredo-aleatorio-longo

NASA_POWER_TIMEOUT_S=15
NASA_WEATHER_CACHE_MINUTES=60
```

Uso no `main.py`:

```python
app.add_middleware(CORSMiddleware,
    allow_origins=settings.cors_origins, allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"])
# ...
return {"type": "dynamic",
        "texture_url": f"{settings.public_base_url}/dynamic_talhoes/{folder_name}/{layer}_cloudless_min_max.png"}
# ...
@app.get("/api/talhao/dates")
def get_available_dates():
    return {"dates": settings.sentinel_dates, "indices": ["ndvi", "evi", "ndre", "ndmi"]}
```

E logging global (substitui os `print()`):

```python
# main.py
import logging
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("orion")
```

### 6.5 P1 — NASA POWER assíncrono com cache TTL (`weather_service.py` reescrito)

```python
import logging, time
from datetime import date, timedelta
from typing import Any

import httpx

from config import settings

logger = logging.getLogger("orion.weather")
_PARAMS = "T2M,T2M_MAX,T2M_MIN,PRECTOTCORR,RH2M,WS2M,ALLSKY_SFC_SW_DWN"
_client: httpx.AsyncClient | None = None
_cache: dict[tuple[float, float], tuple[float, dict[str, Any]]] = {}


async def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=settings.nasa_power_timeout_s)
    return _client


async def fetch_live_nasa_weather(lat: float, lon: float) -> dict[str, Any]:
    key = (round(lat, 3), round(lon, 3))
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < settings.nasa_weather_cache_minutes * 60:
        logger.debug("NASA POWER: cache hit (%s)", key)
        return cached[1]

    try:
        payload = await _fetch_nasa_power(lat, lon)
    except httpx.HTTPError as e:            # rede/timeout → fallback legítimo
        logger.warning("NASA POWER indisponível (%s); usando fallback.", e)
        payload = generate_fallback_weather(lat, lon)
    # NOTE: errors de parse continuam propagando (viram 502 no handler)

    _cache[key] = (now, payload)
    if len(_cache) > 64:                   # teto simples de memória
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    return payload


async def _fetch_nasa_power(lat: float, lon: float) -> dict[str, Any]:
    end = date.today() - timedelta(days=3)      # lag de consolidação da NASA
    start = end - timedelta(days=365)
    resp = await (await _http()).get(
        "https://power.larc.nasa.gov/api/temporal/daily/point",
        params={"parameters": _PARAMS, "community": "AG",
                "longitude": lon, "latitude": lat,
                "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
                "format": "JSON"},
    )
    resp.raise_for_status()
    return process_nasa_payload(resp.json())    # função p/á de 50 linhas, com tipos e tests


# main.py
@app.get("/api/weather/farm/{farm_id}", response_model=schemas.WeatherResponse)
async def get_farm_live_weather(farm_id: int, db: Session = Depends(get_db)):
    farm = db.get(models.Farm, farm_id)
    if not farm:
        raise HTTPException(404, "Fazenda não encontrada.")
    return await fetch_live_nasa_weather(farm.latitude, farm.longitude)
```

### 6.6 P1 — Frontend: veja seção 2.3 (TextureCache) + 2.5 (checklist)

### 6.7 P2 — `lifespan` (substitui `on_event`) e vectorização da paleta

```python
# main.py
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(_: FastAPI):
    with SessionLocal() as db:
        seed_demo_if_empty(db)          # função extraída do on_event
    yield
    # shutdown: fecha httpx.AsyncClient etc.

app = FastAPI(title="Orion Agro API", lifespan=lifespan)
```

```python
# satellite_service.py — replace do loop duplo (~50× mais rápido) + RNG thread-safe
_THRESHOLDS = {"ndvi": (0.35, 0.55, 0.75), "evi": (0.30, 0.60),
               "ndre": (0.35, 0.65), "ndmi": (0.35, 0.65)}
_LUT = {  # mesma paleta de hoje, como arrays
    "ndvi": np.array([[218, 54, 51], [217, 119, 6], [46, 160, 67], [31, 111, 235]], np.uint8),
    # ... evi / ndre / ndmi
}

def colorize(mat_norm: np.ndarray, mask: np.ndarray, layer: str) -> np.ndarray:
    t1, t2, _ = (_THRESHOLDS[layer] + (1.0,))[:3]
    idx = np.select([mat_norm < t1, mat_norm < t2, mat_norm < 1.0], [0, 1, 2], default=3)
    out = np.empty(mat_norm.shape + (4,), dtype=np.uint8)
    out[..., :3] = _LUT[layer][idx]
    out[..., 3] = 255
    out[~mask] = (13, 17, 23, 255)
    return out

# e no campo:  rng = np.random.default_rng(lat_seed ^ lon_seed)  (em vez de np.random.seed global)
```

### 6.8 P2 — `README.md` mínimo que deveria existir

```markdown
# 🚜 Orion Agro — Simulador 3D de Talhões

Plataforma de monitoramento espectral (Sentinel-2: NDVI/EVI/NDRE/NDMI),
agrometeorologia (NASA POWER) e simulação what-if em 3D (Three.js) para
gestão de fazendas.

## Stack
- **Backend:** FastAPI · SQLAlchemy 2 · SQLite · httpx · ReportLab
- **Frontend:** Three.js (split-view 3D) · Leaflet · Chart.js (sem build step)

## Como rodar
1. `python -m venv .venv && .venv/bin/pip install -r backend/requirements.txt`
2. `cp backend/.env.example backend/.env` (definir `JWT_SECRET_KEY`)
3. `cd backend && uvicorn main:app --reload --port 8000`
4. Frontend: `python -m http.server 5501` na raiz (ou servir via o próprio FastAPI)
5. Acesse `http://localhost:5501/fazendas.html`

## Endpoints principais
| Método | Rota | Descrição |
|---|---|---|
| POST | /api/auth/login | Login (devolve token JWT) |
| GET/POST/PUT/DELETE | /api/farms… | CRUD de fazendas/talhões |
| GET | /api/talhao/{farm_id}/texture?layer=ndvi | URL da textura espectral |
| GET | /api/analytics/farm/{farm_id} | Série temporal + zoneamento + safras |
| GET | /api/weather/farm/{farm_id} | Clima NASA POWER (cache 60 min) |
| POST | /api/simulation/what-if | Impacto de N/água/pragas |
| GET | /api/reports/farm/{farm_id}/pdf | Laudo técnico em PDF |

## Dados
- Sentinel-2: `sentinel-21KXQ-<data>/` (13 passagens)
- DEM: Copernicus GL-30 (recorte em `backend/data/`, fora do Git)
```

### 6.9 P2 — `.gitignore` + purga de binários

```gitignore
# Python
__pycache__/
*.py[cod]
.venv*/

# Segredos / ambiente
.env

# Banco e dados de runtime
*.db
*.sqlite3
dynamic_talhoes/

# Dados pesados (usar Git LFS ou CDN)
sentinel-21KXQ-*/
backend/data/
contorno_shp/
```

```bash
# remover do índice (mantém no disco):
git rm -r --cached backend/__pycache__ backend/agro_orion.db \
  sentinel-21KXQ-* dynamic_talhoes
# e, idealmente: git lfs install && git lfs track "*.tif"
```

### 6.10 P1 — Fix do ID do talhão no analytics (1 linha, alto impacto)

```python
# analytics_service.py (hoje: f"farm_{farm_id}_talhao_{farm_id}"  ← ID duplicado)
def get_farm_temporal_series(farm_id, talhao_id, layer="ndvi", total_area_ha=42.54):
    ...
    img_path = os.path.join(BASE_DIR, "dynamic_talhoes",
                            f"farm_{farm_id}_talhao_{talhao_id}",
                            f"{layer}_cloudless_min_max.png")
```

O `main.py` passa a resolver o `talhao_id` da farm antes de chamar o serviço (em vez do fallback perigoso `talhao_id = farm.talhoes[0].id if farm.talhoes else 1`).

### 6.11 P1 — Validações Pydantic completas (`schemas.py`)

```python
from pydantic import BaseModel, EmailStr, Field

class UserCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=6, max_length=72)
    role: str = Field(default="Produtor Rural", max_length=60)

class FarmCreate(BaseModel):            # UMA única definição
    name: str = Field(min_length=2, max_length=160)
    city: str = Field(min_length=2, max_length=160)
    total_area: float = Field(gt=0, le=1_000_000)
    talhao_name: str = Field(min_length=1, max_length=160)
    crop: str = Field(min_length=2, max_length=80)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    kml_coordinates: str | None = Field(default=None, max_length=200_000)

# WhatIfRequest: ver 6.1
```

---

## Anexo A — Evidências de execução

| Teste | Comando | Resultado |
|-------|---------|-----------|
| Boot com venv limpo | `pip install -r backend/requirements.txt && import main` | ❌ `ModuleNotFoundError: No module named 'reportlab'` |
| After `pip install reportlab` + `uvicorn main:app` | boot | ✅ OK |
| `POST /api/simulation/what-if` `{"nitrogen_kg":50,"water_mm":10,"pest_pressure_pct":5,"area_ha":42.54}` | `curl` | ❌ **HTTP 500** — `ResponseValidationError` (3 campos faltando) |
| `GET /api/weather/farm/1` | `curl -w "%{http_code}"` | ✅ 200 (150 ms; fallback local exercitado — NASA indisponível na sandbox) |
| `GET /api/reports/farm/1/pdf?layer=ndvi&date_index=0` | `curl` | ✅ 200, 4.232 bytes (PDF) |
| `git ls-files` | — | 1.085 `.png`, 159 `.tif`, 9 `.pyc`, 1 `.db` versionados |

## Anexo B — Mapa de riscos por área

```
                        Prob.  Impacto   Nota
Segurança (senha p/2)     ALTO    ALTO      C3/C4 — fechar primeiro
Disponibilidade API       ALTO    ALTO      C1/C2 — endpoints quebrados hoje
Estabilidade 3D (FPS)     MÉDIO   MÉDIO     A1/A2 — degrada em sessão longa
Correção dos dados        MÉDIO   MÉDIO     A3 — números falsos sem alerta
Portabilidade             ALTO    MÉDIO     A5 — não roda fora do localhost
Manutenibilidade          ALTO    BAIXO/MED M1/M5/M6/M7/M8
```

---

## Anexo C — Dados REAIS do Talhão (Sentinel-2 + DEM via CDSE) — auditoria da etapa

Executada em **08/09/2026** antes da implementação do serviço CDSE:

| Item | Achado | Fonte |
|------|--------|-------|
| Catálogo STAC | **Coleção `sentinel-2-l2a`**; busca `POST {STAC}/search` com `intersects`, `datetime`, `limit` | documentation.dataspace.copernicus.eu/APIs/SentinelHub/Catalog.html |
| Process API | `POST https://sh.dataspace.copernicus.eu/process/v1`; `bounds.geometry` (Polygon) + `maxCloudCoverage`; respostas `image/tiff` | BeginnersGuide + Process/Examples/S2L2A.html |
| AUTH | OAuth2 `client_credentials` em `identity.dataspace.copernicus.eu/.../token` | Authentication.html |
| DEM | `input.data.type: "dem"` + `demInstance` `COPERNICUS_30` (infill GLO-90) → `COPERNICUS_90` | Data/DEM.html |
| Bandas L2A | B01..B12; true color `2.5*[B04,B03,B02]`; `units:"DN"`/`harmonizeValues:"false"` p/ valores originais | Process/Examples/S2L2A.html |
| Qualidade | SCL e `dataMask` no evalscript; classes 0-3/8-11 tratadas como inválidas | sentinelhub-py docs (referência) |
| Legado | `catalogue.dataspace.copernicus.eu/stac` **descontinuado** — NÃO usar | (comparação das docs) |
| Conectividade | Sandbox sem acesso TCP aos hosts do CDSE (curl → exit 35/000) — integração real fica no script opcional, fora da suíte | execução local |

**Decisões aplicadas:** endpoints centralizados em `services/copernicus_service.py`; nunca versionar `CDSE_CLIENT_SECRET`; sem credenciais → `real_data_status="not_configured"` e fallback procedural explícito; índices com fórmulas documentadas e validadas numericamente; What-If permanece modelo/projeção (nunca "imagem futura do Sentinel"); autenticação dos assets continua via Bearer no frontend.

## Anexo D — Correção do fluxo REAL (teste fora do sandbox, 09/09/2026)

**Sintoma (Windows, credenciais válidas):** OAuth OK; DEM COPERNICUS_30 OK (592–615 m); STAC `status=error` + Process API "CDSE requisição recusada (HTTP 400)".

**Causa raiz encontrada (detalhada por escrita + confirmação de contrato com a doc oficial):**

| # | Problema | Detalhe |
|---|----------|---------|
| D1 | **bbox invertida no STAC/Process (causa do 400)** | `aoi_bounds()` retornava `(min_lat, max_lat, min_lon, max_lon)`; o STAC exige `[minLon, minLat, maxLon, maxLat]`. O corpo enviado tinha `west > east` → HTTP 400. Geométricamente o 400 era inevitável (mesmo OAuth/DEM funcionando, pois o DEM usava conversão própria) |
| D2 | **Diagnóstico insuficiente** | 4xx/5xx guardavam apenas `status`; sem etapa, endpoint, Content-Type e corpo do provedor → impossível distinguir "sem cenas" de "payload inválido" |
| D3 | **`fetch_real_calendar` silenciava o erro** | status "error" não carregava motivo; `status=error` era tratado como "sem cenas" |

**Correções (commit desta etapa):**
- `aoi_bounds()` → `(min_lon, min_lat, max_lon, max_lat)` (documentado e testado); `stac_search`/`_process_request`/`polygon_mask`/`dem_service` adaptados; DEM segue com conversão explícita.
- `CopernicusError` ganha `stage`/`endpoint`/`content_type`/`body_snippet` + `to_detail()`; `_sanitize_snippet()` trunca (1200 chars) e mascara credenciais; 401/403 nunca expõem corpo.
- `fetch_real_calendar` → `(cenas, status, detail)` com `ok` | `no_scene` | `not_configured` | `error` (detalhe seguro em erro).
- Process API S2: `mosaickingOrder="leastCC"` no `dataFilter` (determinismo com múltiplas cenas na janela), corpo auditado contra a doc (`type`, `timeRange`, `maxCloudCoverage`, `bounds.bbox` em ordem geográfica, CRS84, `image/tiff`, evalscript com B02/B03/B04/B05/B08/B11+SCL+dataMask).
- Endpoints: `real_data_error` exposto no JSON da textura e `stac_detail` em `/api/talhao/{id}/dates`.
- Script `test_cdse_connection.py` distingue STAC OK COM CENAS / OK SEM CENAS / ERRO HTTP / ERRO PAYLOAD e PROCESS OK / HTTP 4xx-5xx, mostrando o motivo seguro do provedor.
- Testes novos (Fase 7, sem rede): payload STAC real (bbox em ordem, `collections`, `datetime` RFC3339, GeoJSON `[lon,lat]`), payload Process S2/DEM, datetime inválido → janela até hoje, STAC vazio ≠ STAC erro, parsing de 400 (corpo+Content-Type), 401 sem corpo, sanitização de credenciais, `real_data_error` no pipeline. **253 passed, 1 skipped, 0 failed.**
