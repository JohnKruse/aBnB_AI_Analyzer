from __future__ import annotations

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src import storage_service as storage


def test_reader_does_not_reimport_a_compatibility_export_before_its_baseline_commits(tmp_path, monkeypatch):
    path = tmp_path / "search" / "output_data" / "results_df.csv"
    storage.write_frame(path, pd.DataFrame([{"room_id": "1", "metadata": {"old": True}}]))
    replaced = threading.Event()
    reader_detected_export = threading.Event()
    release = threading.Event()
    export = storage._atomic_export_frame
    changed = storage._legacy_changed

    def paused_export(target, frame):
        export(target, frame)
        replaced.set()
        assert release.wait(5)

    def detect(connection, key, target):
        result = changed(connection, key, target)
        if result and replaced.is_set():
            reader_detected_export.set()
        return result

    monkeypatch.setattr(storage, "_atomic_export_frame", paused_export)
    monkeypatch.setattr(storage, "_legacy_changed", detect)
    new = pd.DataFrame([{"room_id": "1", "metadata": {"values": [1, None]}}])
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(storage.write_frame, path, new)
        try:
            assert replaced.wait(5)
            reader = executor.submit(storage.read_frame, path)
            assert reader_detected_export.wait(5)
        finally:
            release.set()
        writer.result(timeout=5)
        result = reader.result(timeout=5)
    assert result.loc[0, "metadata"] == {"values": [1, None]}
    with sqlite3.connect(path.parent.parent / storage.DATABASE_FILENAME) as connection:
        assert connection.execute("SELECT revision FROM resources WHERE resource_path = ?", ("output_data/results_df.csv",)).fetchone()[0] == 2


def test_edit_after_import_parse_is_not_marked_as_already_imported(tmp_path, monkeypatch):
    path = tmp_path / "search" / "output_data" / "results_df.csv"
    path.parent.mkdir(parents=True)
    pd.DataFrame([{"room_id": "1", "price": 100}]).to_csv(path, index=False)
    write = storage._write_frame_db
    edited = []

    def edit_after_parse(connection, key, frame):
        revision = write(connection, key, frame)
        if not edited:
            edited.append(True)
            pd.DataFrame([{"room_id": "1", "price": 200}]).to_csv(path, index=False)
        return revision

    monkeypatch.setattr(storage, "_write_frame_db", edit_after_parse)
    assert storage.read_frame(path).loc[0, "price"] == 100
    assert storage.read_frame(path).loc[0, "price"] == 200


def _frame(rows):
    return pd.DataFrame(rows)


def test_typed_frame_roundtrip_preserves_ids_missingness_and_reserved_payload_keys(tmp_path):
    path = tmp_path / "search" / "output_data" / "merged_df.csv"
    frame = pd.DataFrame({
        "room_id": pd.Series(["00123", "7"], dtype="object"),
        "price": pd.Series([120.5, np.nan], dtype="float64"),
        "native": pd.Series([True, False], dtype="bool"),
        "payload": pd.Series([
            {"$storage": "none", "value": "ordinary provider content"},
            {"nested": [{"$storage": "date", "value": "2026-01-01"}, None]},
        ], dtype="object"),
        "optional": pd.Series([pd.NA, None], dtype="object"),
    })

    storage.write_frame(path, frame, export=False)
    restored = storage.read_frame(path)

    assert restored["room_id"].tolist() == ["00123", "7"]
    assert restored["native"].tolist() == [True, False]
    assert restored.loc[0, "payload"] == frame.loc[0, "payload"]
    assert restored.loc[1, "payload"] == frame.loc[1, "payload"]
    assert pd.isna(restored.loc[0, "optional"])
    assert restored.loc[1, "optional"] is None
    assert str(restored["price"].dtype) == "float64"


