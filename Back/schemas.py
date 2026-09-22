"""
Schemas Pydantic (contratos de entrada/saída da API).

Regras aplicadas nesta refatoração:
- `FarmCreate` existia DUPLICADO (a 1ª definição era silenciosamente
  sombreada pela 2ª) — agora há uma única definição.
- Validações com `Field` (faixas de latitude/longitude, áreas positivas,
  e-mail válido, senhas com tamanho mínimo, limites agronômicos).
- `WhatIfResponse` espelha EXATAMENTE os campos retornados por
  `simulation_service.calculate_what_if_impact` (corrige o HTTP 500 de
  `ResponseValidationError` no endpoint de simulação).
"""
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field

#: Camadas espectrais suportadas (validação em todos os endpoints)
#: "rgb" = true color real (Sentinel-2 B04/B03/B02) quando disponível.
SpectralLayer = Literal["rgb", "ndvi", "evi", "ndre", "ndmi"]

#: Camadas de ÍNDICE (analytics/estatísticas): RGB não tem zonal/estatística
#: de vigor, apenas visualização.
AnalyticsLayer = Literal["ndvi", "evi", "ndre", "ndmi"]


# ---------------------------------------------------------------- AUTENTICAÇÃO
class UserCreate(BaseModel):
    """
    Contrato de auto-serviço: o papel da conta NÃO faz parte do payload.

    Segurança (fail-closed): qualquer campo `role` enviado no corpo da
    requisição é IGNORADO pelo servidor (Pydantic descarta campos não
    declarados) e a conta recebe rigidamente o papel padrão público
    (`DEFAULT_PUBLIC_ROLE`, em `main.py`). Contas administrativas
    (`admin`) só existem via seed interno da aplicação — nunca via
    rota pública de cadastro.
    """

    name: str = Field(min_length=2, max_length=120, description="Nome do produtor")
    email: EmailStr
    password: str = Field(min_length=6, max_length=72, description="Mínimo 6 caracteres (bcrypt)")


class UserLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    role: str

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    """Resposta de login: token JWT estruturado + dados do usuário."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Vida útil do token em segundos")
    user: UserResponse


# ---------------------------------------------------------------- FAZENDAS
class TalhaoCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    area: float = Field(gt=0, le=1_000_000, description="Área em hectares")
    crop: str = Field(default="Soja / Milho Safrinha", min_length=2, max_length=80)
    latitude: float = Field(default=-22.7182, ge=-90, le=90)
    longitude: float = Field(default=-55.5421, ge=-180, le=180)
    kml_coordinates: Optional[str] = Field(default=None, max_length=200_000)


class TalhaoResponse(TalhaoCreate):
    id: int

    model_config = {"from_attributes": True}


class FarmCreate(BaseModel):
    """Definição única (a duplicata foi removida)."""

    name: str = Field(min_length=2, max_length=160)
    city: Optional[str] = Field(default=None, min_length=2, max_length=160)
    state: Optional[str] = Field(default=None, max_length=100)
    state_code: Optional[str] = Field(default=None, min_length=2, max_length=3)
    total_area: float = Field(gt=0, le=1_000_000, description="Área total em hectares")
    talhao_name: str = Field(min_length=1, max_length=160)
    crop: str = Field(min_length=2, max_length=80)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    location_source: Literal["geometry", "map", "device", "manual", "legacy"] = "legacy"
    kml_coordinates: Optional[str] = Field(default=None, max_length=200_000)


class FarmResponse(BaseModel):
    id: int
    name: str
    city: str
    state: Optional[str] = None
    state_code: Optional[str] = None
    location_source: str = "legacy"
    location_status: str = "unverified"
    location_divergence_km: Optional[float] = None
    total_area: float
    latitude: float
    longitude: float
    # Ownership (PR #3) — somente-leitura: o dono é SEMPRE derivado do
    # usuário autenticado no backend; o payload de criação o ignora.
    owner_id: Optional[int] = None
    is_shared: bool = False
    talhoes: list[TalhaoResponse] = []

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------- WHAT-IF
class WhatIfRequest(BaseModel):
    nitrogen_kg: float = Field(ge=-200, le=500, description="kg de N por hectare")
    water_mm: float = Field(ge=-100, le=300, description="Lâmina de irrigação em mm")
    pest_pressure_pct: float = Field(ge=0, le=100, description="Pressão de pragas em %")
    area_ha: float = Field(default=42.54, gt=0, le=100_000, description="Área em hectares")
    crop: str = Field(default="Soja / Milho Safrinha", min_length=2, max_length=80)


class WhatIfResponse(BaseModel):
    """Contrato exato do `simulation_service.calculate_what_if_impact`."""

    delta_ndvi: float
    delta_yield_sc_ha: float
    total_production_sc: float
    financial_impact_brl: float
    displacement_scale_3d: float
    gross_financial_gain_brl: float
    net_financial_gain_brl: float
    crop: str
    bag_price_brl: float


# ---------------------------------------------------------------- DEM (TOPOGRAFIA)
class HeightmapResponse(BaseModel):
    """
    Disponibilidade do heightmap topográfico (Copernicus DEM GL-30)
    para o talhão. `available=False` → o 3D usa o fallback de deslocamento.
    """

    available: bool
    farm_id: int
    talhao_id: Optional[int] = None
    size: Optional[int] = Field(default=None, ge=64, le=512)
    min_elevation_m: Optional[float] = None
    max_elevation_m: Optional[float] = None
    bounds: Optional[list[float]] = Field(
        default=None, description="[min_lon, min_lat, max_lon, max_lat]"
    )
    #: copernicus_30/90 = render remoto real via Process API CDSE;
    #: copernicus_gl30/local_geotiff = tile local; none = aproximado.
    source: Literal[
        "copernicus_30", "copernicus_90", "copernicus_gl30", "local_geotiff", "none"
    ] = "none"
    heightmap_url: Optional[str] = None
    reason: Optional[str] = None


class ReverseGeocodeRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

class GeometryLocationRequest(BaseModel):
    geometry: str = Field(min_length=2, max_length=200_000)

class LocationResponse(BaseModel):
    latitude: float
    longitude: float
    city: Optional[str] = None
    state: Optional[str] = None
    state_code: Optional[str] = None
    country: str = "Brasil"
    country_code: str = "BR"
    source: str
    confidence: Optional[float] = None
    status: str
    cached: bool = False
