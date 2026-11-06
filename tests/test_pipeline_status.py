"""Saved-data pipeline status tests. All inputs are local fixtures."""

import json
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import src.evaluation_service as evaluation_service
import src.job_service as job_service
import src.pipeline_status as pipeline_status
import src.search_service as search_service
import src.storage_service as storage_service


def _setup(tmp_path, monkeypatch, *, profile=True):
    monkeypatch.setattr(pipeline_status, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(job_service, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(evaluation_service, "PROJECT_ROOT", tmp_path)
    start = date.today() + timedelta(days=30)
    definition = search_service.save_search_definition(
        "trip",
        {
            "location": "Turin",
            "check_in": start.isoformat(),
            "check_out": (start + timedelta(days=4)).isoformat(),
            "adults": 2,
            "children": 0,
            "infants": 0,
            "pets": 0,
            "currency": "EUR",
            "geometry": {"type": "bbox", "south": 45.0, "west": 7.6, "north": 45.2, "east": 7.8},
        },
        create=True,
        root=tmp_path / "searches",
    )
    output = tmp_path / "searches" / "trip" / "output_data"
    output.mkdir(parents=True, exist_ok=True)
    if profile:
        evaluation_service.save_evaluation_profile(
            "trip", "Default", {"cleanliness": 3, "value": 2},
            additional_instructions="Use only the saved trip evidence.",
        )
    return definition, output


def _write_results(output, room_ids, revision=1):
    pd.DataFrame([
        {"room_id": room_id, "trip_revision": revision, "name": f"Listing {room_id}"}
        for room_id in room_ids
    ]).to_csv(output / "results_df.csv", index=False)


def _write_detail(output, room_id, revision=1, status="success", availability="available"):
    row = {
        "room_id": room_id,
        "trip_revision": revision,
        "quote_attempt_status": status,
        "availability_state": availability,
        "details_fetched_at": "2026-10-01T12:00:00+00:00" if status == "success" else None,
    }
    path = output / "details_df.csv"
    previous = pd.read_csv(path, dtype={"room_id": str}).to_dict("records") if path.exists() else []
    pd.DataFrame([*previous, row]).to_csv(path, index=False)


def _write_review(output, room_id, text=""):
    path = output / "reviews_df.csv"
    previous = pd.read_csv(path, dtype={"room_id": str}).to_dict("records") if path.exists() else []
    pd.DataFrame([*previous, {"room_id": room_id, "reviews_text": text, "reviews_retrieved_count": 0}]).to_csv(path, index=False)


def _write_merged(output, room_ids, revision=1):
    pd.DataFrame([
        {"room_id": room_id, "trip_revision": revision, "name": f"Listing {room_id}"}
        for room_id in room_ids
    ]).to_csv(output / "merged_df.csv", index=False)


def test_status_reports_current_revision_and_marks_old_rows_stale(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["old"], revision=definition["trip_revision"] - 1)

    status = pipeline_status.get_pipeline_status("trip")

    assert status["stages"]["discover"]["state"] == "stale"
    assert status["stages"]["discover"]["count"] == 0
    assert status["stages"]["discover"]["stale"] == 1
    assert status["stages"]["details"]["state"] == "blocked"
    assert status["next_stage"] == "discover"


def test_unavailable_details_are_terminal_and_excluded_from_evaluation_scope(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["available", "unavailable"], definition["trip_revision"])
    _write_detail(output, "available", definition["trip_revision"])
    _write_detail(output, "unavailable", definition["trip_revision"], "unavailable", "unavailable")
    _write_review(output, "available", "")

    status = pipeline_status.get_pipeline_status("trip")

    details = status["stages"]["details"]
    assert details["state"] == "success"
    assert details["count"] == 2
    assert details["details"]["attempted"] == 2
    assert details["details"]["succeeded"] == 1
    assert details["details"]["unavailable"] == 1
    assert details["details"]["failed"] == 0
    assert details["details"]["missing"] == 0
    assert details["reviews"]["succeeded"] == 1
    assert details["reviews"]["missing"] == 0
    assert status["eligible_room_ids"] == ["available"]
    assert status["stages"]["evaluate"]["skipped_unavailable"] == 1


def test_review_failure_is_counted_separately_from_successful_details(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["r1"], definition["trip_revision"])
    _write_detail(output, "r1", definition["trip_revision"])
    (output / "collection_failures.json").write_text(json.dumps({
        "trip_revision": definition["trip_revision"],
        "failed": [{"room_id": "r1", "stage": "reviews", "error": "review provider failed"}],
    }), encoding="utf-8")

    details = pipeline_status.get_pipeline_status("trip")["stages"]["details"]

    assert details["state"] == "failed"
    assert details["failed"] == 1
    assert details["details"]["succeeded"] == 1
    assert details["details"]["failed"] == 0
    assert details["reviews"]["succeeded"] == 0
    assert details["reviews"]["failed"] == 1
    assert details["reviews"]["missing"] == 0


def test_nan_fetch_timestamp_without_success_status_is_not_a_completed_detail(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["r1"], definition["trip_revision"])
    pd.DataFrame([{
        "room_id": "r1", "trip_revision": definition["trip_revision"],
        "quote_attempt_status": "", "details_fetched_at": float("nan"),
    }]).to_csv(output / "details_df.csv", index=False)
    _write_review(output, "r1", "")

    details = pipeline_status.get_pipeline_status("trip")["stages"]["details"]

    assert details["state"] == "ready"
    assert details["details"]["succeeded"] == 0
    assert details["details"]["missing"] == 1
    assert details["completed"] is False


def test_rate_limit_counts_unique_failed_and_later_unattempted_ids_separately(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["r1", "r2", "r3"], definition["trip_revision"])
    _write_detail(output, "r1", definition["trip_revision"])
    _write_review(output, "r1", "")
    failure = {
        "room_id": "r2", "stage": "details", "status_code": 429,
        "error": "HTTP 429 Too Many Requests; api_key=secret-value",
    }
    (output / "collection_failures.json").write_text(json.dumps({
        "trip_revision": definition["trip_revision"],
        "rate_limited": True,
        "attempted_room_ids": ["r1", "r2"],
        "unattempted_room_ids": ["r3"],
        "failed": [failure, failure],
        "failure_details": [failure, failure],
    }), encoding="utf-8")

    details = pipeline_status.get_pipeline_status("trip")["stages"]["details"]

    assert details["state"] == "partial"
    assert details["rate_limited"] is True
    assert details["failed"] == 1
    assert details["failed_room_ids"] == ["r2"]
    assert details["unattempted_room_ids"] == ["r3"]
    assert details["details"]["attempted"] == 2
    assert details["details"]["failed"] == 1
    assert details["details"]["unattempted"] == 1
    assert details["reviews"]["succeeded"] == 1
    assert details["reviews"]["failed"] == 0
    assert details["reviews"]["unattempted"] == 2
    assert "429" in details["reason"]
    assert "secret-value" not in details["reason"]


def test_current_unknown_assessments_complete_evaluation_without_numeric_score(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["r1"], definition["trip_revision"])
    _write_detail(output, "r1", definition["trip_revision"])
    _write_review(output, "r1", "")

    def hydrate(search_name, frame, profile_name):
        hydrated = frame.copy()
        hydrated["personal_fit_status"] = "unknown"
        hydrated["personal_fit_profile"] = profile_name
        return hydrated

    monkeypatch.setattr(evaluation_service, "hydrate_personal_evaluations", hydrate)
    stage = pipeline_status.get_pipeline_status("trip")["stages"]["evaluate"]

    assert stage["state"] == "success"
    assert stage["completed"] is True
    assert stage["count"] == 1
    assert stage["scored_count"] == 0
    assert stage["insufficient_evidence_count"] == 1
    assert stage["missing"] == 0


def test_evaluation_job_receives_only_available_listing_ids(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["available", "unavailable"], definition["trip_revision"])
    _write_detail(output, "available", definition["trip_revision"])
    _write_detail(output, "unavailable", definition["trip_revision"], "unavailable", "unavailable")
    _write_review(output, "available", "")
    received = []

    def fake_evaluation(search_name, **kwargs):
        received.extend(kwargs["eligible_room_ids"])
        return {
            "status": "blocked", "rated": 0, "failed": 0, "eligible": 1,
            "total": 2, "message": "Fixture blocked without calling an AI provider.",
        }

    monkeypatch.setattr(evaluation_service, "run_personalized_evaluation", fake_evaluation)
    job_service.start_search_job("trip", "evaluate")
    manifest = job_service._FUTURES[job_service.get_search_job("trip")["job_id"]].result(timeout=3)
    terminal = job_service.get_search_job("trip")

    assert received == ["available"]
    assert terminal["status"] == "blocked"
    assert terminal["status"] != "completed"
    assert terminal["stage_results"]["evaluate"]["status"] == "blocked"


def test_status_reuses_compact_csv_projections_and_hydrated_evaluation_map(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["r1"], definition["trip_revision"])
    _write_detail(output, "r1", definition["trip_revision"])
    _write_review(output, "r1", "")
    _write_merged(output, ["r1"], definition["trip_revision"])
    hydration_calls = []

    def hydrate(search_name, frame, profile_name):
        hydration_calls.append(tuple(frame["room_id"].astype(str)))
        hydrated = frame.copy()
        hydrated["personal_fit_status"] = "unknown"
        hydrated["personal_fit_profile"] = profile_name
        return hydrated

    monkeypatch.setattr(evaluation_service, "hydrate_personal_evaluations", hydrate)
    original_read_csv = pd.read_csv
    read_counts = {}

    def count_read_csv(path, *args, **kwargs):
        name = Path(path).name
        read_counts[name] = read_counts.get(name, 0) + 1
        return original_read_csv(path, *args, **kwargs)

    monkeypatch.setattr(pipeline_status.pd, "read_csv", count_read_csv)
    first = pipeline_status.get_pipeline_status("trip")
    second = pipeline_status.get_pipeline_status("trip")

    assert first["eligible_room_ids"] == second["eligible_room_ids"] == ["r1"]
    assert hydration_calls == [("r1",)]
    assert read_counts == {
        "results_df.csv": 1,
        "details_df.csv": 1,
        "reviews_df.csv": 1,
        "merged_df.csv": 1,
    }


def test_status_projection_and_evaluation_cache_invalidate_after_snapshot_write(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["r1"], definition["trip_revision"])
    _write_detail(output, "r1", definition["trip_revision"])
    _write_review(output, "r1", "")
    _write_merged(output, ["r1"], definition["trip_revision"])
    hydration_calls = []

    def hydrate(search_name, frame, profile_name):
        hydration_calls.append(tuple(frame["room_id"].astype(str)))
        hydrated = frame.copy()
        hydrated["personal_fit_status"] = "unknown"
        hydrated["personal_fit_profile"] = profile_name
        return hydrated

    monkeypatch.setattr(evaluation_service, "hydrate_personal_evaluations", hydrate)
    status = pipeline_status.get_pipeline_status("trip")
    assert status["stages"]["details"]["details"]["succeeded"] == 1

    # Details progress must update immediately, while an unchanged merged CSV
    # keeps the independent evaluation map warm.
    pd.DataFrame([{
        "room_id": "r1",
        "trip_revision": definition["trip_revision"],
        "quote_attempt_status": "unavailable",
        "availability_state": "unavailable",
        "details_fetched_at": None,
    }]).to_csv(output / "details_df.csv", index=False)
    updated = pipeline_status.get_pipeline_status("trip")
    assert updated["stages"]["details"]["details"]["unavailable"] == 1
    assert hydration_calls == [("r1",)]

    _write_results(output, ["r1", "r2"], definition["trip_revision"])
    _write_merged(output, ["r1", "r2"], definition["trip_revision"])
    expanded = pipeline_status.get_pipeline_status("trip")
    assert expanded["eligible_room_ids"] == ["r2"]
    assert hydration_calls[-1] == ("r1", "r2")


def test_status_csv_cache_detects_atomic_replace_with_same_size_and_mtime(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    results_path = output / "results_df.csv"
    _write_results(output, ["r1"], definition["trip_revision"])
    _write_merged(output, ["r1"], definition["trip_revision"])
    original_stat = results_path.stat()
    original_read_csv = pd.read_csv
    result_reads = []

    def count_read_csv(path, *args, **kwargs):
        if Path(path).name == "results_df.csv":
            result_reads.append(path)
        return original_read_csv(path, *args, **kwargs)

    monkeypatch.setattr(pipeline_status.pd, "read_csv", count_read_csv)
    assert pipeline_status.get_pipeline_status("trip")["stages"]["discover"]["count"] == 1
    assert len(result_reads) == 1

    replacement = output / ".results_replacement.csv"
    pd.DataFrame([{
        "room_id": "r2",
        "trip_revision": definition["trip_revision"],
        "name": "Listing r2",
    }]).to_csv(replacement, index=False)
    assert replacement.stat().st_size == original_stat.st_size
    os.replace(replacement, results_path)
    os.utime(results_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    status = pipeline_status.get_pipeline_status("trip")
    assert status["eligible_room_ids"] == ["r2"]
    assert len(result_reads) == 2


def test_evaluation_status_cache_invalidates_for_profile_sidecar_and_trip_changes(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["r1"], definition["trip_revision"])
    _write_detail(output, "r1", definition["trip_revision"])
    _write_review(output, "r1", "")
    _write_merged(output, ["r1"], definition["trip_revision"])
    hydration_calls = []

    def hydrate(search_name, frame, profile_name):
        hydration_calls.append(len(frame))
        hydrated = frame.copy()
        hydrated["personal_fit_status"] = "unknown"
        hydrated["personal_fit_profile"] = profile_name
        return hydrated

    monkeypatch.setattr(evaluation_service, "hydrate_personal_evaluations", hydrate)
    pipeline_status.get_pipeline_status("trip")
    pipeline_status.get_pipeline_status("trip")
    assert hydration_calls == [1]

    # An evaluation sidecar update is visible even when source CSVs are stable.
    sidecar = output / "personal_evaluations.json"
    sidecar.write_text(json.dumps({"profiles": {"Default": {}}}), encoding="utf-8")
    pipeline_status.get_pipeline_status("trip")
    assert hydration_calls == [1, 1]

    evaluation_service.save_evaluation_profile(
        "trip", "Default", {"cleanliness": 4, "value": 2},
        additional_instructions="Changed profile.",
    )
    pipeline_status.get_pipeline_status("trip")
    assert hydration_calls == [1, 1, 1]

    changed = {
        **definition,
        "total_budget": 1400,
    }
    search_service.save_search_definition("trip", changed, root=tmp_path / "searches")
    pipeline_status.get_pipeline_status("trip")
    assert hydration_calls == [1, 1, 1, 1]


def test_active_job_manifest_stays_live_while_csv_status_is_cached(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["r1"], definition["trip_revision"])
    _write_merged(output, ["r1"], definition["trip_revision"])
    jobs = tmp_path / "searches" / "trip" / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    active_path = jobs / "active_job.json"
    active_path.write_text(json.dumps({
        "status": "running", "stage": "discover", "current_stage": "discover",
        "pid": os.getpid(), "message": "first progress update",
    }), encoding="utf-8")
    first = pipeline_status.get_pipeline_status("trip")
    active_path.write_text(json.dumps({
        "status": "running", "stage": "discover", "current_stage": "discover",
        "pid": os.getpid(), "message": "new progress update",
    }), encoding="utf-8")
    second = pipeline_status.get_pipeline_status("trip")
    assert first["active_job"]["message"] == "first progress update"
    assert second["active_job"]["message"] == "new progress update"


def test_status_does_not_cache_old_evaluations_under_an_overlapping_database_write(tmp_path, monkeypatch):
    definition, output = _setup(tmp_path, monkeypatch)
    _write_results(output, ["r1"], definition["trip_revision"])
    _write_merged(output, ["r1"], definition["trip_revision"])
    calls = []

    def hydrate(search_name, frame, profile_name):
        calls.append(len(frame))
        if len(calls) == 1:
            changed = frame.copy()
            changed["total_price"] = 900
            storage_service.write_frame(output / "merged_df.csv", changed, export=False)
        hydrated = frame.copy()
        hydrated["personal_fit_status"] = "unknown"
        hydrated["personal_fit_profile"] = profile_name
        return hydrated

    monkeypatch.setattr(evaluation_service, "hydrate_personal_evaluations", hydrate)
    pipeline_status.get_pipeline_status("trip")
    pipeline_status.get_pipeline_status("trip")
    pipeline_status.get_pipeline_status("trip")
    assert calls == [1, 1]
