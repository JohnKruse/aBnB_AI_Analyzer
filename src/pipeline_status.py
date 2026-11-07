"""Read-only status aggregation for the browser's sequential search workflow."""

from __future__ import annotations

import json
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import pandas as pd

from .search_service import load_search_definition, validate_existing_search_name
from . import storage_service as storage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SENSITIVE_VALUE = re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|authorization|cookie|secret|password)\b(\s*[:=]\s*)([^\s&,;]+)")

# Keep only small status projections in memory. The full merged CSV is loaded
# only on a cold evaluation-status cache miss and is released after hydration.
_STATUS_FRAME_CACHE_MAX = 12
_EVALUATION_STATUS_CACHE_MAX = 16
_STATUS_CACHE_LOCK = threading.RLock()
_STATUS_FRAME_CACHE: "OrderedDict[tuple, tuple[pd.DataFrame, Optional[str]]]" = OrderedDict()
_EVALUATION_STATUS_CACHE: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()
_STATUS_CSV_COLUMNS = {
    "results": ("room_id", "trip_revision", "discovery_state", "search_quote_total", "search_quote_check_in", "search_quote_check_out", "search_quote_currency", "search_quote_party_supported", "discovered_at", "selection_quote_total", "selection_quote_at", "selection_quote_verified"),
    "details": ("room_id", "trip_revision", "quote_attempt_status", "availability_state", "details_fetched_at"),
    "reviews": ("room_id",),
}


def _safe_text(value: Any) -> str:
    text = str(value or "")
    return _SENSITIVE_VALUE.sub(r"\1\2[redacted]", text)[:500]


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def _search_dir(search_name: str) -> Path:
    name = validate_existing_search_name(search_name)
    root = (PROJECT_ROOT / "searches").resolve()
    directory = (root / name).resolve()
    if directory.parent != root:
        raise ValueError("Search workspace path escapes the searches directory.")
    return directory


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if path.name == "collection_failures.json":
        value = storage.read_json(path)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"Saved state at {path} is not a JSON object.")
        return value
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Saved state at {path} is not a JSON object.")
    return value


def _file_signature(path: Path) -> Any:
    """Return metadata that changes on ordinary writes and atomic replacement."""
    if path.suffix in {".csv", ".json"} and path.parent.name != "jobs":
        return storage.signature(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    return (
        int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
        int(getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000))),
        int(stat.st_size),
        int(getattr(stat, "st_ino", 0)),
    )


def _unchanged_or_imported(before: Any, after: Any) -> bool:
    """Accept only identical inputs or an unchanged file's first SQLite import."""
    if before == after:
        return True
    if isinstance(before, tuple) and isinstance(after, tuple) and len(before) == len(after):
        return all(_unchanged_or_imported(old, new) for old, new in zip(before, after))
    if not isinstance(before, str) or not isinstance(after, str):
        return False
    if not before.startswith("legacy:") or not after.startswith(("frame:", "json:")):
        return False
    old_parts = before.split(":", 1)[1].rsplit(":", 4)
    new_parts = after.split(":", 1)[1].rsplit(":", 5)
    return (
        len(old_parts) == 5 and len(new_parts) == 6
        and old_parts[0] == new_parts[0] and new_parts[1] == "1"
        and old_parts[1:] == new_parts[2:]
    )


def _read_csv(
    path: Path,
    *,
    columns: Optional[Sequence[str]] = None,
    cache: bool = True,
) -> tuple[pd.DataFrame, Optional[str]]:
    """Read a compact CSV projection, reusing only a signature-matched snapshot.

    Returned cached frames are shared read-only objects. Status aggregation
    makes copies before filtering or otherwise mutating them.
    """
    path = Path(path)
    root_key = str(Path(PROJECT_ROOT).resolve())
    path_key = str(path.resolve())
    selected = tuple(sorted(set(columns))) if columns is not None else None
    before = _file_signature(path)
    key = (root_key, path_key, before, selected)
    if cache:
        with _STATUS_CACHE_LOCK:
            cached = _STATUS_FRAME_CACHE.get(key)
            if cached is not None:
                _STATUS_FRAME_CACHE.move_to_end(key)
                frame, error = cached
                return frame, error
    if before is None:
        if not storage.exists(path):
            frame, error = pd.DataFrame(), None
            if cache:
                _cache_status_frame(key, path_key, frame, error)
            return frame, error
        # A stat failure is transient; avoid caching an unreadable/unstable result.
        before = None
    try:
        read_options = {"dtype": {"room_id": str}}
        if selected is not None:
            selected_set = set(selected)
            read_options["usecols"] = lambda column: column in selected_set
        frame = storage.read_frame(path, **read_options)
    except Exception as exc:
        return pd.DataFrame(), f"Could not read {path.name}: {type(exc).__name__}: {exc}"
    after = _file_signature(path)
    if cache and before is not None and _unchanged_or_imported(before, after):
        _cache_status_frame((root_key, path_key, after, selected), path_key, frame, None)
    return frame, None


