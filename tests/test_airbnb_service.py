"""Fake-provider tests for collection, geometry boundaries and quote context."""

from datetime import date, timedelta
import json
import threading

import pandas as pd
import pytest

import src.airbnb_service as airbnb_service
import src.search_service as search_service


class FakeProvider:
    def __init__(self, results=None, cancel_event=None, include_reviews_in_details=False):
        self.results = results if results is not None else []
        self.cancel_event = cancel_event
        self.include_reviews_in_details = include_reviews_in_details
        self.search_calls = []
        self.detail_calls = []
        self.review_calls = []

    def search_all(self, **kwargs):
        self.search_calls.append(kwargs)
        return self.results

    def get_details(self, room_url, check_in, check_out, adults, currency):
        self.detail_calls.append({"room_url": room_url, "check_in": check_in, "check_out": check_out, "adults": adults, "currency": currency})
        if self.cancel_event:
            self.cancel_event.set()
        room_id = room_url.rsplit("/", 1)[-1]
        payload = {"room_id": room_id, "name": f"Listing {room_id}", "total_price": 450, "price": 100}
        if self.include_reviews_in_details:
            payload["reviews"] = [{"id": f"detail-review-{room_id}", "comments": "Review included with details."}]
        return payload

    def get_reviews(self, room_url, language="en"):
        self.review_calls.append(room_url)
        room_id = room_url.rsplit("/", 1)[-1]
        return [{"id": f"review-{room_id}", "created_at": "2026-09-01", "comments": "Quiet and clean."}]


def setup_search(tmp_path, monkeypatch, *, children=1, pets=0, free_cancellation=False):
    root = tmp_path
    monkeypatch.setattr(airbnb_service, "PROJECT_ROOT", root)
    searches = root / "searches"
    start = date.today() + timedelta(days=30)
    geometry = {
        "type": "Polygon",
        "coordinates": [[[7.6, 45.0], [7.8, 45.0], [7.8, 45.2], [7.6, 45.2], [7.6, 45.0]]],
    }
    search_service.save_search_definition(
        "trip", {
            "location": "Turin",
            "check_in": start.isoformat(),
            "check_out": (start + timedelta(days=4)).isoformat(),
            "adults": 2,
            "children": children,
            "infants": 0,
            "pets": pets,
            "free_cancellation": free_cancellation,
            "currency": "EUR",
            "geometry": geometry,
        },
        create=True,
        root=searches,
    )


def enable_free_cancellation_filter(monkeypatch):
    load = airbnb_service.load_search_definition

    def load_with_filter(search_name, *, root=None):
        definition = load(search_name, root=root)
        if definition:
            definition["free_cancellation"] = True
        return definition

    monkeypatch.setattr(airbnb_service, "load_search_definition", load_with_filter)


def test_discovery_passes_party_deduplicates_and_keeps_unknown_locations(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch)
    provider = FakeProvider([
        {
            "id": 10, "name": "inside", "latitude": 45.1, "longitude": 7.7,
            "rating": {"value": 4.8},
            "price": {"unit": {"amount": 100}, "total": {"amount": 480}, "break_down": [{"description": "Total", "amount": 480}]},
        },
        {"room_id": "10", "name": "duplicate", "latitude": 45.1, "longitude": 7.7},
        {"room_id": "11", "name": "boundary", "latitude": 45.0, "longitude": 7.7},
        {"room_id": "12", "name": "location unknown"},
        {"room_id": "13", "name": "outside", "latitude": 45.3, "longitude": 7.7},
    ])
    result = airbnb_service.discover_listings("trip", provider=provider)
    saved = pd.read_csv(tmp_path / "searches" / "trip" / "output_data" / "results_df.csv", dtype={"room_id": str})

    assert result["discovered"] == 3
    assert result["location_unknown"] == 1
    assert set(saved["room_id"]) == {"10", "11", "12"}
    assert provider.search_calls[0]["adults"] == 2
    assert provider.search_calls[0]["children"] == 1
    assert provider.search_calls[0]["currency"] == "EUR"
    assert saved.loc[saved.room_id == "10", "total_price"].iloc[0] == 480
    merged = pd.read_csv(tmp_path / "searches" / "trip" / "output_data" / "merged_df.csv", dtype={"room_id": str})
    assert len(merged) == 3
    assert merged.loc[merged.room_id == "10", "quote_status"].iloc[0] == "current"
    assert merged.loc[merged.room_id == "12", "quote_status"].iloc[0] == "missing"


