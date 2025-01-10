"""AI utilities for the BnB AI Analyzer.

This module provides utilities for interacting with AI services,
particularly OpenAI's GPT models.
"""

import json
import openai
import requests
import time
import re
import ast
import pandas as pd
import os
from typing import List, Dict, Any, Optional, Union
from .logging_config import setup_logger, CONSOLIDATED_LOG

# Set up logger for this module
logger = setup_logger(__name__, CONSOLIDATED_LOG)

def query_openai_gptX_with_schema(text, questions, role_prompt, model_name, api_key, file_path=None, function_schema=None, max_tokens=2000, temperature=0.3):
    logger.info("Starting query_openai_gptX_with_schema function.")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if file_path:
        logger.info(f"Processing file input from {file_path}.")
        extracted_text = extract_text(file_path)
        if extracted_text is None:
            logger.error("Unsupported file type. Only PDF and TXT files are supported.")
            return {"error": "Unsupported file type. Only PDF and TXT files are supported."}
        text += "\n" + extracted_text
        logger.info("Text extracted and appended to main text.")

    # We only process the first question since that's what the working version does
    question = questions[0]
    logger.info(f"Preparing to submit question: {question}")
    prompt = f"{role_prompt}\n{text}\n\n###\n\n{question}\nAnswer:"
    data = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature
    }

    if function_schema:
        data["functions"] = [function_schema]
        data["function_call"] = {"name": function_schema["name"]}
        
    # Log the actual request being sent
    logger.info(f"Request data being sent to OpenAI:")
    logger.info(f"Model: {model_name}")
    logger.info(f"Function schema present: {bool(function_schema)}")
    logger.info(f"Data payload: {json.dumps(data, indent=2)}")

    try:
        logger.info("Sending request to OpenAI API.")
        response = requests.post(url, headers=headers, json=data)
        
        # Check the status code and log it
        logger.info(f"Response status code: {response.status_code}")
        response_data = response.json()
        logger.info(f"Raw API Response: {json.dumps(response_data, indent=2)}")

        if response.ok:
            logger.info("Received successful response from OpenAI API.")
            if function_schema:
                logger.info("Processing function call response")
                message = response_data['choices'][0]['message']
                logger.info(f"Message content: {message}")
                if 'function_call' in message:
                    return message['function_call']['arguments']
                else:
                    return message.get('content')
            else:
                return response_data['choices'][0]['message']['content']
        else:
            error_msg = f"API request failed with status code {response.status_code}: {response.text}"
            logger.error(error_msg)
            return error_msg

    except requests.RequestException as e:
        error_msg = f"API request failed with error: {str(e)}"
        logger.error(error_msg)
        return error_msg

def extract_ai_rating(ai_response: str) -> Optional[float]:
    """Extract numerical rating from AI response.
    
    Args:
        ai_response (str): AI response containing rating
        
    Returns:
        float: Extracted rating value or None if not found
    """
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

def clean_ai_review_summary(review_summary: str) -> str:
    """Clean and format AI review summary text.
    
    Args:
        review_summary (str): Raw review summary text
        
    Returns:
        str: Cleaned and formatted review summary
    """
    try:
        # If already cleaned or None, return as is
        if review_summary is None or is_already_cleaned(review_summary):
            return review_summary

        # Try to parse as JSON first
        try:
            review_dict = json.loads(review_summary)
            # If successful, get the first value
            if isinstance(review_dict, dict):
                return list(review_dict.values())[0]
            return review_dict
        except json.JSONDecodeError:
            pass

        # Try to parse as Python literal
        try:
            review_dict = ast.literal_eval(review_summary)
            if isinstance(review_dict, dict):
                return list(review_dict.values())[0]
            return review_dict
        except (ValueError, SyntaxError):
            pass

        # If both parsing attempts fail, try to extract content after colon
        if ':' in review_summary:
            summary_text = review_summary.split(':', 1)[1].strip()
            if summary_text:
                return summary_text

        # If all else fails, return original
        return review_summary

    except Exception as e:
        logger.error(f"Error cleaning review summary: {e}")
        return "Error processing summary"

def is_already_cleaned(review_summary: str) -> bool:
    # This function is not implemented in the provided code
    # It's assumed to be implemented elsewhere in the codebase
    pass

def get_ai_rating(df: pd.DataFrame, text_columns: List[str], ai_config: Dict[str, Any], new_column_name: str, skip_existing: bool = False) -> pd.DataFrame:
    """Get AI ratings for text content in DataFrame.
    
    Args:
        df (pandas.DataFrame): DataFrame containing text to rate
        text_columns (list): List of column names containing text to rate
        ai_config (dict): AI configuration settings
        new_column_name (str): Name for new column containing AI ratings
        skip_existing (bool): If True, skip rows that already have ratings
        
    Returns:
        pandas.DataFrame: DataFrame with new AI rating column
    """
    logger.info("Starting AI rating process for %d properties", len(df))
    
    # Initialize new column if it doesn't exist
    if new_column_name not in df.columns:
        df[new_column_name] = None
    
    # Track success rate
    success_count = 0
    total_count = 0
    
    # Process each row
    for idx, row in df.iterrows():
        total_count += 1
        
        # Skip if rating exists and skip_existing is True
        if skip_existing and pd.notna(row[new_column_name]):
            success_count += 1
            continue
            
        # Combine text from specified columns
        text_to_rate = " ".join(str(row[col]) for col in text_columns if pd.notna(row[col]))
        
        if not text_to_rate.strip():
            logger.warning(f"No text to rate for row {idx}")
            continue
            
        try:
            # Get AI rating
            api_key = os.getenv('OPENAI_API_KEY')
            if not api_key:
                raise ValueError("OpenAI API key not found in environment variables")
                
            response = query_openai_gptX_with_schema(
                text_to_rate,
                ai_config['questions'],
                ai_config['role_prompt'],
                ai_config['model_name'],
                api_key,
                function_schema=ai_config.get('function_schema'),
                max_tokens=ai_config.get('max_tokens', 2000),
                temperature=ai_config.get('temperature', 0.3)
            )
            
            # Extract rating from response
            if ai_config.get('function_schema'):
                rating = extract_ai_rating(response)
            else:
                rating = response
                
            if rating is not None:
                df.at[idx, new_column_name] = rating
                success_count += 1
            else:
                logger.error(f"Failed to extract rating for row {idx}")
                
        except Exception as e:
            logger.error(f"Error processing AI rating for row {idx}: {str(e)}")
            
        # Add delay to avoid rate limits
        time.sleep(1)
    
    logger.info(f"Completed AI rating process. Success rate: {success_count}/{total_count}")
    return df
