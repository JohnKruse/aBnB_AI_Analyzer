import streamlit as st
import pandas as pd
import folium
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium
import os
from datetime import datetime, date
import ast
import yaml
import numpy as np
import csv
import re
import atexit
import sys
from src.utils import load_config
import logging
from src.logging_config import setup_logger, REVIEW_APP_LOG

# Set up logger
logger = setup_logger(__name__, REVIEW_APP_LOG)

# Get the project root directory (where this script is located)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Set the page layout to wide
st.set_page_config(layout="wide")

st.markdown("""
<style>
    .stSlider [data-baseweb="slider"] { max-width: 300px; }
    .stCheckbox { margin-bottom: 0px; }
    .stContainer { padding-top: 1rem; padding-bottom: 1rem; }
</style>
""", unsafe_allow_html=True)

def clean_description(description):
    """Clean HTML description text by removing <br /> tags and extra whitespace."""
    if not isinstance(description, str):
        return ""
    # Remove <br /> tags
    description = description.replace("<br />", "\n")
    # Remove extra whitespace
    description = re.sub(r'\s+', ' ', description).strip()
    return description

def format_ai_review_summary(summary):
    """Format AI review summary by replacing ### with newlines and cleaning whitespace."""
    if not isinstance(summary, str):
        return ""
    # Replace ### with newlines
    summary = summary.replace("###", "\n")
    # Clean up whitespace within each line while preserving newlines
    lines = summary.split('\n')
    cleaned_lines = [re.sub(r'\s+', ' ', line).strip() for line in lines]
    return '\n'.join(line for line in cleaned_lines if line)

def highlight_text(text, keywords):
    """Highlight specified keywords in text using HTML span tags."""
    if not isinstance(text, str) or not keywords:
        return text
    
    highlighted_text = text
    for keyword in keywords:
        if keyword.strip():
            pattern = re.compile(re.escape(keyword.strip()), re.IGNORECASE)
            highlighted_text = pattern.sub(
                lambda m: f'<span style="background-color: yellow">{m.group()}</span>',
                highlighted_text
            )
    return highlighted_text

def format_badges(badges):
    """Format badges string by converting string representation of list to comma-separated string."""
    if not badges or badges == "[]":
        return ""
    try:
        if isinstance(badges, str):
            badges_list = ast.literal_eval(badges)
        else:
            badges_list = badges
        return ", ".join(badges_list)
    except:
        return str(badges)