def test_discovery_normalizes_real_pyairbnb_longitud_payload(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)
    provider = FakeProvider([{
        "room_id": "putney-1",
        "coordinates": {"latitude": 45.1, "longitud": 7.7},
    }])

    result = airbnb_service.discover_listings("trip", provider=provider)

    saved = pd.read_csv(
        tmp_path / "searches" / "trip" / "output_data" / "results_df.csv",
        dtype={"room_id": str},
    )
    assert result["location_unknown"] == 0
    assert saved.loc[0, "location_state"] == "known"
    assert saved.loc[0, "latitude"] == pytest.approx(45.1)
    assert saved.loc[0, "longitude"] == pytest.approx(7.7)


def test_nightly_unit_price_is_never_used_as_total_stay_price():
    total, source, completeness = airbnb_service._explicit_price_total({"unit": {"amount": 100}, "break_down": []})
    assert total is None
    assert source is None
    assert completeness is None

    nightly = {"main": {"price": "€100", "qualifier": "per night", "details": {"Nightly price": "€100"}}}
    assert airbnb_service._explicit_price_total(nightly)[0] is None


def test_pyairbnb_details_total_uses_explicit_total_label_only():
    payload = {
        "raw": [],
        "main": {
            "price": "€100",
            "qualifier": "per night",
            "details": {"Nightly price": "€100", "Total": "€1.250,00"},
        },
    }
    total, source, completeness = airbnb_service._explicit_price_total(payload)
    assert total == 1250
    assert source == "price.main.details.Total"
    assert "total" in completeness


@pytest.mark.parametrize("value", [0, 0.0, -1, True, float("nan"), float("inf"), float("-inf"), "€0", "-€10,00", "NaN", "Infinity"])
def test_stay_total_parser_rejects_nonpositive_nonfinite_and_boolean_values(value):
    assert airbnb_service._parse_amount(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("€1.250,00", 1250.0), ("€1,25", 1.25), ("$1,250.50", 1250.50), (1250, 1250.0)],
)
def test_stay_total_parser_handles_localized_positive_amounts(value, expected):
    assert airbnb_service._parse_amount(value) == pytest.approx(expected)


def _saved_quote_row(definition, *, detail_total=0, search_total=None):
    return {
        "room_id": "saved-quote",
        "trip_revision": definition["trip_revision"],
        "trip_revision_details": definition["trip_revision"],
        "total_price": detail_total,
        "total_price_details": detail_total,
        "price_details": {
            "raw": [],
            "main": {
                "price": None,
                "qualifier": "total",
                "details": {"7 nights x €132.31": "€926.16"},
            },
        },
        "quote_attempt_status": "success",
        "details_fetched_at": "2026-10-01T12:00:00+00:00",
        "quote_check_in": definition["check_in"],
        "quote_check_out": definition["check_out"],
        "quote_currency": definition["currency"],
        "quote_party_supported": True,
        "quote_adults": definition["adults"],
        "quote_children": definition["children"],
        "quote_infants": definition["infants"],
        "quote_pets": definition["pets"],
        "search_quote_total": search_total,
        "search_quote_source": "price.total.amount",
        "search_quote_completeness": "provider_total_tax_completeness_unknown",
        "search_quote_check_in": definition["check_in"],
        "search_quote_check_out": definition["check_out"],
        "search_quote_currency": definition["currency"],
        "search_quote_party_supported": True,
        "quote_status": "current",
        "details_quote_status": "current",
        "search_quote_status": "current" if search_total else "missing",
        "quote_source": "details:price.main.details.Total",
        "quote_total_completeness": "provider_breakdown_total",
    }


def test_saved_quote_repair_clears_zero_subtotal_without_inventing_stay_fees(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)
    definition = search_service.load_search_definition("trip", root=tmp_path / "searches")
    source = pd.DataFrame([_saved_quote_row(definition)])

    repaired = airbnb_service.repair_saved_quote_prices(source, definition)

    assert repaired.loc[0, "quote_status"] == "missing"
    assert pd.isna(repaired.loc[0, "total_price"])
    assert "7 nights x €132.31" in repaired.loc[0, "price_details"]["main"]["details"]
    assert source.loc[0, "total_price"] == 0  # the helper returns a repaired copy


