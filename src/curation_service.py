"""Persistent, search-scoped user curation.

SQLite stores the canonical curation resource; user_curation.json remains a
compatibility export for existing workspaces and command-line tools. The
operational cross-process lock remains a separate filesystem file.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback retains in-process locking.
    fcntl = None

from .search_service import validate_existing_search_name
from . import storage_service

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 2
_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class CurationCorruptError(ValueError):
    """Raised when saved user curation exists but cannot be safely read."""


def _validate_search_name(search_name: str) -> str:
    return validate_existing_search_name(search_name)


def get_curation_file(search_name: str) -> Path:
    """Return the curation path after constraining it to the searches directory."""
    name = _validate_search_name(search_name)
    searches_root = (PROJECT_ROOT / "searches").resolve()
    search_dir = (searches_root / name).resolve()
    if search_dir.parent != searches_root:
        raise ValueError("Search path escapes the searches directory.")
    search_dir.mkdir(parents=True, exist_ok=True)
    return search_dir / "user_curation.json"


def _unique_ids(values: Iterable[Any]) -> List[str]:
    if values is None or isinstance(values, (str, bytes)):
        values = [] if values is None else [values]
    result: List[str] = []
    seen = set()
    for value in values:
        if value is None:
            continue
        room_id = str(value).strip()
        if room_id and room_id not in seen:
            seen.add(room_id)
            result.append(room_id)
    return result


def _normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate the original custom_priority field to the full manual order."""
    order = data.get("manual_order", data.get("custom_priority", []))
    return {
        "schema_version": SCHEMA_VERSION,
        "hidden_room_ids": _unique_ids(data.get("hidden_room_ids", [])),
        "shortlisted_room_ids": _unique_ids(data.get("shortlisted_room_ids", [])),
        "manual_order": _unique_ids(order),
        # Keep the original key for older app versions and user scripts.
        "custom_priority": _unique_ids(order),
    }


