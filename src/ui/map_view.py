"""Folium maps used by the browser workflow."""

from __future__ import annotations

from html import escape
from pathlib import Path

from branca.element import MacroElement, Template
import math
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, unquote, urlencode

import folium
from folium.plugins import Draw, MarkerCluster
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from src.search_service import normalize_geometry, normalize_map_zoom
from src.transit_service import saved_london_stations as _saved_london_stations


DEFAULT_CENTER = (45.0703, 7.6869)  # Torino; editable, with no geocoder request.
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"
TILE_ATTR = 'Tiles © Esri, HERE, Garmin · OpenStreetMap contributors <a href="https://www.openstreetmap.org/copyright">https://www.openstreetmap.org/copyright</a>'


def _center_from_event(event: Dict[str, Any], fallback: Tuple[float, float]) -> Tuple[float, float]:
    center = event.get("center")
    if isinstance(center, dict):
        try:
            return float(center["lat"]), float(center["lng"])
        except (KeyError, TypeError, ValueError):
            pass
    if isinstance(center, (list, tuple)) and len(center) >= 2:
        try:
            return float(center[0]), float(center[1])
        except (TypeError, ValueError):
            pass
    return fallback


def _add_base_map(center: Tuple[float, float], zoom: float) -> folium.Map:
    map_view = folium.Map(location=center, zoom_start=normalize_map_zoom(zoom, fractional=True), tiles=None, control_scale=True,
                          zoom_snap=0.25, zoom_delta=0.25, wheel_px_per_zoom_level=240)
    folium.TileLayer(
        TILE_URL, attr=TILE_ATTR, subdomains="", detect_retina=False, max_zoom=19, max_native_zoom=16, name="Quiet street map",
    ).add_to(map_view)
    return map_view



def _add_transit_stations(map_view: folium.Map) -> None:
    """Load station markers for the visible area without rerunning Streamlit."""
    group = folium.FeatureGroup(name="Train and Tube / Metro stations", show=True)
    group.add_to(map_view)
    script = (Path(__file__).parent / "assets" / "transit_stations.js").read_text(encoding="utf-8")
    overlay = MacroElement()
    overlay._name = "TransitStations"
    overlay.map_name = map_view.get_name()
    overlay.group_name = group.get_name()
    overlay.saved_stations = _saved_london_stations()
    overlay._template = Template(
        "{% macro script(this, kwargs) %}\n" + script +
        "\nsetupBnbTransit({{ this.map_name }}, {{ this.group_name }}, {{ this.saved_stations | tojson }});\n{% endmacro %}"
    )
    map_view.add_child(overlay)


def render_area_map(
    *,
    center: Tuple[float, float],
    zoom: float,
    key: str,
    saved_geometry: Optional[Dict[str, Any]] = None,
    enable_draw: bool = False,
    height: int = 500,
) -> Dict[str, Any]:
    """Let Leaflet own the live viewport; keep its component identity stable.

    streamlit-folium hashes the initialization script into its component key.
    Feeding each drag's center/zoom back into that script remounts the iframe.
    A new caller key (explicit destination lookup) intentionally starts a view.
    """
    seed_key = f"_area_map_initial_view_{key}"
    if seed_key not in st.session_state:
        st.session_state[seed_key] = (tuple(center), normalize_map_zoom(zoom, fractional=True))
    initial_center, initial_zoom = st.session_state[seed_key]
    map_view = _add_base_map(initial_center, initial_zoom)
    _add_transit_stations(map_view)
    boundary = folium.FeatureGroup(name="Saved search area")
    if saved_geometry:
        try:
            area = normalize_geometry(saved_geometry)
            boundary.add_child(folium.GeoJson(area, name="Saved search area", style_function=lambda _: {
                "color": "#e11d48", "weight": 3, "fillColor": "#fb7185", "fillOpacity": 0.15,
            }))
        except (TypeError, ValueError):
            # The definition validation path reports malformed saved geometry.
            pass
    if enable_draw:
        Draw(
            export=False,
            position="topleft",
            draw_options={
                "polyline": False,
                "circle": False,
                "circlemarker": False,
                "marker": False,
                "rectangle": {"shapeOptions": {"color": "#e11d48", "weight": 2}},
                "polygon": {"allowIntersection": False, "shapeOptions": {"color": "#e11d48", "weight": 2}},
            },
            edit_options={"edit": True, "remove": True},
        ).add_to(map_view)
    folium.LayerControl(collapsed=True).add_to(map_view)
    event = st_folium(
        map_view,
        key=key,
        height=height,
        feature_group_to_add=boundary,
        use_container_width=True,
        returned_objects=["all_drawings", "last_active_drawing", "bounds", "center", "zoom"],
    )
    return event if isinstance(event, dict) else {}