def test_saved_quote_repair_uses_only_a_positive_current_discovery_total_for_placeholder_detail(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)
    definition = search_service.load_search_definition("trip", root=tmp_path / "searches")
    source = pd.DataFrame([_saved_quote_row(definition, search_total=1250)])

    repaired = airbnb_service.repair_saved_quote_prices(source, definition)

    assert repaired.loc[0, "quote_status"] == "current"
    assert repaired.loc[0, "total_price"] == 1250
    assert repaired.loc[0, "quote_source"].startswith("discovery_fallback:")
    assert repaired.loc[0, "quote_recovery_reason"]


@pytest.mark.parametrize("mismatch", ["date", "currency", "party"])
def test_saved_quote_repair_does_not_reuse_discovery_total_from_wrong_trip_context(tmp_path, monkeypatch, mismatch):
    setup_search(tmp_path, monkeypatch, children=0)
    definition = search_service.load_search_definition("trip", root=tmp_path / "searches")
    row = _saved_quote_row(definition, search_total=1250)
    if mismatch == "date":
        row["search_quote_check_in"] = "2099-01-01"
    elif mismatch == "currency":
        row["search_quote_currency"] = "USD"
    else:
        row["search_quote_party_supported"] = False

    repaired = airbnb_service.repair_saved_quote_prices(pd.DataFrame([row]), definition)

    assert repaired.loc[0, "quote_status"] == "unverified"
    assert pd.isna(repaired.loc[0, "total_price"])
    assert repaired.loc[0, "unverified_total_price"] == 1250


def test_saved_quote_repair_keeps_a_current_explicit_positive_detail_total(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)
    definition = search_service.load_search_definition("trip", root=tmp_path / "searches")
    row = _saved_quote_row(definition, detail_total=0)
    row["price_details"] = {"main": {"price": "€100", "qualifier": "per night", "details": {"Total": "€1.250,00"}}}

    repaired = airbnb_service.repair_saved_quote_prices(pd.DataFrame([row]), definition)

    assert repaired.loc[0, "total_price"] == 1250
    assert repaired.loc[0, "quote_status"] == "current"
    assert repaired.loc[0, "quote_source"] == "details:price.main.details.Total"


def test_details_checkpoint_reviews_and_unverified_party_quote(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch)
    provider = FakeProvider([{"room_id": "100", "latitude": 45.1, "longitude": 7.7}])
    airbnb_service.discover_listings("trip", provider=provider)

    result = airbnb_service.collect_details_and_reviews("trip", provider=provider)
    assert result["status"] == "success"
    assert result["quote_party_supported"] is False
    assert len(provider.detail_calls) == 1
    assert len(provider.review_calls) == 1
    check_in = provider.detail_calls[0]["check_in"]
    check_out = provider.detail_calls[0]["check_out"]
    assert isinstance(check_in, date)
    assert isinstance(check_out, date)
    assert provider.detail_calls[0]["adults"] == 2

    # Offline contract check against the pinned pyairbnb query builder. Its
    # public get_details annotation says str, but the builder requires date.
    pyairbnb_price = pytest.importorskip("pyairbnb.price")
    variables = pyairbnb_price._build_query_variables(
        room_id="100", check_in=check_in, check_out=check_out, adults=2,
    )
    assert variables["pdpSectionsRequest"]["checkIn"] == check_in.isoformat()
    assert variables["pdpSectionsRequest"]["checkOut"] == check_out.isoformat()

    output = tmp_path / "searches" / "trip" / "output_data"
    details = pd.read_csv(output / "details_df.csv", dtype={"room_id": str})
    reviews = pd.read_csv(output / "reviews_df.csv", dtype={"room_id": str})
    merged = pd.read_csv(output / "merged_df.csv", dtype={"room_id": str})
    assert details.iloc[0]["quote_check_in"]
    assert "review-100" in reviews.iloc[0]["reviews_text"]
    assert merged.iloc[0]["quote_status"] == "unverified"
    assert "quote_party_warning" in merged.columns


