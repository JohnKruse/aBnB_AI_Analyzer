# BnB AI Analyzer

Ever notice how Airbnb only shows you a limited set of properties, even when there are hundreds available? BnB AI Analyzer helps you discover all available Airbnb listings in your target area - not just the ones their algorithm chooses to display. By combining comprehensive property data with AI-powered review analysis, it helps you find and evaluate rentals based on what matters most to you.

The tool works in two phases:
1. First, it performs an exhaustive search of all Airbnb properties in your specified geographic area, uncovering listings that might never appear in your top search results
2. Then, it uses AI to analyze hundreds of reviews, extracting insights about key factors like location convenience, comfort, and accuracy of descriptions - helping you make informed decisions based on real guest experiences

Think of it as having a personal Airbnb researcher who can read every review, check every property, and help you find the perfect match for your specific needs.

Key capabilities:
- 🤖 AI-driven review analysis that distills hundreds of guest experiences into actionable insights
- 📊 Real-time price monitoring and historical trends to help you spot the best deals
- 🗺️ Smart location analysis with public transport overlays and neighborhood amenities
- 🏠 Detailed property insights focusing on what matters: cleanliness, comfort, and convenience

Whether you're a traveler seeking the perfect stay or a host looking to optimize your listing, BnB AI Analyzer turns overwhelming rental data into clear, actionable intelligence.

## Installation

You can set up the project environment using either pip or conda:

### Using pip

```bash
pip install -r requirements.txt
```

### Using conda

```bash
conda env create -f environment.yml
conda activate airbnb
```

## Usage

To start the application, run:

```bash
python abnb_launcher.py
```

This will start the Airbnb analyzer where you can:
- Set up new searches using Airbnb URLs
- Monitor existing searches
- Analyze property reviews and data
- View location-based insights

## Project Structure

- `abnb_launcher.py`: Main entry point and core functionality for the analyzer
- `abnb_launcher_ui.py`: Optional graphical user interface
- `abnb_monitor.py`: Monitoring functionality for rental listings
- `review_app.py`: Application for reviewing and analyzing listing data
- `overlays/`: Directory containing map overlay CSV files
- `searches/`: Directory containing search configurations
- `config.py`: Configuration management and API key handling

## Features

- **Intelligent Search Analysis**
  - Parse and analyze rental URLs for location and search parameters
  - Configurable search areas using geographic coordinates
  - Support for multiple concurrent search configurations

- **Advanced Monitoring**
  - Track listing prices and availability over time
  - Monitor changes in property details and descriptions
  - Automated data collection and updates

- **AI-Powered Review Analysis**
  - Smart review summarization focusing on key aspects:
    - Transportation accessibility
    - Bathroom and amenities
    - Sleeping arrangements
    - Cleanliness
    - Unique property features
  - Automated rating generation based on review content
  - Natural language processing for insight extraction

- **Geographic Visualization**
  - Custom map overlays for analyzing location context
  - Support for multiple overlay layers (e.g., transit stations, points of interest)
  - Visual representation of property distributions

- **Data Management**
  - Organized search-specific data storage
  - CSV export capabilities for further analysis
  - Efficient data updating and versioning

## Logging Configuration

The application uses Python's built-in logging module to track and record events that occur during execution. Logging is configured to write to a rotating file handler, which stores log files in the `logs/` directory. This setup helps in monitoring the application's behavior and diagnosing issues efficiently.

### Log Levels
- **INFO**: General information about the application's operation.
- **DEBUG**: Detailed information useful for diagnosing problems.
- **ERROR**: Records error events that might still allow the application to continue running.

### Log Files
Log files are automatically rotated with a maximum size of 200KB and up to 2 backup files. This ensures that disk space usage is managed effectively while retaining important log information.

### Excluding Logs from Version Control
The `.gitignore` file is configured to exclude the `logs/` directory, preventing log files from being committed to version control.

For further details, refer to the `logging_config.py` file in the `src` directory.

## Dependencies

This project's dependencies can be installed using either:
- `requirements.txt` for standard Python environments using pip
- `abnb.yml` for Conda environments

Choose the appropriate file based on your preferred Python environment manager.

## Setup

1. Copy `.env.template` to `.env` and fill in your API keys
2. Install dependencies: `pip install -r requirements.txt`
3. Run the launcher: `python abnb_launcher.py`

## Overlay Files

The `overlays/` directory contains CSV files for map overlays. Each file must include:
- Station: Name or identifier of the location
- Latitude: Geographic latitude
- Longitude: Geographic longitude

## Contributing

Please maintain the use of "abnb" instead of the original name throughout the codebase.
