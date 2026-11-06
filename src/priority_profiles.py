"""Reusable priority profiles, separate from per-search evaluation snapshots."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.RLock()


def _path(root):
    path = Path(root) / 'searches' / '.priority_profiles.json'
    if path.is_symlink():
        raise ValueError('The priority profile library cannot be a symbolic link.')
    return path


def _load(root):
    from . import evaluation_service as evaluation
    path = _path(root)
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('profiles'), dict):
            raise ValueError('The saved priority profile library is malformed.')
        return data
    # Surface useful profiles saved by older versions; do not import the
    # automatically generated all-3/neutral Default as a personal preference.
    profiles = {}
    searches = path.parent
    if searches.exists():
        for workspace in sorted(searches.iterdir()):
            if not workspace.is_dir() or workspace.is_symlink() or workspace.name.startswith('.'):
                continue
            local = workspace / 'evaluation_profiles.json'
            try:
                if not evaluation._storage_exists(local):
                    continue
                data = evaluation._storage_read_json(local)
                for name, value in data.get('profiles', {}).items():
                    profile = evaluation._normalize_profile(name, value)
                    if name == 'Default' and not profile['additional_instructions'] and all(weight in (0, 3) for weight in profile['weights'].values()):
                        continue
                    stamp = value.get('created_at') or value.get('updated_at') or datetime.fromtimestamp(local.stat().st_mtime, timezone.utc).isoformat()
                    profile.update(created_at=stamp, updated_at=value.get('updated_at', stamp))
                    if name not in profiles or profile['updated_at'] > profiles[name]['updated_at']:
                        profiles[name] = profile
            except (OSError, ValueError, TypeError, KeyError):
                continue
    data = {'schema_version': 1, 'profiles': profiles}
    if profiles:
        _write(root, data)  # Preserve migrated preferences independently of search deletion.
    return data


def _write(root, data):
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.priority-profile-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def list_profiles(root):
    with _LOCK:
        profiles = _load(root)['profiles']
        def created_order(value):
            try:
                stamp = datetime.fromisoformat(str(value.get('created_at', '')).replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                return stamp.timestamp(), value['name']
            except (ValueError, TypeError):
                return 0., value['name']
        return sorted(profiles.values(), key=created_order, reverse=True)


def load_profile(root, name):
    from . import evaluation_service as evaluation
    with _LOCK:
        value = _load(root)['profiles'].get(name)
        if value is None:
            raise KeyError(f"Priority profile '{name}' does not exist.")
        profile = evaluation._normalize_profile(name, value)
        profile['created_at'] = value.get('created_at')
        return profile


def save_profile(root, name, weights, instructions='', *, model_name=None, reasoning_effort=None):
    from . import evaluation_service as evaluation
    name = str(name).strip()
    if not evaluation._PROFILE_NAME.fullmatch(name):
        raise ValueError('Give the profile a name of 1–80 characters without path separators.')
    with _LOCK:
        data = _load(root)
        existing = data['profiles'].get(name, {})
        values = {**existing, 'weights': weights, 'additional_instructions': instructions}
        if model_name:
            values['model_name'] = model_name
        if reasoning_effort is not None:
            values['reasoning_effort'] = reasoning_effort
        profile = evaluation._normalize_profile(name, values)
        now = datetime.now(timezone.utc).isoformat()
        profile.update(created_at=existing.get('created_at') or now, updated_at=now,
                       revision=int(existing.get('revision', 0))+1)
        data['profiles'][name] = profile
        _write(root, data)
        return profile


def delete_profile(root, name):
    with _LOCK:
        data = _load(root)
        if name not in data['profiles']:
            raise KeyError(f"Priority profile '{name}' does not exist.")
        del data['profiles'][name]
        _write(root, data)
