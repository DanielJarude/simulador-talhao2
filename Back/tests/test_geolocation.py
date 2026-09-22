import json
from unittest.mock import Mock, patch
import pytest
from services.geolocation_service import (GeometryError, distance_km, location_matches,
    parse_geometry, representative_point, reverse_geocode, validate_coordinates)

SQUARE=[[-23.01,-48.01],[-23.01,-47.99],[-22.99,-47.99],[-22.99,-48.01]]

def test_polygon_centroid_not_vertex_mean():
    lat,lon=representative_point(SQUARE); assert lat == pytest.approx(-23,abs=1e-5); assert lon == pytest.approx(-48,abs=1e-5)
def test_repeated_closing_point():
    assert representative_point(SQUARE+[SQUARE[0]]) == pytest.approx(representative_point(SQUARE))
def test_irregular_concave_point_is_inside_strategy():
    p=representative_point([[0,0],[0,4],[1,4],[1,1],[4,1],[4,0],[0,0]])
    assert 0 <= p[0] <= 4 and 0 <= p[1] <= 4

def test_multipolygon_area_weighted():
    geo={"type":"MultiPolygon","coordinates":[[[[-48.01,-23.01],[-47.99,-23.01],[-47.99,-22.99],[-48.01,-22.99],[-48.01,-23.01]]],[[[-47.1,-23.1],[-47.09,-23.1],[-47.09,-23.09],[-47.1,-23.09],[-47.1,-23.1]]]]}
    lat,lon=representative_point(geo); assert lon < -47.5
@pytest.mark.parametrize('lat,lon',[(91,0),(-91,0),(0,181),(0,-181)])
def test_invalid_coordinates(lat,lon):
    with pytest.raises(ValueError): validate_coordinates(lat,lon)
def test_normalization_city_state_accents(): assert location_matches('São José','São Paulo','sao jose','SP')
def test_distance_detects_divergence(): assert distance_km((-23,-48),(-22,-48)) > 100

def _response(payload):
    r=Mock(); r.raise_for_status.return_value=None; r.json.return_value=payload; return r
@patch('services.geolocation_service.requests.get')
def test_reverse_success(get):
    get.return_value=_response({'address':{'city':'Capão Bonito','state':'São Paulo','ISO3166-2-lvl4':'BR-SP','country':'Brasil','country_code':'br'}})
    out=reverse_geocode(-24.0,-48.3); assert out['city']=='Capão Bonito' and out['state_code']=='SP'
@patch('services.geolocation_service.requests.get',side_effect=__import__('requests').Timeout())
def test_reverse_timeout(get): assert reverse_geocode(-24.1,-48.4)['status']=='timeout'
@patch('services.geolocation_service.requests.get',side_effect=RuntimeError())
def test_reverse_unavailable(get): assert reverse_geocode(-24.2,-48.5)['status']=='unavailable'

def test_location_routes_require_auth(client):
    assert client.post('/api/location/reverse',json={'latitude':-23,'longitude':-48}).status_code == 401

def test_geometry_route_and_origin(client,admin_token):
    from conftest import auth
    fake={'latitude':-23,'longitude':-48,'city':'Cidade','state':'SP','state_code':'SP','country':'Brasil','country_code':'BR','source':'nominatim','confidence':None,'status':'ok'}
    with patch('main.reverse_geocode',return_value=fake):
        r=client.post('/api/location/from-geometry',json={'geometry':json.dumps(SQUARE)},headers=auth(admin_token))
    assert r.status_code==200 and r.json()['source']=='geometry'
