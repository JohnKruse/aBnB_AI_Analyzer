# Map Overlay Files

This directory contains CSV files that define points of interest to be displayed on the map in the review app. These overlays can help you understand the location of an Airbnb listing relative to important landmarks or facilities.

## File Format

Overlay files must be CSV files with the following required columns:

- `Station`: Name or description of the point of interest (will be shown in popup)
- `Latitude`: Decimal latitude coordinate (e.g., 37.7749)
- `Longitude`: Decimal longitude coordinate (e.g., -122.4194)

Example:
```csv
Station,Latitude,Longitude
Central Station,37.7749,-122.4194
Downtown Bus Stop,37.7833,-122.4167
Main Library,37.7785,-122.4156
```

## Usage

1. Create your CSV file following the format above
2. Place it in this directory
3. Update your config.yaml to reference your overlay files:
   ```yaml
   map_overlay_file_1: overlays/your_overlay1.csv
   map_overlay_file_2: overlays/your_overlay2.csv
   ```

## Display

- Overlay points are shown as red circle markers on the map
- Each point shows its Station name when clicked
- The selected Airbnb listing is shown as a green house icon for reference