def test_legacy_import_preserves_leading_zero_room_ids_and_migration_is_idempotent(tmp_path):
    workspace = tmp_path / "search"
    path = workspace / "output_data" / "results_df.csv"
    path.parent.mkdir(parents=True)
    path.write_text("room_id,total_price\n00123,42.5\n", encoding="utf-8")

    first = storage.migrate_workspace(workspace)
    second = storage.migrate_workspace(workspace)

    assert first["imported_frames"] == 1
    assert second["imported_frames"] == 0
    assert storage.read_frame(path).loc[0, "room_id"] == "00123"
    assert path.read_text(encoding="utf-8").startswith("room_id,total_price")


def test_signature_is_optional_cheap_and_tracks_database_only_writes(tmp_path):
    path = tmp_path / "search" / "output_data" / "merged_df.csv"
    assert storage.signature(path) is None
    path.parent.mkdir(parents=True)
    path.write_text("room_id,value\n00123,1\n", encoding="utf-8")
    legacy_token = storage.signature(path)
    assert legacy_token and legacy_token.startswith("legacy:")

    loaded = storage.read_frame(path)
    database_token = storage.signature(path)
    assert loaded.loc[0, "room_id"] == "00123"
    assert database_token != legacy_token
    assert storage.signature(path) == database_token

    storage.write_frame(path, _frame([{"room_id": "00123", "value": 2}]), export=False)
    changed_token = storage.signature(path)
    assert changed_token != database_token
    assert storage.read_frame(path).loc[0, "value"] == 2


def test_same_size_external_replacement_is_detected_even_when_mtime_is_restored(tmp_path):
    workspace = tmp_path / "search"
    path = workspace / "output_data" / "results_df.csv"
    path.parent.mkdir(parents=True)
    path.write_text("room_id,value\n00123,1\n", encoding="utf-8")
    storage.migrate_workspace(workspace)
    previous_stat = path.stat()

    replacement = path.with_suffix(".new")
    replacement.write_text("room_id,value\n00123,2\n", encoding="utf-8")
    os.utime(replacement, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
    os.replace(replacement, path)

    assert storage.read_frame(path).loc[0, "value"] == 2


def test_database_write_survives_removed_legacy_export_and_exports_on_request(tmp_path):
    workspace = tmp_path / "search"
    frame_path = workspace / "output_data" / "merged_df.csv"
    json_path = workspace / "search_definition.json"
    storage.write_frame(frame_path, _frame([{"room_id": "1", "value": 3}]), export=False)
    storage.write_json(json_path, {"optional": None, "nested": {"active": True}}, export=False)

    assert not frame_path.exists()
    assert storage.exists(frame_path)
    assert storage.read_frame(frame_path).loc[0, "value"] == 3
    assert storage.read_json(json_path) == {"optional": None, "nested": {"active": True}}

    exported = storage.export_workspace(workspace, tmp_path / "portable")
    assert len(exported) == 2
    exported_json = tmp_path / "portable" / "search_definition.json"
    assert json.loads(exported_json.read_text(encoding="utf-8")) == {"optional": None, "nested": {"active": True}}


def test_corrupt_external_json_is_reported_without_replacing_database_value(tmp_path):
    workspace = tmp_path / "search"
    path = workspace / "user_curation.json"
    storage.write_json(path, {"manual_order": ["1"]})
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(storage.StorageFormatError, match="Legacy JSON is unreadable"):
        storage.read_json(path)

    # The external error is visible; the canonical DB payload remains intact.
    path.unlink()
    assert storage.read_json(path) == {"manual_order": ["1"]}


def test_mid_write_error_rolls_back_entire_frame(tmp_path, monkeypatch):
    path = tmp_path / "search" / "output_data" / "merged_df.csv"
    storage.write_frame(path, _frame([{"room_id": "old", "value": 1}]), export=False)
    original = storage._row_payload
    calls = 0

    def fail_second(row):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected row serialization failure")
        return original(row)

    monkeypatch.setattr(storage, "_row_payload", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        storage.write_frame(path, _frame([{"room_id": "new-a", "value": 2}, {"room_id": "new-b", "value": 3}]), export=False)
    monkeypatch.setattr(storage, "_row_payload", original)
    assert storage.read_frame(path).to_dict(orient="records") == [{"room_id": "old", "value": 1}]


def test_concurrent_writers_leave_one_complete_frame(tmp_path):
    path = tmp_path / "search" / "output_data" / "results_df.csv"

    def write(index):
        rows = [{"room_id": f"{index}-{row}", "writer": index, "value": row} for row in range(20)]
        storage.write_frame(path, _frame(rows), export=False)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(8)))

    loaded = storage.read_frame(path)
    assert len(loaded) == 20
    assert loaded["writer"].nunique() == 1
    assert set(loaded["value"]) == set(range(20))