def render_listing_map(
    listings: pd.DataFrame,
    *,
    center: Tuple[float, float],
    zoom: float,
    selected_room_id: Optional[str],
    search_definition: Optional[Dict[str, Any]],
    overlay_1_df: Optional[pd.DataFrame] = None,
    overlay_2_df: Optional[pd.DataFrame] = None,
    key: str,
) -> Dict[str, Any]:
    """Render markers with stable ID tooltips; never infer clicks from coordinates."""
    map_view = _add_base_map(center, zoom)
    _add_transit_stations(map_view)
    geometry = (search_definition or {}).get("geometry")
    if geometry:
        try:
            area = normalize_geometry(geometry)
            map_view.add_child(folium.GeoJson(area, name="Search boundary", style_function=lambda _: {
                "color": "#2563eb", "weight": 2, "fillColor": "#60a5fa", "fillOpacity": 0.05,
            }))
        except (TypeError, ValueError):
            pass
    frame = listings.copy()
    if not frame.empty:
        frame["latitude"] = pd.to_numeric(frame.get("latitude"), errors="coerce")
        frame["longitude"] = pd.to_numeric(frame.get("longitude"), errors="coerce")
        frame = frame.dropna(subset=["latitude", "longitude"])
    marker_cluster = MarkerCluster(name="Listings", overlay=True, control=False, disableClusteringAtZoom=17)
    marker_cluster.add_to(map_view)
    for _, row in frame.iterrows():
        room_id = str(row.get("room_id", "")).strip()
        if not room_id:
            continue
        is_selected = room_id == str(selected_room_id or "")
        is_shortlisted = bool(row.get("is_shortlisted", False))
        price = pd.to_numeric(pd.Series([row.get("verified_total_price")]), errors="coerce").iloc[0]
        currency = str(row.get("currency_ui") or (search_definition or {}).get("currency", "EUR"))
        if pd.notna(price) and math.isfinite(float(price)) and float(price) > 0:
            price_text = f"Airbnb quote · {currency} {price:.2f}"
        else:
            previous_price = pd.to_numeric(pd.Series([row.get("total_price")]), errors="coerce").iloc[0]
            if pd.notna(previous_price) and math.isfinite(float(previous_price)) and float(previous_price) > 0:
                price_text = "Previously quoted Airbnb amount · refresh needed"
            else:
                price_text = "Airbnb quote unavailable"
        name = str(row.get("name") or row.get("title") or "Listing")
        rating = pd.to_numeric(pd.Series([row.get("Airbnb_rating")]), errors="coerce").iloc[0]
        rating_text = f" · ★ {rating:.2f}" if pd.notna(rating) else ""
        marker_id = quote(room_id, safe="")
        tooltip_text = f"BNBID::{marker_id} · {price_text}{rating_text} · {escape(name[:56])}"
        color = "#d3195b" if is_selected else ("#d9a300" if is_shortlisted else "#333333")
        marker = folium.CircleMarker(
            location=[float(row["latitude"]), float(row["longitude"])],
            radius=10 if is_selected else 7,
            color=color,
            weight=3 if is_selected else 2,
            fill=True,
            fill_color=color,
            fill_opacity=0.92,
            tooltip=folium.Tooltip(tooltip_text, sticky=True),
            popup=folium.Popup(
                f"<b>{escape(price_text)}</b><br>{escape(name)}<br><small>Listing ID: {escape(room_id)}</small>",
                max_width=280,
            ),
        )
        marker.add_to(marker_cluster)
    bundled_points = {(round(e['lat'], 4), round(e['lon'], 4)) for e in _saved_london_stations()['elements']}
    for overlay_index, overlay_frame in enumerate((overlay_1_df, overlay_2_df), start=1):
        if overlay_frame is None or overlay_frame.empty:
            continue
        label = "Transit / places 1" if overlay_index == 1 else "Transit / places 2"
        color = "#b42318" if overlay_index == 1 else "#2457a7"
        group = folium.FeatureGroup(name=label, show=True)
        for _, place in overlay_frame.iterrows():
            try:
                latitude = float(place["Latitude"])
                longitude = float(place["Longitude"])
                if (round(latitude, 4), round(longitude, 4)) in bundled_points:
                    continue
                name = escape(str(place.get("Station") or place.get("Name") or f"Place {overlay_index}"))
            except (KeyError, TypeError, ValueError):
                continue
            folium.CircleMarker(
                [latitude, longitude], radius=5, color=color, fill=True,
                fill_color=color, fill_opacity=0.85, tooltip=name, popup=name,
            ).add_to(group)
        group.add_to(map_view)
    folium.LayerControl(collapsed=True).add_to(map_view)
    event = st_folium(
        map_view,
        key=key,
        height=520,
        use_container_width=True,
        returned_objects=["last_object_clicked_tooltip", "center", "zoom"],
    )
    return event if isinstance(event, dict) else {}


