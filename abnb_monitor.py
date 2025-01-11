import os
import yaml
import sys
import pandas as pd
import airbnb
import ast
import time
import json
import glob
from datetime import datetime, timedelta
import gobnb
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
import requests
from requests.exceptions import RequestException, JSONDecodeError
from src.logging_config import setup_logger, MONITOR_LOG

# Set up logger
logger = setup_logger(__name__, MONITOR_LOG)

# Get the project root directory (where this script is located)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def process_room_details(df_results_filtered, currency, check_in, check_out, details_df):
    logger.info(f"Processing details for {len(df_results_filtered)} rooms...")
    
    if 'room_id' not in details_df.columns:
        logger.warning("'room_id' column not found in details_df. Processing all rooms.")
        room_ids_to_process = set(df_results_filtered['room_id'])
    else:
        room_ids_to_process = set(df_results_filtered['room_id']) - set(details_df['room_id'])

    new_details_data = []
    failed_rooms = []
    total_rooms = len(room_ids_to_process)
    processed_count = 0

    logger.info(f"\n{'='*50}")
    logger.info(f"Downloading details for {total_rooms} rooms")
    logger.info(f"{'='*50}\n")

    for room_id in room_ids_to_process:
        max_retries = 3
        retry_delay = 5  # seconds

        for attempt in range(max_retries):
            try:
                data = gobnb.Get_from_room_id(room_id, currency, check_in, check_out, "")
                data['room_id'] = room_id
                new_details_data.append(data)
                break  # Success, exit retry loop
            except Exception as e:
                import traceback
                logger.error(f"Error processing room {room_id} (Attempt {attempt + 1}/{max_retries}): {str(e)}\n{traceback.format_exc()}")
                if attempt < max_retries - 1:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"Max retries reached. Unable to process room {room_id}.")
                    failed_rooms.append(room_id)
        
        processed_count += 1
        progress = (processed_count / total_rooms) * 100
        logger.info(f"\rDownloading details: [{processed_count}/{total_rooms}] {progress:.2f}% complete")

    logger.info("\n")  # Move to the next line after the progress bar
    logger.info(f"{'='*50}")
    logger.info(f"Download complete. Processed {processed_count} rooms.")
    logger.info(f"Successfully fetched details for {len(new_details_data)} rooms.")
    logger.info(f"Failed to fetch details for {len(failed_rooms)} rooms.")
    logger.info(f"{'='*50}\n")

    return new_details_data, failed_rooms

def fetch_and_filter_properties(check_in, check_out, ne_lat, ne_long, sw_lat, sw_long, zoom_value, currency, min_price, default_max_price):
    """
    Fetch Airbnb listings and filter them based on price range.
    
    Args:
        check_in (str): Check-in date
        check_out (str): Check-out date
        ne_lat (float): Northeast latitude
        ne_long (float): Northeast longitude
        sw_lat (float): Southwest latitude
        sw_long (float): Southwest longitude
        zoom_value (float): Map zoom value
        currency (str): Currency code
        min_price (float): Minimum price filter
        default_max_price (float): Maximum price filter
        
    Returns:
        pandas.DataFrame: Filtered DataFrame of listings
    """
    max_retries = 3
    retry_delay = 5  # seconds

    for attempt in range(max_retries):
        try:
            logger.info(f"Fetching Airbnb listings (Attempt {attempt + 1}/{max_retries})...")
            results = gobnb.Search_all(check_in, check_out, ne_lat, ne_long, sw_lat, sw_long, zoom_value, currency, "")
            
            if not results:
                logger.warning("No results returned from gobnb.Search_all()")
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

def get_airbnb_reviews(room_id: str, offset: int = 0, limit: int = 50) -> dict:
    """Get reviews for a room from Airbnb.
    
    Args:
        room_id (str): Room ID to get reviews for
        offset (int): Starting offset for reviews
        limit (int): Maximum number of reviews to fetch
        
    Returns:
        dict: Dictionary containing room_id, reviews list, and concatenated reviews text
    """
    api = airbnb.Api()  # Use the airbnb module's Api class
    try:
        response = api.get_reviews(room_id, offset=offset, limit=limit)
        
        # Check if the response is a string (which might indicate an error message)
        if isinstance(response, str):
            logger.warning(f"Unexpected string response for room {room_id}: {response}")
            return {'room_id': room_id, 'reviews': [], 'reviews_text': ''}

        reviews = response.get('reviews', [])
        
        review_data = []
        reviews_text = []
        for review in reviews:
            created_at = review.get('created_at', 'N/A')
            comments = review.get('comments', 'N/A')
            rating = review.get('rating', 'N/A')
            
            review_data.append({
                "created_at": created_at,
                "comments": comments,
                "rating": rating,
            })
            
            review_text = f"{created_at} {comments} Rating: {rating}"
            reviews_text.append(review_text)
        
        concatenated_reviews = '; '.join(reviews_text)
        
        return {
            'room_id': room_id,
            'reviews': review_data,
            'reviews_text': concatenated_reviews
        }
    except JSONDecodeError as e:
        logger.error(f"JSONDecodeError for room {room_id}: {str(e)}")
        logger.error(f"Response content: {api.last_response.content}")
        return {'room_id': room_id, 'reviews': [], 'reviews_text': ''}
    except RequestException as e:
        logger.error(f"RequestException for room {room_id}: {str(e)}")
        return {'room_id': room_id, 'reviews': [], 'reviews_text': ''}
    except Exception as e:
        import traceback
        logger.error(f"Unexpected error for room {room_id}: {str(e)}\n{traceback.format_exc()}")
        return {'room_id': room_id, 'reviews': [], 'reviews_text': ''}

