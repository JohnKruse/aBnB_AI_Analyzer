"""Validation, geometry and revision tests for browser-created searches."""

from datetime import date, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path

import pytest
import yaml

import src.search_service as search_service


@pytest.mark.parametrize(
    "value, expected",
    [
        (18, 18),
        (18.352051374073234, 18),
        ("18.352051374073234", 18),
        ("18.5", 19),
        ("1.5", 2),
        (Decimal("18.499"), 18),
        ("99", 19),
        ("-99", 2),
    ],
)
def test_normalize_map_zoom_rounds_and_clamps_finite_values(value, expected):
    assert search_service.normalize_map_zoom(value) == expected


@pytest.mark.parametrize(
    "value",
    [True, False, None, "", "  ", "nan", "inf", "-inf", float("nan"), float("inf"), "not a zoom"],
)
def test_normalize_map_zoom_uses_default_for_invalid_values(value):
    assert search_service.normalize_map_zoom(value) == 13


def future_trip(**overrides):
    start = date.today() + timedelta(days=30)
    values = {
        "location": "Turin, Italy",
        "check_in": start.isoformat(),
        "check_out": (start + timedelta(days=4)).isoformat(),
        "adults": 2,
        "children": 1,
        "infants": 0,
        "pets": 0,
        "currency": "EUR",
        "total_budget": 1200,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[7.60, 45.00], [7.80, 45.00], [7.80, 45.20], [7.60, 45.20], [7.60, 45.00]]],
        },
    }
    values.update(overrides)
    return values


def test_validate_party_dates_geometry_and_slug():
    normalized = search_service.validate_trip_definition(future_trip())
    assert normalized["guest_count"] == 3
    assert normalized["currency"] == "EUR"
    assert normalized["free_cancellation"] is False
    assert normalized["bounds"] == {"south": 45.0, "west": 7.6, "north": 45.2, "east": 7.8}
    assert search_service.make_search_slug("Turín, Italy | 2027") == "turin_italy_2027"


def test_free_cancellation_is_validated_and_part_of_trip_revision(tmp_path):
    with pytest.raises(ValueError, match="true or false"):
        search_service.validate_trip_definition(future_trip(free_cancellation="sometimes"))

    root = tmp_path / "searches"
    first = search_service.save_search_definition(
        "turin", future_trip(free_cancellation=True), create=True, root=root
    )
    unchanged = search_service.save_search_definition(
        "turin", future_trip(free_cancellation="yes"), root=root
    )
    changed = search_service.save_search_definition(
        "turin", future_trip(free_cancellation=False), root=root
    )
    assert first["free_cancellation"] is True
    assert unchanged["trip_revision"] == first["trip_revision"]
    assert changed["free_cancellation"] is False
    assert changed["trip_revision"] == first["trip_revision"] + 1
    assert search_service.load_search_definition("turin", root=root)["free_cancellation"] is False
    config = yaml.safe_load((root / "turin" / "config.yaml").read_text())
    assert config["free_cancellation"] is False


def test_legacy_definition_without_free_cancellation_defaults_false(tmp_path):
    root = tmp_path / "searches"
    search_dir = root / "legacy"
    search_dir.mkdir(parents=True)
    values = search_service.validate_trip_definition(future_trip())
    values.pop("free_cancellation")
    (search_dir / "search_definition.json").write_text(json.dumps(values), encoding="utf-8")
    loaded = search_service.load_search_definition("legacy", root=root)
    assert loaded["free_cancellation"] is False

    saved = search_service.save_search_definition("legacy", future_trip(), root=root)
    assert saved["trip_revision"] == values["trip_revision"]


def test_legacy_definition_import_is_idempotent_and_preserves_source_and_audit_time(tmp_path):
    root = tmp_path / "searches"
    search_dir = root / "legacy"
    search_dir.mkdir(parents=True)
    path = search_dir / "search_definition.json"
    values = search_service.validate_trip_definition(future_trip())
    values["created_at"] = "2025-02-03T04:05:06+00:00"
    values["updated_at"] = "2025-03-04T05:06:07+00:00"
    raw = json.dumps(values, indent=2).encode("utf-8")
    path.write_bytes(raw)
    before = path.stat()

    first = search_service.load_search_definition("legacy", root=root)
    second = search_service.load_search_definition("legacy", root=root)

    assert first["updated_at"] == values["updated_at"]
    assert second["updated_at"] == values["updated_at"]
    assert path.read_bytes() == raw
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert (search_dir / "analyzer.sqlite3").exists()


def test_search_definition_remains_available_from_sqlite_after_export_removal(tmp_path):
    root = tmp_path / "searches"
    saved = search_service.save_search_definition("turin", future_trip(), create=True, root=root)
    export = root / "turin" / "search_definition.json"
    export.unlink()

    loaded = search_service.load_search_definition("turin", root=root)

    assert loaded["location"] == saved["location"]
    assert loaded["trip_revision"] == saved["trip_revision"]
    assert loaded["updated_at"] == saved["updated_at"]
    assert not export.exists()