def test_quotes_keep_prior_trip_contexts_and_quote_key_includes_party(tmp_path):
    workspace = tmp_path / "search"
    path = workspace / "output_data" / "merged_df.csv"
    first = pd.DataFrame([{
        "room_id": "1", "trip_revision": 1, "search_quote_check_in": "2027-01-01",
        "search_quote_check_out": "2027-01-03", "search_quote_currency": "EUR",
        "search_quote_adults": 2, "search_quote_children": 0, "search_quote_infants": 0,
        "search_quote_pets": 0, "total_price": 200.0, "quote_status": "current",
        "quote_source": "discovery:explicit_total",
    }])
    second = first.copy()
    second["trip_revision"] = 2
    second["search_quote_check_in"] = "2027-02-01"
    second["search_quote_check_out"] = "2027-02-03"
    second["search_quote_adults"] = 3
    second["total_price"] = 300.0
    storage.write_frame(path, first, export=False)
    storage.write_frame(path, second, export=False)

    with sqlite3.connect(workspace / storage.DATABASE_FILENAME) as connection:
        rows = connection.execute("SELECT trip_revision, check_in, adults, total_price FROM quotes ORDER BY trip_revision").fetchall()
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert schema_version == storage.SCHEMA_VERSION
    assert rows == [(1, "2027-01-01", 2, 200.0), (2, "2027-02-01", 3, 300.0)]


def test_shortlist_snapshots_are_ordered_immutable_and_reject_unknown_ids(tmp_path):
    workspace = tmp_path / "search"
    frame = _frame([{"room_id": "1", "name": "first"}, {"room_id": "2", "name": "second"}])
    snapshot_id = storage.save_shortlist_snapshot(workspace, frame, ["2", "1"], metadata={"revision": 4})

    updated = _frame([{"room_id": "1", "name": "changed"}, {"room_id": "2", "name": "changed too"}])
    loaded = storage.load_shortlist_snapshot(workspace, snapshot_id)
    assert loaded["room_ids"] == ["2", "1"]
    assert loaded["frame"]["name"].tolist() == ["second", "first"]
    assert loaded["metadata"] == {"revision": 4}
    assert updated["name"].tolist() == ["changed", "changed too"]

    with pytest.raises(ValueError, match="absent from the supplied frame"):
        storage.save_shortlist_snapshot(workspace, frame, ["missing"])


def test_search_context_and_curation_have_normalized_rows(tmp_path):
    workspace = tmp_path / "search"
    storage.write_json(workspace / "search_definition.json", {
        "trip_revision": 7, "check_in": "2027-05-01", "check_out": "2027-05-04",
        "currency": "EUR", "adults": 2, "children": 1, "infants": 0,
        "pets": 0, "free_cancellation": False, "total_budget": 500,
        "geometry": {"type": "rectangle"},
    }, export=False)
    storage.write_json(workspace / "user_curation.json", {
        "shortlisted_room_ids": ["2"], "hidden_room_ids": ["1"], "manual_order": ["2", "1"],
    }, export=False)

    with sqlite3.connect(workspace / storage.DATABASE_FILENAME) as connection:
        context = connection.execute("SELECT trip_revision, check_in, free_cancellation FROM search_context").fetchone()
        curation = connection.execute("SELECT room_id, shortlisted, hidden, manual_order FROM curation_items ORDER BY manual_order").fetchall()
    assert context == (7, "2027-05-01", 0)
    assert curation == [("2", 1, 0, 0), ("1", 0, 1, 1)]