def download_reviews_for_room(room_id: str) -> list:
    """Download all reviews for a room with retries.
    
    Args:
        room_id (str): Room ID to download reviews for
        
    Returns:
        list: List of review dictionaries
    """
    all_reviews = []
    offset = 0
    limit = 50
    max_retries = 3
    retry_delay = 5  # seconds
    
    for attempt in range(max_retries):
        try:
            reviews = get_airbnb_reviews(room_id, offset, limit)
            if not reviews['reviews']:  # Check the 'reviews' key in the returned dictionary
                break
            all_reviews.extend(reviews['reviews'])  # Extend with the 'reviews' list
            offset += limit
            
            if len(reviews['reviews']) < limit:
                break
        except Exception as e:
            import traceback
            logger.error(f"Error downloading reviews for room {room_id} (Attempt {attempt + 1}/{max_retries}): {str(e)}\n{traceback.format_exc()}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Max retries reached. Unable to download reviews for room {room_id}.")
                break
    
    return all_reviews

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
            
            # Download reviews for this room
            room_reviews = download_reviews_for_room(room_id)
            reviews_text = format_reviews(room_reviews)
            
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
    formatted_reviews = []
    for review in reviews:
        formatted_review = f"{review['created_at']} {review['comments']} Rating: {review['rating']}"
        formatted_reviews.append(formatted_review)
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
    try:
        # Use regex to find the pattern {"AI_rating":X} or {"AI_rating":X.X}
        # where X is an integer and X.X is a number with one decimal place
        match = re.search(r'{"AI_rating":(\d+(?:\.\d)?)}', str(ai_response))
        if match:
            return float(match.group(1))
    except Exception as e:
        logger.error(f"Error extracting AI rating: {e}")
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

def main():
    try:
        logger.info("Starting abnb_monitor.py")
        
        # Load configuration
        config = load_config()
        logger.info("Configuration loaded successfully")
        
        # Extract configuration values
        base_dir = get_required_config_value(config, 'base_dir')
        currency = get_required_config_value(config, 'currency')
        check_in = get_required_config_value(config, 'check_in')
        check_out = get_required_config_value(config, 'check_out')
        ne_lat = get_required_config_value(config, 'ne_lat')
        ne_long = get_required_config_value(config, 'ne_long')
        sw_lat = get_required_config_value(config, 'sw_lat')
        sw_long = get_required_config_value(config, 'sw_long')
        zoom_value = get_required_config_value(config, 'zoom_value')
        default_max_price = get_required_config_value(config, 'default_max_price')
        min_price = config.get('min_price', 0)
        search_subdir = os.getenv('SEARCH_SUBDIR')

        # Ensure the output_data directory exists
        output_dir = os.path.join(base_dir, 'searches', search_subdir, 'output_data')
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Using output directory: {output_dir}")

        # Download search results
        logger.info("Downloading search results...")
        results_df = fetch_and_filter_properties(check_in, check_out, ne_lat, ne_long, sw_lat, sw_long, zoom_value, currency, min_price, default_max_price)

        if results_df.empty:
            logger.error("Error: No search results found. Please check your search parameters.")
            print("Error: No search results found. Please check your search parameters.")
            return

        logger.info("Columns in results_df after downloading:", results_df.columns)
        logger.info("Number of listings found:", len(results_df))
        logger.info("First few rows of results_df:")
        logger.info(results_df.head())

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
        new_details_data, failed_rooms = process_room_details(results_df, currency, check_in, check_out, details_df)
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

        #### AI Reviews summarization and rating ####
        # use get_ai_rating(df, columns_to_concatenate, ai_config, new_column_name)

        logger.info(f"Columns in reviews_df: {list(reviews_df.columns)}")
        logger.info(f"Number of reviews: {len(reviews_df)}")
        
        if reviews_df.empty:
            logger.info("reviews_df is empty. Skipping AI rating.")
        else:
            reviews_df = get_ai_rating(reviews_df, ['reviews_text'], config['ai_review_summary'], 'AI_review_summary', skip_existing=True)
            logger.info("Reviews DataFrame after AI rating:")
            logger.info(f"Head of reviews_df:\n{reviews_df.head()}")

        # Reformat AI review summary
        reviews_df['AI_review_summary'] = reviews_df['AI_review_summary'].apply(
            lambda x: x if is_already_cleaned(str(x)) else clean_ai_review_summary(x)
        )

        # AI Rating
        logger.info("Generating AI ratings...")
        reviews_df = get_ai_rating(reviews_df, ['AI_review_summary'], config['ai_rating'], 'AI_rating', skip_existing=True)

        # Extract numerical AI rating only for non-numeric values
        reviews_df['AI_rating'] = reviews_df['AI_rating'].apply(lambda x: x if is_numeric(x) else extract_ai_rating(x))

        # Convert the column to float, replacing any remaining non-numeric values with NaN
        reviews_df['AI_rating'] = pd.to_numeric(reviews_df['AI_rating'], errors='coerce')

        # Merge reviews data into merged_df
        logger.info("Merging reviews data into merged_df...")
        merged_df = pd.merge(merged_df, reviews_df, on='room_id', how='left')

        # Verify AI_rating column
        null_ai_ratings = merged_df['AI_rating'].isnull().sum()
        if null_ai_ratings > 0:
            logger.warning(f"{null_ai_ratings} rows have null AI_rating values")
        else:
            logger.info("All rows have valid AI_rating values")

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
        logger.info("Columns in merged_df:", merged_df.columns)
        logger.info("First few rows of merged_df:")
        logger.info(merged_df.head())

        logger.info("########################################################")
        logger.info("Airbnb Monitor has finished running")
        logger.info("########################################################")

    except Exception as e:
        logger.exception("Fatal error in main:")
        raise

if __name__ == "__main__":
    main()