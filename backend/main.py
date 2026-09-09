import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

import models
import schemas
from config import settings
from database import Base, SessionLocal, engine, get_db
from security import create_access_token, get_current_user, get_auth_context, hash_password, require_role, verify_password
from services.analytics_service import get_farm_temporal_series
from services.copernicus_service import (
    CALENDAR_FALLBACK_SOURCE,
    CALENDAR_REAL_SOURCE,
    cdse_dir,
    fetch_real_calendar,
    process_farm_layer,
)
from services.dem_service import process_talhao_heightmap
from services.pdf_service import generate_farm_pdf_report
from services.satellite_service import generate_all_spectral_layers
from services.simulation_service import calculate_what_if_impact
from services.weather_service import fetch_live_nasa_weather
from services.geolocation_service import (GeometryError, distance_km, location_matches, parse_geometry, representative_point, reverse_geocode)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("orion.api")

# Montagem do diretório estático para texturas geradas dinamicamente
BASE_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
dynamic_path = os.path.join(BASE_PROJECT_DIR, "dynamic_talhoes")
os.makedirs(dynamic_path, exist_ok=True)

#: E-mail do administrador de demonstração (criado/associado pelo seed).
DEMO_ADMIN_EMAIL = "admin@orion.com"
#: Farm de demonstração (id 1) — legível por qualquer usuário autenticado.
DEMO_FARM_SHARED = True


