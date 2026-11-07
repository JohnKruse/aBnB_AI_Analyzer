"""Search-scoped background jobs with persisted status and resumable checkpoints.

Workers run in a small in-process thread pool and never call Streamlit. Each
collector stage writes its own per-listing CSV checkpoints; if the web process
exits, the manifest is marked interrupted and the same stage can be restarted.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .airbnb_service import collect_details_and_reviews, discover_listings
from .search_service import load_search_definition, validate_existing_search_name
from . import storage_service as storage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="bnb-search")
_THREAD_LOCK = threading.RLock()
_FUTURES: Dict[str, Future] = {}
_CANCEL_EVENTS: Dict[str, threading.Event] = {}

COLLECTION_RETRY_WAIT_SECONDS = 300
COLLECTION_RESUME_DELAY_SECONDS = 30

ALLOWED_STAGES = {"discover", "details", "refresh", "evaluate", "collect_and_evaluate", "prepare"}


class ActiveSearchJobError(RuntimeError):
    """Raised when a search already has a writer job in progress."""


class PipelinePrerequisiteError(ValueError):
    """Raised when a requested stage has not met its saved-data prerequisites."""


def _profile_is_saved(search_name: str, profile_name: str) -> bool:
    path = _search_dir(search_name) / "evaluation_profiles.json"
    if not storage.exists(path):
        return False
    try:
        data = storage.read_json(path)
    except (OSError, json.JSONDecodeError):
        return False
    profiles = data.get("profiles") if isinstance(data, dict) else None
    return isinstance(profiles, dict) and profile_name in profiles


def _search_dir(search_name: str) -> Path:
    name = validate_existing_search_name(search_name)
    root = (PROJECT_ROOT / "searches").resolve()
    directory = (root / name).resolve()
    if directory.parent != root:
        raise ValueError("Search workspace path escapes the searches directory.")
    return directory


def _jobs_dir(search_name: str) -> Path:
    path = _search_dir(search_name) / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp_name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Job state is unreadable at {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError(f"Job state at {path} is not a JSON object.")
    return result


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _manifest_path(search_name: str, job_id: str) -> Path:
    return _jobs_dir(search_name) / f"{job_id}.json"


def _active_path(search_name: str) -> Path:
    return _jobs_dir(search_name) / "active_job.json"


def _writer_lock_path(search_name: str) -> Path:
    return _jobs_dir(search_name) / "writer.lock"


def _read_active(search_name: str) -> Optional[Dict[str, Any]]:
    return _read_json(_active_path(search_name))


def _write_manifest(search_name: str, manifest: Dict[str, Any]) -> None:
    if _cancel_path(search_name, manifest.get("job_id", "")).exists():
        manifest["cancel_requested"] = True
    manifest["updated_at"] = _timestamp()
    path = _manifest_path(search_name, manifest["job_id"])
    _atomic_json(path, manifest)
    _atomic_json(_active_path(search_name), manifest)


def _pid_is_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, TypeError, ProcessLookupError, PermissionError, OSError):
        return False


def _acquire_writer_lock(search_name: str, job_id: str) -> None:
    lock_path = _writer_lock_path(search_name)
    for _ in range(2):
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            lock = _read_json(lock_path) or {}
            if _pid_is_alive(lock.get("pid")):
                raise ActiveSearchJobError(
                    f"Search '{search_name}' already has an active collection/evaluation job."
                )
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"job_id": job_id, "pid": os.getpid(), "created_at": _timestamp()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        return
    raise ActiveSearchJobError(f"Could not acquire the writer lock for '{search_name}'.")


def _release_writer_lock(search_name: str, job_id: str) -> None:
    lock_path = _writer_lock_path(search_name)
    lock = _read_json(lock_path) or {}
    if lock.get("job_id") == job_id:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _cancel_path(search_name: str, job_id: str) -> Path:
    return _jobs_dir(search_name) / f"{job_id}.cancel"


def _is_cancelled(search_name: str, job_id: str) -> bool:
    event = _CANCEL_EVENTS.get(job_id)
    return bool((event and event.is_set()) or _cancel_path(search_name, job_id).exists())


def _progress_callback(search_name: str, manifest: Dict[str, Any]) -> Callable[[int, int, str], None]:
    def report(done: int, total: int, message: str) -> None:
        stage = manifest.get("current_stage")
        if stage == "discover":
            # The collector reports its one provider batch as 1/1. That is not
            # one listing; the worker replaces this with distinct result count.
            manifest["completed"] = 0
            manifest["total"] = 0
            manifest["attempted"] = 0
            manifest["message"] = "Searching the selected area…"
        elif stage in {"details", "refresh"} and str(message).startswith("Checkpointed "):
            manifest["attempted"] = int(done)
            manifest["total"] = int(total)
            manifest["message"] = str(message)
        else:
            manifest["completed"] = int(done)
            manifest["total"] = int(total)
            manifest["message"] = str(message)
        manifest["heartbeat_at"] = _timestamp()
        _write_manifest(search_name, manifest)
    return report



def _resume_collection(initial, collect, policy, checkpoint):
    """Resume transient failures only, retaining permanent failures across passes."""
    result = deepcopy(initial)
    total = int(result.get('total', 0))
    failures = {(f['room_id'], f.get('stage', 'unknown')): f
                for f in result.get('failure_details', [])}
    # Older collectors may omit error details; never assume those errors transient.
    for rid in result.get('failed_room_ids', []):
        if not any(key[0] == rid for key in failures):
            failures[(rid, 'unknown')] = {'room_id': rid, 'stage': 'unknown', 'retryable': False}
    pending = set(result.get('unattempted_room_ids', []))
    attempted = set(result.get('attempted_room_ids', []))
    skipped = set(result.get('skipped_budget_room_ids', []))
    rounds = 0
    for round_index in range(policy.options.get('max_resume_rounds', 2)):
        if any(f.get('status_code') in {401, 403} or
               (f.get('status_code') == 429 and f.get('retryable') is False) for f in failures.values()):
            break
        retry_ids = sorted(pending | {f['room_id'] for f in failures.values() if f.get('retryable')})
        if not retry_ids or result.get('status') == 'cancelled':
            break
        # Checkpoints are review/detail specific: a retry does not redownload
        # successful components, even for a property that only partly completed.
        delay = max(COLLECTION_RESUME_DELAY_SECONDS * 2**round_index, policy.cooldown_remaining())
        checkpoint(result, {'round': round_index+1, 'room_ids': retry_ids,
                            'cooldown_seconds': delay, 'state': 'waiting'})
        if not policy.wait_retry(delay, f'Automatic collection resume {round_index+1}/2; {len(retry_ids)} remaining listings'):
            break
        rounds += 1
        checkpoint(result, {'round': round_index+1, 'room_ids': retry_ids, 'state': 'retrying'})
        latest = collect(retry_ids)
        unattempted = set(latest.get('unattempted_room_ids', []))
        handled = set(retry_ids)-unattempted
        failures = {key: f for key, f in failures.items() if key[0] not in handled}
        for f in latest.get('failure_details', []):
            failures[(f['room_id'], f.get('stage', 'unknown'))] = f
        for rid in latest.get('failed_room_ids', []):
            if not any(key[0] == rid for key in failures):
                failures[(rid, 'unknown')] = {'room_id': rid, 'stage': 'unknown', 'retryable': False}
        pending = (pending-handled) | unattempted
        attempted.update(latest.get('attempted_room_ids', []))
        skipped.update(latest.get('skipped_budget_room_ids', []))
        unresolved = pending | {key[0] for key in failures}
        result.update(latest)
        result.update(total=total, completed=max(0, total-len(unresolved)),
                      failed_room_ids=sorted({key[0] for key in failures}),
                      failure_details=list(failures.values()), failure_count=len(failures),
                      unattempted_room_ids=sorted(pending), attempted_room_ids=sorted(attempted),
                      skipped_budget_room_ids=sorted(skipped),
                      rate_limited=any(f.get('status_code') == 429 for f in failures.values()))
        if latest.get('status') != 'cancelled':
            result['status'] = ('partial' if result['completed'] else 'failed') if unresolved else 'success'
        checkpoint(result, {'round': rounds, 'state': 'checkpointed'})
    result['resume_rounds'] = rounds
    result['retry_waited_seconds'] = policy.retry_waited_seconds
    result['retry_budget_seconds'] = policy.retry_budget_seconds
    result['retry_stop_reason'] = policy.retry_stop_reason
    if result.get('status') in {'partial', 'failed'} and not any(f.get('status_code') in {401, 403} for f in failures.values()):
        result['message'] = policy.retry_stop_reason or 'Automatic retries finished; remaining failures are saved for manual resume.'
    elif result.get('status') == 'success' and rounds:
        result['message'] = 'Collection completed after automatic resume.'
    checkpoint(result, {'round': rounds, 'state': 'finished'})
    return result


def _worker(search_name: str, manifest: Dict[str, Any]) -> None:
    job_id, stage = manifest["job_id"], manifest["stage"]
    should_cancel = lambda: _is_cancelled(search_name, job_id)
    progress = _progress_callback(search_name, manifest)
    policy = None
    if manifest.get('run_options'):
        from .request_policy import RequestPolicy
        def report_wait(message):
            manifest['message'] = message
            manifest['request_wait'] = deepcopy(policy.wait_state)
            manifest['heartbeat_at'] = _timestamp()
            _write_manifest(search_name, manifest)
        policy = RequestPolicy(manifest['run_options'], should_cancel, report_wait,
                               property_count=len(manifest.get('eligible_room_ids') or []) or 1)
    manifest["status"] = "running"
    manifest["started_at"] = _timestamp()
    manifest["stage_status"] = {}
    _write_manifest(search_name, manifest)
    try:
        results: Dict[str, Any] = {}

        def run_discovery() -> Dict[str, Any]:
            manifest["current_stage"] = "discover"
            _write_manifest(search_name, manifest)
            if policy and stage != "discover":
                from .pipeline_status import get_pipeline_status
                existing = get_pipeline_status(search_name, manifest.get('profile_name', 'Default'))['stages']['discover']
                if existing.get('count', 0) and not existing.get('stale', 0):
                    result = {'status': 'success', 'discovered': existing['count'], 'message': 'Reusing current discovered listings.'}
                else:
                    result = policy.call(lambda: discover_listings(search_name, progress_callback=progress))
            else:
                invoke = lambda: discover_listings(search_name, progress_callback=progress)
                result = policy.call(invoke) if policy else invoke()
            discovered = int(result.get("discovered", 0) or 0)
            manifest["completed"] = discovered
            manifest["total"] = discovered
            manifest["attempted"] = discovered
            manifest["stage_status"]["discover"] = result.get("status", "success")
            manifest["stage_results"]["discover"] = result
            manifest["message"] = result.get("message") or f"Saved {discovered} discovered listing(s)."
            _write_manifest(search_name, manifest)
            return result

        def run_details(refresh: bool = False) -> Dict[str, Any]:
            manifest["current_stage"] = "refresh" if refresh else "details"
            _write_manifest(search_name, manifest)
            if policy:
                policy.retry_budget_seconds = COLLECTION_RETRY_WAIT_SECONDS
            result = collect_details_and_reviews(
                search_name,
                refresh_quotes=refresh,
                should_cancel=should_cancel,
                progress_callback=progress,
                **({"request_policy": policy} if policy else {}),
            )
            if policy:
                def retry_checkpoint(aggregate, retry_state):
                    manifest['collection_retry'] = dict(retry_state,
                        waited_seconds=policy.retry_waited_seconds,
                        budget_seconds=policy.retry_budget_seconds)
                    manifest['stage_results'][manifest['current_stage']] = aggregate
                    manifest['heartbeat_at'] = _timestamp()
                    _write_manifest(search_name, manifest)
                    storage.write_json(_search_dir(search_name) / 'output_data' / 'collection_failures.json', {
                        'failed': aggregate.get('failure_details', []),
                        'trip_revision': manifest.get('trip_revision'),
                        'rate_limited': aggregate.get('rate_limited', False),
                        'attempted_room_ids': aggregate.get('attempted_room_ids', []),
                        'unattempted_room_ids': aggregate.get('unattempted_room_ids', []),
                    })
                def retry_collect(ids):
                    return collect_details_and_reviews(search_name, refresh_quotes=False,
                        retry_room_ids=ids, should_cancel=should_cancel,
                        progress_callback=progress, request_policy=policy)
                result = _resume_collection(result, retry_collect, policy, retry_checkpoint)
            manifest["completed"] = int(result.get("completed", 0) or 0)
            manifest["total"] = int(result.get("total", 0) or 0)
            manifest["attempted"] = int(result.get("total", 0) or 0)
            manifest["failed"] = int(result.get("failure_count", len(result.get("failed_room_ids", []))) or 0)
            manifest["stage_status"][manifest["current_stage"]] = result.get("status", "success")
            manifest["stage_results"][manifest["current_stage"]] = result
            _write_manifest(search_name, manifest)
            return result

        def run_evaluation() -> Dict[str, Any]:
            nonlocal policy
            from .evaluation_service import run_personalized_evaluation
            from .pipeline_status import get_pipeline_status
            manifest["current_stage"] = "evaluate"
            status_snapshot = get_pipeline_status(search_name, manifest.get("profile_name", "Default"))
            manifest["eligible_room_ids"] = list(status_snapshot.get("eligible_room_ids", []))
            collection = manifest["stage_results"].get("details", {})
            excluded = set(collection.get("failed_room_ids", [])) | set(collection.get("unattempted_room_ids", [])) | set(collection.get("skipped_budget_room_ids", []))
            manifest["eligible_room_ids"] = [rid for rid in manifest["eligible_room_ids"] if rid not in excluded]
            if policy:
                # Airbnb cooldowns and retry time must not consume the AI stage's budget.
                policy = RequestPolicy(manifest["run_options"], should_cancel, report_wait,
                                       property_count=len(manifest["eligible_room_ids"]))
            _write_manifest(search_name, manifest)
            result = run_personalized_evaluation(
                search_name,
                profile_name=manifest.get("profile_name", "Default"),
                profile_snapshot=manifest.get("profile_snapshot"),
                eligible_room_ids=manifest.get("eligible_room_ids"),
                progress_callback=progress,
                should_cancel=should_cancel,
                **({"request_policy": policy} if policy else {}),
            )
            eligible = int(result.get("eligible", result.get("total", 0)) or 0)
            manifest["completed"] = int(result.get("rated", 0) or 0)
            manifest["total"] = eligible
            manifest["attempted"] = int(result.get("rated", 0) or 0) + int(result.get("failed", 0) or 0)
            manifest["failed"] = int(result.get("failed", 0) or 0)
            manifest["stage_status"]["evaluate"] = result.get("status", "success")
            manifest["stage_results"]["evaluate"] = result
            _write_manifest(search_name, manifest)
            return result

        if stage == "discover":
            results["discover"] = run_discovery()
        elif stage == "details":
            results["details"] = run_details()
        elif stage == "refresh":
            results["refresh"] = run_details(refresh=True)
        elif stage == "evaluate":
            results["evaluate"] = run_evaluation()
        elif stage in {"collect_and_evaluate", "prepare"}:
            if stage == 'prepare':
                from .pipeline_status import get_pipeline_status
                count = get_pipeline_status(search_name, manifest.get('profile_name', 'Default'))['stages']['discover']['count']
                results['discover'] = {'status': 'success', 'discovered': count, 'message': 'Using saved discovery and processing threshold.'}
            else:
                results["discover"] = run_discovery()
            if results["discover"].get("status") == "no_matches":
                manifest["status"] = "no_matches"
                manifest["result"] = results
                manifest["message"] = "Discovery completed with no matches."
                manifest["completed_at"] = _timestamp()
                _write_manifest(search_name, manifest)
                return
            if should_cancel():
                manifest["status"] = "cancelled"
            else:
                results["details"] = run_details()
                detail_status = results["details"].get("status")
                if should_cancel() or detail_status == "cancelled":
                    manifest["status"] = "cancelled"
                elif detail_status in {"partial", "failed"}:
                    from .pipeline_status import get_pipeline_status
                    ready = get_pipeline_status(search_name, manifest["profile_name"]).get("eligible_room_ids", [])
                    if ready:
                        results["evaluate"] = run_evaluation()
                    manifest["status"] = "partial" if ready or detail_status == "partial" else "failed"
                    manifest["message"] = ("Collection stopped with some listings pending. "
                                           + ("Continued with the ready listings. " if ready else "")
                                           + "Retry preparation to resume missing work.")
                elif detail_status != "success":
                    manifest["status"] = "failed"
                    manifest["message"] = "Collection could not start. Check the saved stage status and retry preparation."
                else:
                    results["evaluate"] = run_evaluation()
        child_statuses = [value.get("status") for value in results.values() if isinstance(value, dict)]
        if should_cancel() or "cancelled" in child_statuses:
            manifest["status"] = "cancelled"
        elif manifest.get("status") in {"failed", "partial", "cancelled", "no_matches"}:
            pass
        elif child_statuses and all(status == "no_matches" for status in child_statuses):
            manifest["status"] = "no_matches"
        elif any(status == "no_candidates" for status in child_statuses):
            manifest["status"] = "no_matches"
        elif any(status in {"failed", "blocked", "no_data", "no_personalized_score"} for status in child_statuses):
            blocking = next(status for status in child_statuses if status in {"failed", "blocked", "no_data", "no_personalized_score"})
            manifest["status"] = "failed" if blocking == "failed" else blocking
        elif "partial" in child_statuses:
            manifest["status"] = "partial"
        elif any(status not in {"success", "no_matches"} for status in child_statuses):
            manifest["status"] = "failed"
        else:
            manifest["status"] = "completed"
        manifest["result"] = results
        manifest["completed_at"] = _timestamp()
        manifest["message"] = manifest.get("message") or f"{stage.replace('_', ' ').title()} job {manifest['status']}."
        _write_manifest(search_name, manifest)
    except Exception as exc:
        manifest["status"] = "cancelled" if should_cancel() else "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["completed_at"] = _timestamp()
        manifest["message"] = manifest["error"]
        _write_manifest(search_name, manifest)
    finally:
        _release_writer_lock(search_name, job_id)


def _validate_job_prerequisites(search_name: str, stage: str, profile_name: str) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[list[str]]]:
    """Validate stage prerequisites against saved trip and data snapshots."""
    from .evaluation_service import load_evaluation_profile
    from .pipeline_status import get_pipeline_status

    definition = load_search_definition(search_name, root=PROJECT_ROOT / "searches")
    if not definition:
        raise PipelinePrerequisiteError("Save a trip definition with dates, party, currency, and map area before starting pipeline work.")
    profile = None
    try:
        profile = load_evaluation_profile(search_name, profile_name)
    except KeyError as exc:
        if stage in {"evaluate", "collect_and_evaluate", "prepare"}:
            raise PipelinePrerequisiteError(f"Save the selected evaluation profile before starting {stage}.") from exc
    if stage in {"evaluate", "collect_and_evaluate", "prepare"} and not _profile_is_saved(search_name, profile_name):
        raise PipelinePrerequisiteError(f"Save the selected evaluation profile '{profile_name}' before starting {stage}.")

    if stage in {"details", "refresh", "evaluate"}:
        status = get_pipeline_status(search_name, profile_name)
        discovered = status["stages"]["discover"]
        if int(discovered.get("count", 0)) <= 0:
            raise PipelinePrerequisiteError("Discover current listings before collecting details or evaluating.")
        if stage == "evaluate":
            if not status.get("eligible_room_ids"):
                raise PipelinePrerequisiteError("At least one available listing needs saved details and a reviews checkpoint before evaluation.")
            weights = (profile or {}).get("weights", {})
            if not any(int(weight) > 0 for weight in weights.values()):
                raise PipelinePrerequisiteError("Give at least one personal-priority criterion a nonzero weight before evaluation.")
            return definition, profile, list(status["eligible_room_ids"])
    if stage in {"collect_and_evaluate", "prepare"}:
        if profile is None:
            raise PipelinePrerequisiteError("Save the selected evaluation profile before starting the combined pipeline.")
        if not any(int(weight) > 0 for weight in profile.get("weights", {}).values()):
            raise PipelinePrerequisiteError("Give at least one personal-priority criterion a nonzero weight before evaluation.")
    if stage == 'prepare':
        from .processing_service import load_selection, select_properties
        directory = _search_dir(search_name)
        selected = load_selection(directory, definition)
        path = directory / 'output_data' / 'results_df.csv'
        if selected is None or not storage.exists(path):
            raise PipelinePrerequisiteError('Discover listings and set the step 2 price threshold first.')
        frame = storage.read_frame(path, dtype={'room_id': str})
        selected_ids = select_properties(frame, definition, selected)['selected_ids']
        if not selected_ids:
            raise PipelinePrerequisiteError('No current listings match the processing threshold.')
        return definition, profile, list(selected_ids)
    return definition, profile, None


def start_search_job(search_name: str, stage: str, *, profile_name: str = "Default", run_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Serialize job startup with search archive/deletion actions."""
    from .search_catalog import get_search_summary, search_lifecycle_lock

    if stage not in ALLOWED_STAGES:
        raise ValueError(f"Unsupported job stage: {stage}")
    root = PROJECT_ROOT / "searches"
    with search_lifecycle_lock(search_name, root=root):
        directory = _search_dir(search_name)
        if not directory.is_dir():
            raise FileNotFoundError(f"Search workspace '{search_name}' does not exist.")
        summary = get_search_summary(search_name, root=root)
        if summary.get("archived"):
            raise PipelinePrerequisiteError("Restore this archived search before starting collection or evaluation.")
        return _start_search_job_locked(search_name, stage, profile_name=profile_name, run_options=run_options)


