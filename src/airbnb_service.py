"""Compatibility adapter for discovery, date-specific details and review collection.

No provider call is made at import time. The adapter inspects the installed
``pyairbnb`` signatures before invoking it, preserving offline and fake-provider
testability.
"""

from __future__ import annotations

import ast
import inspect
import json
import numbers
import re
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from .search_service import _point_in_normalized_geometry, load_search_definition, validate_existing_search_name
from .utils import parse_coordinates, parse_stay_total
from . import storage_service as storage

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CollectorCompatibilityError(RuntimeError):
    """The installed collector cannot satisfy the requested collection stage."""


def _workspace(search_name: str) -> Path:
    name = validate_existing_search_name(search_name)
    root = (PROJECT_ROOT / "searches").resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise ValueError("Search workspace path escapes the searches directory.")
    return path


def _atomic_csv(df: pd.DataFrame, path: Path) -> None:
    storage.write_frame(path, df)


def _atomic_json(value: Any, path: Path) -> None:
    storage.write_json(path, value)


def merge_property_snapshots(
    results: pd.DataFrame,
    details: pd.DataFrame,
    reviews: pd.DataFrame,
    definition: Dict[str, Any],
) -> pd.DataFrame:
    """Join only current discovered IDs and classify each date-specific quote."""
    merged = results.copy()
    current_ids = set(merged.get("room_id", pd.Series(dtype=str)).dropna().astype(str))
    if not details.empty and "room_id" in details.columns:
        details = details[details["room_id"].astype(str).isin(current_ids)]
        merged = merged.merge(details, on="room_id", how="left", suffixes=("", "_details"))
        merged["details_quote_status"] = merged.apply(lambda row: _detail_quote_status(row, definition), axis=1)
    else:
        merged["details_quote_status"] = "missing"

    if not merged.empty and "search_quote_check_in" in merged.columns:
        merged["search_quote_status"] = merged.apply(lambda row: _search_quote_status(row, definition), axis=1)
    else:
        merged["search_quote_status"] = "missing"

    if not merged.empty:
        selected_quotes = merged.apply(lambda row: _choose_quote(row, definition), axis=1, result_type="expand")
        for column in selected_quotes.columns:
            merged[column] = selected_quotes[column]
    else:
        for column in ("total_price", "quote_status", "quote_source", "quote_total_completeness"):
            if column not in merged.columns:
                merged[column] = pd.Series(dtype="object")
    if not reviews.empty and "room_id" in reviews.columns:
        reviews = reviews[reviews["room_id"].astype(str).isin(current_ids)]
        merged = merged.merge(reviews, on="room_id", how="left", suffixes=("", "_reviews"))
    if not merged.empty and 'discovery_state' in merged:
        merged.loc[merged['discovery_state'].eq('not_returned'), 'availability_state'] = 'not_returned'
    return merged


def get_collector(provider: Any = None) -> Any:
    """Load the installed pyairbnb module lazily or return a test provider."""
    if provider is not None:
        return provider
    try:
        import pyairbnb
    except ImportError as exc:
        raise CollectorCompatibilityError(
            "The installed pyairbnb package is unavailable. Install the pinned project dependencies, then retry."
        ) from exc
    return pyairbnb


def _fetch_listing_details(collector, kwargs, policy=None, cached_metadata=None):
    """Fetch page metadata and trip price without pyairbnb's hidden review loop.

    The pinned public get_details wrapper downloads all review pages, calendar
    and host details internally. Each low-level request here is paced/retried
    individually. Bootstrap inputs/cookies stay in memory for review collection.
    """
    page = getattr(getattr(collector, 'details', None), 'get', None)
    price = getattr(collector, 'get_price', None)
    if getattr(collector, '__name__', '') != 'pyairbnb':
        invoke = lambda: _call_with_supported(collector.get_details, kwargs)
        return (policy.call(invoke) if policy else invoke()), None
    if not callable(page) or not callable(price):
        raise CollectorCompatibilityError('Installed pyairbnb lacks separate metadata/price endpoints; refusing an unbounded review download.')
    def invoke(function, operation):
        try:
            return policy.call(function, operation=operation) if policy else function()
        except Exception as exc:
            exc.collection_operation = operation
            raise
    if cached_metadata and callable(getattr(collector, 'get_api_key', None)):
        # Public bootstrap key is run-local; no cookies/credentials enter the cache.
        key = getattr(policy, '_cached_airbnb_api_key', None) if policy else None
        if not key:
            key = invoke(lambda: collector.get_api_key(''), 'api_bootstrap')
            if policy:
                policy._cached_airbnb_api_key = key
        context = None
        data = dict(cached_metadata)
        inputs, cookies = {'api_key': key, 'impression_id': None}, {}
    else:
        context = invoke(lambda: page(kwargs['room_url'], 'en', ''), 'listing_page')
        data, inputs, cookies = context
        data = dict(data)
        data['property_details_fetched_at'] = datetime.now(timezone.utc).isoformat()
    data['price'] = invoke(lambda: _call_with_supported(price, {
        'room_id': kwargs['room_url'].rsplit('/', 1)[-1],
        'check_in': kwargs['check_in'], 'check_out': kwargs['check_out'],
        'adults': kwargs['adults'], 'currency': kwargs['currency'], 'language': 'en',
        'impresion_id': inputs['impression_id'], 'api_key': inputs['api_key'], 'cookies': cookies,
    }), 'stay_price')
    return data, context


def collector_capabilities(provider: Any = None) -> Dict[str, Any]:
    """Describe collector functions and accepted parameters without network use."""
    try:
        module = get_collector(provider)
    except CollectorCompatibilityError as exc:
        return {"available": False, "error": str(exc), "version": None, "functions": {}}
    functions = {}
    for function_name in ("search_all", "get_details", "get_reviews"):
        function = getattr(module, function_name, None)
        try:
            signature = inspect.signature(function) if callable(function) else None
            functions[function_name] = list(signature.parameters) if signature else None
        except (TypeError, ValueError):
            functions[function_name] = None
    version = getattr(module, "__version__", None)
    available = callable(getattr(module, "search_all", None)) and callable(getattr(module, "get_details", None)) and callable(getattr(module, "get_reviews", None))
    return {"available": available, "version": version, "functions": functions}


