"""Tests for abnb_monitor.py functionality."""

import os
import pytest
import pandas as pd
from pathlib import Path
import numpy as np
from abnb_monitor import extract_coordinates, merge_dataframes, aggregate_reviews

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
    """Test merging of all DataFrames."""
    # First merge results with details
    merged_df = pd.merge(
        sample_results_df,
        sample_details_df[['room_id', 'amenities', 'person_capacity', 'is_super_host']],
        on='room_id',
        how='left'
    )
    
    # Then merge with reviews
    final_df = pd.merge(
        merged_df,
        sample_reviews_df.groupby('room_id')['AI_rating'].mean().reset_index(),
        on='room_id',
        how='left'
    )
    
    assert len(final_df) == len(sample_results_df)
    assert 'amenities' in final_df.columns
    assert 'AI_rating' in final_df.columns
    assert final_df.iloc[0]['AI_rating'] == 5.0

def test_review_aggregation(sample_reviews_df):
    """Test aggregation of reviews by room."""
    agg_reviews = sample_reviews_df.groupby('room_id').agg({
        'reviews_text': lambda x: ' '.join(x),
        'AI_rating': 'mean',
        'AI_review_summary': 'first'
    }).reset_index()
    
    assert len(agg_reviews) == 2  # Two unique rooms
    assert agg_reviews.loc[0, 'AI_rating'] == 5.0  # Average of two 5.0 ratings
    assert isinstance(agg_reviews.loc[0, 'AI_review_summary'], str)
    assert 'Location:' in agg_reviews.loc[0, 'AI_review_summary']

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
    """Test merging DataFrames with missing data."""
    # Create a copy with missing data
    results_df_missing = sample_results_df.copy()
    results_df_missing.loc[0, 'room_id'] = '999999'  # Non-existent ID
    
    merged_df = merge_dataframes(results_df_missing, sample_reviews_df, sample_details_df)
    assert len(merged_df) == len(sample_results_df) - 1

def test_aggregate_reviews_empty():
    """Test review aggregation with empty data."""
    empty_df = pd.DataFrame(columns=['reviews_text'])
    result = aggregate_reviews(empty_df)
    assert result == ''

def test_aggregate_reviews_multiple(sample_merged_df):
    """Test aggregation of multiple reviews."""
    # Add another review
    sample_merged_df.loc[1, 'reviews_text'] = 'Another great review'
    result = aggregate_reviews(sample_merged_df)
    assert isinstance(result, str)
    assert len(result.split('\n')) >= 2

def test_extract_coordinates_invalid():
    """Test coordinate extraction with invalid data."""
    invalid_coords = 'Not a coordinate string'
    lat, lon = extract_coordinates(invalid_coords)
    assert pd.isna(lat)
    assert pd.isna(lon)

def test_extract_coordinates_empty():
    """Test coordinate extraction with empty data."""
    lat, lon = extract_coordinates('')
    assert pd.isna(lat)
    assert pd.isna(lon)
