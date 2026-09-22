# Auditoria — Saúde & Evolução da Lavoura (PR #8)

**Data da auditoria:** 11/09/2026  
**Base auditada:** `c16a809` — merge do PR #7, branch de trabalho do PR #8  
**Escopo:** Sentinel-2, STAC/CDSE, bandas, índices, máscaras, texturas, timeline, analytics, zoneamento, frontend, cache, fallbacks e testes.

## 1. Resumo executivo

O repositório já possui um pipeline real de Sentinel-2 L2A via Copernicus Data Space Ecosystem (CDSE), mas a camada de acompanhamento temporal existente não é suficiente para responder, com segurança, o que mudou no talhão:

- o pipeline CDSE processa recortes reais do talhão, aplica SCL/dataMask e calcula estatísticas numéricas;
- os índices reais atualmente disponíveis no pipeline são NDVI, EVI, NDRE e NDMI;
- o calendário STAC real é usado pela timeline 3D quando há credenciais e cenas válidas;
- o endpoint legado `/api/analytics/farm/{farm_id}` ainda mistura calendário configurado/demo, texturas procedurais e valores sintéticos quando não há raster, incluindo estimativa de produtividade;
- as zonas legadas são classificações do valor de uma cena, não zonas de mudança entre duas cenas;
- não existe comparação A/B espacial, persistência, anomalia interna, área de mudança ou integração temporal Sentinel × clima;
- PNGs de índices são representações visuais normalizadas por cena e não podem ser usados para calcular delta espectral.

O PR #8 deve, portanto, criar uma camada independente e explicitamente real para saúde/evolução. O analytics legado será preservado para evitar regressão, mas não será reutilizado como fonte de dados da nova análise quando contiver fallback ou classificação visual.

## 2. O que já existe

### 2.1 Backend e fonte Sentinel

| Componente | Local | Comportamento observado |
|---|---|---|
| Configuração CDSE | `backend/config.py` | OAuth2 client credentials, STAC, Process API, limite de nuvens, TTL, tamanho de raster e janela de busca. |
| Busca STAC | `backend/services/copernicus_service.py::stac_search` | Coleção `sentinel-2-l2a`, `intersects` com geometria real quando disponível, ou bbox derivada. |
| Calendário real | `fetch_real_calendar` e `/api/talhao/{farm_id}/dates` | Consulta apenas metadados STAC, ordena por aquisição, filtra `eo:cloud_cover` pelo limite configurado e identifica a fonte como `sentinel-cdse`. |
| Fallback do calendário | `/api/talhao/{farm_id}/dates` | Datas de `settings.sentinel_dates`, marcadas como `config_fallback`/demonstrativas. Não podem alimentar a nova análise real. |
| Process API | `_process_request` | Solicita somente o recorte do AOI, em TIFF multibanda, sem baixar o produto inteiro. |
| Bandas | `build_bands_evalscript` | B02, B03, B04, B05, B08, B11, SCL e dataMask. |
| Máscara | `build_valid_mask` | Exclui nodata, defeitos, pixels escuros, sombra de nuvem, nuvem média/alta, cirrus e neve; exige `dataMask > 0`. |
| Geometria | `kml_to_geojson_polygon`, `polygon_mask` | Usa o polígono real do talhão quando disponível; sem geometria, usa elipse aproximada explicitamente herdada do pipeline legado. |
| Cache | `_mem_cache`, `dynamic_talhoes/.../cdse` | TTL em memória, PNG/JSON de estatísticas e proveniência em disco, sem credenciais. |
| Privacidade | rotas `/api/talhao/...` | Rotas de textura, heightmap e analytics verificam JWT e ownership/shared/admin. |

### 2.2 Índices atualmente calculados

`index_array` calcula, sobre reflectância L2A, os seguintes índices reais:

- NDVI: B08 e B04;
- NDRE: B08 e B05;
- NDMI: B08 e B11;
- EVI: B08, B04 e B02.

A ordem do raster é `B02, B03, B04, B05, B08, B11, SCL, dataMask`. O Process API já obtém B03 e, portanto, permite GNDVI; B08/B04 permite SAVI; B08/B11 permite NDWI na variante Gao (1996), mas esses índices ainda não estão implementados nem expostos na API legada.

### 2.3 Estatísticas numéricas existentes

`render_layer_png` calcula, para pixels válidos dentro do polígono:

- média;
- mínimo;
- máximo;
- percentis P2 e P98;
- classificação de zonas por limiares do índice;
- pixels válidos, pixels do polígono e percentual válido.

A média/min/max e `valid_pixel_percentage` são derivados de reflectância e máscara real quando o fluxo CDSE é usado. A mediana não é calculada. A informação de cobertura válida é do raster do talhão, não uma estimativa aleatória.

### 2.4 O que é visual

As texturas PNG são uma visualização:

