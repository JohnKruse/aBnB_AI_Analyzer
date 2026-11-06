"""Validated trip and map-area definitions for browser-created searches."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from . import storage_service

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEARCHES_ROOT = PROJECT_ROOT / "searches"
SEARCH_SCHEMA_VERSION = 1
MAX_GUESTS = 20
MAX_AREA_SPAN_DEGREES = 2.0
MAX_AREA_BBOX_SQUARE_DEGREES = 4.0
DEFAULT_MAP_ZOOM = 13
MIN_MAP_ZOOM = 2
MAX_MAP_ZOOM = 19
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SAFE_EXISTING_NAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,200}$")


def normalize_map_zoom(value: Any, default: Any = DEFAULT_MAP_ZOOM, *, fractional: bool = False) -> int | float:
    """Clamp zoom; optionally retain quarter levels for interactive map views."""
    def parse(candidate: Any) -> Optional[int | float]:
        if isinstance(candidate, bool) or candidate is None:
            return None
        try:
            numeric = Decimal(str(candidate).strip())
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not numeric.is_finite():
            return None
        if numeric <= MIN_MAP_ZOOM:
            return MIN_MAP_ZOOM
        if numeric >= MAX_MAP_ZOOM:
            return MAX_MAP_ZOOM
        if fractional:
            return float((numeric * 4).to_integral_value(rounding=ROUND_HALF_UP) / 4)
        return int(numeric.to_integral_value(rounding=ROUND_HALF_UP))

    return parse(value) or parse(default) or DEFAULT_MAP_ZOOM


def validate_search_name(name: str) -> str:
    """Validate a workspace slug before using it in a path."""
    value = str(name or "").strip()
    if not _SAFE_NAME.fullmatch(value):
        raise ValueError("Search name must be 1–64 letters, digits, underscores, or hyphens.")
    return value


def validate_existing_search_name(name: str) -> str:
    """Accept legacy Unicode/dot names while rejecting path components and controls."""
    value = str(name or "").strip()
    if value in {".", ".."} or not _SAFE_EXISTING_NAME.fullmatch(value):
        raise ValueError("Search name must be a single safe directory name.")
    return value


def make_search_slug(label: str) -> str:
    """Create a readable, path-safe workspace name from a destination and dates."""
    text = unicodedata.normalize("NFKD", str(label or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    slug = re.sub(r"_+", "_", slug)[:64].strip("_")
    if not slug:
        raise ValueError("Enter a destination to create a readable search name.")
    if not slug[0].isalnum():
        slug = f"search_{slug}"
    return slug


def _parse_date(value: Any, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid date in YYYY-MM-DD format.") from exc


def _number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number.")
    return number


def _boolean(value: Any, label: str, default: bool = False) -> bool:
    """Normalize strict boolean values while keeping old search files compatible."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise ValueError(f"{label} must be true or false.")


def _lat_lng(point: Any) -> Tuple[float, float]:
    """Read GeoJSON [longitude, latitude] or map {lat, lng} points."""
    if isinstance(point, dict):
        lat = _number(point.get("lat", point.get("latitude")), "Latitude")
        lng = _number(point.get("lng", point.get("lon", point.get("longitude"))), "Longitude")
    elif isinstance(point, (list, tuple)) and len(point) >= 2:
        lng, lat = _number(point[0], "Longitude"), _number(point[1], "Latitude")
    else:
        raise ValueError("Area geometry contains an unsupported point.")
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        raise ValueError("Area coordinates must be within latitude −90..90 and longitude −180..180.")
    return lat, lng


