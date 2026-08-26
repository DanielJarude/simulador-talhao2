import os
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from database import engine, Base, get_db
import models, schemas
from services.simulation_service import calculate_what_if_impact
from services.weather_service import fetch_live_nasa_weather
from services.satellite_service import generate_all_spectral_layers
from services.analytics_service import get_farm_temporal_series, extract_layer_stats
from services.pdf_service import generate_farm_pdf_report

# Criação das tabelas no SQLite
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Orion Agro API",
    version="1.4.0",
    description="API de Suporte ao Simulador 3D, Agrometeorologia NASA POWER, Processamento Espectral Sentinel-2 e Emissão de Laudos em PDF"
)

# Configuração de CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Montagem do diretório estático para texturas geradas dinamicamente
BASE_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dynamic_path = os.path.join(BASE_PROJECT_DIR, "dynamic_talhoes")
os.makedirs(dynamic_path, exist_ok=True)
app.mount("/dynamic_talhoes", StaticFiles(directory=dynamic_path), name="dynamic_talhoes")


@app.on_event("startup")
def startup_populate_db():
    db = next(get_db())
    if not db.query(models.User).filter(models.User.email == "admin@orion.com").first():
        demo_user = models.User(
            name="Produtor Demo",
            email="admin@orion.com",
            hashed_password="123456",
            role="Engenheiro Agrônomo"
        )
        db.add(demo_user)
        
        demo_farm = models.Farm(
            name="Fazenda Orion",
            city="Apucarana - PR",
            total_area=42.54,
            latitude=-22.7182,
            longitude=-55.5421
        )
        db.add(demo_farm)
        db.commit()
        db.refresh(demo_farm)

        demo_talhao = models.Talhao(
            farm_id=demo_farm.id,
            name="Talhão 01",
            area=42.54,
            crop="Soja / Milho Safrinha",
            latitude=-22.7182,
            longitude=-55.5421
        )
        db.add(demo_talhao)
        db.commit()


