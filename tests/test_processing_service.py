from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
from src import processing_service as processing


def test_threshold_is_separate_from_trip_and_uses_only_current_stay_quotes(tmp_path):
    definition = {'trip_revision': 3, 'check_in': '2027-01-01', 'check_out': '2027-01-04',
                  'currency': 'EUR', 'adults': 2, 'children': 0, 'infants': 0, 'pets': 0}
    rows = []
    for rid, amount in [('cheap', 150), ('expensive', 600), ('unknown', None)]:
        rows.append({'room_id': rid, 'trip_revision': 3, 'search_quote_total': amount,
                     'search_quote_check_in': definition['check_in'], 'search_quote_check_out': definition['check_out'],
                     'search_quote_currency': 'EUR', 'search_quote_party_supported': True})
    frame = pd.DataFrame(rows)
    saved = processing.save_selection(tmp_path, definition, {'max_total': 200})
    assert definition['trip_revision'] == 3
    assert processing.select_properties(frame, definition, saved)['selected_ids'] == ['cheap']
    saved = processing.save_selection(tmp_path, definition, {'max_total': 700, 'include_unknown': True})
    assert processing.select_properties(frame, definition, saved)['selected_ids'] == ['cheap', 'expensive', 'unknown']
    ranged = processing.save_selection(tmp_path, definition, {'min_total': 200, 'max_total': 700})
    assert ranged['min_total'] == 200
    assert processing.load_selection(tmp_path, definition) == ranged
    assert processing.select_properties(frame, definition, ranged)['selected_ids'] == ['expensive']
    assert processing.select_properties(frame, definition, {**ranged, 'include_unknown': True})['selected_ids'] == ['expensive', 'unknown']
    frame.loc[0, 'search_quote_check_in'] = '2026-01-01'
    assert processing.select_properties(frame, definition, {**saved, 'include_unknown': False})['selected_ids'] == ['expensive']


def test_retained_absent_rows_are_not_selected_or_called_unavailable():
    stamp = datetime.now(timezone.utc).isoformat()
    previous = pd.DataFrame([{'room_id': 'one', 'trip_revision': 1}, {'room_id': 'two', 'trip_revision': 1}])
    latest = pd.DataFrame([{'room_id': 'two', 'trip_revision': 1}])
    retained = processing.retained_results(previous, latest, 1, stamp)
    assert set(retained.room_id) == {'one', 'two'}
    assert retained.set_index('room_id').loc['one', 'discovery_state'] == 'not_returned'
    assert processing.select_properties(retained, {'trip_revision': 1}, None)['selected_ids'] == ['two']


def test_cache_freshness_expires_at_30_days():
    now = datetime.now(timezone.utc)
    assert processing.recent((now-timedelta(days=29)).isoformat(), now=now)
    assert not processing.recent((now-timedelta(days=31)).isoformat(), now=now)
    assert not processing.recent(None, now=now)


def test_price_slider_rounds_median_up_to_50_and_keeps_saved_choice():
    lower, upper, default = processing.price_slider_limits([700, 840, 1100])
    assert (lower, upper, default) == (50, 1100, 850)
    assert processing.price_slider_limits([800, 900])[2] == 850
    assert processing.price_slider_limits([1, 20])[2] == 50
    assert processing.price_slider_limits([700, 840], saved_max=901)[2] == 950
    assert processing.price_slider_limits([700, 840], current=1500)[1] == 1500


def test_processing_range_rejects_invalid_bounds(tmp_path):
    import pytest
    for minimum, maximum in [(-50, 500), (float('nan'), 500), (600, 500), (float('inf'), None)]:
        with pytest.raises(ValueError):
            processing.save_selection(tmp_path, {'trip_revision': 1},
                                      {'min_total': minimum, 'max_total': maximum})