def _call_with_supported(function: Callable[..., Any], values: Dict[str, Any]) -> Any:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError) as exc:
        raise CollectorCompatibilityError("Collector function signature could not be inspected safely.") from exc
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
    kwargs = values if accepts_kwargs else {key: value for key, value in values.items() if key in signature.parameters}
    missing = [
        name for name, parameter in signature.parameters.items()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and name not in kwargs
    ]
    if missing:
        raise CollectorCompatibilityError(
            "Installed collector is missing required search parameters: " + ", ".join(missing)
        )
    return function(**kwargs)


def _extract_room_id(row: Dict[str, Any]) -> Optional[str]:
    for key in ("room_id", "listing_id", "id"):
        value = row.get(key)
        if value is not None and str(value).strip() and str(value).lower() != "nan":
            if isinstance(value, numbers.Real) and not isinstance(value, bool) and float(value).is_integer():
                return str(int(value))
            return str(value).strip()
    return None


def _parse_nested(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return value
    if isinstance(value, str):
        for parser in (json.loads, ast.literal_eval):
            try:
                return parser(value)
            except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
                pass
    return None


def _parse_amount(value: Any) -> Optional[float]:
    return parse_stay_total(value)


def _explicit_price_total(value: Any) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """Extract only a labeled total; never treat unit/nightly price as stay total."""
    parsed = _parse_nested(value)
    if not isinstance(parsed, dict):
        return None, None, None
    breakdown = parsed.get("break_down", parsed.get("breakdown", []))
    if isinstance(breakdown, list):
        for item in breakdown:
            if not isinstance(item, dict) or str(item.get("description", "")).strip().casefold() != "total":
                continue
            amount = _parse_amount(item.get("amount"))
            if amount is not None:
                return amount, "price.break_down.Total", "provider_breakdown_total"
    total = parsed.get("total")
    amount = _parse_amount(total.get("amount")) if isinstance(total, dict) else _parse_amount(total)
    if amount is not None:
        return amount, "price.total.amount", "provider_total_tax_completeness_unknown"
    main = parsed.get("main")
    if isinstance(main, dict):
        details = main.get("details")
        if isinstance(details, dict):
            for label, price_text in details.items():
                if str(label).strip().casefold() in {"total", "total price", "total stay price"}:
                    amount = _parse_amount(price_text)
                    if amount is not None:
                        return amount, f"price.main.details.{label}", "provider_breakdown_total_tax_completeness_unknown"
        qualifier = main.get("qualifier")
        qualifier_text = " ".join(str(value) for value in qualifier.values()) if isinstance(qualifier, dict) else str(qualifier or "")
        if re.search(r"\btotal\b", qualifier_text, re.I) and not re.search(r"\bper\s+night\b|\bnightly\b", qualifier_text, re.I):
            amount = _parse_amount(main.get("price"))
            if amount is not None:
                return amount, "price.main.price_qualified_total", "provider_qualified_total_tax_completeness_unknown"
    return None, None, None


def _explicit_record_total(record: Dict[str, Any]) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    direct = _parse_amount(record.get("total_price"))
    if direct is not None:
        return direct, "total_price", "explicit_total_price_field"
    return _explicit_price_total(record.get("price"))


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    return str(value).strip().lower() in {"true", "1", "yes"}


def _blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower() in {"", "nan", "none"}


def _matches_revision(value: Any, definition: Dict[str, Any]) -> bool:
    try:
        return int(float(value)) == int(definition["trip_revision"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _matches_party(row: Any, prefix: str, definition: Dict[str, Any]) -> bool:
    if not _truthy(row.get(f"{prefix}party_supported")):
        return False
    for field in ("adults", "children", "infants", "pets"):
        key = f"{prefix}{field}"
        value = row.get(key)
        if _blank(value):
            continue
        try:
            if int(float(value)) != int(definition.get(field, 0)):
                return False
        except (TypeError, ValueError, OverflowError):
            return False
    return True


def _detail_quote_status(row: Any, definition: Optional[Dict[str, Any]]) -> str:
    attempt_status = str(row.get("quote_attempt_status", "")).strip().lower()
    if attempt_status == "unavailable" or str(row.get("availability_state", "")).strip().lower() == "unavailable":
        return "unavailable"
    if attempt_status == "failed":
        return "failed"
    if definition is None:
        stored = str(row.get("details_quote_status", "")).strip().lower()
        if stored in {"current", "unverified", "stale", "unavailable", "failed", "missing"}:
            return stored
        return "current" if str(row.get("quote_status", "")).strip().lower() == "current" and str(row.get("quote_source", "")).startswith("details:") else "missing"
    fetched_at = row.get("details_fetched_at")
    if _blank(fetched_at):
        return "missing"
    trip_revision = row.get("trip_revision_details", row.get("trip_revision"))
    if not _matches_revision(trip_revision, definition):
        return "stale"
    context_ok = (
        str(row.get("quote_check_in", "")) == str(definition.get("check_in", ""))
        and str(row.get("quote_check_out", "")) == str(definition.get("check_out", ""))
        and str(row.get("quote_currency", "")).upper() == str(definition.get("currency", "")).upper()
    )
    party_ok = _matches_party(row, "quote_", definition)
    return "current" if context_ok and party_ok else "unverified"


def _search_quote_status(
    row: Any,
    definition: Optional[Dict[str, Any]],
    *,
    allow_saved_canonical: bool = False,
) -> str:
    amount = _parse_amount(row.get("search_quote_total"))
    if amount is None and allow_saved_canonical and str(row.get("quote_status", "")).strip().lower() == "current":
        source = str(row.get("quote_source", "")).strip().lower()
        if not source.startswith("details:"):
            amount = _parse_amount(row.get("total_price"))
    if amount is None:
        return "missing"
    if definition is None:
        stored = str(row.get("search_quote_status", "")).strip().lower()
        if stored in {"current", "unverified", "stale", "missing"}:
            return stored
        return "current" if str(row.get("quote_status", "")).strip().lower() == "current" else "unverified"
    context_ok = (
        str(row.get("search_quote_check_in", "")) == str(definition.get("check_in", ""))
        and str(row.get("search_quote_check_out", "")) == str(definition.get("check_out", ""))
        and str(row.get("search_quote_currency", "")).upper() == str(definition.get("currency", "")).upper()
        and _matches_party(row, "search_quote_", definition)
        and _matches_revision(row.get("trip_revision"), definition)
    )
    return "current" if context_ok else "unverified"


def _choose_quote(
    row: Any,
    definition: Optional[Dict[str, Any]],
    *,
    preserve_saved_canonical: bool = False,
) -> Dict[str, Any]:
    detail_status = _detail_quote_status(row, definition)
    search_status = _search_quote_status(row, definition, allow_saved_canonical=preserve_saved_canonical)
    detail_amount, detail_source, detail_completeness = _explicit_record_total({
        "total_price": row.get("total_price_details"),
        "price": row.get("price_details"),
    })
    canonical = _parse_amount(row.get("total_price")) if preserve_saved_canonical else None
    saved_source = str(row.get("quote_source", "") or "")
    if detail_status == "current" and detail_amount is None and canonical is not None and saved_source.startswith("details:"):
        detail_amount = canonical
        detail_source = saved_source.removeprefix("details:") or "saved_explicit_detail_total"
        detail_completeness = row.get("quote_total_completeness") or "saved_explicit_detail_total"
    search_amount = _parse_amount(row.get("search_quote_total"))
    if search_amount is None and search_status == "current" and canonical is not None and not saved_source.startswith("details:"):
        search_amount = canonical
    search_source = str(row.get("search_quote_source") or "search_result")
    search_completeness = str(row.get("search_quote_completeness") or "unknown")

    if str(row.get('discovery_state', '')) == 'not_returned':
        return {'total_price': None, 'stale_total_price': detail_amount or search_amount,
                'quote_status': 'stale', 'quote_source': 'not_returned_in_latest_search',
                'quote_total_completeness': None, 'quote_recovery_reason': 'Not returned by the latest discovery; availability is unconfirmed.'}
    try:
        newer_search = pd.to_datetime(row.get('discovered_at'), utc=True) > pd.to_datetime(row.get('details_fetched_at'), utc=True)
    except (ValueError, TypeError):
        newer_search = False
    if newer_search and search_status == 'current' and search_amount is not None:
        return {'total_price': search_amount, 'quote_status': 'current', 'quote_source': f'discovery:{search_source}',
                'quote_total_completeness': search_completeness, 'quote_recovery_reason': 'Price updated by latest discovery.'}
    if detail_status == "unavailable":
        return {"total_price": None, "quote_status": "unavailable", "quote_source": "date_specific_detail_unavailable", "quote_total_completeness": None, "quote_recovery_reason": None}
    if detail_status == "failed":
        if search_amount is not None and search_status == "current":
            return {"total_price": None, "unverified_total_price": search_amount, "quote_status": "unverified", "quote_source": f"discovery_after_detail_failure:{search_source}", "quote_total_completeness": search_completeness, "quote_recovery_reason": "Details failed; current discovery total is unverified."}
        if search_amount is not None and search_status == "unverified":
            return {"total_price": None, "unverified_total_price": search_amount, "quote_status": "unverified", "quote_source": search_source, "quote_total_completeness": search_completeness, "quote_recovery_reason": None}
        return {"total_price": None, "quote_status": "failed", "quote_source": "date_specific_detail_failed", "quote_total_completeness": None, "quote_recovery_reason": None}
    if detail_status == "current" and detail_amount is not None:
        return {"total_price": detail_amount, "quote_status": "current", "quote_source": f"details:{detail_source}", "quote_total_completeness": detail_completeness, "quote_recovery_reason": None}
    if search_status == "current" and search_amount is not None:
        fallback = detail_status == "current"
        source = f"discovery_fallback:{search_source}" if fallback else f"discovery:{search_source}"
        reason = "Invalid detail total; used a positive discovery total for the same trip." if fallback else None
        return {"total_price": search_amount, "quote_status": "current", "quote_source": source, "quote_total_completeness": search_completeness, "quote_recovery_reason": reason}
    if detail_status == "unverified" and detail_amount is not None:
        return {"total_price": None, "unverified_total_price": detail_amount, "quote_status": "unverified", "quote_source": f"details:{detail_source}", "quote_total_completeness": detail_completeness, "quote_recovery_reason": None}
    if search_status == "unverified" and search_amount is not None:
        return {"total_price": None, "unverified_total_price": search_amount, "quote_status": "unverified", "quote_source": search_source, "quote_total_completeness": search_completeness, "quote_recovery_reason": None}
    if detail_status == "stale" and detail_amount is not None:
        return {"total_price": None, "stale_total_price": detail_amount, "quote_status": "stale", "quote_source": f"details:{detail_source}", "quote_total_completeness": detail_completeness, "quote_recovery_reason": None}
    return {"total_price": None, "quote_status": "missing", "quote_source": None, "quote_total_completeness": None, "quote_recovery_reason": None}


def repair_saved_quote_prices(frame: pd.DataFrame, definition: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """Return a copy with verified stay totals repaired from saved evidence only.

    No provider call or file write occurs. Positive matching totals are retained,
    zero/negative/non-finite canonical values are discarded, and a positive
    current discovery total can replace an invalid current detail placeholder.
    Raw provider payload columns are preserved for inspection.
    """
    repaired = frame.copy()
    if repaired.empty:
        return repaired
    quote_rows = []
    for _, series in repaired.iterrows():
        row = series.to_dict()
        row["details_quote_status"] = _detail_quote_status(row, definition)
        row["search_quote_status"] = _search_quote_status(row, definition, allow_saved_canonical=True)
        quote_rows.append(_choose_quote(row, definition, preserve_saved_canonical=True))
    quote_frame = pd.DataFrame(quote_rows, index=repaired.index)
    for column in ("total_price", "quote_status", "quote_source", "quote_total_completeness", "quote_recovery_reason", "unverified_total_price", "stale_total_price"):
        if column in quote_frame.columns:
            repaired[column] = quote_frame[column]
        elif column in {"unverified_total_price", "stale_total_price"}:
            repaired[column] = pd.NA
    return repaired


def _http_status_code(exc: BaseException) -> Optional[int]:
    """Extract an HTTP status without depending on a particular client library."""
    for value in (
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
        getattr(exc, "code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(getattr(exc, "response", None), "status", None),
    ):
        try:
            if value is not None and int(value) == 429:
                return 429
        except (TypeError, ValueError, OverflowError):
            continue
    # urllib-style HTTPError and some provider wrappers expose only a bounded
    # human-readable status line. Avoid treating arbitrary occurrences of 429
    # (for example a listing ID) as a rate-limit response.
    message = str(exc)[:1000]
    if re.search(
        r"(?:\bHTTP(?:\s+Error)?\s+429\b|\b429\s+(?:Client Error|Too Many Requests|rate limit)\b|\bstatus(?:_code)?\s*[=: ]\s*429\b)",
        message,
        flags=re.IGNORECASE,
    ):
        return 429
    return None


def _availability_state(record: Dict[str, Any]) -> str:
    value = record.get("availability_state", record.get("availability"))
    if value is False or str(value).strip().lower() in {"false", "unavailable", "not available"}:
        return "unavailable"
    if value is True or str(value).strip().lower() in {"true", "available"}:
        return "available"
    return "unknown"


def _coordinates(row: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    return parse_coordinates(row)


def _normalize_records(response: Any) -> pd.DataFrame:
    if isinstance(response, pd.DataFrame):
        return response.copy()
    if isinstance(response, dict) and _extract_room_id(response):
        response = [response]
    if isinstance(response, dict):
        for key in ("results", "rooms", "listings", "data"):
            if isinstance(response.get(key), (list, dict)):
                response = response[key]
                break
    if isinstance(response, dict):
        response = list(response.values())
    if not isinstance(response, (list, tuple)):
        raise ValueError(f"Collector returned {type(response).__name__}, expected a listing collection.")
    normalized = []
    for item in response:
        if not isinstance(item, dict):
            normalized.append({"value": item})
            continue
        record = dict(item)
        if not record.get("room_id"):
            record["room_id"] = _extract_room_id(record)
        else:
            record["room_id"] = _extract_room_id({"room_id": record["room_id"]})
        normalized.append(record)
    return pd.DataFrame(normalized)


def discover_listings(
    search_name: str,
    *,
    provider: Any = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """Collect search results for the stored trip and drawn geometry."""
    directory = _workspace(search_name)
    definition = load_search_definition(search_name, root=PROJECT_ROOT / "searches")
    if not definition:
        raise ValueError("This legacy workspace has no saved trip/area definition. Edit its search settings first.")
    collector = get_collector(provider)
    search_function = getattr(collector, "search_all", None)
    if not callable(search_function):
        raise CollectorCompatibilityError("Installed pyairbnb does not expose search_all.")
    params = inspect.signature(search_function).parameters
    accepts_search_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
    required_party = ("adults", "children", "infants")
    unsupported = [field for field in required_party if definition[field] and field not in params and not accepts_search_kwargs]
    if unsupported:
        raise CollectorCompatibilityError(
            "This collector cannot pass the selected party fields: " + ", ".join(unsupported)
        )
    # Cancellation is a report filter, never a discovery constraint (including
    # workspaces created before that setting was removed from the form).
    free_cancellation = False
    supports_cancellation = "free_cancellation" in params or accepts_search_kwargs

    bounds = definition["bounds"]
    call_values = {
        "check_in": definition["check_in"],
        "check_out": definition["check_out"],
        "ne_lat": bounds["north"],
        "ne_long": bounds["east"],
        "sw_lat": bounds["south"],
        "sw_long": bounds["west"],
        "zoom_value": 14,
        "price_min": 0,
        # The collector's price_max is provider-specific (often nightly), so it
        # must not be treated as the user's total-stay budget.
        "price_max": 1000000,
        "currency": definition["currency"],
        "adults": definition["adults"],
        "children": definition["children"],
        "infants": definition["infants"],
        "pets": definition["pets"],
        "free_cancellation": free_cancellation,
    }
    if progress_callback:
        progress_callback(0, 1, "Contacting Airbnb search provider")
    response = _call_with_supported(search_function, call_values)
    frame = _normalize_records(response)
    output = directory / "output_data" / "results_df.csv"
    previous = storage.read_frame(output, dtype={"room_id": str}) if storage.exists(output) else pd.DataFrame()
    if frame.empty:
        from .processing_service import retained_results
        empty = pd.DataFrame(columns=["room_id", "trip_revision", "discovered_at", "total_price", "quote_status"])
        retained = retained_results(previous, empty, definition['trip_revision'], datetime.now(timezone.utc).isoformat())
        _atomic_csv(retained, output)
        details_path = directory / "output_data" / "details_df.csv"
        reviews_path = directory / "output_data" / "reviews_df.csv"
        details = storage.read_frame(details_path, dtype={"room_id": str}) if storage.exists(details_path) else pd.DataFrame()
        reviews = storage.read_frame(reviews_path, dtype={"room_id": str}) if storage.exists(reviews_path) else pd.DataFrame()
        _atomic_csv(merge_property_snapshots(retained, details, reviews, definition), directory / "output_data" / "merged_df.csv")
        return {"status": "no_matches", "discovered": 0, "retained": len(retained), "partial": False,
                "message": "No listings returned; previous listings and evidence were retained."}

    frame["room_id"] = frame.apply(lambda row: _extract_room_id(row.to_dict()), axis=1)
    frame = frame[frame["room_id"].notna()].copy()
    frame["room_id"] = frame["room_id"].astype(str)
    frame = frame.drop_duplicates(subset=["room_id"], keep="first")
    latlng = frame.apply(lambda row: _coordinates(row.to_dict()), axis=1)
    frame["latitude"] = [point[0] for point in latlng]
    frame["longitude"] = [point[1] for point in latlng]
    frame["location_state"] = "known"
    frame.loc[frame["latitude"].isna() | frame["longitude"].isna(), "location_state"] = "unknown"
    geometry = definition["geometry"]
    in_area = []
    for _, row in frame.iterrows():
        lat, lng = row["latitude"], row["longitude"]
        if pd.isna(lat) or pd.isna(lng):
            in_area.append(True)
        else:
            in_area.append(_point_in_normalized_geometry(float(lat), float(lng), geometry))
    frame = frame.loc[in_area].copy()
    frame["trip_revision"] = int(definition["trip_revision"])
    frame["discovered_at"] = datetime.now(timezone.utc).isoformat()
    # Unfiltered results are not evidence that cancellation is unavailable.
    frame["free_cancellation_search_match"] = True if free_cancellation and supports_cancellation else pd.NA
    frame["cancellation_search_check_in"] = definition["check_in"] if free_cancellation and supports_cancellation else pd.NA
    frame["cancellation_search_check_out"] = definition["check_out"] if free_cancellation and supports_cancellation else pd.NA
    frame["cancellation_search_currency"] = definition["currency"] if free_cancellation and supports_cancellation else pd.NA
    frame["cancellation_search_trip_revision"] = int(definition["trip_revision"]) if free_cancellation and supports_cancellation else pd.NA
    if "rating" in frame.columns:
        rating_values = frame["rating"].apply(lambda value: value.get("value") if isinstance(value, dict) else value)
        frame["Airbnb_rating"] = pd.to_numeric(rating_values, errors="coerce")
    signature_has_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
    search_context_supported = all(
        key in params or signature_has_kwargs
        for key in ("check_in", "check_out", "currency", "adults", "children", "infants")
        if key in {"check_in", "check_out", "currency", "adults"} or definition[key]
    ) and (not definition["pets"] or "pets" in params or signature_has_kwargs)
    quote_values = frame.apply(lambda row: _explicit_record_total(row.to_dict()), axis=1)
    frame["search_quote_total"] = [value[0] for value in quote_values]
    frame["search_quote_source"] = [value[1] for value in quote_values]
    frame["search_quote_completeness"] = [value[2] for value in quote_values]
    frame["search_quote_check_in"] = definition["check_in"]
    frame["search_quote_check_out"] = definition["check_out"]
    frame["search_quote_currency"] = definition["currency"]
    frame["search_quote_party_supported"] = search_context_supported
    # Keep a legacy-compatible numeric total only when the provider exposed an
    # explicit trip total (never infer one from the unit/nightly amount).
    frame["total_price"] = frame["search_quote_total"]
    output = directory / "output_data" / "results_df.csv"
    from .processing_service import retained_results
    discovered_count = len(frame)
    frame = retained_results(previous, frame, definition['trip_revision'], datetime.now(timezone.utc).isoformat())
    _atomic_csv(frame, output)
    details_path, reviews_path = directory / "output_data" / "details_df.csv", directory / "output_data" / "reviews_df.csv"
    details = storage.read_frame(details_path, dtype={"room_id": str}) if storage.exists(details_path) else pd.DataFrame()
    reviews = storage.read_frame(reviews_path, dtype={"room_id": str}) if storage.exists(reviews_path) else pd.DataFrame()
    _atomic_csv(merge_property_snapshots(frame, details, reviews, definition), directory / "output_data" / "merged_df.csv")
    if progress_callback:
        progress_callback(1, 1, f"Saved {discovered_count} current listings; retained prior history")
    return {
        "status": "success" if discovered_count else "no_matches",
        "discovered": discovered_count,
        "location_unknown": int(frame["location_state"].eq("unknown").sum()),
        "partial": bool(frame["location_state"].eq("unknown").any()),
        "trip_revision": int(definition["trip_revision"]),
        "free_cancellation_filter_applied": bool(free_cancellation and supports_cancellation),
        "party_limitations": ["Pets were saved with the trip but this search collector does not accept a pets parameter."] if definition["pets"] and "pets" not in params and not accepts_search_kwargs else [],
        "message": "Unknown-location listings are retained; known coordinates were checked against the drawn boundary.",
    }


def normalize_reviews(response: Any) -> Tuple[str, str, int]:
    if isinstance(response, str):
        response = _parse_nested(response)
    if isinstance(response, dict):
        for key in ("reviews", "data", "results"):
            if isinstance(response.get(key), (list, tuple)):
                response = response[key]
                break
    if isinstance(response, pd.DataFrame):
        records = response.to_dict("records")
    elif isinstance(response, dict):
        records = [response]
    elif isinstance(response, (list, tuple)):
        records = [item for item in response if isinstance(item, dict)]
    else:
        records = []
    evidence_text = []
    for review in records:
        comments = review.get("comments", review.get("text", review.get("review", "")))
        if not comments:
            continue
        created = review.get("created_at", review.get("createdAt", review.get("date", "")))
        rating = review.get("rating", "")
        source_id = review.get("id", review.get("review_id", ""))
        evidence_text.append(f"{created} {comments} Rating: {rating} [review_id={source_id}]".strip())
    try:
        serialized = json.dumps(records, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = "[]"
    return "\n".join(evidence_text), serialized, len(records)


def collect_details_and_reviews(
    search_name: str,
    *,
    provider: Any = None,
    request_policy: Any = None,
    refresh_quotes: bool = False,
    retry_room_ids: Optional[Sequence[str]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """Fetch missing details/reviews, checkpointing after each completed listing."""
    directory = _workspace(search_name)
    output_dir = directory / "output_data"
    definition = load_search_definition(search_name, root=PROJECT_ROOT / "searches")
    if not definition:
        raise ValueError("This legacy workspace has no saved trip definition.")
    results_path = output_dir / "results_df.csv"
    if not storage.exists(results_path):
        raise FileNotFoundError("Discover listings before fetching details and reviews.")
    results = storage.read_frame(results_path, dtype={"room_id": str})
    if results.empty:
        return {"status": "no_matches", "completed": 0, "failed_room_ids": []}
    collector = get_collector(provider)
    details_fn = getattr(collector, "get_details", None)
    reviews_fn = getattr(collector, "get_reviews", None)
    if not callable(details_fn) or not callable(reviews_fn):
        raise CollectorCompatibilityError("Installed pyairbnb must provide both get_details and get_reviews.")

    details_path, reviews_path = output_dir / "details_df.csv", output_dir / "reviews_df.csv"
    existing_details = storage.read_frame(details_path, dtype={"room_id": str}) if storage.exists(details_path) else pd.DataFrame()
    existing_reviews = storage.read_frame(reviews_path, dtype={"room_id": str}) if storage.exists(reviews_path) else pd.DataFrame()
    current_revision = int(definition["trip_revision"])
    from .processing_service import load_selection, select_properties, recent
    selection = load_selection(directory, definition)
    scoped_ids = set(select_properties(results, definition, selection)['selected_ids'])
    enforce_freshness = bool(selection is not None or request_policy)
    from .property_cache import PropertyCache
    shared_cache = PropertyCache(PROJECT_ROOT / 'searches')
    shared_cache.seed_existing()
    shared_hits = shared_cache.load(scoped_ids, definition, request_policy.options if request_policy else None)
    local_details = {str(row['room_id']): row for row in json.loads(existing_details.to_json(orient='records'))}
    local_reviews = {str(row['room_id']): row for row in json.loads(existing_reviews.to_json(orient='records'))}
    reused_details = reused_reviews = 0
    from .review_collection import cached_reviews_usable
    for rid, hit in shared_hits.items():
        local = local_details.get(rid, {})
        if not refresh_quotes and 'details' in hit and (
            _detail_quote_status(local, definition) != 'current' or not recent(local.get('details_fetched_at'))
        ):
            local_details[rid] = hit['details']
            reused_details += 1
        review = local_reviews.get(rid, {})
        usable_review = cached_reviews_usable(review, request_policy.options) if request_policy else recent(review.get('reviews_fetched_at'))
        if 'reviews' in hit and not usable_review:
            local_reviews[rid] = hit['reviews']
            reused_reviews += 1
    if reused_details:
        existing_details = pd.DataFrame(local_details.values())
        _atomic_csv(existing_details, details_path)
    if reused_reviews:
        existing_reviews = pd.DataFrame(local_reviews.values())
        _atomic_csv(existing_reviews, reviews_path)
    property_dates = {str(row['room_id']): row.get('property_details_fetched_at') or row.get('details_fetched_at') for row in local_details.values()}
    details_dates = {str(row['room_id']): row.get('quote_attempt_at') if _blank(row.get('details_fetched_at')) else row.get('details_fetched_at') for _, row in existing_details.iterrows()}
    detail_revision_by_id = {}
    detail_attempt_by_id = {}
    if not existing_details.empty and "room_id" in existing_details.columns:
        for _, row in existing_details.iterrows():
            detail_revision_by_id[str(row["room_id"])] = row.get("trip_revision")
            detail_attempt_by_id[str(row["room_id"])] = str(row.get("quote_attempt_status", "")).strip().lower()
    review_ids = set()
    for _, saved_review in existing_reviews.iterrows():
        row = saved_review.to_dict()
        usable = not enforce_freshness or recent(row.get('reviews_fetched_at'))
        if usable and request_policy:
            from .review_collection import cached_reviews_usable
            usable = cached_reviews_usable(row, request_policy.options)
        if usable:
            review_ids.add(str(row['room_id']))
    selected_ids = set(str(rid) for rid in retry_room_ids) if retry_room_ids is not None else None

    work = []
    for _, result in results.iterrows():
        rid = str(result["room_id"])
        if rid not in scoped_ids or (selected_ids is not None and rid not in selected_ids):
            continue
        details_current = detail_revision_by_id.get(rid) == current_revision and detail_attempt_by_id.get(rid) != "failed"
        if enforce_freshness:
            details_current = details_current and recent(details_dates.get(rid)) and recent(property_dates.get(rid))
        reviews_current = rid in review_ids
        if refresh_quotes:
            details_current = False
        if not details_current or not reviews_current:
            work.append((rid, result.to_dict(), not details_current, not reviews_current))

    details_rows = existing_details.to_dict("records")
    reviews_rows = existing_reviews.to_dict("records")
    details_by_id = {str(row.get("room_id")): row for row in details_rows if row.get("room_id") is not None}
    reviews_by_id = {str(row.get("room_id")): row for row in reviews_rows if row.get("room_id") is not None}
    if request_policy:
        from .review_collection import select_reviews, policy_key
        locally_resampled = False
        for rid in review_ids & scoped_ids:
            cached_row = reviews_by_id[rid]
            try:
                old_policy = json.loads(cached_row.get('reviews_policy')) if isinstance(cached_row.get('reviews_policy'), str) else {}
            except (ValueError, TypeError):
                old_policy = {}
            # Preserve the exact recent sample across a moving date cutoff;
            # changing the requested cap/window can be applied locally.
            old_cutoff = old_policy.get('review_cutoff')
            cutoff = request_policy.options.get('review_cutoff')
            window_narrowed = bool(old_cutoff and cutoff and (date.fromisoformat(cutoff)-date.fromisoformat(old_cutoff)).days > 31)
            if old_policy.get('max_reviews') == request_policy.options['max_reviews'] and not window_narrowed:
                continue
            raw = _parse_nested(cached_row.get('reviews_json'))
            if isinstance(raw, list):
                sampled = select_reviews(raw, request_policy.options)
                text, encoded, count = normalize_reviews(sampled)
                cached_row.update({'reviews_json': encoded, 'reviews_text': text, 'reviews_retrieved_count': count,
                                   'reviews_policy': policy_key(request_policy.options),
                                   'reviews_limit': request_policy.options['max_reviews'], 'reviews_cutoff': cutoff})
                locally_resampled = True
        if locally_resampled:
            _atomic_csv(pd.DataFrame(reviews_by_id.values()), reviews_path)
    detail_params = inspect.signature(details_fn).parameters
    accepts_detail_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in detail_params.values())
    required_quote_fields = ["check_in", "check_out", "adults", "currency"]
    required_quote_fields.extend(field for field in ("children", "infants", "pets") if definition[field])
    party_quote_supported = all(field in detail_params or accepts_detail_kwargs for field in required_quote_fields)

    def persist_merged_snapshot() -> None:
        """Refresh browseable current results after each detail/review checkpoint."""
        merged = merge_property_snapshots(
            results,
            pd.DataFrame(details_by_id.values()),
            pd.DataFrame(reviews_by_id.values()),
            definition,
        )
        if not party_quote_supported:
            merged["quote_party_warning"] = "Provider does not support the full selected party/date context in the quote request."
        _atomic_csv(merged, output_dir / "merged_df.csv")

    if not work:
        persist_merged_snapshot()
        return {
            "status": "success", "completed": 0, "total": 0,
            "failed_room_ids": [], "failure_details": [], "attempted_room_ids": [],
            "unattempted_room_ids": [], "rate_limited": False,
            "shared_cache_details": reused_details, "shared_cache_reviews": reused_reviews,
            "message": f"Details and reviews are current. Shared cache reused: {reused_details} details, {reused_reviews} reviews.",
        }
    failed: List[Dict[str, Any]] = []
    completed = 0
    attempted_room_ids: List[str] = []
    rate_limited = False
    access_denied = False
    stopped_index = None
    budget_skipped_ids = []
    rate_limit_stage: Optional[str] = None
    rate_limit_room_id: Optional[str] = None

    for index, (rid, result, needs_details, needs_reviews) in enumerate(work, 1):
        if should_cancel and should_cancel():
            return {
                "status": "cancelled", "completed": completed, "total": len(work),
                "failed_room_ids": [entry["room_id"] for entry in failed],
                "failure_count": len(failed), "failure_details": failed,
                "attempted_room_ids": attempted_room_ids,
                "unattempted_room_ids": [entry[0] for entry in work[index - 1:]],
                "rate_limited": False,
            }
        if progress_callback:
            progress_callback(completed, len(work), f"Fetching details and reviews for {rid} ({index}/{len(work)})")
        url = f"https://www.airbnb.com/rooms/{rid}"
        detail_payload = None
        bootstrap_context = None
        detail_failed = False
        review_failed = False
        if needs_details:
            try:
                if rid not in attempted_room_ids:
                    attempted_room_ids.append(rid)
                detail_kwargs = {
                    "room_url": url,
                    # pyairbnb 2.2.2's public annotation says str, but its
                    # price query builder calls .isoformat() on these values.
                    "check_in": date.fromisoformat(definition["check_in"]),
                    "check_out": date.fromisoformat(definition["check_out"]),
                    "adults": definition["adults"],
                    "children": definition["children"],
                    "infants": definition["infants"],
                    "pets": definition["pets"],
                    "currency": definition["currency"],
                }
                detail_payload, bootstrap_context = _fetch_listing_details(collector, detail_kwargs, request_policy, cached_metadata=shared_hits.get(rid, {}).get('metadata'))
                if not isinstance(detail_payload, dict):
                    raise ValueError("Details provider returned an invalid record.")
                detail_payload = dict(detail_payload)
                detail_payload.update({
                    "room_id": rid,
                    "trip_revision": current_revision,
                    "details_fetched_at": datetime.now(timezone.utc).isoformat(),
                    "quote_attempt_status": "success",
                    "availability_state": _availability_state(detail_payload),
                    "quote_party_supported": party_quote_supported,
                    "quote_adults": definition["adults"],
                    "quote_children": definition["children"],
                    "quote_infants": definition["infants"],
                    "quote_pets": definition["pets"],
                    "quote_check_in": definition["check_in"],
                    "quote_check_out": definition["check_out"],
                    "quote_currency": definition["currency"],
                })
                lat, lng = _coordinates(detail_payload)
                if lat is not None and lng is not None:
                    detail_payload["latitude"], detail_payload["longitude"] = lat, lng
                details_by_id[rid] = detail_payload
            except Exception as exc:
                status_code = _http_status_code(exc)
                access_denied = bool(request_policy and status_code in {401, 403})
                unavailable = type(exc).__name__ == "UnavailableError" and status_code != 429
                detail_failed = not unavailable
                failure_record = dict(details_by_id.get(rid, {}))
                failure_record.update({
                    "room_id": rid,
                    "trip_revision": current_revision,
                    "quote_attempt_status": "unavailable" if unavailable else "failed",
                    "availability_state": "unavailable" if unavailable else "unknown",
                    "quote_attempt_at": datetime.now(timezone.utc).isoformat(),
                    "quote_party_supported": party_quote_supported,
                    "quote_adults": definition["adults"],
                    "quote_children": definition["children"],
                    "quote_infants": definition["infants"],
                    "quote_pets": definition["pets"],
                    "quote_check_in": definition["check_in"],
                    "quote_check_out": definition["check_out"],
                    "quote_currency": definition["currency"],
                    "quote_error": str(exc)[:500],
                })
                details_by_id[rid] = failure_record
                if status_code == 429:
                    rate_limited = True
                    rate_limit_stage = "details"
                    rate_limit_room_id = rid
                if not unavailable:
                    failure = {"room_id": rid, "stage": "details", "error": str(exc)[:500]}
                    if status_code is not None:
                        failure["status_code"] = status_code
                    from .request_policy import retryable
                    failure['retryable'] = retryable(exc)
                    if getattr(exc, 'collection_operation', None):
                        failure['operation'] = exc.collection_operation
                    failed.append(failure)
        # A detail quote may differ from the discovery quote. Apply the
        # price range again before spending requests on reviews or AI.
        if selection is not None and detail_payload is not None and party_quote_supported:
            detail_total, _, _ = _explicit_record_total(detail_payload)
            if detail_total is not None:
                mask = results['room_id'].astype(str).eq(rid)
                results.loc[mask, 'selection_quote_total'] = detail_total
                results.loc[mask, 'selection_quote_at'] = detail_payload['details_fetched_at']
                results.loc[mask, 'selection_quote_verified'] = True
                _atomic_csv(results, results_path)
                if (detail_total < selection.get('min_total', 0) or
                        (selection.get('max_total') is not None and detail_total > selection['max_total'])):
                    needs_reviews = False
                    budget_skipped_ids.append(rid)
        if request_policy and details_by_id.get(rid, {}).get('quote_attempt_status') == 'unavailable':
            needs_reviews = False
        if needs_reviews and not (rate_limited or access_denied):
            try:
                saved_detail = details_by_id.get(rid, {})
                review_metadata = {}
                if request_policy:
                    if rid not in attempted_room_ids:
                        attempted_room_ids.append(rid)
                    from .review_collection import collect_recent_reviews, policy_key
                    raw_reviews, review_metadata = collect_recent_reviews(
                        collector, url, output_dir, rid, request_policy.options, request_policy,
                        bootstrap_context=bootstrap_context,
                    )
                    review_metadata.update({
                        'reviews_policy': policy_key(request_policy.options),
                        'reviews_cutoff': request_policy.options.get('review_cutoff'),
                        'reviews_limit': request_policy.options['max_reviews'],
                    })
                    text, raw_json, count = normalize_reviews(raw_reviews)
                else:
                    raw_reviews = (detail_payload or saved_detail).get("reviews")
                    text, raw_json, count = normalize_reviews(raw_reviews)
                    if not text:
                        if rid not in attempted_room_ids:
                            attempted_room_ids.append(rid)
                        review_kwargs = {"room_url": url, "language": "en"}
                        raw_reviews = _call_with_supported(reviews_fn, review_kwargs)
                        text, raw_json, count = normalize_reviews(raw_reviews)
                reviews_by_id[rid] = {
                    "room_id": rid,
                    "reviews_text": text,
                    "reviews_json": raw_json,
                    "reviews_retrieved_count": count,
                    "reviews_fetched_at": datetime.now(timezone.utc).isoformat(),
                    **review_metadata,
                }
            except Exception as exc:
                review_failed = True
                status_code = _http_status_code(exc)
                access_denied = bool(request_policy and status_code in {401, 403})
                if status_code == 429:
                    rate_limited = True
                    rate_limit_stage = "reviews"
                    rate_limit_room_id = rid
                failure = {"room_id": rid, "stage": "reviews", "error": str(exc)[:500]}
                if status_code is not None:
                    failure["status_code"] = status_code
                from .request_policy import retryable
                failure['retryable'] = retryable(exc)
                if getattr(exc, 'collection_operation', None):
                    failure['operation'] = exc.collection_operation
                failed.append(failure)
        if not detail_failed and not review_failed:
            completed += 1

        # The shared cache survives search deletion. Never cache failed attempts.
        shared_cache.store(
            json.loads(pd.DataFrame([details_by_id[rid]]).to_json(orient='records')) if rid in details_by_id else [],
            json.loads(pd.DataFrame([reviews_by_id[rid]]).to_json(orient='records')) if rid in reviews_by_id else [],
            definition,
        )
        # A checkpoint is persisted after every listing, even when one stage failed.
        details_rows = list(details_by_id.values())
        reviews_rows = list(reviews_by_id.values())
        _atomic_csv(pd.DataFrame(details_rows, columns=["room_id"] if not details_rows else None), details_path)
        _atomic_csv(pd.DataFrame(reviews_rows, columns=["room_id"] if not reviews_rows else None), reviews_path)
        _atomic_json({
            "failed": failed,
            "trip_revision": current_revision,
            "rate_limited": rate_limited,
            "attempted_room_ids": attempted_room_ids,
            "unattempted_room_ids": [entry[0] for entry in work[index:]] if rate_limited or access_denied else [],
        }, output_dir / "collection_failures.json")
        persist_merged_snapshot()
        if progress_callback:
            message = f"Checkpointed {index} of {len(work)} listings"
            if rate_limited:
                message = f"Rate limit (HTTP 429) at {rid}; stopped before further requests. Checkpoint saved."
            if access_denied:
                message = "Airbnb denied access; stopped further collection. Checkpoints saved."
            progress_callback(index, len(work), message)
        if rate_limited or access_denied:
            stopped_index = index
            break

    # Final checkpoint also refreshes any quote status after an interrupted run.
    persist_merged_snapshot()
    status = (
        ("partial" if completed else "failed")
        if stopped_index is not None else ("partial" if failed else "success")
    )
    unattempted_room_ids = (
        [entry[0] for entry in work[stopped_index:]] if stopped_index is not None else []
    )
    if rate_limited:
        message = (
            f"Stopped after HTTP 429 during {rate_limit_stage} for listing {rate_limit_room_id}. "
            f"Completed {completed} of {len(work)} listings; {len(unattempted_room_ids)} later listing(s) were not attempted. "
            + ("Request retries exhausted; automatic resume will follow if its limits permit." if request_policy
               else "Wait before retrying; no automatic retry was made.")
        )
    elif access_denied:
        message = "Airbnb denied access. Ready listings were saved and remaining collection is pending."
    else:
        message = "Quotes are keyed to the saved trip revision and are labeled current, stale or unverified from provider parameter support."
    return {
        "status": status,
        "completed": completed,
        "total": len(work),
        "failed_room_ids": [entry["room_id"] for entry in failed],
        "failure_count": len(failed),
        "failure_details": failed,
        "attempted_room_ids": attempted_room_ids,
        "unattempted_room_ids": unattempted_room_ids,
        "rate_limited": rate_limited,
        "skipped_budget_room_ids": budget_skipped_ids,
        "quote_party_supported": party_quote_supported,
        "shared_cache_details": reused_details, "shared_cache_reviews": reused_reviews,
        "message": message,
    }

