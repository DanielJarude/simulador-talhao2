import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

import models
import schemas
from config import settings
from database import Base, SessionLocal, engine, get_db
from security import create_access_token, get_current_user, hash_password, require_role, verify_password
from services.analytics_service import get_farm_temporal_series
from services.dem_service import process_talhao_heightmap
from services.pdf_service import generate_farm_pdf_report
from services.satellite_service import generate_all_spectral_layers
from services.simulation_service import calculate_what_if_impact
from services.weather_service import fetch_live_nasa_weather

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("orion.api")

# Montagem do diretório estático para texturas geradas dinamicamente
BASE_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dynamic_path = os.path.join(BASE_PROJECT_DIR, "dynamic_talhoes")
os.makedirs(dynamic_path, exist_ok=True)


# ---------------------------------------------------------------------------
# Ciclo de vida (lifespan moderno — substitui @app.on_event, deprecado)
# ---------------------------------------------------------------------------
def seed_demo_data(db: Session) -> None:
    """Popula a base com dados de demonstração se estiver vazia."""
    if db.query(models.User).filter(models.User.email == "admin@orion.com").first():
        return

    demo_farm = models.Farm(
        name="Fazenda Orion",
        city="Apucarana - PR",
        total_area=42.54,
        latitude=-22.7182,
        longitude=-55.5421,
    )
    db.add(demo_farm)
    db.commit()
    db.refresh(demo_farm)

    db.add(
        models.Talhao(
            farm_id=demo_farm.id,
            name="Talhão 01",
            area=42.54,
            crop="Soja / Milho Safrinha",
            latitude=-22.7182,
            longitude=-55.5421,
        )
    )
    # Senha do demo SEMPRE hasheada (bcrypt) — nunca em texto puro.
    # O demo é o administrador da plataforma (role "admin" → RBAC liberado).
    db.add(
        models.User(
            name="Admin Demo",
            email="admin@orion.com",
            hashed_password=hash_password("123456"),
            role="admin",
        )
    )
    db.commit()
    logger.info("Seed de demonstração aplicado (admin@orion.com).")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.jwt_secret_key:
        # Segredo efêmero p/ desenvolvimento: tokens expiram a cada restart.
        # Em produção, defina JWT_SECRET_KEY no .env (obrigatório).
        settings.jwt_secret_key = secrets.token_urlsafe(48)
        logger.warning(
            "JWT_SECRET_KEY não definido — usando segredo efêmero "
            "(tokens serão invalidados a cada restart). Defina JWT_SECRET_KEY no .env."
        )
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_demo_data(db)
    logger.info("Orion Agro API pronta.")
    yield
    logger.info("Orion Agro API finalizada.")


app = FastAPI(
    title=settings.app_name,
    version="1.5.0",
    description=(
        "API de Suporte ao Simulador 3D, Agrometeorologia NASA POWER, "
        "Processamento Espectral Sentinel-2 e Emissão de Laudos em PDF"
    ),
    lifespan=lifespan,
)

# Configuração de CORS a partir do settings (origens explícitas — nunca "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/dynamic_talhoes", StaticFiles(directory=dynamic_path), name="dynamic_talhoes")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_farm_or_404(db: Session, farm_id: int) -> models.Farm:
    farm = db.get(models.Farm, farm_id)
    if not farm:
        raise HTTPException(status_code=404, detail="Fazenda não encontrada.")
    return farm


def _resolve_talhao_id(farm: models.Farm) -> int:
    """ID real do 1º talhão da fazenda (corrige o bug multi-talhão)."""
    return farm.talhoes[0].id if farm.talhoes else farm.id


# ---------------------------------------------------------------------------
# AUTENTICAÇÃO
# ---------------------------------------------------------------------------
#: Papel fixo de contas criadas via auto-serviço (fail-closed).
#: O payload de registro NÃO controla privilégios: `UserCreate` nem
#: declara o campo `role`, e o servidor atribui sempre este papel.
#: Contas "admin" existem apenas via seed interno (seed_demo_data).
DEFAULT_PUBLIC_ROLE = "Produtor Rural"


