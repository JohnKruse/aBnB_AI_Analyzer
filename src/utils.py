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
from datetime import datetime
import logging
import sys

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

def extract_coordinates(df: pd.DataFrame) -> pd.DataFrame:
    """Extract latitude and longitude from location data in DataFrame.
    
    Args:
        df (pandas.DataFrame): DataFrame containing location data
        
    Returns:
        pandas.DataFrame: DataFrame with extracted coordinates
    """
    if 'location' in df.columns:
        try:
            df['lat'] = df['location'].apply(lambda x: ast.literal_eval(str(x))['lat'] if pd.notna(x) else None)
            df['lng'] = df['location'].apply(lambda x: ast.literal_eval(str(x))['lng'] if pd.notna(x) else None)
        except Exception as e:
            print(f"Error extracting coordinates: {e}")
    return df

def extract_total_price(price: Dict[str, Any]) -> Optional[float]:
    """Extract total price from price dictionary.
    
    Args:
        price (dict): Dictionary containing price information
        
    Returns:
        float: Total price amount
    """
    if isinstance(price, dict):
        if 'total' in price and 'amount' in price['total']:
            return float(price['total']['amount'])
    return None

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