# --- AUTENTICAÇÃO ---
@app.post("/api/auth/register", response_model=schemas.UserResponse)
def register(user_in: schemas.UserCreate, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.email == user_in.email).first():
        raise HTTPException(status_code=400, detail="E-mail já cadastrado.")
    new_user = models.User(
        name=user_in.name,
        email=user_in.email,
        hashed_password=user_in.password,
        role=user_in.role
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@app.post("/api/auth/login", response_model=schemas.UserResponse)
def login(login_in: schemas.UserLogin, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(
        models.User.email == login_in.email,
        models.User.hashed_password == login_in.password
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
    return user


# --- FAZENDAS & TALHÕES (CRUD COMPLETO) ---
@app.get("/api/farms", response_model=list[schemas.FarmResponse])
def get_farms(db: Session = Depends(get_db)):
    return db.query(models.Farm).all()


@app.get("/api/farms/{farm_id}", response_model=schemas.FarmResponse)
def get_farm_by_id(farm_id: int, db: Session = Depends(get_db)):
    farm = db.query(models.Farm).filter(models.Farm.id == farm_id).first()
    if not farm:
        raise HTTPException(status_code=404, detail="Fazenda não encontrada.")
    return farm


@app.post("/api/farms", response_model=schemas.FarmResponse)
def create_farm(farm_in: schemas.FarmCreate, db: Session = Depends(get_db)):
    new_farm = models.Farm(
        name=farm_in.name,
        city=farm_in.city,
        total_area=farm_in.total_area,
        latitude=farm_in.latitude,
        longitude=farm_in.longitude
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
        kml_coordinates=farm_in.kml_coordinates
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
        kml_coordinates=farm_in.kml_coordinates
    )

    return new_farm


@app.put("/api/farms/{farm_id}", response_model=schemas.FarmResponse)
def update_farm(farm_id: int, farm_in: schemas.FarmCreate, db: Session = Depends(get_db)):
    farm = db.query(models.Farm).filter(models.Farm.id == farm_id).first()
    if not farm:
        raise HTTPException(status_code=404, detail="Fazenda não encontrada.")
    
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

    talhao_id = farm.talhoes[0].id if farm.talhoes else 1
    kml_coords = farm.talhoes[0].kml_coordinates if farm.talhoes else farm_in.kml_coordinates

    generate_all_spectral_layers(
        farm_id=farm.id,
        talhao_id=talhao_id,
        lat=farm.latitude,
        lon=farm.longitude,
        area_ha=farm.total_area,
        kml_coordinates=kml_coords
    )

    return farm


@app.delete("/api/farms/{farm_id}")
def delete_farm(farm_id: int, db: Session = Depends(get_db)):
    farm = db.query(models.Farm).filter(models.Farm.id == farm_id).first()
    if not farm:
        raise HTTPException(status_code=404, detail="Fazenda não encontrada.")
    
    db.delete(farm)
    db.commit()
    return {"success": True, "message": f"Fazenda '{farm.name}' excluída com sucesso."}


# --- TEXTURA DINÂMICA DE TALHÃO MULTI-ÍNDICES (NDVI, EVI, NDRE, NDMI) ---
@app.get("/api/talhao/{farm_id}/texture")
def get_talhao_texture(farm_id: int, layer: str = "ndvi", db: Session = Depends(get_db)):
    farm = db.query(models.Farm).filter(models.Farm.id == farm_id).first()
    if not farm:
        raise HTTPException(status_code=404, detail="Fazenda não encontrada.")
    
    if farm_id == 1:
        return {
            "type": "native",
            "path_pattern": f"sentinel-21KXQ-{{date}}/{layer}_cloudless_min_max.png"
        }
    
    talhao = farm.talhoes[0] if farm.talhoes else None
    talhao_id = talhao.id if talhao else 1
    folder_name = f"farm_{farm.id}_talhao_{talhao_id}"
    expected_file = os.path.join(dynamic_path, folder_name, f"{layer}_cloudless_min_max.png")

    if not os.path.exists(expected_file):
        kml_coords = talhao.kml_coordinates if talhao else None
        generate_all_spectral_layers(
            farm_id=farm.id,
            talhao_id=talhao_id,
            lat=farm.latitude,
            lon=farm.longitude,
            area_ha=farm.total_area,
            kml_coordinates=kml_coords
        )
    
    return {
        "type": "dynamic",
        "texture_url": f"http://localhost:8000/dynamic_talhoes/{folder_name}/{layer}_cloudless_min_max.png"
    }


# --- DATAS DISPONÍVEIS ---
@app.get("/api/talhao/dates")
def get_available_dates():
    return {
        "dates": [
            "2025-04-07", "2025-04-22", "2025-05-02", "2025-06-11",
            "2025-10-04", "2025-10-11", "2025-11-18", "2025-11-20",
            "2025-12-10", "2025-12-18", "2026-01-27", "2026-02-11", "2026-03-08"
        ],
        "indices": ["ndvi", "evi", "ndre", "ndmi"]
    }


# --- SÉRIE TEMPORAL, ZONEAMENTO E ESTIMATIVA DE SAFRAS ---
@app.get("/api/analytics/farm/{farm_id}")
def get_farm_analytics(farm_id: int, layer: str = "ndvi", db: Session = Depends(get_db)):
    farm = db.query(models.Farm).filter(models.Farm.id == farm_id).first()
    if not farm:
        raise HTTPException(status_code=404, detail="Fazenda não encontrada.")
    
    return get_farm_temporal_series(farm_id=farm.id, layer=layer, total_area_ha=farm.total_area)


# --- CLIMA AO VIVO (NASA POWER) ---
@app.get("/api/weather/farm/{farm_id}")
def get_farm_live_weather(farm_id: int, db: Session = Depends(get_db)):
    farm = db.query(models.Farm).filter(models.Farm.id == farm_id).first()
    if not farm:
        raise HTTPException(status_code=404, detail="Fazenda não encontrada.")
    
    weather_data = fetch_live_nasa_weather(farm.latitude, farm.longitude)
    return weather_data


# --- SIMULAÇÃO WHAT-IF ---
@app.post("/api/simulation/what-if", response_model=schemas.WhatIfResponse)
def simulate_what_if(req: schemas.WhatIfRequest):
    return calculate_what_if_impact(
        n_kg=req.nitrogen_kg,
        w_mm=req.water_mm,
        pest_pct=req.pest_pressure_pct,
        area_ha=req.area_ha
    )


# --- DOWNLOAD DO LAUDO TÉCNICO EM PDF ---
@app.get("/api/reports/farm/{farm_id}/pdf")
def download_farm_report_pdf(
    farm_id: int, 
    layer: str = "ndvi", 
    date_index: int = 0,
    n_kg: float = 0.0,
    w_mm: float = 0.0,
    pest_pct: float = 0.0,
    db: Session = Depends(get_db)
):
    farm = db.query(models.Farm).filter(models.Farm.id == farm_id).first()
    if not farm:
        raise HTTPException(status_code=404, detail="Fazenda não encontrada.")

    talhao = farm.talhoes[0] if farm.talhoes else None
    crop_name = talhao.crop if talhao else "Soja / Milho Safrinha"

    # 1. Clima NASA POWER
    w_data = fetch_live_nasa_weather(farm.latitude, farm.longitude)

    # 2. Estatísticas temporais e estimativa de produtividade
    temporal_data = get_farm_temporal_series(farm_id=farm.id, layer=layer, total_area_ha=farm.total_area)
    timeline = temporal_data.get("timeline", [])
    yield_predictions = temporal_data.get("yield_predictions", {})

    selected_stats = timeline[min(date_index, len(timeline)-1)] if timeline else {
        "mean": 0.68,
        "zones": {
            "stress": {"pct": 10.0, "ha": round(farm.total_area * 0.1, 2)},
            "medium": {"pct": 25.0, "ha": round(farm.total_area * 0.25, 2)},
            "good": {"pct": 45.0, "ha": round(farm.total_area * 0.45, 2)},
            "dense": {"pct": 20.0, "ha": round(farm.total_area * 0.2, 2)}
        }
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
            "longitude": farm.longitude
        },
        weather_data=w_data,
        analytics_data={"current_zones": selected_stats.get("zones", {})},
        sim_data=sim_impact,
        yield_data=yield_predictions
    )

    filename = f"Laudo_Agronomico_{farm.name.replace(' ', '_')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )