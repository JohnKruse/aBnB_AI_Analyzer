"""Streamlit browser workflow for creating, collecting, and curating searches.

The page is deliberately a thin caller of the persistent service modules. It
never calls Airbnb or an AI provider during rendering; those requests start only
when the user presses a stage button and are executed by the durable job service.
"""

from __future__ import annotations

import ast
from collections import OrderedDict
import hashlib
import json
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, AgGridTheme, DataReturnMode, GridOptionsBuilder, GridUpdateMode, JsCode

from src.airbnb_service import collector_capabilities
from src.branding import github_logo_link
from src.curation_service import (
    CurationCorruptError,
    dismiss_property,
    ensure_manual_order,
    extend_manual_order,
    load_user_curation,
    reorder_visible_properties,
    restore_all_properties,
    restore_property,
    toggle_shortlist_property,
)
from src.feature_extractor import enrich_properties_dataframe
from src.pipeline_service import (
    create_search_workspace,
    get_search_status,
    list_available_searches,
    load_search_config,
)
from src.search_service import (
    geocode_location,
    load_search_definition,
    make_search_slug,
    normalize_geometry,
    normalize_map_zoom,
    point_in_polygon,
    reverse_geocode_location,
    save_search_definition,
    validate_trip_definition,
)
from src.ui.map_view import (
    DEFAULT_CENTER,
    bounds_from_event,
    extract_drawn_geometry,
    render_area_map,
    render_listing_map,
    room_id_from_tooltip,
)


PRIORITY_LABELS = {
    "cleanliness": "Cleanliness",
    "quiet_sleep": "Quiet and sleep quality",
    "beds": "Beds and sleeping arrangements",
    "transit_walkability": "Transit and walkability",
    "bathroom_quality": "Bathroom quality",
    "temperature_comfort": "Temperature comfort",
    "workspace_wifi": "Workspace and Wi-Fi",
    "value": "Value for money",
}
TOP_N_OPTIONS = [10, 20, 30, 50, 0]
TOP_N_LABELS = {10: "10", 20: "20", 30: "30", 50: "50", 0: "All"}
SORT_OPTIONS = ["Manual Order", "AI Fit", "Airbnb quote", "Airbnb Rating", "Value"]
AMENITY_MODES = ["Any", "Confirmed Present", "Present or Unknown"]
HOME_TYPE_OPTIONS = ["Entire home/apt", "Private room", "Shared room", "Hotel room", "Unknown"]
CANCELLATION_FILTER_OPTIONS = ["Any", "Free cancellation offered", "Not free or unknown"]
ACTIVE_JOB_STATES = {"queued", "running", "cancelling"}
MAX_TRIP_DATE = date(2100, 12, 31)
PREPARED_SNAPSHOT_CACHE_KEY = "_prepared_browser_snapshots"
PREPARED_SNAPSHOT_CACHE_LIMIT = 2
PREPARED_SNAPSHOT_CACHE_VERSION = 1


def _coerce_date(value: Any) -> Optional[date]:
    """Read a Streamlit or persisted date value without raising on stale state."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _checkout_for_checkin(
    check_in: date,
    current_checkout: Any,
    previous_checkin: Any = None,
    previous_checkout: Any = None,
    *,
    default_nights: int = 5,
) -> date:
    """Keep a valid checkout, or move it with check-in using the prior stay length."""
    minimum = check_in + timedelta(days=1)
    checkout = _coerce_date(current_checkout)
    if checkout is not None and checkout >= minimum:
        return min(checkout, MAX_TRIP_DATE)

    prior_in = _coerce_date(previous_checkin)
    prior_out = _coerce_date(previous_checkout)
    nights = (prior_out - prior_in).days if prior_in and prior_out else default_nights
    nights = max(int(nights), 1)
    return min(max(check_in + timedelta(days=nights), minimum), MAX_TRIP_DATE)


def _prepare_checkout_widget(check_in: date, checkout_key: str, scope: str, default_checkout: date) -> date:
    """Repair checkout state before its widget is instantiated in this run."""
    previous_in_key = f"{scope}_previous_check_in"
    previous_out_key = f"{scope}_previous_check_out"
    current_checkout = st.session_state.get(checkout_key, default_checkout)
    normalized = _checkout_for_checkin(
        check_in,
        current_checkout,
        st.session_state.get(previous_in_key),
        st.session_state.get(previous_out_key),
    )
    if checkout_key in st.session_state and _coerce_date(st.session_state[checkout_key]) != normalized:
        # The checkout widget has not been instantiated in this rerun yet.
        st.session_state[checkout_key] = normalized
    return normalized


def _remember_trip_dates(scope: str, check_in: date, check_out: date) -> None:
    st.session_state[f"{scope}_previous_check_in"] = check_in
    st.session_state[f"{scope}_previous_check_out"] = check_out


def _colored_button(container, label, *, tone="danger", **kwargs):
    """Scope a semantic button color without changing its key or behavior."""
    with container.container():
        st.markdown(f'<span data-bnb-button-tone="{tone}" aria-hidden="true"></span>', unsafe_allow_html=True)
        return st.button(label, **kwargs)


def _service(name: str):
    """Import a service lazily so an unfinished optional worker does not break import."""
    module = __import__(name, fromlist=["*"])
    return module


def _missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, set)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _parse_nested(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return value
    if isinstance(value, str) and value.strip():
        for parser in (json.loads, ast.literal_eval):
            try:
                return parser(value)
            except (ValueError, TypeError, SyntaxError, json.JSONDecodeError):
                pass
    return None


def _explicit_total(value: Any) -> float:
    """Read a stay total only when the source labels it as a total.

    Airbnb's nested ``unit.amount`` is a nightly rate, so it is intentionally not
    used to fill a missing total-stay quote.
    """
    if isinstance(value, (int, float, np.number)) and not _missing(value):
        number = float(value)
        return number if math.isfinite(number) and number > 0 else np.nan
    parsed = _parse_nested(value)
    if isinstance(parsed, dict):
        for key in ("total_price", "total_amount", "stay_total"):
            try:
                number = float(parsed.get(key))
                if math.isfinite(number) and number > 0:
                    return number
            except (TypeError, ValueError):
                pass
        total = parsed.get("total")
        if isinstance(total, dict):
            for key in ("amount", "formatted_amount"):
                raw = total.get(key)
                try:
                    number = float(raw)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number) and number > 0:
                    return number
        breakdown = parsed.get("break_down", parsed.get("breakdown", []))
        if isinstance(breakdown, (list, tuple)):
            for item in breakdown:
                if not isinstance(item, dict) or str(item.get("description", "")).strip().casefold() != "total":
                    continue
                try:
                    number = float(item.get("amount"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number) and number > 0:
                    return number
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        # A plain scalar in the total_price field is an explicit total column.
        try:
            number = float(text.replace("€", "").replace("$", "").replace("£", "").strip())
            return number if math.isfinite(number) and number > 0 else np.nan
        except ValueError:
            pass
    return np.nan


def _value(row: pd.Series, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row.index and not _missing(row.get(key)):
            return row.get(key)
    return default


def _js_string(value: Any) -> str:
    """Encode a string literal for the small static AgGrid callbacks."""
    return json.dumps(str(value)).replace("<", r"\u003c").replace(">", r"\u003e").replace("&", r"\u0026")


def _numeric(series: pd.Series, default: float = np.nan) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    if not math.isnan(default):
        values = values.fillna(default)
    return values


def _room_ids(frame: pd.DataFrame) -> List[str]:
    if frame.empty or "room_id" not in frame.columns:
        return []
    return frame["room_id"].dropna().astype(str).map(str.strip).loc[lambda col: col.ne("")].drop_duplicates().tolist()


def _quote_status(row: pd.Series, definition: Optional[Dict[str, Any]]) -> str:
    """Recheck quote context at display time after trip edits/revisions."""
    status = str(_value(row, "quote_status", default="")).lower()
    availability = str(_value(row, "availability_state", default="")).lower()
    if status == "unavailable" or availability == "unavailable":
        return "unavailable"
    if status == "failed":
        return "failed"
    if not definition:
        return "legacy / unverified"
    fetched = _value(row, "details_fetched_at", "quote_fetched_at")
    if fetched is None or not str(fetched).strip():
        existing = str(_value(row, "quote_status", default="missing")).lower()
        return "missing" if existing in {"", "none", "missing", "nan"} else "unverified"
    status = str(_value(row, "quote_status", default="unverified")).lower()
    if status in {"stale", "missing", "unverified"}:
        return status

    quote_revision = _value(row, "trip_revision_details", "quote_trip_revision", "trip_revision")
    try:
        revision_ok = int(float(quote_revision)) == int(definition.get("trip_revision", 0))
    except (TypeError, ValueError):
        revision_ok = False
    context_ok = (
        str(_value(row, "quote_check_in", default="")) == str(definition.get("check_in", ""))
        and str(_value(row, "quote_check_out", default="")) == str(definition.get("check_out", ""))
        and str(_value(row, "quote_currency", default="")).upper() == str(definition.get("currency", "")).upper()
    )
    party_ok = str(_value(row, "quote_party_supported", default="")).lower() in {"true", "1", "yes"}
    for key in ("adults", "children", "infants", "pets"):
        expected = definition.get(key, 1 if key == "adults" else 0)
        actual = _value(row, f"quote_{key}")
        try:
            if int(float(actual)) != int(expected):
                party_ok = False
        except (TypeError, ValueError):
            party_ok = False
    return "current" if revision_ok and context_ok and party_ok else ("stale" if not revision_ok else "unverified")


def _prepare_dataset(frame: pd.DataFrame, definition: Optional[Dict[str, Any]]) -> pd.DataFrame:
    data = frame.copy()
    if data.empty:
        return data
    from src import airbnb_service

    repair_quotes = getattr(airbnb_service, "repair_saved_quote_prices", None)
    if callable(repair_quotes):
        repaired = repair_quotes(data, definition)
        if isinstance(repaired, pd.DataFrame):
            data = repaired
    if "room_id" not in data.columns:
        data["room_id"] = data.index.astype(str)
    data["room_id"] = data["room_id"].astype(str)
    data = data.drop_duplicates(subset=["room_id"], keep="first")
    data = enrich_properties_dataframe(data, search_context=definition)
    for column in ("total_price", "price", "Airbnb_rating", "rating", "AI_rating", "personal_fit", "evaluation_score", "person_capacity", "latitude", "longitude"):
        if column not in data.columns:
            data[column] = np.nan
        if column == "total_price":
            original_total = data[column]
            data[column] = original_total.map(_explicit_total)
        else:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    valid_totals = pd.to_numeric(data["total_price"], errors="coerce")
    data["total_price"] = valid_totals.where(np.isfinite(valid_totals) & valid_totals.gt(0))
    if "personal_fit" not in frame.columns and "personal_fit_score" in frame.columns:
        data["personal_fit"] = pd.to_numeric(frame["personal_fit_score"], errors="coerce")
    if "coverage" not in frame.columns and "personal_fit_coverage" in frame.columns:
        data["coverage"] = pd.to_numeric(frame["personal_fit_coverage"], errors="coerce")
    if "personal_fit" in data.columns:
        data["personal_fit"] = pd.to_numeric(data["personal_fit"], errors="coerce")
    if "quote_status" not in data.columns:
        data["quote_status"] = "missing"
    data["quote_status_ui"] = data.apply(lambda row: _quote_status(row, definition), axis=1)
    invalid_current_quote = data["quote_status_ui"].eq("current") & data["total_price"].isna()
    data.loc[invalid_current_quote, "quote_status_ui"] = "unverified"
    if "personal_fit_status" in data.columns:
        data["personal_fit_status"] = data["personal_fit_status"].fillna("missing").astype(str)
        data["personal_fit_prior_score"] = data["personal_fit"].where(data["personal_fit_status"].eq("stale"))
        data["personal_fit"] = data["personal_fit"].where(data["personal_fit_status"].eq("scored"))
    else:
        data["personal_fit_status"] = data["personal_fit"].map(lambda value: "scored" if pd.notna(value) else "missing")
        data["personal_fit_prior_score"] = np.nan
    data["verified_total_price"] = data["total_price"].where(data["quote_status_ui"].eq("current"))
    data["currency_ui"] = str((definition or {}).get("currency", "EUR"))
    return data


def _small_fingerprint(value: Any) -> str:
    """Fingerprint small definitions/profiles without touching listing frames."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _trip_definition_fingerprint(definition: Optional[Dict[str, Any]]) -> str:
    """Fingerprint operative trip fields, excluding volatile audit timestamps."""
    payload = dict(definition or {})
    payload.pop("created_at", None)
    payload.pop("updated_at", None)
    return _small_fingerprint(payload)


def _file_signature(path: Path) -> Tuple[Any, ...]:
    """Cheap atomic-replacement-aware signature for a saved input file."""
    try:
        resolved = path.resolve()
        stat = resolved.stat()
        return (
            str(resolved),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
            int(getattr(stat, "st_ino", 0)),
        )
    except OSError:
        return (str(path), None)


def _resource_signature(path: Path) -> Tuple[Any, ...]:
    """Combine storage revision and export-file signature for cache invalidation."""
    file_token = _file_signature(path)
    try:
        storage = __import__("src.storage_service", fromlist=["signature"])
        storage_signature = getattr(storage, "signature", None)
        if not callable(storage_signature):
            storage_signature = getattr(storage, "resource_signature", None)
        if callable(storage_signature):
            return ("resource", str(storage_signature(path)), "file", file_token)
    except (ImportError, AttributeError):
        pass
    return ("file", file_token)


def _prepared_snapshot_key(
    search_name: str,
    definition: Optional[Dict[str, Any]],
    profile_name: str,
    profile: Dict[str, Any],
    evaluation_service: Any,
) -> Tuple[Any, ...]:
    """Build a cache key from persisted input signatures and small fingerprints."""
    from src.search_service import SEARCHES_ROOT, validate_existing_search_name

    safe_name = validate_existing_search_name(search_name)
    searches_root = Path(SEARCHES_ROOT).resolve()
    workspace = (searches_root / safe_name).resolve()
    if workspace.parent != searches_root:
        raise ValueError("Search workspace path is outside the configured searches directory.")
    inputs = (
        _resource_signature(workspace / "output_data" / "merged_df.csv"),
        _resource_signature(workspace / "search_definition.json"),
        _resource_signature(workspace / "evaluation_profiles.json"),
        _resource_signature(workspace / "output_data" / "personal_evaluations.json"),
        _resource_signature(workspace / "processing_selection.json"),
    )
    implementation_paths = [Path(__file__)]
    for module_name in ("review_app", "src.feature_extractor", "src.airbnb_service", "src.evaluation_service", "src.storage_service"):
        try:
            module = __import__(module_name, fromlist=["*"])
            implementation_paths.append(Path(module.__file__))
        except (ImportError, AttributeError, TypeError):
            continue
    implementation_signatures = tuple(_file_signature(path) for path in implementation_paths)
    fingerprint = getattr(evaluation_service, "profile_fingerprint", None)
    try:
        profile_token = fingerprint(profile) if callable(fingerprint) else _small_fingerprint(profile)
    except Exception:
        profile_token = _small_fingerprint(profile)
    return (
        str(workspace),
        safe_name,
        PREPARED_SNAPSHOT_CACHE_VERSION,
        inputs,
        implementation_signatures,
        _trip_definition_fingerprint(definition),
        str(profile_name),
        str(profile_token),
    )


def _is_initial_storage_import_transition(before: Tuple[Any, ...], after: Tuple[Any, ...]) -> bool:
    """Recognize only revision-1 imports of unchanged legacy files during load."""
    if len(before) != 8 or len(after) != 8 or before[:3] != after[:3] or before[4:] != after[4:]:
        return False
    if not isinstance(before[3], tuple) or not isinstance(after[3], tuple) or len(before[3]) != len(after[3]):
        return False
    saw_import = False
    for old_input, new_input in zip(before[3], after[3]):
        if old_input == new_input:
            continue
        if (
            not isinstance(old_input, tuple) or not isinstance(new_input, tuple)
            or len(old_input) != 4 or len(new_input) != 4
            or old_input[0] != "resource" or new_input[0] != "resource"
            or old_input[2:] != new_input[2:]
        ):
            return False
        old_parts = str(old_input[1]).split(":")
        new_parts = str(new_input[1]).split(":")
        if (
            len(old_parts) != 6 or len(new_parts) != 7
            or old_parts[0] != "legacy"
            or new_parts[0] not in {"frame", "json"}
            or old_parts[1] != new_parts[1]
            or new_parts[2] != "1"
            or old_parts[2:] != new_parts[3:]
        ):
            return False
        saw_import = True
    return saw_import


