"""Utility functions for the BnB AI Analyzer.

This module contains common utility functions used across the BnB AI Analyzer project.
Functions are organized into categories:
- File operations
- Data processing
- AI operations
- Configuration handling
"""

import os
import yaml
import pandas as pd
from typing import Optional, Dict, Any, List
from pathlib import Path
import re
import ast
import json
import math
import numbers
from datetime import datetime
import logging
import sys
from collections.abc import Mapping

# Set up logging
logger = logging.getLogger(__name__)

# Get the project root directory
PROJECT_ROOT = Path(__file__).parent.parent.absolute()

# File Operations
def save_df(df: pd.DataFrame, path: str, reorder: bool = True) -> None:
    """Save a DataFrame to a CSV file, optionally reordering columns by width.
    
    Args:
        df (pandas.DataFrame): The DataFrame to save
        path (str): The file path where the CSV should be saved
        reorder (bool): Whether to reorder columns by width (default: True)
    """
    if reorder:
        # Reorder columns by their maximum string length
        col_lens = {col: df[col].astype(str).str.len().max() for col in df.columns}
        ordered_cols = sorted(col_lens.items(), key=lambda x: x[1])
        df = df[[col[0] for col in ordered_cols]]
    
    df.to_csv(path, index=False)

def load_or_create(file_path: str) -> pd.DataFrame:
    """Load a DataFrame from a CSV file or create an empty one if it doesn't exist.
    
    Args:
        file_path (str): Path to the CSV file
        
    Returns:
        pandas.DataFrame: The loaded or newly created DataFrame
    """
    if os.path.exists(file_path):
        return pd.read_csv(file_path)
    return pd.DataFrame()

# Data Processing
def extract_rating_info(rating_data: Dict[str, Any]) -> tuple[Optional[float], Optional[int]]:
    """Extract rating value and review count from rating data.
    
    Args:
        rating_data (dict): Dictionary containing rating information
        
    Returns:
        tuple: (rating value, review count)
    """
    if isinstance(rating_data, dict):
        return rating_data.get('value'), rating_data.get('reviewCount')
    return None, None

def _parse_coordinate_payload(value: Any) -> Any:
    """Parse native or CSV-serialized coordinate containers without raising."""
    if isinstance(value, str) and value.strip():
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(value)
            except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, (Mapping, list, tuple)):
                return parsed
    return value


def _validated_coordinate(value: Any, *, latitude: bool) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    upper_bound = 90 if latitude else 180
    if not math.isfinite(number) or not -upper_bound <= number <= upper_bound:
        return None
    return number


def _coordinate_pairs(value: Any, depth: int = 0):
    """Yield coordinate pairs from a bounded set of provider container shapes."""
    if depth > 5:
        return
    parsed = _parse_coordinate_payload(value)
    if isinstance(parsed, Mapping):
        latitude_keys = ("latitude", "lat")
        longitude_keys = ("longitude", "longitud", "lng", "lon")
        # Try every alias pairing. A canonical NaN or out-of-range value must
        # not prevent a valid alias in this same mapping from being considered.
        for latitude_key in latitude_keys:
            if latitude_key not in parsed:
                continue
            for longitude_key in longitude_keys:
                if longitude_key in parsed:
                    yield parsed[latitude_key], parsed[longitude_key]
        for nested_key in ("coordinates", "location", "location_data", "coordinate"):
            if nested_key in parsed:
                yield from _coordinate_pairs(parsed[nested_key], depth + 1)
    elif isinstance(parsed, (list, tuple)) and len(parsed) >= 2:
        # Airbnb/GeoJSON coordinate arrays use [longitude, latitude].
        yield parsed[1], parsed[0]


def parse_coordinates(record: Any) -> tuple[Optional[float], Optional[float]]:
    """Return a valid ``(latitude, longitude)`` pair from provider data.

    The helper accepts native mappings, JSON/Python-literal strings, nested
    ``location``/``coordinates`` fields, and coordinate arrays. It recognizes
    Airbnb's historical ``longitud`` spelling and rejects non-finite or
    out-of-range values. Invalid canonical fields are skipped so nested values
    can still recover coordinates from existing CSV rows.
    """
    for raw_latitude, raw_longitude in _coordinate_pairs(record):
        latitude = _validated_coordinate(raw_latitude, latitude=True)
        longitude = _validated_coordinate(raw_longitude, latitude=False)
        if latitude is not None and longitude is not None:
            return latitude, longitude
    return None, None


