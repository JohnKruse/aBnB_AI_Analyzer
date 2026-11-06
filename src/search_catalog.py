"""Metadata catalog and safe lifecycle operations for saved searches.

The catalog reads the compact trip definition and timestamp columns from the
workspace database. Large listing frames are never loaded to render the
search selector. CSV-only legacy workspaces use file modification times as an
explicitly approximate fallback.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import shutil
import threading
import uuid
from collections import OrderedDict
from contextlib import ExitStack, closing, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

import yaml

from . import search_service, storage_service

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows relies on the thread lock.
    fcntl = None


class SearchLifecycleError(RuntimeError):
    """Raised when an archive or delete conflicts with an active writer."""


_LOCKS_GUARD = threading.Lock()
_LIFECYCLE_LOCKS: Dict[str, threading.RLock] = {}
_TIMESTAMP_CACHE_LOCK = threading.RLock()
_TIMESTAMP_CACHE: "OrderedDict[Tuple[str, str, str], Optional[str]]" = OrderedDict()
_TIMESTAMP_CACHE_MAX = 256
_OUTPUT_TIMESTAMP_FIELDS = {
    "results_df.csv": "discovered_at",
    "details_df.csv": "details_fetched_at",
    "reviews_df.csv": "reviews_fetched_at",
}


def _searches_root(root: Optional[os.PathLike[str] | str]) -> Path:
    if root is None:
        root = search_service.SEARCHES_ROOT
    return Path(root).expanduser().resolve()


def _validate_name(name: str) -> str:
    return search_service.validate_existing_search_name(name)


def _workspace_path(name: str, root: Path, *, require_exists: bool = True) -> Path:
    """Return a direct, non-symlink child workspace below root."""
    safe_name = _validate_name(name)
    candidate = root / safe_name
    if candidate.is_symlink():
        raise ValueError(f"Search workspace '{safe_name}' cannot be a symbolic link.")
    if require_exists and not candidate.is_dir():
        raise FileNotFoundError(f"Search workspace '{safe_name}' does not exist.")
    if not candidate.exists():
        return candidate
    resolved = candidate.resolve(strict=True)
    if resolved.parent != root or resolved != candidate.absolute():
        raise ValueError("Search workspace path escapes the searches directory.")
    return resolved


def _safe_child_directory(parent: Path, name: str, *, create: bool = False) -> Path:
    child = parent / name
    if child.is_symlink():
        raise ValueError(f"Unsafe symbolic link at {child}.")
    if create:
        child.mkdir(parents=False, exist_ok=True)
    if child.exists():
        resolved = child.resolve(strict=True)
        if resolved.parent != parent or resolved != child.absolute() or not resolved.is_dir():
            raise ValueError(f"Unsafe directory path at {child}.")
        return resolved
    return child


@contextmanager
def search_lifecycle_lock(
    name: str,
    *,
    root: Optional[os.PathLike[str] | str] = None,
) -> Iterator[None]:
    """Serialize job startup with archive/delete for one search slug.

    Lock files live beside workspaces so moving a workspace to trash cannot
    remove the lock used by another process.
    """
    safe_name = _validate_name(name)
    searches = _searches_root(root)
    searches.mkdir(parents=True, exist_ok=True)
    lifecycle_dir = _safe_child_directory(searches, ".lifecycle", create=True)
    digest = hashlib.sha256(safe_name.encode("utf-8")).hexdigest()
    lock_path = lifecycle_dir / f"{digest}.lock"
    if lock_path.is_symlink():
        raise ValueError(f"Unsafe lifecycle lock path at {lock_path}.")
    lock_key = str(lock_path.absolute())
    with _LOCKS_GUARD:
        thread_lock = _LIFECYCLE_LOCKS.setdefault(lock_key, threading.RLock())
    with thread_lock:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(lock_path), flags, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _read_object(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unreadable JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON at {path} must contain an object.")
    return value


def _pid_is_alive(value: Any) -> bool:
    try:
        pid = int(value)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (TypeError, ValueError, ProcessLookupError, OSError):
        return False


def _assert_not_active(workspace: Path) -> None:
    jobs = _safe_child_directory(workspace, "jobs")
    if not jobs.exists():
        return
    for filename in ("writer.lock", "active_job.json"):
        path = jobs / filename
        if path.is_symlink():
            raise SearchLifecycleError(f"Cannot verify active job state because {path} is a symbolic link.")
        if not path.exists():
            continue
        try:
            state = _read_object(path)
        except ValueError as exc:
            raise SearchLifecycleError(f"Cannot verify active job state: {exc}") from exc
        if not state:
            if filename == "writer.lock":
                raise SearchLifecycleError(f"Cannot verify active writer lock at {path}.")
            continue
        if filename == "writer.lock":
            if _pid_is_alive(state.get("pid")):
                raise SearchLifecycleError("This search has an active writer job.")
            continue
        status = str(state.get("status", "")).casefold()
        if status not in {"queued", "running"}:
            continue
        pid = state.get("pid")
        if pid is None or _pid_is_alive(pid):
            raise SearchLifecycleError("This search has a queued or running job.")


def _today(value: Any = None) -> date:
    if value is None:
        return datetime.now(ZoneInfo("Europe/Rome")).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("today must be a date or YYYY-MM-DD value.") from exc


def _parse_trip_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _definition_for(name: str, workspace: Path, root: Path) -> Tuple[Dict[str, Any], str, Optional[str]]:
    definition_path = workspace / "search_definition.json"
    try:
        data = search_service.load_search_definition(name, root=root)
    except (OSError, ValueError) as exc:
        return {}, "search_definition.json", str(exc)
    if isinstance(data, dict):
        return data, "search_definition.json", None

    config_path = workspace / "config.yaml"
    if not config_path.exists():
        return {}, "unknown", None
    if config_path.is_symlink():
        return {}, "config.yaml", f"Unsafe symbolic link at {config_path}."
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {}, "config.yaml", f"Could not read {config_path}: {exc}"
    if not isinstance(loaded, dict):
        return {}, "config.yaml", f"Configuration at {config_path} is not a mapping."
    return loaded, "config.yaml", None


def _metadata_path(workspace: Path) -> Path:
    return workspace / "search_metadata.json"


def _read_metadata(workspace: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    path = _metadata_path(workspace)
    if path.is_symlink():
        return {}, f"Unsafe symbolic link at {path}."
    try:
        value = storage_service.read_json(path, default=None)
    except (OSError, ValueError) as exc:
        return {}, str(exc)
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return {}, f"Search metadata at {path} must be a JSON object."
    return value, None


def _normalize_timestamp(value: Any) -> Optional[Tuple[datetime, str]]:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip()
    if not raw or raw.casefold() in {"nan", "none", "null", "nat"}:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            parsed_date = date.fromisoformat(raw)
            parsed = datetime.combine(parsed_date, datetime.min.time())
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed, parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _legacy_file_timestamp(path: Path) -> Optional[str]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _database_timestamp(path: Path, field: str) -> Optional[str]:
    """Project one timestamp field from SQLite rows without decoding frames."""
    workspace = path.parent.parent
    database = workspace / storage_service.DATABASE_FILENAME
    if not database.is_file():
        return None
    resource_key = path.relative_to(workspace).as_posix()
    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    latest: Optional[Tuple[datetime, str]] = None
    try:
        # The key is fixed by this module; JSON1 extracts only one small scalar
        # per row, rather than materializing row_json or loading the DataFrame.
        query = (
            "SELECT json_extract(row_json, '$." + field + "') "
            "FROM frame_rows WHERE resource_path = ?"
        )
        for (raw,) in connection.execute(query, (resource_key,)):
            candidate = _normalize_timestamp(raw)
            if candidate is not None and (latest is None or candidate[0] > latest[0]):
                latest = candidate
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return latest[1] if latest else None


def _timestamp_for_resource(path: Path, field: str) -> Tuple[Optional[str], str, bool]:
    try:
        signature = storage_service.signature(path)
    except (OSError, ValueError):
        signature = None
    if signature is None:
        return None, "unknown", False
    cache_key = (str(path.resolve()), signature, field)
    with _TIMESTAMP_CACHE_LOCK:
        if cache_key in _TIMESTAMP_CACHE:
            result = _TIMESTAMP_CACHE.pop(cache_key)
            _TIMESTAMP_CACHE[cache_key] = result
        else:
            result = None
            if signature.startswith("frame:"):
                result = _database_timestamp(path, field)
            elif signature.startswith("legacy:"):
                # Do not import or scan an old CSV merely to populate the menu.
                result = _legacy_file_timestamp(path)
            _TIMESTAMP_CACHE[cache_key] = result
            while len(_TIMESTAMP_CACHE) > _TIMESTAMP_CACHE_MAX:
                _TIMESTAMP_CACHE.popitem(last=False)
    if signature.startswith("legacy:"):
        return result, "file modification time", result is not None
    return result, field, False


def _latest_listing_timestamp(workspace: Path) -> Tuple[Optional[str], str, bool]:
    found: List[Tuple[datetime, str, str, bool]] = []
    for filename, field in (("results_df.csv", "discovered_at"), ("details_df.csv", "details_fetched_at")):
        path = workspace / "output_data" / filename
        value, source, approximate = _timestamp_for_resource(path, field)
        parsed = _normalize_timestamp(value)
        if parsed is not None:
            found.append((parsed[0], parsed[1], source, approximate))
    if not found:
        return None, "unknown", False
    _, value, source, approximate = max(found, key=lambda item: item[0])
    return value, source, approximate


def _property_count(workspace: Path) -> Optional[int]:
    """Count IDs using the database index or a streaming legacy CSV read."""
    database = workspace / storage_service.DATABASE_FILENAME
    for filename in ("results_df.csv", "merged_df.csv"):
        path = workspace / "output_data" / filename
        try:
            signature = storage_service.signature(path)
            if not signature:
                continue
            if signature.startswith("frame:") and database.is_file():
                with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)) as connection:
                    return connection.execute(
                        "SELECT COUNT(DISTINCT room_id) FROM frame_rows WHERE resource_path = ? AND COALESCE(json_extract(row_json, '$.discovery_state'), 'returned') != 'not_returned'",
                        (f"output_data/{filename}",),
                    ).fetchone()[0]
            with path.open(newline="", encoding="utf-8") as handle:
                return len({row["room_id"] for row in csv.DictReader(handle) if row.get("room_id") and row.get("discovery_state") != "not_returned"})
        except (OSError, sqlite3.Error, ValueError):
            return None
    return 0


def _workspace_updated_at(workspace: Path) -> Optional[str]:
    """Include evaluation/profile edits as well as collection in Last updated."""
    database = workspace / storage_service.DATABASE_FILENAME
    if database.is_file() and not database.is_symlink():
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)) as connection:
                value = connection.execute("SELECT MAX(updated_at) FROM resources").fetchone()[0]
                normalized = _normalize_timestamp(value)
                if normalized:
                    return normalized[1]
        except sqlite3.Error:
            pass
    # Legacy workspaces have no resource timestamps; use file times explicitly.
    files = [workspace / "config.yaml", workspace / "search_definition.json",
             workspace / "evaluation_profiles.json", workspace / "user_curation.json"]
    files.extend(workspace / "output_data" / filename for filename in
                 ("results_df.csv", "details_df.csv", "reviews_df.csv", "merged_df.csv"))
    dates = [value for path in files if path.is_file() and not path.is_symlink()
             if (value := _legacy_file_timestamp(path))]
    return max(dates) if dates else None


def _summary_from_workspace(name: str, workspace: Path, root: Path, today: date) -> Dict[str, Any]:
    definition, definition_source, definition_error = _definition_for(name, workspace, root)
    metadata, metadata_error = _read_metadata(workspace)
    check_in = definition.get("check_in")
    check_out = definition.get("check_out")
    trip_revision = definition.get("trip_revision")
    checkout_date = _parse_trip_date(check_out)
    override = metadata.get("archive_override")
    override_valid = True
    if override == "active":
        override_valid = (
            str(metadata.get("archive_override_check_out") or "") == str(check_out or "")
            and metadata.get("archive_override_trip_revision") == trip_revision
        )
    if override == "archived":
        archived, archive_source = True, "manual"
    elif override == "active" and override_valid:
        archived, archive_source = False, "manual"
    elif metadata_error:
        archived, archive_source = True, "unknown"
    elif metadata.get("archived") is True and override is None:
        # Compatibility with early metadata exports that stored only a boolean.
        archived, archive_source = True, "manual"
    elif checkout_date is not None and checkout_date < today:
        archived, archive_source = True, "checkout_date"
    else:
        archived, archive_source = False, "none"

    if archive_source == "manual":
        archive_reason = str(metadata.get("archive_reason") or ("Manually archived" if archived else "Manually restored"))
    elif archive_source == "checkout_date":
        archive_reason = "Checkout date has passed"
    elif archive_source == "unknown":
        archive_reason = "Archive state could not be verified"
    else:
        archive_reason = ""

    listings_at, listings_source, listings_approximate = _latest_listing_timestamp(workspace)
    reviews_path = workspace / "output_data" / "reviews_df.csv"
    reviews_at, reviews_source, reviews_approximate = _timestamp_for_resource(reviews_path, "reviews_fetched_at")
    people = None
    if "adults" in definition:
        people = sum(int(definition.get(key) or 0) for key in ("adults", "children", "infants"))
    updates = [_normalize_timestamp(value) for value in (listings_at, reviews_at, _workspace_updated_at(workspace), definition.get("updated_at"), definition.get("created_at"))]
    valid_updates = [value for value in updates if value]
    updated_at = max(valid_updates)[1] if valid_updates else None
    return {
        "name": name,
        "people": people,
        "properties_count": _property_count(workspace),
        "updated_at": updated_at,
        "location": definition.get("location", definition.get("destination", "Unknown")) or "Unknown",
        "check_in": check_in,
        "check_out": check_out,
        "archived": bool(archived),
        "archive_reason": archive_reason,
        "archive_override": override if override_valid else None,
        "archive_source": archive_source,
        "listings_updated_at": listings_at,
        "listings_updated_at_source": listings_source,
        "listings_updated_at_is_approximate": listings_approximate,
        "reviews_updated_at": reviews_at,
        "reviews_updated_at_source": reviews_source,
        "reviews_updated_at_is_approximate": reviews_approximate,
        "trip_details_known": bool(definition.get("location") and check_in and check_out),
        "definition_source": definition_source,
        "definition_error": definition_error,
        "metadata_error": metadata_error,
        "trip_revision": trip_revision,
    }


def get_search_summary(
    name: str,
    *,
    root: Optional[os.PathLike[str] | str] = None,
    today: Any = None,
) -> Dict[str, Any]:
    """Return one search's trip, collection dates, and effective archive state."""
    searches = _searches_root(root)
    workspace = _workspace_path(name, searches)
    return _summary_from_workspace(_validate_name(name), workspace, searches, _today(today))


