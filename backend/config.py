"""
Configuração centralizada da API (padrão 12-factor).

Todos os valores podem ser sobrescritos por variáveis de ambiente
ou por um arquivo `.env` na pasta do backend (veja `.env.example`).
"""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Diretório do módulo de configuração (independe do CWD do processo).
_BACKEND_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # O `.env` é resolvido contra a PASTA DO MÓDULO (backend/.env) e,
        # como reforço, contra o CWD. Assim o servidor perde as credenciais
        # CDSE se for iniciado de `backend/` OU da raiz do repositório.
        # (Antes: `env_file=".env"` — relativo ao CWD → backend/.env era
        # ignorado quando o processo subia da raiz, caindo silenciosamente
        # no "not_configured" e no calendário fixo.)
        env_file=[_BACKEND_DIR / ".env", ".env"],
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

    # --- Reverse geocoding (Nominatim; configure outro endpoint compatível) ---
    geocoder_url: str = "https://nominatim.openstreetmap.org/reverse"
    geocoder_user_agent: str = "OrionAgro/1.6 (contato: admin@orionagro.local)"
    geocoder_timeout_s: float = Field(default=8.0, gt=0, le=60)
    geocoder_cache_hours: int = Field(default=168, ge=1, le=8760)

    # --- NASA POWER (clima) ---
    nasa_power_timeout_s: float = Field(default=15.0, gt=0, le=120)
    #: TTL do cache in-memory das respostas da NASA POWER.
    nasa_weather_cache_minutes: int = Field(default=60, ge=1, le=1440)

    # --- NASA POWER — serviço climático consolidado (PR #7) ---
    #: Base da API (fixa por padrão; sobrescrever p/ mirror local em testes/suporte).
    nasa_power_base_url: str = "https://power.larc.nasa.gov"
    #: Defasagem NRT da fonte (dias): os ~3 dias mais recentes podem vir
    #: incompletos; presets usam `hoje - NRT` como fim de período.
    nasa_power_nrt_lag_days: int = Field(default=3, ge=0, le=30)
    #: Máximo de dias por período de consulta (evita consultas pesadas).
    nasa_power_max_period_days: int = Field(default=366, ge=7, le=366)
    #: Início do catálogo Daily da NASA POWER.
    nasa_power_catalog_start: str = "1981-01-01"
    #: TTL (horas) do cache in-memory das séries diárias (PR #7).
    nasa_climate_cache_hours: int = Field(default=6, ge=0, le=168)
    #: Limite de entradas do cache raw (LRU por idade).
    nasa_climate_cache_max_entries: int = Field(default=128, ge=8, le=4096)
    #: Baseline histórico: quantos anos anteriores (mesma janela do calendário)
    #: entram na referência. Não é climatologia oficial (exige 30 anos/WMO).
    nasa_climate_baseline_years: int = Field(default=5, ge=1, le=10)
    #: Chuva "relevante" para dias de chuva/sequência seca (mm/dia).
    nasa_climate_rainy_day_threshold_mm: float = Field(default=1.0, ge=0.1, le=25.0)
    #: Faixa (±%) em torno da referência histórica para "próxima da referência".
    nasa_climate_baseline_band_pct: float = Field(default=20.0, ge=1.0, le=50.0)

    # --- Sentinel-2 (fonte única de dados para toda a stack) ---
    sentinel_dates: list[str] = [
        "2025-04-07", "2025-04-22", "2025-05-02", "2025-06-11",
        "2025-10-04", "2025-10-11", "2025-11-18", "2025-11-20",
        "2025-12-10", "2025-12-18", "2026-01-27", "2026-02-11", "2026-03-08",
    ]
    spectral_indices: list[str] = ["ndvi", "evi", "ndre", "ndmi"]

    # --- Copernicus Data Space Ecosystem (CDSE) — dados REAIS ---
    # PR #4 etapa 2: Sentinel-2 L2A real via CDSE (STAC + Process API).
    # Sem client_id/client_secret o serviço fica DESATIVADO e o sistema usa o
    # fallback procedural explícito — nunca quebra, nunca inventa dado real.
    # Endpoints confirmados na documentação oficial (documentation.dataspace.
    # copernicus.eu): token OAuth2 client_credentials, Catalog/STAC e Process.
    cdse_enabled: bool = True                   # desativa tudo se False
    cdse_client_id: str = ""                    # nunca versionar segredo
    cdse_client_secret: str = ""                # nunca versionar segredo
    cdse_token_url: str = (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
        "protocol/openid-connect/token"
    )
    cdse_stac_url: str = "https://stac.dataspace.copernicus.eu/v1"
    cdse_process_url: str = "https://sh.dataspace.copernicus.eu/process/v1"
    cdse_lookback_days: int = Field(default=60, ge=1, le=730)
    cdse_max_cloud_cover: float = Field(default=20.0, ge=0.0, le=100.0)
    cdse_timeout_s: float = Field(default=45.0, gt=0.0, le=300.0)
    cdse_cache_hours: int = Field(default=12, ge=0, le=720)
    cdse_retry_on_429: bool = True
    cdse_retry_backoff_s: float = Field(default=2.0, gt=0.0, le=30.0)
    cdse_raster_size: int = Field(default=256, ge=64, le=512)


settings = Settings()