def test_unavailable_detail_quote_never_falls_back_to_discovery_total(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)

    class UnavailableError(Exception):
        pass

    class UnavailableProvider(FakeProvider):
        def get_details(self, room_url, check_in, check_out, adults, currency):
            self.detail_calls.append({"room_url": room_url, "check_in": check_in, "check_out": check_out, "adults": adults, "currency": currency})
            raise UnavailableError("No availability for these dates")

    provider = UnavailableProvider([{
        "room_id": "200",
        "latitude": 45.1,
        "longitude": 7.7,
        "price": {"unit": {"amount": 100}, "total": {"amount": 480}},
    }])
    airbnb_service.discover_listings("trip", provider=provider)
    result = airbnb_service.collect_details_and_reviews("trip", provider=provider)

    output = tmp_path / "searches" / "trip" / "output_data"
    details = pd.read_csv(output / "details_df.csv", dtype={"room_id": str})
    merged = pd.read_csv(output / "merged_df.csv", dtype={"room_id": str})
    assert result["status"] == "success"
    assert details.loc[0, "availability_state"] == "unavailable"
    assert details.loc[0, "quote_attempt_status"] == "unavailable"
    assert merged.loc[0, "quote_status"] == "unavailable"
    assert pd.isna(merged.loc[0, "total_price"])


def test_generic_detail_failure_keeps_search_total_only_as_unverified(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)

    class FailingProvider(FakeProvider):
        def get_details(self, room_url, check_in, check_out, adults, currency):
            raise OSError("temporary provider error")

    provider = FailingProvider([{
        "room_id": "201",
        "latitude": 45.1,
        "longitude": 7.7,
        "price": {"unit": {"amount": 100}, "total": {"amount": 525}},
    }])
    airbnb_service.discover_listings("trip", provider=provider)
    result = airbnb_service.collect_details_and_reviews("trip", provider=provider)

    merged = pd.read_csv(
        tmp_path / "searches" / "trip" / "output_data" / "merged_df.csv",
        dtype={"room_id": str},
    )
    assert result["status"] == "partial"
    assert merged.loc[0, "quote_status"] == "unverified"
    assert pd.isna(merged.loc[0, "total_price"])
    assert merged.loc[0, "unverified_total_price"] == 525


def test_details_http_429_stops_before_later_listings_and_checkpoints(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)

    class DetailsRateLimitedProvider(FakeProvider):
        def get_details(self, room_url, check_in, check_out, adults, currency):
            room_id = room_url.rsplit("/", 1)[-1]
            self.detail_calls.append({"room_url": room_url, "check_in": check_in, "check_out": check_out})
            if room_id == "2":
                raise OSError("HTTP Error 429: Too Many Requests")
            return {"room_id": room_id, "name": f"Listing {room_id}", "total_price": 450}

    provider = DetailsRateLimitedProvider([
        {"room_id": str(room_id), "latitude": 45.1, "longitude": 7.7}
        for room_id in ("1", "2", "3")
    ])
    airbnb_service.discover_listings("trip", provider=provider)
    result = airbnb_service.collect_details_and_reviews("trip", provider=provider)

    assert result["status"] == "partial"
    assert result["rate_limited"] is True
    assert result["completed"] == 1
    assert result["failed_room_ids"] == ["2"]
    assert result["attempted_room_ids"] == ["1", "2"]
    assert result["unattempted_room_ids"] == ["3"]
    assert [call["room_url"].rsplit("/", 1)[-1] for call in provider.detail_calls] == ["1", "2"]
    assert provider.review_calls == ["https://www.airbnb.com/rooms/1"]
    assert result["failure_details"] == [{
        "room_id": "2", "stage": "details",
        "error": "HTTP Error 429: Too Many Requests", "status_code": 429, "retryable": True,
    }]

    output = tmp_path / "searches" / "trip" / "output_data"
    snapshot = json.loads((output / "collection_failures.json").read_text(encoding="utf-8"))
    assert snapshot["rate_limited"] is True
    assert snapshot["attempted_room_ids"] == ["1", "2"]
    assert snapshot["unattempted_room_ids"] == ["3"]
    assert snapshot["failed"][0]["room_id"] == "2"
    details = pd.read_csv(output / "details_df.csv", dtype={"room_id": str})
    reviews = pd.read_csv(output / "reviews_df.csv", dtype={"room_id": str})
    assert details["room_id"].tolist() == ["1", "2"]
    assert reviews["room_id"].tolist() == ["1"]