@app.post("/api/auth/register", response_model=schemas.UserResponse, status_code=201)
def register(user_in: schemas.UserCreate, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.email == user_in.email).first():
        raise HTTPException(status_code=400, detail="E-mail já cadastrado.")
    new_user = models.User(
        name=user_in.name,
        email=user_in.email,
        hashed_password=hash_password(user_in.password),  # bcrypt — nunca em texto puro
        role=DEFAULT_PUBLIC_ROLE,  # papel forçado: o payload não define privilégios
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@app.post("/api/auth/login", response_model=schemas.TokenResponse)
def login(login_in: schemas.UserLogin, db: Session = Depends(get_db)):
    """Valida credenciais (bcrypt) e emite o token JWT estruturado."""
    user = db.query(models.User).filter(models.User.email == login_in.email).first()
    if not user:
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
    if not verify_password(login_in.password, user.hashed_password):
        # Migração transparente de bases antigas com senha em texto puro:
        # se a senha digitada bater com o valor legado, rehasheia na hora.
        if user.hashed_password == login_in.password:
            logger.warning("Usuário %s com senha legado em texto puro — rehasheada.", user.email)
            user.hashed_password = hash_password(login_in.password)
            db.commit()
        else:
            raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
    token = create_access_token(user)
    return schemas.TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
        user=schemas.UserResponse.model_validate(user),
    )


# ---------------------------------------------------------------------------
# FAZENDAS & TALHÕES (CRUD COMPLETO)
#
# Matriz de acesso (RBAC):
#   GET (leitura)                  → pública
#   POST /api/farms (criação)      → autenticado (uso padrão)
#   PUT  /api/farms/{id}           → admin (modificação estrutural sensível:
#                                     reescreve geometria/área e regenera as
#                                     camadas espectrais do talhão)
#   DELETE /api/farms/{id}         → admin (operação destrutiva)
# ---------------------------------------------------------------------------
@app.get("/api/farms", response_model=list[schemas.FarmResponse])
def get_farms(db: Session = Depends(get_db)):
    return db.query(models.Farm).all()


@app.get("/api/farms/{farm_id}", response_model=schemas.FarmResponse)
def get_farm_by_id(farm_id: int, db: Session = Depends(get_db)):
    return _get_farm_or_404(db, farm_id)


@app.post("/api/farms", response_model=schemas.FarmResponse, status_code=201)
def create_farm(
    farm_in: schemas.FarmCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    new_farm = models.Farm(
        name=farm_in.name,
        city=farm_in.city,
        total_area=farm_in.total_area,
        latitude=farm_in.latitude,
        longitude=farm_in.longitude,
    )
    db.add(new_farm)
    db.commit()
    db.refresh(new_farm)

    new_talhao = models.Talhao(
        farm_id=new_farm.id,
        name=farm_in.talhao_name,
        area=farm_in.total_area,
        crop=farm_in.crop,
        latitude=farm_in.latitude,
        longitude=farm_in.longitude,
        kml_coordinates=farm_in.kml_coordinates,
    )
    db.add(new_talhao)
    db.commit()
    db.refresh(new_farm)

    generate_all_spectral_layers(
        farm_id=new_farm.id,
        talhao_id=new_talhao.id,
        lat=new_farm.latitude,
        lon=new_farm.longitude,
        area_ha=new_farm.total_area,
        kml_coordinates=farm_in.kml_coordinates,
    )
    return new_farm


@app.put("/api/farms/{farm_id}", response_model=schemas.FarmResponse)
def update_farm(
    farm_id: int,
    farm_in: schemas.FarmCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin")),  # modificação estrutural sensível
):
    farm = _get_farm_or_404(db, farm_id)

    farm.name = farm_in.name
    farm.city = farm_in.city
    farm.total_area = farm_in.total_area
    farm.latitude = farm_in.latitude
    farm.longitude = farm_in.longitude

    if farm.talhoes:
        talhao = farm.talhoes[0]
        talhao.name = farm_in.talhao_name
        talhao.area = farm_in.total_area
        talhao.crop = farm_in.crop
        talhao.latitude = farm_in.latitude
        talhao.longitude = farm_in.longitude
        if farm_in.kml_coordinates:
            talhao.kml_coordinates = farm_in.kml_coordinates

    db.commit()
    db.refresh(farm)

    talhao_id = _resolve_talhao_id(farm)
    kml_coords = farm.talhoes[0].kml_coordinates if farm.talhoes else farm_in.kml_coordinates

    generate_all_spectral_layers(
        farm_id=farm.id,
        talhao_id=talhao_id,
        lat=farm.latitude,
        lon=farm.longitude,
        area_ha=farm.total_area,
        kml_coordinates=kml_coords,
    )
    return farm


@app.delete("/api/farms/{farm_id}")
def delete_farm(farm_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_role("admin"))):
    farm = _get_farm_or_404(db, farm_id)
    name = farm.name
    db.delete(farm)
    db.commit()
    return {"success": True, "message": f"Fazenda '{name}' excluída com sucesso."}