def extract_drawn_geometry(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Choose the last user-drawn feature from the component's current collection."""
    drawings = event.get("all_drawings")
    if isinstance(drawings, list) and drawings:
        for feature in reversed(drawings):
            if isinstance(feature, dict) and (feature.get("geometry") or {}).get("type") in {"Polygon", "Rectangle"}:
                return feature
    active = event.get("last_active_drawing")
    if isinstance(active, dict) and (active.get("geometry") or {}).get("type") in {"Polygon", "Rectangle"}:
        return active
    return None


def bounds_from_event(event: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Normalize streamlit-folium Leaflet bounds to the search service's shape."""
    bounds = event.get("bounds")
    try:
        if isinstance(bounds, dict):
            south_west = bounds.get("_southWest") or bounds.get("southWest") or bounds.get("southwest")
            north_east = bounds.get("_northEast") or bounds.get("northEast") or bounds.get("northeast")
            if south_west and north_east:
                return {
                    "south": float(south_west.get("lat")), "west": float(south_west.get("lng")),
                    "north": float(north_east.get("lat")), "east": float(north_east.get("lng")),
                }
            if {"south", "west", "north", "east"}.issubset(bounds):
                return {key: float(bounds[key]) for key in ("south", "west", "north", "east")}
        if isinstance(bounds, (list, tuple)) and len(bounds) >= 2:
            sw, ne = bounds[0], bounds[1]
            if isinstance(sw, dict) and isinstance(ne, dict):
                return {"south": float(sw["lat"]), "west": float(sw["lng"]), "north": float(ne["lat"]), "east": float(ne["lng"])}
            if isinstance(sw, (list, tuple)) and isinstance(ne, (list, tuple)):
                return {"south": float(sw[0]), "west": float(sw[1]), "north": float(ne[0]), "east": float(ne[1])}
    except (KeyError, TypeError, ValueError):
        return None
    return None


def room_id_from_tooltip(value: Any) -> Optional[str]:
    """Extract only the stable room ID deliberately embedded in marker tooltip."""
    if not isinstance(value, str) or not value.startswith("BNBID::"):
        return None
    return unquote(value.partition(" · ")[0].removeprefix("BNBID::")) or None

