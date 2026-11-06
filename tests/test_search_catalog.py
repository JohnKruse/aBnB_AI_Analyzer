"""Focused catalog, timestamp, and workspace lifecycle tests."""

import json
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src import search_catalog, search_service, storage_service


def _workspace(root: Path, name: str = "trip", *, checkout: date | None = None, legacy: bool = False) -> Path:
    checkout = checkout or (date.today() + timedelta(days=15))
    workspace = root / name
    workspace.mkdir(parents=True)
    definition = search_service.validate_trip_definition(
        {
            "location": "Turin",
            "check_in": (checkout - timedelta(days=3)).isoformat(),
            "check_out": checkout.isoformat(),
            "adults": 2,
            "children": 0,
            "infants": 0,
            "pets": 0,
            "currency": "EUR",
            "geometry": {"type": "bbox", "south": 45.0, "west": 7.6, "north": 45.2, "east": 7.8},
        },
        allow_past=True,
    )
    if legacy:
        (workspace / "config.yaml").write_text(yaml.safe_dump(definition), encoding="utf-8")
    else:
        storage_service.write_json(workspace / "search_definition.json", definition)
    return workspace


def test_summary_uses_trip_dates_and_autoarchives_only_after_checkout(tmp_path):
    today = date(2030, 5, 1)
    root = tmp_path / "searches"
    _workspace(root, "past", checkout=today - timedelta(days=1))
    _workspace(root, "today", checkout=today)
    _workspace(root, "future", checkout=today + timedelta(days=1))
    legacy = root / "unknown"
    legacy.mkdir()
    (legacy / "config.yaml").write_text("location: Unknown dates\n", encoding="utf-8")

    summaries = {item["name"]: item for item in search_catalog.list_search_summaries(root, today=today)}
    assert summaries["past"]["archived"] is True
    assert summaries["past"]["archive_source"] == "checkout_date"
    assert summaries["today"]["archived"] is False
    assert summaries["future"]["archived"] is False
    assert summaries["unknown"]["check_out"] is None
    assert summaries["unknown"]["archived"] is False
    assert summaries["unknown"]["trip_details_known"] is False


def test_manual_restore_of_expired_trip_is_tied_to_trip_revision(tmp_path):
    root = tmp_path / "searches"
    workspace = _workspace(root, checkout=date.today() - timedelta(days=2))
    active = search_catalog.set_search_archived("trip", False, root=root)
    assert active["archived"] is False
    assert active["archive_override"] == "active"

    definition_path = workspace / "search_definition.json"
    definition = storage_service.read_json(definition_path)
    definition["check_out"] = (date.today() + timedelta(days=10)).isoformat()
    definition["trip_revision"] += 1
    storage_service.write_json(definition_path, definition)
    refreshed = search_catalog.get_search_summary("trip", root=root)
    assert refreshed["archived"] is False
    assert refreshed["archive_override"] is None

    later = search_catalog.get_search_summary(
        "trip", root=root, today=date.today() + timedelta(days=11)
    )
    assert later["archived"] is True
    assert later["archive_source"] == "checkout_date"


def test_unarchiving_future_trip_clears_override_and_later_autoarchives(tmp_path):
    root = tmp_path / "searches"
    checkout = date.today() + timedelta(days=2)
    _workspace(root, checkout=checkout)
    restored = search_catalog.set_search_archived("trip", False, root=root)
    assert restored["archived"] is False
    assert restored["archive_override"] is None
    later = search_catalog.get_search_summary("trip", root=root, today=checkout + timedelta(days=1))
    assert later["archived"] is True
    assert later["archive_source"] == "checkout_date"


def test_archive_state_is_saved_in_sqlite_and_preserves_other_metadata(tmp_path):
    root = tmp_path / "searches"
    workspace = _workspace(root)
    metadata_path = workspace / "search_metadata.json"
    storage_service.write_json(metadata_path, {"custom_note": "keep me"})
    result = search_catalog.set_search_archived("trip", True, root=root)
    assert result["archived"] is True
    metadata_path.unlink()
    loaded = search_catalog.get_search_summary("trip", root=root)
    assert loaded["archived"] is True
    assert loaded["archive_source"] == "manual"
    assert not metadata_path.exists()  # A read does not regenerate the export.
    metadata = storage_service.read_json(metadata_path)
    assert metadata["custom_note"] == "keep me"


