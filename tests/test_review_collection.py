from types import SimpleNamespace
import pytest
from src.review_collection import collect_recent_reviews, select_reviews


class Policy:
    options = {'review_cutoff': '2025-01-01', 'max_reviews': 100, 'max_attempts': 3}
    def call(self, fn, **kwargs):
        return fn()


def review(rid, stamp):
    return {'id': rid, 'createdAt': stamp, 'comments': 'Quiet.'}


def test_review_pages_stop_at_cutoff_and_reuse_checkpoint(tmp_path):
    offsets = []
    def page(api_key, offset, product_id, **kwargs):
        offsets.append(offset)
        return [review('new', '2026-09-01')] if offset == 0 else [review('old', '2020-01-01')]
    collector = SimpleNamespace(
        details=SimpleNamespace(get=lambda *args: ({}, {'api_key': 'private', 'product_id': 'one'}, {})),
        reviews=SimpleNamespace(get_from_offset=page))
    records, meta = collect_recent_reviews(collector, 'url', tmp_path, 'one', Policy.options, Policy())
    assert [r['id'] for r in records] == ['new']
    assert offsets == [0, 50]
    assert meta['reviews_stop_reason'] == 'date_cutoff'
    again, _ = collect_recent_reviews(collector, 'url', tmp_path, 'one', Policy.options, Policy())
    assert again == records and offsets == [0, 50]


def test_failed_review_page_resumes_at_saved_offset(tmp_path):
    offsets = []
    failing = [True]
    def page(key, offset, product_id, **kwargs):
        offsets.append(offset)
        if offset == 50 and failing[0]:
            raise TimeoutError('page failed')
        return [review('new', '2026-09-01')] if offset == 0 else []
    collector = SimpleNamespace(
        details=SimpleNamespace(get=lambda *args: ({}, {'api_key': 'private', 'product_id': 'one'}, {})),
        reviews=SimpleNamespace(get_from_offset=page))
    with pytest.raises(TimeoutError):
        collect_recent_reviews(collector, 'url', tmp_path, 'one', Policy.options, Policy())
    failing[0] = False
    records, _ = collect_recent_reviews(collector, 'url', tmp_path, 'one', Policy.options, Policy())
    assert offsets == [0, 50, 50]
    assert len(records) == 1


def test_filter_keeps_undated_reviews_and_deduplicates():
    records = select_reviews([review('old', '2020-01-01'), review('new', '2026-01-01'),
                              review('new', '2026-01-01'), review('unknown', None)], Policy.options)
    assert [r['id'] for r in records] == ['new', 'unknown']
