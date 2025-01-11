#!/usr/bin/env python3

import os
import sys
import yaml
import re
import subprocess
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
from urllib.parse import urlparse, parse_qs
from src.logging_config import setup_logger, LAUNCHER_LOG

# Set up logger
logger = setup_logger(__name__, LAUNCHER_LOG)

# Log start of new run with clear separator
logger.info("")
logger.info("\n" + "="*80)
logger.info("STARTING NEW AIRBNB ANALYZER RUN AT %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
logger.info("="*80 + "\n")

# Define project root directory
PROJECT_ROOT = Path(__file__).parent.absolute()

class AbnbLauncher:
    def __init__(self):
        logger.info("Initializing AbnbLauncher")
        self.searches_dir = PROJECT_ROOT / "searches"
        self.searches_dir.mkdir(exist_ok=True)
        logger.debug(f"Searches directory: {self.searches_dir}")

    def get_existing_searches(self) -> list:
        """Return list of existing search directories."""
        logger.debug("Getting list of existing searches")
        return [d.name for d in self.searches_dir.iterdir() if d.is_dir()]

    def parse_abnb_url(self, url: str) -> Dict[str, Any]:
        """Extract search parameters from AbnB URL."""
        logger.info(f"Parsing URL: {url}")
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        
        # Extract single values from potentially multi-valued parameters
        data = {
            'location': params.get('query', [''])[0].replace('%20', '_').replace('%2C', ''),
            'checkin': params.get('checkin', [''])[0],
            'checkout': params.get('checkout', [''])[0],
            'ne_lat': params.get('ne_lat', [''])[0],
            'ne_lng': params.get('ne_lng', [''])[0],
            'sw_lat': params.get('sw_lat', [''])[0],
            'sw_lng': params.get('sw_lng', [''])[0],
            'zoom': params.get('zoom', [''])[0]
        }
        
        # Extract city name (first part before any comma or underscore)
        data['city'] = data['location'].split(',')[0].split('_')[0]
        
        return data

    def create_config_yaml(self, search_dir: Path, params: Dict[str, Any]) -> None:
        """Create config.yaml file with extracted parameters."""
        logger.info(f"Creating config.yaml for search directory: {search_dir}")
        config = {
            'search_subdir': search_dir.name,
            'input_subdir': f"{search_dir.name}/input_data",
            'output_subdir': f"{search_dir.name}/output_data",
            
            'check_in': params['checkin'],
            'check_out': params['checkout'],
            'default_occupants': 1,
            
            'currency': 'EUR',
            'default_min_price': 0,
            'default_max_price': 5000,
            
            'default_min_user_rating': 0,
            'default_max_user_rating': 6,
            
            'ne_lat': params['ne_lat'],
            'ne_long': params['ne_lng'],
            'sw_lat': params['sw_lat'],
            'sw_long': params['sw_lng'],
            'zoom_value': params['zoom'],
            
            'highlight_keywords': [
                'private', 'shared', 'sharing', 'attached', 'separate', 
                'en suite', 'bathroom', 'underground', 'metro', 'train', 
                'tube', 'bus', 'stop', 'wifi', 'wi-fi', 'hot water', 
                'washer', 'laundry', 'washing', 'machine'
            ],
            'selected_categories': [],
            'map_overlay_file_1': '',
            'map_overlay_file_2': '',
            
            'ai_review_summary': {
                'questions': [
                    'Summarize the following AbnB reviews into concise bullet points focusing on these areas: '
                    '1. Transportation 2. Bathroom and hot water 3. Sleeping arrangements '
                    '4. Cleanliness 5. Unexpected Points'
                ],
                'role_prompt': "You are a review summarizer specializing in extracting concise, focused summaries "
                              "from AbnB reviews. Your task is to summarize guest reviews by categorizing feedback "
                              "into specific areas, providing 1 or 2 bullet points for each category. Each bullet "
                              "point should be succinct and convey only essential information.",
                'model_name': "gpt-4o-mini",
                'max_tokens': 500,
                'temperature': 0.1
            },
            
            'ai_rating': {
                'questions': [
                    'Provide a numerical rating between 1 and 5 based on the text you are given.'
                ],
                'role_prompt': "You are an expert rating analyst. Your task is to provide a numerical rating between "
                              "1 and 5 based on the text you are given. Please provide a rating based on the following "
                              "criteria: 1. Transportation 2. Bathroom and hot water 3. Sleeping arrangements "
                              "4. Cleanliness 5. Unexpected Points. A lack of specific mentions should lower the rating.",
                'model_name': "gpt-4o-mini",
                'max_tokens': 500,
                'temperature': 0.1,
                'function_schema': {
                    'name': 'rate_string',
                    'description': "Evaluate a given string and return a rating between 1 and 5.",
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'AI_rating': {
                                'type': 'number',
                                'minimum': 1.0,
                                'maximum': 5.0,
                                'description': "Overall rating."
                            }
                        },
                        'required': ['AI_rating']
                    }
                }
            }
        }
        
        config_file = search_dir / "config.yaml"
        with open(config_file, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
        logger.info(f"Config.yaml created for search directory: {search_dir}")

    def create_new_search(self) -> Optional[str]:
        """Create a new search directory and configuration."""
        logger.info("Creating new search and getting user input")
        print("\nPlease set up your search on AbnB with desired location, dates, and map zoom.")
        print("Then copy the URL from your browser and paste it here.")
        url = input("\nPaste the AbnB URL: ").strip()
        
        if not url:
            logger.error("No URL provided")
            print("No URL provided. Aborting.")
            return None
            
        try:
            params = self.parse_abnb_url(url)
        except Exception as e:
            logger.exception(f"Error parsing URL: {e}")
            print(f"Error parsing URL: {e}")
            return None
            
        suggested_name = f"{params['city']}_{params['checkin']}"
        new_name = input(f"\nSuggested name for this search is {suggested_name}. "
                        f"Press Enter to accept or type a new name: ").strip()
        search_name = new_name if new_name else suggested_name
        search_name = search_name.replace(' ', '_')
        
        search_dir = self.searches_dir / search_name
        input_dir = search_dir / "input_data"
        output_dir = search_dir / "output_data"
        
        search_dir.mkdir(exist_ok=True)
        input_dir.mkdir(exist_ok=True)
        output_dir.mkdir(exist_ok=True)
        
        self.create_config_yaml(search_dir, params)
        
        logger.info(f"New search directory created: {search_dir}")
        
        print("\nThe config file has entries for the paths of up to two map overlays (e.g., train stations).")
        print("Map overlay CSV files should contain the following columns: 'Station', 'Latitude', and 'Longitude'.")
        print("If you want to use map overlays, you will need to create these CSV files from the relevant data.")
        
        edit_config = input("\nDo you want to review and edit the config file now? (y/n) ").lower().strip()
        if edit_config == 'y':
            subprocess.run(['open', '-e', str(search_dir / "config.yaml")])
            input("\nTextEdit has been opened with the config file. Press Enter when you've finished editing and saved the file.")
        else:
            logger.info("User chose not to edit config file")
            print("\nPlease remember to review the config file before running the search if needed.")
        
        return search_name

    def select_search(self) -> Optional[str]:
        """Let user select an existing search or create a new one."""
        logger.info("Selecting search option by user")
        existing_searches = self.get_existing_searches()
        
        print("\nSelect a search context:")
        for i, name in enumerate(existing_searches + ["New_AbnB_Search"], 1):
            print(f"{i}. {name}")
            
        while True:
            try:
                choice = int(input("\nEnter number: "))
                if 1 <= choice <= len(existing_searches) + 1:
                    break
                logger.error("Invalid choice")
                print("Invalid choice. Please try again.")
            except ValueError:
                logger.error("Invalid input")
                print("Please enter a number.")
        
        if choice == len(existing_searches) + 1:
            return self.create_new_search()
        else:
            logger.info(f"Selected existing search: {existing_searches[choice - 1]}")
            return existing_searches[choice - 1]

    def run_pipeline(self, search_name: str) -> None:
        """Run the AbnB monitoring pipeline."""
        logger.info(f"Running pipeline for search: {search_name}")
        if not search_name:
            logger.error("No search name provided")
            print("No search name provided. Aborting.")
            return

        os.environ['SEARCH_SUBDIR'] = search_name
        logger.info(f"Using search subdir: {search_name}")
        print(f"\nUsing search subdir: {search_name}")
        
        config_path = self.searches_dir / search_name / "config.yaml"
        if not config_path.exists():
            logger.error(f"Config file not found at {config_path}")
            print(f"Error: Config file not found at {config_path}")
            return

        # Run abnb_monitor.py
        logger.info("Running abnb_monitor.py")
        try:
            subprocess.run([sys.executable, PROJECT_ROOT / "abnb_monitor.py"], check=True)
            logger.info("abnb_monitor.py completed successfully")
            print("abnb_monitor.py completed successfully.")
        except subprocess.CalledProcessError as e:
            logger.error(f"abnb_monitor.py failed to complete successfully with error: {e}")
            print(f"Error: abnb_monitor.py failed to complete successfully with error: {e}")
            return
        # Run review_app.py
        logger.info("Running review_app.py")
        try:
            subprocess.run(["streamlit", "run", str(PROJECT_ROOT / "review_app.py")], check=True)
            logger.info("review_app.py completed successfully")
            print("review_app.py completed successfully.")
        except subprocess.CalledProcessError as e:
            logger.error(f"review_app.py failed to complete successfully with error: {e}")
            print(f"Error: review_app.py failed to complete successfully with error: {e}")
            return

def main():
    try:
        logger.info("Starting AbnbLauncher")
        launcher = AbnbLauncher()
        search_name = launcher.select_search()
        launcher.run_pipeline(search_name)
    except Exception as e:
        logger.exception("An error occurred in main:")
        raise

if __name__ == "__main__":
    main()