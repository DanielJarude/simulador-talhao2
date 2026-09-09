#!/usr/bin/env python3
"""
Conexão real com o Copernicus Data Space Ecosystem (CDSE) — OPCIONAL.

NÃO faz parte da suíte pytest (que NUNCA acessa a rede). Rode manualmente
apenas quando tiver credenciais válidas (e rede liberada para os hosts do
Copernicus) para validar o fluxo real ponta a ponta:

    python scripts/test_cdse_connection.py --lat -22.7182 --lon -55.5421 --area 42.54

Segurança:
- NUNCA imprime client_secret/access_token/refresh_token;
- as credenciais vêm do .env do backend (CDSE_CLIENT_ID/CDSE_CLIENT_SECRET);
- se não configurado, imprime status claro e sai com código 0 (não é erro de app).
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import date

RUN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RUN_DIR, "backend"))

# Carrega .env do backend ANTES de importar config (mesma mecânica do app).
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(os.path.join(RUN_DIR, "backend", ".env"))
except ImportError:
    pass

from services import copernicus_service as cds  # noqa: E402


def _mask(value: str) -> str:
    if not value:
        return "(vazio)"
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-2:]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, default=-22.7182)
    parser.add_argument("--lon", type=float, default=-55.5421)
    parser.add_argument("--area", type=float, default=42.54)
    parser.add_argument("--kml", default=None, help="JSON [[lat,lon],...]")
    parser.add_argument("--date", default=None, help="Data alvo (YYYY-MM-DD); default = hoje")
    parser.add_argument("--layer", default="ndvi",
                        choices=cds.SUPPORTED_LAYERS)
    parser.add_argument("--demo", action="store_true",
                        help="Rodar também o DEM COPERNICUS_30 → COPERNICUS_90")
    args = parser.parse_args()

    print("=" * 72)
    print("TESTE DE CONEXÃO — COPERNICUS DATA SPACE ECOSYSTEM (CDSE)")
    print("=" * 72)
    print(f"CDSE_ENABLED      : {cds.settings.cdse_enabled}")
    print(f"CDSE_CLIENT_ID    : {_mask(cds.settings.cdse_client_id)}")
    print(f"CDSE_CLIENT_SECRET: {_mask(cds.settings.cdse_client_secret)} (nunca exibido por extenso)")
    print(f"STAC URL          : {cds.settings.cdse_stac_url}")
    print(f"PROCESS URL       : {cds.settings.cdse_process_url}")
    print(f"TOKEN URL         : {cds.settings.cdse_token_url}")
    print(f"LOOKBACK          : {cds.settings.cdse_lookback_days} dias")
    print(f"MAX CLOUD COVER   : {cds.settings.cdse_max_cloud_cover}%")
    print("=" * 72)

    if not cds.is_configured():
        print("\n[STATUS] DADOS SATELITAIS REAIS NÃO CONFIGURADOS")
        print("Preencha CDSE_CLIENT_ID/CDSE_CLIENT_SECRET em backend/.env e tente de novo.")
        print("O aplicativo segue funcionando com o fallback procedural — sem quebrar.")
        return 0

    print("\n[1/4] Autenticação OAuth2 (client_credentials)…")
    token = cds._get_token()
    print(f"  OK — token obtido com {len(token)} caracteres (valor não exibido).")

    print("\n[2/4] Calendário real via STAC (sentinel-2-l2a)…")
    calendar, status = cds.fetch_real_calendar(args.lat, args.lon, args.area, args.kml)
    if status != "ok":
        print(f"  SEM CENAS: status={status}")
    else:
        print(f"  OK — {len(calendar)} cenas úteis (<= {cds.settings.cdse_max_cloud_cover}%):")
        for c in calendar[:6]:
            print(f"    {c['date']}  nuvens={c['cloud_cover']}%  produto={c['product_id']}")

    print(f"\n[3/4] Process API — camada {args.layer.upper()} real do talhão…")
    target = args.date or date.today().isoformat()
    result = cds.process_farm_layer(
        farm_id=99001, talhao_id=1,
        lat=args.lat, lon=args.lon, area_ha=args.area,
        kml_coordinates=args.kml, layer=args.layer, date_str=target,
    )
    if result.get("data_origin") == "sentinel":
        print(f"  OK — proveniência:")
        for key in ("collection", "product_id", "acquisition_date", "cloud_cover",
                    "processing_level", "valid_pixel_percentage", "selection_reason"):
            print(f"    {key}: {result.get(key)}")
        print(f"    bands: {result.get('bands')}")
        print(f"    stats: {json.dumps(result.get('stats'), ensure_ascii=False)}")
    else:
        print(f"  FALLBACK ({result.get('real_data_status')}): "
              f"{result.get('real_data_message')}")

    if args.demo:
        print("\n[4/4] DEM real (COPERNICUS_30 → COPERNICUS_90)…")
        bounds = cds.aoi_bounds(args.lat, args.lon, args.area, args.kml)
        home = cds.fetch_dem_png(bounds, 256, instance="COPERNICUS_30")
        if home:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                fh.write(home["png_bytes"])
                print(f"  OK COPERNICUS_30 — {home['min_elevation_m']}–{home['max_elevation_m']} m "
                      f"(PNG {fh.name})")
        else:
            print("  COPERNICUS_30 indisponível — tentando COPERNICUS_90…")
            fallback = cds.fetch_dem_png(bounds, 256, instance="COPERNICUS_90")
            if fallback:
                print(f"  OK COPERNICUS_90 — {fallback['min_elevation_m']}–"
                      f"{fallback['max_elevation_m']} m")
            else:
                print("  Ambos indisponíveis — o 3D usará o fallback aproximado.")

    print("\nCONCLUSÃO: fluxo real validado" if calendar or result.get("data_origin") == "sentinel"
          else "\nCONCLUSÃO: sem dado real nesta região/janela — app segue no fallback explícito.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
