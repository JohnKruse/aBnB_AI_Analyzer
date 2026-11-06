"""Station overlay rendering and real JavaScript lifecycle without network calls."""
import shutil
import subprocess
from pathlib import Path

import folium
import pytest

from src.ui import map_view


def test_selection_map_keeps_area_drawing_and_adds_station_layer(monkeypatch):
    captured = []
    def render(view, **kwargs):
        captured.append((view.get_root().render(), kwargs))
        return {'zoom': 13, 'all_drawings': []}
    monkeypatch.setattr(map_view, 'st_folium', render)
    result = map_view.render_area_map(center=(51.46, -.22), zoom=13, key='selection',
        enable_draw=True, saved_geometry={'type':'bbox','south':51.44,'west':-.24,'north':51.48,'east':-.19})
    html, options = captured[0]
    assert 'Train and Tube / Metro stations' in html
    assert 'setupBnbTransit(map_' in html
    assert 'overpass-api.de/api/interpreter' in html
    assert 'L.Control.Draw' in html
    boundary = options['feature_group_to_add']
    assert boundary.layer_name == 'Saved search area'
    assert any(isinstance(child, folium.GeoJson) for child in boundary._children.values())
    assert 'all_drawings' in options['returned_objects']
    assert result['zoom'] == 13


def test_station_overlay_javascript():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is unavailable')
    script = Path(__file__).with_name('transit_stations.cjs')
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True)


def test_london_station_data_is_embedded_with_readable_putney_names():
    data = map_view._saved_london_stations()
    names = {e['tags']['name']: e for e in data['elements']}
    assert names['Putney']['tags']['station'] == 'rail'
    assert names['East Putney']['tags']['station'] == 'subway'
    assert names['Putney Bridge']['tags']['station'] == 'subway'
    assert len(names) > 300
    view = folium.Map(location=[51.46, -.22])
    map_view._add_transit_stations(view)
    html = view.get_root().render()
    assert 'East Putney' in html and 'Putney Bridge' in html
    assert 'Saved London station data' in html


def test_results_map_shares_stations_and_keeps_listing_selection(monkeypatch):
    import pandas as pd
    captured = []
    def render(view, **kwargs):
        captured.append((view.get_root().render(), kwargs))
        return {'last_object_clicked_tooltip': 'BNBID::one · GBP 100'}
    monkeypatch.setattr(map_view, 'st_folium', render)
    result = map_view.render_listing_map(pd.DataFrame([{'room_id':'one', 'latitude':51.46,
        'longitude':-.22,'name':'Flat','verified_total_price':100}]),
        center=(51.46,-.22),zoom=8,selected_room_id='one',search_definition=None,
        overlay_1_df=pd.DataFrame([{'Station':'EastPutney','Latitude':51.4586,'Longitude':-.2112}]),key='results')
    html, options = captured[0]
    assert 'Train and Tube / Metro stations' in html
    assert 'East Putney' in html and 'Putney Bridge' in html
    assert 'setupBnbTransit(map_' in html
    assert 'last_object_clicked_tooltip' in options['returned_objects']
    assert map_view.room_id_from_tooltip(result['last_object_clicked_tooltip']) == 'one'
    assert html.count('L.circleMarker(') == 1  # No duplicate old CSV station marker.


def test_area_map_component_identity_survives_pan_zoom_and_boundary_updates(monkeypatch):
    from streamlit_folium import generate_js_hash, _get_map_string
    monkeypatch.setattr(map_view.st, 'session_state', {})
    captures = []
    def render(view, **kwargs):
        view.render()
        captures.append((generate_js_hash(_get_map_string(view), kwargs['key']), view.location,
                         view.options['zoom'], kwargs['feature_group_to_add']))
        return {'center': {'lat':51.47, 'lng':-.20}, 'zoom':16}
    monkeypatch.setattr(map_view, 'st_folium', render)
    map_view.render_area_map(center=(51.46,-.22),zoom=13,key='stable')
    map_view.render_area_map(center=(51.47,-.20),zoom=16,key='stable',saved_geometry={
        'type':'bbox','south':51.45,'west':-.23,'north':51.48,'east':-.18})
    assert captures[0][0] == captures[1][0], 'A pan, zoom or boundary lock must not remount the map'
    assert captures[1][1] == [51.46,-.22] and captures[1][2] == 13
    assert captures[1][3]._children, 'Boundary updates use the dynamic layer'
    map_view.render_area_map(center=(45.07,7.68),zoom=12,key='explicit-new-destination')
    assert captures[2][0] != captures[0][0]
    assert captures[2][1] == [45.07,7.68] and captures[2][2] == 12


def test_maps_support_fine_zoom_and_preserve_fractional_view_events():
    from src.ui.browser_app import _map_zoom
    view = map_view._add_base_map((51.46,-.22),13.25)
    assert view.options['zoom'] == 13.25
    assert view.options['zoomSnap'] == .25
    assert view.options['zoomDelta'] == .25
    assert view.options['wheelPxPerZoomLevel'] == 240
    assert _map_zoom({'zoom':13.75},13) == 13.75
    assert _map_zoom({},13.75) == 13.75
