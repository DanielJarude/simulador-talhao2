# Auditoria geográfica — PR #6

## Estado anterior

- `farms.latitude/longitude` e `talhoes.latitude/longitude` guardavam pontos duplicados; ambos tinham o fallback silencioso `-22.7182/-55.5421`.
- Município e UF estavam concatenados em `farms.city`; não havia UF normalizada, origem, estado de validação ou divergência.
- POST/PUT exigiam cidade, latitude e longitude digitadas. O frontend ainda convertia zero/falha de parse nas coordenadas demo com `parseFloat(x) || fallback`.
- KML/GeoJSON era interpretado apenas no navegador. Somente o primeiro `coordinates`/primeiro Feature/anel era usado. O “centroide” era a média dos vértices (incluindo o ponto de fechamento repetido). Não havia validação geoespacial backend nem MultiPolygon.
- A área era informada no formulário; o backend não a derivava da geometria. DEM calcula bbox do JSON `[lat,lon]`; Sentinel/CDSE recebe geometria, centro e área; NASA POWER recebe o ponto da fazenda.
- O mapa 3D usava `farm.latitude/longitude`; cabeçalho, card e PDF usavam `farm.city`; Sentinel, DEM e NASA recebiam `farm.latitude/longitude` (DEM preservava bbox da geometria).
- A demo era criada somente se `id=1` não existisse, com `Apucarana - PR` e `-22.7182/-55.5421`; não tinha geometria criada pelo seed. Registros existentes nunca tinham a localização atualizada. `index.html` também conserva esses dados apenas como fallback visual explícito durante o carregamento.
- A seed fazia backfill seguro de ownership, mas não de metadados geográficos. Ausência/erro de geometria caía silenciosamente em elipse/bbox por área em serviços de demonstração.

## Causa

Cidade era um texto independente do ponto e do polígono. A UI calculava uma média de vértices e aceitava/forçava coordenadas demonstrativas. A seed não revisava a demo existente. Portanto mapa/serviços podiam apontar para um lugar enquanto o rótulo dizia outro.

## Solução

A fonte canônica é resolvida no backend em toda criação/edição: geometria válida tem prioridade absoluta; sem geometria, somente uma localização explicitamente confirmada (`map`, `device`, `manual` ou legado compatível). O ponto é o centroide de área em projeção equiretangular local, ponderado por área no MultiPolygon. Se cair fora de geometria côncava, é usado um ponto interior no segmento horizontal interno mais largo. Ponto de fechamento é removido e faixas são validadas.

O provider isolado é Nominatim/OpenStreetMap, configurável por ambiente, com User-Agent, timeout e cache em memória por coordenada arredondada/TTL. Timeout ou indisponibilidade não perde o ponto nem impede permanentemente o cadastro: o estado fica explícito. Município/UF retornados vencem texto legado; comparação remove acentos/caixa e normaliza siglas. Distância Haversine registra divergência superior a 5 km.

A migração `0002` adiciona `state`, `state_code`, `location_source`, `location_status` e `location_divergence_km`, sem apagar banco ou alterar ownership. Dados antigos ficam `legacy/unverified`; ao editar/confirmar, são reconciliados. Geometrias antigas são priorizadas na próxima edição mesmo que o payload não as substitua.

## Demo auditada

A demo versionada **não possui polígono**. Para o ponto efetivamente armazenado (`-22.7182, -55.5421`), consulta Nominatim realizada em 09/09/2026 retornou **Ponta Porã — MS** (Sanga Puitã/MS-386), e não Capão Bonito nem Apucarana. Por isso este PR não hardcoda “Capão Bonito”. A demo existente permanece marcada `legacy/unverified` até confirmação/reconciliação, preservando dados do usuário.