def test_listing_and_review_timestamps_project_from_sqlite_without_reading_frames(tmp_path, monkeypatch):
    root = tmp_path / "searches"
    workspace = _workspace(root)
    output = workspace / "output_data"
    output.mkdir()
    storage_service.write_frame(
        output / "results_df.csv",
        pd.DataFrame({"room_id": ["r1", "r2"], "discovered_at": ["2025-01-02T03:00:00Z", "2025-01-03T03:00:00Z"]}),
        export=False,
    )
    storage_service.write_frame(
        output / "details_df.csv",
        pd.DataFrame({"room_id": ["r1"], "details_fetched_at": ["2025-02-04T03:00:00Z"]}),
        export=False,
    )
    storage_service.write_frame(
        output / "reviews_df.csv",
        pd.DataFrame({"room_id": ["r1"], "reviews_fetched_at": ["2025-03-05T03:00:00Z"]}),
        export=False,
    )
    monkeypatch.setattr(storage_service, "read_frame", lambda *args, **kwargs: pytest.fail("full frame read"))

    summary = search_catalog.get_search_summary("trip", root=root)
    assert summary["listings_updated_at"] == "2025-02-04T03:00:00Z"
    assert summary["listings_updated_at_source"] == "details_fetched_at"
    assert summary["listings_updated_at_is_approximate"] is False
    assert summary["reviews_updated_at"] == "2025-03-05T03:00:00Z"
    assert summary["reviews_updated_at_source"] == "reviews_fetched_at"
    assert summary["reviews_updated_at_is_approximate"] is False


def test_timestamp_projection_cache_invalidates_on_database_revision(tmp_path, monkeypatch):
    root = tmp_path / "searches"
    workspace = _workspace(root)
    output = workspace / "output_data"
    output.mkdir()
    path = output / "results_df.csv"
    storage_service.write_frame(path, pd.DataFrame({"discovered_at": ["2024-01-01T00:00:00Z"]}), export=False)
    calls = 0
    original = search_catalog._database_timestamp

    def counting_query(resource, field):
        nonlocal calls
        calls += 1
        return original(resource, field)

    monkeypatch.setattr(search_catalog, "_database_timestamp", counting_query)
    first = search_catalog.get_search_summary("trip", root=root)
    second = search_catalog.get_search_summary("trip", root=root)
    assert first["listings_updated_at"] == second["listings_updated_at"]
    assert calls == 1

    storage_service.write_frame(path, pd.DataFrame({"discovered_at": ["2026-02-03T00:00:00Z"]}), export=False)
    updated = search_catalog.get_search_summary("trip", root=root)
    assert updated["listings_updated_at"] == "2026-02-03T00:00:00Z"
    assert calls == 2


def test_legacy_csv_falls_back_to_approximate_file_time(tmp_path):
    root = tmp_path / "searches"
    workspace = _workspace(root, legacy=True)
    output = workspace / "output_data"
    output.mkdir()
    path = output / "results_df.csv"
    path.write_text("room_id,discovered_at\nr1,\n", encoding="utf-8")
    expected = 1_700_000_000
    os.utime(path, (expected, expected))

    summary = search_catalog.get_search_summary("trip", root=root)
    assert summary["listings_updated_at"] == "2023-11-14T22:13:20Z"
    assert summary["listings_updated_at_source"] == "file modification time"
    assert summary["listings_updated_at_is_approximate"] is True
    assert not (workspace / "analyzer.sqlite3").exists()


def test_lifecycle_lock_serializes_threads(tmp_path):
    root = tmp_path / "searches"
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def first():
        with search_catalog.search_lifecycle_lock("trip", root=root):
            entered.set()
            assert release.wait(timeout=2)

    def second():
        assert entered.wait(timeout=2)
        with search_catalog.search_lifecycle_lock("trip", root=root):
            finished.set()

    thread_a = threading.Thread(target=first)
    thread_b = threading.Thread(target=second)
    thread_a.start()
    thread_b.start()
    assert entered.wait(timeout=2)
    time.sleep(0.05)
    assert not finished.is_set()
    release.set()
    thread_a.join(timeout=2)
    thread_b.join(timeout=2)
    assert finished.is_set()