def _cache_status_frame(
    key: tuple,
    path_key: str,
    frame: pd.DataFrame,
    error: Optional[str],
) -> None:
    with _STATUS_CACHE_LOCK:
        # Newer revisions of the same file replace older projections.
        for old_key in list(_STATUS_FRAME_CACHE):
            if old_key[0] == key[0] and old_key[1] == path_key and old_key != key:
                _STATUS_FRAME_CACHE.pop(old_key, None)
        _STATUS_FRAME_CACHE[key] = (frame, error)
        _STATUS_FRAME_CACHE.move_to_end(key)
        while len(_STATUS_FRAME_CACHE) > _STATUS_FRAME_CACHE_MAX:
            _STATUS_FRAME_CACHE.popitem(last=False)


def _id_set(frame: pd.DataFrame) -> set[str]:
    if frame.empty or "room_id" not in frame.columns:
        return set()
    return {
        str(value).strip()
        for value in frame["room_id"].dropna().tolist()
        if str(value).strip()
    }


def _revision_rows(frame: pd.DataFrame, revision: Optional[int]) -> tuple[pd.DataFrame, int]:
    if frame.empty:
        return frame.copy(), 0
    if revision is None or "trip_revision" not in frame.columns:
        return frame.iloc[0:0].copy(), len(frame)
    revisions = pd.to_numeric(frame["trip_revision"], errors="coerce")
    current = frame.loc[revisions == revision].copy()
    return current, len(frame) - len(current)


def _active_manifest(search_dir: Path) -> Optional[Dict[str, Any]]:
    path = search_dir / "jobs" / "active_job.json"
    try:
        active = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "failed", "message": "The saved active job state is unreadable."}
    if active and active.get("status") in {"queued", "running"}:
        try:
            os.kill(int(active.get("pid", -1)), 0)
        except (ValueError, TypeError, ProcessLookupError, PermissionError, OSError):
            active = dict(active)
            active["status"] = "interrupted"
            active["message"] = "The worker process is no longer running; restart this stage to resume from checkpoints."
    return active


def _job_summary(manifest: Optional[Dict[str, Any]], stage: str) -> Optional[Dict[str, Any]]:
    if not manifest:
        return None
    result = manifest.get("stage_results", {}).get(stage)
    if result is None and manifest.get("stage") == stage:
        result = manifest.get("result")
    return {
        "job_id": manifest.get("job_id"),
        "stage": manifest.get("stage"),
        "status": manifest.get("status"),
        "stage_status": manifest.get("stage_status", {}).get(stage),
        "message": _safe_text(manifest.get("message", "")),
        "error": _safe_text(manifest.get("error")) if manifest.get("error") else None,
        "created_at": manifest.get("created_at"),
        "started_at": manifest.get("started_at"),
        "completed_at": manifest.get("completed_at"),
        "trip_revision": manifest.get("trip_revision"),
        "profile_name": manifest.get("profile_name"),
        "profile_fingerprint": manifest.get("profile_fingerprint"),
        "result": _safe_value(result),
    }


def _job_history(search_dir: Path) -> Dict[str, Dict[str, Any]]:
    jobs_dir = search_dir / "jobs"
    latest: Dict[str, Dict[str, Any]] = {"_successful": {}, "_all": []}
    if not jobs_dir.exists():
        return latest
    manifests = []
    for path in jobs_dir.glob("*.json"):
        if path.name == "active_job.json":
            continue
        try:
            manifest = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if manifest:
            manifests.append(manifest)
    manifests.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))
    latest["_all"] = manifests
    for manifest in manifests:
        stage_status = manifest.get("stage_status") or {}
        stage_results = manifest.get("stage_results") or {}
        for raw_stage in ("discover", "details", "refresh", "evaluate"):
            if raw_stage not in stage_status and raw_stage not in stage_results and manifest.get("stage") != raw_stage:
                continue
            stage = "details" if raw_stage == "refresh" else raw_stage
            latest[stage] = manifest
            terminal_stage_state = stage_status.get(raw_stage)
            if terminal_stage_state in {"success", "completed"}:
                latest["_successful"][stage] = manifest
    return latest


def _job_state(manifest: Optional[Dict[str, Any]], stage: str) -> Optional[str]:
    if not manifest:
        return None
    if manifest.get("status") in {"queued", "running"} and (
        manifest.get("current_stage") == stage
        or manifest.get("stage") == stage
        or (manifest.get("stage") == "collect_and_evaluate" and manifest.get("current_stage") == stage)
    ):
        return "running"
    stage_state = manifest.get("stage_status", {}).get(stage)
    if not stage_state and manifest.get("stage") == stage:
        stage_state = manifest.get("status")
    if not stage_state and manifest.get("stage") == "collect_and_evaluate":
        stage_state = manifest.get("stage_status", {}).get(stage)
    mapping = {
        "completed": "success",
        "success": "success",
        "partial": "partial",
        "failed": "failed",
        "cancelled": "cancelled",
        "interrupted": "partial",
        "no_matches": "no_matches",
        "blocked": "blocked",
        "no_data": "blocked",
        "no_candidates": "no_matches",
        "no_personalized_score": "blocked",
    }
    return mapping.get(str(stage_state), None)


