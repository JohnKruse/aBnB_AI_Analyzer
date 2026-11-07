"""Offline lifecycle tests for persisted search jobs and cooperative cancellation."""

import threading
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

import src.evaluation_service as evaluation_service
import src.job_service as job_service
import src.search_service as search_service
import src.pipeline_status as pipeline_status


def _wait_for_terminal(search_name, timeout=3):
    assert job_service._FUTURES[job_service.get_search_job(search_name)["job_id"]].result(timeout=timeout) is None
    return job_service.get_search_job(search_name)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(job_service, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(evaluation_service, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pipeline_status, "PROJECT_ROOT", tmp_path)
    start = date.today() + timedelta(days=30)
    search_service.save_search_definition(
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


def test_job_manifest_tracks_completion_and_profile_snapshot(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(job_service, "discover_listings", lambda search, **kwargs: {"status": "success", "discovered": 2})
    started = job_service.start_search_job("trip", "discover", profile_name="Default")
    result = _wait_for_terminal("trip")
    assert result["status"] == "completed"
    assert result["stage"] == "discover"
    assert result["profile_name"] == "Default"
    assert result["profile_snapshot"]["weights"]["cleanliness"] >= 0
    assert result["stage_results"]["discover"]["discovered"] == 2
    assert result["job_id"] == started["job_id"]


def test_same_search_cannot_run_two_writers_and_cancel_is_cooperative(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def blocking_discover(search, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return {"status": "success", "discovered": 1}

    monkeypatch.setattr(job_service, "discover_listings", blocking_discover)
    first = job_service.start_search_job("trip", "discover")
    assert entered.wait(timeout=1)
    with pytest.raises(job_service.ActiveSearchJobError):
        job_service.start_search_job("trip", "discover")
    job_service.cancel_search_job("trip")
    release.set()
    result = _wait_for_terminal("trip")
    assert result["status"] == "cancelled"
    assert result["cancel_requested"] is True
    assert result["job_id"] == first["job_id"]


def test_job_status_reports_no_matches_as_terminal_state(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(job_service, "discover_listings", lambda search, **kwargs: {"status": "no_matches", "discovered": 0})
    job_service.start_search_job("trip", "discover")
    result = _wait_for_terminal("trip")
    assert result["status"] == "no_matches"


def test_job_rejects_unknown_stage_and_traversal(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Unsupported job stage"):
        job_service.start_search_job("trip", "unknown")
    with pytest.raises(ValueError):
        job_service.start_search_job("../trip", "discover")


def test_archived_search_must_be_restored_before_starting_a_job(tmp_path, monkeypatch):
    from src.search_catalog import set_search_archived

    _setup(tmp_path, monkeypatch)
    root = tmp_path / "searches"
    set_search_archived("trip", True, root=root)
    with pytest.raises(job_service.PipelinePrerequisiteError, match="Restore"):
        job_service.start_search_job("trip", "discover")
    monkeypatch.setattr(job_service, "discover_listings", lambda *args, **kwargs: {"status": "success", "discovered": 1})
    set_search_archived("trip", False, root=root)
    job_service.start_search_job("trip", "discover")
    assert _wait_for_terminal("trip")["status"] == "completed"


def test_delete_rejects_an_active_job_and_deleted_search_cannot_restart(tmp_path, monkeypatch):
    from src.search_catalog import delete_search

    _setup(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def discover(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return {"status": "success", "discovered": 1}

    monkeypatch.setattr(job_service, "discover_listings", discover)
    job_service.start_search_job("trip", "discover")
    assert entered.wait(timeout=2)
    try:
        with pytest.raises((ValueError, RuntimeError), match="(?i)active|running|job"):
            delete_search("trip", confirmation="trip", root=tmp_path / "searches")
    finally:
        release.set()
    _wait_for_terminal("trip")
    removed = delete_search("trip", confirmation="trip", root=tmp_path / "searches")
    assert not (tmp_path / "searches" / "trip").exists()
    assert not (Path(removed) / "analyzer.sqlite3").exists()
    with pytest.raises(FileNotFoundError):
        job_service.start_search_job("trip", "discover")
    assert not (tmp_path / "searches" / "trip").exists()


def test_details_and_evaluation_cannot_start_before_discovery(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(job_service.PipelinePrerequisiteError, match="Discover current listings"):
        job_service.start_search_job("trip", "details")
    with pytest.raises(job_service.PipelinePrerequisiteError):
        job_service.start_search_job("trip", "evaluate")
    assert not (tmp_path / "searches" / "trip" / "jobs" / "writer.lock").exists()


def test_discovery_progress_is_indeterminate_until_actual_count_is_saved(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    output_dir = tmp_path / "searches" / "trip" / "output_data"
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"room_id": "old", "trip_revision": 1}]).to_csv(output_dir / "results_df.csv", index=False)
    entered = threading.Event()
    release = threading.Event()

    def blocking_discover(search, progress_callback=None):
        if progress_callback:
            progress_callback(0, 1, "Contacting Airbnb search provider")
        entered.set()
        release.wait(timeout=2)
        return {"status": "success", "discovered": 1}

    monkeypatch.setattr(job_service, "discover_listings", blocking_discover)
    job_service.start_search_job("trip", "discover")
    assert entered.wait(timeout=1)
    snapshot = pipeline_status.get_pipeline_status("trip")
    stage = snapshot["stages"]["discover"]
    assert stage["state"] == "running"
    assert stage["count"] == 0
    assert stage["total"] == 0
    assert stage["previous_count"] == 1
    assert snapshot["active_job"]["current_stage"] == "discover"
    release.set()
    result = _wait_for_terminal("trip")
    assert result["completed"] == 1
    assert result["total"] == 1


def test_no_matches_remains_retryable_and_blocks_later_stages(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(job_service, "discover_listings", lambda search, **kwargs: {"status": "no_matches", "discovered": 0})
    job_service.start_search_job("trip", "discover")
    result = _wait_for_terminal("trip")
    assert result["status"] == "no_matches"
    status = pipeline_status.get_pipeline_status("trip")
    assert status["stages"]["discover"]["state"] == "no_matches"
    assert status["stages"]["discover"]["can_start"] is True
    assert status["stages"]["details"]["state"] == "no_matches"
    assert status["stages"]["evaluate"]["state"] == "no_matches"
    with pytest.raises(job_service.PipelinePrerequisiteError, match="Discover current listings"):
        job_service.start_search_job("trip", "details")


def test_combined_job_stops_before_evaluation_when_collection_is_partial(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evaluation_service.save_evaluation_profile(
        "trip", "Default", {"cleanliness": 3}, additional_instructions="Use the available trip evidence."
    )
    output_dir = tmp_path / "searches" / "trip" / "output_data"

    def discover(search, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"room_id": "one", "trip_revision": 1}]).to_csv(output_dir / "results_df.csv", index=False)
        return {"status": "success", "discovered": 1}

    evaluation_called = []
    monkeypatch.setattr(job_service, "discover_listings", discover)
    monkeypatch.setattr(job_service, "collect_details_and_reviews", lambda *args, **kwargs: {
        "status": "partial", "completed": 0, "total": 1, "failed_room_ids": ["one"], "failure_count": 1,
    })
    monkeypatch.setattr(evaluation_service, "run_personalized_evaluation", lambda *args, **kwargs: evaluation_called.append(True))

    job_service.start_search_job("trip", "collect_and_evaluate")
    result = _wait_for_terminal("trip")
    assert result["status"] == "partial"
    assert result["stage_status"]["details"] == "partial"
    assert "evaluate" not in result["stage_status"]
    assert evaluation_called == []


def test_pipeline_status_keeps_discovery_success_after_details_job_starts(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evaluation_service.save_evaluation_profile("trip", "Default", {"cleanliness": 3})
    output_dir = tmp_path / "searches" / "trip" / "output_data"

    def discover(search, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"room_id": "r1", "trip_revision": 1}]).to_csv(output_dir / "results_df.csv", index=False)
        return {"status": "success", "discovered": 1}

    def collect(search, **kwargs):
        pd.DataFrame([{
            "room_id": "r1", "trip_revision": 1, "quote_attempt_status": "success",
            "details_fetched_at": "2026-10-01T12:00:00+00:00",
        }]).to_csv(output_dir / "details_df.csv", index=False)
        pd.DataFrame([{"room_id": "r1", "reviews_text": "", "reviews_retrieved_count": 0}]).to_csv(output_dir / "reviews_df.csv", index=False)
        return {"status": "success", "completed": 1, "total": 1, "failed_room_ids": []}

    monkeypatch.setattr(job_service, "discover_listings", discover)
    monkeypatch.setattr(job_service, "collect_details_and_reviews", collect)
    job_service.start_search_job("trip", "discover")
    discovery_job = _wait_for_terminal("trip")
    job_service.start_search_job("trip", "details")
    _wait_for_terminal("trip")

    status = pipeline_status.get_pipeline_status("trip")
    assert status["stages"]["discover"]["state"] == "success"
    assert status["stages"]["discover"]["count"] == 1
    assert status["stages"]["discover"]["last_successful_job"]["job_id"] == discovery_job["job_id"]
    assert status["stages"]["details"]["state"] == "success"
    assert status["next_stage"] == "evaluate"



def test_automatic_job_retries_discovery_and_evaluates_successful_subset(tmp_path, monkeypatch):
    from src.request_policy import RequestPolicy
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    import requests
    _setup(tmp_path, monkeypatch)
    evaluation_service.save_evaluation_profile('trip', 'Default', {'cleanliness': 3})
    monkeypatch.setattr(RequestPolicy, 'wait', lambda *args: None)
    calls = []
    def discover(*args, **kwargs):
        calls.append('discover')
        if len(calls) == 1:
            response = requests.Response()
            response.status_code = 503
            raise requests.HTTPError(response=response)
        return {'status': 'success', 'discovered': 2}
    def collect(*args, **kwargs):
        assert kwargs['request_policy'].retry_budget_seconds == 300
        kwargs['request_policy'].retry_waited_seconds = 300
        calls.append('collect')
        return {'status': 'partial', 'completed': 1, 'total': 2, 'failed_room_ids': ['bad']}
    def evaluate(*args, **kwargs):
        assert kwargs['eligible_room_ids'] == ['good']
        assert kwargs['request_policy'].retry_waited_seconds == 0
        calls.append('evaluate')
        return {'status': 'success', 'rated': 1, 'eligible': 1}
    monkeypatch.setattr(job_service, 'discover_listings', discover)
    monkeypatch.setattr(job_service, 'collect_details_and_reviews', collect)
    monkeypatch.setattr(evaluation_service, 'run_personalized_evaluation', evaluate)
    monkeypatch.setattr(pipeline_status, 'get_pipeline_status', lambda *args: {
        'stages': {'discover': {'count': 0}}, 'eligible_room_ids': ['good', 'bad']})
    job_service.start_search_job('trip', 'collect_and_evaluate', run_options={'review_years': 1})
    result = _wait_for_terminal('trip')
    assert calls == ['discover', 'discover', 'collect', 'evaluate']
    assert result['status'] == 'partial'
    assert result['run_options']['review_years'] == 1


def test_automatic_resume_reuses_current_discovery(tmp_path, monkeypatch):
    from src.request_policy import RequestPolicy
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    _setup(tmp_path, monkeypatch)
    evaluation_service.save_evaluation_profile('trip', 'Default', {'cleanliness': 3})
    monkeypatch.setattr(RequestPolicy, 'wait', lambda *args: None)
    monkeypatch.setattr(job_service, 'discover_listings', lambda *a, **kw: pytest.fail('Must reuse current discovery'))
    monkeypatch.setattr(job_service, 'collect_details_and_reviews', lambda *a, **kw: {'status': 'success', 'completed': 0, 'total': 0})
    monkeypatch.setattr(evaluation_service, 'run_personalized_evaluation', lambda *a, **kw: {'status': 'success', 'rated': 1, 'eligible': 1})
    monkeypatch.setattr(pipeline_status, 'get_pipeline_status', lambda *args: {
        'stages': {'discover': {'count': 1, 'stale': 0}}, 'eligible_room_ids': ['good']})
    job_service.start_search_job('trip', 'collect_and_evaluate', run_options={})
    result = _wait_for_terminal('trip')
    assert result['status'] == 'completed'
    assert 'Reusing' in result['stage_results']['discover']['message']


def test_prepare_requires_threshold_and_does_not_rediscover(tmp_path, monkeypatch):
    from src.processing_service import save_selection
    from src import storage_service
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    evaluation_service.save_evaluation_profile('trip', 'Default', {'cleanliness': 3})
    with pytest.raises(job_service.PipelinePrerequisiteError, match='threshold'):
        job_service.start_search_job('trip', 'prepare', run_options={})
    directory = tmp_path / 'searches' / 'trip'
    definition = search_service.load_search_definition('trip', root=tmp_path / 'searches')
    storage_service.write_frame(directory / 'output_data' / 'results_df.csv', pd.DataFrame([
        {'room_id': 'one', 'trip_revision': 1, 'search_quote_total': 200,
         'search_quote_check_in': definition['check_in'], 'search_quote_check_out': definition['check_out'],
         'search_quote_currency': definition['currency'], 'search_quote_party_supported': True}]))
    save_selection(directory, definition, {'max_total': 250})
    monkeypatch.setattr(pipeline_status, 'get_pipeline_status', lambda *args: {
        'stages': {'discover': {'count': 1}}, 'eligible_room_ids': ['one']})
    monkeypatch.setattr(job_service, 'discover_listings', lambda *a, **kw: pytest.fail('Prepare must not rediscover'))
    monkeypatch.setattr(job_service, 'collect_details_and_reviews', lambda *a, **kw: {'status': 'success', 'completed': 1, 'total': 1})
    monkeypatch.setattr(evaluation_service, 'run_personalized_evaluation', lambda *a, **kw: {'status': 'success', 'rated': 1, 'eligible': 1})
    job_service.start_search_job('trip', 'prepare', run_options={})
    assert _wait_for_terminal('trip')['status'] == 'completed'


def test_collection_resume_only_retries_transient_and_untouched(monkeypatch):
    from src.request_policy import RequestPolicy, automatic_options
    policy = RequestPolicy(automatic_options(), property_count=23)
    waits = []
    monkeypatch.setattr(policy, 'wait', lambda seconds, message: waits.append(seconds))
    monkeypatch.setattr(policy, 'cooldown_remaining', lambda: 0)
    initial = {'status': 'partial', 'total': 4, 'completed': 1,
        'failed_room_ids': ['permanent', 'temporary'],
        'failure_details': [
            {'room_id': 'permanent', 'stage': 'details', 'status_code': 400, 'retryable': False},
            {'room_id': 'temporary', 'stage': 'reviews', 'status_code': 429, 'retryable': True}],
        'unattempted_room_ids': ['untouched']}
    calls, checkpoints = [], []
    def collect(ids):
        calls.append(ids)
        return {'status': 'success', 'total': 2, 'completed': 2,
                'failed_room_ids': [], 'failure_details': [], 'attempted_room_ids': ids}
    result = job_service._resume_collection(initial, collect, policy,
                                          lambda result, state: checkpoints.append((result.copy(), state)))
    assert calls == [['temporary', 'untouched']]
    assert waits == [30]
    assert result['total'] == 4 and result['completed'] == 3
    assert result['status'] == 'partial'
    assert result['failed_room_ids'] == ['permanent']
    assert not result['rate_limited']
    assert checkpoints[-1][0]['failure_details'][0]['room_id'] == 'permanent'


def test_collection_resume_is_bounded_and_recovers(monkeypatch):
    from src.request_policy import RequestPolicy, automatic_options
    for succeeds in (False, True):
        policy = RequestPolicy(automatic_options())
        monkeypatch.setattr(policy, 'wait', lambda *args: None)
        monkeypatch.setattr(policy, 'cooldown_remaining', lambda: 0)
        failure = {'status': 'failed', 'total': 1, 'completed': 0,
                   'failed_room_ids': ['one'], 'failure_details': [
                       {'room_id': 'one', 'stage': 'reviews', 'status_code': 429, 'retryable': True}]}
        calls = []
        def collect(ids):
            calls.append(ids)
            if succeeds:
                return {'status': 'success', 'total': 1, 'completed': 1, 'failed_room_ids': []}
            return failure.copy()
        result = job_service._resume_collection(failure, collect, policy, lambda *args: None)
        assert len(calls) == (1 if succeeds else 2)
        assert result['status'] == ('success' if succeeds else 'failed')
        assert result['retry_waited_seconds'] == (30 if succeeds else 90)


def test_collection_resume_honors_budget_quota_and_cancellation(monkeypatch):
    from src.request_policy import RequestPolicy, automatic_options, RunCancelled
    policy = RequestPolicy(automatic_options())
    monkeypatch.setattr(policy, 'cooldown_remaining', lambda: 3601)
    failure = {'status': 'failed', 'total': 2, 'completed': 0,
        'failed_room_ids': ['one'], 'unattempted_room_ids': ['two'],
        'failure_details': [{'room_id': 'one', 'stage': 'reviews', 'status_code': 429, 'retryable': True}]}
    def forbidden(ids):
        pytest.fail('Must not call provider')
    result = job_service._resume_collection(failure, forbidden, policy, lambda *args: None)
    assert result['retry_stop_reason'] and result['resume_rounds'] == 0
    failure['failure_details'][0]['retryable'] = False
    policy = RequestPolicy(automatic_options())
    assert job_service._resume_collection(failure, forbidden, policy, lambda *args: None)['resume_rounds'] == 0
    failure['failure_details'][0]['retryable'] = True
    policy = RequestPolicy(automatic_options(), should_cancel=lambda: True)
    monkeypatch.setattr(policy, 'cooldown_remaining', lambda: 0)
    with pytest.raises(RunCancelled):
        job_service._resume_collection(failure, forbidden, policy, lambda *args: None)
