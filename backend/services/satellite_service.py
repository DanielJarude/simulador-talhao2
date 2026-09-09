import os
import json
import numpy as np
from PIL import Image, ImageDraw

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def get_spectral_palette(layer_name: str, val: float):
    """
    Mapeamento de cor contínuo e agronômico por índice espectral.
    """
    v = max(0.0, min(1.0, float(val)))
    
    if layer_name == "ndvi":
        if v < 0.35:
            return [218, 54, 51, 255]   # Vermelho - Solo exposto / Estresse
        elif v < 0.55:
            return [217, 119, 6, 255]   # Amarelo/Laranja - Vigor Médio
        elif v < 0.75:
            return [46, 160, 67, 255]   # Verde - Bom Vigor
        else:
            return [31, 111, 235, 255]  # Azul - Alto Vigor

    elif layer_name == "evi":
        if v < 0.30:
            return [160, 82, 45, 255]   # Marrom Solo
        elif v < 0.60:
            return [154, 205, 50, 255]  # Verde Claro
        else:
            return [0, 100, 0, 255]     # Verde Escuro

    elif layer_name == "ndre":
        if v < 0.35:
            return [199, 21, 133, 255]  # Magenta / Déficit de N
        elif v < 0.65:
            return [255, 215, 0, 255]   # Amarelo Clorofila Média
        else:
            return [0, 201, 87, 255]    # Verde Esmeralda / Ótimo N

    elif layer_name == "ndmi":
        if v < 0.35:
            return [220, 20, 60, 255]   # Vermelho / Seca
        elif v < 0.65:
            return [0, 206, 209, 255]   # Ciano / Hidratação Média
        else:
            return [0, 0, 205, 255]     # Azul Real / Alta Água Foliar

    return [46, 160, 67, 255]


def generate_all_spectral_layers(farm_id: int, talhao_id: int, lat: float, lon: float, area_ha: float, kml_coordinates: str = None):
    folder_name = f"farm_{farm_id}_talhao_{talhao_id}"
    target_dir = os.path.join(BASE_DIR, "dynamic_talhoes", folder_name)
    os.makedirs(target_dir, exist_ok=True)

    size = 256
    mask_img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask_img)

    # 1. Rasterizar Polígono KML
    polygon_points = []
    if kml_coordinates:
        try:
            coords = json.loads(kml_coordinates) if isinstance(kml_coordinates, str) else kml_coordinates
            if coords and len(coords) >= 3:
                lats = [pt[0] for pt in coords]
                lons = [pt[1] for pt in coords]
                min_lat, max_lat = min(lats), max(lats)
                min_lon, max_lon = min(lons), max(lons)
                span_lat = max(max_lat - min_lat, 1e-6)
                span_lon = max(max_lon - min_lon, 1e-6)

                margin = 0.12 * size
                drawable_size = size - (2 * margin)

                pixel_poly = []
                for pt in coords:
                    p_lat, p_lon = pt[0], pt[1]
                    px = margin + ((p_lon - min_lon) / span_lon) * drawable_size
                    py = margin + ((max_lat - p_lat) / span_lat) * drawable_size
                    pixel_poly.append((px, py))

                draw.polygon(pixel_poly, fill=255)
                polygon_points = pixel_poly
        except Exception as e:
            print("Erro no recorte KML:", e)

    if not polygon_points:
        draw.ellipse([size * 0.15, size * 0.15, size * 0.85, size * 0.85], fill=255)

    mask = np.array(mask_img) > 128

    # 2. Gerar Campo Georreferenciado Específico para este Talhão
    lat_seed = int((abs(lat) * 1000000)) % 2147483647
    lon_seed = int((abs(lon) * 1000000)) % 2147483647
    np.random.seed(lat_seed ^ lon_seed)

    freq_x = 1.2 + (lat_seed % 4) * 0.6
    freq_y = 1.2 + (lon_seed % 4) * 0.6
    angle = ((lat_seed + lon_seed) % 180) * np.pi / 180.0

    x = np.linspace(-1.5, 1.5, size)
    y = np.linspace(-1.5, 1.5, size)
    xx, yy = np.meshgrid(x, y)

    xr = xx * np.cos(angle) - yy * np.sin(angle)
    yr = xx * np.sin(angle) + yy * np.cos(angle)

    spatial_variance = (
        0.5 * np.sin(xr * freq_x * 2.5) 
        + 0.3 * np.cos(yr * freq_y * 2.5) 
        + 0.2 * np.sin((xr + yr) * 1.8)
        + np.random.normal(0, 0.05, (size, size))
    )
    spatial_variance = (spatial_variance - spatial_variance.min()) / (spatial_variance.max() - spatial_variance.min() + 1e-6)

    # Bandas espectrais calibradas
    nir = 0.35 + (spatial_variance * 0.50)
    red = 0.08 + ((1.0 - spatial_variance) * 0.25)
    green = 0.09 + (spatial_variance * 0.18)
    blue = 0.05 + ((1.0 - spatial_variance) * 0.15)
    red_edge = 0.20 + (spatial_variance * 0.35)
    swir = 0.10 + ((1.0 - spatial_variance) * 0.28)

    eps = 1e-6
    indices = {
        "ndvi": (nir - red) / (nir + red + eps),
        "evi": 2.5 * (nir - red) / (nir + 6.0 * red - 7.5 * blue + 1.0 + eps),
        "ndre": (nir - red_edge) / (nir + red_edge + eps),
        "ndmi": (nir - swir) / (nir + swir + eps)
    }

    # 3. Exportar Texturas Coloridas
    for layer_name, mat in indices.items():
        mat_valid = mat[mask]
        min_v = np.percentile(mat_valid, 2) if len(mat_valid) > 0 else 0.0
        max_v = np.percentile(mat_valid, 98) if len(mat_valid) > 0 else 1.0
        mat_norm = (mat - min_v) / (max_v - min_v + eps)

        color_map = np.zeros((size, size, 4), dtype=np.uint8)
        for i in range(size):
            for j in range(size):
                if not mask[i, j]:
                    color_map[i, j] = [13, 17, 23, 255]
                else:
                    color_map[i, j] = get_spectral_palette(layer_name, mat_norm[i, j])

        img = Image.fromarray(color_map, 'RGBA')
        out_file = os.path.join(target_dir, f"{layer_name}_cloudless_min_max.png")
        img.save(out_file)

    # PR #4b — RGB true color (B04→R, B03→G, B02→B, escala ×2.5 — mesmo
    # padrão visual da documentação CDSE para Sentinel-2 L2A). É o fallback
    # PROCEDURAL quando não há dado real; nunca se apresenta como Sentinel.
    rgb_map = np.zeros((size, size, 4), dtype=np.uint8)
    rgb_map[..., 3] = 255
    rgb_map[..., 0] = np.clip(red * 2.5 * 255.0, 0, 255).astype(np.uint8)
    rgb_map[..., 1] = np.clip(green * 2.5 * 255.0, 0, 255).astype(np.uint8)
    rgb_map[..., 2] = np.clip(blue * 2.5 * 255.0, 0, 255).astype(np.uint8)
    rgb_map[~mask] = [13, 17, 23, 255]
    Image.fromarray(rgb_map, "RGBA").save(
        os.path.join(target_dir, "rgb_cloudless_min_max.png")
    )

    return target_dir