# ---------------------------------------------------------------------------
# TEXTURA DINÂMICA DE TALHÃO MULTI-ÍNDICES (NDVI, EVI, NDRE, NDMI)
# ---------------------------------------------------------------------------
@app.get("/api/talhao/{farm_id}/texture")
def get_talhao_texture(
    farm_id: int,
    layer: schemas.SpectralLayer = "ndvi",
    db: Session = Depends(get_db),
):
    farm = _get_farm_or_404(db, farm_id)

    if farm_id == 1:
        return {
            "type": "native",
            "path_pattern": f"sentinel-21KXQ-{{date}}/{layer}_cloudless_min_max.png",
        }

    talhao_id = _resolve_talhao_id(farm)
    folder_name = f"farm_{farm.id}_talhao_{talhao_id}"
    expected_file = os.path.join(dynamic_path, folder_name, f"{layer}_cloudless_min_max.png")

    if not os.path.exists(expected_file):
        talhao = farm.talhoes[0] if farm.talhoes else None
        generate_all_spectral_layers(
            farm_id=farm.id,
            talhao_id=talhao_id,
            lat=farm.latitude,
            lon=farm.longitude,
            area_ha=farm.total_area,
            kml_coordinates=talhao.kml_coordinates if talhao else None,
        )

    # URL montada a partir da configuração (sem localhost hardcoded)
    return {
        "type": "dynamic",
        "texture_url": (
            f"{settings.public_base_url}/dynamic_talhoes/{folder_name}/{layer}_cloudless_min_max.png"
        ),
    }


# ---------------------------------------------------------------------------
# DATAS DISPONÍVEIS (fonte única: config.settings)
# ---------------------------------------------------------------------------
@app.get("/api/talhao/dates")
def get_available_dates():
    return {
        "dates": settings.sentinel_dates,
        "indices": settings.spectral_indices,
    }


# ---------------------------------------------------------------------------
# SÉRIE TEMPORAL, ZONEAMENTO E ESTIMATIVA DE SAFRAS
# ---------------------------------------------------------------------------
@app.get("/api/analytics/farm/{farm_id}")
def get_farm_analytics(
    farm_id: int,
    layer: schemas.SpectralLayer = "ndvi",
    db: Session = Depends(get_db),
):
    farm = _get_farm_or_404(db, farm_id)
    talhao_id = _resolve_talhao_id(farm)  # ID real do talhão (fix multi-talhão)
    return get_farm_temporal_series(
        farm_id=farm.id,
        layer=layer,
        total_area_ha=farm.total_area,
        talhao_id=talhao_id,
    )


# ---------------------------------------------------------------------------
# CLIMA AO VIVO (NASA POWER) — com cache TTL em weather_service
# ---------------------------------------------------------------------------
@app.get("/api/weather/farm/{farm_id}")
def get_farm_live_weather(farm_id: int, db: Session = Depends(get_db)):
    farm = _get_farm_or_404(db, farm_id)
    return fetch_live_nasa_weather(farm.latitude, farm.longitude)