def test_reviews_http_429_stops_before_later_listings(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)

    class ReviewRateLimitedProvider(FakeProvider):
        def get_reviews(self, room_url, language="en"):
            self.review_calls.append(room_url)
            room_id = room_url.rsplit("/", 1)[-1]
            if room_id == "2":
                error = RuntimeError("rate limit")
                error.response = type("Response", (), {"status_code": 429})()
                raise error
            return [{"id": f"review-{room_id}", "comments": "Quiet and clean."}]

    provider = ReviewRateLimitedProvider([
        {"room_id": str(room_id), "latitude": 45.1, "longitude": 7.7}
        for room_id in ("1", "2", "3")
    ])
    airbnb_service.discover_listings("trip", provider=provider)
    result = airbnb_service.collect_details_and_reviews("trip", provider=provider)

    assert result["rate_limited"] is True
    assert result["status"] == "partial"
    assert result["completed"] == 1
    assert result["failed_room_ids"] == ["2"]
    assert result["failure_details"][0]["stage"] == "reviews"
    assert result["failure_details"][0]["status_code"] == 429
    assert result["attempted_room_ids"] == ["1", "2"]
    assert result["unattempted_room_ids"] == ["3"]
    assert [call["room_url"].rsplit("/", 1)[-1] for call in provider.detail_calls] == ["1", "2"]
    assert [call.rsplit("/", 1)[-1] for call in provider.review_calls] == ["1", "2"]
    details = pd.read_csv(
        tmp_path / "searches" / "trip" / "output_data" / "details_df.csv",
        dtype={"room_id": str},
    )
    assert details["room_id"].tolist() == ["1", "2"]


def test_cooperative_cancel_saves_partial_merged_snapshot(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)
    cancel_event = threading.Event()
    provider = FakeProvider([
        {"room_id": "100", "latitude": 45.1, "longitude": 7.7},
        {"room_id": "101", "latitude": 45.1, "longitude": 7.7},
    ], cancel_event=cancel_event)
    airbnb_service.discover_listings("trip", provider=provider)
    result = airbnb_service.collect_details_and_reviews(
        "trip", provider=provider, should_cancel=cancel_event.is_set,
    )
    assert result["status"] == "cancelled"
    assert result["completed"] == 1
    merged = pd.read_csv(tmp_path / "searches" / "trip" / "output_data" / "merged_df.csv", dtype={"room_id": str})
    assert len(merged) == 2
    assert merged["details_fetched_at"].notna().sum() == 1
    assert merged["quote_status"].eq("current").sum() == 1


def test_reuses_reviews_returned_by_details_without_duplicate_request(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)
    provider = FakeProvider(
        [{"room_id": "100", "latitude": 45.1, "longitude": 7.7}],
        include_reviews_in_details=True,
    )
    airbnb_service.discover_listings("trip", provider=provider)
    airbnb_service.collect_details_and_reviews("trip", provider=provider)
    assert provider.review_calls == []
    reviews = pd.read_csv(tmp_path / "searches" / "trip" / "output_data" / "reviews_df.csv", dtype={"room_id": str})
    assert "detail-review-100" in reviews.iloc[0]["reviews_text"]


def test_empty_discovery_clears_stale_merged_snapshot(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0)
    output = tmp_path / "searches" / "trip" / "output_data"
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"room_id": "stale", "name": "Old result"}]).to_csv(output / "merged_df.csv", index=False)
    result = airbnb_service.discover_listings("trip", provider=FakeProvider([]))
    assert result["status"] == "no_matches"
    merged = pd.read_csv(output / "merged_df.csv")
    assert merged.empty
    assert "room_id" in merged.columns


def test_collector_that_cannot_pass_party_is_rejected_before_call(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch)

    class OldProvider:
        called = False

        def search_all(self, check_in, check_out):
            self.called = True
            return []

    provider = OldProvider()
    with pytest.raises(airbnb_service.CollectorCompatibilityError, match="children"):
        airbnb_service.discover_listings("trip", provider=provider)
    assert provider.called is False