# ---------------------------------------------------------------------------
# Ciclo de vida (lifespan moderno — substitui @app.on_event, deprecado)
# ---------------------------------------------------------------------------
def seed_demo_data(db: Session) -> None:
    """Popula a base com dados de demonstração se estiver vazia.

    PR #3 (ownership): o usuário admin demo é criado ANTES da fazenda demo, e
    esta fica vinculada a ele (owner_id) e marcada como compartilhada
    (is_shared=True) para funcionar como vitrine legível por qualquer usuário
    autenticado. O seed é idempotente: se o admin já existe, apenas garante o
    backfill de fazendas órfãs (owner_id nulo) e da flag de farm demo.
    """
    admin = db.query(models.User).filter(models.User.email == DEMO_ADMIN_EMAIL).first()
    if admin is None:
        admin = models.User(
            name="Admin Demo",
            email=DEMO_ADMIN_EMAIL,
            hashed_password=hash_password("123456"),
            role="admin",
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        logger.info("Seed de demonstração aplicado (admin@orion.com).")

    demo_farm = db.query(models.Farm).filter(models.Farm.id == 1).first()
    if demo_farm is None:
        demo_farm = models.Farm(
            name="Fazenda Orion",
            city="Apucarana - PR",
            total_area=42.54,
            latitude=-22.7182,
            longitude=-55.5421,
            owner_id=admin.id,
            is_shared=DEMO_FARM_SHARED,
        )
        db.add(demo_farm)

    # Backfill defensivo (idempotente): qualquer fazenda sem dono (banco
    # legado) passa a pertencer ao admin demo; a farm demo (id 1) é sempre
    # marcada como compartilhada.
    orphans = db.query(models.Farm).filter(models.Farm.owner_id.is_(None)).all()
    for farm in orphans:
        farm.owner_id = admin.id
    if demo_farm is not None:
        demo_farm.is_shared = DEMO_FARM_SHARED

    db.commit()
    logger.info("Backfill de ownership concluído (dono demo -> admin@orion.com).")


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

    # Migrações Alembic (PR #3): bancos novos já nascem com a FK via
    # Base.metadata.create_all; bancos legados recebem a mesma estrutura via
    # batch migration. Falhas de migration são fatais para não iniciar a API
    # com um schema divergente.
    _apply_migrations()

    with SessionLocal() as db:
        seed_demo_data(db)
    logger.info("Orion Agro API pronta.")
    yield
    logger.info("Orion Agro API finalizada.")


def _apply_migrations() -> None:
    """
    Aplica as migrações pendentes (idempotente) ao subir a aplicação.

    Executa `alembic upgrade head` no mesmo processo do boot. O Alembic é uma
    dependência obrigatória da aplicação: se o arquivo de configuração estiver
    ausente ou a migration falhar, interrompemos o boot em vez de mascarar o
    erro e iniciar com um schema sem a FK física.
    """
    ini = os.path.join(BACKEND_DIR, "alembic.ini")
    if not os.path.exists(ini):
        raise RuntimeError(f"Arquivo Alembic ausente: {ini}")

    from alembic import command
    from alembic.config import Config

    cfg = Config(ini)
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    command.upgrade(cfg, "head")
    logger.info("Migrações Alembic aplicadas (upgrade head).")


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

# PR #4 — o mount público /dynamic_talhoes foi REMOVIDO: os PNGs por fazenda
# são privados e só podem ser acessados pelas rotas autenticadas
# (`/api/talhao/{id}/texture.png` e `/api/talhao/{id}/heightmap.png`).
# O diretório continua sendo o cache físico usado pelos serviços.


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


def _sentinel_native_texture_path(layer: str, date: str) -> str:
    """
    Caminho físico de uma textura Sentinel-2 nativa de demonstração.

    Os datasets `sentinel-21KXQ-*` NÃO são versionados (ficam fora do Git /
    na máquina do desenvolvedor). Quando ausentes — como em qualquer clone
    limpo — o pipeline 3D usa o fallback explícito (textura procedural
    autenticada + `data_origin="procedural"`), nunca um asset 404 silencioso.
    """
    return os.path.join(
        BASE_PROJECT_DIR,
        f"sentinel-21KXQ-{date}",
        f"{layer}_cloudless_min_max.png",
    )


# ---------------------------------------------------------------------------
# Ownership / Privacidade (PR #3)
# ---------------------------------------------------------------------------
def _is_admin(user: models.User) -> bool:
    """Usuário tem papel admin (nível máximo da hierarquia de RBAC)."""
    return settings.role_hierarchy.get(user.role.strip().lower(), 1) >= settings.role_hierarchy.get("admin", 2)


def _can_access_farm(user: models.User, farm: models.Farm) -> bool:
    """
    Regra de LEITURA/uso de uma fazenda:

    - admin                → acesso global (qualquer fazenda);
    - dono da fazenda       → acesso total à própria fazenda;
    - fazenda compartilhada → qualquer usuário AUTENTICADO pode ler;
    - demais                → sem acesso.
    """
    if _is_admin(user):
        return True
    if farm.owner_id is not None and farm.owner_id == user.id:
        return True
    return bool(farm.is_shared)


def _can_write_farm(user: models.User, farm: models.Farm) -> bool:
    """
    Regra de ESCRITA (PUT/DELETE): apenas o DONO ou um ADMIN.

    Uma fazenda compartilhada (ex.: demo) é legível por qualquer usuário
    autenticado, mas NÃO editável/excluível por não-donos — isso evita que um
    usuário comum apague a fazenda de demonstração ou a de outro.
    """
    if _is_admin(user):
        return True
    return farm.owner_id is not None and farm.owner_id == user.id


def _require_farm_access(db: Session, farm_id: int, user: models.User) -> models.Farm:
    """
    Carrega a fazenda e aplica a regra de ownership (leitura).

    - Sem dono → 401 (autenticação precede autorização);
    - Com dono, mas sem permissão → 404 (evita expor a existência do recurso
      alheio — PR #3 prefere 404 consistente e seguro em vez de 403);
    - inexistente → 404.
    """
    farm = _get_farm_or_404(db, farm_id)
    if not _can_access_farm(user, farm):
        raise HTTPException(status_code=404, detail="Fazenda não encontrada.")
    return farm


def _require_farm_write(db: Session, farm_id: int, user: models.User) -> models.Farm:
    """
    Carrega a fazenda e aplica a regra de ownership (escrita: PUT/DELETE).

    - Sem dono → 401 (autenticação precede autorização);
    - Com dono, mas sem permissão (nem dono, nem admin, nem escrita) → 404
      (PR #3: não expõe a existência do recurso alheio);
    - inexistente → 404.
    """
    farm = _get_farm_or_404(db, farm_id)
    if not _can_write_farm(user, farm):
        raise HTTPException(status_code=404, detail="Fazenda não encontrada.")
    return farm


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


def _canonical_location(farm_in: schemas.FarmCreate, existing=None) -> dict:
    """Resolve localização uma única vez na gravação; geometria sempre vence legado."""
    geometry = farm_in.kml_coordinates
    old_lat = existing.latitude if existing else None
    old_lon = existing.longitude if existing else None
    if geometry:
        lat, lon = representative_point(geometry)
        source = "geometry"
    else:
        if farm_in.latitude is None or farm_in.longitude is None:
            raise HTTPException(422, "Selecione no mapa, use sua localização ou importe um talhão.")
        lat, lon, source = farm_in.latitude, farm_in.longitude, farm_in.location_source
    # Payloads da API antiga continuam aceitos e são explicitamente marcados;
    # não fingimos que o texto legado passou por reverse geocoding.
    geo = (reverse_geocode(lat, lon) if source != "legacy" else
           {"city": None, "state": None, "state_code": None, "status": "unverified"})
    detected_city, detected_state = geo.get("city"), geo.get("state_code") or geo.get("state")
    supplied_city = farm_in.city or (existing.city if existing else None)
    supplied_state = farm_in.state_code or farm_in.state or (existing.state_code if existing else None)
    mismatch = bool(detected_city and supplied_city and not location_matches(detected_city, detected_state, supplied_city.split(" - ")[0], supplied_state or (supplied_city.split(" - ")[-1] if " - " in supplied_city else None)))
    divergence = distance_km((old_lat, old_lon), (lat, lon)) if geometry and old_lat is not None and old_lon is not None else None
    status = geo["status"]
    if mismatch or (divergence is not None and divergence > 5): status = "legacy_divergence_corrected"
    city = detected_city or supplied_city or "Município não identificado"
    state = geo.get("state") or farm_in.state
    code = geo.get("state_code") or farm_in.state_code
    display = f"{city} - {code}" if code and code.lower() not in city.lower() else city
    return dict(latitude=lat, longitude=lon, city=display, state=state, state_code=code, location_source=source, location_status=status, location_divergence_km=divergence)


@app.post("/api/location/reverse", response_model=schemas.LocationResponse)
def reverse_location(req: schemas.ReverseGeocodeRequest, user: models.User = Depends(get_current_user)):
    return reverse_geocode(req.latitude, req.longitude)

@app.post("/api/location/from-geometry", response_model=schemas.LocationResponse)
def location_from_geometry(req: schemas.GeometryLocationRequest, user: models.User = Depends(get_current_user)):
    try:
        lat, lon = representative_point(req.geometry)
    except GeometryError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = reverse_geocode(lat, lon); result["source"] = "geometry"
    return result


# ---------------------------------------------------------------------------
# FAZENDAS & TALHÕES (CRUD COMPLETO)
#
# Matriz de acesso (PR #3 — ownership + privacidade multiusuário):
#   GET    /api/farms            → autenticado; usuário comum vê SÓ as próprias;
#                                   admin vê todas
#   GET    /api/farms/{id}       → autenticado; dono | admin | farm compartilhada
#   POST   /api/farms            → autenticado; cria vinculada ao usuário logado
#                                   (owner_id NUNCA vem do payload — derivado do token)
#   PUT    /api/farms/{id}       → dono da fazenda | admin (modificação estrutural)
#   DELETE /api/farms/{id}       → dono da fazenda | admin (operação destrutiva)
#   Anônimo                      → 401 em qualquer leitura de fazenda
# ---------------------------------------------------------------------------
@app.get("/api/farms", response_model=list[schemas.FarmResponse])
def get_farms(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    if _is_admin(user):
        return db.query(models.Farm).all()
    # Usuário comum: apenas as próprias fazendas (privacidade multiusuário).
    return db.query(models.Farm).filter(models.Farm.owner_id == user.id).all()


@app.get("/api/farms/{farm_id}", response_model=schemas.FarmResponse)
def get_farm_by_id(
    farm_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    return _require_farm_access(db, farm_id, user)


@app.post("/api/farms", response_model=schemas.FarmResponse, status_code=201)
def create_farm(
    farm_in: schemas.FarmCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    # O dono é SEMPRE o usuário autenticado. O payload não tem (nem poderia
    # ter) owner_id — falha-fechada contra escalada de ownership.
    try:
        location = _canonical_location(farm_in)
    except GeometryError as exc:
        raise HTTPException(422, str(exc)) from exc
    new_farm = models.Farm(
        name=farm_in.name, total_area=farm_in.total_area, owner_id=user.id,
        is_shared=False, **location
    )
    db.add(new_farm)
    db.commit()
    db.refresh(new_farm)

    new_talhao = models.Talhao(
        farm_id=new_farm.id,
        name=farm_in.talhao_name,
        area=farm_in.total_area,
        crop=farm_in.crop,
        latitude=new_farm.latitude,
        longitude=new_farm.longitude,
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
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    farm = _require_farm_write(db, farm_id, user)  # apenas dono | admin

    effective_geometry = farm_in.kml_coordinates or (farm.talhoes[0].kml_coordinates if farm.talhoes else None)
    effective_input = farm_in.model_copy(update={"kml_coordinates": effective_geometry})
    try:
        location = _canonical_location(effective_input, farm)
    except GeometryError as exc:
        raise HTTPException(422, str(exc)) from exc
    farm.name = farm_in.name
    farm.total_area = farm_in.total_area
    for key, value in location.items(): setattr(farm, key, value)

    if farm.talhoes:
        talhao = farm.talhoes[0]
        talhao.name = farm_in.talhao_name
        talhao.area = farm_in.total_area
        talhao.crop = farm_in.crop
        talhao.latitude = farm.latitude
        talhao.longitude = farm.longitude
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
def delete_farm(
    farm_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    farm = _require_farm_write(db, farm_id, user)  # apenas dono | admin
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
    date: str | None = Query(default=None, description="Passagem Sentinel-2 (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    # Ownership: só dono | admin | farm compartilhada (PR #3 — privacidade).
    farm = _require_farm_access(db, farm_id, user)
    request_date = date or (settings.sentinel_dates[0] if settings.sentinel_dates else "2025-04-07")

    # PR #4b — DADOS REAIS via CDSE (Sentinel-2 L2A) quando configurado.
    # A chamada NUNCA quebra: sem credenciais/sem cena/erro → status explícito
    # e o fluxo cai no fallback procedural (nunca tela branca).
    talhao = farm.talhoes[0] if farm.talhoes else None
    try:
        cdse_result = process_farm_layer(
            farm_id=farm.id,
            talhao_id=_resolve_talhao_id(farm),
            lat=farm.latitude,
            lon=farm.longitude,
            area_ha=farm.total_area,
            kml_coordinates=talhao.kml_coordinates if talhao else None,
            layer=layer,
            date_str=request_date,
        )
    except Exception:
        logger.exception("CDSE: falha inesperada no processamento")
        cdse_result = {
            "data_origin": "procedural",
            "real_data_status": "error",
            "real_data_message": "DADOS REAIS INDISPONÍVEIS (ERRO INESPERADO)",
            "layer": layer,
            "date": request_date,
        }

    if cdse_result.get("data_origin") == "sentinel" and cdse_result.get("texture_url"):
        # Proveniência + textura REAL (fase 13). `type` mantém compatibilidade.
        return {
            "type": "dynamic",
            "data_origin": "sentinel",
            "date": cdse_result.get("date", request_date),
            "texture_url": cdse_result["texture_url"],
            "collection": cdse_result.get("collection"),
            "product_id": cdse_result.get("product_id"),
            "acquisition_date": cdse_result.get("acquisition_date"),
            "cloud_cover": cdse_result.get("cloud_cover"),
            "processing_level": cdse_result.get("processing_level"),
            "bands": cdse_result.get("bands"),
            "valid_pixel_percentage": cdse_result.get("valid_pixel_percentage"),
            "selection_reason": cdse_result.get("selection_reason"),
            "index_formulas": cdse_result.get("index_formulas"),
            "stats": cdse_result.get("stats"),
            "real_data_status": cdse_result.get("real_data_status"),
        }

    if farm_id == 1:
        native_path = _sentinel_native_texture_path(layer, request_date)
        if os.path.exists(native_path):
            # Dataset Sentinel-2 nativo presente nesta máquina: é dado REAL.
            return {
                "type": "native",
                "data_origin": "sentinel",
                "date": request_date,
                "texture_url": (
                    f"sentinel-21KXQ-{request_date}/{layer}_cloudless_min_max.png"
                ),
                "path_pattern": (
                    f"sentinel-21KXQ-{{date}}/{layer}_cloudless_min_max.png"
                ),
            }
        # PR #4 — clone limpo não tem o dataset `sentinel-21KXQ-*`: nunca
        # devolver uma URL de asset inexistente (404 → malha branca). O fluxo
        # cai no MESMO pipeline das fazendas dinâmicas (textura procedural),
        # servida por rota autenticada, com `data_origin` explícito para o
        # frontend exibir "VISUALIZAÇÃO APROXIMADA".
        logger.warning(
            "Sentinel-2 nativo ausente para a farm demo (%s) — usando "
            "visualização procedural aproximada. Coloque o dataset em "
            "sentinel-21KXQ-* para voltar ao dado real.",
            native_path,
        )

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

    # URL autenticada (PR #3 — o PNG por fazenda é privado por usuário e
    # servido por rota com checagem de ownership, não via StaticFiles solto).
    return {
        "type": "dynamic",
        "data_origin": "procedural",
        "date": request_date,
        "texture_url": (
            f"{settings.public_base_url}/api/talhao/{farm.id}/texture.png?layer={layer}"
        ),
        **{
            k: v for k, v in cdse_result.items()
            if k.startswith("real_data_")
        },
    }


@app.get("/api/talhao/{farm_id}/texture.png")
def get_talhao_texture_png(
    farm_id: int,
    layer: schemas.SpectralLayer = "ndvi",
    date: str | None = Query(default=None, description="Data real da aquisição (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    """Streaming autenticado do PNG da textura espectral (PR #3 — privacidade)."""
    farm = _require_farm_access(db, farm_id, user)
    talhao_id = _resolve_talhao_id(farm)

    # PR #4b — textura REAL (Sentinel-2 L2A) cacheadada em disco pelo CDSE.
    if date:
        real_path = os.path.join(
            cdse_dir(farm.id, talhao_id, date), f"{layer}.png"
        )
        if os.path.exists(real_path):
            return StreamingResponse(open(real_path, "rb"), media_type="image/png")

    folder_name = f"farm_{farm.id}_talhao_{talhao_id}"
    file_path = os.path.join(dynamic_path, folder_name, f"{layer}_cloudless_min_max.png")
    if not os.path.exists(file_path):
        talhao = farm.talhoes[0] if farm.talhoes else None
        generate_all_spectral_layers(
            farm_id=farm.id,
            talhao_id=talhao_id,
            lat=farm.latitude,
            lon=farm.longitude,
            area_ha=farm.total_area,
            kml_coordinates=talhao.kml_coordinates if talhao else None,
        )
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Textura indisponível.")
    return StreamingResponse(open(file_path, "rb"), media_type="image/png")


# ---------------------------------------------------------------------------
# DATAS DISPONÍVEIS (fonte única: config.settings)
# ---------------------------------------------------------------------------
@app.get("/api/talhao/dates")
def get_available_dates():
    # Público/legado: APENAS calendário DEMONSTRATIVO (nunca apresentado como real).
    return {
        "dates": settings.sentinel_dates,
        "indices": settings.spectral_indices,
        "visual_layers": ["rgb", *settings.spectral_indices],
        "source": "config_fallback",
        "source_label": "Calendário demonstrativo (datas de demonstração)",
        "is_real": False,
    }


def calendar_fallback_reason(status: str | None, detail: dict | None = None) -> str:
    """Motivo em pt-BR (seguro — nunca segredos) de quando o calendário NÃO é real."""
    if status == "not_configured":
        return "CDSE não configurado (credenciais ausentes ou CDSE_ENABLED=false)"
    if status == "no_scene":
        return "Sem cena Sentinel-2 válida na janela (política de nuvens)"
    if status == "error":
        stage = (detail or {}).get("stage") or "cdse"
        return f"Erro ao consultar o catálogo Sentinel-2 ({stage})"
    return "Catálogo Sentinel-2 real indisponível"


@app.get("/api/talhao/{farm_id}/dates")
def get_farm_dates(
    farm_id: int,
    period_days: int | None = Query(
        default=None, ge=30, le=730,
        description="Janela em dias a partir de hoje (30/60/90/180/365/início-fim).",
    ),
    start: date | None = Query(default=None, description="Início da janela personalizada (YYYY-MM-DD)."),
    end: date | None = Query(default=None, description="Fim da janela personalizada (YYYY-MM-DD)."),
    limit: int = Query(default=12, ge=1, le=120, description="Máximo de cenas devolvidas."),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """
    Calendário temporal da fazenda: passagens REAIS Sentinel-2 (CDSE)
    quando configurado — mais recente primeiro, dentro da janela pedida
    (padrão 60 dias) — ou a lista oficial de datas da aplicação como
    fallback explícito. A consulta usa SOMENTE metadados STAC: nenhum
    asset é processado para montar o calendário.
    O campo `source` diferencia: "sentinel-cdse" | "config".
    """
    farm = _require_farm_access(db, farm_id, user)
    talhao = farm.talhoes[0] if farm.talhoes else None
    # Janela personalizada (start/end) tem precedência; senão, `period_days`
    # define o lookback a partir de hoje; sem nenhum dos dois, usa o padrão.
    eff_limit = limit
    if start or end:
        lookback_days = None
        eff_limit = max(limit, 60) if (start and end) else limit
    elif period_days:
        lookback_days = period_days
        eff_limit = max(limit, min(60, max(12, period_days // 5)))
    else:
        lookback_days = None
    try:
        calendar, status, detail = fetch_real_calendar(
            lat=farm.latitude,
            lon=farm.longitude,
            area_ha=farm.total_area,
            kml_coordinates=talhao.kml_coordinates if talhao else None,
            limit=eff_limit,
            lookback_days=lookback_days,
            start_date=start,
            end_date=end,
        )
    except Exception:
        logger.exception("CDSE: falha ao obter calendário real")
        calendar, status, detail = [], "error", None
    if calendar:
        logger.info(
            "[3D-DATES] source=%s count=%d latest=%s",
            CALENDAR_REAL_SOURCE, len(calendar), calendar[0]["date"],
        )
        return {
            "dates": [c["date"] for c in calendar],
            "calendar": calendar,
            "source": CALENDAR_REAL_SOURCE,
            "source_label": "Copernicus Sentinel-2 — catálogo STAC real",
            "is_real": True,
            "status": status,
            "stac_detail": detail,
            "indices": settings.spectral_indices,
            "visual_layers": ["rgb", *settings.spectral_indices],
            "period_days": period_days,
            "window_start": start.isoformat() if start else None,
            "window_end": end.isoformat() if end else None,
            "latest_date": calendar[0]["date"],
            "count": len(calendar),
        }
    # Fallback EXPLÍCITO: datas de demonstração — a UI deve rotulá-las assim.
    fallback_status = status or ("not_configured" if not calendar else "no_scene")
    fallback_reason = calendar_fallback_reason(fallback_status, detail)
    logger.warning(
        "[3D-DATES] source=%s reason=%s count=0",
        CALENDAR_FALLBACK_SOURCE, fallback_status,
    )
    return {
        "dates": sorted(settings.sentinel_dates, reverse=True),  # demo, desc p/ "Mais recente"
        "calendar": [],
        "source": CALENDAR_FALLBACK_SOURCE,
        "source_label": "Calendário demonstrativo (datas de demonstração)",
        "is_real": False,
        "status": fallback_status,
        "fallback_reason": fallback_reason,
        "stac_detail": detail,
        "indices": settings.spectral_indices,
        "visual_layers": ["rgb", *settings.spectral_indices],
        "period_days": period_days,
        "window_start": start.isoformat() if start else None,
        "window_end": end.isoformat() if end else None,
        "latest_date": None,
        "count": 0,
    }


# ---------------------------------------------------------------------------
# SÉRIE TEMPORAL, ZONEAMENTO E ESTIMATIVA DE SAFRAS
# ---------------------------------------------------------------------------
@app.get("/api/analytics/farm/{farm_id}")
def get_farm_analytics(
    farm_id: int,
    layer: schemas.AnalyticsLayer = "ndvi",
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    farm = _require_farm_access(db, farm_id, user)  # ownership (PR #3)
    talhao_id = _resolve_talhao_id(farm)  # ID real do talhão (fix multi-talhão)
    talhao = farm.talhoes[0] if farm.talhoes else None

    # PR #4b — calendário REAL (STAC) é anexado como diagnóstico; a série em
    # si continua na grade oficial, usando estatísticas REAIS quando cacheadas.
    real_calendar: list = []
    try:
        real_calendar, _, _ = fetch_real_calendar(
            lat=farm.latitude,
            lon=farm.longitude,
            area_ha=farm.total_area,
            kml_coordinates=talhao.kml_coordinates if talhao else None,
            limit=12,
        )
    except Exception:
        logger.exception("CDSE: falha ao obter calendário para analytics")

    return get_farm_temporal_series(
        farm_id=farm.id,
        layer=layer,
        total_area_ha=farm.total_area,
        talhao_id=talhao_id,
        real_calendar=real_calendar,
    )


# ---------------------------------------------------------------------------
# CLIMA AO VIVO (NASA POWER) — com cache TTL em weather_service
# ---------------------------------------------------------------------------
@app.get("/api/weather/farm/{farm_id}")
def get_farm_live_weather(
    farm_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    farm = _require_farm_access(db, farm_id, user)  # ownership (PR #3)
    return fetch_live_nasa_weather(farm.latitude, farm.longitude)


# ---------------------------------------------------------------------------
# TOPOGRAFIA — Copernicus DEM GL-30 (heightmap p/ Three.js)
# ---------------------------------------------------------------------------
@app.get("/api/talhao/{farm_id}/heightmap", response_model=schemas.HeightmapResponse)
def get_talhao_heightmap(
    farm_id: int,
    size: int = Query(default=256, ge=64, le=512, description="Lado do heightmap (potência de 2)"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    farm = _require_farm_access(db, farm_id, user)  # ownership (PR #3)
    talhao = farm.talhoes[0] if farm.talhoes else None
    hm = process_talhao_heightmap(
        farm_id=farm.id,
        talhao_id=_resolve_talhao_id(farm),
        lat=farm.latitude,
        lon=farm.longitude,
        area_ha=farm.total_area,
        kml_coordinates=talhao.kml_coordinates if talhao else None,
        size=size,
    )
    # URL autenticada (PR #3): o PNG do heightmap por fazenda é privado.
    if hm.get("heightmap_url"):
        hm["heightmap_url"] = (
            f"{settings.public_base_url}/api/talhao/{farm.id}/heightmap.png?size={size}"
        )
    return hm


@app.get("/api/talhao/{farm_id}/heightmap.png")
def get_talhao_heightmap_png(
    farm_id: int,
    size: int = Query(default=256, ge=64, le=512, description="Lado do heightmap (potência de 2)"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    """Streaming autenticado do PNG do heightmap (PR #3 — privacidade)."""
    farm = _require_farm_access(db, farm_id, user)
    talhao = farm.talhoes[0] if farm.talhoes else None
    hm = process_talhao_heightmap(
        farm_id=farm.id,
        talhao_id=_resolve_talhao_id(farm),
        lat=farm.latitude,
        lon=farm.longitude,
        area_ha=farm.total_area,
        kml_coordinates=talhao.kml_coordinates if talhao else None,
        size=size,
    )
    url = hm.get("heightmap_url")
    if not url:
        raise HTTPException(status_code=404, detail="Heightmap indisponível.")
    # Resolve o caminho físico a partir da BASE do projeto.
    # - forma legada /dynamic_talhoes/farm_X_talhao_Y/heightmap.png → usa o
    #   próprio caminho (fonte da verdade);
    # - forma autenticada /api/talhao/{id}/heightmap.png → reconstrói a partir
    #   do talhão real da fazenda (não confunda farm_id com talhao_id).
    if "/dynamic_talhoes/" in url:
        suffix = url.split("/dynamic_talhoes/", 1)[1]
        local = os.path.join(dynamic_path, suffix)
    else:
        talhao_id = _resolve_talhao_id(farm)
        local = os.path.join(
            dynamic_path, f"farm_{farm.id}_talhao_{talhao_id}", "heightmap.png"
        )
    if not os.path.exists(local):
        raise HTTPException(status_code=404, detail="Heightmap indisponível.")
    return StreamingResponse(open(local, "rb"), media_type="image/png")


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
    layer: schemas.AnalyticsLayer = "ndvi",
    date_index: int = Query(default=0, ge=0, le=len(settings.sentinel_dates) - 1),
    n_kg: float = Query(default=0.0, ge=-200, le=500),
    w_mm: float = Query(default=0.0, ge=-100, le=300),
    pest_pct: float = Query(default=0.0, ge=0, le=100),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),  # exige Bearer token
):
    farm = _require_farm_access(db, farm_id, user)  # ownership (PR #3)
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


# ---------------------------------------------------------------------------
# FRONTEND NA MESMA ORIGEM (PR #4 — clone limpo roda só com uvicorn)
#
# Permite abrir http://localhost:8000/ (ou o preview/deploy) sem um servidor
# estático separado nem `http://localhost:8000` hardcoded: o frontend usa URL
# relativa quando servido pelo próprio backend. As rotas `/api/*` têm
# precedência (registradas acima); este catch-all só atende o restante e
# serve APENAS o allowlist (páginas/JS do frontend + PNGs nativos
# `sentinel-21KXQ-*` quando o dataset está presente na máquina — nunca
# `.env`/diretórios internos do backend).
# ---------------------------------------------------------------------------
_FRONTEND_STATIC_FILES = {"index.html", "fazendas.html", "auth.html", "dashboard.html", "app.js"}
_FRONTEND_DEFAULT_PAGE = "index.html"
_FRONTEND_ROOT_REAL = os.path.realpath(BASE_PROJECT_DIR)


@app.get("/", include_in_schema=False)
def serve_frontend_root():
    return serve_frontend(_FRONTEND_DEFAULT_PAGE)


@app.get("/{frontend_path:path}", include_in_schema=False)
def serve_frontend(frontend_path: str):
    path = (frontend_path or _FRONTEND_DEFAULT_PAGE).lstrip("/")
    if not path or path.endswith("/"):
        path = f"{path}{_FRONTEND_DEFAULT_PAGE}"
    # APIs/Rotas de dados desconhecidas continuam sendo erro JSON (e não HTML).
    if path.startswith(("api/", "dynamic_talhoes/", "docs", "redoc", "openapi.json")):
        raise HTTPException(status_code=404, detail="Recurso não encontrado.")

    first_seg = path.split("/", 1)[0]
    is_frontend_file = path in _FRONTEND_STATIC_FILES
    is_sentinel_png = bool(first_seg.startswith("sentinel-21KXQ-")) and path.endswith(".png")
    if not (is_frontend_file or is_sentinel_png):
        raise HTTPException(status_code=404, detail="Recurso não encontrado.")

    candidate = os.path.join(BASE_PROJECT_DIR, path)
    real = os.path.realpath(candidate)
    inside_root = real == _FRONTEND_ROOT_REAL or real.startswith(_FRONTEND_ROOT_REAL + os.sep)
    if not inside_root or not os.path.isfile(real):
        raise HTTPException(status_code=404, detail="Recurso não encontrado.")

    if path.endswith(".png"):
        media = "image/png"
    elif path.endswith(".js"):
        media = "application/javascript"
    else:
        media = "text/html; charset=utf-8"
    return FileResponse(real, media_type=media)
