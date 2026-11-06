"""Offline browser collection can resume from SQLite without compatibility exports."""

import pandas as pd

import review_app
from src import airbnb_service, evaluation_service, pipeline_service, pipeline_status
from src import search_service, storage_service
from src.ui.browser_app import _prepare_dataset
from tests.test_airbnb_service import FakeProvider, setup_search


def test_collection_browsing_and_status_work_after_exports_are_removed(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)
    for module in (evaluation_service, pipeline_service, pipeline_status):
        monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(search_service, "SEARCHES_ROOT", tmp_path / "searches")
    workspace = tmp_path / "searches" / "trip"
    provider = FakeProvider([{
        "room_id": "123", "name": "A quiet apartment", "latitude": 45.1,
        "longitude": 7.7, "person_capacity": 2, "room_type": "Entire home/apt",
        "price": {"total": {"amount": 500}},
    }])
    assert airbnb_service.discover_listings("trip", provider=provider)["discovered"] == 1
    result = airbnb_service.collect_details_and_reviews("trip", provider=provider)
    assert result["status"] == "success"
    assert (workspace / "analyzer.sqlite3").exists()
    collected = storage_service.read_frame(workspace / "output_data" / "merged_df.csv")
    assert collected.loc[0, "total_price"] == 450

    for path in (workspace / "output_data").glob("*.csv"):
        path.unlink()
    (workspace / "search_definition.json").unlink()
    failures = workspace / "output_data" / "collection_failures.json"
    if failures.exists():
        failures.unlink()

    definition = search_service.load_search_definition("trip")
    frame = review_app.load_search_dataset("trip", enrich=False)
    prepared = _prepare_dataset(frame, definition)
    assert prepared.loc[0, "verified_total_price"] == 450
    status = pipeline_status.get_pipeline_status("trip")
    assert status["stages"]["discover"]["count"] == 1
    assert status["stages"]["details"]["state"] == "success"
    assert pipeline_service.get_search_status("trip")["merged_count"] == 1
    calls = (len(provider.detail_calls), len(provider.review_calls))
    resumed = airbnb_service.collect_details_and_reviews("trip", provider=provider)
    assert resumed["completed"] == 0
    assert (len(provider.detail_calls), len(provider.review_calls)) == calls


def test_legacy_import_preserves_files_and_accepts_later_external_csv_edits(tmp_path):
    output = tmp_path / "searches" / "legacy" / "output_data"
    output.mkdir(parents=True)
    path = output / "merged_df.csv"
    pd.DataFrame([{"room_id": "00123", "total_price": 500}]).to_csv(path, index=False)
    original = path.read_bytes()
    imported = storage_service.read_frame(path, dtype={"room_id": str})
    assert imported.loc[0, "room_id"] == "00123"
    assert path.read_bytes() == original
    path.unlink()
    assert storage_service.read_frame(path).loc[0, "total_price"] == 500
    pd.DataFrame([{"room_id": "00123", "total_price": 550}]).to_csv(path, index=False)
    changed = storage_service.read_frame(path, dtype={"room_id": str})
    assert changed.loc[0, "total_price"] == 550