def test_pets_are_passed_to_collectors_that_accept_them(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0, pets=1)

    class PetAwareProvider(FakeProvider):
        def get_details(self, room_url, check_in, check_out, adults, currency, **kwargs):
            self.detail_calls.append({
                "room_url": room_url,
                "check_in": check_in,
                "check_out": check_out,
                "adults": adults,
                "currency": currency,
                **kwargs,
            })
            return {"room_id": room_url.rsplit("/", 1)[-1], "total_price": 500}

    provider = PetAwareProvider([{"room_id": "300", "latitude": 45.1, "longitude": 7.7}])
    airbnb_service.discover_listings("trip", provider=provider)
    result = airbnb_service.collect_details_and_reviews("trip", provider=provider)
    assert provider.search_calls[0]["pets"] == 1
    assert provider.detail_calls[0]["pets"] == 1
    assert result["quote_party_supported"] is True


def test_legacy_free_cancellation_setting_does_not_restrict_discovery(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0, free_cancellation=True)
    provider = FakeProvider([{"room_id": "cancel-1", "latitude": 45.1, "longitude": 7.7}])
    result = airbnb_service.discover_listings("trip", provider=provider)
    saved = pd.read_csv(tmp_path / "searches" / "trip" / "output_data" / "results_df.csv")
    assert provider.search_calls[0]["free_cancellation"] is False
    assert result["free_cancellation_filter_applied"] is False
    assert pd.isna(saved.loc[0, "free_cancellation_search_match"])


def test_unfiltered_results_leave_cancellation_state_unknown(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0, free_cancellation=False)
    provider = FakeProvider([{"room_id": "not-filtered", "latitude": 45.1, "longitude": 7.7}])

    result = airbnb_service.discover_listings("trip", provider=provider)
    saved = pd.read_csv(
        tmp_path / "searches" / "trip" / "output_data" / "results_df.csv",
        dtype={"room_id": str},
    )

    assert provider.search_calls[0]["free_cancellation"] is False
    assert result["free_cancellation_filter_applied"] is False
    assert pd.isna(saved.loc[0, "free_cancellation_search_match"])
    assert pd.isna(saved.loc[0, "cancellation_search_currency"])


def test_free_cancellation_filter_rejects_collectors_that_cannot_apply_it(tmp_path, monkeypatch):
    setup_search(tmp_path, monkeypatch, children=0, free_cancellation=True)
    enable_free_cancellation_filter(monkeypatch)

    class LimitedProvider:
        called = False

        def search_all(self, check_in, check_out, adults):
            self.called = True
            return []

    provider = LimitedProvider()
    airbnb_service.discover_listings("trip", provider=provider)
    assert provider.called is True



def test_automatic_collection_bounds_reviews_and_resumes_without_redownloading(tmp_path, monkeypatch):
    import requests
    from src.request_policy import RequestPolicy, automatic_options
    setup_search(tmp_path, monkeypatch, children=0)
    provider = FakeProvider([{'room_id': '10', 'latitude': 45.1, 'longitude': 7.7}])
    airbnb_service.discover_listings('trip', provider=provider)
    attempts = []
    def reviews(room_url, language='en'):
        attempts.append(room_url)
        if len(attempts) == 1:
            raise requests.Timeout('temporary review failure')
        return [{'id': 'old', 'createdAt': '2020-01-01', 'comments': 'Old.'},
                {'id': 'new', 'createdAt': '2026-11-01', 'comments': 'New.'}]
    provider.get_reviews = reviews
    policy = RequestPolicy(automatic_options({'review_cutoff': '2025-01-01'}))
    monkeypatch.setattr(policy, 'wait', lambda *args: None)
    result = airbnb_service.collect_details_and_reviews('trip', provider=provider, request_policy=policy)
    assert result['status'] == 'success'
    assert len(attempts) == 2
    saved = pd.read_csv(tmp_path / 'searches' / 'trip' / 'output_data' / 'reviews_df.csv')
    assert saved.iloc[0].reviews_retrieved_count == 1
    assert 'New.' in saved.iloc[0].reviews_text and 'Old.' not in saved.iloc[0].reviews_text
    airbnb_service.collect_details_and_reviews('trip', provider=provider, request_policy=policy)
    assert len(attempts) == 2 and len(provider.detail_calls) == 1