def _start_search_job_locked(search_name: str, stage: str, *, profile_name: str = "Default", run_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Start one background job for a search; completed stages resume from CSV checkpoints."""
    if stage not in ALLOWED_STAGES:
        raise ValueError(f"Unsupported job stage: {stage}")
    directory = _search_dir(search_name)
    if not directory.exists():
        raise FileNotFoundError(f"Search workspace '{search_name}' does not exist.")
    if run_options is not None:
        from .request_policy import automatic_options
        run_options = automatic_options(run_options)
        storage.write_json(directory / "run_options.json", run_options)
        if stage in {"evaluate", "collect_and_evaluate", "prepare"} and not os.getenv("OPENAI_API_KEY"):
            raise PipelinePrerequisiteError("Set OPENAI_API_KEY before starting an automatic AI run.")
    definition, profile_snapshot, eligible_room_ids = _validate_job_prerequisites(search_name, stage, profile_name)
    if profile_snapshot is None:
        from .evaluation_service import load_evaluation_profile
        profile_snapshot = load_evaluation_profile(search_name, "Default")
    from .evaluation_service import profile_fingerprint
    with _THREAD_LOCK:
        job_id = uuid.uuid4().hex
        _acquire_writer_lock(search_name, job_id)
        manifest = {
            "schema_version": 1,
            "job_id": job_id,
            "search_name": search_name,
            "stage": stage,
            "profile_name": profile_name,
            "profile_snapshot": profile_snapshot,
            "run_options": run_options,
            "profile_fingerprint": profile_fingerprint(profile_snapshot),
            "eligible_room_ids": eligible_room_ids,
            "trip_revision": int(definition["trip_revision"]) if definition else None,
            "current_stage": stage,
            "status": "queued",
            "created_at": _timestamp(),
            "updated_at": _timestamp(),
            "heartbeat_at": _timestamp(),
            "pid": os.getpid(),
            "completed": 0,
            "total": 0,
            "message": "Queued; waiting for a worker.",
            "stage_status": {},
            "stage_results": {},
            "result": None,
            "error": None,
            "cancel_requested": False,
        }
        _write_manifest(search_name, manifest)
        _CANCEL_EVENTS[job_id] = threading.Event()
        future = _EXECUTOR.submit(_worker, search_name, manifest)
        _FUTURES[job_id] = future
        return deepcopy(_read_active(search_name) or manifest)


def get_search_job(search_name: str) -> Optional[Dict[str, Any]]:
    """Return the latest job state and recover a stale running manifest."""
    with _THREAD_LOCK:
        manifest = _read_active(search_name)
        if not manifest:
            return None
        if manifest.get("status") in {"queued", "running"}:
            future = _FUTURES.get(manifest.get("job_id", ""))
            worker_pid = manifest.get("pid")
            worker_alive = _pid_is_alive(worker_pid)
            # A different live process owns this job. It must remain the writer.
            if worker_pid != os.getpid() and worker_alive:
                manifest["remote_worker"] = True
            elif not worker_alive or future is None:
                manifest["status"] = "interrupted"
                manifest["message"] = "Worker stopped before the job finished. Start the same stage to resume from checkpoints."
                manifest["completed_at"] = _timestamp()
                _write_manifest(search_name, manifest)
                _release_writer_lock(search_name, manifest.get("job_id", ""))
        return deepcopy(manifest)


def cancel_search_job(search_name: str) -> Optional[Dict[str, Any]]:
    """Request cooperative cancellation after the currently active provider call."""
    manifest = get_search_job(search_name)
    if not manifest or manifest.get("status") not in {"queued", "running"}:
        return manifest
    job_id = manifest["job_id"]
    event = _CANCEL_EVENTS.get(job_id)
    if event:
        event.set()
    _cancel_path(search_name, job_id).touch(exist_ok=True)
    manifest["cancel_requested"] = True
    manifest["message"] = "Stop requested; the worker will stop after the active provider call and save its checkpoint."
    _write_manifest(search_name, manifest)
    return manifest

