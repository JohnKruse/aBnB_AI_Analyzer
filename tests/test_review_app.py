"""Tests for review_app.py functionality."""

import os
import pytest
import pandas as pd
from pathlib import Path
import numpy as np
from review_app import filter_by_price_range, filter_by_rating, filter_by_amenities, filter_by_capacity

@pytest.fixture
def sample_merged_df():
    """Create a sample merged DataFrame with all necessary columns."""
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
        'latitude': [41.7889, 41.7912],
        'longitude': [-107.2354, -107.2389],
        'kind': ['ROOMS', 'ROOMS'],
        'type': ['REGULAR', 'REGULAR'],
        'category': ['entire_home', 'entire_home'],
        'badges': [['GUEST_FAVORITE'], ['SUPERHOST']],
        'reviews_text': [
            '2024-10-05T20:45:05Z Great spot for me & the dog. Rating: 5\n2024-09-11T20:18:03Z Walkable from downtown. Rating: 5',
            '2024-10-29T20:19:13Z Thank you so much! This was a fantastic spot. Rating: 5'
        ],
        'AI_rating': [5.0, 4.5],
        'AI_review_summary': [
            'Location: Near downtown\nCleanliness: Excellent\nAmenities: Well-equipped',
            'Location: Convenient\nComfort: High\nHost: Responsive'
        ],
        'amenities': [
            ['Wifi', 'Kitchen', 'Free parking'],
            ['Wifi', 'Kitchen', 'Washer']
        ],
        'person_capacity': [6, 4],
        'is_super_host': [True, False]
    }
    return pd.DataFrame(data)

def test_filter_by_price_range(sample_merged_df):
    """Test filtering properties by price range."""
    min_price = 170
    max_price = 200
    
    filtered_df = filter_by_price_range(sample_merged_df, min_price, max_price)
    
    assert len(filtered_df) == 1
    assert filtered_df.iloc[0]['price'] == 185.0
    assert filtered_df.iloc[0]['room_id'] == '707269743259778182'

def test_filter_by_rating(sample_merged_df):
    """Test filtering properties by minimum rating."""
    min_rating = 4.9
    
    filtered_df = filter_by_rating(sample_merged_df, min_rating)
    
    assert len(filtered_df) == 1
    assert filtered_df.iloc[0]['rating'] == 4.94
    assert filtered_df.iloc[0]['room_id'] == '49269562'

def test_geographic_bounds(sample_merged_df):
    """Test filtering properties within geographic bounds."""
    bounds = {
        'ne_lat': 41.7920,
        'ne_long': -107.2350,
        'sw_lat': 41.7910,
        'sw_long': -107.2400
    }
    
    filtered_df = sample_merged_df[
        (sample_merged_df['latitude'] >= bounds['sw_lat']) &
        (sample_merged_df['latitude'] <= bounds['ne_lat']) &
        (sample_merged_df['longitude'] >= bounds['sw_long']) &
        (sample_merged_df['longitude'] <= bounds['ne_long'])
    ]
    
    assert len(filtered_df) == 1
    assert filtered_df.iloc[0]['room_id'] == '707269743259778182'

def test_filter_by_amenities(sample_merged_df):
    """Test filtering properties by required amenities."""
    required_amenities = ['Wifi', 'Kitchen']
    
    filtered_df = filter_by_amenities(sample_merged_df, required_amenities)
    
    assert len(filtered_df) == 2  # Both properties have Wifi and Kitchen
    assert 'Free parking' in filtered_df.iloc[0]['amenities']
    assert 'Washer' in filtered_df.iloc[1]['amenities']

def test_filter_by_capacity(sample_merged_df):
    """Test filtering properties by minimum person capacity."""
    min_capacity = 5
    
    filtered_df = filter_by_capacity(sample_merged_df, min_capacity)
    
    assert len(filtered_df) == 1
    assert filtered_df.iloc[0]['person_capacity'] == 6
    assert filtered_df.iloc[0]['room_id'] == '49269562'

def test_filter_by_price_range_invalid():
    """Test filtering by price range with invalid inputs."""
    empty_df = pd.DataFrame(columns=['price'])
    filtered_df = filter_by_price_range(empty_df, -100, 200)  # Invalid min price
    assert len(filtered_df) == 0
    
    filtered_df = filter_by_price_range(empty_df, 200, 100)  # Min > max
    assert len(filtered_df) == 0

def test_filter_by_rating_edge_cases(sample_merged_df):
    """Test filtering by rating with edge cases."""
    # Test with rating exactly at threshold
    filtered_df = filter_by_rating(sample_merged_df, 4.5)
    assert len(filtered_df) > 0
    
    # Test with very high rating threshold
    filtered_df = filter_by_rating(sample_merged_df, 5.1)
    assert len(filtered_df) == 0

def test_filter_by_amenities_no_match(sample_merged_df):
    """Test filtering by amenities with no matches."""
    filtered_df = filter_by_amenities(sample_merged_df, ['NonexistentAmenity'])
    assert len(filtered_df) == 0

def test_filter_by_amenities_partial_match(sample_merged_df):
    """Test filtering by amenities with partial matches."""
    amenities = ['Wifi', 'NonexistentAmenity']
    filtered_df = filter_by_amenities(sample_merged_df, amenities)
    assert len(filtered_df) > 0

def test_filter_by_capacity_zero(sample_merged_df):
    """Test filtering by capacity with zero guests."""
    filtered_df = filter_by_capacity(sample_merged_df, 0)
    assert len(filtered_df) == 0

def test_filter_by_capacity_large(sample_merged_df):
    """Test filtering by capacity with large number of guests."""
    filtered_df = filter_by_capacity(sample_merged_df, 100)
    assert len(filtered_df) == 0