def test_raising_threshold_only_fetches_newly_included_properties(tmp_path, monkeypatch):
    from src.request_policy import RequestPolicy, automatic_options
    from src.processing_service import save_selection
    setup_search(tmp_path, monkeypatch, children=0)
    provider = FakeProvider([{'room_id': rid, 'latitude': 45.1, 'longitude': 7.7,
                             'total_price': total} for rid, total in [('10', 100), ('20', 200), ('30', 300)]])
    original_details = provider.get_details
    def priced_details(*args, **kwargs):
        payload = original_details(*args, **kwargs)
        payload['total_price'] = int(payload['room_id']) * 10
        return payload
    import functools
    provider.get_details = functools.wraps(original_details)(priced_details)
    airbnb_service.discover_listings('trip', provider=provider)
    directory = tmp_path / 'searches' / 'trip'
    definition = search_service.load_search_definition('trip', root=tmp_path / 'searches')
    policy = RequestPolicy(automatic_options())
    monkeypatch.setattr(policy, 'wait', lambda *args: None)
    save_selection(directory, definition, {'max_total': 150})
    airbnb_service.collect_details_and_reviews('trip', provider=provider, request_policy=policy)
    assert len(provider.detail_calls) == 1 and len(provider.review_calls) == 1
    save_selection(directory, definition, {'max_total': 250})
    airbnb_service.collect_details_and_reviews('trip', provider=provider, request_policy=policy)
    assert len(provider.detail_calls) == 2 and len(provider.review_calls) == 2
    assert provider.detail_calls[-1]['room_url'].endswith('/20')
    airbnb_service.collect_details_and_reviews('trip', provider=provider, request_policy=policy)
    assert len(provider.detail_calls) == 2 and len(provider.review_calls) == 2
    # A discovery refresh updates prices but retains data and marks absent IDs.
    provider.results = [{'room_id': '20', 'latitude': 45.1, 'longitude': 7.7, 'total_price': 220}]
    airbnb_service.discover_listings('trip', provider=provider)
    from src import storage_service
    merged = storage_service.read_frame(directory / 'output_data' / 'merged_df.csv')
    assert set(merged.room_id.astype(str)) == {'10', '20', '30'}
    assert merged.set_index('room_id').loc['10', 'availability_state'] == 'not_returned'
    airbnb_service.collect_details_and_reviews('trip', provider=provider, request_policy=policy)
    assert len(provider.detail_calls) == 2 and len(provider.review_calls) == 2