def format_date(date_value):
    """Format date string to consistent YYYY-MM-DD format."""
    if not date_value:
        return ""
    
    if isinstance(date_value, datetime):
        return date_value.strftime("%Y-%m-%d")
    
    try:
        # Try parsing with various formats
        for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"]:
            try:
                return datetime.strptime(str(date_value), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return str(date_value)
    except:
        return str(date_value)

def format_sub_description(sub_description):
    """Format sub-description by extracting title and items from dictionary representation."""
    if not sub_description or sub_description == "[]":
        return ""
    
    try:
        if isinstance(sub_description, str):
            sub_desc_dict = ast.literal_eval(sub_description)
        else:
            sub_desc_dict = sub_description
            
        if isinstance(sub_desc_dict, list):
            formatted_items = []
            for item in sub_desc_dict:
                if isinstance(item, dict):
                    title = item.get('title', '')
                    items = item.get('items', [])
                    if title and items:
                        formatted_items.append(f"{title}: {', '.join(items)}")
            return "\n".join(formatted_items)
        return str(sub_description)
    except:
        return str(sub_description)

def extract_price(price_str):
    """Extract numeric price value from price string."""
    if not price_str:
        return None
    try:
        # Remove currency symbols and convert to float
        price = re.sub(r'[^\d.]', '', str(price_str))
        return float(price) if price else None
    except:
        return None

def extract_total_price(price_str):
    """Extract total price value from price string."""
    if not price_str:
        return None
    try:
        # Remove currency symbols and convert to float
        price = re.sub(r'[^\d.]', '', str(price_str))
        return float(price) if price else None
    except:
        return None

def extract_review_info(rating_str):
    """Extract rating and review count from rating string."""
    if not rating_str or rating_str == "nan":
        return None, None
    
    try:
        if isinstance(rating_str, str):
            rating_dict = ast.literal_eval(rating_str)
        else:
            rating_dict = rating_str
            
        rating = rating_dict.get('value', None)  # Changed from 'rating' to 'value'
        review_count = rating_dict.get('reviewCount', None)
        
        return rating, review_count
    except:
        return None, None

def load_overlay(overlay_file):
    """Load map overlay data from CSV file."""
    if not overlay_file or not os.path.exists(overlay_file):
        print(f"Overlay file not found: {overlay_file}")
        return None
    
    try:
        overlay_df = pd.read_csv(overlay_file)
        if 'latitude' in overlay_df.columns and 'longitude' in overlay_df.columns:
            return overlay_df
        else:
            print(f"Overlay file {overlay_file} missing required columns")
            return None
    except Exception as e:
        print(f"Error loading overlay file {overlay_file}: {str(e)}")
        return None

def load_results_and_details(config):
    logger.info("Loading results and details data...")
    full_path = os.path.join(config['base_dir'], 'searches', config['output_subdir'])
    logger.info("Full path to results and details subdir: %s", full_path)

    merged_data_file = os.path.join(full_path, 'merged_df.csv')

    if not os.path.exists(merged_data_file):
        logger.error("File %s does not exist.", merged_data_file)
        st.error(f"File {merged_data_file} does not exist.")
        st.stop()

    merged_df = pd.read_csv(merged_data_file, dtype={'room_id': str})
    
    logger.info("Loaded merged data from %s: %d rows, %d columns", merged_data_file, len(merged_df), len(merged_df.columns))
    logger.info("Columns in merged data: %s", merged_df.columns.tolist())
    logger.info("Data types: %s", merged_df.dtypes.to_dict())
    
    # Log any NaN values in key columns
    key_columns = ['room_id', 'name', 'price_total', 'rating']
    for col in key_columns:
        if col in merged_df.columns:
            nan_count = merged_df[col].isna().sum()
            if nan_count > 0:
                logger.warning("Found %d NaN values in column '%s'", nan_count, col)

    return merged_df

def remove_duplicates(df):
    duplicate_room_ids = df[df.duplicated(subset='room_id', keep=False)]
    if not duplicate_room_ids.empty:
        print(f"Duplicate room_id values found and removed: {duplicate_room_ids['room_id'].tolist()}")
        # st.warning(f"Duplicate room_id values found and removed: {duplicate_room_ids['room_id'].tolist()}")
        df = df.drop_duplicates(subset='room_id')
    return df

def calculate_average_coordinates(df):
    avg_latitude = df["latitude"].mean() if "latitude" in df.columns else 41.14
    avg_longitude = df["longitude"].mean() if "longitude" in df.columns else -104.82
    return avg_latitude, avg_longitude

def load_ratings(config):
    """Load user ratings from CSV file."""
    logger.info("Loading user ratings")
    search_subdir = os.getenv('SEARCH_SUBDIR')
    if not search_subdir:
        print("Error: SEARCH_SUBDIR environment variable is not set.")
        st.error("SEARCH_SUBDIR environment variable is not set.")
        st.stop()

    # Use PROJECT_ROOT and search_subdir to construct the path
    ratings_file = os.path.join(PROJECT_ROOT, 'searches', search_subdir, 'user_ratings.csv')
    print(f"Looking for ratings in: {ratings_file}")

    if os.path.exists(ratings_file):
        df = pd.read_csv(ratings_file, dtype={'room_id': str, 'user_rating': str, 'user_notes': str})
        if 'user_rating' not in df.columns:
            print("Error: 'user_rating' column is missing from the ratings file")
            st.error("'user_rating' column is missing from the ratings file.")
            st.stop()
        logger.info(f"Loaded ratings data from {ratings_file}")
        return df
    else:
        print(f"No ratings file found at {ratings_file}, creating empty DataFrame")
        return pd.DataFrame(columns=['room_id', 'user_rating', 'user_notes'])

def merge_results_with_ratings(df_results, df_ratings, config):
    """
    Merge the results DataFrame with the ratings DataFrame
    """
    logger.info("Starting merge of results with ratings...")
    logger.info("Results DataFrame: %d rows, %d columns", len(df_results), len(df_results.columns))
    logger.info("Ratings DataFrame: %d rows, %d columns", len(df_ratings), len(df_ratings.columns))
    
    # Log the key columns in each DataFrame
    logger.info("Results columns: %s", df_results.columns.tolist())
    logger.info("Ratings columns: %s", df_ratings.columns.tolist())

    # Ensure room_id is string type in both DataFrames
    df_results['room_id'] = df_results['room_id'].astype(str)
    df_ratings['room_id'] = df_ratings['room_id'].astype(str)
    
    # Log unique room_ids in each DataFrame
    logger.info("Unique room_ids in results: %d", df_results['room_id'].nunique())
    logger.info("Unique room_ids in ratings: %d", df_ratings['room_id'].nunique())

    # Perform the merge
    merged_df = pd.merge(df_results, df_ratings[['room_id', 'user_rating', 'user_notes']], 
                        on='room_id', how='left')
    
    logger.info("After merge: %d rows (change of %d rows)", 
                len(merged_df), len(merged_df) - len(df_results))

    # Check for any rows where the merge might have failed
    missing_ratings = merged_df['user_rating'].isna().sum()
    if missing_ratings > 0:
        logger.warning("%d properties have no user ratings after merge", missing_ratings)

    # Fill NaN values
    merged_df['user_rating'] = merged_df['user_rating'].fillna(6)  # 6 means unrated
    merged_df['user_notes'] = merged_df['user_notes'].fillna('')
    
    logger.info("Final merged DataFrame: %d rows, %d columns", len(merged_df), len(merged_df.columns))
    logger.info("Columns in merged result: %s", merged_df.columns.tolist())

    # Save debug data
    debug_file = os.path.join(config['base_dir'], 'searches', config['output_subdir'], 'merged_df_debug.csv')
    os.makedirs(os.path.dirname(debug_file), exist_ok=True)
    merged_df.to_csv(debug_file, index=False)
    logger.info("Debug data saved to %s", debug_file)

    return merged_df

def save_rating_and_notes(config, selected_row, user_rating, user_notes):
    """Save user ratings and notes to CSV file."""
    logger.info("Saving user ratings and notes")
    search_subdir = os.getenv('SEARCH_SUBDIR')
    if not search_subdir:
        print("Error: SEARCH_SUBDIR environment variable is not set.")
        st.error("SEARCH_SUBDIR environment variable is not set.")
        st.stop()

    # Use PROJECT_ROOT and search_subdir to construct the path
    ratings_file = os.path.join(PROJECT_ROOT, 'searches', search_subdir, 'user_ratings.csv')
    
    # Debug output
    debug_file = os.path.join(PROJECT_ROOT, 'searches', search_subdir, 'merged_df_debug.csv')
    
    print(f"Saving ratings to: {ratings_file}")
    print(f"Debug file: {debug_file}")

    room_id = selected_row["room_id"]

    # Update merged_df
    global merged_df
    if room_id in merged_df["room_id"].values:
        merged_df.loc[merged_df["room_id"] == room_id, "user_rating"] = str(user_rating)
        merged_df.loc[merged_df["room_id"] == room_id, "user_notes"] = user_notes
        merged_df['user_rating'] = merged_df['user_rating'].astype(str)

    # Load existing ratings
    if os.path.exists(ratings_file):
        df_ratings = pd.read_csv(ratings_file, dtype={'room_id': str, 'user_rating': str, 'user_notes': str})
    else:
        df_ratings = pd.DataFrame(columns=["room_id", "user_rating", "user_notes"], dtype=str)

    # Extract the updated ratings from merged_df
    updated_ratings = merged_df[['room_id', 'user_rating', 'user_notes']].copy()

    # Merge updated ratings with existing ratings
    df_ratings = pd.merge(df_ratings, updated_ratings, on='room_id', how='outer', suffixes=('', '_updated'))
    df_ratings['user_rating'] = df_ratings['user_rating_updated'].combine_first(df_ratings['user_rating'])
    df_ratings['user_notes'] = df_ratings['user_notes_updated'].combine_first(df_ratings['user_notes'])
    df_ratings = df_ratings.drop(columns=['user_rating_updated', 'user_notes_updated'])

    # Save ratings
    df_ratings.to_csv(ratings_file, index=False)

    # Resort and filter merged_df
    merged_df = sort_and_filter_properties(merged_df, 
                                           min_price=0, 
                                           max_price=float('inf'), 
                                           min_user_rating=0, 
                                           max_user_rating=6, 
                                           min_ai_rating=0,
                                           max_ai_rating=5,
                                           min_abnb_rating=1,
                                           max_abnb_rating=5,
                                           person_capacity=1, 
                                           selected_categories=[])

    # Save merged data
    merged_data_file = os.path.join(config['base_dir'], 'searches', config['search_subdir'], 'merged_data.csv')
    merged_df.to_csv(merged_data_file, index=False)

    st.success("Rating and notes saved!")
    
    # Return the top property
    return merged_df.iloc[0] if not merged_df.empty else None

def sort_and_filter_properties(df, min_price, max_price, min_user_rating, max_user_rating, 
                               min_ai_rating, max_ai_rating, min_airbnb_rating, max_airbnb_rating, 
                               person_capacity, selected_categories):
    """
    Filter properties, being very permissive with NaN values - only filter when we have actual values
    """
    logger.info("Starting property filtering with %d initial properties", len(df))
    logger.info("Filter criteria - Price: €%d-€%d, User Rating: %d-%d, AI Rating: %d-%d, Airbnb Rating: %d-%d, Min Capacity: %d, Categories: %s",
                min_price, max_price, min_user_rating, max_user_rating, min_ai_rating, max_ai_rating,
                min_airbnb_rating, max_airbnb_rating, person_capacity, selected_categories)
    
    # Save DataFrame before any processing for debugging
    debug_file = os.path.join(config['base_dir'], 'searches', config['output_subdir'], 'display_df.csv')
    os.makedirs(os.path.dirname(debug_file), exist_ok=True)
    df.to_csv(debug_file, index=False)
    logger.info("Saved pre-processing DataFrame to %s", debug_file)
    
    # Convert ratings to numeric, treating empty strings as NaN and filling with 6 (unrated)
    df['user_rating'] = pd.to_numeric(df['user_rating'], errors='coerce').fillna(6)
    logger.info("User ratings after conversion: %s", df['user_rating'].tolist())
    
    # Convert other ratings to numeric
    df['AI_rating'] = pd.to_numeric(df['AI_rating'], errors='coerce')
    df['Airbnb_rating'] = pd.to_numeric(df['Airbnb_rating'], errors='coerce')
    df['total_price'] = pd.to_numeric(df['total_price'], errors='coerce')
    
    # Start with all properties
    filtered_df = df.copy()
    logger.info("Initial properties: %d rows", len(filtered_df))
    
    # Filter by price if it exists
    price_mask = filtered_df['total_price'].notna()
    if price_mask.any():
        n_before = len(filtered_df)
        filtered_df = filtered_df[~price_mask | ((filtered_df["total_price"] >= min_price) & (filtered_df["total_price"] <= max_price))]
        n_after = len(filtered_df)
        logger.info("Price filter (€%d-€%d): %d -> %d rows (removed %d)", 
                   min_price, max_price, n_before, n_after, n_before - n_after)
    
    # Filter by user rating (all properties should have a rating of 6 if unrated)
    n_before = len(filtered_df)
    filtered_df = filtered_df[(filtered_df["user_rating"] >= min_user_rating) & (filtered_df["user_rating"] <= max_user_rating)]
    n_after = len(filtered_df)
    logger.info("User rating filter (%d-%d): %d -> %d rows (removed %d)", 
               min_user_rating, max_user_rating, n_before, n_after, n_before - n_after)
    
    # Filter by AI rating only for properties that have an AI rating
    ai_rating_mask = filtered_df['AI_rating'].notna()
    if ai_rating_mask.any():
        n_before = len(filtered_df)
        filtered_df = filtered_df[~ai_rating_mask | ((filtered_df["AI_rating"] >= min_ai_rating) & (filtered_df["AI_rating"] <= max_ai_rating))]
        n_after = len(filtered_df)
        logger.info("AI rating filter (%d-%d): %d -> %d rows (removed %d)", 
                   min_ai_rating, max_ai_rating, n_before, n_after, n_before - n_after)
    
    # Filter by Airbnb rating only for properties that have an Airbnb rating
    airbnb_rating_mask = filtered_df['Airbnb_rating'].notna()
    if airbnb_rating_mask.any():
        n_before = len(filtered_df)
        filtered_df = filtered_df[~airbnb_rating_mask | ((filtered_df["Airbnb_rating"] >= min_airbnb_rating) & (filtered_df["Airbnb_rating"] <= max_airbnb_rating))]
        n_after = len(filtered_df)
        logger.info("Airbnb rating filter (%d-%d): %d -> %d rows (removed %d)", 
                   min_airbnb_rating, max_airbnb_rating, n_before, n_after, n_before - n_after)
    
    # Filter by person capacity if it exists
    capacity_mask = filtered_df['person_capacity'].notna()
    if capacity_mask.any():
        n_before = len(filtered_df)
        filtered_df = filtered_df[~capacity_mask | (filtered_df["person_capacity"] >= person_capacity)]
        n_after = len(filtered_df)
        logger.info("Person capacity filter (>=%d): %d -> %d rows (removed %d)", 
                   person_capacity, n_before, n_after, n_before - n_after)
    
    # Apply category filter if any categories are selected
    if selected_categories:
        n_before = len(filtered_df)
        filtered_df = filtered_df[filtered_df['category'].isin(selected_categories)]
        n_after = len(filtered_df)
        logger.info("Category filter %s: %d -> %d rows (removed %d)", 
                   selected_categories, n_before, n_after, n_before - n_after)
    
    logger.info("Final filtered properties: %d rows (total removed: %d)", 
                len(filtered_df), len(df) - len(filtered_df))
    
    # Sort by price_total
    filtered_df = filtered_df.sort_values('total_price')
    
    return filtered_df

def display_filtered_listings(df_display, check_in, check_out, overlay_1_df, overlay_2_df):
    logger.info("Displaying filtered listings")
    st.subheader("Filtered Listings")
    st.write(f"Showing listings for {check_in} to {check_out}")
    
    required_columns = ["name", "total_price", "rating", "user_rating", "user_notes", "AI_rating"]
    available_columns = [col for col in required_columns if col in df_display.columns]

    if available_columns:
        options = df_display.apply(
            lambda row: (f"User Rating: {row['user_rating'] if pd.notna(row['user_rating']) else 'None'}  —  "
                        f"AI Rating: {f'{row['AI_rating']:.1f}' if pd.notna(row['AI_rating']) else 'None'}  —  "
                        f"AbnB Rating: {f'{extract_review_info(row['rating'])[0]:.2f} (Count: {extract_review_info(row['rating'])[1]})' if all(x is not None for x in extract_review_info(row['rating'])) else 'None'}  —  "
                        f"Title: {row['name'] if pd.notna(row['name']) else 'Untitled'}  —  "
                        f"Price: €{f'{float(row['total_price']):.2f}' if pd.notna(row['total_price']) else '0.00'} "
                        f" -------- User Notes: {(row['user_notes'][:50] + '...') if pd.notna(row['user_notes']) and len(str(row['user_notes'])) > 50 else (row['user_notes'] if pd.notna(row['user_notes']) else 'None')}"),
            axis=1
        ).tolist()

        selected_listing = st.selectbox("Select listing", options)
        if selected_listing is not None:
            selected_listing_index = options.index(selected_listing)
            if not df_display.empty:
                selected_row = df_display.iloc[selected_listing_index]
                display_listing_details(selected_row, available_columns, overlay_1_df, overlay_2_df)
            else:
                st.error("No matching listing found.")
        else:
            st.warning("No listing selected. Please choose a listing from the dropdown.")
            return
    else:
        st.write("Required columns are not available in the DataFrame.")

def display_listing_details(selected_row, available_columns, overlay_1_df, overlay_2_df):
    logger.info(f"Displaying details for listing {selected_row['room_id']}")
    st.subheader("Listing Details")
    # Initialize session state for user rating if not already set
    if 'user_rating' not in st.session_state:
        # Use existing rating if available, otherwise default to 0
        st.session_state['user_rating'] = int(selected_row["user_rating"]) if pd.notna(selected_row["user_rating"]) else 0

    # Check if a new property is selected
    if st.session_state.get('last_selected_row') != selected_row["room_id"]:
        # Reset and load new values from merged_df
        st.session_state['user_notes'] = selected_row["user_notes"]
        st.session_state['last_selected_row'] = selected_row["room_id"]
    else:
        # Ensure session state variables are set
        if 'user_notes' not in st.session_state:
            st.session_state['user_notes'] = ""

    col1, col2, col3 = st.columns([1, 4, 2])
    
    with col1:
        st.subheader("Rate and Add Notes to Selected Listing")
        # Display and update the slider and text area using session state
        st.session_state['user_rating'] = st.slider(
            "Your rating", 
            0, 6, 
            int(st.session_state['user_rating']),
            key="user_rating_slider"
        )
        
        # Display the formatted rating
        st.write("Rating: " + ("Not Rated" if st.session_state['user_rating'] == 6 else str(st.session_state['user_rating'])))
        
        st.session_state['user_notes'] = st.text_area(
            "Your notes", 
            st.session_state['user_notes'],
            key="user_notes_area"
        )
        
        if st.button("Save Rating and Notes"):
            top_property = save_rating_and_notes(config, selected_row, st.session_state['user_rating'], st.session_state['user_notes'])
            if top_property is not None:
                st.session_state['selected_row'] = top_property
                st.rerun()

    with col2:
        st.subheader("Listing Details")
        # Create URL link
        room_id = selected_row.get("room_id", "")
        if room_id:
            url = f"https://www.airbnb.com/rooms/{room_id}"
            url_link = f' <a href="{url}" target="_blank">↗</a>'
        else:
            url_link = ""

        # Combine name and total price on one line, with price in larger, bold font
        name = selected_row.get('name', 'N/A')
        total_price = f"€{selected_row.get('total_price', 'N/A'):.2f}"
        ai_rating = selected_row.get("AI_rating", "N/A")
        st.markdown(f"**{name}** &nbsp;&nbsp;-&nbsp;&nbsp; <span style='font-size: 1.2em; font-weight: bold;'>{total_price}</span> &nbsp;&nbsp;-&nbsp;&nbsp; <span style='font-size: 1.2em; font-weight: bold; color: #1E90FF;'>AI Rating: {ai_rating}</span>{url_link}", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)  # Add vertical space

        # Combine person capacity, category, and room type on one line
        person_capacity = selected_row.get("person_capacity", "N/A")
        category = selected_row.get("category", "N/A")
        room_type = selected_row.get("room_type", "N/A")
        st.markdown(f"**Capacity:** {person_capacity} &nbsp;&nbsp;|&nbsp;&nbsp; **Category:** {category} &nbsp;&nbsp;|&nbsp;&nbsp; **Room Type:** {room_type}")

        st.markdown("<br>", unsafe_allow_html=True)  # Add vertical space

        # Combine badges and super host status on one line
        badges = format_badges(selected_row.get("badges", "N/A"))
        is_super_host = "Yes" if selected_row.get("is_super_host") else "No"
        st.markdown(f"**Badges:** {badges} &nbsp;&nbsp;|&nbsp;&nbsp; **Super Host:** {is_super_host}")

        st.markdown("<br>", unsafe_allow_html=True)  # Add vertical space

        details = {
            "AI Review Summary": format_ai_review_summary(selected_row.get("AI_review_summary", "N/A")),
            "Description": clean_description(selected_row.get("description", "N/A")),
            "Sub Description": format_sub_description(selected_row.get("sub_description", "N/A")),
            "Location Description": clean_description(selected_row.get("location_description", "N/A"))
        }

        for key, value in details.items():
            if key == "AI Review Summary":
                st.markdown(f"### {key}")
                st.markdown(value)
            else:
                st.write(f"**{key}:**")
                highlighted_value = highlight_text(value, highlight_keywords_list)
                st.markdown(highlighted_value, unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)  # Add vertical space after each detail

    with col3:
        st.subheader("Map of Selected Listing")
        display_map(selected_row, overlay_1_df, overlay_2_df)
        
        if st.button("Refresh Map"):
            st.rerun()

def display_map(selected_row, overlay_1_df, overlay_2_df):
    logger.info(f"Displaying map for listing {selected_row['room_id']}")
    latitude = selected_row.get('latitude')
    longitude = selected_row.get('longitude')

    # Check if latitude and longitude are valid
    if latitude is None or longitude is None or pd.isna(latitude) or pd.isna(longitude):
        st.error("Invalid coordinates for the selected listing.")
        return

    # Convert to float explicitly
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except ValueError:
        st.error("Unable to convert coordinates to float values.")
        return

    # Create the map
    try:
        m = folium.Map(location=[latitude, longitude], zoom_start=14, tiles=None)

        # Add CartoDB Voyager tiles
        folium.TileLayer(
            tiles='https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
            attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
            subdomains='abcd',
            max_zoom=20
        ).add_to(m)

        # Add marker for the listing
        folium.Marker(
            [latitude, longitude],
            popup=selected_row['name'],
            tooltip=selected_row['name'],
            icon=folium.Icon(color='green', icon='home')
        ).add_to(m)

        # Add overlay 1 stations if available
        if overlay_1_df is not None:
            for _, station in overlay_1_df.iterrows():
                try:
                    folium.CircleMarker(
                        location=[float(station['Latitude']), float(station['Longitude'])],
                        radius=5,
                        popup=station['Station'],
                        color='red',
                        fill=True,
                        fillColor='red'
                    ).add_to(m)
                except ValueError as e:
                    st.warning(f"Error adding overlay 1 station {station['Station']}: {str(e)}")

        # Add overlay 2 stations if available
        if overlay_2_df is not None:
            for _, station in overlay_2_df.iterrows():
                try:
                    folium.CircleMarker(
                        location=[float(station['Latitude']), float(station['Longitude'])],
                        radius=5,
                        popup=station['Station'],
                        color='blue',
                        fill=True,
                        fillColor='blue'
                    ).add_to(m)
                except ValueError as e:
                    st.warning(f"Error adding overlay 2 station {station['Station']}: {str(e)}")

        # Add legend
        legend_html = '''
        <div style="position: fixed; bottom: 50px; left: 50px; width: 150px; height: 100px; 
        border:2px solid grey; z-index:9999; font-size:14px; background-color:white;
        ">&nbsp; Legend <br>
        &nbsp; <i class="fa fa-home fa-2x" style="color:green"></i> Listing <br>
        '''
        if overlay_1_df is not None:
            legend_html += '&nbsp; <i class="fa fa-circle fa-2x" style="color:red"></i> Overlay 1 <br>'
        if overlay_2_df is not None:
            legend_html += '&nbsp; <i class="fa fa-circle fa-2x" style="color:blue"></i> Overlay 2'
        legend_html += '</div>'
        m.get_root().html.add_child(folium.Element(legend_html))

        st_folium(m, width=700, height=500)
    except Exception as e:
        st.error(f"An error occurred while creating the map: {str(e)}")
        st.error("Please check the coordinates and overlay data for any inconsistencies.")

def save_settings_to_config(config, min_price, max_price, min_user_rating, max_user_rating, 
                            min_ai_rating, max_ai_rating, min_abnb_rating, max_abnb_rating, 
                            person_capacity, selected_categories, highlight_keywords):
    config["default_min_price"] = min_price
    config["default_max_price"] = max_price
    config["default_min_user_rating"] = min_user_rating
    config["default_max_user_rating"] = max_user_rating
    config["default_min_ai_rating"] = min_ai_rating
    config["default_max_ai_rating"] = max_ai_rating
    config["default_min_abnb_rating"] = min_abnb_rating
    config["default_max_abnb_rating"] = max_abnb_rating
    config["default_occupants"] = person_capacity
    config["selected_categories"] = selected_categories
    config["highlight_keywords"] = highlight_keywords
    save_config(config)

def save_and_end_session(config, df_ratings, min_price, max_price, min_user_rating, max_user_rating, 
                         min_ai_rating, max_ai_rating, min_abnb_rating, max_abnb_rating, 
                         person_capacity, selected_categories, highlight_keywords):
    df_ratings_filtered = df_ratings[(df_ratings["user_rating"] != "6") | (df_ratings["user_notes"] != "")]
    df_ratings_filtered.to_csv("user_ratings.csv", index=False)
    
    save_settings_to_config(config, min_price, max_price, min_user_rating, max_user_rating, 
                            min_ai_rating, max_ai_rating, min_abnb_rating, max_abnb_rating, 
                            person_capacity, selected_categories, highlight_keywords)
    
    st.success("Data and settings saved successfully!")
    st.warning("Your session has ended. To completely exit the application:")
    st.info("""
    1. Close this browser tab.
    2. Go to the terminal/command prompt where you started the Streamlit app.
    3. Press Ctrl+C to stop the Streamlit server.
    
    To start a new session, run 'streamlit run review_app.py' again.
    """)
    st.stop()

def ensure_compatible_types(df):
    problematic_columns = [0, 27]  # Indices of problematic columns
    for idx in problematic_columns:
        col = df.columns[idx]
        if df[col].dtype == 'object':
            df[col] = df[col].astype('str')
        elif df[col].dtype == np.float64:
            df[col] = df[col].astype('float32')
    return df

def load_overlay(overlay_file):
    """Load map overlay data from CSV file."""
    if not overlay_file or not os.path.exists(overlay_file):
        return None
    try:
        overlay_df = pd.read_csv(overlay_file)
        if all(col in overlay_df.columns for col in ['Station', 'Latitude', 'Longitude']):
            return overlay_df
    except Exception as e:
        print(f"Error loading overlay file: {e}")
    return None

# Load config
config = load_config()

# Check for default overlay files
overlay_1_file = config.get('map_overlay_file_1') or os.path.join(PROJECT_ROOT, 'overlays', 'overlay_1.csv')
overlay_2_file = config.get('map_overlay_file_2') or os.path.join(PROJECT_ROOT, 'overlays', 'overlay_2.csv')

# Print messages about overlay loading status
if overlay_1_file:
    overlay_1 = load_overlay(overlay_1_file)
    if overlay_1 is None:
        print("Overlay 1 could not be loaded.")
    else:
        print(f"Overlay 1 loaded successfully with {len(overlay_1)} entries.")
else:
    overlay_1 = None

if overlay_2_file:
    overlay_2 = load_overlay(overlay_2_file)
    if overlay_2 is None:
        print("Overlay 2 could not be loaded.")
    else:
        print(f"Overlay 2 loaded successfully with {len(overlay_2)} entries.")
else:
    overlay_2 = None

# Main script
import os
print()
print()
print("***********************************************************")
print("***********************************************************")
print("Starting REVIEW_APP.PY main script execution")
print(f"Current working directory: {os.getcwd()}")
print(f"BASE_DIR environment variable: {os.getenv('BASE_DIR', 'Not set')}")
print(f"SEARCH_SUBDIR environment variable: {os.getenv('SEARCH_SUBDIR', 'Not set')}")

# Initialize session state
if 'selected_row' not in st.session_state:
    st.session_state['selected_row'] = None

print("Loading results and details")
merged_df = load_results_and_details(config)
merged_df = remove_duplicates(merged_df)

# Calculate average coordinates with failover values
avg_latitude = merged_df['latitude'].mean() if ('latitude' in merged_df.columns and not merged_df['latitude'].isna().all()) else 41.14
avg_longitude = merged_df['longitude'].mean() if ('longitude' in merged_df.columns and not merged_df['longitude'].isna().all()) else -104.82

print("Loading user ratings")
df_ratings = load_ratings(config)

print("Merging results with ratings")
merged_df = merge_results_with_ratings(merged_df, df_ratings, config)

print("Columns in merged_df:", merged_df.columns.tolist())

# Load the map overlay data
overlay_1_df = load_overlay(overlay_1_file)
overlay_2_df = load_overlay(overlay_2_file)

# Print messages about overlay loading status
if overlay_1_df is None:
    print("Overlay 1 could not be loaded.")
else:
    print(f"Overlay 1 loaded successfully with {len(overlay_1_df)} entries.")

if overlay_2_df is None:
    print("Overlay 2 could not be loaded.")
else:
    print(f"Overlay 2 loaded successfully with {len(overlay_2_df)} entries.")

# Create a container for all filter controls
with st.container():
    st.subheader("Filters")
    
    # Create five columns for the sliders
    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        min_price, max_price = st.slider(
            "Price range (€)",
            min_value=int(merged_df["total_price"].min()),
            max_value=int(merged_df["total_price"].max()),
            value=(config.get("default_min_price", 0), config.get("default_max_price", 2000)),
            key="price_slider"
        )

    with col2:
        min_user_rating, max_user_rating = st.slider(
            "User rating",
            min_value=0,
            max_value=6,
            value=(config.get("default_min_user_rating", 0), config.get("default_max_user_rating", 6)),
            key="rating_slider"
        )

    with col3:
        min_ai_rating, max_ai_rating = st.slider(
            "AI rating",
            min_value=0,
            max_value=5,
            value=(config.get("default_min_ai_rating", 0), config.get("default_max_ai_rating", 5)),
            key="ai_rating_slider"
        )

    with col4:
        min_abnb_rating, max_abnb_rating = st.slider(
            "Airbnb rating",
            min_value=0,
            max_value=5,
            value=(config.get("default_min_abnb_rating", 0), config.get("default_max_abnb_rating", 5)),
            key="abnb_rating_slider"
        )

    with col5:
        max_person_capacity = merged_df["person_capacity"].max()
        person_capacity = st.slider(
            "Person capacity",
            min_value=1,
            max_value=int(max_person_capacity),
            value=config.get("default_occupants", 1),
            key="capacity_slider"
        )

    # Category checkboxes
    categories = merged_df['category'].unique().tolist()
    
    # Use columns for category checkboxes
    st.write("Categories:")
    category_cols = st.columns(4)  # Adjust the number of columns as needed
    selected_categories = []
    for i, category in enumerate(categories):
        if category_cols[i % 4].checkbox(category, key=f"category_{i}", value=category in config.get("selected_categories", []), help=f"Select {category}"):
            selected_categories.append(category)

# Update the keyword input to use the config
st.subheader("Keyword Highlighting")
default_keywords = config.get("highlight_keywords", "private bathroom, shared bathroom")
if isinstance(default_keywords, list):
    default_keywords = ', '.join(default_keywords)
elif isinstance(default_keywords, str):
    default_keywords = ', '.join(word.strip() for word in default_keywords.split(','))

highlight_keywords = st.text_input("Enter keywords to highlight (comma-separated):", 
                                   value=default_keywords,
                                   help="These keywords will be highlighted in the listing details.",
                                   key="highlight_keywords_input")
highlight_keywords_list = [kw.strip() for kw in highlight_keywords.split(',') if kw.strip()]

# Update the function call with all required arguments
df_display = sort_and_filter_properties(merged_df, min_price, max_price, min_user_rating, max_user_rating, 
                                        min_ai_rating, max_ai_rating, min_abnb_rating, max_abnb_rating, 
                                        person_capacity, selected_categories)

# Display filter summary
st.write(f"Selected categories: {len(selected_categories)} | Rentals available: {len(df_display)}")

# Display "Save and Quit" button
if st.button("Save and Quit"):
    save_and_end_session(config, df_ratings, min_price, max_price, min_user_rating, max_user_rating, 
                         min_ai_rating, max_ai_rating, min_abnb_rating, max_abnb_rating, 
                         person_capacity, selected_categories, highlight_keywords_list)

# Display filtered listings
st.markdown(f"### 🏠 Rentals available: {len(df_display)}")
st.markdown(f"### Filtered Listings (Check In: {format_date(config['check_in'])} | Check Out: {format_date(config['check_out'])})")
display_filtered_listings(df_display, config['check_in'], config['check_out'], overlay_1_df, overlay_2_df)

print("Main script execution completed")