- os valores são normalizados individualmente entre P2 e P98 de cada cena;
- a paleta (`satellite_service.py`/`render_layer_png`) transforma o valor normalizado em cor;
- a imagem não preserva o valor espectral por pixel;
- não é cientificamente válido subtrair PNGs coloridos de datas diferentes para obter delta.

A textura procedural de `generate_all_spectral_layers` é demonstrativa e usa campos sintéticos; é marcada pelo pipeline como `data_origin="procedural"` quando não há dado real. Ela não pode alimentar a nova camada de Saúde & Evolução.

### 2.5 Analytics legado

`backend/services/analytics_service.py` e `/api/analytics/farm/{farm_id}`:

- usam `settings.sentinel_dates` como grade fixa;
- tentam ler estatísticas CDSE cacheadas;
- depois tentam PNG nativo/procedural;
- quando não encontram dados, retornam `mean=0.68` e zonas 10/25/45/20 com áreas sintéticas;
- calculam estimativa de produtividade por modelo empírico a partir de datas hardcoded.

Esse contrato é coberto por testes anteriores e deve permanecer para compatibilidade. Ele não é uma série temporal real completa e não será usado pelo PR #8 para declarar tendência, áreas de mudança ou saúde da lavoura.

### 2.6 Frontend existente

`index.html` já possui:

- timeline 3D reconstruída a partir do calendário da API;
- seleção de camadas RGB/NDVI/EVI/NDRE/NDMI;
- carregamento autenticado de texturas;
- gráfico legado “Curva Real da Safra” ligado ao endpoint de analytics;
- cartões legados de zoneamento da data escolhida;
- painel climático PR #7 com estados, proveniência, cobertura e linguagem conservadora;
- cache/preload 3D e separação Real × What-If.

Não há ainda:

- painel de Saúde & Evolução;
- série temporal real específica da análise;
- seleção de data A/B;
- camada delta;
- área em hectares de melhora/estabilidade/queda;
- persistência ou anomalia interna;
- estado explícito de dados insuficientes para essa análise.

## 3. Respostas obrigatórias da auditoria

### 3.1 Quais índices são reais?

NDVI, EVI, NDRE e NDMI são calculados sobre bandas reais do Process API quando `data_origin="sentinel"`, com máscara SCL/dataMask. O fato de um PNG ter sido gerado não é suficiente: a origem e a proveniência precisam ser verificadas.

### 3.2 Quais valores são derivados?

Média, mínimo, máximo, P2/P98, percentual de pixels válidos e percentuais/áreas de zonas são derivados dos pixels válidos. Áreas legadas são obtidas multiplicando a proporção de pixels pela área cadastrada. A futura análise de zonas de mudança usará a máscara do polígono e pixels pareados nas duas datas, informando também a área não observada.

### 3.3 Quais valores são apenas visuais?

As cores dos PNGs, as paletas e a normalização P2/P98 são somente visuais. O deslocamento 3D por textura/What-If também é visual/modelado e não é observação agronômica.

### 3.4 Quais valores são mockados/hardcoded?

- `settings.sentinel_dates`: datas demonstrativas quando o STAC não está configurado;
- fallback do analytics: média 0,68 e zonas 10/25/45/20;
- `estimate_seasonal_yield`: datas, fórmulas e preços empíricos fixos;
- `satellite_service.py`: rasters procedurais gerados a partir de latitude/longitude;
- dados fake do clima apenas nos testes (`tests/conftest.py`), não na nova análise.

Nenhum desses valores será apresentado como dado Sentinel real no PR #8.

### 3.5 Como o sistema calcula NDVI hoje?

Para cada pixel válido, `NDVI = (B08 - B04) / (B08 + B04 + epsilon)`, com `epsilon=1e-9` para evitar divisão por zero. São removidos pixels SCL inválidos, dataMask zero e valores não finitos. A estatística da cena é calculada sobre os pixels restantes dentro do polígono.

### 3.6 Como seleciona datas Sentinel?

O STAC busca a coleção `sentinel-2-l2a` na janela solicitada, com geometria real quando possível. O calendário filtra cobertura de nuvens conhecida até `CDSE_MAX_CLOUD_COVER`, ordena da mais recente para a mais antiga e retorna metadados de aquisição. Para uma data solicitada na textura, `select_best_scene` prioriza correspondência exata; essa decisão será usada na nova análise apenas para datas previamente confirmadas no calendário real.

### 3.7 Como lida com nuvens?

Há dois níveis:

1. `eo:cloud_cover` no item STAC filtra cenas do calendário;
2. SCL/dataMask removem pixels de nuvem, sombra, cirrus, neve, nodata e defeito no raster.

O sistema atual não produz uma qualidade combinada por cena nem distingue, no analytics legado, área sem observação. O PR #8 passará a retornar `cloud_cover`, `valid_pixel_pct`, qualidade objetiva e cobertura pareada da comparação.

### 3.8 Como calcula estatísticas da área?

