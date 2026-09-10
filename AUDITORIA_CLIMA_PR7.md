# Auditoria do sistema climático/agronômico — PR #7 (Clime & Inteligência Agronômica)

> Data: 09/09/2026 · Base analisada: `main` @ `1e1ed18` (após merge do PR #6)
> Método: leitura completa do backend (`services/`, `main.py`, `models.py`, `schemas.py`, `config.py`),
> frontend (`index.html`, `app.js`, `dashboard.html`, `fazendas.html`), testes (`tests/`) e
> verificação **ao vivo** da API NASA POWER (resposta real obtida em 09/09/2026 para a
> coordenada canônica da demo: `-22.7182, -55.5421`).

---

## 1. O que já existe

### 1.1 Backend

| Componente | Localização | Descrição |
|---|---|---|
| Serviço climático dedicado | `backend/services/weather_service.py` | Consulta **NASA POWER Daily** (`/api/temporal/daily/point`, `community=AG`) com parâmetros `T2M, T2M_MAX, T2M_MIN, PRECTOTCORR, RH2M, WS2M, ALLSKY_SFC_SW_DWN`; janela fixa de **365 dias** (fim = hoje − 3 dias, por defasagem NRT da fonte); cache in-memory com TTL (`nasa_weather_cache_minutes`, padrão 60 min) e LRU (64 entradas); timeout configurável (`nasa_power_timeout_s`, padrão 15 s) |
| Endpoint legado | `GET /api/weather/farm/{farm_id}` em `backend/main.py` | Protegido por JWT (`get_current_user`) + ownership (`_require_farm_access` — PR #3); usa **localização canônica** `farm.latitude/longitude` (PR #6) |
| Consumidor: PDF | `backend/services/pdf_service.py` | O laudo em PDF inclui "Resumo Agrometeorológico (NASA POWER)" (chuva acumulada, temp. média, extremos) a partir do mesmo payload |
| Config | `backend/config.py` | `nasa_power_timeout_s=15`, `nasa_weather_cache_minutes=60` |
| Validação geográfica | `backend/services/geolocation_service.py` | `validate_coordinates(lat, lon)` (faixas, finitude) — **reutilizável** pelo novo serviço |

### 1.2 Frontend

| Componente | Localização | Descrição |
|---|---|---|
| Painel de clima no Dashboard | `index.html` (tel. `screen-dash`) | KPIs "Chuva Acumulada (Total 12 Meses — NASA POWER)" e "Temperatura Média"; gráficos mensais `rainChart`/`tempChart` (Chart.js); tabela "Série Recente & Janela de Pulverização (NASA POWER)" — últimos 14 dias com status derivado (FAVORÁVEL/ATENÇÃO/INAPTO) |
| Carregamento | `loadLiveWeather()` em `index.html` | `fetch` autenticado de `/api/weather/farm/{id}`; sem tratamento de erro explícito visível ao usuário (falha → KPIs ficam "Carregando..." ou com valor do fallback) |
| Cliente | `WeatherService.getLiveWeather` em `app.js` | Mesmo endpoint, com tratamento de 401/403 |
| Página isolada | `dashboard.html` | Dashboard **estático** com dados 100% hardcodados (ver §3) |

### 1.3 Testes existentes

- `tests/test_services.py` — 5 testes do cache de clima: hit, separação por coordenadas, expiração de TTL, fallback em erro de rede, fallback cacheado (monkeypatch de `weather_service._fetch_and_process`).
- `tests/conftest.py` — `fetch_live_nasa_weather` é **simulado no nível da aplicação** (`patch.object(main, "fetch_live_nasa_weather")`): a suíte **nunca** acessa a NASA. O clima fake injetado (`FAKE_WEATHER`) espelha a forma do payload real.
- Regressão de bootstrap: `tests/test_3d_bootstrap_order.py` **executa o script inline real do `index.html` numa VM Node** com stubs de DOM/THREE/Leaflet/Chart — qualquer JS novo adicionado a `index.html` será executado por esse teste (restringe o que pode ser escrito: sem TDZ, sem erro fatal em microtasks, sem globals inexistentes no sandbox).

---

## 2. O que é real

1. **NASA POWER Daily API** — verificação ao vivo em 09/09/2026 (resposta real para a coordenada da demo, período 01–10/08/2026) confirmou:
   - Os 7 parâmetros usados são **realmente suportados** na comunidade AG (daily): `T2M` (°C, média diária), `T2M_MAX` (°C), `T2M_MIN` (°C), `PRECTOTCORR` (mm/day), `RH2M` (%), `WS2M` (m/s), `ALLSKY_SFC_SW_DWN` (**MJ/m²/day** — na comunidade AG o valor diário é o **acumulado do dia**, não W/m²).
   - `fill_value: -999.0`; `time_standard: LST` (hora solar local, padrão da API); `header.api.version: v2.9.7`; fontes `FLASHFLUX/GEOSIT/POWER`; a geometria de resposta inclui elevação do ponto (~541 m na demo).
   - A natureza dos dados é **reanálise/modelo** (MERRA-2 para meteorologia; SRB/CERES/FLASHFlux para radiação), sobre grade global de ~0,5° — **não é medição pontual**. Isso deve ser dito sempre na UI (Fase 3/8).
2. **Ownership/autenticação do endpoint legado** — real e testado (PR #3).
3. **Localização canônica** — o legado usa `farm.latitude/longitude` (resolvidas pelo PR #6: geometria > ponto confirmado > legado). **Não há coordenada fixa como localização principal** no caminho da API.
4. **Cache com TTL** — real e testado (TTL configurável).

## 3. O que é mockado/simulado

| Item | Onde | Gravidade |
|---|---|---|
| **Fallback climatológico SILENTES** | `weather_service.generate_fallback_weather()` — clima fixo de "região S. do PR" (chuvas [220,180,…] mm/mês, temps [33.0,…] °C) devolvido em qualquer exceção de rede/timeout, **sem nenhum marcador de que é demo** | **Alta** — viola Fases 9/18: dado inventado misturado a real |
| **`dashboard.html` 100% hardcodado** | array `passagens` com 13 "registros" climáticos inventados + gráficos mensais com arrays fixos; a página rotula "Ciclo Completo (NASA)" sugerindo dado real; não faz nenhuma chamada de API (nem autenticação) | **Alta** — dados demonstrativos apresentados como reais |
| **Imputação silenciosa no parsing legado** | `weather_service._fetch_and_process`: dia sem RH → 70,0%; dia sem vento → 6,0 km/h; `T2M_MAX/MIN` ausentes → média; `PRECTOTCORR` ausente → 0,0 mm; valores negativos de chuva → `max(0.0, v)` (esconde anomalia da fonte) | **Média** — lacunas viram valores "normais" sem sinalização |
| `FAKE_WEATHER` em `conftest.py` | apenas em testes | ok (isolado, declarado) |
| `activeFarm` inicial em `index.html` (-22.7182/-55.5421) | fallback **visual** substituído por `loadActiveFarm()` no boot | ok (documentado no PR #6) |

## 4. O que está incompleto

1. **Período**: só existe a janela fixa de 365 dias. Não há 7/15/30 dias nem personalizado (Fase 4).
2. **Baseline/histórico**: nenhuma comparação com referência histórica (Fase 6).
3. **Proveniência por indicador**: o payload não informa fonte/unidade/período/resolução/natureza (observado × modelo) por métrica (Fase 3).
4. **Confiança**: nenhum nível de confiança nem critério (Fase 8).
5. **Estados de erro**: fallback silencioso (§3) + `loadLiveWeather()` ignora `!res.ok` sem mensagem ao usuário (Fase 9).
6. **Agregações coerentes por período**: só há agregado mensal fixo (12 meses) e "últimos 14 dias" para a tabela de pulverização; não há acumulado do período pedido, dias de chuva, sequência seca, amplitude térmica, desvios (Fases 4/7).
7. **Unidades**: o payload legado entrega **strings formatadas** (`"25,3 °C"`, `"6,1 km/h"`) — unidade acoplada ao texto; o vento é convertido m/s→km/h apenas no consumo.
8. **Evapotranspiração**: inexistente — **corretamente** inexistente; a NASA POWER não entrega ET na Daily API AG e este PR **não** inventará (Fase 3).

## 5. O que está duplicado

1. Clima hardcodado do `dashboard.html` × fallback climatológico de `weather_service` (mesma ideia, valores diferentes, nenhum rotulado).
2. `Talhao.latitude/longitude` × `Farm.latitude/longitude` (duplicata histórica do PR #6; a fazenda é a fonte canônica — preservar como está).
3. Dois consumidores da mesma rota legado (`loadLiveWeather` inline e `WeatherService` em `app.js`) — tolerável; o novo serviço climático terá um único ponto de uso.

## 6. O que NÃO utiliza a localização canônica

- **Nada no caminho de API** — o endpoint legado e o novo usam sempre `farm.latitude/longitude`.
- `dashboard.html` não usa localização alguma (página estática de vitrine) — será rotulada como demonstrativa (§8).
- Os defaults `-22.7182/-55.5421` nas colunas `Farm/Talhao` (model) e no `activeFarm` inicial (frontend) são fallbacks de schema/visual documentados no PR #6, não fontes primárias. **Não serão alterados** (risco de regressão).

## 7. O que deve ser PRESERVADO (não regredir)

- Contrato exato de `GET /api/weather/farm/{farm_id}` (consumido por `index.html`, `app.js`, **PDF** e testes) — campos `summary/monthly/recent_applications`.
- Cache TTL legado + `nasa_weather_cache_minutes` (testes dependem do mecanismo).
- `main.fetch_live_nasa_weather` como nome importado em `main.py` (o `conftest.py` faz `patch.object(main, ...)` — mudar o nome quebra a suíte inteira).
- Parâmetros NASA validados, defasagem NRT de 3 dias, tratamento de fill `-999.0`.
- Ownership (PR #3), RBAC, localização canônica (PR #6), Sentinel/CDSE (PR #5), DEM, 3D, What-If, Alembic — **inalterados**.
- A tabela "Janela de Pulverização" (regra de negócio derivada existente) — mantida; será rotulada como regra heurística no novo painel.

## 8. O que deve ser SUBSTITUÍDO

1. **Fallback silencioso** → estado explícito: no novo serviço, falha da fonte = erro tipado (`ClimateSourceError`) → HTTP 503 com mensagem "Dados climáticos temporariamente indisponíveis." **Nunca** dado inventado.
2. **Imputação silenciosa** → `None` + contagem em `data_quality.coverage_pct` (lacuna é lacuna).
3. **`dashboard.html` sem rótulo** → banner explícito **"DADOS DEMONSTRATIVOS"** no topo (a página não tem contexto de autenticação; convertê-la a dados reais fica para um PR futuro).
4. No payload legado (sem quebrar o contrato): campos **aditivos** `data_origin` (`"nasa_power"` | `"climatology_demo"`) e `is_real` (bool) + `provenance` mínimo — o frontend passa a rotular "DADOS DEMONSTRATIVOS" quando `is_real == false`.

## 9. O que pode ser REUTILIZADO

- `weather_service.py` como referência de arquitetura (cache in-memory thread-safe com TTL + LRU, logging estruturado) — o novo `climate_service.py` segue o **mesmo padrão**.
- `geolocation_service.validate_coordinates` — validação de coordenadas no novo serviço/endpoint.
- `_require_farm_access` — ownership do novo endpoint.
- `Chart.js` (já carregado no `index.html`) — sem nova dependência (Fase 13).
- Padrões de teste: monkeypatch de rede + TestClient com patch de nível aplicação (Fase 16).
- Rota `GET /api/.../farm/{farm_id}` + `Depends(get_current_user)` — padrão de endpoint.

## 10. Possíveis regressões que este PR poderia causar (e mitigações)

| Risco | Causa | Mitigação |
|---|---|---|
| Quebrar `index.html` (KPIs/gráficos/tabela) | mudança no shape do payload legado | **não** mudar o shape; apenas campos aditivos |
| Quebrar o laudo PDF | PDF lê `weather_data.summary.*` | payload legado inalterado |
| Quebrar a suíte (310 testes) | renomear `fetch_live_nasa_weather` ou `main.fetch_live_nasa_weather`; mudar `conftest.FAKE_WEATHER` | nomes preservados; novos testes em arquivos novos |
| Quebrar `test_3d_bootstrap_order.py` | JS novo no `index.html` com TDZ, erro em bootstrap ou global ausente no sandbox Node | JS novo: apenas *function declarations* + chamada dentro de `loadActiveFarm()` (fluxo async com try/catch); usar somente globals já presentes no harness (`fetch`, `document`, `Chart`, `API_URL`, `authHeaders`, `AuthService`); validado rodando a suíte |
| Sobrecarregar a NASA POWER | baseline multi-anual a cada clique | janelas pequenas por ano (mesmo comprimento do período pedido), cada uma cacheada; baseline padrão 5 anos (máx. 10, configurável); TTL 6 h; período máximo 366 dias |
| Regressão em ownership/privacidade | novo endpoint sem checagem | `_require_farm_access` idêntico ao legado + testes dedicados |
| "Alucinação agronômica" | texto interpretativo agressivo | camada de interpretação conservadora com categorias explícitas (Fase 5/18) + teste que veda palavras-causalidade |

---

## Decisão de arquitetura do PR #7

- **Novo serviço dedicado**: `backend/services/climate_service.py` (conforme sugerido pelo enunciado) — fonte única para o novo painel:
  - `fetch_nasa_power_daily(lat, lon, start, end)` → série diária normalizada (fill → `None`), com metadados da resposta (versão da API, `time_standard`, fontes).
  - `build_climate_report(lat, lon, start, end, baseline_years)` → agregações + baseline + indicadores + interpretações + confiança + proveniência.
- **Novo endpoint**: `GET /api/climate/farm/{farm_id}` (`preset=7d|15d|30d` ou `start`/`end` personalizado; `baseline_years` 1–10, padrão 5) — ownership + localização canônica + validação de coordenadas.
- **`weather_service.py` vira módulo legado compatível** (12 meses + janela de pulverização + PDF), mantendo contrato e agora com marcadores explícitos de origem (`data_origin`/`is_real`).
- **Cache**: raw daily em `(lat,lon arredondadas, start, end, parâmetros)`, TTL configurável (`nasa_climate_cache_hours`, padrão 6 h), LRU 128; falhas **não** são cacheadas.
- **Frontend**: seção "Clima & Condições Agronômicas" integrada ao Dashboard de `index.html` (mesmo `screen-dash`, mesmos estilos), com presets de período, métricas com delta vs. referência histórica, indicadores calculados, interpretação conservadora, 2 gráficos diários (precipitação e temperatura, com linha de referência histórica) e rodapé de proveniência/confiança. Sem nova dependência (Chart.js existente).
- **Sem nova migration** (nada é persistido; o clima é derivado sob demanda).
- **Preparação PR #8/#9**: toda série diária usa datas ISO-8601 absolutas e `period` com `start/end`, permitindo junção temporal (não causal) com o calendário Sentinel-2 por data; o painel não faz correlação clima×espectro neste PR.

---

## Anexo — PR #7-FIX.1 (correção de cobertura, lacunas e confiabilidade)

> Data: 10/09/2026 · Motivo: playtest humano na Fazenda Orion (`-22.7182, -55.5421`)
> com presets 7d/15d/30d. Os comportamentos de 7d (`insufficient_data`) e 15d
> foram aprovados e **preservados**; 6 defeitos no cenário de 30 dias com
> precipitação ainda não consolidada (defasagem NRT da fonte) bloquearam o merge.
> Correção aplicada **na mesma branch/PR #7** — sem novo PR, sem merge.

### Causa raiz dos 6 defeitos

| # | Sintoma no playtest | Causa raiz |
|---|---|---|
| 1+6 | Painel mostrava simultaneamente "1% dos dias completos" e "cobertura de 70%" | Duas linhas do serviço formatavam a **mesma razão** de modos distintos: a mensagem parcial usava `f"{0.70:.0f}%"` → **"1%"** (razão tratada como inteiro), enquanto a leitura de qualidade usava `f"{0.70:.0%}"` → **"70%"** (correto). O conceito único e ambíguo de "cobertura" escondia que dois percentuais diferentes estavam sendo calculados (dias completos × cobertura da variável). |
| 2 | "Maior sequência seca: 17 dias" com dias faltando no meio | O valor era calculado apenas sobre os dias **conhecidos** (limite inferior) e apresentado como fato do período completo. Regra imposta: **dado ausente não é zero e não é "sem chuva"** — com lacunas, o indicador vira `null` + razão. |
| 3 | Agregações (acumulado, médias) pareciam representar o período inteiro | Falta de declaração de *sobre quantos dias* cada agregado foi calculado. Agora cada métrica declara `available_days`/`requested_days`/`complete` e a UI lê "25,0 mm (21/30 dias c/ dados)". |
| 4 | Desvio de chuva −91,4% (4,7 mm atual × 54,41 mm referência) | Comparação de período **parcial** contra baseline **completo** como se fossem equivalentes. Metodologia adotada (documentada em `baseline.methodology`): **A** comparar apenas as datas com observação atual (mesmas posições da janela) + **B** omitir o desvio se a cobertura da variável for < 50%. |
| 5 | Confiança única "média/limitada" para todas as métricas | A confiança era geral, não refletia a cobertura da variável usada por cada interpretação. Agora: confiança **por métrica** (100% → alta; 75–99,9% → média; <75% → limitada; −1 degrau se referência < 3 anos) + confiança **geral** (dias completos ≥98% e ≥3 anos → alta; 90–98% ou 1–2 anos → média; <90% ou 0 anos → limitada), cada interpretação declarando `confidence` + `confidence_basis`. |

### Definição formal de cobertura (campo `coverage` da API)

1. **Cobertura geral** — `overall_days`/`overall_pct`: dias com o conjunto
   **mínimo** para a análise geral = `T2M` + `PRECTOTCORR` (definição declarada
   em `overall_definition`). Se `overall_days == 0` → `insufficient_data`.
2. **Cobertura por variável** — `by_metric{precipitation, temperature,
   humidity, radiation, wind}`: `available_days` + `pct` de cada variável
   individual.
3. **Dias completos** — `complete_days`/`complete_days_pct`: dias com **todas**
   as 7 variáveis simultâneas. `complete_days < total` → `partial` (com
   mensagem de contagem explícita "N de M dias … (pct%)").

Percentuais sempre são **razão × 100** arredondados (nunca `.0f` sobre razão).

### Mudanças de API (payload)

- `coverage` (novo, top-level) — os três conceitos acima.
- `metrics[*]` — ganham `available_days`, `requested_days`, `complete`;
  precipitação ganha `accumulated_over_days`.
- `metrics[<m>].baseline` — bloco por métrica: `compared`, `compared_days`,
  `total_days`, `current_value`, `reference_value`, `deviation`,
  `deviation_pct`, `classification`, `classification_label`, `reference_years`,
  `reason` (ometido → `compared=false` + `reason`).
- `metrics.precipitation.longest_dry_streak_days` — `null` +
  `longest_dry_streak_reason` quando há lacunas de precipitação.
- `indicators` — ganham `over_days`/`total_days`, `reason` (cálculo omitido) e
  `compared_days` (desvios).
- `interpretations` — cada uma com `confidence` + `confidence_basis`
  (cobertura da variável usada + anos de referência); nova interpretação
  "Qualidade dos dados" no estado parcial.
- `confidence` — `scope`, `level`, `complete_days_pct`, `valid_baseline_years`,
  `by_metric`, `criteria`, `reasons`.
- `data_quality` — `complete_days(_pct)`, `by_metric_days`,
  `missing_days(_total)`, `omitted_calculations`.
- `status` — `insufficient_data` (sem conjunto mínimo), `partial` (qualquer
  dia incompleto), `ok`.

### O que NÃO mudou (preserva aprovado no playtest)

- 7d sem dados consolidados → `insufficient_data` explícito, **sem fallback**
  e sem dado inventado;
- 15d completo → `ok` com baseline;
- defasagem NRT: NULL continua NULL (dias recentes ausentes nunca viram
  0 mm / 0 °C / 0%);
- localização canônica (PR #6), Sentinel/DEM/3D, auth/ownership, PDF,
  endpoint legado, ausência de ET (sem metodologia inventada).

### Testes (FIX.1)

`tests/test_climate_service.py` (45 → 82) e
`tests/test_frontend_climate_panel.py` (9 → 13) cobrem os 20 cenários
obrigatórios: os três conceitos de cobertura; regressão ratio × percentage
(21/30 → "21 de 30 … 70%", nunca "1%"); cenário 21/30 ponta a ponta;
precipitação com lacuna (acumulado "nos N dias"); sequência seca interrompida
por NULL (unit + relatório + interpretação); baseline parcial (A: mesmas
datas; B: omissão < 50%); confiança por métrica × geral e `confidence_basis`;
mensagem parcial explícita; nenhuma imputação (NULL nunca vira 0); 7d
insufficient / 15d ok / 30d parcial; e as regressões pré-existentes.
Suíte completa: **424 testes (423 passando + 1 skip** por dataset Sentinel-2
fora do git).