def _status_source_signatures(search_dir: Path) -> tuple:
    output_dir = search_dir / "output_data"
    return tuple(
        (name, _file_signature(output_dir / filename))
        for name, filename in (
            ("results", "results_df.csv"),
            ("details", "details_df.csv"),
            ("reviews", "reviews_df.csv"),
        )
    )


def _evaluation_status_key(
    search_name: str,
    search_dir: Path,
    profile_name: str,
    profile_hash: Optional[str],
    revision: Optional[int],
    current_ids: set[str],
) -> tuple:
    output_dir = search_dir / "output_data"
    return (
        str(Path(PROJECT_ROOT).resolve()),
        search_name,
        profile_name,
        profile_hash,
        revision,
        tuple(sorted(current_ids)),
        _file_signature(output_dir / "merged_df.csv"),
        _file_signature(output_dir / "personal_evaluations.json"),
        _file_signature(search_dir / "evaluation_profiles.json"),
        _file_signature(search_dir / "search_definition.json"),
    )


def _cached_evaluation_status(key: tuple, source_signatures: tuple) -> Optional[Dict[str, Any]]:
    with _STATUS_CACHE_LOCK:
        cached = _EVALUATION_STATUS_CACHE.get(key)
        if cached is None:
            return None
        if cached.get("uses_source_snapshots") and cached.get("source_signatures") != source_signatures:
            _EVALUATION_STATUS_CACHE.pop(key, None)
            return None
        _EVALUATION_STATUS_CACHE.move_to_end(key)
        # Copy the small maps so downstream code cannot mutate the cached value.
        return {
            "eval_statuses": dict(cached["eval_statuses"]),
            "eval_profile_names": dict(cached["eval_profile_names"]),
            "merged_error": cached.get("merged_error"),
        }


def _cache_evaluation_status(key: tuple, value: Dict[str, Any]) -> None:
    with _STATUS_CACHE_LOCK:
        root_key, search_name, profile_name = key[:3]
        for old_key in list(_EVALUATION_STATUS_CACHE):
            if old_key[:3] == (root_key, search_name, profile_name) and old_key != key:
                _EVALUATION_STATUS_CACHE.pop(old_key, None)
        _EVALUATION_STATUS_CACHE[key] = value
        _EVALUATION_STATUS_CACHE.move_to_end(key)
        while len(_EVALUATION_STATUS_CACHE) > _EVALUATION_STATUS_CACHE_MAX:
            _EVALUATION_STATUS_CACHE.popitem(last=False)


def _evaluation_status_maps(
    search_name: str,
    profile_name: str,
    profile: Optional[Dict[str, Any]],
    profile_hash: Optional[str],
    definition: Optional[Dict[str, Any]],
    search_dir: Path,
    current_results: pd.DataFrame,
    current_details: pd.DataFrame,
    reviews: pd.DataFrame,
    current_ids: set[str],
) -> tuple[Dict[str, str], Dict[str, str], Optional[str], Optional[str]]:
    """Return evaluation status maps, hydrating only when a source signature changes."""
    output_dir = search_dir / "output_data"
    key = _evaluation_status_key(
        search_name, search_dir, profile_name, profile_hash,
        int(definition["trip_revision"]) if definition else None,
        current_ids,
    )
    source_signatures = _status_source_signatures(search_dir)
    cached = _cached_evaluation_status(key, source_signatures)
    if cached is not None:
        return (
            cached["eval_statuses"],
            cached["eval_profile_names"],
            cached.get("merged_error"),
            None,
        )

    merged_path = output_dir / "merged_df.csv"
    merged, merged_error = _read_csv(merged_path, cache=False)
    uses_source_snapshots = merged.empty
    if not merged_error and merged.empty and not current_results.empty and definition:
        # Older workspaces may not yet have a merged snapshot. Read full source
        # columns only for this cold fallback; the normal status path keeps only
        # compact ID/revision/attempt projections in memory.
        full_results, results_error = _read_csv(output_dir / "results_df.csv", cache=False)
        full_details, details_error = _read_csv(output_dir / "details_df.csv", cache=False)
        full_reviews, reviews_error = _read_csv(output_dir / "reviews_df.csv", cache=False)
        fallback_errors = [error for error in (results_error, details_error, reviews_error) if error]
        if fallback_errors:
            merged_error = "; ".join(fallback_errors)
        else:
            try:
                from .airbnb_service import merge_property_snapshots

                merged = merge_property_snapshots(
                    full_results, full_details, full_reviews, definition
                )
            except Exception as exc:
                merged_error = f"Could not merge saved snapshots: {type(exc).__name__}: {exc}"

    eval_statuses: Dict[str, str] = {}
    eval_profile_names: Dict[str, str] = {}
    hydration_error = None
    if not merged_error and not merged.empty and "room_id" in merged.columns:
        eval_frame = merged
        if current_ids:
            eval_frame = eval_frame[eval_frame["room_id"].astype(str).isin(current_ids)]
        try:
            from .evaluation_service import hydrate_personal_evaluations

            hydrated = hydrate_personal_evaluations(search_name, eval_frame, profile_name) if profile else eval_frame
            if not hydrated.empty and "personal_fit_status" in hydrated.columns:
                eval_statuses = hydrated.set_index(hydrated["room_id"].astype(str))["personal_fit_status"].to_dict()
            if not hydrated.empty and "personal_fit_profile" in hydrated.columns:
                eval_profile_names = hydrated.set_index(hydrated["room_id"].astype(str))["personal_fit_profile"].to_dict()
        except Exception as exc:
            hydration_error = f"Could not validate saved evaluations: {type(exc).__name__}: {exc}"

    after_source_signatures = _status_source_signatures(search_dir)
    after_key = _evaluation_status_key(
        search_name, search_dir, profile_name, profile_hash,
        int(definition["trip_revision"]) if definition else None,
        current_ids,
    )
    if (
        hydration_error is None
        and merged_error is None
        and _unchanged_or_imported(key, after_key)
        and (not uses_source_snapshots or _unchanged_or_imported(source_signatures, after_source_signatures))
    ):
        _cache_evaluation_status(after_key, {
            "eval_statuses": eval_statuses,
            "eval_profile_names": eval_profile_names,
            "merged_error": merged_error,
            "uses_source_snapshots": uses_source_snapshots,
            "source_signatures": after_source_signatures,
        })
    return eval_statuses, eval_profile_names, merged_error, hydration_error


