"""Contratos do frontend REAL de gestão (fazendas.html), não helpers do 3D."""
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
HTML=(ROOT/'fazendas.html').read_text(encoding='utf-8')
APP=(ROOT/'app.js').read_text(encoding='utf-8')

def form(fragment_id):
    pos=HTML.index(fragment_id); start=HTML.rfind('<form',0,pos); end=HTML.index('</form>',pos)
    return HTML[start:end]

def test_frontend_real_is_fazendas_html():
    assert 'Gestão de Propriedades' in HTML and '<script src="app.js"></script>' in HTML

def test_card_legacy_warning_and_no_coordinates():
    render=HTML[HTML.index('async function renderFarms'):HTML.index('// Modal de Edição')]
    assert '⚠ Localização não verificada' in render
    assert 'Lat: ${lat}' not in render and 'Lon: ${lon}' not in render
    assert 'Verificar localização' in render

def test_create_has_no_primary_city_or_coordinates():
    create=form('farm-name')
    assert '<label>Município / UF</label>' not in create
    assert 'Centroide Geográfico' not in create
    assert '<details><summary>Opções avançadas' in create
    assert '<input type="hidden" id="farm-city">' in create

def test_create_location_choices_are_visible():
    create=form('farm-name')
    assert 'Selecionar localização no mapa' in create
    assert 'Usar minha localização' in create
    assert 'Importar Arquivo do Talhão' in create
    assert 'LOCALIZAÇÃO DETECTADA' in create

def test_geolocation_only_after_user_action():
    call=HTML.index('navigator.geolocation.getCurrentPosition')
    handler=HTML.rfind('function useDeviceLocation',0,call)
    assert handler >= 0
    startup=HTML[HTML.index('<script>'):handler]
    assert 'getCurrentPosition(' not in startup

def test_kml_detection_requires_confirmation_and_area_is_automatic():
    assert "showDetection(context,g,'geometry')" in HTML
    assert 'polygonAreaHa(latLngs)' in HTML
    assert "document.getElementById('farm-area').value=area.toFixed(2)" in HTML
    assert "locationState[c].confirmed=false" in HTML
    assert 'commitDetection(c)' in HTML

def test_edit_legacy_presents_stored_and_detected_location():
    assert 'ATENÇÃO — LOCALIZAÇÃO NÃO VERIFICADA' in HTML
    assert 'Localização cadastrada:' in HTML
    assert 'Coordenadas armazenadas:' in HTML
    assert 'Localização detectada pelas coordenadas:' in HTML
    assert "reverseLocation('edit',farm.latitude,farm.longitude" in HTML

def test_map_click_is_candidate_not_persistence():
    click=HTML[HTML.index("maps[context].on('click'"):HTML.index('function setMarker')]
    assert 'locationState[context].candidate=e.latlng' in click
    assert 'FarmService.updateFarm' not in click
    confirm=HTML[HTML.index('async function confirmMapLocation'):HTML.index('function useDeviceLocation')]
    assert "reverseLocation(c,p.lat,p.lng,'map',true)" in confirm
    assert 'persistConfirmedEdit' in HTML

def test_nominatim_failure_is_explicit_and_preserves_existing():
    assert 'Não foi possível verificar a localização agora.' in HTML
    assert 'Os dados existentes foram preservados.' in HTML

def test_app_sends_location_origin_without_silent_demo_coordinates():
    assert 'location_source: locationSource' in APP
    assert 'parseFloat(lat) || -22.7182' not in APP
    assert 'parseFloat(lon) || -55.5421' not in APP
