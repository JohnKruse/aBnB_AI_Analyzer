"""Collect bounded newest-first reviews, checkpointing each provider page."""
from __future__ import annotations

import hashlib
import json
from datetime import date

from . import storage_service as storage


def review_date(review):
    raw = review.get('created_at', review.get('createdAt', review.get('date')))
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def policy_key(options):
    return json.dumps({key: options[key] for key in ('review_cutoff', 'max_reviews')}, sort_keys=True)


def select_reviews(records, options):
    cutoff = date.fromisoformat(options['review_cutoff']) if options.get('review_cutoff') else None
    unique = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        stamp = review_date(record)
        if cutoff and stamp and stamp < cutoff:
            continue
        identity = str(record.get('id', record.get('review_id')) or json.dumps(record, sort_keys=True, default=str))
        unique.setdefault(identity, record)
    ordered = sorted(unique.values(), key=lambda record: review_date(record) or date.min, reverse=True)
    return ordered[:options['max_reviews']]


def cached_reviews_usable(row, options):
    """A moving cutoff must not trigger a daily redownload inside the 30-day cache."""
    from .processing_service import recent
    if not recent(row.get('reviews_fetched_at')):
        return False
    try:
        raw_policy = row.get('reviews_policy')
        if not isinstance(raw_policy, str) or not raw_policy.strip():
            return True  # Fresh legacy snapshots can be sampled locally.
        old = json.loads(raw_policy)
        cap = int(old.get('max_reviews', row.get('reviews_limit') or 0))
        if cap < options['max_reviews']:
            return False
        old_cutoff = old.get('review_cutoff')
        new_cutoff = options.get('review_cutoff')
        return not old_cutoff or (new_cutoff and old_cutoff <= new_cutoff) or row.get('reviews_stop_reason') == 'review_cap'
    except (ValueError, TypeError):
        # Legacy reviews have no sampling metadata. Reuse them if enough
        # original records are present to derive the requested sample locally.
        return False


def collect_recent_reviews(collector, url, directory, room_id, options, policy, *, bootstrap_context=None):
    """Use pyairbnb's MOST_RECENT page endpoint instead of its unbounded wrapper."""
    page_api = getattr(getattr(collector, 'reviews', None), 'get_from_offset', None)
    bootstrap = getattr(getattr(collector, 'details', None), 'get', None)
    if not callable(page_api) or not callable(bootstrap):
        raw = policy.call(lambda: collector.get_reviews(room_url=url, language='en'), interval=3., operation='review_page')
        if isinstance(raw, dict):
            raw = raw.get('reviews', raw.get('data', raw.get('results', [])))
        if not isinstance(raw, list):
            raise ValueError('Reviews provider returned an invalid collection.')
        return select_reviews(raw, options), {'reviews_page_limited': False}
    checkpoint = directory / 'review_pages' / (hashlib.sha256(str(room_id).encode()).hexdigest()+'.json')
    saved = storage.read_json(checkpoint, default={}) or {}
    signature = policy_key(options)
    from .processing_service import recent
    if saved.get('policy') != signature or not recent(saved.get('saved_at')):
        saved = {'policy': signature, 'offset': 0, 'records': [], 'pages': 0, 'done': False}
    if saved['done']:
        return saved['records'], {'reviews_page_limited': True, 'reviews_pages': saved['pages'], 'reviews_stop_reason': saved.get('stop_reason')}
    _, inputs, _ = bootstrap_context if bootstrap_context is not None else policy.call(lambda: bootstrap(url, 'en', ''), operation='review_bootstrap')
    cutoff = date.fromisoformat(options['review_cutoff']) if options.get('review_cutoff') else None
    # Also bound pages when dates/IDs are malformed or the endpoint repeats data.
    max_pages = (options['max_reviews']+49)//50+1
    while not saved['done']:
        page = policy.call(lambda: page_api(inputs['api_key'], saved['offset'], inputs['product_id'], language='en'), interval=3., operation='review_page')
        if not isinstance(page, list):
            raise ValueError('Review page was not a list; checkpoint retained for retry.')
        dates = [review_date(record) for record in page]
        saved['records'] = select_reviews(saved['records']+page, options)
        saved['offset'] += 50
        saved['pages'] += 1
        reason = ('end' if not page else
                  'review_cap' if len(saved['records']) >= options['max_reviews'] else
                  'date_cutoff' if cutoff and dates and all(stamp and stamp < cutoff for stamp in dates) else
                  'page_cap' if saved['pages'] >= max_pages else None)
        from datetime import datetime, timezone
        saved['saved_at'] = datetime.now(timezone.utc).isoformat()
        saved['done'] = reason is not None
        saved['stop_reason'] = reason
        storage.write_json(checkpoint, saved)
    return saved['records'], {'reviews_page_limited': True, 'reviews_pages': saved['pages'], 'reviews_stop_reason': saved['stop_reason']}