# ---------------------------------------------------------------------------
# TOPOGRAFIA — Copernicus DEM GL-30 (heightmap p/ Three.js)
# ---------------------------------------------------------------------------
@app.get("/api/talhao/{farm_id}/heightmap", response_model=schemas.HeightmapResponse)
def get_talhao_heightmap(
    farm_id: int,
    size: int = Query(default=256, ge=64, le=512, description="Lado do heightmap (potência de 2)"),
    db: Session = Depends(get_db),
):
    farm = _get_farm_or_404(db, farm_id)
    talhao = farm.talhoes[0] if farm.talhoes else None
    return process_talhao_heightmap(
        farm_id=farm.id,
        talhao_id=_resolve_talhao_id(farm),
        lat=farm.latitude,
        lon=farm.longitude,
        area_ha=farm.total_area,
        kml_coordinates=talhao.kml_coordinates if talhao else None,
        size=size,
    )


# ---------------------------------------------------------------------------
# SIMULAÇÃO WHAT-IF (protegida)
# ---------------------------------------------------------------------------
@app.post("/api/simulation/what-if", response_model=schemas.WhatIfResponse)
def simulate_what_if(req: schemas.WhatIfRequest, user: models.User = Depends(get_current_user)):
    return calculate_what_if_impact(
        n_kg=req.nitrogen_kg,
        w_mm=req.water_mm,
        pest_pct=req.pest_pressure_pct,
        area_ha=req.area_ha,
        crop=req.crop,
    )


# ---------------------------------------------------------------------------
# DOWNLOAD DO LAUDO TÉCNICO EM PDF
# ---------------------------------------------------------------------------
@app.get("/api/reports/farm/{farm_id}/pdf")
def download_farm_report_pdf(
    farm_id: int,
    layer: schemas.SpectralLayer = "ndvi",
    date_index: int = Query(default=0, ge=0, le=len(settings.sentinel_dates) - 1),
    n_kg: float = Query(default=0.0, ge=-200, le=500),
    w_mm: float = Query(default=0.0, ge=-100, le=300),
    pest_pct: float = Query(default=0.0, ge=0, le=100),
    db: Session = Depends(get_db),
):
    farm = _get_farm_or_404(db, farm_id)
    talhao = farm.talhoes[0] if farm.talhoes else None
    crop_name = talhao.crop if talhao else "Soja / Milho Safrinha"

    # 1. Clima NASA POWER (cache TTL)
    w_data = fetch_live_nasa_weather(farm.latitude, farm.longitude)

    # 2. Estatísticas temporais e estimativa de produtividade
    temporal_data = get_farm_temporal_series(
        farm_id=farm.id,
        layer=layer,
        total_area_ha=farm.total_area,
        talhao_id=_resolve_talhao_id(farm),
    )
    timeline = temporal_data.get("timeline", [])
    yield_predictions = temporal_data.get("yield_predictions", {})

    selected_stats = timeline[min(date_index, len(timeline) - 1)] if timeline else {
        "mean": 0.68,
        "zones": {
            "stress": {"pct": 10.0, "ha": round(farm.total_area * 0.1, 2)},
            "medium": {"pct": 25.0, "ha": round(farm.total_area * 0.25, 2)},
            "good": {"pct": 45.0, "ha": round(farm.total_area * 0.45, 2)},
            "dense": {"pct": 20.0, "ha": round(farm.total_area * 0.2, 2)},
        },
    }

    # 3. Simulação What-If
    sim_impact = calculate_what_if_impact(n_kg, w_mm, pest_pct, farm.total_area, crop_name)
    sim_impact["n_kg"] = int(n_kg)
    sim_impact["w_mm"] = int(w_mm)
    sim_impact["pest_pct"] = int(pest_pct)

    pdf_bytes = generate_farm_pdf_report(
        farm_data={
            "name": farm.name,
            "city": farm.city,
            "total_area": farm.total_area,
            "crop": crop_name,
            "latitude": farm.latitude,
            "longitude": farm.longitude,
        },
        weather_data=w_data,
        analytics_data={"current_zones": selected_stats.get("zones", {})},
        sim_data=sim_impact,
        yield_data=yield_predictions,
    )

    filename = f"Laudo_Agronomico_{farm.name.replace(' ', '_')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