def _load_prepared_snapshot(
    search_name: str,
    definition: Optional[Dict[str, Any]],
    profile_name: str,
    evaluation_service: Any,
    profile: Dict[str, Any],
) -> pd.DataFrame:
    """Load, hydrate, and prepare once; reuse the same read-only frame per session."""
    cache_key = _prepared_snapshot_key(search_name, definition, profile_name, profile, evaluation_service)
    cache = st.session_state.get(PREPARED_SNAPSHOT_CACHE_KEY)
    if not isinstance(cache, OrderedDict):
        cache = OrderedDict()
    if cache_key in cache:
        cache.move_to_end(cache_key)
        st.session_state[PREPARED_SNAPSHOT_CACHE_KEY] = cache
        return cache[cache_key]

    raw_dataset = __import__("review_app").load_search_dataset(search_name, enrich=False)
    hydrate = getattr(evaluation_service, "hydrate_personal_evaluations", None)
    hydration_failed = False
    if callable(hydrate) and not raw_dataset.empty:
        try:
            raw_dataset = hydrate(search_name, raw_dataset, profile_name=profile_name)
        except Exception:
            # Keep the saved listing table usable if an evaluation sidecar is damaged.
            hydration_failed = True
    prepared = _prepare_dataset(raw_dataset, definition)
    if not hydration_failed:
        # A read may migrate a CSV resource or overlap a writer. If any input
        # token changes while building the frame, serve this result once but do
        # not bind it to the newer revision; the next rerun will read a stable
        # snapshot and cache it under a matching pre/post key.
        post_read_key = _prepared_snapshot_key(search_name, definition, profile_name, profile, evaluation_service)
        if post_read_key == cache_key or _is_initial_storage_import_transition(cache_key, post_read_key):
            cache[post_read_key] = prepared
            cache.move_to_end(post_read_key)
            while len(cache) > PREPARED_SNAPSHOT_CACHE_LIMIT:
                cache.popitem(last=False)
    st.session_state[PREPARED_SNAPSHOT_CACHE_KEY] = cache
    return prepared


def _auto_sort(frame: pd.DataFrame, sort_by: str) -> pd.DataFrame:
    """Apply deterministic sorts; stale or absent quote data sorts after current quotes."""
    data = frame.copy()
    if data.empty:
        return data
    data["room_id"] = data["room_id"].astype(str)
    if sort_by == "AI Fit":
        data["_sort_score"] = pd.to_numeric(data.get("personal_fit", pd.Series(np.nan, index=data.index)), errors="coerce")
        if "personal_fit_status" in data.columns:
            data["_sort_score"] = data["_sort_score"].where(data["personal_fit_status"].eq("scored"))
        data["_missing"] = data["_sort_score"].isna()
        return data.sort_values(["_missing", "_sort_score", "room_id"], ascending=[True, False, True], kind="stable").drop(columns=["_sort_score", "_missing"])
    if sort_by in {"Total Price", "Airbnb quote"}:
        data["_missing"] = data["verified_total_price"].isna()
        return data.sort_values(["_missing", "verified_total_price", "room_id"], ascending=[True, True, True], kind="stable").drop(columns="_missing")
    if sort_by == "Airbnb Rating":
        data["_missing"] = data["Airbnb_rating"].isna()
        return data.sort_values(["_missing", "Airbnb_rating", "room_id"], ascending=[True, False, True], kind="stable").drop(columns="_missing")
    if sort_by == "Value":
        data["_value"] = data["Airbnb_rating"] / data["verified_total_price"].where(data["verified_total_price"] > 0)
        data["_missing"] = data["_value"].isna()
        return data.sort_values(["_missing", "_value", "room_id"], ascending=[True, False, True], kind="stable").drop(columns=["_value", "_missing"])
    return data.sort_values("room_id", kind="stable")


def _amenity_filter(frame: pd.DataFrame, column: str, mode: str) -> pd.DataFrame:
    if mode == "Any" or frame.empty:
        return frame
    states = frame[column].fillna("unknown").astype(str).str.lower() if column in frame.columns else pd.Series("unknown", index=frame.index)
    if mode == "Confirmed Present":
        return frame[states.eq("present")]
    return frame[states.isin(["present", "unknown", "conflict"])]