def get_pipeline_status(search_name: str, profile_name: str = "Default") -> Dict[str, Any]:
    """Aggregate persisted counts and stage history without mutating workspace data."""
    search_dir = _search_dir(search_name)
    if not search_dir.exists():
        raise FileNotFoundError(f"Search workspace '{search_name}' does not exist.")
    try:
        definition = load_search_definition(search_name, root=PROJECT_ROOT / "searches")
        definition_error = None
    except Exception as exc:
        definition = None
        definition_error = f"Trip definition is unreadable: {type(exc).__name__}: {exc}"
    revision = int(definition["trip_revision"]) if definition else None
    output_dir = search_dir / "output_data"
    results, results_error = _read_csv(
        output_dir / "results_df.csv", columns=_STATUS_CSV_COLUMNS["results"]
    )
    details, details_error = _read_csv(
        output_dir / "details_df.csv", columns=_STATUS_CSV_COLUMNS["details"]
    )
    reviews, reviews_error = _read_csv(
        output_dir / "reviews_df.csv", columns=_STATUS_CSV_COLUMNS["reviews"]
    )
    active = _active_manifest(search_dir)
    history = _job_history(search_dir)
    has_writer = bool((search_dir / "jobs" / "writer.lock").exists() and active and active.get("status") in {"queued", "running"})

    all_result_ids = _id_set(results)
    current_results, stale_result_rows = _revision_rows(results, revision)
    current_ids = _id_set(current_results)
    if definition is None and results_error is None:
        # Legacy workspaces can display prior results, but a stage cannot claim
        # trip-current freshness or start until a definition is saved.
        current_ids = all_result_ids

    if 'discovery_state' in current_results:
        current_results = current_results[~current_results['discovery_state'].eq('not_returned')]
        current_ids = _id_set(current_results)
    discover_job = history.get("discover")
    discover_job_state = _job_state(discover_job, "discover")
    if definition_error or results_error:
        discover_state = "failed"
        discover_reason = definition_error or results_error
    elif definition is None:
        discover_state = "blocked"
        discover_reason = "Save dates, party, currency, and a map area before discovering listings."
    elif active and active.get("status") in {"queued", "running"} and active.get("current_stage") == "discover":
        discover_state = "running"
        discover_reason = active.get("message", "Discovery is running.")
    elif current_ids:
        # Current-revision rows remain usable when a later retry fails before
        # replacing that saved snapshot. The latest attempt stays visible in
        # the separate job summary.
        discover_state = "success"
        discover_reason = f"{len(current_ids)} current listings are saved."
        if discover_job_state in {"partial", "failed", "cancelled"}:
            discover_reason += f" Last attempt: {discover_job_state}."
    elif stale_result_rows:
        discover_state = "stale"
        discover_reason = f"Saved discovery rows belong to an older trip revision ({stale_result_rows} rows)."
    elif discover_job_state == "no_matches":
        discover_state = "no_matches"
        discover_reason = "Discovery completed with no listings in the selected area."
    elif discover_job_state in {"failed", "cancelled", "partial"}:
        discover_state = discover_job_state
        discover_reason = (discover_job or {}).get("error") or (discover_job or {}).get("message", "Discovery did not finish.")
    else:
        discover_state = "ready"
        discover_reason = "Ready to discover listings for this trip."
    discover = {
        "state": discover_state,
        "count": 0 if discover_state == "running" else len(current_ids),
        "total": 0 if discover_state == "running" else len(current_ids),
        "previous_count": len(current_ids) if discover_state == "running" else 0,
        "completed": discover_state == "success",
        "failed": int(discover_state == "failed"),
        "missing": 0 if current_ids else 1,
        "stale": stale_result_rows,
        "can_start": bool(definition and not has_writer and discover_state not in {"running", "success"}),
        "reason": discover_reason,
        "job": _job_summary(discover_job, "discover"),
        "last_attempt": _job_summary(discover_job, "discover"),
        "last_successful_job": _job_summary(history.get("_successful", {}).get("discover"), "discover"),
    }

    from .processing_service import load_selection, select_properties
    processing_selection = load_selection(search_dir, definition) if definition else None
    discovered_ids = set(current_ids)
    if processing_selection is not None:
        current_ids &= set(select_properties(current_results, definition, processing_selection)['selected_ids'])
    current_details, stale_detail_rows = _revision_rows(details, revision)
    if not current_details.empty and "room_id" in current_details.columns:
        current_details = current_details[current_details["room_id"].astype(str).isin(current_ids)]
    details_by_id = {
        str(row.get("room_id")): row.to_dict()
        for _, row in current_details.iterrows()
        if row.get("room_id") is not None and str(row.get("room_id")).strip()
    }
    review_ids = _id_set(reviews) & current_ids
    collection_failure_ids: set[str] = set()
    detail_failure_ids_from_snapshot: set[str] = set()
    review_failure_ids_from_snapshot: set[str] = set()
    unclassified_failure_ids: set[str] = set()
    snapshot_attempted_ids: set[str] = set()
    snapshot_unattempted_ids: set[str] = set()
    failure_details: list[Dict[str, Any]] = []
    rate_limited = False
    collection_failure_error = None
    failure_snapshot_path = output_dir / "collection_failures.json"
    try:
        failure_snapshot = _read_json(failure_snapshot_path)
        if failure_snapshot and (revision is None or int(failure_snapshot.get("trip_revision", -1)) == revision):
            rate_limited = bool(failure_snapshot.get("rate_limited", False))
            snapshot_attempted_ids = {str(rid).strip() for rid in failure_snapshot.get("attempted_room_ids", []) if str(rid).strip()} & current_ids
            snapshot_unattempted_ids = {str(rid).strip() for rid in failure_snapshot.get("unattempted_room_ids", []) if str(rid).strip()} & current_ids
            failure_details = [item for item in failure_snapshot.get("failure_details", []) if isinstance(item, dict)]
            failure_details.extend(item for item in failure_snapshot.get("failed", []) if isinstance(item, dict))
            for item in failure_details:
                room_id = str(item.get("room_id", "")).strip()
                if not room_id or room_id not in current_ids:
                    continue
                collection_failure_ids.add(room_id)
                failed_stage = str(item.get("stage", "")).strip().lower()
                if failed_stage in {"detail", "details", "get_details", "quote"}:
                    detail_failure_ids_from_snapshot.add(room_id)
                elif failed_stage in {"review", "reviews", "get_reviews"}:
                    review_failure_ids_from_snapshot.add(room_id)
                else:
                    unclassified_failure_ids.add(room_id)
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        collection_failure_error = "The saved collection failure list is unreadable."
    detail_success_ids: set[str] = set()
    detail_unavailable_ids: set[str] = set()
    detail_failed_ids: set[str] = set()
    detail_attempt_ids: set[str] = set()
    for room_id, row in details_by_id.items():
        attempt_status = str(row.get("quote_attempt_status", "")).strip().lower()
        if attempt_status == "unavailable" or str(row.get("availability_state", "")).strip().lower() == "unavailable":
            detail_unavailable_ids.add(room_id)
            detail_attempt_ids.add(room_id)
        elif attempt_status == "failed":
            detail_failed_ids.add(room_id)
            detail_attempt_ids.add(room_id)
        elif attempt_status == "success" or (
            pd.notna(row.get("details_fetched_at"))
            and str(row.get("details_fetched_at")).strip().lower() not in {"", "nan", "none"}
        ):
            detail_success_ids.add(room_id)
            detail_attempt_ids.add(room_id)
    detail_job = history.get("details")
    detail_failure_result = (detail_job or {}).get("stage_results", {}).get("details") or (detail_job or {}).get("stage_results", {}).get("refresh") or {}
    if (detail_job or {}).get("trip_revision") in {None, revision}:
        attempted_from_job = {str(rid).strip() for rid in detail_failure_result.get("attempted_room_ids", []) if str(rid).strip()} & current_ids
        detail_attempt_ids |= attempted_from_job
        rate_limited = rate_limited or bool(detail_failure_result.get("rate_limited", False))
        failure_details.extend(item for item in detail_failure_result.get("failure_details", []) if isinstance(item, dict))
        for item in detail_failure_result.get("failure_details", []):
            if not isinstance(item, dict):
                continue
            room_id = str(item.get("room_id", "")).strip()
            failed_stage = str(item.get("stage", "")).strip().lower()
            if room_id not in current_ids:
                continue
            collection_failure_ids.add(room_id)
            if failed_stage in {"detail", "details", "get_details", "quote"}:
                detail_failure_ids_from_snapshot.add(room_id)
            elif failed_stage in {"review", "reviews", "get_reviews"}:
                review_failure_ids_from_snapshot.add(room_id)
            else:
                unclassified_failure_ids.add(room_id)
        snapshot_unattempted_ids |= {str(rid).strip() for rid in detail_failure_result.get("unattempted_room_ids", []) if str(rid).strip()} & current_ids
    detail_failed_ids |= detail_failure_ids_from_snapshot
    detail_attempt_ids |= detail_failed_ids
    detail_missing_ids = current_ids - detail_attempt_ids
    review_expected_ids = (current_ids - detail_unavailable_ids)
    review_failed_ids = (review_failure_ids_from_snapshot & review_expected_ids & current_ids) - detail_unavailable_ids - review_ids
    review_attempted_ids = review_ids | review_failed_ids
    review_missing_ids = review_expected_ids - review_attempted_ids
    unclassified_failure_ids -= detail_failed_ids | review_failed_ids
    overall_failed_ids = detail_failed_ids | review_failed_ids | unclassified_failure_ids
    detail_terminal_ids = detail_success_ids | detail_unavailable_ids
    completed_detail_ids = detail_unavailable_ids | (detail_success_ids & review_ids)
    detail_job_state = _job_state(detail_job, "details")
    active_detail = bool(active and active.get("status") in {"queued", "running"} and active.get("current_stage") in {"details", "refresh"})
    details_complete = bool(current_ids) and len(completed_detail_ids) == len(current_ids) and not overall_failed_ids
    detail_count = len(completed_detail_ids)
    unclassified_failures = max(
        len(unclassified_failure_ids),
        int(detail_failure_result.get("failure_count", 0) or 0) - len(detail_failed_ids) - len(review_failed_ids),
    )
    unclassified_failures = min(len(current_ids), unclassified_failures)
    # Older failure snapshots did not identify the failed substage. Treat an
    # ID with no detail checkpoint as attempted, but keep it unclassified.
    detail_attempt_ids |= unclassified_failure_ids - detail_success_ids - detail_unavailable_ids
    detail_missing_ids = current_ids - detail_attempt_ids
    unattempted_ids = snapshot_unattempted_ids or (current_ids - detail_attempt_ids - overall_failed_ids - detail_unavailable_ids)
    first_failure = next((item.get("error") for item in failure_details if item.get("error")), None)
    first_failure = _safe_text(first_failure or (detail_job or {}).get("message") or "")
    if rate_limited and "429" not in first_failure and "rate limit" not in first_failure.lower():
        first_failure = f"The provider rate limited collection (HTTP 429). {first_failure}".strip()
    if definition_error or details_error or reviews_error or collection_failure_error:
        details_state = "failed"
        details_reason = definition_error or collection_failure_error or "; ".join(message for message in (details_error, reviews_error) if message)
    elif not definition:
        details_state = "blocked"
        details_reason = "Save a trip definition before collecting details and reviews."
    elif not current_ids:
        details_state = "no_matches" if discover_state == "no_matches" else "blocked"
        details_reason = "Discover current listings before collecting details and reviews."
    elif active_detail:
        details_state = "running"
        details_reason = active.get("message", "Details and reviews are being collected.")
    elif details_complete:
        details_state = "success"
        unavailable_n = len(detail_unavailable_ids)
        details_reason = f"Details and reviews are complete for {len(current_ids)} listings; {unavailable_n} unavailable listing(s) were terminal skips."
        if detail_job_state in {"partial", "failed", "cancelled"}:
            details_reason += f" Last attempt: {detail_job_state}; current complete records are retained."
    elif detail_job_state in {"failed", "cancelled"} and not detail_attempt_ids:
        details_state = detail_job_state
        details_reason = (detail_job or {}).get("error") or (detail_job or {}).get("message", "Collection did not finish.")
    elif overall_failed_ids and not completed_detail_ids:
        details_state = "failed"
        details_reason = f"Collection has {len(overall_failed_ids)} failed listing(s) and no complete records; retry this stage."
        if first_failure:
            details_reason += f" First error: {first_failure}"
    elif detail_attempt_ids or detail_job_state == "partial":
        details_state = "partial"
        details_reason = f"Collection covers {detail_count} of {len(current_ids)} current listings; {len(overall_failed_ids)} failed and {len(unattempted_ids)} not attempted. Retry this stage to fill missing records."
        if first_failure:
            details_reason += f" First error: {first_failure}"
    else:
        details_state = "ready"
        details_reason = "Ready to collect current listing details and reviews."
    reviews_failed: Optional[int] = None if unclassified_failures and review_missing_ids else len(review_failed_ids)
    details_stage = {
        "state": details_state,
        "count": detail_count,
        "total": len(current_ids),
        "completed": details_complete,
        "failed": len(overall_failed_ids),
        "missing": len(current_ids - completed_detail_ids - overall_failed_ids),
        "unattempted": len(unattempted_ids),
        "stale": stale_detail_rows,
        "rate_limited": rate_limited,
        "attempted_room_ids": sorted(detail_attempt_ids | review_attempted_ids),
        "failed_room_ids": sorted(overall_failed_ids),
        "unattempted_room_ids": sorted(unattempted_ids),
        "can_start": bool(definition and current_ids and not has_writer and details_state not in {"running", "success", "no_matches"}),
        "reason": details_reason,
        "details": {
            "attempted": len(detail_attempt_ids),
            "succeeded": len(detail_success_ids),
            "unavailable": len(detail_unavailable_ids),
            "failed": len(detail_failed_ids),
            "unclassified_failures": len(unclassified_failure_ids),
            "missing": len(detail_missing_ids),
            "unattempted": len(current_ids - detail_attempt_ids - detail_failed_ids - detail_unavailable_ids),
        },
        "reviews": {
            "attempted": len(review_attempted_ids),
            "succeeded": len(review_ids),
            "failed": reviews_failed,
            "unclassified_failures": unclassified_failures if reviews_failed is None else 0,
            "missing": len(review_missing_ids),
            "unattempted": len(review_missing_ids - unclassified_failure_ids),
        },
        "failure_details": _safe_value(failure_details),
        "job": _job_summary(detail_job, "details"),
        "last_attempt": _job_summary(detail_job, "details"),
        "last_successful_job": _job_summary(history.get("_successful", {}).get("details"), "details"),
    }

    profile_error = None
    profile_saved = False
    saved_profile_path = search_dir / "evaluation_profiles.json"
    if storage.exists(saved_profile_path):
        try:
            saved_profile_data = storage.read_json(saved_profile_path)
            saved_profiles = saved_profile_data.get("profiles", {}) if isinstance(saved_profile_data, dict) else {}
            profile_saved = isinstance(saved_profiles, dict) and profile_name in saved_profiles
        except (OSError, json.JSONDecodeError, AttributeError):
            profile_error = "Saved evaluation profiles are unreadable; save the selected profile again."
    try:
        from .evaluation_service import (
            load_evaluation_profile,
            profile_fingerprint,
        )
        profile = load_evaluation_profile(search_name, profile_name)
        profile_hash = profile_fingerprint(profile)
        if not profile_saved:
            profile_error = f"Save the selected evaluation profile '{profile_name}' before evaluation."
    except Exception as exc:
        profile = None
        profile_hash = None
        profile_error = f"Selected profile is unavailable: {type(exc).__name__}: {exc}"

    eligible_ids = sorted(completed_detail_ids - detail_unavailable_ids - overall_failed_ids)
    pending_collection_ids = sorted(current_ids - set(eligible_ids) - detail_unavailable_ids)
    matching_profile_jobs = [
        manifest for manifest in history.get("_all", [])
        if manifest.get("profile_name", "Default") == profile_name
        and ("evaluate" in (manifest.get("stage_status") or {}) or manifest.get("stage") == "evaluate")
    ]
    evaluation_job = matching_profile_jobs[-1] if matching_profile_jobs else None
    evaluation_job_current = (
        evaluation_job
        if evaluation_job and evaluation_job.get("profile_fingerprint") == profile_hash
        else None
    )
    successful_profile_jobs = [
        manifest for manifest in matching_profile_jobs
        if manifest.get("stage_status", {}).get("evaluate") in {"success", "completed"}
        and manifest.get("profile_fingerprint") == profile_hash
    ]
    evaluation_job_state = _job_state(evaluation_job_current, "evaluate")
    evaluation_active = bool(active and active.get("status") in {"queued", "running"} and active.get("current_stage") == "evaluate")
    eval_statuses: Dict[str, str] = {}
    eval_profile_names: Dict[str, str] = {}
    merged_error = None
    hydration_error = None
    try:
        eval_statuses, eval_profile_names, merged_error, hydration_error = _evaluation_status_maps(
            search_name,
            profile_name,
            profile,
            profile_hash,
            definition,
            search_dir,
            current_results,
            current_details,
            reviews,
            current_ids,
        )
    except Exception as exc:
        hydration_error = f"Could not validate saved evaluations: {type(exc).__name__}: {exc}"
    if hydration_error:
        profile_error = profile_error or hydration_error
    scored_ids = {room_id for room_id in eligible_ids if eval_statuses.get(room_id) == "scored" and eval_profile_names.get(room_id) == profile_name}
    insufficient_evidence_ids = {room_id for room_id in eligible_ids if eval_statuses.get(room_id) == "unknown" and eval_profile_names.get(room_id) == profile_name}
    evaluated_ids = scored_ids | insufficient_evidence_ids
    stale_eval_ids = {room_id for room_id in eligible_ids if eval_statuses.get(room_id) == "stale"}
    missing_eval_ids = set(eligible_ids) - evaluated_ids - stale_eval_ids
    eval_result = (evaluation_job_current or {}).get("stage_results", {}).get("evaluate") or {}
    eval_failed_count = int(eval_result.get("failed", 0) or 0)
    active_eval_stage = bool(active and active.get("status") in {"queued", "running"} and active.get("current_stage") == "evaluate")
    has_active_criteria = bool(profile and any(int(weight) > 0 for weight in (profile.get("weights") or {}).values()))
    if definition_error or merged_error:
        evaluation_state = "failed"
        evaluation_reason = definition_error or merged_error
    elif not definition:
        evaluation_state = "blocked"
        evaluation_reason = profile_error or "Save a trip definition before evaluating listings."
    elif not current_ids:
        evaluation_state = "no_matches" if discover_state == "no_matches" else "blocked"
        evaluation_reason = "Discover listings before evaluation."
    elif profile_error:
        evaluation_state = "blocked"
        evaluation_reason = profile_error
    elif not eligible_ids:
        evaluation_state = "blocked" if pending_collection_ids else "no_matches"
        evaluation_reason = ("AI can start once at least one listing has details and a reviews checkpoint."
                             if pending_collection_ids else "All current listings are unavailable for these dates.")
    elif not has_active_criteria:
        evaluation_state = "blocked"
        evaluation_reason = "Give at least one personal-priority criterion a nonzero weight before evaluation."
    elif evaluation_active:
        evaluation_state = "running"
        evaluation_reason = active.get("message", "Personalized evaluation is running.")
    elif len(evaluated_ids) == len(eligible_ids):
        evaluation_state = "success"
        evaluation_reason = f"All {len(eligible_ids)} eligible listings have current evaluations; {len(scored_ids)} have numeric scores and {len(insufficient_evidence_ids)} have insufficient evidence."
        if evaluation_job_state in {"partial", "failed", "cancelled"}:
            evaluation_reason += f" Last attempt: {evaluation_job_state}; current scores are retained."
    elif eval_failed_count and not scored_ids:
        evaluation_state = "failed"
        evaluation_reason = (evaluation_job_current or {}).get("message", "Evaluation failed.")
    elif evaluated_ids or stale_eval_ids or (evaluation_job_state == "partial"):
        evaluation_state = "partial"
        evaluation_reason = f"{len(scored_ids)} of {len(eligible_ids)} eligible listings have current scores."
    elif evaluation_job_state in {"blocked", "no_matches", "failed", "cancelled"}:
        evaluation_state = evaluation_job_state
        evaluation_reason = (evaluation_job_current or {}).get("message", "Evaluation did not produce current scores.")
    else:
        evaluation_state = "ready"
        evaluation_reason = "Ready to evaluate eligible listings with the selected profile."
    if eligible_ids and pending_collection_ids:
        evaluation_reason += f" {len(pending_collection_ids)} listing(s) still need details or reviews."
    evaluation = {
        "state": evaluation_state,
        "count": len(evaluated_ids),
        "evaluated_count": len(evaluated_ids),
        "total": len(eligible_ids),
        "scored_count": len(scored_ids),
        "insufficient_evidence_count": len(insufficient_evidence_ids),
        "eligible": len(eligible_ids),
        "skipped_unavailable": len(detail_unavailable_ids),
        "completed": evaluation_state == "success",
        "failed": eval_failed_count,
        "missing": len(missing_eval_ids),
        "stale": len(stale_eval_ids),
        "can_start": bool(definition and profile and has_active_criteria and eligible_ids and not has_writer and evaluation_state not in {"running", "success", "no_matches"}),
        "reason": evaluation_reason,
        "profile_fingerprint": profile_hash,
        "job": _job_summary(evaluation_job_current, "evaluate"),
        "last_attempt": _job_summary(evaluation_job, "evaluate"),
        "last_successful_job": _job_summary(successful_profile_jobs[-1], "evaluate") if successful_profile_jobs else None,
    }

    stages = {"discover": discover, "details": details_stage, "evaluate": evaluation}
    next_stage = None
    if discover_state != "success":
        next_stage = "discover" if discover["can_start"] else None
    elif details_state != "success":
        next_stage = "details" if details_stage["can_start"] else None
    elif details_state == "success" and evaluation_state not in {"success", "no_matches"}:
        next_stage = "evaluate" if evaluation["can_start"] else None
    return {
        "search_name": search_name,
        "profile_name": profile_name,
        "trip_revision": revision,
        "profile_fingerprint": profile_hash,
        "processing_selected_count": len(current_ids),
        "discovered_count": len(discovered_ids),
        "eligible_room_ids": eligible_ids,
        "pending_collection_room_ids": pending_collection_ids,
        "active_job": active,
        "next_stage": next_stage,
        "stages": stages,
    }