@contextmanager
def _curation_lock(search_name: str):
    path = get_curation_file(search_name)
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        lock_path = path.with_suffix(".lock")
        with lock_path.open("a", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield path
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_user_curation(search_name: str) -> Dict[str, Any]:
    """Load curation state, raising a clear error instead of hiding corruption."""
    with _curation_lock(search_name) as path:
        return _read_curation_unlocked(search_name, path)


def save_user_curation(search_name: str, curation_data: Dict[str, Any]) -> None:
    """Atomically persist normalized curation state."""
    with _curation_lock(search_name) as path:
        _write_curation(path, curation_data)


def _write_curation(path: Path, curation_data: Dict[str, Any]) -> None:
    """Persist normalized state through SQLite and refresh its JSON export."""
    cleaned = _normalize(curation_data)
    storage_service.write_json(path, cleaned)


def _read_curation_unlocked(search_name: str, path: Path) -> Dict[str, Any]:
    try:
        if not storage_service.exists(path):
            return _normalize({})
        data = storage_service.read_json(path)
        if not isinstance(data, dict):
            raise ValueError("root value must be a JSON object")
        return _normalize(data)
    except (OSError, ValueError, TypeError) as exc:
        raise CurationCorruptError(
            f"Saved curation for '{search_name}' is unreadable at {path}: {exc}"
        ) from exc


def dismiss_property(search_name: str, room_id: str) -> None:
    with _curation_lock(search_name) as path:
        curation = _read_curation_unlocked(search_name, path)
        rid = str(room_id)
        curation["hidden_room_ids"] = _unique_ids([*curation["hidden_room_ids"], rid])
        _write_curation(path, curation)


def restore_property(search_name: str, room_id: str) -> None:
    with _curation_lock(search_name) as path:
        curation = _read_curation_unlocked(search_name, path)
        rid = str(room_id)
        curation["hidden_room_ids"] = [x for x in curation["hidden_room_ids"] if x != rid]
        _write_curation(path, curation)


def restore_all_properties(search_name: str) -> None:
    with _curation_lock(search_name) as path:
        curation = _read_curation_unlocked(search_name, path)
        curation["hidden_room_ids"] = []
        _write_curation(path, curation)


def toggle_shortlist_property(search_name: str, room_id: str) -> bool:
    with _curation_lock(search_name) as path:
        curation = _read_curation_unlocked(search_name, path)
        rid = str(room_id)
        if rid in curation["shortlisted_room_ids"]:
            curation["shortlisted_room_ids"].remove(rid)
            shortlisted = False
        else:
            curation["shortlisted_room_ids"].append(rid)
            shortlisted = True
        _write_curation(path, curation)
        return shortlisted


def seed_manual_order(search_name: str, ordered_room_ids: Sequence[Any]) -> List[str]:
    """Seed the persisted full order from the current automatic ordering once."""
    with _curation_lock(search_name) as path:
        curation = _read_curation_unlocked(search_name, path)
        existing = curation["manual_order"]
        if not existing:
            curation["manual_order"] = _unique_ids(ordered_room_ids)
            _write_curation(path, curation)
        return curation["manual_order"]


def ensure_manual_order(
    search_name: str,
    all_current_ids: Sequence[Any],
    preferred_order: Sequence[Any] | None = None,
) -> List[str]:
    """Atomically preserve saved order and append newly discovered IDs.

    The persisted list remains the full search history so a temporary discovery
    omission does not erase a user's placement. The return value is projected
    onto the current IDs for immediate display.
    """
    current = _unique_ids(all_current_ids)
    with _curation_lock(search_name) as path:
        curation = _read_curation_unlocked(search_name, path)
        saved = _unique_ids(curation["manual_order"])
        if not saved:
            saved = _unique_ids(preferred_order) if preferred_order is not None else []
        seen = set(saved)
        for room_id in current:
            if room_id not in seen:
                saved.append(room_id)
                seen.add(room_id)
        if saved != curation["manual_order"]:
            curation["manual_order"] = saved
            _write_curation(path, curation)
        return extend_manual_order(current, saved)


def extend_manual_order(
    room_ids: Sequence[Any],
    existing_order: Sequence[Any],
) -> List[str]:
    """Project a saved order onto current IDs and append new IDs in input order."""
    available = _unique_ids(room_ids)
    available_set = set(available)
    order = [rid for rid in _unique_ids(existing_order) if rid in available_set]
    included = set(order)
    order.extend(rid for rid in available if rid not in included)
    return order


def reorder_visible_properties(
    search_name: str,
    reordered_visible_ids: Sequence[Any],
    all_current_ids: Sequence[Any] | None = None,
) -> List[str]:
    """Reorder shown IDs in their existing full-order slots.

    IDs outside the dragged visible set retain their relative order, including
    listings omitted by filters or the Top-N limit.
    """
    with _curation_lock(search_name) as path:
        curation = _read_curation_unlocked(search_name, path)
        visible_order = _unique_ids(reordered_visible_ids)
        if all_current_ids is None:
            all_current_ids = [*curation["manual_order"], *visible_order]
        if curation["manual_order"]:
            full_order = _unique_ids(curation["manual_order"])
            included = set(full_order)
            full_order.extend(rid for rid in _unique_ids(all_current_ids) if rid not in included)
        else:
            full_order = _unique_ids(all_current_ids)
        visible_set = set(visible_order)
        if not visible_set.issubset(set(full_order)):
            raise ValueError("Dragged IDs must belong to the current search order.")
        iterator = iter(visible_order)
        new_order = [next(iterator) if rid in visible_set else rid for rid in full_order]
        curation["manual_order"] = new_order
        _write_curation(path, curation)
        return new_order


def move_property_priority(
    search_name: str,
    room_id: str,
    direction: str,
    visible_room_ids: Sequence[Any] | None = None,
) -> None:
    """Move a listing within the current visible set (keyboard-accessible drag alternative)."""
    if direction not in {"up", "down"}:
        raise ValueError("direction must be 'up' or 'down'")
    with _curation_lock(search_name) as path:
        curation = _read_curation_unlocked(search_name, path)
        rid = str(room_id)
        if visible_room_ids is None:
            order = list(curation["manual_order"])
            if rid not in order:
                order.append(rid)
            visible = order
        else:
            visible = _unique_ids(visible_room_ids)
            order = extend_manual_order(
                [*curation["manual_order"], *visible],
                curation["manual_order"] if curation["manual_order"] else visible,
            )

        if rid not in visible:
            raise ValueError(f"Room {rid} is not in the visible set.")
        visible_index = visible.index(rid)
        target_index = visible_index - 1 if direction == "up" else visible_index + 1
        if target_index < 0 or target_index >= len(visible):
            return
        neighbor = visible[target_index]
        positions = {value: idx for idx, value in enumerate(order)}
        first, second = positions[rid], positions[neighbor]
        order[first], order[second] = order[second], order[first]
        curation["manual_order"] = order
        _write_curation(path, curation)