def _filter_listings(
    all_rows: pd.DataFrame,
    *,
    definition: Optional[Dict[str, Any]],
    curation: Dict[str, Any],
    party_size: int,
    bathroom_mode: str,
    ac_mode: str,
    washer_mode: str,
    availability_mode: str,
    min_price: float,
    max_price: float,
    min_rating: float,
    sort_by: str,
    top_n: int,
    include_unknown_prices: bool = False,
    home_types: Optional[Sequence[str]] = None,
    min_beds: int = 0,
    min_bedrooms: int = 0,
    include_unknown_bed_counts: bool = False,
    cancellation_mode: str = "Any",
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return shown, matching, and hidden rows; price limits use only current quotes."""
    if all_rows.empty:
        return all_rows.copy(), all_rows.copy(), all_rows.copy()
    geometry = (definition or {}).get("geometry")
    if geometry:
        try:
            area = normalize_geometry(geometry)
        except (TypeError, ValueError):
            area = None
        if area:
            latitude = pd.to_numeric(all_rows["latitude"] if "latitude" in all_rows else pd.Series(index=all_rows.index, dtype=float), errors="coerce")
            longitude = pd.to_numeric(all_rows["longitude"] if "longitude" in all_rows else pd.Series(index=all_rows.index, dtype=float), errors="coerce")
            known_location = latitude.notna() & longitude.notna()
            in_area = pd.Series(True, index=all_rows.index, dtype=bool)
            for row_index in all_rows.index[known_location]:
                in_area.loc[row_index] = point_in_polygon(
                    float(latitude.loc[row_index]), float(longitude.loc[row_index]), area,
                )
            all_rows = all_rows[in_area].copy()
            if all_rows.empty:
                return all_rows.copy(), all_rows.copy(), all_rows.copy()
    ids = all_rows["room_id"].astype(str)
    hidden_ids = set(map(str, curation.get("hidden_room_ids", [])))
    hidden = all_rows[ids.isin(hidden_ids)].copy()
    matching = all_rows[~ids.isin(hidden_ids)].copy()
    matching["is_shortlisted"] = matching["room_id"].isin(set(map(str, curation.get("shortlisted_room_ids", []))))
    availability = matching.get("availability_state", pd.Series("unknown", index=matching.index)).fillna("unknown").astype(str).str.lower()
    known_unavailable = matching["quote_status_ui"].eq("unavailable") | availability.eq("unavailable")
    if availability_mode == "Available only":
        matching = matching[availability.eq("available") & ~known_unavailable]
    elif availability_mode == "Available or unknown":
        matching = matching[~known_unavailable]
    capacity = pd.to_numeric(matching["person_capacity"], errors="coerce")
    matching = matching[capacity.ge(max(int(party_size), 1)) | capacity.isna()]
    if bathroom_mode == "Exclusive bathroom":
        matching = matching[matching["bathroom_privacy"].eq("Private / Dedicated")]
    elif bathroom_mode == "Ensuite only":
        bath = matching["bathroom_evidence"].map(lambda fact: _parse_nested(fact).get("state") if isinstance(_parse_nested(fact), dict) else "unknown")
        matching = matching[bath.eq("ensuite_exclusive")]
    elif bathroom_mode == "Shared bathroom":
        matching = matching[matching["bathroom_privacy"].eq("Shared")]
    matching = _amenity_filter(matching, "ac_state", ac_mode)
    matching = _amenity_filter(matching, "washer_state", washer_mode)

    if home_types is not None:
        type_values = matching.get("accommodation_type", pd.Series("Unknown", index=matching.index))
        type_values = type_values.fillna("Unknown").astype(str)
        matching = matching[type_values.isin(list(home_types))]
    if int(min_beds) > 0:
        beds = pd.to_numeric(matching.get("bed_count", pd.Series(np.nan, index=matching.index)), errors="coerce")
        bed_match = beds.ge(int(min_beds))
        if include_unknown_bed_counts:
            bed_match |= beds.isna()
        matching = matching[bed_match]
    if int(min_bedrooms) > 0:
        bedrooms = pd.to_numeric(matching.get("bedroom_count", pd.Series(np.nan, index=matching.index)), errors="coerce")
        bedroom_match = bedrooms.ge(int(min_bedrooms))
        if include_unknown_bed_counts:
            bedroom_match |= bedrooms.isna()
        matching = matching[bedroom_match]
    cancellation = matching.get("cancellation_state", pd.Series("unknown", index=matching.index)).fillna("unknown").astype(str).str.lower()
    if cancellation_mode == "Free cancellation offered":
        matching = matching[cancellation.eq("free")]
    elif cancellation_mode == "Not free or unknown":
        matching = matching[~cancellation.eq("free")]

    over_budget = pd.DataFrame(columns=matching.columns)
    verified_prices = pd.to_numeric(matching["verified_total_price"], errors="coerce")
    valid_prices = verified_prices.notna() & np.isfinite(verified_prices) & verified_prices.gt(0)
    price_ok = valid_prices & verified_prices.between(float(min_price), float(max_price))
    if include_unknown_prices:
        price_ok |= ~valid_prices
    matching = matching[price_ok]
    rating = pd.to_numeric(matching["Airbnb_rating"], errors="coerce")
    matching = matching[rating.isna() | rating.ge(float(min_rating))]

    if sort_by == "Manual Order":
        curation_order = list(map(str, curation.get("manual_order", [])))
        full_order = extend_manual_order(all_rows["room_id"].astype(str).tolist(), curation_order)
        position = {room_id: index for index, room_id in enumerate(full_order)}
        matching = matching.assign(_manual_pos=matching["room_id"].map(position).fillna(len(position)))
        matching = matching.sort_values(["_manual_pos", "room_id"], kind="stable").drop(columns="_manual_pos")
    else:
        matching = _auto_sort(matching, sort_by)
    matching = matching.reset_index(drop=True)
    shown = matching.head(int(top_n)).copy() if int(top_n) > 0 else matching.copy()
    return shown, matching, hidden


def _active_definition(search_name: str) -> Optional[Dict[str, Any]]:
    try:
        return load_search_definition(search_name)
    except Exception as exc:
        st.error(f"The saved trip definition could not be read: {exc}")
        return None


def _automatic_run_options(scope: str) -> Dict[str, Any]:
    from src import storage_service as storage
    from src.search_service import SEARCHES_ROOT
    previous = storage.read_json(Path(SEARCHES_ROOT) / scope / "run_options.json", default={}) if scope != "new" else {}
    previous = previous or {}
    with st.expander("Automatic run: reviews and retries", expanded=False):
        years = st.selectbox("Review dates", [2, 1, 0], index=[2, 1, 0].index(previous.get("review_years", 2)),
                             format_func=lambda value: f"Last {value} year(s)" if value else "All dates",
                             key=f"review_years_{scope}")
        cap = st.number_input("Maximum reviews per property", min_value=25, max_value=500,
                              value=25, step=25, key=f"review_cap_v2_{scope}")
        st.caption("Newest reviews first. Up to 3 attempts per request and 2 automatic resume rounds. Collection spends up to 5 minutes waiting to retry, then AI evaluates the listings that are ready. Completed work is checkpointed. The app must stay running; the browser tab can be closed.")
    return {"review_years": years, "max_reviews": int(cap), "max_attempts": 3}


def _suggest_search_name(location: str, existing_names: Sequence[str]) -> str:
    label = location.split(",", 1)[0].strip()
    try:
        prefix = make_search_slug(label, preserve_case=True)
    except ValueError:
        prefix = "Map_Area"
    stamp = datetime.now(ZoneInfo("Europe/Rome")).strftime("%y%m%d")
    taken = {name.casefold() for name in existing_names}
    number = 1
    while True:
        suffix = f"_{stamp}_{number:02d}"
        candidate = prefix[:64 - len(suffix)].rstrip("_") + suffix
        if candidate.casefold() not in taken:
            return candidate
        number += 1


def _map_search_location(bounds: Dict[str, float], lookup: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Return the saved location and its short name for the selected area."""
    if (lookup and bounds["south"] <= lookup["lat"] <= bounds["north"]
            and bounds["west"] <= lookup["lng"] <= bounds["east"]):
        return lookup["query"], lookup["query"]
    latitude = (bounds["south"] + bounds["north"]) / 2
    longitude = (bounds["west"] + bounds["east"]) / 2
    inferred = reverse_geocode_location(latitude, longitude)
    if inferred:
        return inferred, inferred
    return f"Map area ({latitude:.4f}, {longitude:.4f})", "Map Area"


def _new_search_form(existing_names: Sequence[str]) -> None:
    st.header("1. Search area and listings")
    st.caption("Look up a destination or pan and zoom the map to choose your search area.")

    col_left, col_right = st.columns([1, 1], gap="large")

    with col_left:
        loc_col, btn_col = st.columns([3, 1])
        with loc_col:
            destination = st.text_input("Destination or neighborhood", key="new_destination",
                                        placeholder="Optional · e.g. Turin, Italy")
        with btn_col:
            st.write("")
            st.write("")
            find_clicked = st.button("Find", key="new_find_location", type="secondary")

        query = destination.strip()
        last_geocoded = st.session_state.get("_last_geocoded_query")
        if not query:
            st.session_state.pop("_last_geocoded_query", None)
            st.session_state.pop("new_destination_lookup", None)
            st.session_state.pop("_geocode_error", None)
        elif find_clicked or query != last_geocoded:
            geo = geocode_location(query)
            st.session_state["_last_geocoded_query"] = query
            st.session_state["new_destination_lookup"] = {**geo, "query": query} if geo else None
            if geo:
                st.session_state["new_map_view_revision"] = int(st.session_state.get("new_map_view_revision", 0)) + 1
                st.session_state.pop("new_visible_bounds", None)
                st.session_state["new_map_center"] = (geo["lat"], geo["lng"])
                st.session_state["new_map_zoom_state"] = geo["zoom"]
                st.session_state["new_map_area_locked"] = False
                st.session_state["new_area_geometry"] = None
                st.session_state.pop("new_map_area_location", None)
                st.session_state.pop("_geocode_error", None)
            else:
                st.session_state["_geocode_error"] = f"Could not locate '{query}'. You can still pan and zoom the map."

        lookup = st.session_state.get("new_destination_lookup")
        locked_location = st.session_state.get("new_map_area_location")
        name_seed = locked_location[1] if locked_location else query
        name_key = "new_search_name"
        current_name = st.session_state.get(name_key, "").strip()
        auto_name = not current_name or current_name == st.session_state.get("_new_search_suggested_name")
        if auto_name:
            suggested = _suggest_search_name(name_seed, existing_names) if name_seed else ""
            st.session_state[name_key] = suggested
            st.session_state["_new_search_suggested_name"] = suggested
        workspace_name = st.text_input(
            "Search name", key=name_key, max_chars=64, placeholder="Suggested when you choose an area",
        )
        if st.session_state.get("_geocode_error"):
            st.warning(st.session_state["_geocode_error"])
        if locked_location:
            st.caption(f"📍 Search location: {locked_location[0]}")
        elif lookup:
            st.caption(f"📍 Found: {lookup.get('display_name', query)}")

        first = date.today() + timedelta(days=30)
        date_left, date_right = st.columns(2)
        check_in_key = "new_check_in"
        saved_check_in = _coerce_date(st.session_state.get(check_in_key))
        if saved_check_in is not None and (saved_check_in < date.today() or saved_check_in >= MAX_TRIP_DATE):
            st.session_state[check_in_key] = min(max(saved_check_in, date.today()), MAX_TRIP_DATE - timedelta(days=1))
        with date_left:
            check_in = st.date_input(
                "Check-in", value=first, min_value=date.today(), max_value=MAX_TRIP_DATE - timedelta(days=1), key=check_in_key,
            )
        checkout_default = _prepare_checkout_widget(
            check_in,
            checkout_key="new_check_out",
            scope="new_trip",
            default_checkout=check_in + timedelta(days=5),
        )
        with date_right:
            check_out = st.date_input(
                "Check-out",
                value=checkout_default,
                min_value=check_in + timedelta(days=1),
                max_value=MAX_TRIP_DATE,
                key="new_check_out",
            )
        _remember_trip_dates("new_trip", check_in, check_out)

        party_cols = st.columns(4)
        adults = party_cols[0].number_input("Adults", min_value=1, max_value=20, value=2, step=1, key="new_adults")
        children = party_cols[1].number_input("Children", min_value=0, max_value=20, value=0, step=1, key="new_children")
        infants = party_cols[2].number_input("Infants", min_value=0, max_value=20, value=0, step=1, key="new_infants")
        pets = party_cols[3].number_input("Pets", min_value=0, max_value=20, value=0, step=1, key="new_pets")

        currency = st.text_input("Currency", value="EUR", max_chars=3, key="new_currency").upper()
        visible_bounds = st.session_state.get("new_visible_bounds")
        bounds = visible_bounds or (lookup.get("bounds") if lookup else None)
        geometry = st.session_state.get("new_area_geometry") if locked_location else None
        if not geometry and bounds:
            geometry = {"type": "bbox", **bounds}

        if st.button("Discover listings", type="primary", key="save_new_search", disabled=not geometry):
            try:
                bounds = normalize_geometry(geometry)["bounds"]
                label, seed = locked_location or _map_search_location(bounds, lookup)
                name = (workspace_name.strip() if not auto_name else
                        _suggest_search_name(seed, [*existing_names, *list_available_searches()]))
                create_search_workspace(
                    name=name,
                    check_in=check_in.isoformat(),
                    check_out=check_out.isoformat(),
                    location_query=label,
                    ne_lat=bounds["north"],
                    ne_lng=bounds["east"],
                    sw_lat=bounds["south"],
                    sw_lng=bounds["west"],
                    zoom=st.session_state.get("new_map_zoom_state", 13),
                    currency=currency,
                    adults=int(adults),
                    children=int(children),
                    infants=int(infants),
                    pets=int(pets),
                    total_budget=None,
                    free_cancellation=False,
                    geometry=geometry,
                )
                from src.evaluation_service import list_priority_profiles, use_priority_profile
                available_profiles = list_priority_profiles()
                if available_profiles:
                    use_priority_profile(name, available_profiles[0]['name'])
                job_error = None
                try:
                    job_service = _service("src.job_service")
                    job_service.start_search_job(name, "discover", run_options={})
                except Exception as exc:
                    job_error = str(exc)
                if job_error:
                    st.session_state[f"automatic_start_error_{name}"] = job_error
                st.session_state["_pending_active_search_selection"] = name
                st.session_state["_pending_search_list_view"] = "Active"
                st.session_state["new_area_geometry"] = None
                st.session_state["new_map_area_locked"] = False
                st.session_state.pop("new_map_area_location", None)
                st.success(f"Saved search '{name}'. Discovering listings; choose your price threshold next.")
                st.rerun()
            except Exception as exc:
                st.error(f"Search was not started: {exc}")

    with col_right:
        is_locked = bool(st.session_state.get("new_map_area_locked", False))
        area_btn_col, area_status_col = st.columns(2)
        with area_btn_col:
            if not is_locked:
                if st.button("📍 Use Map Area", key="new_use_visible_area",
                             help="Lock the current map view as your search area and suggest a name."):
                    if not visible_bounds:
                        st.info("Pan or zoom the map once so its visible bounds are registered.")
                    else:
                        area = {"type": "bbox", **visible_bounds}
                        try:
                            normalize_geometry(area)
                        except ValueError as exc:
                            st.warning(str(exc))
                        else:
                            st.session_state["new_map_area_location"] = _map_search_location(visible_bounds, lookup)
                            st.session_state["new_area_geometry"] = area
                            st.session_state["new_map_area_locked"] = True
                            st.rerun()
            elif st.button("🔓 Unlock Map Area", key="new_unlock_area", help="Choose a different map area."):
                st.session_state["new_map_area_locked"] = False
                st.session_state["new_area_geometry"] = None
                st.session_state.pop("new_map_area_location", None)
                st.rerun()

        with area_status_col:
            if is_locked:
                st.success("🔒 Search area locked")
            else:
                st.caption("Pan or zoom, then click Use Map Area.")

        if "new_map_center" not in st.session_state:
            st.session_state["new_map_center"] = (float(DEFAULT_CENTER[0]), float(DEFAULT_CENTER[1]))
        if "new_map_zoom_state" not in st.session_state:
            st.session_state["new_map_zoom_state"] = 13
        else:
            st.session_state["new_map_zoom_state"] = normalize_map_zoom(st.session_state["new_map_zoom_state"], fractional=True)

        area_event = render_area_map(
            center=tuple(st.session_state["new_map_center"]),
            zoom=st.session_state["new_map_zoom_state"],
            key=f"new_search_area_map_{st.session_state.get('new_map_view_revision', 0)}",
            saved_geometry=st.session_state.get("new_area_geometry"),
            height=500,
        )
        if not is_locked:
            st.session_state["new_map_center"] = _map_center(area_event, tuple(st.session_state["new_map_center"]))
            st.session_state["new_map_zoom_state"] = _map_zoom(area_event, st.session_state["new_map_zoom_state"])
            visible_bounds = bounds_from_event(area_event)
            if visible_bounds:
                st.session_state["new_visible_bounds"] = visible_bounds


def _map_center(event: Dict[str, Any], fallback: Tuple[float, float]) -> Tuple[float, float]:
    center = event.get("center")
    try:
        if isinstance(center, dict):
            return float(center["lat"]), float(center["lng"])
        if isinstance(center, (list, tuple)):
            return float(center[0]), float(center[1])
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return fallback


def _map_zoom(event: Dict[str, Any], fallback: float) -> float:
    return normalize_map_zoom(event.get("zoom") if isinstance(event, dict) else None, default=normalize_map_zoom(fallback, fractional=True), fractional=True)


def _render_trip_editor(search_name: str, definition: Optional[Dict[str, Any]], config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    with st.expander("Trip dates, party, and search area", expanded=definition is None):
        st.caption("Changing dates, party, currency, destination, or area creates a trip revision. The processing threshold is saved separately. Old price quotes stay marked stale until refreshed.")
        current = definition or {}
        try:
            current_in = date.fromisoformat(str(current.get("check_in") or config.get("check_in")))
            current_out = date.fromisoformat(str(current.get("check_out") or config.get("check_out")))
        except (TypeError, ValueError):
            current_in = date.today() + timedelta(days=30)
            current_out = current_in + timedelta(days=5)
        current_in = min(current_in, MAX_TRIP_DATE - timedelta(days=1))
        current_out = min(current_out, MAX_TRIP_DATE)
        location = st.text_input("Destination or neighborhood", value=str(current.get("location") or config.get("location") or search_name), key=f"edit_location_{search_name}")
        d1, d2 = st.columns(2)
        check_in_key = f"edit_checkin_{search_name}"
        saved_check_in = _coerce_date(st.session_state.get(check_in_key))
        if saved_check_in is not None and saved_check_in >= MAX_TRIP_DATE:
            st.session_state[check_in_key] = MAX_TRIP_DATE - timedelta(days=1)
        with d1:
            check_in = st.date_input(
                "Check-in", value=current_in, max_value=MAX_TRIP_DATE - timedelta(days=1), key=check_in_key,
            )
        checkout_key = f"edit_checkout_{search_name}"
        checkout_default = _prepare_checkout_widget(
            check_in,
            checkout_key=checkout_key,
            scope=f"edit_trip_{search_name}",
            default_checkout=max(current_out, check_in + timedelta(days=1)),
        )
        with d2:
            check_out = st.date_input(
                "Check-out",
                value=checkout_default,
                min_value=check_in + timedelta(days=1),
                max_value=MAX_TRIP_DATE,
                key=checkout_key,
            )
        _remember_trip_dates(f"edit_trip_{search_name}", check_in, check_out)
        cols = st.columns(5)
        adults = cols[0].number_input("Adults", min_value=1, max_value=20, value=int(current.get("adults", config.get("adults", 1)) or 1), key=f"edit_adults_{search_name}")
        children = cols[1].number_input("Children", min_value=0, max_value=20, value=int(current.get("children", config.get("children", 0)) or 0), key=f"edit_children_{search_name}")
        infants = cols[2].number_input("Infants", min_value=0, max_value=20, value=int(current.get("infants", config.get("infants", 0)) or 0), key=f"edit_infants_{search_name}")
        pets = cols[3].number_input("Pets", min_value=0, max_value=20, value=int(current.get("pets", config.get("pets", 0)) or 0), key=f"edit_pets_{search_name}")
        currency = cols[4].text_input("Currency", value=str(current.get("currency") or config.get("currency") or "EUR"), max_chars=3, key=f"edit_currency_{search_name}").upper()
        bounds = (current or {}).get("bounds") or config
        north = float((bounds or {}).get("north", (bounds or {}).get("ne_lat", DEFAULT_CENTER[0])))
        south = float((bounds or {}).get("south", (bounds or {}).get("sw_lat", DEFAULT_CENTER[0])))
        east = float((bounds or {}).get("east", (bounds or {}).get("ne_long", DEFAULT_CENTER[1])))
        west = float((bounds or {}).get("west", (bounds or {}).get("sw_long", DEFAULT_CENTER[1])))
        initial_center = ((north + south) / 2, (east + west) / 2)
        session_key = f"edit_map_center_{search_name}"
        zoom_key = f"edit_map_zoom_{search_name}"
        if session_key not in st.session_state:
            st.session_state[session_key] = initial_center
        if zoom_key not in st.session_state:
            st.session_state[zoom_key] = normalize_map_zoom(config.get("zoom_value"))
        else:
            st.session_state[zoom_key] = normalize_map_zoom(st.session_state[zoom_key], fractional=True)
        area_key = f"edit_area_geometry_{search_name}"
        if area_key not in st.session_state and current.get("geometry"):
            st.session_state[area_key] = current["geometry"]
        area_event = render_area_map(
            center=tuple(st.session_state[session_key]),
            zoom=st.session_state[zoom_key],
            key=f"edit_area_map_{search_name}",
            saved_geometry=st.session_state.get(area_key),
        )
        st.session_state[session_key] = _map_center(area_event, tuple(st.session_state[session_key]))
        st.session_state[zoom_key] = _map_zoom(area_event, st.session_state[zoom_key])
        drawn = extract_drawn_geometry(area_event)
        if drawn:
            st.session_state[area_key] = drawn
        view_bounds = bounds_from_event(area_event)
        if view_bounds:
            st.session_state[f"{area_key}_visible_bounds"] = view_bounds
        bcol1, bcol2 = st.columns([1, 2])
        if bcol1.button("Use visible map area", key=f"use_visible_{search_name}"):
            visible = st.session_state.get(f"{area_key}_visible_bounds")
            if visible:
                st.session_state[area_key] = {"type": "bbox", **visible}
                st.rerun()
        bcol2.caption("The map boundary is approximate search coverage, not a building-level location guarantee.")
        if st.button("Save trip changes", type="primary", key=f"save_trip_{search_name}"):
            try:
                if not st.session_state.get(area_key):
                    raise ValueError("Please click 'Use visible map area' to select the search boundary.")
                candidate = {
                    "location": location,
                    "check_in": check_in,
                    "check_out": check_out,
                    "adults": int(adults),
                    "children": int(children),
                    "infants": int(infants),
                    "pets": int(pets),
                    "currency": currency,
                    "total_budget": None,
                    "free_cancellation": False,
                    "geometry": st.session_state[area_key],
                }
                new_definition = save_search_definition(search_name, candidate)
                st.success(f"Trip revision {new_definition['trip_revision']} saved.")
                return new_definition
            except Exception as exc:
                st.error(f"Trip changes were not saved: {exc}")
        return definition



def _render_discovery_section(search_name, snapshot, profile_name, profile, listing_count, *, archived=False):
    """Keep initial discovery, refresh and editing together at the start."""
    st.caption("Discover or update the listings for these dates, guests and map area. Then choose which properties to prepare in step 2.")
    if isinstance(snapshot, dict):
        if archived:
            stages = dict(snapshot.get('stages') or {})
            stages['discover'] = {**(stages.get('discover') or {}), 'can_start': False,
                                  'reason': 'Enable this saved search to collect listings.'}
            snapshot = {**snapshot, 'stages': stages}
        _render_pipeline_stages(search_name=search_name, snapshot=snapshot,
            profile=profile,
            listing_count=listing_count, jobs=_service("src.job_service"),
            active=_pipeline_is_active(snapshot), visible_stage_keys=("discover",), show_summary=False)
    active, status_error = _search_job_is_active(search_name)
    definition = _active_definition(search_name)
    discovered = _status_count(((snapshot or {}).get('stages') or {}).get('discover', {}).get('count'))
    if discovered or snapshot is None:
        if st.button("Refresh listings + prices", key=f"rediscover_{search_name}", disabled=archived or active or status_error or not definition):
            try:
                _service("src.job_service").start_search_job(search_name, "discover", run_options={})
                st.rerun()
            except Exception as exc:
                st.error(f"Could not refresh discovery: {exc}")
    _render_price_refresh(search_name, snapshot or {}, _service("src.job_service"), active or status_error or archived)
    st.caption("Updates: refresh discovery here to update the histogram; adjust the budget in step 2 to widen or narrow the selection. Existing evidence is retained.")


def _render_price_selection(search_name: str, definition: Optional[Dict[str, Any]], data: pd.DataFrame) -> Optional[Dict[str, Any]]:
    from src import storage_service as storage
    from src.search_service import SEARCHES_ROOT
    from src.processing_service import load_selection, select_properties
    st.markdown("**Choose a price range**")
    directory = Path(SEARCHES_ROOT) / search_name
    saved = load_selection(directory, definition) if definition else None
    active, status_error = _search_job_is_active(search_name)
    path = directory / "output_data" / "results_df.csv"
    if not definition or not storage.exists(path):
        st.info("Discover listings first. Collection and AI evaluation start after you choose a threshold.")
        return None
    frame = storage.read_frame(path, dtype={'room_id': str})
    summary = select_properties(frame, definition, None)
    prices = summary['known_prices']
    all_prices = st.checkbox("Process all prices", value=bool(saved and saved.get('max_total') is None), key=f"process_all_prices_{search_name}", disabled=active)
    histogram = st.empty()
    from src.processing_service import price_slider_limits
    slider_key = f"processing_range_slider_{search_name}_{definition['trip_revision']}"
    current_range = st.session_state.get(slider_key)
    saved_minimum = int(np.ceil(float(saved.get('min_total', 0)) / 50) * 50) if saved else 0
    _, upper, initial = price_slider_limits(prices, saved.get('max_total') if saved else None,
                                           max(current_range) if current_range else saved_minimum)
    initial = max(initial, saved_minimum)
    minimum, maximum = st.slider(f"Total stay price range ({definition['currency']})", min_value=0,
                        max_value=upper, value=(saved_minimum, initial), step=50, key=slider_key,
                        disabled=active or all_prices, format="%d")
    if not prices.empty:
        import altair as alt
        counts, edges = np.histogram(prices.to_numpy(), bins=min(20, max(1, int(np.sqrt(len(prices))))))
        included, _ = np.histogram(prices[prices.between(minimum, maximum) | all_prices].to_numpy(), bins=edges)
        bins = pd.DataFrame({'From': edges[:-1], 'To': edges[1:], 'Included': included, 'Excluded': counts-included})
        bars = alt.Chart(bins).transform_fold(['Included', 'Excluded'], as_=['Selection', 'Properties']).mark_bar().encode(
            x=alt.X('From:Q', bin='binned', title=f'Total stay price ({definition["currency"]})'),
            x2='To:Q', y=alt.Y('Properties:Q', title='Properties', stack='zero'),
            color=alt.Color('Selection:N', scale=alt.Scale(domain=['Included', 'Excluded'], range=['#16826b', '#d8dfe3'])),
            tooltip=['From:Q', 'To:Q', 'Selection:N', 'Properties:Q'],
        )
        if not all_prices:
            line = alt.Chart(pd.DataFrame({'Threshold': [minimum, maximum]})).mark_rule(color='#b74b35', strokeWidth=2).encode(x='Threshold:Q')
            bars = bars + line
        histogram.altair_chart(bars, use_container_width=True)
    st.caption(f"{summary['current_count']} currently returned · {len(prices)} usable total-stay quotes · {summary['unknown_count']} unknown/unverified. Histogram uses the latest usable stay quotes for these dates and guests (initially from discovery).")
    unknown = st.checkbox("Also fetch properties with unknown/unverified prices", value=bool(saved and saved.get('include_unknown')), key=f"processing_unknown_{search_name}", disabled=active)
    candidate = {'min_total': 0.0 if all_prices else float(minimum),
                 'max_total': None if all_prices else float(maximum), 'include_unknown': unknown}
    chosen = select_properties(frame, definition, candidate)
    st.caption(f"{len(chosen['selected_ids'])} selected for processing · {chosen['excluded_count']} outside this selection. Existing details, reviews and AI assessments are retained when you change the threshold.")
    if not data.empty and 'room_id' in data:
        from src.processing_service import recent
        selected_data = data[data['room_id'].astype(str).isin(chosen['selected_ids'])]
        counts = {label: int(selected_data.get(field, pd.Series(dtype=object)).map(recent).sum())
                  for label, field in [('details', 'details_fetched_at'), ('reviews', 'reviews_fetched_at'), ('AI assessments', 'personal_fit_evaluated_at')]}
        st.caption('Recent saved data (within 30 days): ' + ' · '.join(f'{count} {label}' for label, count in counts.items()))
    return {**candidate, 'selected_count': len(chosen['selected_ids'])}


NEW_PRIORITY_PROFILE = "＋ New profile"


PROFILE_WEIGHT_COLUMNS = dict(zip(PRIORITY_LABELS, (
    "Clean", "Quiet", "Beds", "Transit", "Bath", "Temp", "Wi-Fi", "Value",
)))


def _prime_priority_selection(service: Any, search_name: str) -> str:
    key = f"profile_select_{search_name}"
    if not callable(getattr(service, 'list_priority_profiles', None)):
        return str(st.session_state.get(key, 'Default'))
    names = [profile['name'] for profile in service.list_priority_profiles()]
    chosen = st.session_state.get(key)
    if chosen not in names:
        chosen = names[0] if names else NEW_PRIORITY_PROFILE
        st.session_state[key] = chosen
    if chosen != NEW_PRIORITY_PROFILE:
        service.use_priority_profile(search_name, chosen)
        return chosen
    return 'Default'


def _render_priority_table(search_name: str, profiles: Sequence[Dict[str, Any]], selected: str) -> None:
    names = [profile["name"] for profile in profiles]
    rows = [{
        "Use": "",
        "Profile": profile["name"],
        **{column: profile["weights"].get(key, 3) for key, column in PROFILE_WEIGHT_COLUMNS.items()},
        "Instructions": profile.get("additional_instructions", ""),
    } for profile in profiles]
    frame = pd.DataFrame(rows)
    builder = GridOptionsBuilder.from_dataframe(frame)
    builder.configure_default_column(editable=False, sortable=False, resizable=True, flex=1, minWidth=60)
    builder.configure_selection(selection_mode="single", use_checkbox=True, suppressRowDeselection=True)
    builder.configure_column("Use", width=52, minWidth=52, maxWidth=52, flex=0, pinned="left")
    builder.configure_column("Profile", flex=2, minWidth=105, tooltipField="Profile")
    for key, column in PROFILE_WEIGHT_COLUMNS.items():
        builder.configure_column(column, headerTooltip=PRIORITY_LABELS[key])
    builder.configure_column("Instructions", flex=2, minWidth=105, tooltipField="Instructions")
    options = builder.build()
    options["getRowId"] = JsCode("function(params) { return params.data.Profile; }")
    options["initialState"] = {"rowSelection": [selected]}
    options.update(rowHeight=30, headerHeight=32)
    selection_key = hashlib.sha256(json.dumps([selected, *names]).encode()).hexdigest()[:12]
    response = AgGrid(
        frame, gridOptions=options, height=min(216, 35 + 30 * len(frame)),
        key=f"priority_table_{search_name}_{selection_key}", update_mode=GridUpdateMode.NO_UPDATE,
        update_on=["selectionChanged"], allow_unsafe_jscode=True, enable_enterprise_modules=False,
        theme=AgGridTheme.STREAMLIT,
    )
    if "selectionchanged" in _grid_event_type(response.event_data):
        checked = response.selected_rows
        if checked is not None and not checked.empty:
            name = checked.iloc[0]["Profile"]
            if name in names and name != selected:
                st.session_state[f"profile_select_{search_name}"] = name
                st.session_state.pop(f"priority_editor_{search_name}", None)
                st.session_state.pop(f"delete_priority_requested_{search_name}", None)
                st.rerun()


def _render_priority_draft(service, search_name, selected, profiles, mode, shared_profiles) -> None:
    adding = mode == "add"
    source = service.default_evaluation_profile() if adding else next(p for p in profiles if p["name"] == selected)
    scope = f"{search_name}_{NEW_PRIORITY_PROFILE if adding else selected}"
    with st.form(f"priority_draft_{scope}"):
        name = st.text_input("Profile name", value="" if adding else selected, max_chars=80,
                             key=f"profile_name_{scope}").strip()
        weights = st.data_editor(
            pd.DataFrame([{column: source["weights"].get(key, 3) for key, column in PROFILE_WEIGHT_COLUMNS.items()}]),
            column_config={
                column: st.column_config.NumberColumn(
                    help=PRIORITY_LABELS[key], min_value=0, max_value=5, step=1, required=True,
                )
                for key, column in PROFILE_WEIGHT_COLUMNS.items()
            },
            hide_index=True, use_container_width=True, key=f"priority_weights_{scope}",
        )
        instructions = st.text_area("Additional instructions", value=source.get("additional_instructions", ""),
                                    height=70, key=f"instructions_{scope}")
        st.caption("Weights: 0 ignores a criterion, 5 gives it the highest priority. Use a new name to save a copy.")
        save_col, cancel_col, _ = st.columns([2, 1, 4])
        save = save_col.form_submit_button("Save profile", type="primary")
        cancel = cancel_col.form_submit_button("Cancel")
    editor_key = f"priority_editor_{search_name}"
    if cancel:
        st.session_state.pop(editor_key, None)
        st.rerun()
    if save:
        try:
            if not name:
                raise ValueError("Enter a profile name.")
            if (adding or name != selected) and any(profile["name"] == name for profile in profiles):
                raise ValueError("A profile with that name already exists.")
            values = {key: int(weights.iloc[0][column]) for key, column in PROFILE_WEIGHT_COLUMNS.items()}
            if shared_profiles:
                service.save_priority_profile(name, values, instructions,
                                              model_name=source.get("model_name"), reasoning_effort=source.get("reasoning_effort"))
                service.use_priority_profile(search_name, name)
            else:
                service.save_evaluation_profile(search_name, name, values, instructions, hard_filters=source.get("hard_filters"))
            st.session_state[f"profile_select_{search_name}"] = name
            st.session_state.pop(editor_key, None)
            st.rerun()
        except Exception as exc:
            st.error(f"Could not save the profile: {exc}")


def _render_profile_editor(search_name: str, *, can_run: bool = True, processing_selection: Optional[Dict[str, Any]] = None,
                           snapshot: Optional[Dict[str, Any]] = None) -> str:
    """Select a saved profile, opening the editor only on demand."""
    try:
        service = _service("src.evaluation_service")
        shared_profiles = callable(getattr(service, 'list_priority_profiles', None))
        if shared_profiles:
            profiles = service.list_priority_profiles()
        else:
            profiles = [service.load_evaluation_profile(search_name, name)
                        for name in service.list_evaluation_profiles(search_name)]
    except Exception as exc:
        st.error(f"Could not load evaluation profiles: {exc}")
        return "Default"

    names = [profile["name"] for profile in profiles]
    selected = st.session_state.get(f"profile_select_{search_name}", "Default")
    if names:
        selected = selected if selected in names else names[0]
        st.markdown(f"**Evaluation profiles** · Using **{selected}**")
        _render_priority_table(search_name, profiles, selected)
    else:
        st.markdown("**Evaluation profiles**")
        st.caption("Add a profile to set the priorities you want to reuse.")
    chosen = selected in names
    editor_key = f"priority_editor_{search_name}"
    delete_key = f"delete_priority_requested_{search_name}"
    add_col, edit_col, delete_col, _ = st.columns([1, 1, 1, 4])
    if add_col.button("Add", key=f"add_profile_{search_name}"):
        st.session_state[editor_key] = "add"
        st.session_state.pop(delete_key, None)
    if edit_col.button("Edit", key=f"edit_profile_{search_name}", disabled=not chosen):
        st.session_state[editor_key] = "edit"
        st.session_state.pop(delete_key, None)
    if _colored_button(delete_col, "Delete", key=f"delete_profile_{search_name}", disabled=not (shared_profiles and chosen)):
        st.session_state[delete_key] = selected
        st.session_state.pop(editor_key, None)

    if chosen and st.session_state.get(delete_key) == selected:
        st.caption(f"Delete the reusable profile ‘{selected}’? Previous evaluations are retained.")
        confirm, cancel, _ = st.columns([2, 1, 4])
        if _colored_button(confirm, "Confirm delete", key=f"confirm_delete_profile_{search_name}"):
            try:
                service.delete_priority_profile(selected)
                st.session_state.pop(f"profile_select_{search_name}", None)
                st.session_state.pop(delete_key, None)
                st.rerun()
            except Exception as exc:
                st.error(f"Could not delete the profile: {exc}")
        if cancel.button("Cancel", key=f"cancel_delete_profile_{search_name}"):
            st.session_state.pop(delete_key, None)
            st.rerun()

    mode = st.session_state.get(editor_key)
    if mode == "add" or (mode == "edit" and chosen):
        _render_priority_draft(service, search_name, selected, profiles, mode, shared_profiles)

    profile_file = Path(service.PROJECT_ROOT) / "searches" / search_name / "evaluation_profiles.json"
    saved = chosen and (shared_profiles or profile_file.exists())
    st.subheader("3. Prepare selected properties")
    st.caption("Fetch details and recent reviews, then evaluate with your saved priorities. The run continues automatically with bounded retries; recent saved work is reused.")
    run_options = _automatic_run_options(search_name)
    active_job, status_error = _search_job_is_active(search_name)
    from src.processing_service import load_selection, save_selection
    from src.search_service import SEARCHES_ROOT
    definition = _active_definition(search_name)
    directory = Path(SEARCHES_ROOT) / search_name
    committed = load_selection(directory, definition) if definition and processing_selection else None
    same_selection = bool(committed and processing_selection and all(
        committed.get(key, default) == processing_selection.get(key, default)
        for key, default in (("min_total", 0), ("max_total", None), ("include_unknown", False))
    ))
    stages = (snapshot or {}).get("stages", {})
    collection = stages.get("details", {})
    evaluation = stages.get("evaluate", {})
    stage = "prepare"
    action = "Fetch selected + evaluate"
    if same_selection and evaluation.get("can_start") and collection.get("state") in {"success", "partial", "failed", "cancelled"}:
        stage = "evaluate"
        action = ("Retry evaluation" if evaluation.get("failed")
                  else "Evaluate ready listings" if collection.get("state") != "success"
                  else "Evaluate listings")
    elif same_selection and collection.get("state") in {"partial", "failed", "cancelled"}:
        action = "Retry preparation"
    if st.button(action, key=f"run_all_{search_name}", type="primary",
                 disabled=not chosen or bool(mode) or not can_run or active_job or status_error
                 or not processing_selection or not processing_selection.get("selected_count"),
                 help="Uses the checked profile and reuses saved collection and evaluation work."):
        try:
            if not saved:  # Older workspaces can offer a Default that has never been saved.
                profile = service.load_evaluation_profile(search_name, selected)
                service.save_evaluation_profile(search_name, selected, profile["weights"],
                                                profile["additional_instructions"], hard_filters=profile.get("hard_filters"))
            if stage == "prepare":
                save_selection(directory, definition, processing_selection)
            _service("src.job_service").start_search_job(search_name, stage, profile_name=selected, run_options=run_options)
            st.rerun()
        except Exception as exc:
            st.error(f"Could not start {action.lower()}: {exc}")
    return selected if chosen else "Default"


def _render_job_controls(
    search_name: str,
    profile_name: str,
    profile: Optional[Dict[str, Any]] = None,
    listing_count: int = 0,
    initial_snapshot: Optional[Dict[str, Any]] = None,
) -> None:
    """Poll collection/AI progress below the automatic preparation action."""
    try:
        status_service = _service("src.pipeline_status")
        jobs = _service("src.job_service")
    except ImportError:
        st.warning("Pipeline progress is not available in this build. Saved search data remains usable.")
        return
    try:
        if initial_snapshot is None:
            initial_snapshot = status_service.get_pipeline_status(search_name, profile_name=profile_name)
    except Exception as exc:
        st.error(f"Could not read pipeline progress: {exc}")
        initial_snapshot = None

    if not isinstance(initial_snapshot, dict):
        initial_snapshot = {
            "active_job": None,
            "next_stage": None,
            "stages": {
                key: {"state": "blocked", "can_start": False, "reason": "Pipeline status is unavailable."}
                for key in ("discover", "details", "evaluate")
            },
        }

    active = _pipeline_is_active(initial_snapshot)
    first_render = {"pending": True}
    was_active_key = f"_pipeline_was_active_{search_name}"

    @st.fragment(run_every=1 if active else None)
    def render_pipeline_fragment() -> None:
        if first_render["pending"]:
            snapshot = initial_snapshot
            first_render["pending"] = False
        else:
            try:
                snapshot = status_service.get_pipeline_status(search_name, profile_name=profile_name)
            except Exception as exc:
                st.error(f"Could not refresh pipeline progress: {exc}")
                return

        is_active = _pipeline_is_active(snapshot)
        if is_active:
            st.session_state[was_active_key] = True
        elif st.session_state.get(was_active_key, False):
            # A full-app rerun reloads the finished CSV snapshot once, then
            # clears the transition flag so the terminal state does not loop.
            st.session_state[was_active_key] = False
            st.rerun(scope="app")

        _render_pipeline_stages(
            search_name=search_name,
            snapshot=snapshot,
            profile=profile,
            listing_count=listing_count,
            jobs=jobs,
            active=is_active,
            visible_stage_keys=("details", "evaluate"),
        )
        if is_active and _colored_button(st, "Cancel active job", key=f"cancel_job_{search_name}"):
            try:
                jobs.cancel_search_job(search_name)
                st.rerun(scope="app")
            except Exception as exc:
                st.error(f"Cancellation could not be requested: {exc}")

    render_pipeline_fragment()


def _render_price_refresh(search_name: str, snapshot: Dict[str, Any], jobs: Any, disabled: bool) -> None:
    """Keep the optional price-only refresh with other search updates."""
    with st.expander("Update collected prices", expanded=False):
        st.caption("Recheck prices and availability for the saved processing selection. Recent property information and reviews are reused; this does not run AI. To change the returned listings or histogram, use Refresh listings + prices in step 1.")
        has_discovery = _status_count((snapshot.get("stages") or {}).get("discover", {}).get("count"))
        from src.processing_service import load_selection
        from src.search_service import SEARCHES_ROOT
        definition = _active_definition(search_name)
        has_selection = bool(definition and load_selection(Path(SEARCHES_ROOT) / search_name, definition))
        if not has_selection:
            st.caption("Choose your budget in step 2 and start preparation in step 3 before updating selected prices.")
        if st.button("Refresh prices + availability", key=f"start_{search_name}_refresh",
                     disabled=disabled or not has_discovery or not has_selection, use_container_width=True):
            try:
                jobs.start_search_job(search_name, "refresh", run_options={})
                st.rerun(scope="app")
            except Exception as exc:
                st.error(f"Could not start price refresh: {exc}")


def _status_count(value: Any) -> Optional[int]:
    """Convert a status count to a nonnegative integer without trusting input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return max(0, int(number))


def _pipeline_is_active(snapshot: Dict[str, Any]) -> bool:
    active_job = snapshot.get("active_job")
    job_state = str((active_job or {}).get("status", "")).lower() if isinstance(active_job, dict) else ""
    if job_state in ACTIVE_JOB_STATES:
        return True
    stages = snapshot.get("stages") or {}
    return any(str((stages.get(key) or {}).get("state", "")).lower() == "running" for key in ("discover", "details", "evaluate"))


def _stage_status_label(state: str, is_next: bool) -> str:
    labels = {
        "blocked": "🔒 Locked",
        "ready": "➡️ Ready next" if is_next else "⏳ Waiting",
        "running": "⏳ Running",
        "success": "✅ Complete",
        "partial": "⚠️ Partial",
        "failed": "❌ Failed",
        "cancelled": "⚠️ Cancelled",
        "stale": "⚠️ Stale",
        "no_matches": "⚠️ No matches",
    }
    return labels.get(state, "⏳ Pending")


def _retry_countdown_message(job, now=None):
    """Only a live worker's scheduled wait can promise an automatic retry."""
    if not isinstance(job, dict) or job.get('status') != 'running':
        return None
    wait = job.get('request_wait') or {}
    if wait.get('kind') != 'retry':
        return None
    try:
        until = float(wait['until'])
        if not math.isfinite(until):
            return None
        remaining = max(0, math.ceil(until-(datetime.now().timestamp() if now is None else now)))
    except (KeyError, ValueError, TypeError):
        return None
    resume = job.get('collection_retry') or {}
    suffix = ''
    if resume.get('state') == 'waiting':
        maximum = (job.get('run_options') or {}).get('max_resume_rounds', 2)
        count = len(resume.get('room_ids') or [])
        suffix = f" Resume round {resume.get('round', 1)} of {maximum} · {count} listing(s) remaining."
    elif wait.get('message'):
        suffix = f" {wait['message']}."
    if remaining == 0:
        return 'Automatic retry is due; waiting for the worker to resume.' + suffix
    minutes, seconds = divmod(remaining, 60)
    return f'Automatic retry in {minutes:02d}:{seconds:02d}.' + suffix + ' Checkpoints are saved.'


def _render_stage_counts(stage_key: str, stage: Dict[str, Any], active_job: Optional[Dict[str, Any]] = None) -> None:
    count = _status_count(stage.get("count"))
    total = _status_count(stage.get("total"))
    completed = _status_count(stage.get("completed"))
    failed = _status_count(stage.get("failed"))
    missing = _status_count(stage.get("missing"))

    if stage_key == "discover":
        if stage.get("state") == "running":
            previous_count = _status_count(stage.get("previous_count"))
            if previous_count:
                st.caption(f"Previous saved results: {previous_count} listings.")
        elif count is not None:
            st.caption(f"{count} listings found.")
    elif stage_key == "details":
        if count is not None:
            st.caption(f"{count} of {total} listings processed." if total is not None else f"{count} listings processed.")
        for section, label in (("details", "Details"), ("reviews", "Reviews")):
            coverage = stage.get(section)
            if not isinstance(coverage, dict):
                continue
            attempted = _status_count(coverage.get("attempted"))
            succeeded = _status_count(coverage.get("succeeded"))
            unavailable = _status_count(coverage.get("unavailable"))
            section_failed = _status_count(coverage.get("failed"))
            section_missing = _status_count(coverage.get("missing"))
            unattempted = _status_count(coverage.get("unattempted"))
            parts = []
            if succeeded is not None:
                parts.append(f"{succeeded} ready")
            if attempted is not None:
                parts.append(f"{attempted} attempted")
            if unavailable is not None and unavailable:
                parts.append(f"{unavailable} unavailable")
            if section_failed is not None:
                parts.append(f"{section_failed} failed")
            if section_missing is not None:
                parts.append(f"{section_missing} missing")
            if unattempted:
                parts.append(f"{unattempted} not attempted")
            unclassified = _status_count(coverage.get("unclassified_failures"))
            if unclassified:
                parts.append(f"{unclassified} unclassified failures")
            if parts:
                st.caption(f"{label}: " + " · ".join(parts))
    elif stage_key == "evaluate":
        evaluated = _status_count(stage.get("evaluated_count"))
        if evaluated is None:
            evaluated = count
        scored = _status_count(stage.get("scored_count"))
        insufficient = _status_count(stage.get("insufficient_evidence_count"))
        if evaluated is not None:
            st.caption(f"Current-profile assessments valid: {evaluated}" + (f" of {total}." if total is not None else "."))
        if scored is not None or insufficient is not None:
            score_count = scored if scored is not None else 0
            unknown_count = insufficient if insufficient is not None else 0
            st.caption(f"{score_count} scored · {unknown_count} insufficient evidence")
        elif count is not None:
            st.caption(f"Current-profile scores: {count}" + (f" of {total}." if total is not None else "."))
        if failed is not None or missing is not None:
            parts = []
            if failed is not None:
                parts.append(f"{failed} failed")
            if missing is not None:
                parts.append(f"{missing} missing")
            st.caption("Evaluation coverage: " + " · ".join(parts))
        stale = _status_count(stage.get("stale"))
        if stale:
            st.caption(f"{stale} previous-profile or stale assessments.")

    if stage.get("state") == "running":
        active_stage = str((active_job or {}).get("current_stage") or (active_job or {}).get("stage") or "")
        progress_total = _status_count((active_job or {}).get("total")) if active_stage in {stage_key, "collect_and_evaluate"} else None
        progress_done = _status_count((active_job or {}).get("completed")) if active_stage in {stage_key, "collect_and_evaluate"} else None
        if stage_key == "discover":
            st.caption("Searching… The new result count will appear when discovery finishes.")
        elif progress_total and progress_done is not None:
            st.progress(min(progress_done / progress_total, 1.0))
            st.caption(f"{progress_done} of {progress_total} processed in this stage.")
        else:
            st.caption("Work is running; waiting for progress counts.")
    elif stage.get("state") == "partial":
        parts = []
        if stage_key == "discover" and count is not None:
            parts.append(f"{count} listings found")
        elif stage_key == "details" and count is not None:
            parts.append(f"{count} of {total} processed" if total is not None else f"{count} processed")
        elif stage_key == "evaluate":
            evaluated = _status_count(stage.get("evaluated_count"))
            if evaluated is None:
                evaluated = count
            if evaluated is not None:
                parts.append(f"{evaluated} valid assessments")
        if failed is not None:
            parts.append(f"{failed} failed")
        if parts:
            st.caption("Partial result: " + " · ".join(parts) + ".")

    reason = stage.get("reason")
    job = stage.get("job") if isinstance(stage.get("job"), dict) else {}
    last_attempt = stage.get("last_attempt") if isinstance(stage.get("last_attempt"), dict) else {}
    message = job.get("error") or job.get("message") or last_attempt.get("error") or last_attempt.get("message")
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if not result and isinstance(last_attempt.get("result"), dict):
        result = last_attempt["result"]
    countdown = _retry_countdown_message(active_job)
    active_stage = str((active_job or {}).get('current_stage') or (active_job or {}).get('stage') or '')
    if countdown and active_stage == stage_key:
        st.warning(countdown)
    elif stage.get("rate_limited") or result.get("rate_limited") or str(result.get("status", "")).lower() == "rate_limited":
        if (active_job or {}).get('status') in {'running', 'queued'} and active_stage == stage_key:
            st.warning('The provider rate limited this stage. Checkpoints are saved; the worker is checking or resuming remaining work.')
        else:
            st.warning('The provider rate limited this stage. No automatic retry is scheduled. Checkpoints are saved. Retry preparation to resume remaining work.')
    if reason and stage.get("state") in {"blocked", "failed", "cancelled", "stale", "partial", "no_matches"}:
        st.caption(str(reason))
    if message and stage.get("state") in {"failed", "cancelled", "partial", "no_matches"} and message != reason:
        st.caption(str(message))


def _render_pipeline_stages(
    *,
    search_name: str,
    snapshot: Dict[str, Any],
    profile: Optional[Dict[str, Any]],
    listing_count: int,
    jobs: Any,
    active: bool,
    visible_stage_keys: Sequence[str] = ("discover", "details", "evaluate"),
    show_summary: bool = True,
) -> None:
    """Show the requested operations within the page's numbered workflow."""
    stages = snapshot.get("stages") or {}
    next_stage = snapshot.get("next_stage")
    active_job = snapshot.get("active_job") if isinstance(snapshot.get("active_job"), dict) else None
    if show_summary:
        if active and active_job:
            label = str(active_job.get("current_stage") or active_job.get("stage") or "Pipeline").replace("_", " ").title()
            countdown = _retry_countdown_message(active_job)
            st.info(f"Running now: {label}. {countdown or active_job.get('message') or ''}".strip())
        elif active:
            st.info("A background stage is running.")
        elif all((stages.get(key) or {}).get("state") == "success" for key in ("discover", "details", "evaluate")):
            st.success("Preparation is complete for the current trip and profile. Continue to step 4 to review and share.")
        elif snapshot.get("pending_collection_room_ids") and (stages.get("evaluate") or {}).get("state") == "success":
            st.info("AI evaluated all ready listings. Retry preparation for the listings marked with an asterisk.")

    columns = st.columns(len(visible_stage_keys))
    definitions = [
        ("discover", "Discover listings", "Find listings inside the saved search area."),
        ("details", "Fetch details + reviews", "Collect listing details and review evidence."),
        ("evaluate", "AI evaluation", "Score evidence using the selected saved profile."),
    ]
    definitions = [entry for entry in definitions if entry[0] in visible_stage_keys]
    for column, (stage_key, title, description) in zip(columns, definitions):
        stage = stages.get(stage_key) if isinstance(stages.get(stage_key), dict) else {}
        state = str(stage.get("state", "blocked")).lower()
        is_next = stage_key == next_stage
        with column.container(border=True):
            st.markdown(f"**{title}**")
            stage_status_label = _stage_status_label(state, is_next)
            if state == "success":
                st.success(stage_status_label)
            elif state == "failed":
                st.error(stage_status_label)
            elif state in {"partial", "cancelled", "stale", "no_matches"}:
                st.warning(stage_status_label)
            elif state == "running":
                st.info(stage_status_label)
            elif state == "ready" and is_next:
                st.info(stage_status_label)
            else:
                st.caption(stage_status_label)
            st.caption(description)
            if stage_key == "evaluate":
                effective_profile = profile or {}
                scope_count = _status_count(stage.get('eligible'))
                if scope_count is None:
                    scope_count = _status_count(stage.get('total'))
                if scope_count is None:
                    scope_count = listing_count
                st.caption(
                    f"Model: {effective_profile.get('model_name') or 'provider default'} · "
                    f"reasoning effort: {effective_profile.get('reasoning_effort') or 'provider default'} · "
                    f"{scope_count} ready for AI · {listing_count} in the saved results."
                )
            _render_stage_counts(stage_key, stage, active_job)
            if stage_key != "discover" or state == "success":
                continue
            allowed = bool(stage.get("can_start")) and not active
            disabled_reason = "Wait for the active job to finish." if active else stage.get("reason")
            button_label = "Retry discovery" if state in {"partial", "failed", "cancelled", "no_matches"} else title
            if st.button(
                button_label,
                key=f"pipeline_step_{search_name}_{stage_key}",
                disabled=not allowed,
                type="primary" if allowed else "secondary",
                use_container_width=True,
                help=None if allowed else disabled_reason,
            ):
                try:
                    jobs.start_search_job(search_name, "discover", run_options={})
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not start {title.lower()}: {exc}")


def _trip_summary(definition: Optional[Dict[str, Any]], search_name: str) -> str:
    if not definition:
        return f"{search_name} · legacy trip context"
    party = f"{definition['adults']} adults, {definition['children']} children, {definition['infants']} infants"
    if definition.get("pets"):
        party += f", {definition['pets']} pets"
    return f"{definition.get('location') or search_name} · {definition['check_in']} → {definition['check_out']} · {party}"


def _load_overlay(path_value: Any) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """Load a user-configured local map overlay without making network requests."""
    if _missing(path_value) or not str(path_value).strip():
        return None, None
    path = Path(str(path_value)).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    try:
        path = path.resolve(strict=True)
        if not path.is_file():
            return None, f"Configured map overlay is not a file: {path}"
        frame = pd.read_csv(path)
        required = {"Station", "Latitude", "Longitude"}
        if not required.issubset(frame.columns):
            return None, f"Map overlay {path.name} needs Station, Latitude, and Longitude columns."
        frame["Latitude"] = pd.to_numeric(frame["Latitude"], errors="coerce")
        frame["Longitude"] = pd.to_numeric(frame["Longitude"], errors="coerce")
        frame = frame.dropna(subset=["Latitude", "Longitude"])
        return frame, None
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        return None, f"Could not read configured map overlay: {exc}"


def _quote_label(row: pd.Series, currency: str, definition: Optional[Dict[str, Any]]) -> str:
    status = str(row.get("quote_status_ui", "missing"))
    amount = pd.to_numeric(pd.Series([row.get("total_price")]), errors="coerce").iloc[0]
    if pd.isna(amount) or not math.isfinite(float(amount)) or float(amount) <= 0:
        return f"Airbnb quoted stay amount unavailable ({status})"
    amount_text = f"{currency} {float(amount):,.2f}"
    if status == "current":
        nights = 0
        if definition:
            try:
                nights = (date.fromisoformat(definition["check_out"]) - date.fromisoformat(definition["check_in"])).days
            except (KeyError, TypeError, ValueError):
                nights = 0
        night_context = f" · {nights} nights · average {currency} {float(amount) / nights:,.2f}/night" if nights > 0 else ""
        return f"Airbnb quoted stay total: {amount_text}{night_context}"
    return f"Previously quoted Airbnb stay amount: {amount_text} · {status}; refresh to update this trip's quote"


def _quote_metadata_lines(row: pd.Series) -> List[str]:
    """Show quote provenance and fee limits without implying a checkout total."""
    source = str(_value(row, "quote_source", default=""))
    if source.startswith("details:"):
        source_line = f"Source: Airbnb detail response · {source.removeprefix('details:')}"
    elif source.startswith("discovery_fallback:"):
        source_line = f"Source: Airbnb search response · detail total unavailable ({source.removeprefix('discovery_fallback:')})"
    elif source.startswith("discovery:"):
        source_line = f"Source: Airbnb search response · {source.removeprefix('discovery:')}"
    elif source:
        source_line = f"Source: {source}"
    else:
        source_line = "Source: not recorded in saved quote metadata"

    completeness = str(_value(row, "quote_total_completeness", default="")).strip().casefold()
    if completeness in {"provider_breakdown_total", "provider_breakdown_total_tax_completeness_unknown", "provider_total_tax_completeness_unknown", "provider_qualified_total_tax_completeness_unknown", "saved_explicit_detail_total", "explicit_total_price_field", "unknown", ""}:
        fees_line = "Taxes and fees: inclusion is not confirmed by the saved quote; check Airbnb before booking."
    else:
        fees_line = f"Taxes and fees: saved source completeness is {completeness.replace('_', ' ')}; check Airbnb's checkout amount before booking."

    retrieved = _value(row, "details_fetched_at", "quote_fetched_at", "discovered_at")
    lines = [source_line, fees_line]
    if retrieved is not None and not pd.isna(retrieved):
        lines.append(f"Retrieved: {retrieved}")
    reason = _value(row, "quote_recovery_reason")
    if reason is not None and not pd.isna(reason):
        lines.append(f"Quote note: {reason}")
    return lines


def _render_evidence(row: pd.Series, key: str, label: str) -> None:
    fact = _parse_nested(row.get(key))
    if not isinstance(fact, dict):
        st.write(f"**{label}:** unknown")
        return
    state = str(fact.get("state", "unknown")).replace("_", " ")
    suffix = " · conflicting evidence" if fact.get("conflict") else ""
    st.write(f"**{label}:** {state}{suffix}")
    evidence = fact.get("evidence") or []
    for source in evidence[:3]:
        if isinstance(source, dict):
            st.caption(f"{source.get('source', 'source')}: {source.get('text', '')}")


def _airbnb_url(room_id: str, definition: Optional[Dict[str, Any]]) -> str:
    params = {}
    if definition:
        params = {
            "check_in": definition.get("check_in"),
            "check_out": definition.get("check_out"),
            "adults": definition.get("adults"),
            "children": definition.get("children"),
            "infants": definition.get("infants"),
            "pets": definition.get("pets"),
        }
        params = {key: value for key, value in params.items() if value is not None}
    base = f"https://www.airbnb.com/rooms/{quote(str(room_id), safe='')}"
    return base + ("?" + urlencode(params) if params else "")


def _render_details(
    row: Optional[pd.Series],
    *,
    search_name: str,
    definition: Optional[Dict[str, Any]],
    matching_ids: Sequence[str],
    visible_ids: Sequence[str],
    all_current_ids: Sequence[str],
    curation: Dict[str, Any],
) -> None:
    st.subheader("Listing details")
    if row is None:
        st.info("Select a row or map marker to inspect one listing.")
        return
    room_id = str(row.get("room_id", ""))
    title = str(_value(row, "name", "title", default="Listing"))
    currency = str((definition or {}).get("currency", "EUR"))
    st.markdown(f"#### {title}")
    st.caption(f"Listing ID {room_id}")
    availability = str(_value(row, "availability_state", default="unknown")).lower()
    if availability == "unavailable" or str(row.get("quote_status_ui", "")).lower() == "unavailable":
        st.error("Unavailable for the selected trip dates according to the latest detail check.")
    elif availability in {"unknown", "nan", ""}:
        st.caption("Availability has not been verified for this trip.")
    st.write(_quote_label(row, currency, definition))
    for quote_metadata in _quote_metadata_lines(row):
        st.caption(quote_metadata)
    rating = row.get("Airbnb_rating")
    rating_text = f"★ {float(rating):.2f}" if pd.notna(rating) else "Rating unavailable"
    capacity = row.get("person_capacity")
    capacity_text = f"Sleeps {int(capacity)}" if pd.notna(capacity) else "Capacity unknown"
    st.write(f"{rating_text} · {capacity_text}")
    home_type = str(_value(row, "accommodation_type", default="Unknown"))
    bed_count = pd.to_numeric(pd.Series([row.get("bed_count")]), errors="coerce").iloc[0]
    bedroom_count = pd.to_numeric(pd.Series([row.get("bedroom_count")]), errors="coerce").iloc[0]
    beds_text = str(int(bed_count)) if pd.notna(bed_count) and float(bed_count).is_integer() else (f"{bed_count:g}" if pd.notna(bed_count) else "Unknown")
    bedrooms_text = str(int(bedroom_count)) if pd.notna(bedroom_count) and float(bedroom_count).is_integer() else (f"{bedroom_count:g}" if pd.notna(bedroom_count) else "Unknown")
    beds_label = "bed" if pd.notna(bed_count) and float(bed_count) == 1 else "beds"
    bedrooms_label = "bedroom" if pd.notna(bedroom_count) and float(bedroom_count) == 1 else "bedrooms"
    cancellation = str(_value(row, "cancellation_state", default="unknown")).lower()
    cancellation_labels = {
        "free": "Free cancellation",
        "conditional": "Conditional cancellation terms",
        "not_free": "Not free; refund terms unknown",
        "nonrefundable": "Nonrefundable",
        "unknown": "Unknown cancellation terms",
    }
    st.write(f"{home_type} · {beds_text} {beds_label} · {bedrooms_text} {bedrooms_label} · {cancellation_labels.get(cancellation, 'Unknown cancellation terms')}")
    with st.expander("Accommodation and cancellation evidence", expanded=False):
        _render_evidence(row, "accommodation_type_evidence", "Home type")
        _render_evidence(row, "bed_count_evidence", "Beds")
        _render_evidence(row, "bedroom_count_evidence", "Bedrooms")
        _render_evidence(row, "cancellation_evidence", "Cancellation")
    fit_status = str(_value(row, "personal_fit_status", default="missing"))
    personal_fit = _value(row, "personal_fit", "personal_fit_score", "evaluation_score")
    if fit_status == "stale":
        prior = _value(row, "personal_fit_prior_score", "personal_fit_score")
        if prior is not None and pd.notna(prior):
            st.caption(f"Prior personalized fit: {float(prior):.2f} / 5 · stale for the selected profile or current source evidence")
    elif fit_status == "scored" and personal_fit is not None:
        coverage = _value(row, "coverage", "personal_fit_coverage", "evaluation_coverage")
        st.metric("Personal fit", f"{float(personal_fit):.2f} / 5", help=f"Evidence coverage: {float(coverage):.0%}" if coverage is not None and pd.notna(coverage) else None)
        profile = _value(row, "personal_fit_profile")
        if profile:
            st.caption(f"Profile: {profile}")
        model = _value(row, "personal_fit_model")
        effort = _value(row, "personal_fit_reasoning_effort")
        if model or effort:
            st.caption("Evaluation settings: " + " · ".join(str(value) for value in (model, effort) if value))
        breakdown = _parse_nested(_value(row, "personal_fit_breakdown"))
        if isinstance(breakdown, dict):
            with st.expander("Personal fit by priority", expanded=False):
                for criterion, finding in breakdown.items():
                    if not isinstance(finding, dict):
                        continue
                    st.markdown(f"**{PRIORITY_LABELS.get(criterion, criterion.replace('_', ' ').title())}** · {finding.get('score', 'not assessed')}")
                    if finding.get("rationale"):
                        st.caption(str(finding["rationale"]))
                    for evidence in (finding.get("evidence") or [])[:2]:
                        if isinstance(evidence, dict):
                            source_id = evidence.get("source_id") or "evidence"
                            st.write(f"> {evidence.get('quote', '')}  \n> — {source_id}")
    elif pd.notna(row.get("AI_rating")):
        st.caption(f"Legacy AI rating: {float(row['AI_rating']):.2f} · not a personalized fit score")

    photos = row.get("images_details")
    if _missing(photos) or not photos:
        photos = row.get("images")
    photos = _parse_nested(photos)
    photo_urls = []
    if isinstance(photos, (list, tuple)):
        photo_urls = [str(item.get("url")) for item in photos if isinstance(item, dict) and item.get("url")]
    elif isinstance(photos, dict) and photos.get("url"):
        photo_urls = [str(photos["url"])]
    if photo_urls:
        st.image(photo_urls[:3], width=150)
    st.markdown("**Amenities and evidence**")
    _render_evidence(row, "bathroom_evidence", "Bathroom")
    _render_evidence(row, "ac_evidence", "Air conditioning")
    _render_evidence(row, "washer_evidence", "Clothes washer")
    with st.expander("Reviews and listing evidence", expanded=True):
        summary = _value(row, "AI_review_summary", "review_summary")
        if summary is not None:
            st.write(str(summary))
        review_text = _value(row, "reviews_text")
        if review_text is not None:
            st.caption("Stored guest-review excerpts (original language retained)")
            st.write(str(review_text)[:1600])
        review_coverage = _parse_nested(_value(row, "personal_fit_review_coverage"))
        if isinstance(review_coverage, dict):
            retrieved = review_coverage.get("reviews_retrieved_count", "unknown")
            available = review_coverage.get("reviews_available_count", "unknown")
            analyzed = review_coverage.get("reviews_analyzed_count", "unknown")
            date_min = review_coverage.get("review_date_min") or "unknown"
            date_max = review_coverage.get("review_date_max") or "unknown"
            st.caption(f"Review coverage: {retrieved} retrieved · {available} available · {analyzed} analyzed · dates {date_min} to {date_max}")
            if review_coverage.get("reviews_truncated") or review_coverage.get("details_truncated"):
                st.warning("Some review or listing evidence was truncated for this evaluation.")
            if review_coverage.get("details_truncated_fields"):
                st.caption("Truncated listing evidence fields: " + ", ".join(map(str, review_coverage["details_truncated_fields"])))
        if summary is None and review_text is None:
            st.caption("No collected review evidence is available yet.")
        themes = _parse_nested(_value(row, "review_findings", "topic_findings"))
        if isinstance(themes, (list, dict)):
            st.json(themes, expanded=False)

    st.link_button("Open on Airbnb ↗", _airbnb_url(room_id, definition), use_container_width=True)
    action1, action2 = st.columns(2)
    is_shortlisted = room_id in set(map(str, curation.get("shortlisted_room_ids", [])))
    if action1.button("★ Shortlisted" if is_shortlisted else "☆ Shortlist", key=f"shortlist_{search_name}_{room_id}", use_container_width=True):
        toggle_shortlist_property(search_name, room_id)
        st.rerun()
    if action2.button("Hide listing", key=f"hide_{search_name}_{room_id}", use_container_width=True):
        dismiss_property(search_name, room_id)
        st.session_state[f"undo_hidden_{search_name}"] = room_id
        st.session_state[f"selected_listing_{search_name}"] = None
        st.rerun()
    if st.session_state.get(f"sort_choice_{search_name}") != "Manual Order":
        st.caption("Move Up/Down switches to Manual Order and saves the full search order.")
    move_left, move_right = st.columns(2)
    if move_left.button("Move up ↑", key=f"move_up_{search_name}_{room_id}", disabled=room_id not in visible_ids, use_container_width=True):
        _move_visible_listing(search_name, room_id, "up", visible_ids, all_current_ids)
        st.session_state[f"_pending_manual_sort_{search_name}"] = True
        st.rerun()
    if move_right.button("Move down ↓", key=f"move_down_{search_name}_{room_id}", disabled=room_id not in visible_ids, use_container_width=True):
        _move_visible_listing(search_name, room_id, "down", visible_ids, all_current_ids)
        st.session_state[f"_pending_manual_sort_{search_name}"] = True
        st.rerun()


def _move_visible_listing(
    search_name: str,
    room_id: str,
    direction: str,
    visible_room_ids: Sequence[str],
    all_current_ids: Sequence[str],
) -> None:
    """Move a row in its visible neighborhood while persisting against fresh state."""
    visible = list(map(str, visible_room_ids))
    if room_id not in visible:
        return
    source = visible.index(room_id)
    target = source - 1 if direction == "up" else source + 1
    if target < 0 or target >= len(visible):
        return
    visible[source], visible[target] = visible[target], visible[source]
    reorder_visible_properties(search_name, visible, all_current_ids=all_current_ids)


def _render_hidden_drawer(search_name: str, hidden: pd.DataFrame) -> None:
    count = len(hidden)
    with st.expander(f"Hidden listings ({count})", expanded=False):
        undo_id = st.session_state.get(f"undo_hidden_{search_name}")
        if undo_id:
            u1, u2 = st.columns([1, 3])
            if u1.button("Undo last hide", key=f"undo_hide_{search_name}"):
                restore_property(search_name, str(undo_id))
                st.session_state[f"undo_hidden_{search_name}"] = None
                st.rerun()
            u2.caption(f"Listing {undo_id} was hidden and can be restored.")
        if hidden.empty:
            st.caption("No hidden listings.")
            return
        if st.button("Restore all", key=f"restore_all_{search_name}"):
            restore_all_properties(search_name)
            st.rerun()
        for _, row in hidden.iterrows():
            room_id = str(row["room_id"])
            col_name, col_action = st.columns([4, 1])
            col_name.write(f"{_value(row, 'name', 'title', default='Listing')} · {room_id}")
            if col_action.button("Restore", key=f"restore_{search_name}_{room_id}"):
                restore_property(search_name, room_id)
                st.rerun()


def _render_filters(
    search_name: str,
    definition: Optional[Dict[str, Any]],
    data: pd.DataFrame,
    profile_hard: Optional[Dict[str, Any]] = None,
    profile_name: str = "Default",
    *,
    browse: bool = True,
) -> Dict[str, Any]:
    if not browse:
        return {}
    with st.expander("Property browser filters", expanded=False):
        profile_hard = profile_hard or {}
        hard_scope = f"{search_name}_{profile_name}"
        party_size = sum(int((definition or {}).get(key, default)) for key, default in (("adults", 1), ("children", 0), ("infants", 0)))
        min_party = int(st.number_input("Minimum advertised capacity", min_value=1, max_value=20, value=max(1, party_size, int(profile_hard.get("minimum_capacity", 1) or 1)), key=f"capacity_filter_{hard_scope}"))
        if profile_hard.get("ensuite_only"):
            bathroom_default = "Ensuite only"
        elif profile_hard.get("exclusive_bathroom"):
            bathroom_default = "Exclusive bathroom"
        else:
            bathroom_default = "Any"
        bathroom_options = ["Any", "Exclusive bathroom", "Ensuite only", "Shared bathroom"]
        bathroom_mode = st.selectbox("Bathroom", bathroom_options, index=bathroom_options.index(bathroom_default), key=f"bath_filter_{hard_scope}")
        if bathroom_mode != "Any" and not data.empty:
            uncertain_bathrooms = int(data["bathroom_privacy"].isin(["Undetermined", "Conflicting Evidence"]).sum())
            st.caption(f"{uncertain_bathrooms} listings have unknown/conflicting bathroom evidence; choose Any to inspect all.")
        ac_defaults = {"any": "Any", "present": "Confirmed Present", "present_or_unknown": "Present or Unknown"}
        ac_default = ac_defaults.get(profile_hard.get("ac_mode"), "Any")
        ac_mode = st.selectbox("Air conditioning", AMENITY_MODES, index=AMENITY_MODES.index(ac_default), key=f"ac_filter_{hard_scope}")
        if ac_mode == "Confirmed Present" and not data.empty:
            unknown_ac = int(data["ac_state"].fillna("unknown").ne("present").sum())
            st.caption(f"{unknown_ac} listings are unconfirmed or conflicting; choose Present or Unknown to inspect them.")
        washer_default = ac_defaults.get(profile_hard.get("washer_mode"), "Any")
        washer_mode = st.selectbox("Clothes washer", AMENITY_MODES, index=AMENITY_MODES.index(washer_default), key=f"washer_filter_{hard_scope}")
        if washer_mode == "Confirmed Present" and not data.empty:
            unknown_washer = int(data["washer_state"].fillna("unknown").ne("present").sum())
            st.caption(f"{unknown_washer} listings are unconfirmed or conflicting; choose Present or Unknown to inspect them.")
        availability_options = ["Available or unknown", "Available only", "Include unavailable"]
        availability_mode = st.selectbox("Availability", availability_options, key=f"availability_filter_{search_name}")
        if availability_mode != "Include unavailable" and not data.empty:
            unavailable_count = int(data["quote_status_ui"].eq("unavailable").sum())
            st.caption(f"{unavailable_count} listings are known unavailable for these dates and excluded.")
        observed_types = set(data.get("accommodation_type", pd.Series(dtype=str)).dropna().astype(str))
        home_type_options = HOME_TYPE_OPTIONS + sorted(observed_types.difference(HOME_TYPE_OPTIONS))
        home_types = st.multiselect(
            "Home type",
            options=home_type_options,
            default=home_type_options,
            key=f"home_type_filter_{search_name}",
            help="Selected types are combined as alternatives. Clear all choices to show no listings.",
        )
        bed_col, bedroom_col = st.columns(2)
        min_beds = bed_col.number_input("Minimum beds", min_value=0, max_value=30, value=0, step=1, key=f"min_beds_{search_name}", help="Beds include advertised sofa beds. Zero means no minimum.")
        min_bedrooms = bedroom_col.number_input("Minimum bedrooms", min_value=0, max_value=20, value=0, step=1, key=f"min_bedrooms_{search_name}", help="Zero means no minimum.")
        bed_count_unknown = int(pd.to_numeric(data.get("bed_count", pd.Series(np.nan, index=data.index)), errors="coerce").isna().sum()) if not data.empty else 0
        bedroom_count_unknown = int(pd.to_numeric(data.get("bedroom_count", pd.Series(np.nan, index=data.index)), errors="coerce").isna().sum()) if not data.empty else 0
        include_unknown_bed_counts = st.checkbox(
            "Include listings with unknown bed counts when a minimum is set",
            value=False,
            key=f"include_unknown_bed_counts_{search_name}",
        )
        if bed_count_unknown or bedroom_count_unknown:
            st.caption(f"{bed_count_unknown} listings have unknown bed counts · {bedroom_count_unknown} have unknown bedroom counts.")
        cancellation_mode = st.selectbox(
            "Cancellation terms",
            CANCELLATION_FILTER_OPTIONS,
            key=f"cancellation_filter_{search_name}",
            help="Only explicit free-cancellation evidence matches the free option; unknown terms are never assumed free.",
        )
        if not data.empty:
            cancellation_states = data.get("cancellation_state", pd.Series("unknown", index=data.index)).fillna("unknown").astype(str).str.lower()
            cancellation_unknown = int(cancellation_states.eq("unknown").sum())
            cancellation_conditional = int(cancellation_states.eq("conditional").sum())
            st.caption(f"Cancellation evidence: {int(cancellation_states.eq('free').sum())} free · {cancellation_conditional} conditional · {cancellation_unknown} unknown.")
            if cancellation_mode == "Free cancellation offered" and int(cancellation_states.eq("free").sum()) == 0:
                st.caption("No collected listing has confirmed free-cancellation evidence. Choose Any to include unknown terms; cancellation filters do not change discovery.")
        currency = str((definition or {}).get("currency", "EUR"))
        known_prices = data.loc[data.get("quote_status_ui", pd.Series(index=data.index, dtype=str)).eq("current"), "verified_total_price"].dropna() if not data.empty else pd.Series(dtype=float)
        known_prices = pd.to_numeric(known_prices, errors="coerce")
        known_prices = known_prices[np.isfinite(known_prices) & known_prices.gt(0)]
        upper_default = max(5000.0, float(known_prices.max()) if not known_prices.empty else 5000.0)
        min_price = st.number_input(f"Minimum current Airbnb quoted amount ({currency})", min_value=0.0, value=0.0, step=50.0, key=f"min_price_{search_name}")
        max_price = st.number_input(f"Maximum current Airbnb quoted amount ({currency})", min_value=0.0, value=upper_default, step=100.0, key=f"max_price_{search_name}")
        verified_mask = (
            data.get("quote_status_ui", pd.Series("missing", index=data.index)).eq("current")
            & pd.to_numeric(data.get("verified_total_price", pd.Series(np.nan, index=data.index)), errors="coerce").gt(0)
            & np.isfinite(pd.to_numeric(data.get("verified_total_price", pd.Series(np.nan, index=data.index)), errors="coerce"))
        ) if not data.empty else pd.Series(dtype=bool)
        unknown_price_count = int((~verified_mask).sum()) if not data.empty else 0
        include_unknown_prices = st.checkbox(
            "Include listings with unknown prices",
            value=False,
            key=f"include_unknown_prices_{search_name}",
        )
        st.caption("Only positive, current Airbnb quoted amounts appear by default. Unknown, stale, or invalid quotes are excluded.")
        if unknown_price_count:
            st.caption(f"{unknown_price_count} listings have no positive, current Airbnb quote" + (" and are included for inspection." if include_unknown_prices else "; enable the checkbox to inspect them."))
        min_rating = st.slider("Minimum Airbnb rating", min_value=0.0, max_value=5.0, value=0.0, step=0.1, key=f"min_rating_{search_name}")
        sort_by = st.selectbox("Sort results", SORT_OPTIONS, index=SORT_OPTIONS.index("AI Fit"), key=f"sort_choice_{search_name}")
        top_n = st.selectbox("Listings shown", TOP_N_OPTIONS, index=1, format_func=lambda value: f"Top {TOP_N_LABELS[value]}" if value else "All", key=f"top_n_{search_name}")
        shortlist_only = st.checkbox("Shortlisted only", value=False, key=f"shortlist_only_{search_name}")
    return {
        "party_size": min_party,
        "bathroom_mode": bathroom_mode,
        "ac_mode": ac_mode,
        "washer_mode": washer_mode,
        "home_types": list(home_types),
        "min_beds": int(min_beds),
        "min_bedrooms": int(min_bedrooms),
        "include_unknown_bed_counts": bool(include_unknown_bed_counts),
        "cancellation_mode": cancellation_mode,
        "availability_mode": availability_mode,
        "min_price": float(min_price),
        "max_price": max(float(max_price), float(min_price)),
        "include_unknown_prices": bool(include_unknown_prices),
        "min_rating": float(min_rating),
        "sort_by": sort_by,
        "top_n": int(top_n),
        "shortlist_only": shortlist_only,
    }


def _render_grid(
    shown: pd.DataFrame,
    *,
    search_name: str,
    sort_by: str,
    all_current_ids: Sequence[str],
    selected_room_id: Optional[str],
) -> Tuple[Optional[str], bool]:
    if shown.empty:
        st.info("No listings match these filters.")
        return None, False
    display_columns = [
        column for column in (
            "room_id", "name", "verified_total_price", "quote_status_ui", "Airbnb_rating",
            "personal_fit", "AI_rating", "accommodation_type", "bed_count", "bedroom_count",
            "cancellation_state", "bathroom_privacy", "ac_state", "washer_state", "is_shortlisted",
        ) if column in shown.columns
    ]
    grid_data = shown[display_columns].copy()
    if "name" in grid_data and "collection_pending" in shown:
        pending = shown["collection_pending"].fillna(False)
        grid_data.loc[pending, "name"] = grid_data.loc[pending, "name"].fillna("Listing").astype(str) + " *"
    if "room_id" in grid_data.columns:
        grid_data.index = grid_data["room_id"].astype(str)
    grid_data = grid_data.rename(columns={
        "verified_total_price": "Airbnb quote",
        "quote_status_ui": "Quote status",
        "Airbnb_rating": "Airbnb rating",
        "personal_fit": "Personal fit",
        "AI_rating": "Legacy AI rating",
        "accommodation_type": "Home type",
        "bed_count": "Beds",
        "bedroom_count": "Bedrooms",
        "cancellation_state": "Cancellation",
        "bathroom_privacy": "Bathroom",
        "ac_state": "AC",
        "washer_state": "Washer",
        "is_shortlisted": "Shortlisted",
    })
    builder = GridOptionsBuilder.from_dataframe(grid_data)
    builder.configure_selection(selection_mode="single", use_checkbox=False, suppressRowDeselection=False)
    builder.configure_default_column(editable=False, filter=False, sortable=False, resizable=True)
    options = builder.build()
    options.update({
        "getRowId": JsCode("function(params) { return String(params.data.room_id); }"),
        "getRowStyle": JsCode(
            "function(params) { return String(params.data.room_id) === "
            + _js_string(selected_room_id or "")
            + " ? {backgroundColor: '#fff0f4', borderLeft: '3px solid #d3195b'} : null; }"
        ),
        "onFirstDataRendered": JsCode(
            "function(event) { var node = event.api.getRowNode(" + _js_string(selected_room_id or "")
            + "); if (node) { node.setSelected(true); event.api.ensureNodeVisible(node, 'middle'); } }"
        ),
        "onRowDataUpdated": JsCode(
            "function(event) { var node = event.api.getRowNode(" + _js_string(selected_room_id or "")
            + "); if (node) { node.setSelected(true); event.api.ensureNodeVisible(node, 'middle'); } }"
        ),
        "rowSelection": "single",
        "animateRows": True,
        "rowDragManaged": sort_by == "Manual Order",
        "suppressMoveWhenRowDragging": False,
        "suppressRowClickSelection": False,
    })
    if sort_by == "Manual Order":
        first_field = options.get("columnDefs", [{}])[0].get("field")
        if first_field:
            options["columnDefs"][0]["rowDrag"] = True
    response = AgGrid(
        grid_data,
        gridOptions=options,
        data_return_mode=DataReturnMode.AS_INPUT,
        update_mode=GridUpdateMode.NO_UPDATE,
        update_on=["selectionChanged", "rowDragEnd"],
        height=min(690, max(260, 44 * len(grid_data) + 38)),
        enable_enterprise_modules=False,
        allow_unsafe_jscode=True,
        key=f"list_grid_{search_name}_{sort_by}_{selected_room_id or 'none'}",
        theme=AgGridTheme.STREAMLIT,
    )
    event = response.event_data or {}
    event_type = _grid_event_type(event)
    order_after_drag = response.rows_id_after_sort_and_filter
    if sort_by == "Manual Order":
        try:
            drag_key = f"last_drag_signature_{search_name}"
            changed, signature = _persist_grid_drag_once(
                search_name,
                event,
                order_after_drag,
                all_current_ids,
                st.session_state.get(drag_key),
            )
            if changed:
                st.session_state[drag_key] = signature
                st.session_state[f"manual_order_changed_{search_name}"] = True
                st.rerun()
        except Exception as exc:
            st.error(f"The new order was not saved: {exc}")
    if "selectionchanged" in event_type:
        selected = response.selected_rows
        if selected is not None and not selected.empty and "room_id" in selected:
            return str(selected.iloc[0]["room_id"]), True
        return None, True
    return None, False


def _grid_event_type(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    return str(event.get("type", event.get("eventType", event.get("name", "")))).casefold()


def _persist_grid_drag_once(
    search_name: str,
    event_data: Any,
    displayed_room_ids: Optional[Sequence[Any]],
    all_current_ids: Sequence[Any],
    previous_signature: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Ignore the same AgGrid return payload after the rerun that persisted it."""
    event_type = _grid_event_type(event_data)
    if "rowdragend" not in event_type or not displayed_room_ids:
        return False, previous_signature
    visible_order = [str(room_id) for room_id in displayed_room_ids]
    signature = json.dumps([event_type, visible_order], separators=(",", ":"), ensure_ascii=False)
    if signature == previous_signature:
        return False, previous_signature
    reorder_visible_properties(search_name, visible_order, all_current_ids=all_current_ids)
    return True, signature



def _render_report_export(
    search_name: str,
    data: pd.DataFrame,
    definition: Optional[Dict[str, Any]],
    profile: Dict[str, Any],
    curation: Dict[str, Any],
) -> None:
    """Prepare an email attachment; never collect or evaluate during export."""
    from src.report_service import build_property_report

    st.markdown("**Export an interactive property report**")
    st.caption("Download one HTML file, then open it in your browser or attach it to an email. Filtering, sorting, favorites and notes work locally; street-map tiles and Airbnb links need internet.")
    title = st.text_input("Report title", value=_trip_summary(definition, search_name), key=f"report_title_{search_name}")
    scope = st.selectbox("Properties to include", ["All properties", "Top 30 by AI fit", "Shortlisted properties"], key=f"report_scope_{search_name}")
    eligible = data[~data["room_id"].astype(str).isin(set(map(str, curation.get("hidden_room_ids", []))))].copy()
    if scope == "Top 30 by AI fit":
        eligible = eligible.sort_values(["personal_fit", "verified_total_price"], ascending=[False, True], na_position="last", kind="stable").head(30)
    elif scope == "Shortlisted properties":
        eligible = eligible[eligible["room_id"].astype(str).isin(set(map(str, curation.get("shortlisted_room_ids", []))))]
    st.caption(f"{len(eligible)} properties will be included. Hidden properties are excluded. Incomplete collection and AI assessments remain labelled as unknown or stale.")
    if eligible.empty:
        st.info("No properties match this export selection.")
        return
    try:
        from src.processing_service import load_selection
        from src.search_service import SEARCHES_ROOT
        selection = load_selection(Path(SEARCHES_ROOT) / search_name, definition) if definition else None
        report_trip = dict(definition or {})
        report_trip['processing_max_total'] = selection.get('max_total') if selection else None
        html = build_property_report(eligible, title=title, search_name=search_name, definition=report_trip, profile=profile, curation=curation)
        st.download_button("Download standalone webpage", data=html, file_name=f"{search_name}_properties.html", mime="text/html", key=f"download_report_{search_name}", type="primary")
    except Exception as exc:
        st.error(f"Could not prepare the property webpage: {exc}")


def _render_existing_search(search_name: str, *, archived: bool = False) -> None:
    start_error = st.session_state.pop(f"automatic_start_error_{search_name}", None)
    if start_error:
        st.error(f"Search saved, but discovery could not start: {start_error}. Use Refresh listings + prices after correcting the problem.")
    try:
        config = load_search_config(search_name)
    except Exception as exc:
        st.error(f"Could not load search configuration: {exc}")
        config = {}
    definition = _active_definition(search_name)
    st.caption(_trip_summary(definition, search_name))
    current_profile_name = str(st.session_state.get(f"profile_select_{search_name}", "Default"))
    try:
        evaluation_service = _service("src.evaluation_service")
        current_profile_name = _prime_priority_selection(evaluation_service, search_name)
        persisted_profile = evaluation_service.load_evaluation_profile(search_name, current_profile_name)
        profile_hard_defaults = persisted_profile.get("hard_filters", {})
    except Exception:
        evaluation_service = None
        persisted_profile = {}
        profile_hard_defaults = {}
    try:
        if evaluation_service is not None:
            data = _load_prepared_snapshot(
                search_name, definition, current_profile_name, evaluation_service, persisted_profile,
            )
        else:
            raw_dataset = __import__("review_app").load_search_dataset(search_name, enrich=False)
            data = _prepare_dataset(raw_dataset, definition)
    except Exception as exc:
        st.error(f"Could not load listing results: {exc}")
        data = pd.DataFrame()
    curation_error = None
    try:
        curation = load_user_curation(search_name)
    except CurationCorruptError as exc:
        curation_error = str(exc)
        curation = {"manual_order": [], "hidden_room_ids": [], "shortlisted_room_ids": []}
    if curation_error:
        st.error(curation_error)

    if st.session_state.pop(f"_pending_manual_sort_{search_name}", False):
        st.session_state[f"sort_choice_{search_name}"] = "Manual Order"

    try:
        initial_snapshot = _service("src.pipeline_status").get_pipeline_status(search_name, profile_name=current_profile_name)
    except Exception:
        initial_snapshot = None
    st.subheader("1. Search area and listings")
    if not archived:
        updated_definition = _render_trip_editor(search_name, definition, config)
        if updated_definition and updated_definition != definition:
            st.rerun()
    _render_discovery_section(search_name, initial_snapshot, current_profile_name, persisted_profile, len(data), archived=archived)
    st.subheader("2. Budget and priorities")
    processing_selection = _render_price_selection(search_name, definition, data)
    profile_name = _render_profile_editor(
        search_name, can_run=not archived, processing_selection=processing_selection, snapshot=initial_snapshot,
    )
    try:
        effective_profile = evaluation_service.load_evaluation_profile(search_name, profile_name)
    except Exception:
        effective_profile = {}
    if archived:
        st.info("This search is archived. Restore it to collect listings or run AI evaluation; saved results remain available to browse.")
    else:
        _render_job_controls(search_name, profile_name, effective_profile, len(data),
                             initial_snapshot=initial_snapshot if profile_name == current_profile_name else None)

    with st.expander("Collector capability and coverage", expanded=False):
        try:
            capabilities = collector_capabilities()
            st.json(capabilities)
            st.caption("Provider search results are a sample with possible missing areas, rate limits, and unavailable coordinates; they are not guaranteed exhaustive.")
        except Exception as exc:
            st.caption(f"Collector capabilities could not be inspected locally: {exc}")

    st.subheader("4. Review and share")
    if data.empty:
        if archived:
            st.info("No listings are stored for this archived search. Restore it to discover an initial result set.")
        else:
            st.info("No listings are stored for this search yet. Use Discover listings above to collect an initial result set.")
        _render_hidden_drawer(search_name, pd.DataFrame())
        return

    pending = set((initial_snapshot or {}).get("pending_collection_room_ids", []))
    collection_state = ((initial_snapshot or {}).get("stages", {}).get("details") or {}).get("state")
    if pending and collection_state in {"running", "partial", "failed", "cancelled"}:
        data = data.assign(collection_pending=data["room_id"].astype(str).isin(pending))
        st.caption("* Details or reviews are pending. Retry preparation to fill the gaps.")
    _render_report_export(search_name, data, definition, effective_profile, curation)
    browse = st.checkbox("Show in-app property browser (optional)", value=False, key=f"browse_properties_{search_name}")
    filters = _render_filters(search_name, definition, data, profile_hard_defaults, profile_name, browse=browse)
    if not browse:
        return

    full_auto = _auto_sort(data, filters["sort_by"] if filters["sort_by"] != "Manual Order" else "Total Price")
    all_current_ids = full_auto["room_id"].astype(str).tolist()
    manual_order = list(map(str, curation.get("manual_order", [])))
    if filters["sort_by"] == "Manual Order":
        ensure_manual_order(search_name, all_current_ids, preferred_order=all_current_ids)
        curation = load_user_curation(search_name)

    shown, matching, hidden = _filter_listings(
        data,
        definition=definition,
        curation=curation,
        party_size=filters["party_size"],
        bathroom_mode=filters["bathroom_mode"],
        ac_mode=filters["ac_mode"],
        washer_mode=filters["washer_mode"],
        availability_mode=filters["availability_mode"],
        min_price=filters["min_price"],
        max_price=filters["max_price"],
        min_rating=filters["min_rating"],
        sort_by=filters["sort_by"],
        top_n=filters["top_n"],
        include_unknown_prices=filters["include_unknown_prices"],
        home_types=filters["home_types"],
        min_beds=filters["min_beds"],
        min_bedrooms=filters["min_bedrooms"],
        include_unknown_bed_counts=filters["include_unknown_bed_counts"],
        cancellation_mode=filters["cancellation_mode"],
    )
    if filters["shortlist_only"]:
        matching = matching[matching["is_shortlisted"]]
        shown = matching.head(filters["top_n"]) if filters["top_n"] > 0 else matching.copy()
    unknown_location = int(matching[["latitude", "longitude"]].isna().any(axis=1).sum()) if not matching.empty else 0
    st.subheader(_trip_summary(definition, search_name))
    matching_count = len(matching)
    actual_shown = len(shown)
    collected_count = len(data)
    hidden_count = len(hidden)
    st.caption(f"{actual_shown} shown of {matching_count} matching · {collected_count} collected · {hidden_count} hidden · {unknown_location} matching listings with unknown coordinates")
    if filters["sort_by"] == "AI Fit" and not data.get("personal_fit", pd.Series(dtype=float)).notna().any():
        st.info("No personalized fit assessments are stored yet. Results stay unrated for AI Fit until that stage is available.")
    elif filters["sort_by"] == "Value":
        st.caption("Value is sorted by Airbnb rating divided by the current Airbnb quote; stale quotes are unranked.")

    list_col, map_col, detail_col = st.columns([1.2, 1.5, 1.0], gap="medium")
    selected_key = f"selected_listing_{search_name}"
    selected_room_id = st.session_state.get(selected_key)
    visible_ids = set(shown["room_id"].astype(str))
    if selected_room_id and selected_room_id not in visible_ids:
        st.session_state[selected_key] = None
        selected_room_id = None
        st.info("The selected listing is no longer in the shown result set; selection was cleared.")
    with list_col:
        st.markdown("#### Results")
        if filters["sort_by"] == "Manual Order":
            st.caption("Drag a row to set its persistent position. Listings outside this view keep their relative order.")
        selected_from_grid, grid_selection_event = _render_grid(
            shown,
            search_name=search_name,
            sort_by=filters["sort_by"],
            all_current_ids=all_current_ids,
            selected_room_id=selected_room_id,
        )
        if grid_selection_event:
            if selected_from_grid != selected_room_id:
                st.session_state[selected_key] = selected_from_grid
                st.rerun()
            selected_room_id = selected_from_grid
    with map_col:
        st.markdown("#### Map")
        map_center_key = f"map_center_{search_name}"
        map_zoom_key = f"map_zoom_{search_name}"
        if map_center_key not in st.session_state:
            if definition:
                bounds = definition.get("bounds", {})
                st.session_state[map_center_key] = (
                    (float(bounds.get("north", DEFAULT_CENTER[0])) + float(bounds.get("south", DEFAULT_CENTER[0]))) / 2,
                    (float(bounds.get("east", DEFAULT_CENTER[1])) + float(bounds.get("west", DEFAULT_CENTER[1]))) / 2,
                )
            else:
                st.session_state[map_center_key] = DEFAULT_CENTER
        if map_zoom_key not in st.session_state:
            st.session_state[map_zoom_key] = normalize_map_zoom(config.get("zoom_value"))
        else:
            st.session_state[map_zoom_key] = normalize_map_zoom(st.session_state[map_zoom_key], fractional=True)
        overlay_1, overlay_1_error = _load_overlay(config.get("map_overlay_file_1"))
        overlay_2, overlay_2_error = _load_overlay(config.get("map_overlay_file_2"))
        if overlay_1_error:
            st.warning(overlay_1_error)
        if overlay_2_error:
            st.warning(overlay_2_error)
        map_event = render_listing_map(
            shown,
            center=tuple(st.session_state[map_center_key]),
            zoom=st.session_state[map_zoom_key],
            selected_room_id=selected_room_id,
            search_definition=definition,
            overlay_1_df=overlay_1,
            overlay_2_df=overlay_2,
            key=f"listing_map_{search_name}_{selected_room_id or 'none'}",
        )
        st.session_state[map_center_key] = _map_center(map_event, tuple(st.session_state[map_center_key]))
        st.session_state[map_zoom_key] = _map_zoom(map_event, st.session_state[map_zoom_key])
        unknown_map_rows = shown[shown[["latitude", "longitude"]].isna().any(axis=1)]
        st.markdown("**Location unknown**")
        unknown_key = f"unknown_location_select_{search_name}"
        unknown_ids = unknown_map_rows["room_id"].astype(str).tolist()
        unknown_names = {
            str(row["room_id"]): str(_value(row, "name", "title", default="Listing"))
            for _, row in unknown_map_rows.iterrows()
        }
        if not unknown_ids:
            st.caption("No currently shown listings have unknown coordinates.")
            if st.session_state.get(unknown_key):
                st.session_state[unknown_key] = ""
        else:
            st.caption("These results stay in the list and details, but do not have a mappable coordinate.")
            if st.session_state.get(unknown_key) not in [None, "", *unknown_ids]:
                st.session_state[unknown_key] = ""
        chosen_unknown = st.selectbox(
            "Inspect a listing without coordinates",
            [""] + unknown_ids,
            format_func=lambda value: "Choose a listing" if not value else f"{unknown_names.get(value, 'Listing')} · {value}",
            key=unknown_key,
            disabled=not unknown_ids,
        )
        if chosen_unknown:
            st.session_state[selected_key] = chosen_unknown
            selected_room_id = chosen_unknown
        clicked_id = room_id_from_tooltip(map_event.get("last_object_clicked_tooltip"))
        event_token = f"{search_name}:{clicked_id}" if clicked_id else None
        event_key = f"last_map_click_{search_name}"
        if clicked_id and clicked_id in visible_ids and event_token != st.session_state.get(event_key):
            st.session_state[event_key] = event_token
            st.session_state[selected_key] = clicked_id
            selected_room_id = clicked_id
            st.rerun()
        elif not clicked_id:
            st.session_state[event_key] = None
    selected_row = None
    if selected_room_id:
        selected_rows = data[data["room_id"].astype(str).eq(str(selected_room_id))]
        if not selected_rows.empty:
            selected_row = selected_rows.iloc[0]
    with detail_col:
        _render_details(
            selected_row,
            search_name=search_name,
            definition=definition,
            matching_ids=matching["room_id"].astype(str).tolist(),
            visible_ids=shown["room_id"].astype(str).tolist(),
            all_current_ids=all_current_ids,
            curation=curation,
        )
    _render_hidden_drawer(search_name, hidden)


def _open_new_search() -> None:
    """Select the new-search form before sidebar widgets are rendered."""
    st.session_state["active_search_selection"] = "＋ New Search"
    st.session_state["_checked_search_deletions"] = []
    st.session_state["_search_table_epoch"] = st.session_state.get("_search_table_epoch", 0) + 1


def _search_catalog(available_searches: Sequence[str]) -> List[Dict[str, Any]]:
    """Read catalog summaries, keeping older/mocked workspace listings usable."""
    records: Dict[str, Dict[str, Any]] = {}
    try:
        from src.search_service import SEARCHES_ROOT
        catalog = __import__("src.search_catalog", fromlist=["list_search_summaries"])
        list_summaries = getattr(catalog, "list_search_summaries", None)
        if callable(list_summaries):
            raw = list_summaries(root=SEARCHES_ROOT)
            if isinstance(raw, dict):
                raw = list(raw.values())
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, str):
                        summary = {"name": item}
                    elif isinstance(item, dict):
                        summary = dict(item)
                    else:
                        continue
                    name = str(summary.get("name") or summary.get("search_name") or summary.get("slug") or "").strip()
                    if name:
                        summary["name"] = name
                        records[name] = summary
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        pass

    for name in available_searches:
        name = str(name)
        records.setdefault(name, {"name": name, "location": "", "archived": False})
    return list(records.values())


def _summary_is_archived(summary: Dict[str, Any]) -> bool:
    value = summary.get("archived", False)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "archived"}
    return bool(value)


def _search_selector_label(summary: Dict[str, Any]) -> str:
    name = str(summary.get("name", ""))
    location = str(summary.get("location") or summary.get("location_query") or "Destination unknown")
    check_in = str(summary.get("check_in") or "dates unknown")
    check_out = str(summary.get("check_out") or "dates unknown")
    listing_update = _compact_catalog_date(summary.get("listings_updated_at"))
    review_update = _compact_catalog_date(summary.get("reviews_updated_at"))
    archived = " · Archived" if _summary_is_archived(summary) else ""
    return f"{location} · {check_in} → {check_out} · L {listing_update} · R {review_update} · {name}{archived}"


def _compact_catalog_date(value: Any) -> str:
    if not value:
        return "—"
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        parsed = _catalog_local_datetime(parsed)
        return f"{parsed.day} {parsed:%b}"
    except ValueError:
        return raw[:10]


def _catalog_local_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    try:
        return value.astimezone(ZoneInfo("Europe/Rome"))
    except ZoneInfoNotFoundError:
        return value


def _readable_catalog_datetime(value: Any) -> str:
    raw = str(value)
    try:
        parsed = _catalog_local_datetime(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        if "T" in raw or " " in raw:
            suffix = f" {parsed:%Z}" if parsed.tzinfo else ""
            return f"{parsed.day} {parsed:%b %Y %H:%M}{suffix}"
        return f"{parsed.day} {parsed:%b %Y}"
    except ValueError:
        return raw


def _catalog_timestamp(summary: Dict[str, Any], field: str, label: str) -> str:
    value = summary.get(field)
    if not value:
        return f"{label}: not recorded"
    value = _readable_catalog_datetime(value)
    approximate = summary.get(f"{field}_is_approximate") or summary.get(f"{field}_approximate")
    source = summary.get(f"{field}_source")
    notes = []
    if approximate:
        notes.append("approximate")
    if source:
        notes.append(f"{source} source")
    suffix = f" ({', '.join(notes)})" if notes else ""
    return f"{label}: {value}{suffix}"


def _search_job_is_active(search_name: str) -> Tuple[bool, bool]:
    """Return (active, status-read-failed) for archive/delete controls."""
    try:
        service = __import__("src.job_service", fromlist=["get_search_job"])
        getter = getattr(service, "get_search_job", None)
        if not callable(getter):
            return False, True
        job = getter(search_name)
    except Exception:
        return False, True
    state = str((job or {}).get("status", "")).casefold()
    return state in ACTIVE_JOB_STATES, False


def _clear_search_snapshot(search_name: str) -> None:
    cache = st.session_state.get(PREPARED_SNAPSHOT_CACHE_KEY)
    if not isinstance(cache, OrderedDict):
        return
    for key in list(cache):
        if isinstance(key, tuple) and len(key) > 1 and key[1] == search_name:
            cache.pop(key, None)


def _search_table_rows(summaries: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    """Use workspace IDs as stable row identities, independent of timestamps."""
    rows = []
    for item in summaries:
        updates = []
        for field in ("listings_updated_at", "reviews_updated_at", "updated_at"):
            raw = item.get(field)
            if not raw:
                continue
            try:
                stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=ZoneInfo("Europe/Rome"))
                updates.append((stamp.timestamp(), str(raw)))
            except (ValueError, TypeError):
                continue
        updated = max(updates)[1] if updates else None
        rows.append({
            "Location": item.get("location") or item["name"],
            "Stay dates": f"{item.get('check_in') or '—'} → {item.get('check_out') or '—'}",
            "People": item.get("people"),
            "Properties": item.get("properties_count"),
            "Last updated": _readable_catalog_datetime(updated) if updated else "—",
            "Search": item["name"],
        })
    return pd.DataFrame(rows, columns=["Search", "Location", "Stay dates", "People", "Last updated", "Properties"])


def _apply_search_table_event(response: Any, names: Sequence[str]) -> bool:
    """Checkbox selection controls deletion; cell clicks only open a search."""
    event = response.event_data or {}
    kind = _grid_event_type(event)
    if "selectionchanged" in kind:
        selected = response.selected_rows
        checked = [] if selected is None or selected.empty else [
            str(name) for name in selected.get("Search", []) if str(name) in names
        ]
        if checked != st.session_state.get("_checked_search_deletions", []):
            st.session_state["_checked_search_deletions"] = checked
            st.session_state.pop("_delete_search_requested", None)
            return True
    elif "cellclicked" in kind:
        data = event.get("data") or {}
        name = data.get("Search")
        if name in names and name != st.session_state.get("active_search_selection"):
            st.session_state["active_search_selection"] = name
            return True
    return False


def _render_search_table(summaries: Sequence[Dict[str, Any]], table_key: str) -> None:
    frame = _search_table_rows(summaries)
    names = frame["Search"].tolist()
    frame.insert(0, "Delete", "")
    builder = GridOptionsBuilder.from_dataframe(frame)
    builder.configure_default_column(editable=False, sortable=True, resizable=True, minWidth=75)
    builder.configure_selection(
        selection_mode="multiple", use_checkbox=True, header_checkbox=True,
        pre_selected_rows=[index for index, name in enumerate(names)
                           if name in st.session_state.get("_checked_search_deletions", [])],
        suppressRowClickSelection=True,
    )
    builder.configure_column("Delete", header_name="Delete", width=85, minWidth=85, maxWidth=85,
                             pinned="left", sortable=False, checkboxSelection=True,
                             headerCheckboxSelection=True)
    builder.configure_column("Search", width=150)
    builder.configure_column("Stay dates", width=165)
    builder.configure_column("Last updated", width=150)
    builder.configure_column("Location", width=125)
    builder.configure_column("People", width=80)
    builder.configure_column("Properties", width=95)
    options = builder.build()
    options["getRowId"] = JsCode("function(params) { return String(params.data.Search); }")
    options["suppressRowClickSelection"] = True
    response = AgGrid(
        frame, gridOptions=options, height=400, key=table_key,
        data_return_mode=DataReturnMode.AS_INPUT, update_mode=GridUpdateMode.NO_UPDATE,
        update_on=["selectionChanged", "cellClicked"],
        enable_enterprise_modules=False, allow_unsafe_jscode=True, theme=AgGridTheme.STREAMLIT,
    )
    if _apply_search_table_event(response, names):
        st.rerun()


def _render_search_panel(summaries: Sequence[Dict[str, Any]]) -> str:
    names = [str(item["name"]) for item in summaries]
    options = ["＋ New Search", *names]
    pending = st.session_state.pop("_pending_active_search_selection", None)
    if pending in options:
        st.session_state["active_search_selection"] = pending
        st.session_state["_checked_search_deletions"] = []
        st.session_state["_search_table_epoch"] = st.session_state.get("_search_table_epoch", 0) + 1
    choice = st.session_state.get("active_search_selection")
    if choice not in options:
        env_search = __import__("os").getenv("SEARCH_SUBDIR")
        choice = env_search if env_search in names else (names[0] if names else "＋ New Search")
        st.session_state["active_search_selection"] = choice
    with st.sidebar:
        st.header("Searches")
        if names:
            # Only membership/order changes recreate this widget. Collection
            # metadata may change freely without selecting another workspace.
            table_key = "search_table_" + hashlib.sha256(json.dumps(names).encode()).hexdigest()[:12] + "_" + str(st.session_state.get("_search_table_epoch", 0))
            _render_search_table(summaries, table_key)
        else:
            st.info("No saved searches yet.")
        st.caption(f"Selected: {choice if choice != '＋ New Search' else 'New search'}")
        new_col, delete_col = st.columns(2)
        if new_col.button("＋ New Search", type="primary", key="new_search_button", use_container_width=True):
            _open_new_search()
            st.session_state.pop("_delete_search_requested", None)
            st.rerun()
        selected_names = [name for name in st.session_state.get("_checked_search_deletions", []) if name in names]
        targets = selected_names
        st.caption("Check the Delete column to mark searches for deletion. Clicking other cells only opens the search.")
        statuses = [_search_job_is_active(name) for name in targets]
        active_job = any(active for active, _ in statuses)
        status_error = any(error for _, error in statuses)
        label = f"Delete checked ({len(targets)})"
        if _colored_button(delete_col, label, key="delete_search_button", use_container_width=True,
                             disabled=not targets or active_job or status_error):
            st.session_state["_delete_search_requested"] = tuple(targets)
        if active_job:
            st.caption("Wait for all selected searches' active jobs to finish before deleting them.")
        elif status_error:
            st.caption("Deletion is unavailable until every selected search's job status can be checked.")
        if targets and st.session_state.get("_delete_search_requested") == tuple(targets):
            st.warning(f"Permanently delete {len(targets)} search(es)? Their databases and saved settings will be removed. Property details and reviews are retained separately; records newer than 30 days can be reused.")
            for name in targets:
                st.text(name)
            confirm, cancel = st.columns(2)
            if _colored_button(confirm, "Confirm Delete", key="confirm_delete_search", disabled=active_job or status_error, type="primary"):
                try:
                    from src.search_service import SEARCHES_ROOT
                    from src.search_catalog import delete_searches
                    delete_searches(targets, confirmation=targets, root=SEARCHES_ROOT)
                    for name in targets:
                        _clear_search_snapshot(name)
                    st.session_state.pop("_delete_search_requested", None)
                    st.session_state["_checked_search_deletions"] = []
                    remaining = [name for name in names if name not in targets]
                    if remaining:
                        following = [name for name in names[names.index(choice) + 1:] if name in remaining] if choice in names else []
                        next_choice = choice if choice in remaining else (following[0] if following else remaining[-1])
                        st.session_state["active_search_selection"] = next_choice
                        st.session_state["_search_table_epoch"] = st.session_state.get("_search_table_epoch", 0) + 1
                    else:
                        _open_new_search()
                    logging.getLogger("review_app").info("Permanently deleted searches %r", targets)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not complete deletion: {exc}")
            if cancel.button("Cancel", key="cancel_delete_search"):
                st.session_state.pop("_delete_search_requested", None)
                st.rerun()
    previous = st.session_state.get("_last_rendered_search_selection")
    if previous != choice:
        logging.getLogger("review_app").info("Search selection changed: %r -> %r", previous, choice)
        st.session_state["_last_rendered_search_selection"] = choice
    return choice


def render_browser_app() -> None:
    """Render the search manager and selected preparation workspace."""
    st.set_page_config(page_title="Airbnb Analyzer", page_icon="🏠", layout="wide", initial_sidebar_state="expanded")
    button_css = (Path(__file__).parent / "assets" / "buttons.css").read_text(encoding="utf-8")
    st.markdown(f"<style>{button_css}</style>", unsafe_allow_html=True)
    st.markdown(f'<h1 style="margin:0;padding:0">{github_logo_link()}</h1>', unsafe_allow_html=True)
    panel_open = st.session_state.get("search_panel_open", True)
    if st.button("Hide searches" if panel_open else "☰ Show searches", key="toggle_search_panel"):
        st.session_state["search_panel_open"] = not panel_open
        st.rerun()
    panel_style = """display: block !important; transform: none !important;
        width: min(820px, 90vw) !important; min-width: min(820px, 90vw) !important;
        max-width: 90vw !important; margin-left: 0 !important;""" if panel_open else "display: none !important;"
    st.markdown("<style>[data-testid='stSidebar'] {" + panel_style + "}" +
                "[data-testid='collapsedControl'], [data-testid='stSidebarCollapsedControl'], " +
                "[data-testid='stSidebarCollapseButton'] {display:none !important;}" +
                "</style>", unsafe_allow_html=True)
    st.caption("Define a trip and area, collect listing evidence, then export an interactive property webpage.")
    try:
        searches = list_available_searches()
    except Exception as exc:
        st.error(f"Search workspaces could not be listed: {exc}")
        searches = []
    summaries = _search_catalog(searches)
    choice = _render_search_panel(summaries)
    if choice == "＋ New Search":
        _new_search_form([item["name"] for item in summaries])
        return
    summary = next((item for item in summaries if item.get("name") == choice), {})
    archived = _summary_is_archived(summary)
    if archived:
        if st.button("Enable this saved search", key=f"restore_search_{choice}"):
            from src.search_catalog import set_search_archived
            from src.search_service import SEARCHES_ROOT
            set_search_archived(choice, False, root=SEARCHES_ROOT)
            st.rerun()
    _render_existing_search(choice, archived=archived)


__all__ = ["render_browser_app"]