def test_cache_expires_after_30_days_and_missing_results_are_retained(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from src import storage_service
    from src.processing_service import save_selection
    from src.request_policy import RequestPolicy, automatic_options
    setup_search(tmp_path, monkeypatch, children=0)
    provider = FakeProvider([{'room_id': '10', 'latitude': 45.1, 'longitude': 7.7, 'total_price': 100}])
    original_details = provider.get_details
    def priced_details(*args, **kwargs):
        payload = original_details(*args, **kwargs)
        payload['total_price'] = int(payload['room_id']) * 10
        return payload
    import functools
    provider.get_details = functools.wraps(original_details)(priced_details)
    airbnb_service.discover_listings('trip', provider=provider)
    directory = tmp_path / 'searches' / 'trip'
    definition = search_service.load_search_definition('trip', root=tmp_path / 'searches')
    save_selection(directory, definition, {'max_total': 150})
    policy = RequestPolicy(automatic_options())
    monkeypatch.setattr(policy, 'wait', lambda *args: None)
    airbnb_service.collect_details_and_reviews('trip', provider=provider, request_policy=policy)
    old = (datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
    for filename, field in [('details_df.csv', 'details_fetched_at'), ('reviews_df.csv', 'reviews_fetched_at')]:
        path = directory / 'output_data' / filename
        frame = storage_service.read_frame(path)
        frame[field] = old
        storage_service.write_frame(path, frame)
    # Expire the independent shared cache too: local expiry alone can now be
    # recovered from another still-fresh copy, which is intentional.
    import sqlite3
    from src.property_cache import PropertyCache
    with sqlite3.connect(PropertyCache(tmp_path/'searches').path) as db:
        db.execute('UPDATE evidence SET fetched=?', (old,))
    airbnb_service.collect_details_and_reviews('trip', provider=provider, request_policy=policy)
    assert len(provider.detail_calls) == 2 and len(provider.review_calls) == 2
    provider.results = []
    result = airbnb_service.discover_listings('trip', provider=provider)
    assert result['status'] == 'no_matches'
    merged = storage_service.read_frame(directory / 'output_data' / 'merged_df.csv')
    assert len(merged) == 1 and merged.iloc[0]['discovery_state'] == 'not_returned'
    assert merged.iloc[0]['availability_state'] != 'unavailable'
    assert storage_service.exists(directory / 'output_data' / 'reviews_df.csv')


def test_detail_requote_over_threshold_skips_reviews_until_threshold_raised(tmp_path, monkeypatch):
    from src.processing_service import save_selection
    from src.request_policy import RequestPolicy, automatic_options
    setup_search(tmp_path, monkeypatch, children=0)
    provider = FakeProvider([{'room_id': '10', 'latitude': 45.1, 'longitude': 7.7, 'total_price': 100}])
    airbnb_service.discover_listings('trip', provider=provider)
    directory = tmp_path / 'searches' / 'trip'
    definition = search_service.load_search_definition('trip', root=tmp_path / 'searches')
    save_selection(directory, definition, {'max_total': 150})
    policy = RequestPolicy(automatic_options())
    monkeypatch.setattr(policy, 'wait', lambda *args: None)
    first = airbnb_service.collect_details_and_reviews('trip', provider=provider, request_policy=policy)
    assert first['skipped_budget_room_ids'] == ['10']  # Details quote is 450.
    assert len(provider.detail_calls) == 1 and not provider.review_calls
    save_selection(directory, definition, {'max_total': 500})
    airbnb_service.collect_details_and_reviews('trip', provider=provider, request_policy=policy)
    assert len(provider.detail_calls) == 1 and len(provider.review_calls) == 1


def test_real_collector_path_never_runs_hidden_full_review_wrapper(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.request_policy import RequestPolicy, automatic_options
    setup_search(tmp_path, monkeypatch, children=0)
    provider = FakeProvider([{'room_id':'1','latitude':45.1,'longitude':7.7}])
    airbnb_service.discover_listings('trip',provider=provider)
    provider.__name__ = 'pyairbnb'
    calls = []
    def forbidden(*args, **kwargs):
        pytest.fail('High-level details / unbounded reviews must never be called')
    def page(*args):
        calls.append('page')
        return {'name':'Flat','room_id':'1'}, {'api_key':'private-bootstrap-key','product_id':'product1','impression_id':'impression1'}, {'cookie':'private-cookie'}
    def price(**kwargs):
        calls.append('price')
        assert kwargs['api_key'] == 'private-bootstrap-key'
        assert kwargs['cookies'] == {'cookie':'private-cookie'}
        assert kwargs['adults'] == 2 and kwargs['currency'] == 'EUR'
        return {'total':450}
    def reviews(api_key, offset, product_id, **kwargs):
        calls.append(('reviews',offset))
        assert api_key == 'private-bootstrap-key' and product_id == 'product1'
        return [{'id':str(i),'created_at':'2026-10-01','comments':'Quiet'} for i in range(50)]
    provider.get_details = forbidden
    provider.get_reviews = forbidden
    provider.details = SimpleNamespace(get=page)
    provider.get_price = price
    provider.reviews = SimpleNamespace(get_from_offset=reviews)
    policy = RequestPolicy(automatic_options({'review_cutoff':'2025-01-01','max_reviews':25}))
    monkeypatch.setattr(policy,'wait',lambda *args: None)
    result = airbnb_service.collect_details_and_reviews('trip',provider=provider,request_policy=policy)
    assert result['status'] == 'success'
    assert calls == ['page','price',('reviews',0)], 'Only three paced requests; reuse the page bootstrap'
    saved = airbnb_service.storage.read_frame(tmp_path/'searches/trip/output_data/reviews_df.csv')
    assert int(saved.iloc[0]['reviews_retrieved_count']) == 25
    details = airbnb_service.storage.read_frame(tmp_path/'searches/trip/output_data/details_df.csv')
    assert 'private-bootstrap-key' not in details.to_json()
    assert 'private-cookie' not in details.to_json()