@pytest.mark.parametrize(
    "filename,state",
    [
        ("writer.lock", {"pid": os.getpid()}),
        ("active_job.json", {"status": "running", "pid": os.getpid()}),
        ("active_job.json", {"status": "queued", "pid": os.getpid()}),
    ],
)
def test_archive_and_delete_reject_live_job_state(tmp_path, filename, state):
    root = tmp_path / "searches"
    workspace = _workspace(root)
    jobs = workspace / "jobs"
    jobs.mkdir()
    (jobs / filename).write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(search_catalog.SearchLifecycleError):
        search_catalog.set_search_archived("trip", True, root=root)
    with pytest.raises(search_catalog.SearchLifecycleError):
        search_catalog.delete_search("trip", confirmation="trip", root=root)


def test_delete_requires_exact_slug_and_removes_database_and_workspace(tmp_path):
    root = tmp_path / "searches"
    workspace = _workspace(root)
    other = _workspace(root, "keep-me")
    storage_service.write_frame(workspace / "output_data" / "results_df.csv", pd.DataFrame([{"room_id": "123"}]))
    with pytest.raises(ValueError, match="exact search ID"):
        search_catalog.delete_search("trip", confirmation="Trip", root=root)
    assert (workspace / "analyzer.sqlite3").exists()
    search_catalog.delete_search("trip", confirmation="trip", root=root)
    assert not workspace.exists()
    assert not (root / ".trash").exists()
    assert (other / "analyzer.sqlite3").exists()
    assert [item["name"] for item in search_catalog.list_search_summaries(root)] == ["keep-me"]
    with pytest.raises(FileNotFoundError):
        search_catalog.get_search_summary("trip", root=root)


def test_batch_delete_checks_all_jobs_before_deleting_any_search(tmp_path):
    root = tmp_path / "searches"
    first = _workspace(root, "first")
    busy = _workspace(root, "busy")
    keep = _workspace(root, "keep")
    jobs = busy / "jobs"
    jobs.mkdir()
    active = jobs / "active_job.json"
    active.write_text(json.dumps({"status": "running", "pid": os.getpid()}))
    with pytest.raises(search_catalog.SearchLifecycleError):
        search_catalog.delete_searches(["first", "busy"], confirmation=["first", "busy"], root=root)
    assert first.exists() and busy.exists()
    active.unlink()
    search_catalog.delete_searches(["first", "busy"], confirmation=["first", "busy"], root=root)
    assert not first.exists() and not busy.exists()
    assert (keep / "analyzer.sqlite3").exists()


def test_delete_rejects_workspace_symlinks_and_does_not_follow_internal_symlinks(tmp_path):
    root = tmp_path / "searches"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sentinel = elsewhere / "keep.txt"
    sentinel.write_text("keep")
    root.mkdir()
    (root / "trip").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        search_catalog.delete_search("trip", confirmation="trip", root=root)
    (root / "trip").unlink()
    workspace = _workspace(root)
    (workspace / "external").symlink_to(elsewhere, target_is_directory=True)
    search_catalog.delete_search("trip", confirmation="trip", root=root)
    assert sentinel.read_text() == "keep"


def test_catalog_counts_unique_properties_and_people_without_loading_frames(tmp_path, monkeypatch):
    root = tmp_path / "searches"
    workspace = _workspace(root)
    storage_service.write_frame(workspace / "output_data" / "results_df.csv", pd.DataFrame([
        {"room_id": "one", "discovered_at": "2026-11-05T16:00:00Z"},
        {"room_id": "two", "discovered_at": "2026-11-05T16:00:00Z"},
        {"room_id": "one", "discovered_at": "2026-11-05T16:00:00Z"},
    ]))
    monkeypatch.setattr(storage_service, "read_frame", lambda *a, **kw: pytest.fail("Catalog must not load full frames"))
    summary = search_catalog.get_search_summary("trip", root=root)
    assert summary["people"] == 2
    assert summary["properties_count"] == 2
    assert summary["updated_at"]


def test_corrupt_metadata_fails_closed_for_jobs_and_archive_mutation(tmp_path):
    root = tmp_path / "searches"
    workspace = _workspace(root)
    metadata = workspace / "search_metadata.json"
    metadata.write_text("{bad json", encoding="utf-8")
    summary = search_catalog.get_search_summary("trip", root=root)
    assert summary["archived"] is True
    assert summary["archive_source"] == "unknown"
    assert summary["metadata_error"]
    with pytest.raises(ValueError, match="search_metadata.json"):
        search_catalog.set_search_archived("trip", False, root=root)
