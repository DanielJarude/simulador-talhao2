"""
Configuração centralizada da API (padrão 12-factor).

Todos os valores podem ser sobrescritos por variáveis de ambiente
ou por um arquivo `.env` na pasta do backend (veja `.env.example`).
"""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- API ---
    app_name: str = "Orion Agro API"
    #: URL pública do servidor. Usada para montar URLs de texturas
    #: devolvidas à API (elimina o `http://localhost:8000` hardcoded).
    public_base_url: str = "http://localhost:8000"
    #: Origens permitidas no CORS. Nunca usar `["*"]` junto de
    #: `allow_credentials=True` — o navegador rejeita essa combinação.
    cors_origins: list[str] = [
        "http://localhost:5501",
        "http://127.0.0.1:5501",
        "http://localhost:8000",
    ]

    # --- Banco de dados ---
    database_url: str = "sqlite:///./agro_orion.db"

    # --- Segurança (JWT) ---
    #: Segredo do HS256. Em produção, OBRIGATÓRIO via .env (longo e aleatório).
    #: Se vazio, um segredo efêmero é gerado no boot (tokens expiram no restart).
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=720, ge=5, le=100_800)

    # --- RBAC (Controle de Acesso Baseado em Papéis) ---
    #: Níveis de papel (maior número = mais privilégio). O papel "admin"
    #: é o de nível máximo e tem acesso GLOBAL a rotas protegidas por
    #: papéis específicos; os demais papéis (padrão "user") são restritos.
    #: Papéis ausentes/desconhecidos caem no nível de "user" (fail-closed).
    #: Sobrescreva via env ROLE_HIERARCHY (JSON) para adicionar níveis.
    role_hierarchy: dict[str, int] = {
        "user": 1,
        "produtor rural": 1,
        "operador de máquinas": 1,
        "engenheiro agrônomo": 1,
        "admin": 2,
    }

    # --- Copernicus DEM GL-30 (topografia) ---
    #: Diretório (relativo à raiz do repositório) onde ficam as tiles GeoTIFF.
    dem_dir: str = "backend/data/dem"
    #: Tenta baixar automaticamente a tile SRTM que cobre a fazenda.
    dem_download_enabled: bool = True
    #: Templates de download; `{tile}` = nome no padrão Copernicus
    #: (ex.: Copernicus_Dem_GLO30_s23_w056). Ajuste ao layout do espelho.
    dem_download_urls: list[str] = [
        "https://opentopography.s3.sdsc.edu/raster/COP30/DGED_2023_1/{tile}.tif",
        "https://opentopography.s3.sdsc.edu/raster/COP30/{tile}.tif",
    ]
    dem_download_timeout_s: float = Field(default=30.0, gt=0, le=300)

    # --- NASA POWER (clima) ---
    nasa_power_timeout_s: float = Field(default=15.0, gt=0, le=120)
    #: TTL do cache in-memory das respostas da NASA POWER.
    nasa_weather_cache_minutes: int = Field(default=60, ge=1, le=1440)

    # --- Sentinel-2 (fonte única de dados para toda a stack) ---
    sentinel_dates: list[str] = [
        "2025-04-07", "2025-04-22", "2025-05-02", "2025-06-11",
        "2025-10-04", "2025-10-11", "2025-11-18", "2025-11-20",
        "2025-12-10", "2025-12-18", "2026-01-27", "2026-02-11", "2026-03-08",
    ]
    spectral_indices: list[str] = ["ndvi", "evi", "ndre", "ndmi"]


settings = Settings()