def normalize_geometry(geometry: Any) -> Dict[str, Any]:
    """Validate a rectangle or simple GeoJSON polygon and calculate its bounds."""
    if not isinstance(geometry, dict):
        raise ValueError("Draw a rectangle or polygon on the map before saving.")
    if geometry.get("type") == "Feature":
        geometry = geometry.get("geometry") or {}
    kind = str(geometry.get("type", "")).lower()

    if kind in {"bbox", "rectangle"} or {"south", "west", "north", "east"}.issubset(geometry):
        bounds = geometry.get("bounds", geometry)
        south = _number(bounds.get("south", bounds.get("sw_lat")), "South latitude")
        west = _number(bounds.get("west", bounds.get("sw_lng", bounds.get("sw_long"))), "West longitude")
        north = _number(bounds.get("north", bounds.get("ne_lat")), "North latitude")
        east = _number(bounds.get("east", bounds.get("ne_lng", bounds.get("ne_long"))), "East longitude")
        if not (-90 <= south < north <= 90) or not (-180 <= west < east <= 180):
            raise ValueError("Rectangle bounds must have south < north and west < east within the map.")
        points = [(south, west), (south, east), (north, east), (north, west)]
        normalized: Dict[str, Any] = {
            "type": "Polygon",
            "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
            "shape": "rectangle",
        }
    elif kind == "polygon":
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) != 1:
            raise ValueError("Only a single polygon without holes is supported.")
        ring = coordinates[0]
        if not isinstance(ring, list) or len(ring) < 3:
            raise ValueError("Draw a polygon with at least three distinct points.")
        if len(ring) > 200:
            raise ValueError("Polygon boundaries are limited to 200 vertices in this release.")
        points = [_lat_lng(point) for point in ring]
        if points[0] != points[-1]:
            points.append(points[0])
        if len(set(points[:-1])) < 3:
            raise ValueError("Draw a polygon with at least three distinct points.")
        if _polygon_self_intersects(points):
            raise ValueError("Polygon edges cannot cross; draw one simple boundary without holes.")
        signed_area = sum(
            points[index][1] * points[index + 1][0] - points[index + 1][1] * points[index][0]
            for index in range(len(points) - 1)
        )
        if abs(signed_area) < 1e-12:
            raise ValueError("Polygon area must be greater than zero.")
        longitudes = [lng for _, lng in points[:-1]]
        latitudes = [lat for lat, _ in points[:-1]]
        if max(longitudes) - min(longitudes) > 180:
            raise ValueError("Dateline-crossing polygons are not supported.")
        normalized = {
            "type": "Polygon",
            "coordinates": [[[lng, lat] for lat, lng in points]],
            "shape": "polygon",
        }
    else:
        raise ValueError("Area geometry must be a rectangle or a single polygon.")

    lats = [lat for lat, _ in points]
    lngs = [lng for _, lng in points]
    south, north, west, east = min(lats), max(lats), min(lngs), max(lngs)
    if east <= west or north <= south:
        raise ValueError("Area must have nonzero width and height.")
    if north - south > MAX_AREA_SPAN_DEGREES or east - west > MAX_AREA_SPAN_DEGREES:
        raise ValueError(f"Search area is too large; each side must be at most {MAX_AREA_SPAN_DEGREES:g}°.")
    if (north - south) * (east - west) > MAX_AREA_BBOX_SQUARE_DEGREES:
        raise ValueError("Search area is too large for the initial browser search workflow.")
    normalized["bounds"] = {"south": south, "west": west, "north": north, "east": east}
    return normalized


def _orientation(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> float:
    """Cross product for points stored as (latitude, longitude)."""
    return (b[1] - a[1]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[1] - a[1])


def _segments_intersect(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float], d: Tuple[float, float]) -> bool:
    eps = 1e-12
    o1, o2, o3, o4 = _orientation(a, b, c), _orientation(a, b, d), _orientation(c, d, a), _orientation(c, d, b)
    if (o1 > eps and o2 < -eps or o1 < -eps and o2 > eps) and (o3 > eps and o4 < -eps or o3 < -eps and o4 > eps):
        return True

    def on_segment(p: Tuple[float, float], q: Tuple[float, float], r: Tuple[float, float]) -> bool:
        return (
            min(p[0], r[0]) - eps <= q[0] <= max(p[0], r[0]) + eps
            and min(p[1], r[1]) - eps <= q[1] <= max(p[1], r[1]) + eps
        )

    return (
        abs(o1) <= eps and on_segment(a, c, b)
        or abs(o2) <= eps and on_segment(a, d, b)
        or abs(o3) <= eps and on_segment(c, a, d)
        or abs(o4) <= eps and on_segment(c, b, d)
    )


def _polygon_self_intersects(points: Sequence[Tuple[float, float]]) -> bool:
    """Reject intersections between nonadjacent edges of a closed polygon."""
    edge_count = len(points) - 1
    for first in range(edge_count):
        for second in range(first + 1, edge_count):
            if second == first + 1 or (first == 0 and second == edge_count - 1):
                continue
            if _segments_intersect(points[first], points[first + 1], points[second], points[second + 1]):
                return True
    return False


