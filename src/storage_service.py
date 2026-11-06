"""SQLite-backed storage for search workspaces, with CSV/JSON compatibility.

The legacy resource paths remain the public identifiers.  A path such as
``searches/turin/output_data/merged_df.csv`` is stored in
``searches/turin/analyzer.sqlite3`` and may also be exported to its CSV path.
SQLite is authoritative; compatibility exports can be regenerated at any time.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import threading
import uuid
import warnings
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

try:
    import fcntl
except ImportError:  # pragma: no cover - SQLite still serializes writes on Windows.
    fcntl = None

try:
    import numpy as np
except ImportError:  # pragma: no cover - pandas requires numpy in supported installs.
    np = None


SCHEMA_VERSION = 2
DATABASE_FILENAME = "analyzer.sqlite3"
_SEARCH_SUBDIRS = {"output_data", "jobs", "evaluation_profiles", "personal_evaluations", "evaluation_cache", "profiles"}
_SCHEMA_LOCK = threading.RLock()


class StorageFormatError(ValueError):
    """A legacy CSV/JSON resource is malformed and could not be imported."""


class StorageExportWarning(UserWarning):
    """SQLite committed successfully, but its compatibility export failed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _path(value: os.PathLike[str] | str) -> Path:
    return Path(value).expanduser().resolve()


def _workspace_for_resource(value: os.PathLike[str] | str) -> Path:
    path = _path(value)
    if path.is_dir():
        if path.name in _SEARCH_SUBDIRS:
            return path.parent
        return path
    parent = path.parent
    candidates = (parent, *parent.parents)
    # A database anywhere above the resource is the strongest workspace clue.
    for candidate in candidates:
        if (candidate / DATABASE_FILENAME).exists():
            return candidate
    # Search-workspace layout markers take precedence over nested resource dirs.
    for candidate in candidates:
        if candidate.name == "output_data":
            return candidate.parent
        if candidate.name == "jobs":
            return candidate.parent
        if candidate.name in {"evaluation_profiles", "personal_evaluations", "profiles", "evaluation_cache"}:
            return candidate.parent.parent if candidate.parent.name == "output_data" else candidate.parent
        if candidate.name == "searches":
            relative = parent.relative_to(candidate) if parent != candidate else Path()
            return candidate / relative.parts[0] if relative.parts else candidate
        if (candidate / "output_data").exists() or (candidate / "config.yaml").exists() or (candidate / "search_definition.json").exists():
            return candidate
    return parent


def _relative_resource(path: Path, workspace: Path) -> str:
    try:
        relative = path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"Resource {path} is outside workspace {workspace}.") from exc
    key = relative.as_posix()
    if not key or key == DATABASE_FILENAME or key.startswith("jobs/"):
        raise ValueError("Operational job files and the database itself are not storage resources.")
    if ".." in relative.parts:
        raise ValueError("Resource path escapes its workspace.")
    return key


def _database_path(workspace: os.PathLike[str] | str) -> Path:
    return _path(workspace) / DATABASE_FILENAME


def _connect(workspace: os.PathLike[str] | str, *, create: bool = False) -> Optional[sqlite3.Connection]:
    db_path = _database_path(workspace)
    if not create and not db_path.exists():
        return None
    if create:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if journal_mode != "wal":
        connection.execute("PRAGMA journal_mode = WAL")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        connection.close()
        raise StorageFormatError(
            f"Workspace database schema version {version} is newer than supported version {SCHEMA_VERSION}."
        )
    if version < SCHEMA_VERSION:
        with _schema_lock(db_path):
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                connection.close()
                raise StorageFormatError(
                    f"Workspace database schema version {version} is newer than supported version {SCHEMA_VERSION}."
                )
            if version < SCHEMA_VERSION:
                _ensure_schema(connection)
    return connection


