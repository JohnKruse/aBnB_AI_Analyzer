"""Price selection is separate from the trip and collected evidence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from pathlib import Path

import pandas as pd

from . import storage_service as storage

CACHE_DAYS = 30


def recent(value, *, now=None):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (now or datetime.now(timezone.utc))-stamp
        return timedelta(0) <= age <= timedelta(days=CACHE_DAYS)
    except (ValueError, TypeError):
        return False


def load_selection(directory: Path, definition):
    value = storage.read_json(directory / 'processing_selection.json', default=None)
    if not isinstance(value, dict) or value.get('trip_revision') != definition.get('trip_revision'):
        return None
    return value


def save_selection(directory: Path, definition, value):
    maximum = value.get('max_total')
    if maximum is not None:
        maximum = float(maximum)
        if not math.isfinite(maximum) or maximum <= 0:
            raise ValueError('The processing threshold must be a positive total-stay amount.')
    saved = {'trip_revision': definition['trip_revision'], 'max_total': maximum,
             'include_unknown': bool(value.get('include_unknown', False)),
             'updated_at': datetime.now(timezone.utc).isoformat()}
    storage.write_json(directory / 'processing_selection.json', saved)
    return saved


def select_properties(frame, definition, selection):
    """Never use nightly amounts, old-trip quotes, or absent search results."""
    from .airbnb_service import _search_quote_status
    current = frame.copy()
    if 'trip_revision' in current:
        current = current[pd.to_numeric(current['trip_revision'], errors='coerce').eq(definition['trip_revision'])]
    if 'discovery_state' in current:
        current = current[~current['discovery_state'].eq('not_returned')]
    amounts = pd.to_numeric(current.get('search_quote_total', pd.Series(float('nan'), index=current.index)), errors='coerce')
    detail_amounts = pd.to_numeric(current.get('selection_quote_total', pd.Series(float('nan'), index=current.index)), errors='coerce')
    quote_at = pd.to_datetime(current.get('selection_quote_at', pd.Series(index=current.index, dtype=object)), errors='coerce', utc=True)
    discovered_at = pd.to_datetime(current.get('discovered_at', pd.Series(index=current.index, dtype=object)), errors='coerce', utc=True)
    from_details = current.get('selection_quote_verified', pd.Series(False, index=current.index)).eq(True) & quote_at.notna() & (discovered_at.isna() | quote_at.ge(discovered_at))
    amounts = amounts.where(~from_details, detail_amounts)
    valid = amounts.gt(0) & amounts.map(lambda x: math.isfinite(x) if pd.notna(x) else False)
    if not current.empty:
        valid &= from_details | current.apply(lambda row: _search_quote_status(row, definition) == 'current', axis=1)
    if selection is None:
        selected = current
    else:
        within = valid if selection['max_total'] is None else valid & amounts.le(selection['max_total'])
        if selection.get('include_unknown'):
            within |= ~valid
        selected = current[within]
    return {'selected_ids': selected.get('room_id', pd.Series(dtype=str)).astype(str).tolist(),
            'current_count': len(current), 'known_prices': amounts[valid],
            'unknown_count': int((~valid).sum()), 'excluded_count': len(current)-len(selected)}


def retained_results(previous, fresh, revision, stamp):
    """Preserve previously seen IDs; absence from a search is not proof of unavailability."""
    fresh = fresh.copy()
    fresh['discovery_state'] = 'returned'
    fresh['last_seen_at'] = stamp
    if previous.empty or 'room_id' not in previous:
        return fresh
    old = previous[~previous['room_id'].astype(str).isin(set(fresh.get('room_id', pd.Series(dtype=str)).astype(str)))].copy()
    if 'trip_revision' in old:
        old = old[pd.to_numeric(old['trip_revision'], errors='coerce').eq(revision)]
    old['discovery_state'] = 'not_returned'
    old['discovery_checked_at'] = stamp
    if fresh.empty:
        return old.reset_index(drop=True)
    if old.empty:
        return fresh.reset_index(drop=True)
    return pd.concat([fresh, old], ignore_index=True, sort=False)


def price_slider_limits(prices, saved_max=None, current=None):
    """Currency-independent 50-unit steps with a median rounded upward."""
    values = pd.to_numeric(pd.Series(prices), errors='coerce')
    values = values[values.gt(0) & values.map(lambda value: math.isfinite(value) if pd.notna(value) else False)]
    rounded = lambda value: max(50, int(math.ceil(float(value)/50)*50))
    default = rounded(saved_max if saved_max is not None else (values.median() if not values.empty else 1000))
    upper = max(100, default, rounded(values.max()) if not values.empty else 1000,
                rounded(current) if current is not None else 50)
    return 50, upper, default