def validate_trip_definition(
    values: Dict[str, Any],
    *,
    allow_past: bool = False,
    preserve_updated_at: bool = False,
) -> Dict[str, Any]:
    """Normalize and validate trip dates, party, currency, budget and area."""
    if not isinstance(values, dict):
        raise ValueError("Trip details must be supplied as a mapping.")
    location = str(values.get("location", values.get("destination", ""))).strip()
    if not location:
        raise ValueError("Enter a destination or neighborhood.")
    if len(location) > 200:
        raise ValueError("Destination text must be 200 characters or fewer.")

    check_in = _parse_date(values.get("check_in"), "Check-in")
    check_out = _parse_date(values.get("check_out"), "Check-out")
    if check_out <= check_in:
        raise ValueError("Check-out must be after check-in.")
    if not allow_past and check_in < date.today():
        raise ValueError("Check-in must be today or later for a new browser search.")

    party: Dict[str, int] = {}
    for field in ("adults", "children", "infants", "pets"):
        raw = values.get(field, 1 if field == "adults" else 0)
        try:
            numeric_float = _number(raw, field.title())
        except ValueError as exc:
            raise ValueError(f"{field.title()} must be a whole number.") from exc
        if not numeric_float.is_integer():
            raise ValueError(f"{field.title()} must be a whole number.")
        numeric = int(numeric_float)
        if numeric < 0 or (field == "adults" and numeric < 1):
            raise ValueError("At least one adult is required; other party counts cannot be negative.")
        party[field] = numeric
    guest_count = party["adults"] + party["children"] + party["infants"]
    if guest_count > MAX_GUESTS:
        raise ValueError(f"Total guests cannot exceed {MAX_GUESTS} in this release.")

    currency = str(values.get("currency", "EUR")).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Currency must be a three-letter code, such as EUR or USD.")

    budget = values.get("total_budget")
    if budget in (None, ""):
        budget_value = None
    else:
        budget_value = _number(budget, "Total-stay budget")
        if budget_value <= 0:
            raise ValueError("Total-stay budget must be greater than zero.")

    geometry = normalize_geometry(values.get("geometry", values.get("area")))
    bounds = geometry["bounds"]
    now = datetime.now(timezone.utc).isoformat()
    free_cancellation = _boolean(values.get("free_cancellation", False), "Free cancellation")
    return {
        "schema_version": SEARCH_SCHEMA_VERSION,
        "location": location,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        **party,
        "guest_count": guest_count,
        "currency": currency,
        "total_budget": budget_value,
        "free_cancellation": free_cancellation,
        "geometry": geometry,
        "bounds": bounds,
        "trip_revision": int(values.get("trip_revision", 1)),
        "created_at": values.get("created_at", now),
        "updated_at": values.get("updated_at", now) if preserve_updated_at else now,
    }


def point_in_polygon(latitude: float, longitude: float, geometry: Dict[str, Any]) -> bool:
    """Test a point against a rectangle/polygon, including polygon boundaries."""
    shape = normalize_geometry(geometry)
    return _point_in_normalized_geometry(latitude, longitude, shape)


def _point_in_normalized_geometry(latitude: float, longitude: float, shape: Dict[str, Any]) -> bool:
    """Fast boundary-inclusive test for an already validated polygon."""
    bounds = shape["bounds"]
    lat = _number(latitude, "Latitude")
    lng = _number(longitude, "Longitude")
    if not (bounds["south"] <= lat <= bounds["north"] and bounds["west"] <= lng <= bounds["east"]):
        return False
    ring = shape["coordinates"][0]
    inside = False
    epsilon = 1e-10
    for index in range(len(ring) - 1):
        x1, y1 = ring[index]
        x2, y2 = ring[index + 1]
        cross = (lng - x1) * (y2 - y1) - (lat - y1) * (x2 - x1)
        if abs(cross) <= epsilon and min(x1, x2) - epsilon <= lng <= max(x1, x2) + epsilon and min(y1, y2) - epsilon <= lat <= max(y1, y2) + epsilon:
            return True
        if (y1 > lat) != (y2 > lat):
            intersect_lng = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lng < intersect_lng:
                inside = not inside
    return inside


def _safe_search_dir(search_name: str, root: Optional[Path] = None) -> Path:
    name = validate_existing_search_name(search_name)
    root = Path(root or SEARCHES_ROOT).resolve()
    directory = (root / name).resolve()
    if directory.parent != root:
        raise ValueError("Search workspace path escapes its configured root.")
    return directory


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _assert_trip_not_locked(directory: Path) -> None:
    """Do not change dates/party/area while a background writer uses this trip."""
    lock_path = directory / "jobs" / "writer.lock"
    if not lock_path.exists():
        return
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(lock.get("pid", -1))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return
    if pid <= 0:
        return
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    except OSError:
        return
    raise ValueError("This search has an active collection or evaluation job. Wait for it to finish or cancel it before changing the trip.")


def _trip_context(definition: Dict[str, Any]) -> Dict[str, Any]:
    context = {key: definition.get(key) for key in (
        "location", "check_in", "check_out", "adults", "children", "infants", "pets",
        "currency", "total_budget", "geometry",
    )}
    context["free_cancellation"] = _boolean(definition.get("free_cancellation", False), "Free cancellation")
    return context