def test_external_definition_edit_is_reimported_and_corrupt_export_is_explicit(tmp_path):
    root = tmp_path / "searches"
    search_service.save_search_definition("turin", future_trip(), create=True, root=root)
    export = root / "turin" / "search_definition.json"
    edited = json.loads(export.read_text(encoding="utf-8"))
    edited.update({
        "adults": 3,
        "guest_count": 4,
        "updated_at": "2026-03-04T05:06:07+00:00",
    })
    export.write_text(json.dumps(edited, indent=2), encoding="utf-8")

    loaded = search_service.load_search_definition("turin", root=root)
    assert loaded["adults"] == 3
    assert loaded["updated_at"] == edited["updated_at"]

    export.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        search_service.load_search_definition("turin", root=root)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"adults": 0}, "adult"),
        ({"adults": 1.5}, "whole number"),
        ({"children": -1}, "negative"),
        ({"total_budget": float("inf")}, "finite"),
        ({"check_out": (date.today() + timedelta(days=20)).isoformat()}, "after check-in"),
        ({"check_in": (date.today() - timedelta(days=1)).isoformat()}, "today or later"),
    ],
)
def test_trip_validation_rejects_bad_context(overrides, message):
    with pytest.raises(ValueError, match=message):
        search_service.validate_trip_definition(future_trip(**overrides))


def test_polygon_includes_boundary_and_rejects_crossing_or_zero_area():
    polygon = future_trip()["geometry"]
    assert search_service.point_in_polygon(45.1, 7.7, polygon)
    assert search_service.point_in_polygon(45.0, 7.7, polygon)
    assert not search_service.point_in_polygon(45.3, 7.7, polygon)

    bowtie = {"type": "Polygon", "coordinates": [[[7.6, 45.0], [7.8, 45.2], [7.8, 45.0], [7.6, 45.2], [7.6, 45.0]]]}
    with pytest.raises(ValueError, match="cross"):
        search_service.normalize_geometry(bowtie)
    zero_area = {"type": "Polygon", "coordinates": [[[7.6, 45.0], [7.7, 45.0], [7.8, 45.0], [7.6, 45.0]]]}
    with pytest.raises(ValueError, match="greater than zero"):
        search_service.normalize_geometry(zero_area)


def test_trip_revision_changes_with_party_and_dates_and_preserves_legacy_yaml(tmp_path):
    root = tmp_path / "searches"
    first = search_service.save_search_definition("turin", future_trip(), create=True, root=root)
    config_path = root / "turin" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["ai"] = {"review_summary": {"questions": ["retain this user prompt"]}}
    config["user_setting"] = "keep-me"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    changed = future_trip(adults=3)
    second = search_service.save_search_definition("turin", changed, root=root)
    assert first["trip_revision"] == 1
    assert second["trip_revision"] == 2
    saved_config = yaml.safe_load(config_path.read_text())
    assert saved_config["user_setting"] == "keep-me"
    assert saved_config["ai"]["review_summary"]["questions"] == ["retain this user prompt"]
    assert search_service.load_search_definition("turin", root=root)["trip_revision"] == 2


def test_create_rejects_duplicate_and_path_traversal(tmp_path):
    root = tmp_path / "searches"
    search_service.save_search_definition("turin", future_trip(), create=True, root=root)
    with pytest.raises(FileExistsError):
        search_service.save_search_definition("turin", future_trip(), create=True, root=root)
    with pytest.raises(ValueError):
        search_service.save_search_definition("../turin", future_trip(), create=True, root=root)


def test_trip_cannot_change_while_search_writer_is_active(tmp_path):
    root = tmp_path / "searches"
    search_service.save_search_definition("turin", future_trip(), create=True, root=root)
    lock_path = root / "turin" / "jobs" / "writer.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "job_id": "active"}), encoding="utf-8")
    with pytest.raises(ValueError, match="active collection or evaluation job"):
        search_service.save_search_definition("turin", future_trip(adults=3), root=root)


def test_geocode_location_handles_empty_and_whitespace():
    assert search_service.geocode_location("") is None
    assert search_service.geocode_location("   ") is None


def test_geocode_location_parses_successful_response(monkeypatch):
    class FakeResponse:
        status_code = 200
        def json(self):
            return [{
                "lat": "45.0677",
                "lon": "7.6824",
                "display_name": "Torino, Piemonte, Italia",
                "boundingbox": ["45.0067", "45.1402", "7.5778", "7.7733"],
            }]

    import requests
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: FakeResponse())
    result = search_service.geocode_location("Turin")
    assert result is not None
    assert pytest.approx(result["lat"], 0.001) == 45.0677
    assert pytest.approx(result["lng"], 0.001) == 7.6824
    assert result["zoom"] == 13
    assert result["bounds"]["south"] == 45.0067
    assert result["display_name"] == "Torino, Piemonte, Italia"


def test_geocode_location_handles_network_error(monkeypatch):
    import requests
    def mock_get(*args, **kwargs):
        raise requests.RequestException("Network unavailable")
    monkeypatch.setattr(requests, "get", mock_get)
    assert search_service.geocode_location("Anywhere") is None