No pipeline real, estatísticas são calculadas nos pixels válidos dentro da máscara do polígono rasterizado. No pipeline legado procedural, a classificação é derivada das cores do PNG sintético. A nova camada usará somente o primeiro caminho e preservará `null` quando não houver pixels válidos.

### 3.9 O que pode ser reutilizado?

- autenticação, ownership e `_require_farm_access`;
- resolução do talhão e geometria canônica;
- `fetch_real_calendar`, `SceneInfo` e seleção STAC;
- bandas, SCL/dataMask, máscara do polígono e Process API;
- fórmulas e proveniência do CDSE;
- cache de texturas/rasters já existente como base, sem usar PNG normalizado para delta;
- integração de frontend com `authHeaders`, `API_URL`, navegação e painel climático;
- testes de fórmulas, máscara, cenas, 3D e PR #7.

### 3.10 O que precisa ser corrigido ou isolado?

- separar o contrato real de Saúde & Evolução do analytics legado;
- não usar `settings.sentinel_dates` na nova série quando não vierem do STAC;
- implementar mediana/percentis e metadados de qualidade por aquisição;
- preservar arrays numéricos/máscaras para comparação espacial;
- criar delta A/B em valores de índice, nunca em PNGs;
- criar zonas calculadas por pixels pareados, com área observada e não observada;
- adicionar persistência e baseline interno, sem limiar universal de “bom/ruim”;
- integrar clima apenas como contexto temporal, nunca como causalidade;
- retornar explicitamente insuficiência e falha da fonte, sem preencher valores;
- evitar que o frontend rotule a série legada sintética como “real”.

## 4. Riscos científicos identificados

1. NDVI, NDRE, NDMI, SAVI, EVI, GNDVI e NDWI são sinais espectrais; nenhum diagnostica sozinho doença, praga, deficiência, compactação, irrigação necessária ou produtividade.
2. Variação absoluta é preferível a percentual para índices limitados; o PR #8 usará `delta = índice_B - índice_A`.
3. Uma única cena não permite tendência; cenas ruins ou com pouca cobertura não entram na classificação de tendência.
4. Nuvem residual, sombra, atmosfera, fenologia, geometria e diferença de aquisição podem alterar o índice.
5. NDWI possui variantes; a implementação deve chamar explicitamente a variante Gao (NIR–SWIR1), evitando confundi-la com NDWI de água superficial (Green–NIR).
6. SAVI depende do parâmetro `L`; será documentado `L=0,5` como compromisso para cobertura vegetal intermediária, não como calibração universal.
7. Área de zonas é uma estimativa raster da geometria cadastrada; a máscara real será usada e pixels sem observação serão separados, sem extrapolação.
8. Relação temporal com chuva/temperatura é contexto de coincidência; a ausência de causalidade será explicitada.

## 5. Riscos de regressão

- Alterar `settings.spectral_indices` ou os contratos antigos pode quebrar a timeline 3D e testes de schema.
- Alterar `index_array`, `build_valid_mask` ou Process API pode quebrar textura real, DEM e testes CDSE.
- Colocar chamadas de Saúde & Evolução no caminho de inicialização 3D pode aumentar latência e interferir no preload.
- Usar URLs de imagem sem Bearer pode violar ownership; o mapa de diferença será servido por rota autenticada e carregado via `fetch`/blob no frontend.
- Substituir o analytics legado pode quebrar PDF, What-If e testes de compatibilidade; a integração será aditiva.
- Falha da NASA POWER não pode impedir a análise espectral; o contexto climático será opcional.

## 6. Plano de implementação derivado da auditoria

1. Acrescentar fórmulas puras e suporte de bandas para SAVI, GNDVI e NDWI (variante Gao), sem alterar a semântica das texturas legadas.
2. Criar `crop_health_service.py` com contratos separados em `source_data`, `metrics` e `interpretation`.
3. Reutilizar STAC/Process API para adquirir cenas reais e manter arrays/máscaras numéricas em cache.
4. Criar timeline, qualidade, deltas, tendência conservadora, baseline interno e persistência.
5. Criar comparação A/B, mapa delta autenticado, zonas e áreas.
6. Criar contexto climático opcional entre duas aquisições, usando o serviço PR #7 sem afirmar causalidade.
7. Criar endpoints protegidos por ownership.
8. Integrar painel aditivo no Dashboard, sem tocar no fluxo de inicialização 3D.
9. Criar testes unitários, API, frontend/contratos e regressão da suíte existente.

## 7. Conclusão da auditoria

O repositório tem infraestrutura suficiente para implementar o PR #8 com dados reais, mas não possui ainda uma camada de evolução científica segura. A implementação deve distinguir rigorosamente dado de fonte, métrica derivada e interpretação. Em especial, nenhum fallback procedural, textura normalizada ou estimativa de produtividade do analytics legado deve aparecer como série Sentinel real.
