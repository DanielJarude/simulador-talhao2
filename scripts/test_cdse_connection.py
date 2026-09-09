#!/usr/bin/env python3
"""
Conexão real com o Copernicus Data Space Ecosystem (CDSE) — OPCIONAL.

NÃO faz parte da suíte pytest (que NUNCA acessa a rede). Rode manualmente
apenas quando tiver credenciais válidas (e rede liberada para os hosts do
Copernicus) para validar o fluxo real ponta a ponta:

    python scripts/test_cdse_connection.py --lat -22.7182 --lon -55.5421 --area 42.54 --demo

Saídas distinguem claramente:
    STAC OK COM CENAS · STAC OK SEM CENAS · STAC ERRO HTTP · STAC ERRO PAYLOAD
    PROCESS API OK · PROCESS API HTTP 4xx/5xx · FALLBACK (motivo)

Segurança:
- NUNCA imprime client_secret/access_token/refresh_token;
- corpo de erro do provedor é truncado e sanitizado (nunca Authorization);
- as credenciais vêm do .env do backend (CDSE_CLIENT_ID/CDSE_CLIENT_SECRET);
- se não configurado, imprime status claro e sai com código 0 (não é erro de app).
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import date, timedelta

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


def _print_detail(title: str, err: dict | None, *, indent: str = "  ") -> None:
    """Imprime o diagnóstico SEGURO (status/endpoint/tipo/corpo truncado)."""
    if not err:
        print(f"{indent}(sem detalhe do provedor)")
        return
    print(f"{indent}ETAPA     : {err.get('stage') or '—'}")
    print(f"{indent}HTTP      : {err.get('http_status') or '—'}")
    print(f"{indent}ENDPOINT  : {err.get('endpoint') or '—'}")
    print(f"{indent}CONTENT-TYPE: {err.get('content_type') or '—'}")
    body = err.get("message") or ""
    print(f"{indent}MOTIVO    : {body[:600] if body else '—'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, default=-22.7182)
    parser.add_argument("--lon", type=float, default=-55.5421)
    parser.add_argument("--area", type=float, default=42.54)
    parser.add_argument("--kml", default=None, help="JSON [[lat,lon],...]")
    parser.add_argument("--date", default=None, help="Data alvo (YYYY-MM-DD); default = hoje")
    parser.add_argument("--layer", default="ndvi", choices=cds.SUPPORTED_LAYERS)
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

    # ------------------------------------------------------------------ 1/4
    print("\n[1/4] AUTENTICAÇÃO OAuth2 (client_credentials)…")
    try:
        token = cds._get_token()
        print(f"  OK — token obtido com {len(token)} caracteres (valor não exibido).")
    except cds.CopernicusError as exc:
        print(f"  FALHA — status={exc.status} endpoint={exc.endpoint} motivo={exc}")
        return 1

    # ------------------------------------------------------------------ 2/4
    print("\n[2/4] STAC sentinel-2-l2a (busca real de cenas)…")
    end = date.today()
    start = end - timedelta(days=cds.settings.cdse_lookback_days)
    print(f"  JANELA   : {start.isoformat()} → {end.isoformat()} "
          f"({cds.settings.cdse_lookback_days} dias)")
    bounds = cds.aoi_bounds(args.lat, args.lon, args.area, args.kml)
    print(f"  AOI bbox : [minLon={bounds[0]:.6f}, minLat={bounds[1]:.6f}, "
          f"maxLon={bounds[2]:.6f}, maxLat={bounds[3]:.6f}]")
    print(f"  GEOMETRIA: {'polígono KML (intersects)' if args.kml else 'bbox (sem KML)'}")

    calendar, status, detail = cds.fetch_real_calendar(
        args.lat, args.lon, args.area, args.kml, limit=12,
    )
    if status == "ok":
        print(f"  STAC OK COM CENAS — {len(calendar)} cena(s) útil(eis) "
              f"(<= {cds.settings.cdse_max_cloud_cover}%):")
        for c in calendar[:6]:
            print(f"    {c['date']}  nuvens={c['cloud_cover']}%  produto={c['product_id']}")
    elif status == "no_scene":
        print(f"  STAC OK SEM CENAS — resposta 200 válida, 0 itens na janela "
              f"({cds.settings.cdse_lookback_days} dias, cobertura <= "
              f"{cds.settings.cdse_max_cloud_cover}%). Não é erro remoto.")
    elif status == "not_configured":
        print("  STAC NÃO CONFIGURADO — credenciais ausentes.")
    else:
        print("  STAC ERRO — o provedor recusou a busca:")
        _print_detail("STAC", detail)
        if detail and detail.get("http_status") in (400, 422):
            print(f"  (provavelmente payload/geometria/datetime inválidos — "
                  f"verifique o MOTIVO acima)")

    # Contrato exato consumido pelo frontend (item 9 do playtest — sem segredos):
    # source real → timeline SÓ com estas datas; fallback → demo rotulada.
    src = cds.CALENDAR_REAL_SOURCE if status == "ok" else cds.CALENDAR_FALLBACK_SOURCE
    print("\n  [3D-DATES] CONTRATO DA TIMELINE (o que o frontend vai montar):")
    print(f"  [3D-DATES] source={src}")
    print(f"  [3D-DATES] count={len(calendar)}")
    print(f"  [3D-DATES] latest={calendar[0]['date'] if calendar else '-'}")
    print(f"  [3D-DATES] dates={','.join(c['date'] for c in calendar[:12])}")
    print(f"  [3D-DATES] lista_fixa_config_usada={'NAO' if status == 'ok' else 'SIM - apenas como fallback'}")

    # ------------------------------------------------------------------ 3/4
    print(f"\n[3/4] PROCESS API — camada {args.layer.upper()} real do talhão…")
    target = args.date or end.isoformat()
    print(f"  DATA ALVO: {target} (janela de busca = lookback a partir desta data)")
    result = cds.process_farm_layer(
        farm_id=99001, talhao_id=1,
        lat=args.lat, lon=args.lon, area_ha=args.area,
        kml_coordinates=args.kml, layer=args.layer, date_str=target,
    )
    if result.get("data_origin") == "sentinel":
        print("  PROCESS API OK — proveniência:")
        for key in ("collection", "product_id", "acquisition_date", "cloud_cover",
                    "processing_level", "valid_pixel_percentage", "selection_reason"):
            print(f"    {key}: {result.get(key)}")
        print(f"    bands: {result.get('bands')}")
        print(f"    stats: {json.dumps(result.get('stats'), ensure_ascii=False)}")
    else:
        status_txt = result.get("real_data_status")
        err = result.get("real_data_error")
        stage = (err or {}).get("stage") if isinstance(err, dict) else None
        if status_txt == "error" and stage == "PROCESS_API":
            print("  PROCESS API ERRO — o provedor recusou o processamento:")
            _print_detail("PROCESS", err)
        elif status_txt == "error":
            print(f"  FALLBACK (erro na etapa {stage or 'desconhecida'}): "
                  f"{result.get('real_data_message')}")
            _print_detail("FALHA", err)
        elif status_txt == "no_scene":
            print("  PROCESS API NÃO EXECUTADO — nenhuma cena Sentinel-2 na janela:")
            print(f"    {result.get('real_data_message')}")
        else:
            print(f"  FALLBACK ({status_txt}): {result.get('real_data_message')}")

    # ------------------------------------------------------------------ 4/4
    if args.demo:
        print("\n[4/4] DEM real (COPERNICUS_30 → COPERNICUS_90)…")
        home = cds.fetch_dem_png(bounds, 256, instance="COPERNICUS_30")
        if home:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                fh.write(home["png_bytes"])
                print(f"  DEM COPERNICUS_30 OK — {home['min_elevation_m']}–"
                      f"{home['max_elevation_m']} m (PNG {fh.name})")
        else:
            print("  DEM COPERNICUS_30 indisponível — tentando COPERNICUS_90…")
            fallback = cds.fetch_dem_png(bounds, 256, instance="COPERNICUS_90")
            if fallback:
                print(f"  DEM COPERNICUS_90 OK — {fallback['min_elevation_m']}–"
                      f"{fallback['max_elevation_m']} m")
            else:
                print("  DEM ambos indisponíveis — o 3D usará o fallback aproximado.")

    has_real = bool(calendar) or result.get("data_origin") == "sentinel"
    print("\nCONCLUSÃO: fluxo real validado" if has_real
          else "\nCONCLUSÃO: sem dado real nesta região/janela (motivo acima) — "
               "app segue no fallback explícito.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
