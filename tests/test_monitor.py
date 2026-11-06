"""Tests for abnb_monitor.py functionality."""

import os
import json
import pytest
import pandas as pd
from pathlib import Path
import numpy as np
from src.airbnb_service import merge_property_snapshots, normalize_reviews
from src.utils import extract_coordinates, parse_coordinates, parse_stay_total, extract_total_price


TRIP = {
    "trip_revision": 1,
    "check_in": "2027-02-01",
    "check_out": "2027-02-05",
    "currency": "EUR",
}

@pytest.fixture
def sample_results_df():
    """Create a sample results DataFrame based on real data structure."""
    data = {
        'room_id': ['49269562', '707269743259778182'],
        'name': ['Home in Rawlins', 'Downtown Apartment'],
        'title': ['Home in Rawlins', 'Downtown Apartment'],
        'price': [164.0, 185.0],
        'total_price': [164.0, 185.0],
        'rating': [4.94, 4.5],
        'Airbnb_rating': [4.94, 4.5],
        'Airbnb_review_count': [174, 156],
        'coordinates': [
            {'latitude': 41.7889, 'longitude': -107.2354},
            {'latitude': 41.7912, 'longitude': -107.2389}
        ],
        'kind': ['ROOMS', 'ROOMS'],
        'type': ['REGULAR', 'REGULAR'],
        'category': ['entire_home', 'entire_home'],
        'badges': [['GUEST_FAVORITE'], ['SUPERHOST']],
        'long_stay_discount': [{}, {}],
        'images': [[], []]
    }
    return pd.DataFrame(data)

@pytest.fixture
def sample_reviews_df():
    """Create a sample reviews DataFrame based on real data structure."""
    data = {
        'room_id': ['49269562', '49269562', '707269743259778182'],
        'AI_rating': [5.0, 5.0, 4.5],
        'reviews_text': [
            '2024-10-05T20:45:05Z Great spot for me & the dog. Rating: 5',
            '2024-09-11T20:18:03Z Walkable from downtown. Perfect for a remote worker. Rating: 5',
            '2024-10-29T20:19:13Z Thank you so much! This was a fantastic spot. Rating: 5'
        ],
        'AI_review_summary': [
            'Location: Near downtown\nCleanliness: Excellent\nAmenities: Well-equipped',
            'Location: Central\nParking: Easy\nValue: Good',
            'Location: Convenient\nComfort: High\nHost: Responsive'
        ],
        'ai_response': [
            '{"AI_rating":5}',
            '{"AI_rating":5}',
            '{"AI_rating":4.5}'
        ]
    }
    return pd.DataFrame(data)

@pytest.fixture
def sample_details_df():
    """Create a sample details DataFrame based on real data structure."""
    data = {
        'room_id': ['49269562', '707269743259778182'],
        'home_tier': [1, 1],
        'person_capacity': [6, 4],
        'co_hosts': [[], []],
        'language': ['en', 'en'],
        'title': ['Home in Rawlins', 'Downtown Apartment'],
        'is_super_host': [True, False],
        'longitude': [-107.2354, -107.2389],
        'latitude': [41.7889, 41.7912],
        'room_type': ['En', 'En'],
        'coordinates': [
            {'latitude': 41.7889, 'longitude': -107.2354},
            {'latitude': 41.7912, 'longitude': -107.2389}
        ],
        'rating': [4.94, 4.5],
        'price': [164.0, 185.0],
        'amenities': [
            ['Wifi', 'Kitchen', 'Free parking'],
            ['Wifi', 'Kitchen', 'Washer']
        ]
    }
    return pd.DataFrame(data)

@pytest.fixture
def sample_merged_df():
    """Create a sample merged DataFrame."""
    data = {
        'room_id': ['49269562', '707269743259778182'],
        'reviews_text': [
            '2024-10-05T20:45:05Z Great spot for me & the dog. Rating: 5',
            '2024-10-29T20:19:13Z Thank you so much! This was a fantastic spot. Rating: 5'
        ]
    }
    return pd.DataFrame(data)

def test_dataframe_merge(sample_results_df, sample_reviews_df, sample_details_df):
    """Test the collector's snapshot merge keeps search IDs and review evidence."""
    reviews = sample_reviews_df.groupby("room_id").agg({
        "reviews_text": lambda values: "\n".join(values),
        "AI_rating": "mean",
        "AI_review_summary": "first",
    }).reset_index()
    final_df = merge_property_snapshots(sample_results_df, sample_details_df, reviews, TRIP)
    
    assert len(final_df) == len(sample_results_df)
    assert 'amenities' in final_df.columns
    assert 'AI_rating' in final_df.columns
    assert final_df.iloc[0]['AI_rating'] == 5.0

def test_review_aggregation(sample_reviews_df):
    """Normalize raw review records without discarding source IDs."""
    raw_reviews = [
        {"id": f"r{index}", "comments": text, "created_at": f"2024-10-{index + 1:02d}"}
        for index, text in enumerate(sample_reviews_df["reviews_text"].tolist())
    ]
    text, raw_json, count = normalize_reviews(raw_reviews)
    assert count == 3
    assert "review_id=r0" in text
    assert "review_id=r2" in text
    assert '"id": "r1"' in raw_json