@contextmanager
def _schema_lock(db_path: Path):
    with _SCHEMA_LOCK:
        lock_path = db_path.with_suffix(db_path.suffix + ".schema.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS resources (
            resource_path TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('frame', 'json')),
            revision INTEGER NOT NULL DEFAULT 1,
            schema_json TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS frame_rows (
            resource_path TEXT NOT NULL REFERENCES resources(resource_path) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            room_id TEXT,
            row_json TEXT NOT NULL,
            PRIMARY KEY(resource_path, ordinal)
        );
        CREATE INDEX IF NOT EXISTS frame_rows_room_id_idx ON frame_rows(resource_path, room_id);
        CREATE TABLE IF NOT EXISTS json_documents (
            resource_path TEXT PRIMARY KEY REFERENCES resources(resource_path) ON DELETE CASCADE,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS file_state (
            resource_path TEXT PRIMARY KEY,
            content_hash TEXT,
            file_size INTEGER,
            mtime_ns INTEGER,
            ctime_ns INTEGER,
            inode INTEGER,
            imported_at TEXT
        );
        CREATE TABLE IF NOT EXISTS search_context (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            trip_revision INTEGER,
            check_in TEXT,
            check_out TEXT,
            currency TEXT,
            adults INTEGER,
            children INTEGER,
            infants INTEGER,
            pets INTEGER,
            free_cancellation INTEGER,
            total_budget REAL,
            geometry_json TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quotes (
            quote_key TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            trip_revision INTEGER,
            check_in TEXT,
            check_out TEXT,
            currency TEXT,
            adults INTEGER,
            children INTEGER,
            infants INTEGER,
            pets INTEGER,
            status TEXT,
            total_price REAL,
            source TEXT,
            source_frame TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS curation_items (
            room_id TEXT PRIMARY KEY,
            shortlisted INTEGER NOT NULL DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0,
            manual_order INTEGER,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shortlist_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            name TEXT,
            created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            frame_schema_json TEXT NOT NULL,
            row_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shortlist_snapshot_rows (
            snapshot_id TEXT NOT NULL REFERENCES shortlist_snapshots(snapshot_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            room_id TEXT NOT NULL,
            row_json TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, ordinal)
        );
        """
    )
    with _transaction(connection):
        state_columns = {row["name"] for row in connection.execute("PRAGMA table_info(file_state)")}
        for column in ("ctime_ns", "inode"):
            if column not in state_columns:
                connection.execute(f"ALTER TABLE file_state ADD COLUMN {column} INTEGER")
        quote_columns = {row["name"] for row in connection.execute("PRAGMA table_info(quotes)")}
        if "quote_key" not in quote_columns:
            old_rows = connection.execute("SELECT * FROM quotes").fetchall()
            connection.execute("ALTER TABLE quotes RENAME TO quotes_v1")
            connection.execute(
                "CREATE TABLE quotes (quote_key TEXT PRIMARY KEY, room_id TEXT NOT NULL, trip_revision INTEGER, check_in TEXT, check_out TEXT, currency TEXT, adults INTEGER, children INTEGER, infants INTEGER, pets INTEGER, status TEXT, total_price REAL, source TEXT, source_frame TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            for old in old_rows:
                payload = _json_loads(old["payload_json"])
                record = dict(old)
                record.update(payload if isinstance(payload, dict) else {})
                _store_quote_row(connection, record, str(old["source_frame"]))
            connection.execute("DROP TABLE quotes_v1")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def _transaction(connection: sqlite3.Connection):
    """Serialize a resource mutation and keep its indexes atomic."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _encode(value: Any) -> Any:
    """Encode values without stringifying nested provider payloads."""
    if value is None:
        return {"$storage": "none"}
    if value is pd.NA:
        return {"$storage": "pd.NA"}
    if value is pd.NaT:
        return {"$storage": "pd.NaT"}
    if isinstance(value, (datetime, pd.Timestamp)):
        return {"$storage": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$storage": "date", "value": value.isoformat()}
    if np is not None and isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return {"$storage": "float", "value": "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")}
    if isinstance(value, tuple):
        return {"$storage": "tuple", "items": [_encode(item) for item in value]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        encoded = {str(key): _encode(item) for key, item in value.items()}
        if "$storage" in encoded:
            return {"$storage": "dict", "items": [[str(key), _encode(item)] for key, item in value.items()]}
        return encoded
    if isinstance(value, (str, bool, int, float)):
        return value
    # Avoid silently losing unexpected payload types. Their repr is a typed
    # fallback that remains visible in the stored data and can be reviewed.
    return {"$storage": "repr", "type": type(value).__name__, "value": repr(value)}


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        tag = value.get("$storage")
        if tag == "none":
            return None
        if tag == "pd.NA":
            return pd.NA
        if tag == "pd.NaT":
            return pd.NaT
        if tag == "datetime":
            return pd.Timestamp(value["value"])
        if tag == "date":
            return date.fromisoformat(value["value"])
        if tag == "float":
            return float(value["value"])
        if tag == "tuple":
            return tuple(_decode(item) for item in value.get("items", []))
        if tag == "dict":
            return {str(key): _decode(item) for key, item in value.get("items", [])}
        if tag == "repr":
            return value.get("value")
        return {key: _decode(item) for key, item in value.items()}
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(_encode(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _json_loads(value: str) -> Any:
    return _decode(json.loads(value))


def _frame_schema(frame: pd.DataFrame) -> Dict[str, Any]:
    return {
        "columns": [str(column) for column in frame.columns],
        "dtypes": {str(column): str(frame[column].dtype) for column in frame.columns},
        "index_name": frame.index.name,
    }


def _row_payload(row: Mapping[str, Any]) -> str:
    return _json_dumps(dict(row))


def _frame_from_rows(schema: Dict[str, Any], payloads: Sequence[str]) -> pd.DataFrame:
    columns = list(schema.get("columns", []))
    rows = [_json_loads(payload) for payload in payloads]
    frame = pd.DataFrame(rows, columns=columns, dtype=object)
    for column, dtype in schema.get("dtypes", {}).items():
        if column not in frame:
            continue
        try:
            if dtype != "object":
                frame[column] = frame[column].astype(dtype)
        except (TypeError, ValueError, OverflowError):
            pass
    frame.index.name = schema.get("index_name")
    return frame


def _hash_file(path: Path) -> Tuple[str, int, int, int, int]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    stat = path.stat()
    return digest.hexdigest(), int(stat.st_size), int(stat.st_mtime_ns), int(stat.st_ctime_ns), int(stat.st_ino)


def _stored_state(connection: sqlite3.Connection, resource_key: str) -> Optional[sqlite3.Row]:
    return connection.execute("SELECT * FROM file_state WHERE resource_path = ?", (resource_key,)).fetchone()


def _remember_file(
    connection: sqlite3.Connection, resource_key: str, path: Path,
    imported_state: Optional[Tuple[str, int, int, int, int]] = None,
) -> None:
    if not path.exists() or not path.is_file():
        connection.execute(
            "INSERT INTO file_state(resource_path, content_hash, file_size, mtime_ns, ctime_ns, inode, imported_at) VALUES(?, NULL, NULL, NULL, NULL, NULL, ?) "
            "ON CONFLICT(resource_path) DO UPDATE SET content_hash=NULL, file_size=NULL, mtime_ns=NULL, ctime_ns=NULL, inode=NULL, imported_at=excluded.imported_at",
            (resource_key, _utc_now()),
        )
        return
    digest, size, mtime, ctime, inode = imported_state if imported_state is not None else _hash_file(path)
    connection.execute(
        "INSERT INTO file_state(resource_path, content_hash, file_size, mtime_ns, ctime_ns, inode, imported_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(resource_path) DO UPDATE SET content_hash=excluded.content_hash, file_size=excluded.file_size, mtime_ns=excluded.mtime_ns, ctime_ns=excluded.ctime_ns, inode=excluded.inode, imported_at=excluded.imported_at",
        (resource_key, digest, size, mtime, ctime, inode, _utc_now()),
    )


def _legacy_changed(connection: sqlite3.Connection, resource_key: str, path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    state = _stored_state(connection, resource_key)
    if state is None:
        return True
    stat = path.stat()
    if (
        state["file_size"] == stat.st_size
        and state["mtime_ns"] == stat.st_mtime_ns
        and state["ctime_ns"] == stat.st_ctime_ns
        and state["inode"] == stat.st_ino
    ):
        return False
    digest, _, _, _, _ = _hash_file(path)
    return digest != state["content_hash"]


def _resource_row(connection: sqlite3.Connection, resource_key: str) -> Optional[sqlite3.Row]:
    return connection.execute("SELECT * FROM resources WHERE resource_path = ?", (resource_key,)).fetchone()


def _atomic_export_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp_name = handle.name
            json.dump(_public_json_value(payload), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _public_json_value(value: Any) -> Any:
    """Convert internal values to ordinary JSON types for compatibility files."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if np is not None and isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _public_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public_json_value(item) for item in value]
    if isinstance(value, (str, bool, int, float)):
        return value
    return repr(value)


def _atomic_export_frame(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp_name = handle.name
            frame.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _upsert_resource(connection: sqlite3.Connection, resource_key: str, kind: str, schema: Optional[Dict[str, Any]]) -> int:
    previous = _resource_row(connection, resource_key)
    revision = int(previous["revision"]) + 1 if previous else 1
    connection.execute(
        "INSERT INTO resources(resource_path, kind, revision, schema_json, updated_at) VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(resource_path) DO UPDATE SET kind=excluded.kind, revision=excluded.revision, schema_json=excluded.schema_json, updated_at=excluded.updated_at",
        (resource_key, kind, revision, _json_dumps(schema) if schema is not None else None, _utc_now()),
    )
    return revision


def _sync_search_context(connection: sqlite3.Connection, resource_key: str, payload: Any) -> None:
    if Path(resource_key).name != "search_definition.json" or not isinstance(payload, dict):
        return
    connection.execute(
        "INSERT INTO search_context(singleton, trip_revision, check_in, check_out, currency, adults, children, infants, pets, free_cancellation, total_budget, geometry_json, payload_json, updated_at) "
        "VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET trip_revision=excluded.trip_revision, check_in=excluded.check_in, check_out=excluded.check_out, currency=excluded.currency, adults=excluded.adults, children=excluded.children, infants=excluded.infants, pets=excluded.pets, free_cancellation=excluded.free_cancellation, total_budget=excluded.total_budget, geometry_json=excluded.geometry_json, payload_json=excluded.payload_json, updated_at=excluded.updated_at",
        (
            payload.get("trip_revision"), payload.get("check_in"), payload.get("check_out"), payload.get("currency"),
            payload.get("adults"), payload.get("children"), payload.get("infants"), payload.get("pets"),
            int(str(payload.get("free_cancellation", False)).strip().casefold() in {"true", "1", "yes"}), payload.get("total_budget"),
            _json_dumps(payload.get("geometry")) if payload.get("geometry") is not None else None,
            _json_dumps(payload), _utc_now(),
        ),
    )


def _sync_curation(connection: sqlite3.Connection, resource_key: str, payload: Any) -> None:
    if Path(resource_key).name != "user_curation.json" or not isinstance(payload, dict):
        return
    shortlisted = [str(item) for item in payload.get("shortlisted_room_ids", []) if item is not None]
    hidden = [str(item) for item in payload.get("hidden_room_ids", []) if item is not None]
    order = [str(item) for item in payload.get("manual_order", payload.get("custom_priority", [])) if item is not None]
    connection.execute("DELETE FROM curation_items")
    positions = {room_id: index for index, room_id in enumerate(order)}
    for room_id in dict.fromkeys([*shortlisted, *hidden, *order]):
        curation_row = {
            "room_id": room_id,
            "shortlisted": room_id in shortlisted,
            "hidden": room_id in hidden,
            "manual_order": positions.get(room_id),
        }
        connection.execute(
            "INSERT INTO curation_items(room_id, shortlisted, hidden, manual_order, payload_json, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
            (room_id, int(curation_row["shortlisted"]), int(curation_row["hidden"]), curation_row["manual_order"], _json_dumps(curation_row), _utc_now()),
        )


def _sync_quotes(connection: sqlite3.Connection, resource_key: str, frame: pd.DataFrame) -> None:
    if Path(resource_key).name != "merged_df.csv":
        return
    for row in frame.to_dict(orient="records"):
        _store_quote_row(connection, row, resource_key)


def _is_missing_scalar(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, (dict, list, tuple, set)):
        return False
    try:
        result = pd.isna(value)
        return bool(result) if not hasattr(result, "__len__") else False
    except (TypeError, ValueError):
        return False


def _first_present(*values: Any) -> Any:
    return next((value for value in values if not _is_missing_scalar(value)), None)


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or type(value).__name__ == "bool_" or _is_missing_scalar(value):
        return None
    try:
        number = float(value)
        return int(number) if math.isfinite(number) and number.is_integer() else None
    except (TypeError, ValueError, OverflowError):
        return None


def _quote_key(values: Sequence[Any]) -> str:
    stable = json.dumps(_encode(list(values)), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _store_quote_row(connection: sqlite3.Connection, row: Mapping[str, Any], resource_key: str) -> None:
    room_id = row.get("room_id")
    if _is_missing_scalar(room_id):
        return
    source = str(_first_present(row.get("quote_source"), row.get("source")) or "")
    details_first = source.startswith("details:")
    search_fields = ("search_quote_check_in", "search_quote_check_out", "search_quote_currency")
    detail_fields = ("quote_check_in", "quote_check_out", "quote_currency")
    if details_first:
        check_in, check_out, currency = [
            _first_present(row.get(detail_fields[index]), row.get(search_fields[index])) for index in range(3)
        ]
        party = [
            _first_present(row.get(f"quote_{key}"), row.get(f"search_quote_{key}"), row.get(key))
            for key in ("adults", "children", "infants", "pets")
        ]
        trip_revision = _first_present(row.get("trip_revision_details"), row.get("quote_trip_revision"), row.get("trip_revision"))
    else:
        check_in, check_out, currency = [
            _first_present(row.get(search_fields[index]), row.get(detail_fields[index])) for index in range(3)
        ]
        party = [
            _first_present(row.get(f"search_quote_{key}"), row.get(f"quote_{key}"), row.get(key))
            for key in ("adults", "children", "infants", "pets")
        ]
        trip_revision = _first_present(row.get("trip_revision"), row.get("quote_trip_revision"), row.get("trip_revision_details"))
    revision_value = _optional_int(trip_revision)
    party_values = [_optional_int(value) for value in party]
    currency_value = None if _is_missing_scalar(currency) else str(currency).upper()
    check_in_value = None if _is_missing_scalar(check_in) else str(check_in)
    check_out_value = None if _is_missing_scalar(check_out) else str(check_out)
    key_parts = [str(room_id), revision_value, check_in_value, check_out_value, currency_value, *party_values]
    quote_key = _quote_key(key_parts)
    total = row.get("total_price")
    if isinstance(total, bool) or type(total).__name__ == "bool_" or _is_missing_scalar(total):
        total_value = None
    else:
        try:
            amount = float(total)
            total_value = amount if math.isfinite(amount) and amount > 0 else None
        except (TypeError, ValueError, OverflowError):
            total_value = None
    status_value = _first_present(row.get("quote_status"), row.get("details_quote_status"))
    connection.execute(
        "INSERT INTO quotes(quote_key, room_id, trip_revision, check_in, check_out, currency, adults, children, infants, pets, status, total_price, source, source_frame, payload_json, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(quote_key) DO UPDATE SET status=excluded.status, total_price=excluded.total_price, source=excluded.source, source_frame=excluded.source_frame, payload_json=excluded.payload_json, updated_at=excluded.updated_at",
        (
            quote_key, str(room_id), revision_value, check_in_value, check_out_value, currency_value,
            *party_values, str(status_value) if not _is_missing_scalar(status_value) else None,
            total_value, source or None, resource_key, _row_payload(dict(row)), _utc_now(),
        ),
    )


def _write_frame_db(connection: sqlite3.Connection, resource_key: str, frame: pd.DataFrame) -> int:
    schema = _frame_schema(frame)
    revision = _upsert_resource(connection, resource_key, "frame", schema)
    connection.execute("DELETE FROM frame_rows WHERE resource_path = ?", (resource_key,))
    records = frame.to_dict(orient="records")
    for ordinal, row in enumerate(records):
        room_id = row.get("room_id")
        stored_room_id = None if room_id is None or pd.isna(room_id) else str(room_id)
        connection.execute(
            "INSERT INTO frame_rows(resource_path, ordinal, room_id, row_json) VALUES(?, ?, ?, ?)",
            (resource_key, ordinal, stored_room_id, _row_payload(row)),
        )
    _sync_quotes(connection, resource_key, frame)
    return revision


def _write_json_db(connection: sqlite3.Connection, resource_key: str, payload: Any) -> int:
    revision = _upsert_resource(connection, resource_key, "json", None)
    encoded = _json_dumps(payload)
    connection.execute(
        "INSERT INTO json_documents(resource_path, payload_json) VALUES(?, ?) "
        "ON CONFLICT(resource_path) DO UPDATE SET payload_json=excluded.payload_json",
        (resource_key, encoded),
    )
    decoded = _json_loads(encoded)
    _sync_search_context(connection, resource_key, decoded)
    _sync_curation(connection, resource_key, decoded)
    return revision


def _read_frame_db(connection: sqlite3.Connection, resource_key: str, **kwargs: Any) -> Optional[pd.DataFrame]:
    started_transaction = not connection.in_transaction
    if started_transaction:
        connection.execute("BEGIN")
    try:
        resource = _resource_row(connection, resource_key)
        if resource is None or resource["kind"] != "frame":
            if started_transaction:
                connection.execute("COMMIT")
            return None
        schema = _json_loads(resource["schema_json"]) if resource["schema_json"] else {"columns": [], "dtypes": {}}
        columns = list(schema.get("columns", []))
        payloads = [row["row_json"] for row in connection.execute(
            "SELECT row_json FROM frame_rows WHERE resource_path = ? ORDER BY ordinal", (resource_key,)
        )]
        if started_transaction:
            connection.execute("COMMIT")
    except BaseException:
        if started_transaction and connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    frame = _frame_from_rows(schema, payloads)
    usecols = kwargs.pop("usecols", None)
    columns_arg = kwargs.pop("columns", None)
    nrows = kwargs.pop("nrows", None)
    dtype = kwargs.pop("dtype", None)
    parse_dates = kwargs.pop("parse_dates", None)
    if kwargs:
        unexpected = ", ".join(sorted(kwargs))
        raise TypeError(f"Unsupported read_frame options: {unexpected}")
    selected = columns_arg if columns_arg is not None else usecols
    if selected is not None:
        if callable(selected):
            chosen = [column for column in columns if selected(column)]
        elif isinstance(selected, (str, int)):
            chosen = [selected]
        else:
            chosen = list(selected)
        if chosen and all(isinstance(value, int) for value in chosen):
            chosen = [columns[index] for index in chosen]
        frame = frame.loc[:, [column for column in chosen if column in frame.columns]]
    if nrows is not None:
        frame = frame.head(int(nrows))
    if dtype is not None:
        try:
            if isinstance(dtype, Mapping):
                for column, column_type in dtype.items():
                    if column in frame.columns:
                        frame[column] = frame[column].astype(column_type)
            else:
                frame = frame.astype(dtype)
        except (TypeError, ValueError, OverflowError):
            # Match read_csv's permissive per-column parsing: unaffected columns
            # stay available and callers can apply their own conversion.
            pass
    if parse_dates:
        date_columns = [parse_dates] if isinstance(parse_dates, str) else list(parse_dates) if not isinstance(parse_dates, dict) else list(parse_dates)
        for column in date_columns:
            if column in frame.columns:
                frame[column] = pd.to_datetime(frame[column], errors="coerce")
    return frame


def _read_json_db(connection: sqlite3.Connection, resource_key: str) -> Tuple[bool, Any]:
    row = connection.execute("SELECT payload_json FROM json_documents WHERE resource_path = ?", (resource_key,)).fetchone()
    return (False, None) if row is None else (True, _json_loads(row["payload_json"]))


def _import_legacy(path: Path, workspace: Path, resource_key: str, connection: sqlite3.Connection) -> int:
    suffix = path.suffix.casefold()
    try:
        if suffix == ".csv":
            frame = pd.read_csv(path, dtype={"room_id": str})
            with _transaction(connection):
                revision = _write_frame_db(connection, resource_key, frame)
                _remember_file(connection, resource_key, path)
        elif suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            with _transaction(connection):
                revision = _write_json_db(connection, resource_key, payload)
                _remember_file(connection, resource_key, path)
        else:
            raise StorageFormatError(f"Unsupported legacy resource format at {path}.")
    except StorageFormatError:
        raise
    except Exception as exc:
        raise StorageFormatError(f"Legacy resource is unreadable at {path}: {exc}") from exc
    return revision


def _sync_external(path: Path, workspace: Path, resource_key: str, connection: sqlite3.Connection) -> bool:
    if not path.exists() or not path.is_file():
        return False
    # Most reads are the compatibility export just written by this service.
    # Check its stat baseline without taking SQLite's writer lock first.
    if _resource_row(connection, resource_key) is not None and not _legacy_changed(connection, resource_key, path):
        return False
    connection.execute("BEGIN IMMEDIATE")
    try:
        if _resource_row(connection, resource_key) is not None and not _legacy_changed(connection, resource_key, path):
            connection.execute("COMMIT")
            return False
        before = _hash_file(path)
        if path.suffix.casefold() == ".csv":
            try:
                frame = pd.read_csv(path, dtype={"room_id": str})
            except Exception as exc:
                raise StorageFormatError(f"Legacy CSV is unreadable at {path}: {exc}") from exc
            if before != _hash_file(path):
                raise StorageFormatError(f"Legacy CSV changed during import at {path}; retry the read.")
            _write_frame_db(connection, resource_key, frame)
        elif path.suffix.casefold() == ".json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise StorageFormatError(f"Legacy JSON is unreadable at {path}: {exc}") from exc
            if before != _hash_file(path):
                raise StorageFormatError(f"Legacy JSON changed during import at {path}; retry the read.")
            _write_json_db(connection, resource_key, payload)
        else:
            raise StorageFormatError(f"Unsupported legacy resource format at {path}.")
        _remember_file(connection, resource_key, path, imported_state=before)
        connection.execute("COMMIT")
        return True
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _export_resource(workspace: Path, resource_key: str, connection: sqlite3.Connection) -> None:
    path = workspace / Path(resource_key)
    # Keep export replacement and its baseline under the same writer lock.
    # Otherwise another reader can mistake our own export for an external edit
    # and import it before its baseline is recorded, replacing typed DB values.
    with _transaction(connection):
        resource = _resource_row(connection, resource_key)
        if resource is None:
            return
        if resource["kind"] == "frame":
            frame = _read_frame_db(connection, resource_key)
            if frame is None:
                return
            _atomic_export_frame(path, frame)
        else:
            present, payload = _read_json_db(connection, resource_key)
            if not present:
                return
            _atomic_export_json(path, payload)
        _remember_file(connection, resource_key, path)


def _warn_export_error(path: Path, exc: BaseException) -> None:
    warnings.warn(
        f"SQLite saved {path}, but the CSV/JSON compatibility export failed: {exc}. The database copy is available.",
        StorageExportWarning,
        stacklevel=3,
    )


def read_frame(path: os.PathLike[str] | str, **pandasopts: Any) -> pd.DataFrame:
    """Read a DataFrame from SQLite, lazily importing a legacy CSV if needed."""
    resource_path = _path(path)
    workspace = _workspace_for_resource(resource_path)
    resource_key = _relative_resource(resource_path, workspace)
    connection = _connect(workspace, create=resource_path.exists())
    if connection is None:
        if resource_path.exists():
            connection = _connect(workspace, create=True)
        else:
            return pd.DataFrame()
    try:
        _sync_external(resource_path, workspace, resource_key, connection)
        frame = _read_frame_db(connection, resource_key, **pandasopts)
        return pd.DataFrame() if frame is None else frame
    finally:
        connection.close()


def write_frame(path: os.PathLike[str] | str, frame: pd.DataFrame, *, export: bool = True) -> None:
    """Commit a frame to SQLite and optionally refresh its compatibility CSV."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("write_frame expects a pandas DataFrame.")
    resource_path = _path(path)
    workspace = _workspace_for_resource(resource_path)
    resource_key = _relative_resource(resource_path, workspace)
    connection = _connect(workspace, create=True)
    try:
        # Import any user edit before replacing the canonical resource so its
        # baseline is current and won't overwrite this explicit DB write later.
        _sync_external(resource_path, workspace, resource_key, connection)
        with _transaction(connection):
            _write_frame_db(connection, resource_key, frame.copy())
        if export:
            try:
                _export_resource(workspace, resource_key, connection)
            except Exception as exc:
                _warn_export_error(resource_path, exc)
    finally:
        connection.close()


def read_json(path: os.PathLike[str] | str, default: Any = None) -> Any:
    """Read JSON from SQLite, lazily importing a legacy file if present."""
    resource_path = _path(path)
    workspace = _workspace_for_resource(resource_path)
    resource_key = _relative_resource(resource_path, workspace)
    connection = _connect(workspace, create=resource_path.exists())
    if connection is None:
        return default
    try:
        _sync_external(resource_path, workspace, resource_key, connection)
        present, payload = _read_json_db(connection, resource_key)
        return payload if present else default
    finally:
        connection.close()


def write_json(path: os.PathLike[str] | str, payload: Any, *, export: bool = True) -> None:
    """Commit JSON to SQLite and optionally refresh its compatibility file."""
    resource_path = _path(path)
    workspace = _workspace_for_resource(resource_path)
    resource_key = _relative_resource(resource_path, workspace)
    connection = _connect(workspace, create=True)
    try:
        _sync_external(resource_path, workspace, resource_key, connection)
        with _transaction(connection):
            _write_json_db(connection, resource_key, payload)
        if export:
            try:
                _export_resource(workspace, resource_key, connection)
            except Exception as exc:
                _warn_export_error(resource_path, exc)
    finally:
        connection.close()


def exists(path: os.PathLike[str] | str) -> bool:
    """Return true when either SQLite or the legacy path has the resource."""
    resource_path = _path(path)
    workspace = _workspace_for_resource(resource_path)
    resource_key = _relative_resource(resource_path, workspace)
    connection = _connect(workspace)
    if connection is not None:
        try:
            if _resource_row(connection, resource_key) is not None:
                return True
        finally:
            connection.close()
    return resource_path.is_file()


resource_exists = exists


def signature(path: os.PathLike[str] | str) -> Optional[str]:
    """Return a cheap, non-mutating revision token for cache keys.

    A legacy resource is represented by filesystem metadata until a read imports
    it. Recompute after the first read to use the newly established DB revision.
    """
    resource_path = _path(path)
    workspace = _workspace_for_resource(resource_path)
    resource_key = _relative_resource(resource_path, workspace)
    stat_token = None
    if resource_path.is_file():
        stat = resource_path.stat()
        stat_token = f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_ino}"
    connection = _connect(workspace)
    if connection is None:
        return f"legacy:{resource_key}:{stat_token}" if stat_token is not None else None
    try:
        resource = _resource_row(connection, resource_key)
        if resource is None:
            return f"legacy:{resource_key}:{stat_token}" if stat_token is not None else None
        return f"{resource['kind']}:{resource_key}:{int(resource['revision'])}:{stat_token or 'missing'}"
    finally:
        connection.close()


resource_signature = signature


def migrate_workspace(workspace: os.PathLike[str] | str) -> Dict[str, int]:
    """Idempotently import supported legacy CSV/JSON resources in a workspace."""
    root = _path(workspace)
    connection = _connect(root, create=True)
    imported_frames = imported_json = refreshed = unchanged = 0
    try:
        candidates = []
        for candidate in root.rglob("*"):
            if not candidate.is_file() or candidate.suffix.casefold() not in {".csv", ".json"}:
                continue
            relative = candidate.relative_to(root)
            if DATABASE_FILENAME in relative.parts or "jobs" in relative.parts or any(part.startswith(".") for part in relative.parts):
                continue
            candidates.append(candidate)
        for candidate in sorted(candidates):
            key = candidate.relative_to(root).as_posix()
            existed = _resource_row(connection, key) is not None
            changed = _sync_external(candidate, root, key, connection)
            if not existed and changed:
                if candidate.suffix.casefold() == ".csv":
                    imported_frames += 1
                else:
                    imported_json += 1
            elif changed:
                refreshed += 1
            else:
                unchanged += 1
    finally:
        connection.close()
    return {"imported_frames": imported_frames, "imported_json": imported_json, "refreshed_external": refreshed, "unchanged": unchanged}


def export_workspace(workspace: os.PathLike[str] | str, targetdir: os.PathLike[str] | str) -> List[Path]:
    """Export every stored CSV/JSON resource to a portable directory."""
    root = _path(workspace)
    destination = _path(targetdir)
    connection = _connect(root)
    if connection is None:
        return []
    try:
        resources = connection.execute("SELECT resource_path, kind FROM resources ORDER BY resource_path").fetchall()
        exported: List[Path] = []
        for resource in resources:
            key = resource["resource_path"]
            target = destination / Path(key)
            try:
                if resource["kind"] == "frame":
                    frame = _read_frame_db(connection, key)
                    if frame is None:
                        continue
                    _atomic_export_frame(target, frame)
                else:
                    present, payload = _read_json_db(connection, key)
                    if not present:
                        continue
                    _atomic_export_json(target, payload)
                exported.append(target)
            except Exception as exc:
                _warn_export_error(target, exc)
        return exported
    finally:
        connection.close()


def save_shortlist_snapshot(
    workspace: os.PathLike[str] | str,
    frame: pd.DataFrame,
    room_ids: Sequence[Any],
    *,
    name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Save an immutable, ordered shortlist snapshot with copied row payloads."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame.")
    root = _path(workspace)
    connection = _connect(root, create=True)
    snapshot_id = uuid.uuid4().hex
    ordered_ids = list(dict.fromkeys(str(value).strip() for value in room_ids if value is not None and str(value).strip()))
    indexed = frame.copy()
    if "room_id" not in indexed.columns:
        indexed["room_id"] = indexed.index.astype(str)
    indexed["room_id"] = indexed["room_id"].astype(str)
    by_id = {str(row.get("room_id")): row for row in indexed.to_dict(orient="records")}
    missing_ids = [room_id for room_id in ordered_ids if room_id not in by_id]
    if missing_ids:
        connection.close()
        raise ValueError("Cannot snapshot room IDs absent from the supplied frame: " + ", ".join(missing_ids[:10]))
    schema = _frame_schema(indexed)
    try:
        with _transaction(connection):
            connection.execute(
                "INSERT INTO shortlist_snapshots(snapshot_id, name, created_at, metadata_json, frame_schema_json, row_count) VALUES(?, ?, ?, ?, ?, ?)",
                (snapshot_id, name, _utc_now(), _json_dumps(metadata or {}), _json_dumps(schema), len(ordered_ids)),
            )
            ordinal = 0
            for room_id in ordered_ids:
                row = by_id[room_id]
                connection.execute(
                    "INSERT INTO shortlist_snapshot_rows(snapshot_id, ordinal, room_id, row_json) VALUES(?, ?, ?, ?)",
                    (snapshot_id, ordinal, room_id, _row_payload(row)),
                )
                ordinal += 1
        return snapshot_id
    finally:
        connection.close()


def load_shortlist_snapshot(workspace: os.PathLike[str] | str, snapshot_id: str) -> Optional[Dict[str, Any]]:
    """Load a saved shortlist snapshot without consulting mutable current rows."""
    connection = _connect(workspace)
    if connection is None:
        return None
    try:
        snapshot = connection.execute("SELECT * FROM shortlist_snapshots WHERE snapshot_id = ?", (str(snapshot_id),)).fetchone()
        if snapshot is None:
            return None
        rows = connection.execute(
            "SELECT room_id, row_json FROM shortlist_snapshot_rows WHERE snapshot_id = ? ORDER BY ordinal",
            (str(snapshot_id),),
        ).fetchall()
        schema = _json_loads(snapshot["frame_schema_json"])
        payloads = [row["row_json"] for row in rows]
        frame = _frame_from_rows(schema, payloads)
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "name": snapshot["name"],
            "created_at": snapshot["created_at"],
            "metadata": _json_loads(snapshot["metadata_json"]),
            "room_ids": [row["room_id"] for row in rows],
            "frame": frame,
        }
    finally:
        connection.close()

