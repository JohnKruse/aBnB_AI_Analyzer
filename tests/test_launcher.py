"""Tests for abnb_launcher.py functionality."""

import os
import pytest
import tempfile
import yaml
from pathlib import Path

@pytest.fixture
def temp_search_dir():
    """Create a temporary search directory with test config."""
    with tempfile.TemporaryDirectory() as temp_dir:
        search_dir = Path(temp_dir) / "test_search"
        search_dir.mkdir()
        
        # Create a test config file
        config = {
            "search_name": "test_search",
            "check_in": "2024-06-01",
            "check_out": "2024-06-07",
            "coordinates": {
                "ne_lat": 40.7,
                "ne_long": -73.9,
                "sw_lat": 40.6,
                "sw_long": -74.0
            },
            "currency": "USD"
        }
        
        config_path = search_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(config, f)
            
        yield temp_dir

def test_search_dir_creation(temp_search_dir):
    """Test that search directory is created with proper config."""
    config_path = Path(temp_search_dir) / "test_search" / "config.yaml"
    assert config_path.exists()
    
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    assert config["search_name"] == "test_search"
    assert config["currency"] == "USD"
    assert "coordinates" in config
