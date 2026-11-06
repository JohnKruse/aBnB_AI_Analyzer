import os
import yaml
import sys
import pandas as pd
from pyairbnb.start import search_all, get_details, get_reviews as pyairbnb_get_reviews
from pyairbnb.price import UnavailableError
from pyairbnb import details
import ast
import re
import time
import json
import glob
import requests
from datetime import datetime, date, timedelta
from src.ai_utils import (
    query_openai_gptX_with_schema,
    extract_ai_rating,
    clean_ai_review_summary,
    get_ai_rating
)
from src.utils import (
    get_required_config_value,
    extract_rating_info,
    extract_coordinates,
    extract_total_price,
    is_already_cleaned,
    is_numeric,
    save_df,
    format_reviews,
    load_config
)
import logging
from requests.exceptions import RequestException, JSONDecodeError
from src.logging_config import setup_logger, MONITOR_LOG

# Set up logger
logger = setup_logger(__name__, MONITOR_LOG)

# Get the project root directory (where this script is located)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def parse_date(date_val):
    """Ensure date is a datetime.date instance as required by pyairbnb."""
    if isinstance(date_val, (date, datetime)):
        return date_val if isinstance(date_val, date) else date_val.date()
    if isinstance(date_val, str) and date_val:
        try:
            return datetime.strptime(date_val, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None

def process_room_details(df_results_filtered, currency, check_in, check_out, details_df):
    """Process details for a list of rooms."""
    logger.info(f"Processing details for {len(df_results_filtered)} rooms...")
    check_in_date = parse_date(check_in)
    check_out_date = parse_date(check_out)
    
    if 'room_id' not in details_df.columns:
        logger.warning("'room_id' column not found in details_df. Processing all rooms.")
        room_ids_to_process = set(df_results_filtered['room_id'])
    else:
        room_ids_to_process = set(df_results_filtered['room_id']) - set(details_df['room_id'])

    new_details_data = []
    failed_rooms = []
    total_rooms = len(room_ids_to_process)
    
    for idx, room_id in enumerate(room_ids_to_process, 1):
        logger.info(f"Processing room {idx}/{total_rooms} (ID: {room_id})")
        max_retries = 3
        retry_delay = 5  # seconds

        for attempt in range(max_retries):
            try:
                room_url = f"https://www.airbnb.com/rooms/{room_id}"
                data = get_details(room_url=room_url, currency=currency, check_in=check_in_date, check_out=check_out_date)
                data['room_id'] = room_id
                new_details_data.append(data)
                break  # Success, exit retry loop
            except UnavailableError as e:
                logger.warning(f"Room {room_id} is unavailable for requested dates: {e}")
                failed_rooms.append(room_id)
                break
            except Exception as e:
                logger.error(f"Error processing room {room_id} (Attempt {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    failed_rooms.append(room_id)

    if failed_rooms:
        logger.warning(f"Failed to process {len(failed_rooms)} rooms: {failed_rooms}")

    return new_details_data

def fetch_and_filter_properties(check_in, check_out, ne_lat, ne_long, sw_lat, sw_long, zoom_value, currency, min_price, default_max_price):
    """
    Fetch Airbnb listings and filter them based on price range.
    """
    max_retries = 3
    retry_delay = 5  # seconds

    for attempt in range(max_retries):
        try:
            logger.info(f"Fetching Airbnb listings (Attempt {attempt + 1}/{max_retries})...")
            results = search_all(
                check_in=str(check_in),
                check_out=str(check_out),
                ne_lat=float(ne_lat),
                ne_long=float(ne_long),
                sw_lat=float(sw_lat),
                sw_long=float(sw_long),
                zoom_value=int(round(float(zoom_value))),
                price_min=int(float(min_price)) if min_price else 0,
                price_max=int(float(default_max_price)) if default_max_price else 5000,
                currency=currency
            )
            
            if not results:
                logger.warning("No results returned from search_all")
                return pd.DataFrame()

            logger.info(f"Successfully fetched {len(results)} listings.")
            df_results = pd.DataFrame(results)
            df_results_deduped = df_results.drop_duplicates(subset='room_id').reset_index(drop=True)
            logger.info(f"After deduplication: {len(df_results_deduped)} unique listings.")

            # Normalize coordinates
            df_results_deduped = normalize_coordinates(df_results_deduped)

            df_results_deduped['total_price'] = df_results_deduped['price'].apply(extract_total_price)
            filtered_df = df_results_deduped[(df_results_deduped['total_price'] >= min_price) & (df_results_deduped['total_price'] <= default_max_price)]
            logger.info(f"After price filtering: {len(filtered_df)} listings remaining.")

            return filtered_df

        except Exception as e:
            import traceback
            logger.error(f"Error fetching Airbnb listings (Attempt {attempt + 1}/{max_retries}): {str(e)}\n{traceback.format_exc()}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error("Max retries reached. Unable to fetch Airbnb listings.")
                return pd.DataFrame()

def get_airbnb_reviews(room_id: str) -> list:
    """
    Get reviews for a room from Airbnb.
    
    Args:
        room_id (str): Room ID to get reviews for
        
    Returns:
        list: List of review dictionaries
    """
    try:
        room_url = f"https://www.airbnb.com/rooms/{room_id}"
        logger.info(f"Fetching reviews from {room_url}")
        
        reviews_data = pyairbnb_get_reviews(room_url, language="en")
        if reviews_data:
            return reviews_data

        # Fallback
        data, price_input, cookies = details.get(room_url, "en", "")
        if not data:
            logger.warning(f"No data found for room {room_id}")
            return []
            
        product_id = price_input.get("product_id")
        api_key = price_input.get("api_key")
        if not product_id or not api_key:
            logger.warning(f"Missing product_id or api_key for room {room_id}")
            return []
            
        reviews_url = f"https://www.airbnb.com/api/v3/PdpReviews?key={api_key}&locale=en&productId={product_id}&_format=for_p3"
        response = requests.get(reviews_url, headers={"User-Agent": "Mozilla/5.0"})
        if not response.ok:
            logger.warning(f"Failed to get reviews for room {room_id}: {response.status_code}")
            return []
            
        return response.json().get("reviews", [])
        
    except Exception as e:
        logger.error(f"Error getting reviews for room {room_id}: {str(e)}")
        return []

def download_reviews_for_room(room_id: str) -> dict:
    """Download all reviews for a room with retries.
    
    Args:
        room_id (str): Room ID to download reviews for
        
    Returns:
        dict: Dictionary with room_id, reviews list, and formatted reviews text
    """
    max_retries = 3
    retry_delay = 5  # seconds
    
    for attempt in range(max_retries):
        try:
            reviews = get_airbnb_reviews(room_id)
            reviews_text = format_reviews(reviews) if reviews else ""
            return {'room_id': room_id, 'reviews': reviews, 'reviews_text': reviews_text}
                
        except Exception as e:
            logger.error(f"Error downloading reviews for room {room_id} (Attempt {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Max retries reached. Unable to download reviews for room {room_id}.")
                return {'room_id': room_id, 'reviews': [], 'reviews_text': ''}

    return {'room_id': room_id, 'reviews': [], 'reviews_text': ''}

def update_reviews_df(reviews_df: pd.DataFrame, results_df: pd.DataFrame, details_df: pd.DataFrame) -> pd.DataFrame:
    """Update reviews DataFrame with new reviews and additional details.
    
    Args:
        reviews_df (pd.DataFrame): Existing reviews DataFrame
        results_df (pd.DataFrame): DataFrame with room results
        details_df (pd.DataFrame): DataFrame with room details
        
    Returns:
        pd.DataFrame: Updated reviews DataFrame
    """
    # If reviews_df is empty, we'll download all reviews
    if reviews_df.empty:
        rooms_to_update = results_df['room_id']
    else:
        # Only download reviews for new rooms
        rooms_to_update = set(results_df['room_id']) - set(reviews_df['room_id'])

    new_reviews = []
    for room_id in rooms_to_update:
        try:
            # Get additional details from details_df
            room_details = details_df[details_df['room_id'] == room_id]
            highlights = ''
            location_descriptions = ''
            description = ''
            
            if not room_details.empty:
                highlights = room_details['highlights'].iloc[0] if 'highlights' in room_details.columns else ''
                location_descriptions = room_details['location_descriptions'].iloc[0] if 'location_descriptions' in room_details.columns else ''
                description = room_details['description'].iloc[0] if 'description' in room_details.columns else ''
            
            # Check if reviews already exist in details_df
            reviews_text = ""
            existing_reviews = None
            if not room_details.empty and 'reviews' in room_details.columns:
                raw_rev = room_details['reviews'].iloc[0]
                if isinstance(raw_rev, list):
                    existing_reviews = raw_rev
                elif isinstance(raw_rev, str) and raw_rev.strip() and raw_rev.strip() != '[]':
                    try:
                        existing_reviews = ast.literal_eval(raw_rev)
                    except Exception:
                        try:
                            existing_reviews = json.loads(raw_rev)
                        except Exception:
                            existing_reviews = None

            if existing_reviews and isinstance(existing_reviews, list) and len(existing_reviews) > 0:
                reviews_text = format_reviews(existing_reviews)
            else:
                # Fallback to downloading reviews only if not already in details_df
                room_reviews = download_reviews_for_room(room_id)
                reviews_text = room_reviews.get('reviews_text', '')
            
            # Always create an entry, even if there are no reviews
            if not reviews_text:
                reviews_text = "No reviews available for this property."
            
            # Append additional details to reviews_text
            additional_info = f"\nHighlights: {highlights}\nLocation Description: {location_descriptions}\nDescription: {description}"
            reviews_text += additional_info
            
            new_reviews.append({'room_id': room_id, 'reviews_text': reviews_text})
            
        except Exception as e:
            logger.error(f"Error processing reviews for room {room_id}: {str(e)}")
            # Still create an entry even if there was an error
            new_reviews.append({
                'room_id': room_id, 
                'reviews_text': f"Error retrieving reviews: {str(e)}\nNo reviews available for this property."
            })

    # Append new reviews to the existing DataFrame
    if new_reviews:
        new_reviews_df = pd.DataFrame(new_reviews)
        reviews_df = pd.concat([reviews_df, new_reviews_df], ignore_index=True)

    return reviews_df

def format_reviews(reviews: list) -> str:
    """Format a list of reviews into a single string.
    
    Args:
        reviews (list): List of review dictionaries
        
    Returns:
        str: Formatted reviews string
    """
    if not isinstance(reviews, list):
        return ""
    formatted_reviews = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        date_str = (
            review.get('createdAt')
            or review.get('created_at')
            or review.get('localizedDate')
            or review.get('date')
            or 'N/A'
        )
        comments = review.get('comments') or ''
        rating = review.get('rating') or 'N/A'
        formatted_reviews.append(f"{date_str} {comments} Rating: {rating}")
    return '; '.join(formatted_reviews)

def clean_ai_review_summary(review_summary):
    """
    Clean and format AI review summary text.
    
    Args:
        review_summary (str): Raw review summary text
        
    Returns:
        str: Cleaned and formatted review summary
    """
    logger.debug("Cleaning AI review summary")
    
    if not review_summary:
        logger.warning("Empty review summary provided")
        return "No summary available"
        
    try:
        # Convert the string representation of the dictionary to an actual dictionary
        review_dict = ast.literal_eval(review_summary)
        
        # Extract the review text after the colon (the first value in the dictionary)
        summary_text = list(review_dict.values())[0]
        
        logger.debug("Successfully cleaned AI review summary")
        return summary_text
        
    except (ValueError, SyntaxError) as e:
        logger.error(f"Error parsing review summary: {str(e)}")
        return "Invalid format or no summary available"
    except Exception as e:
        logger.error(f"Unexpected error cleaning review summary: {str(e)}")
        return "Error processing summary"

def extract_ai_rating(ai_response):
    """
    Extract numerical rating from AI response.
    
    Args:
        ai_response (str): AI response containing rating
        
    Returns:
        float: Extracted rating value or None if not found
    """
    if not ai_response:
        logger.warning("Empty AI response received")
        return None
        
    try:
        # First try to parse as JSON
        if isinstance(ai_response, str):
            try:
                data = json.loads(ai_response)
                if isinstance(data, dict) and 'AI_rating' in data:
                    return float(data['AI_rating'])
            except json.JSONDecodeError:
                pass
        
        # Try regex pattern matching as fallback
        pattern = r'"AI_rating":\s*(\d+(?:\.\d+)?)'
        match = re.search(pattern, str(ai_response))
        if match:
            return float(match.group(1))
            
        # Match explicit rating: e.g. "Rating: 4.5", "4.5 / 5", or a leading number
        match_num = re.search(r'(?:rating|score)?\s*:?\s*\b([1-5](?:\.\d+)?)\b', str(ai_response), re.IGNORECASE)
        if match_num:
            return float(match_num.group(1))

        logger.warning(f"Could not extract AI rating from response: {ai_response}")
        return None
        
    except Exception as e:
        logger.error(f"Error extracting AI rating: {str(e)}")
        logger.error(f"Problematic AI response: {ai_response}")
        return None

def is_numeric(value):
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False

def load_or_create(file_path: str) -> pd.DataFrame:
    """Load a DataFrame from a CSV file or create an empty one if it doesn't exist.
    
    Args:
        file_path (str): Path to the CSV file
        
    Returns:
        pandas.DataFrame: The loaded or newly created DataFrame
    """
    if os.path.exists(file_path):
        return pd.read_csv(file_path)
    else:
        return pd.DataFrame()

def load_or_create_reviews_df(file_path: str) -> pd.DataFrame:
    """Load a reviews DataFrame from a CSV file or create an empty one if it doesn't exist.
    
    Args:
        file_path (str): Path to the CSV file
        
    Returns:
        pandas.DataFrame: The loaded or newly created DataFrame
    """
    if os.path.exists(file_path):
        return pd.read_csv(file_path)
    else:
        return pd.DataFrame(columns=['room_id', 'reviews_text'])

def update_details_df(details_df: pd.DataFrame, new_details_data: pd.DataFrame) -> pd.DataFrame:
    """Update details DataFrame with new data, removing duplicates.
    
    Args:
        details_df (pd.DataFrame): Existing details DataFrame
        new_details_data (pd.DataFrame or list): New data to add
        
    Returns:
        pd.DataFrame: Updated DataFrame with duplicates removed
    """
    # Convert new_details_data to a DataFrame if it's not already
    if not isinstance(new_details_data, pd.DataFrame):
        new_details_df = pd.DataFrame(new_details_data)
    else:
        new_details_df = new_details_data

    # Concatenate the existing details_df with the new data
    updated_df = pd.concat([details_df, new_details_df], ignore_index=True)

    # Remove duplicates based on 'room_id', keeping the latest entry
    updated_df = updated_df.drop_duplicates(subset='room_id', keep='last')

    return updated_df

def normalize_coordinates(df):
    """
    Extract coordinates from nested dictionaries.
    
    Note: The gobnb module intentionally uses 'longitud' (not 'longitude') as the key name
    in its coordinate dictionaries. This is not a spelling error and should not be changed.
    """
    if 'coordinates' in df.columns:
        df['latitude'] = df['coordinates'].apply(lambda x: x.get('latitude') if isinstance(x, dict) else None)
        df['longitude'] = df['coordinates'].apply(lambda x: x.get('longitud') if isinstance(x, dict) else None)  # 'longitud' is the correct key name
    return df

def get_room_details(room_id: str, currency: str, check_in: str, check_out: str):
    """
    Get details for a room from Airbnb.
    """
    max_retries = 3
    retry_delay = 5  # seconds
    new_details_data = []
    check_in_date = parse_date(check_in)
    check_out_date = parse_date(check_out)

    try:
        for attempt in range(max_retries):
            try:
                room_url = f"https://www.airbnb.com/rooms/{room_id}"
                data = get_details(room_url=room_url, currency=currency, check_in=check_in_date, check_out=check_out_date)
                if data:
                    data['room_id'] = room_id
                    new_details_data.append(data)
                    break  # Success, exit retry loop
            except UnavailableError as e:
                logger.warning(f"Room {room_id} is unavailable for requested dates: {e}")
                return None
            except Exception as e:
                logger.error(f"Error processing room {room_id} (Attempt {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"Failed to get details for room {room_id} after {max_retries} attempts")
                    return None

        if not new_details_data:
            logger.warning(f"No details found for room {room_id}")
            return None

        return new_details_data[0]

    except Exception as e:
        logger.error(f"Error getting details for room {room_id}: {str(e)}")
        return None

def main():
    try:
        logger.info("Starting abnb_monitor.py")
        
        # Load configuration
        search_subdir = os.getenv('SEARCH_SUBDIR')
        if not search_subdir:
            logger.error("SEARCH_SUBDIR environment variable not set")
            sys.exit(1)
            
        config_path = os.path.join('searches', search_subdir, 'config.yaml')
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        logger.info("Configuration loaded successfully")
        
        # Set default AI configuration if not present
        if 'ai_config' not in config:
            logger.warning("AI configuration not found in config.yaml, using defaults")
            config['ai_config'] = {
                'model_name': 'gpt-3.5-turbo',
                'api_key': os.getenv('OPENAI_API_KEY'),
                'temperature': 0.3,
                'max_tokens': 2000
            }
        
        # Validate OpenAI API key
        if not config['ai_config'].get('api_key'):
            logger.warning("OpenAI API key not found, AI features will be disabled")
            config['ai_config'] = None
        
        # Create output directory if it doesn't exist
        output_dir = os.path.join('searches', search_subdir, 'output_data')
        logger.info(f"Using output directory: {output_dir}")
        os.makedirs(output_dir, exist_ok=True)

        # Download search results
        logger.info("Downloading search results...")
        results_df = fetch_and_filter_properties(
            config['check_in'],
            config['check_out'],
            config['ne_lat'],
            config['ne_long'],
            config['sw_lat'],
            config['sw_long'],
            config['zoom_value'],
            config['currency'],
            config.get('min_price', 0),
            config.get('max_price', 1000000)
        )

        if results_df.empty:
            logger.error("Error: No search results found. Please check your search parameters.")
            sys.exit(1)

        logger.info(f"Columns in results_df after downloading: {list(results_df.columns)}")
        logger.info(f"Number of listings found: {len(results_df)}")
        logger.info("First few rows of results_df:")
        logger.info(results_df.head().to_string())

        # Ensure 'room_id' column exists and is named correctly
        if 'room_id' not in results_df.columns:
            # Check if there's a column that might represent room_id (e.g., 'id', 'listing_id')
            potential_id_columns = [col for col in results_df.columns if 'id' in col.lower()]
            if potential_id_columns:
                results_df['room_id'] = results_df[potential_id_columns[0]]
                logger.info(f"Renamed '{potential_id_columns[0]}' to 'room_id'")
            else:
                logger.error("Error: Could not find a suitable column for 'room_id'")
                return

        # Extract Airbnb rating and review count
        results_df['Airbnb_rating'], results_df['Airbnb_review_count'] = zip(*results_df['rating'].apply(extract_rating_info))

        # Convert Airbnb_rating to float and Airbnb_review_count to int
        results_df['Airbnb_rating'] = pd.to_numeric(results_df['Airbnb_rating'], errors='coerce')
        results_df['Airbnb_review_count'] = pd.to_numeric(results_df['Airbnb_review_count'], errors='coerce').astype('Int64')

        # Load or create DataFrames
        logger.info("Loading or creating DataFrames...")
        details_df = load_or_create(os.path.join(output_dir, 'details_df.csv'))
        reviews_df = load_or_create_reviews_df(os.path.join(output_dir, 'reviews_df.csv'))

        # Process room details first
        logger.info("Processing room details...")
        new_details_data = []
        failed_rooms = []
        
        if 'room_id' in details_df.columns and not details_df.empty:
            existing_room_ids = set(details_df['room_id'].astype(str))
            room_ids_to_process = [rid for rid in results_df['room_id'] if str(rid) not in existing_room_ids]
            logger.info(f"Cache hit: {len(existing_room_ids)} rooms already in details_df. Fetching {len(room_ids_to_process)} remaining.")
        else:
            room_ids_to_process = list(results_df['room_id'])

        total_rooms = len(room_ids_to_process)
        for idx, room_id in enumerate(room_ids_to_process, 1):
            logger.info(f"Processing room {idx}/{total_rooms} (ID: {room_id})")
            room_details = get_room_details(room_id, config['currency'], config['check_in'], config['check_out'])
            if room_details is None:
                failed_rooms.append(room_id)
            else:
                new_details_data.append(room_details)

        if failed_rooms:
            logger.warning(f"Failed to process {len(failed_rooms)} rooms: {failed_rooms}")

        details_df = update_details_df(details_df, new_details_data)
        details_df = extract_coordinates(details_df)

        # Save updated details_df
        logger.info("Saving updated details_df...")
        save_df(details_df, os.path.join(output_dir, 'details_df.csv'))

        # Now update reviews with the updated details_df
        logger.info("Updating reviews DataFrame...")
        reviews_df = update_reviews_df(reviews_df, results_df, details_df)

        # After processing room details
        if 'room_id' not in details_df.columns:
            logger.error("Error: 'room_id' column is missing from details_df after processing.")
            logger.info("Columns in details_df:", details_df.columns)
            return  # Exit the function if 'room_id' is missing

        # Merge results_df and details_df
        logger.info("Merging results_df and details_df...")
        merged_df = pd.merge(results_df, details_df, on='room_id', how='left', suffixes=('', '_details'))

        logger.info(f"Columns in reviews_df: {list(reviews_df.columns)}")
        logger.info(f"Number of reviews: {len(reviews_df)}")
        
        # Check for OpenAI API key
        if not os.getenv('OPENAI_API_KEY'):
            logger.info("OPENAI_API_KEY not configured. Skipping AI review summarization and rating.")
            if 'AI_review_summary' not in reviews_df.columns:
                reviews_df['AI_review_summary'] = 'AI features not enabled (no OPENAI_API_KEY)'
            if 'AI_rating' not in reviews_df.columns:
                reviews_df['AI_rating'] = None
        else:
            if reviews_df.empty:
                logger.info("reviews_df is empty. Skipping AI rating.")
            else:
                reviews_df = get_ai_rating(reviews_df, ['reviews_text'], config.get('ai_review_summary', {}), 'AI_review_summary', skip_existing=True)
                logger.info(f"Reviews DataFrame after AI review summary:\n{reviews_df.head()}")

            # Reformat AI review summary
            reviews_df['AI_review_summary'] = reviews_df['AI_review_summary'].apply(
                lambda x: x if is_already_cleaned(str(x)) else clean_ai_review_summary(x)
            )

            # AI Rating
            logger.info("Generating AI ratings...")
            reviews_df = get_ai_rating(reviews_df, ['AI_review_summary'], config.get('ai_rating', {}), 'AI_rating', skip_existing=True)

            # Extract numerical AI rating only for non-numeric values
            reviews_df['AI_rating'] = reviews_df['AI_rating'].apply(lambda x: x if is_numeric(x) else extract_ai_rating(x))
            reviews_df['AI_rating'] = pd.to_numeric(reviews_df['AI_rating'], errors='coerce')

        # Merge reviews data into merged_df
        logger.info("Merging reviews data into merged_df...")
        # Drop overlapping columns from merged_df (except room_id) to prevent _x/_y column collisions
        overlap_cols = [c for c in reviews_df.columns if c != 'room_id' and c in merged_df.columns]
        if overlap_cols:
            logger.info(f"Dropping overlapping columns from merged_df before merge: {overlap_cols}")
            merged_df = merged_df.drop(columns=overlap_cols)

        merged_df = pd.merge(merged_df, reviews_df, on='room_id', how='left')

        # Check for null AI ratings
        if 'AI_rating' in merged_df.columns:
            null_ai_ratings = merged_df['AI_rating'].isnull().sum()
            if null_ai_ratings > 0:
                logger.warning(f"{null_ai_ratings} rows have null AI_rating values")
        else:
            merged_df['AI_rating'] = np.nan

        # Save all DataFrames at the end
        logger.info("Saving all DataFrames...")
        save_df(results_df, os.path.join(output_dir, 'results_df.csv'))
        save_df(details_df, os.path.join(output_dir, 'details_df.csv'))
        save_df(reviews_df, os.path.join(output_dir, 'reviews_df.csv'))
        save_df(merged_df, os.path.join(output_dir, 'merged_df.csv'))

        # Save failed rooms
        with open(os.path.join(output_dir, "failed_rooms.txt"), "w") as f:
            for room_id in failed_rooms:
                f.write(f"{room_id}\n")

        logger.info(f"Processing complete. CSV files have been saved in the '{output_dir}' directory.")
        logger.info(f"Total listings: {len(results_df)}")
        logger.info(f"New details fetched: {len(new_details_data)}")
        logger.info(f"Failed rooms: {len(failed_rooms)}")
        logger.info(f"Failed room IDs saved to {output_dir}/failed_rooms.txt")

        # Add this before calling get_ai_rating
        logger.info(f"Columns in merged_df: {merged_df.columns}")
        logger.info(f"First few rows of merged_df:\n{merged_df.head()}")

        logger.info("########################################################")
        logger.info("Airbnb Monitor has finished running")
        logger.info("########################################################")

    except Exception as e:
        logger.error(f"Fatal error in main: {str(e)}", exc_info=True)
        raise

if __name__ == "__main__":
    main()