def save_search_definition(
    search_name: str,
    values: Dict[str, Any],
    *,
    create: bool = False,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create or update a search definition, revisioning changed trip context.

    A changed date, party, currency, budget, cancellation filter, destination
    or area increments the trip revision. Callers can use that revision to mark
    old discovery results and price quotes stale.
    """
    name = validate_search_name(search_name) if create else validate_existing_search_name(search_name)
    directory = _safe_search_dir(name, root)
    definition_path = directory / "search_definition.json"
    if create and directory.exists() and any(directory.iterdir()):
        raise FileExistsError(f"Search '{name}' already exists; choose another name.")
    existing = None
    definition_exists = False
    try:
        definition_exists = storage_service.exists(definition_path)
        if definition_exists:
            existing = storage_service.read_json(definition_path)
            if existing is not None and not isinstance(existing, dict):
                raise ValueError(f"Existing search definition at {definition_path} is not a JSON object.")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Existing search definition at"):
            raise
        raise ValueError(f"Existing search definition is unreadable: {exc}") from exc
    if not definition_exists and ((directory / "config.yaml").exists() or (directory / "output_data").exists()):
        # Legacy workspace: preserve its directory and treat context as revision 1.
        existing = None
    if not create and existing is not None:
        _assert_trip_not_locked(directory)

    candidate = validate_trip_definition(values, allow_past=not create and existing is None)
    if existing:
        same_context = _trip_context(existing) == _trip_context(candidate)
        candidate["trip_revision"] = int(existing.get("trip_revision", 1)) + (0 if same_context else 1)
        candidate["created_at"] = existing.get("created_at", candidate["created_at"])
    else:
        candidate["trip_revision"] = 1
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "output_data").mkdir(parents=True, exist_ok=True)
    storage_service.write_json(definition_path, candidate)

    config = {
        "location": candidate["location"],
        "check_in": candidate["check_in"],
        "check_out": candidate["check_out"],
        "currency": candidate["currency"],
        "ne_lat": candidate["bounds"]["north"],
        "ne_long": candidate["bounds"]["east"],
        "sw_lat": candidate["bounds"]["south"],
        "sw_long": candidate["bounds"]["west"],
        "adults": candidate["adults"],
        "children": candidate["children"],
        "infants": candidate["infants"],
        "pets": candidate["pets"],
        "default_occupants": candidate["guest_count"],
        "total_budget": candidate["total_budget"],
        "free_cancellation": candidate["free_cancellation"],
        "trip_revision": candidate["trip_revision"],
        "search_name": name,
        "search_subdir": name,
        "output_subdir": f"{name}/output_data",
    }
    config_path = directory / "config.yaml"
    existing_config: Dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded_config, dict):
                existing_config = loaded_config
        except (OSError, yaml.YAMLError):
            raise ValueError(f"Existing search config is unreadable at {config_path}; it was left unchanged.")
    existing_config.update(config)
    _atomic_write(config_path, yaml.safe_dump(existing_config, sort_keys=False, allow_unicode=True))
    return candidate


def load_search_definition(search_name: str, *, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Read a stored definition, returning None for a legacy-only workspace."""
    path = _safe_search_dir(search_name, root) / "search_definition.json"
    try:
        if not storage_service.exists(path):
            return None
        data = storage_service.read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Search definition is unreadable at {path}: {exc}") from exc
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"Search definition at {path} is not a JSON object.")
    return validate_trip_definition(data, allow_past=True, preserve_updated_at=True)


def geocode_location(query: str, timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    """Geocode a place, address, or neighborhood using OpenStreetMap Nominatim.

    Returns a dict with 'lat', 'lng', 'zoom', 'bounds', and 'display_name', or None if unresolved.
    """
    clean = (query or "").strip()
    if not clean:
        return None
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "AirbnbAIAnalyzer/2.0 (contact: github.com)"}
    params = {"q": clean, "format": "json", "limit": 1}
    try:
        import requests

        response = requests.get(url, params=params, headers=headers, timeout=timeout)
        if response.status_code == 200:
            items = response.json()
            if isinstance(items, list) and len(items) > 0:
                item = items[0]
                lat = float(item["lat"])
                lng = float(item["lon"])
                bbox = item.get("boundingbox")
                zoom = DEFAULT_MAP_ZOOM
                bounds = None
                if bbox and len(bbox) == 4:
                    south, north = float(bbox[0]), float(bbox[1])
                    west, east = float(bbox[2]), float(bbox[3])
                    span = max(abs(north - south), abs(east - west))
                    if span > 2.0:
                        zoom = 9
                    elif span > 0.8:
                        zoom = 11
                    elif span > 0.2:
                        zoom = 12
                    elif span > 0.05:
                        zoom = 13
                    else:
                        zoom = 14
                    bounds = {"south": south, "north": north, "west": west, "east": east}
                return {
                    "lat": lat,
                    "lng": lng,
                    "zoom": zoom,
                    "bounds": bounds,
                    "display_name": item.get("display_name", clean),
                }
    except Exception:
        return None
    return None


