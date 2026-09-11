# Referências bibliográficas das fórmulas — Orion

Este documento registra a origem científica e institucional das principais fórmulas usadas no projeto e separa referências publicadas de heurísticas internas do software.

## Índices espectrais

### NDVI

**Fórmula:** `(NIR - Red) / (NIR + Red)`  
**No Sentinel-2:** `(B08 - B04) / (B08 + B04)`

**Referência:** ROUSE, J. W.; HAAS, R. H.; SCHELL, J. A.; DEERING, D. W. *Monitoring vegetation systems in the Great Plains with ERTS*. Third Earth Resources Technology Satellite-1 Symposium. NASA SP-351, v. 1, p. 309–317, 1974. NASA Technical Reports Server, Document ID 19740022614.

### SAVI

**Fórmula:** `((NIR - Red) / (NIR + Red + L)) × (1 + L)`  
**No projeto:** `L = 0,5`.

**Referência:** HUETE, A. R. A soil-adjusted vegetation index (SAVI). *Remote Sensing of Environment*, v. 25, n. 3, p. 295–309, 1988. DOI: 10.1016/0034-4257(88)90106-X.

O valor `L = 0,5` é uma escolha convencional; não deve ser descrito como calibração universal.

### EVI

**Fórmula:** `2,5 × (NIR - Red) / (NIR + 6×Red - 7,5×Blue + 1)`  
**No Sentinel-2:** NIR=B08, Red=B04, Blue=B02.

**Referência:** HUETE, A.; DIDAN, K.; MIURA, T.; RODRIGUEZ, E. P.; GAO, X.; FERREIRA, L. G. Overview of the radiometric and biophysical performance of the MODIS vegetation indices. *Remote Sensing of Environment*, v. 83, n. 1–2, p. 195–213, 2002. DOI: 10.1016/S0034-4257(02)00096-2.

### NDWI para vegetação

**Fórmula:** `(NIR - SWIR) / (NIR + SWIR)`  
**Implementação atual:** `(B08 - B11) / (B08 + B11)`.

**Referência:** GAO, B.-C. NDWI—A normalized difference water index for remote sensing of vegetation liquid water from space. *Remote Sensing of Environment*, v. 58, n. 3, p. 257–266, 1996. DOI: 10.1016/S0034-4257(96)00067-3.

**Ressalva:** Gao (1996) utiliza aproximadamente 0,86 µm e 1,24 µm. B11 do Sentinel-2 está aproximadamente em 1,61 µm. Portanto, a implementação B08/B11 deve ser documentada como adaptação NIR–SWIR ao Sentinel-2, e não como reprodução espectral exata do artigo original.

### GNDVI / índice normalizado com banda verde

**Fórmula:** `(NIR - Green) / (NIR + Green)`  
**No Sentinel-2:** `(B08 - B03) / (B08 + B03)`.

**Referência:** GITELSON, A. A.; KAUFMAN, Y. J.; MERZLYAK, M. N. Use of a green channel in remote sensing of global vegetation from EOS-MODIS. *Remote Sensing of Environment*, v. 58, n. 3, p. 289–298, 1996. DOI: 10.1016/S0034-4257(96)00072-7.

### NDMI / NIR–SWIR

**Implementação atual:** `(B08 - B11) / (B08 + B11)`.

**Referência relacionada:** HARDISKY, M. A.; KLEMAS, V.; SMART, R. M. The influence of soil salinity, growth form, and leaf moisture on the spectral radiance of *Spartina alterniflora* canopies. *Photogrammetric Engineering & Remote Sensing*, v. 49, n. 1, p. 77–83, 1983.

A nomenclatura de índices NIR–SWIR varia na literatura. No código atual, NDMI e NDWI (Gao) usam exatamente B08/B11; portanto, são matematicamente redundantes. A nomenclatura deve ser revisada antes de tratar os dois como indicadores independentes.

## Sentinel-2: bandas, resolução e Level-2A

**Referências institucionais:**

- EUROPEAN SPACE AGENCY (ESA). *Sentinel-2 User Handbook*. Issue 1, Revision 2, 24 jul. 2015.
- EUROPEAN SPACE AGENCY (ESA). *Sentinel-2 Level-2A Algorithm Theoretical Basis Document (ATBD)*. Ref. S2-PDGS-MPC-ATBD-L2A, Issue 2.10, 15 nov. 2021.
- Copernicus Data Space Ecosystem / Sentinel Hub. Documentação do produto Sentinel-2 L2A.

No MSI, B02/B03/B04/B08 têm resolução espacial nativa de 10 m; B05/B11, 20 m. Combinações que misturam essas bandas exigem uma grade comum/reamostragem no processamento.

## Dados climáticos

O projeto usa dados diários da NASA POWER, comunidade AG, incluindo temperatura a 2 m, precipitação corrigida, umidade relativa, vento e radiação solar.

**Referência institucional:** NASA Langley Research Center. *POWER — Prediction Of Worldwide Energy Resources: Methodology, Meteorological Data, Data Sources and Daily API*.

As agregações matemáticas do projeto seguem operações transparentes: precipitação acumulada é a soma dos dias válidos; médias usam somente valores disponíveis; valores de preenchimento/dados ausentes não são convertidos silenciosamente em zero.

## Regras internas que não devem ser atribuídas a esses artigos

Os itens seguintes são heurísticas/regras de implementação do Orion e precisam de validação própria antes de serem apresentados como metodologia científica validada:

- limiar espacial `T = max(0,05, 2 × MAD)`, limitado a 0,10;
- exigência de pelo menos 3 cenas e 14 dias para tendência;
- thresholds Alta/Média/Limitada/Insuficiente para qualidade de cena;
- regra de persistência baseada em três cenas e duas transições;
- classificações e textos de atenção/interpretação;
- pesos RGB legados 0,25 / 0,48 / 0,68 / 0,88;
- fórmulas legadas de produtividade de soja e milho;
- preços fixos usados no faturamento legado.

As fórmulas legadas de produtividade não devem ser apresentadas como estimativas científicas enquanto não houver modelo calibrado e validado por cultura, região, estádio fenológico e dados de campo.

## Rastreabilidade no código

Na implementação do PR #8, os índices e as regras da nova camada estão principalmente em `backend/services/crop_health_service.py`. A camada climática está em `backend/services/climate_service.py`. Os cálculos legados/demonstrativos permanecem em `backend/services/analytics_service.py` e devem ser tratados separadamente da nova camada baseada em dados reais.