def extract_coordinates(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize coordinates in a DataFrame, including existing saved rows.

    Existing canonical columns are revalidated on every call. Missing, NaN,
    infinite, or out-of-range values therefore fall through to nested source
    fields rather than masking a valid provider coordinate.
    """
    if not df.empty:
        coords = df.apply(lambda row: parse_coordinates(row.to_dict()), axis=1)
        df["lat"] = [point[0] for point in coords]
        df["lng"] = [point[1] for point in coords]
        df["latitude"] = df["lat"]
        df["longitude"] = df["lng"]
    else:
        for column in ("lat", "lng", "latitude", "longitude"):
            if column not in df.columns:
                df[column] = pd.Series(dtype="float64")
    return df


def parse_stay_total(value: Any) -> Optional[float]:
    """Parse an already identified stay-total amount as a positive finite number.

    Currency symbols and localized decimal/thousands separators are accepted.
    This function does not decide whether an amount is a stay total; callers
    must select an explicit total field first.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, numbers.Number):
        try:
            amount = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return amount if math.isfinite(amount) and amount > 0 else None
    if not isinstance(value, str):
        return None
    cleaned = "".join(char for char in value.strip().replace("\u00a0", "").replace(" ", "") if char.isdigit() or char in ",.-")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        tail = cleaned.rsplit(",", 1)[-1]
        cleaned = cleaned.replace(",", ".") if len(tail) in {1, 2} else cleaned.replace(",", "")
    try:
        amount = float(cleaned)
    except (TypeError, ValueError, OverflowError):
        return None
    return amount if math.isfinite(amount) and amount > 0 else None


def extract_total_price(price: Any) -> float:
    """Extract an explicit total, returning NaN when price is unknown.

    Numeric and scalar string inputs are treated as already selected totals.
    Provider dictionaries must label a stay total; nightly unit amounts are
    never used as a fallback.
    """
    parsed = price
    if isinstance(price, str):
        try:
            parsed = json.loads(price)
        except (json.JSONDecodeError, TypeError):
            try:
                parsed = ast.literal_eval(price)
            except (ValueError, SyntaxError, TypeError):
                parsed = price
    if not isinstance(parsed, dict):
        amount = parse_stay_total(parsed)
        return amount if amount is not None else math.nan

    breakdown = parsed.get("break_down", parsed.get("breakdown", []))
    if isinstance(breakdown, list):
        for item in breakdown:
            if isinstance(item, dict) and str(item.get("description", "")).strip().casefold() == "total":
                amount = parse_stay_total(item.get("amount"))
                if amount is not None:
                    return amount
    total = parsed.get("total")
    if isinstance(total, dict):
        amount = parse_stay_total(total.get("amount"))
    else:
        amount = parse_stay_total(total)
    if amount is not None:
        return amount
    main = parsed.get("main")
    if isinstance(main, dict) and isinstance(main.get("details"), dict):
        for label, raw_amount in main["details"].items():
            if str(label).strip().casefold() in {"total", "total price", "total stay price"}:
                amount = parse_stay_total(raw_amount)
                if amount is not None:
                    return amount
    return math.nan

def format_reviews(reviews: List[Dict[str, Any]]) -> str:
    """Format a list of reviews into a single string.
    
    Args:
        reviews (list): List of review dictionaries
        
    Returns:
        str: Formatted reviews string
    """
    review_texts = []
    for review in reviews:
        created_at = review.get('created_at', 'N/A')
        comments = review.get('comments', 'N/A')
        rating = review.get('rating', 'N/A')
        review_texts.append(f"{created_at} {comments} Rating: {rating}")
    return '; '.join(review_texts)

def is_already_cleaned(text: str) -> bool:
    """Check if the AI review summary has already been cleaned.
    
    Args:
        text (str): Text to check
        
    Returns:
        bool: True if already cleaned, False otherwise
    """
    return not bool(re.search(r'[{}\[\]]', str(text)))

def is_numeric(value: Any) -> bool:
    """Check if a value can be converted to a float.
    
    Args:
        value: Value to check
        
    Returns:
        bool: True if numeric, False otherwise
    """
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False

# Configuration
def get_required_config_value(config: Dict[str, Any], key: str) -> Any:
    """Get a required value from the config dictionary.
    
    Args:
        config (dict): Configuration dictionary
        key (str): Key to look up
        
    Returns:
        Any: Value from config
        
    Raises:
        SystemExit: If key not found in config
    """
    value = config.get(key)
    if value is None:
        logger.error(f"Error: Required configuration '{key}' is missing. Please check your config file.")
        sys.exit(1)
    return value

def load_config() -> Dict[str, Any]:
    """Load configuration from YAML file based on SEARCH_SUBDIR environment variable.
    
    Returns:
        dict: Configuration dictionary with added base_dir
        
    Raises:
        SystemExit: If config file not found or invalid
    """
    search_subdir = os.getenv('SEARCH_SUBDIR')
    if not search_subdir:
        logger.error("Error: SEARCH_SUBDIR environment variable is not set.")
        sys.exit(1)

    # Get project root directory from the current file location
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, "searches", search_subdir, "config.yaml")
    logger.info(f"Search subdirectory: {search_subdir}")
    logger.info(f"Config file path: {config_path}")

    if not os.path.exists(config_path):
        logger.error(f"Error: Config file not found at {config_path}")
        sys.exit(1)

    try:
        with open(config_path, 'r') as config_file:
            config = yaml.safe_load(config_file)
        
        # Add base_dir to config
        config['base_dir'] = base_dir
        config['search_subdir'] = search_subdir
        
        logger.info("Config file loaded successfully.")
        return config
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML in config file: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error loading config file: {e}")
        sys.exit(1)
