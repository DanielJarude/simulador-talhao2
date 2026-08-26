from pydantic import BaseModel
from typing import List, Optional

class UserCreate(BaseModel):
    name: str
    email: str
    password: str
    role: str = "Produtor Rural"

class UserLogin(BaseModel):
    email: str
    password: str

class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    role: str
    class Config:
        from_attributes = True

class TalhaoCreate(BaseModel):
    name: str
    area: float
    crop: str = "Soja / Milho Safrinha"
    latitude: float = -22.7182
    longitude: float = -55.5421
    kml_coordinates: Optional[str] = None

class TalhaoResponse(TalhaoCreate):
    id: int
    class Config:
        from_attributes = True

class FarmCreate(BaseModel):
    name: str
    city: str
    total_area: float
    talhao_name: str
    crop: str
    latitude: float
    longitude: float

class FarmResponse(BaseModel):
    id: int
    name: str
    city: str
    total_area: float
    latitude: float
    longitude: float
    talhoes: List[TalhaoResponse] = []

    class Config:
        from_attributes = True

class WhatIfRequest(BaseModel):
    nitrogen_kg: float
    water_mm: float
    pest_pressure_pct: float
    area_ha: float = 42.54

class WhatIfResponse(BaseModel):
    delta_ndvi: float
    delta_yield_sc_ha: float
    total_production_sc: float
    financial_impact_brl: float
    displacement_scale_3d: float

class FarmCreate(BaseModel):
    name: str
    city: str
    total_area: float
    talhao_name: str
    crop: str
    latitude: float
    longitude: float
    kml_coordinates: Optional[str] = None