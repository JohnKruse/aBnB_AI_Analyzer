import os
import logging
from logging.handlers import RotatingFileHandler

# Define log file paths
MONITOR_LOG = os.path.join('logs', 'bnb_analyzer.log')

# Logging configuration
MAX_LOG_LINE_LENGTH = 200  # Configurable maximum length for log messages

class TruncatingFormatter(logging.Formatter):
    """Custom formatter that truncates messages that are too long."""
    def __init__(self, fmt=None, datefmt=None, max_length=MAX_LOG_LINE_LENGTH):
        super().__init__(fmt, datefmt)
        self.max_length = max_length

    def format(self, record):
        # First, format the message as normal
        message = super().format(record)
        
        # If message is too long, truncate it and add an indicator
        if len(message) > self.max_length:
            truncated_length = self.max_length - 3  # Leave room for '...'
            message = message[:truncated_length] + '...'
        
        return message

def setup_logger(name, log_file, level=logging.INFO):
    """Set up a logger with rotating file handler.
    
    Args:
        name (str): Logger name (usually __name__ of the calling module)
        log_file (str): Path to the log file
        level (int): Logging level (default: logging.INFO)
    
    Returns:
        logging.Logger: Configured logger instance
    """
    # Create logs directory if it doesn't exist
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    
    # Create formatters with filename included
    file_formatter = TruncatingFormatter(
        '%(asctime)s - [%(filename)s] - %(levelname)s - %(message)s'
    )
    console_formatter = TruncatingFormatter(
        '[%(filename)s] %(levelname)s: %(message)s'
    )
    
    # Create rotating file handler (200KB max size, max 2 backup files)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=200*1024,  # 200KB
        backupCount=2,
        encoding='utf-8'
    )
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(level)
    
    # Create console handler with a simpler formatter that includes filename
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(level)
    
    # Get or create logger
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Remove existing handlers to avoid duplicates
    logger.handlers = []
    
    # Add handlers
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# Constants for log file paths relative to project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, 'logs')

# Single consolidated log file
CONSOLIDATED_LOG = os.path.join(LOG_DIR, 'bnb_analyzer.log')
# Use the consolidated log for all components
LAUNCHER_LOG = CONSOLIDATED_LOG
REVIEW_APP_LOG = CONSOLIDATED_LOG
