"""Configuration handling for the BnB AI Analyzer.

This module handles all configuration aspects including:
- Loading YAML configuration files
- Managing environment variables
- Handling API keys
"""
import os
import yaml
from typing import Optional

def get_api_key(key_name: str, required: bool = True) -> Optional[str]:
    """
    Get API key from environment variables with proper error handling.
    
    Args:
        key_name: Name of the environment variable containing the API key
        required: If True, raise error when key is missing; if False, return None
        
    Returns:
        str: The API key if found
        None: If key not found and required=False
        
    Raises:
        EnvironmentError: If required=True and key not found
    """
    api_key = os.getenv(key_name)
    
    if api_key is None:
        msg = f"Environment variable {key_name} not found. "
        if os.name == 'nt':  # Windows
            msg += "On Windows, you can set it using:\n"
            msg += f'setx {key_name} "your-api-key-here"'
        else:  # Unix-like systems
            msg += "You can set it using:\n"
            msg += f'export {key_name}="your-api-key-here"'
        
        if required:
            raise EnvironmentError(msg)
        else:
            print(f"Warning: {msg}")
            return None
            
    return api_key

def get_openai_api_key(required: bool = True) -> Optional[str]:
    """Get OpenAI API key from environment."""
    return get_api_key('OPENAI_API_KEY', required)

def get_google_api_key(required: bool = True) -> Optional[str]:
    """Get Google API key from environment."""
    return get_api_key('GOOGLE_API_KEY', required)

def load_config():
    """
    Load configuration from YAML file and environment variables.
    
    The configuration combines:
    1. YAML file settings from the search subdirectory
    2. Environment variables for API keys and search directory
    3. Computed paths and directories
    
    Returns:
        dict: Complete configuration dictionary
        
    Raises:
        ValueError: If SEARCH_SUBDIR environment variable is not set
        FileNotFoundError: If config file is not found
        EnvironmentError: If required API keys are not set
    """
    search_subdir = os.getenv('SEARCH_SUBDIR')
    if not search_subdir:
        raise ValueError("SEARCH_SUBDIR environment variable is not set.")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, 'searches', search_subdir, 'config.yaml')

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found at {config_path}")

    with open(config_path, 'r') as config_file:
        config = yaml.safe_load(config_file)

    # Add base_dir and search_subdir to the config
    config['base_dir'] = base_dir
    config['search_subdir'] = search_subdir
    
    # Add API keys to config
    config['openai_api_key'] = get_openai_api_key()
    config['google_api_key'] = get_google_api_key()

    return config

# Load the configuration
config = load_config()