def list_search_summaries(
    root: Optional[os.PathLike[str] | str] = None,
    today: Any = None,
) -> List[Dict[str, Any]]:
    """List visible workspaces with compact metadata, excluding trash/locks."""
    searches = _searches_root(root)
    if not searches.exists():
        return []
    if searches.is_symlink() or not searches.is_dir():
        raise ValueError(f"Searches root is not a safe directory: {searches}")
    as_of = _today(today)
    summaries: List[Dict[str, Any]] = []
    for candidate in sorted(searches.iterdir(), key=lambda path: path.name.casefold()):
        if candidate.name.startswith(".") or candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            name = _validate_name(candidate.name)
            workspace = _workspace_path(name, searches)
        except (ValueError, FileNotFoundError):
            continue
        summaries.append(_summary_from_workspace(name, workspace, searches, as_of))
    return summaries


def set_search_archived(
    search_name: str,
    archived: bool,
    *,
    root: Optional[os.PathLike[str] | str] = None,
) -> Dict[str, Any]:
    """Set manual archive state without changing trip or collected data."""
    if not isinstance(archived, bool):
        raise ValueError("archived must be true or false.")
    name = _validate_name(search_name)
    searches = _searches_root(root)
    with search_lifecycle_lock(name, root=searches):
        workspace = _workspace_path(name, searches)
        _assert_not_active(workspace)
        metadata, error = _read_metadata(workspace)
        if error:
            raise ValueError(error)
        definition, _, definition_error = _definition_for(name, workspace, searches)
        if definition_error:
            raise ValueError(f"Cannot safely update archive state: {definition_error}")
        checkout = definition.get("check_out")
        checkout_date = _parse_trip_date(checkout)
        revision = definition.get("trip_revision")
        metadata.pop("archive_reason", None)
        metadata["archive_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if archived:
            metadata["archived"] = True
            metadata["archive_override"] = "archived"
            metadata["archive_reason"] = "Manually archived"
            metadata.pop("archive_override_check_out", None)
            metadata.pop("archive_override_trip_revision", None)
        elif checkout_date is not None and checkout_date < _today():
            # An expired trip requires an explicit active override. Tie it to
            # its dates/revision so an edited trip cannot retain stale state.
            metadata["archived"] = False
            metadata["archive_override"] = "active"
            metadata["archive_reason"] = "Manually restored"
            metadata["archive_override_check_out"] = str(checkout)
            metadata["archive_override_trip_revision"] = revision
        else:
            # Future and unknown-date searches follow normal automatic state.
            metadata.pop("archived", None)
            metadata.pop("archive_override", None)
            metadata.pop("archive_override_check_out", None)
            metadata.pop("archive_override_trip_revision", None)
        storage_service.write_json(_metadata_path(workspace), metadata)
        return _summary_from_workspace(name, workspace, searches, _today())


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def delete_search(
    search_name: str,
    confirmation: str,
    *,
    root: Optional[os.PathLike[str] | str] = None,
) -> str:
    """Permanently remove one workspace after exact confirmation."""
    return delete_searches([search_name], confirmation=[confirmation], root=root)[0]


def delete_searches(
    search_names: List[str],
    confirmation: List[str],
    *,
    root: Optional[os.PathLike[str] | str] = None,
) -> List[str]:
    """Lock and validate the entire explicit batch before removing any search."""
    names = list(dict.fromkeys(_validate_name(name) for name in search_names))
    if not names or list(confirmation) != names:
        raise ValueError("Confirm the exact search IDs to permanently delete these workspaces.")
    searches = _searches_root(root)
    with ExitStack() as locks:
        for name in sorted(names):
            locks.enter_context(search_lifecycle_lock(name, root=searches))
        workspaces = [_workspace_path(name, searches) for name in names]
        for workspace in workspaces:
            _assert_not_active(workspace)
        # Preserve reusable property evidence before deleting any workspace.
        from .property_cache import PropertyCache
        cache = PropertyCache(searches)
        for workspace in workspaces:
            cache.import_workspace(workspace)
        for workspace in workspaces:
            # Each workspace owns its database, including all tables and WALs.
            # Internal symlinks are unlinked, never followed.
            shutil.rmtree(workspace)
        return [str(workspace) for workspace in workspaces]


__all__ = [
    "SearchLifecycleError",
    "delete_search",
    "delete_searches",
    "get_search_summary",
    "list_search_summaries",
    "search_lifecycle_lock",
    "set_search_archived",
]
