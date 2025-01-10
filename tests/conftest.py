"""Shared pytest fixtures and configuration."""

import os
import pytest
import tempfile
import yaml
from pathlib import Path

@pytest.fixture(scope="session")
def test_data_dir():
    """Create a temporary directory for test data."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)

@pytest.fixture(scope="session")
def test_config():
    """Create a test configuration."""
    return {
        "search_name": "test_search",
        "check_in": "2024-06-01",
        "check_out": "2024-06-07",
        "coordinates": {
            "ne_lat": 40.7,
            "ne_long": -73.9,
            "sw_lat": 40.6,
            "sw_long": -74.0
        },
        "currency": "USD",
        "min_price": 100,
        "max_price": 300,
        "min_rating": 4.0
    }

@pytest.fixture(scope="session")
def test_search_dir(test_data_dir, test_config):
    """Create a test search directory with configuration."""
    search_dir = test_data_dir / test_config["search_name"]
    search_dir.mkdir()
    
    # Create config file
    config_path = search_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(test_config, f)
    
    # Create output directory
    output_dir = search_dir / "output_data"
    output_dir.mkdir()
    
    return search_dir