def test_coordinate_extraction(sample_results_df):
    """Test extraction of coordinates from the coordinates column."""
    df = sample_results_df.copy()
    
    # Extract latitude and longitude
    df['latitude'] = df['coordinates'].apply(lambda x: x['latitude'])
    df['longitude'] = df['coordinates'].apply(lambda x: x['longitude'])
    
    assert 'latitude' in df.columns
    assert 'longitude' in df.columns
    assert df.iloc[0]['latitude'] == 41.7889
    assert df.iloc[0]['longitude'] == -107.2354

def test_merge_dataframes_missing_data(sample_results_df, sample_reviews_df, sample_details_df):
    """Search rows remain visible even when no detail/review row matches."""
    # Create a copy with missing data
    results_df_missing = sample_results_df.copy()
    results_df_missing.loc[0, 'room_id'] = '999999'  # Non-existent ID
    
    reviews = sample_reviews_df.groupby("room_id").agg({"reviews_text": "first"}).reset_index()
    merged_df = merge_property_snapshots(results_df_missing, sample_details_df, reviews, TRIP)
    assert len(merged_df) == len(sample_results_df)
    assert merged_df.loc[merged_df.room_id == "999999", "name"].iloc[0] == "Home in Rawlins"
    assert pd.isna(merged_df.loc[merged_df.room_id == "999999", "person_capacity"].iloc[0])

def test_aggregate_reviews_empty():
    text, raw_json, count = normalize_reviews([])
    assert text == ""
    assert raw_json == "[]"
    assert count == 0

def test_aggregate_reviews_multiple(sample_merged_df):
    """Test preserving multiple distinct review excerpts."""
    sample_merged_df.loc[1, 'reviews_text'] = 'Another great review'
    records = [
        {"id": str(index), "comments": row.reviews_text, "created_at": "2024-10-01"}
        for index, row in sample_merged_df.iterrows()
    ]
    result, _, count = normalize_reviews(records)
    assert count == 2
    assert "Great spot" in result
    assert "Another great review" in result

def test_extract_coordinates_invalid():
    """Test coordinate extraction with invalid data."""
    invalid_coords = pd.DataFrame({"location": ["Not a coordinate string"]})
    extracted = extract_coordinates(invalid_coords)
    assert pd.isna(extracted.loc[0, "lat"])
    assert pd.isna(extracted.loc[0, "lng"])

def test_extract_coordinates_empty():
    """Test coordinate extraction with empty data."""
    extracted = extract_coordinates(pd.DataFrame({"location": [""]}))
    assert pd.isna(extracted.loc[0, "lat"])
    assert pd.isna(extracted.loc[0, "lng"])


def test_extract_coordinates_supports_pyairbnb_longitud_and_saved_payload_shapes():
    data = pd.DataFrame([
        {
            "latitude": np.nan,
            "longitude": np.nan,
            "coordinates": {"latitude": 51.45672, "longitud": -0.21581},
        },
        {"coordinates": json.dumps({"latitude": 51.5, "longitud": -0.3})},
        {"location": "{'latitude': 51.6, 'longitud': -0.4}"},
        {
            "latitude": 120,
            "longitude": 0,
            "location": {"lat": 51.7, "lon": -0.5},
        },
        {"coordinates": [7.7, 45.1]},
    ])

    extracted = extract_coordinates(data)

    assert list(zip(extracted["latitude"], extracted["longitude"])) == [
        (51.45672, -0.21581),
        (51.5, -0.3),
        (51.6, -0.4),
        (51.7, -0.5),
        (45.1, 7.7),
    ]


@pytest.mark.parametrize(
    "record",
    [
        {"latitude": np.nan, "longitud": -0.2},
        {"coordinates": {"latitude": np.inf, "longitud": -0.2}},
        {"coordinates": {"latitude": 91, "longitud": -0.2}},
        {"coordinates": {"latitude": 51.4, "longitud": -181}},
    ],
)
def test_parse_coordinates_rejects_nonfinite_and_out_of_range_pairs(record):
    assert parse_coordinates(record) == (None, None)


def test_extract_coordinates_is_idempotent_and_falls_back_after_nan_canonical_values():
    frame = pd.DataFrame([{
        "latitude": np.nan,
        "longitude": np.nan,
        "coordinates": "{'latitude': 51.45672, 'longitud': -0.21581}",
    }])
    first = extract_coordinates(frame)
    second = extract_coordinates(first)
    assert tuple(second.loc[0, ["latitude", "longitude"]]) == (51.45672, -0.21581)


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "€0", "-€1,00"])
def test_parse_stay_total_rejects_invalid_amounts(value):
    assert parse_stay_total(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("€926,16", 926.16), ("€1.250,00", 1250.0), ("$1,250.50", 1250.5)],
)
def test_parse_stay_total_accepts_locale_formats(value, expected):
    assert parse_stay_total(value) == pytest.approx(expected)


def test_legacy_total_extractor_never_falls_back_to_nightly_unit_amount():
    nightly_only = {"unit": {"amount": 150, "discount": 125}}
    explicit_total = {"unit": {"amount": 150}, "total": {"amount": "€926,16"}}

    assert pd.isna(extract_total_price(nightly_only))
    assert extract_total_price(explicit_total) == pytest.approx(926.16)
    assert extract_total_price(100) == 100
