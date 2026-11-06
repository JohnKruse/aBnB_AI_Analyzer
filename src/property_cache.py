"""Shared 30-day Airbnb evidence cache, independent of search-owned databases."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import math
import numbers
from pathlib import Path
import sqlite3
import threading

import pandas as pd

from .processing_service import recent

_LOCK = threading.RLock()
_FIELDS = ('check_in', 'check_out', 'adults', 'children', 'infants', 'pets', 'currency')


def _context(definition):
    return json.dumps({k: str(definition.get(k, 0 if k in _FIELDS[2:6] else '')).upper()
                       for k in _FIELDS}, sort_keys=True)


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat() if stamp <= datetime.now(timezone.utc) else None
    except (ValueError, TypeError):
        return None


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k.lower() not in {
            'api_key', 'access_token', 'token', 'cookies', 'cookie', 'authorization', 'headers'}}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, numbers.Real) and not math.isfinite(value):
        return None
    return value


def _metadata(row):
    return {k: v for k, v in row.items() if k not in {
        'price', 'total_price', 'availability_state', 'trip_revision', 'calendar',
        'reviews', 'reviews_json', 'reviews_text', 'host_details', 'policies', 'free_cancellation',
    } and not k.startswith(('quote_', 'search_quote', 'selection_quote', 'personal_fit'))
            and not any(word in k.lower() for word in ('cancellation', 'refund', 'availability', 'booking'))}


class PropertyCache:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / '.property_cache.sqlite3'
        self.root.mkdir(parents=True, exist_ok=True)
        with _LOCK, closing(self._connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS evidence (kind TEXT, room_id TEXT, context TEXT, fetched TEXT, payload TEXT, PRIMARY KEY(kind, room_id, context))')
            db.execute('CREATE TABLE IF NOT EXISTS imports (workspace TEXT PRIMARY KEY, signature TEXT)')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def store(self, details=(), reviews=(), definition=None):
        from .airbnb_service import _detail_quote_status
        records = []
        for raw in details:
            row = _clean(raw)
            rid = str(row.get('room_id') or '')
            if not rid:
                continue
            stamp = _stamp(row.get('property_details_fetched_at') or row.get('details_fetched_at'))
            if stamp:
                meta = _metadata(row)
                meta['property_details_fetched_at'] = stamp
                records.append(('metadata', rid, '', str(stamp), json.dumps(meta, allow_nan=False)))
            if definition and _stamp(row.get('details_fetched_at')) and _detail_quote_status(row, definition) == 'current':
                records.append(('quote', rid, _context(definition), _stamp(row['details_fetched_at']), json.dumps(row, allow_nan=False)))
        for raw in reviews:
            row = _clean(raw)
            if row.get('room_id') is not None and _stamp(row.get('reviews_fetched_at')):
                records.append(('reviews', str(row['room_id']), str(row.get('reviews_policy') or 'legacy'),
                                _stamp(row['reviews_fetched_at']), json.dumps(row, allow_nan=False)))
        with _LOCK, closing(self._connect()) as db, db:
            db.executemany('INSERT INTO evidence VALUES (?,?,?,?,?) ON CONFLICT(kind,room_id,context) DO UPDATE SET fetched=excluded.fetched,payload=excluded.payload WHERE excluded.fetched >= evidence.fetched', records)

    def load(self, room_ids, definition, options=None):
        from .review_collection import cached_reviews_usable
        found = {}
        with _LOCK, closing(self._connect()) as db:
            for rid in room_ids:
                item = {}
                for kind, context, stamp, payload in db.execute('SELECT kind,context,fetched,payload FROM evidence WHERE room_id=? ORDER BY fetched DESC', (str(rid),)):
                    if not recent(stamp):
                        continue
                    row = json.loads(payload)
                    if kind == 'metadata':
                        item.setdefault('metadata', row)
                    elif kind == 'quote' and context == _context(definition) and recent(row.get('property_details_fetched_at') or row.get('details_fetched_at')):
                        row['trip_revision'] = definition['trip_revision']
                        row['shared_cache_reused'] = True
                        item.setdefault('details', row)
                    elif kind == 'reviews' and (not options or cached_reviews_usable(row, options)):
                        row['shared_cache_reused'] = True
                        item.setdefault('reviews', row)
                if item:
                    found[str(rid)] = item
        return found

    def import_workspace(self, workspace):
        """Backfill old search evidence, including before permanent deletion."""
        from . import storage_service as storage
        from .search_service import load_search_definition
        workspace = Path(workspace)
        if workspace.is_symlink() or not workspace.is_dir():
            return
        try:
            definition = load_search_definition(workspace.name, root=workspace.parent)
        except (OSError, ValueError):
            definition = None
        frames = []
        for filename in ('details_df.csv', 'reviews_df.csv'):
            path = workspace / 'output_data' / filename
            frame = storage.read_frame(path, dtype={'room_id':str}) if storage.exists(path) else pd.DataFrame()
            frames.append(json.loads(frame.to_json(orient='records', date_format='iso')))
        self.store(*frames, definition=definition)

    def seed_existing(self):
        """Import changed workspaces once, without touching another active writer."""
        for workspace in self.root.iterdir():
            if workspace.name.startswith('.') or workspace.is_symlink() or not workspace.is_dir():
                continue
            files = [workspace/'analyzer.sqlite3', workspace/'analyzer.sqlite3-wal',
                     workspace/'output_data/details_df.csv', workspace/'output_data/reviews_df.csv']
            signature = json.dumps([(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files if p.exists()])
            with _LOCK, closing(self._connect()) as db:
                saved = db.execute('SELECT signature FROM imports WHERE workspace=?', (workspace.name,)).fetchone()
            if saved and saved[0] == signature:
                continue
            try:
                self.import_workspace(workspace)
            except (OSError, ValueError, sqlite3.DatabaseError):
                continue  # One damaged legacy search must not block collection.
            with _LOCK, closing(self._connect()) as db, db:
                db.execute('INSERT OR REPLACE INTO imports VALUES (?,?)', (workspace.name, signature))
