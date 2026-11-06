"""Portable report map behavior without external network requests."""
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from src.report_service import build_property_report


def test_export_map_works_without_a_website_referrer():
    html = build_property_report(pd.DataFrame(), title="Trip", search_name="trip")
    assert "tile.openstreetmap.org/" not in html
    assert "Canvas/World_Light_Gray_Base/MapServer/tile/" in html
    assert "Tiles ©" in html and "OpenStreetMap</a> contributors" in html
    assert "Some street tiles could not load" in html


def test_report_map_tile_lifecycle():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the report JavaScript behavior test")
    subprocess.run([node, str(Path(__file__).with_name("report_map.cjs"))], check=True)


def _payload(html):
    import json
    import re

    return json.loads(re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html, re.S).group(1))


def test_export_preserves_joined_evidence_trip_links_and_curated_order():
    from datetime import datetime, timezone
    from urllib.parse import parse_qs, urlparse

    frame = pd.DataFrame([
        {"room_id": "one", "description": "Quiet", "personal_fit_breakdown": {"quiet_sleep": {"score": 5}}, "verified_total_price": 800},
        {"room_id": "two", "reviews_text": "Very clean", "verified_total_price": float("nan")},
        {"room_id": "hidden", "description": "Do not export"},
    ])
    html = build_property_report(
        frame, title="Turin winter", search_name="turin",
        definition={"check_in": "2027-01-02", "check_out": "2027-01-06", "adults": 2, "children": 1, "currency": "EUR"},
        profile={"name": "Quiet", "model_name": "test-model", "api_key": "must-not-export"},
        curation={"manual_order": ["two", "one"], "hidden_room_ids": ["hidden"], "shortlisted_room_ids": ["one", "hidden", "outside"]},
        created_at=datetime(2026, 11, 5, 12, 30, tzinfo=timezone.utc),
    )
    payload = _payload(html)
    assert payload["title"] == "Turin winter"
    assert payload["created_at"] == "2026-11-05T12:30:00+00:00"
    assert [row["room_id"] for row in payload["rows"]] == ["two", "one"]
    assert payload["rows"][0]["verified_total_price"] is None
    assert payload["rows"][1]["personal_fit_breakdown"]["quiet_sleep"]["score"] == 5
    assert payload["state"]["favorites"] == ["one"]
    query = parse_qs(urlparse(payload["rows"][1]["airbnb_url"]).query)
    assert query == {"check_in": ["2027-01-02"], "check_out": ["2027-01-06"], "adults": ["2"], "children": ["1"]}
    assert "must-not-export" not in html
    assert '<script src=' not in html and '<link rel="stylesheet"' not in html


def test_export_escapes_script_terminators_in_all_embedded_content():
    from html.parser import HTMLParser

    hostile = '</script><script>window.injected=true</script><img src=x onerror=alert(1)>'
    html = build_property_report(pd.DataFrame([{"room_id": "x/?", "name": hostile, "reviews_text": hostile}]), title=hostile, search_name="trip")
    payload = _payload(html)
    assert payload["title"] == hostile
    assert payload["rows"][0]["reviews_text"] == hostile
    assert payload["rows"][0]["airbnb_url"].endswith("/x%2F%3F")

    class Scripts(HTMLParser):
        count = 0

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.count += 1

    parser = Scripts()
    parser.feed(html)
    assert parser.count == 2  # The JSON payload and the bundled application only.


def test_export_includes_offline_train_and_tube_icons_and_putney_stations():
    html = build_property_report(pd.DataFrame(), title="Putney", search_name="putney")
    transit = _payload(html)['transit']
    assert 'East Putney' in {e['tags']['name'] for e in transit['elements']}
    assert 'Putney' in {e['tags']['name'] for e in transit['elements']}
    assert transit['icons']['rail'] != transit['icons']['metro']
    assert '<svg' in transit['icons']['rail']
    assert '<circle' in transit['icons']['metro']
    assert 'id="stations"' in html


def test_export_defaults_to_ai_fit_descending():
    html = build_property_report(pd.DataFrame(), title='Trip', search_name='trip')
    assert _payload(html)['state']['sort'] == {'key':'fit','direction':-1}
    assert 'sort={key:"fit",direction:-1};state.sort=sort' in html
