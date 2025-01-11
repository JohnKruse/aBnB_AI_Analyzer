# Get the directory of the script and use it as the base directory
BASE_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
export BASE_DIR
echo "BASE_DIR: $BASE_DIR"  # Debug statement

# Ask the user to select an existing search subdirectory or create a new one
echo
echo "Select a search context:"
select SEARCH_SUBDIR in $(ls "$BASE_DIR/searches") "New_AbnB_Search"; do
    if [ "$SEARCH_SUBDIR" = "New_AbnB_Search" ]; then
        echo
        echo "Please set up your search on AbnB with desired location, dates, and map zoom."
        echo "Then copy the URL from your browser and paste it here."
        echo
        read -p "Paste the AbnB URL: " ABNB_URL

        # Extract information from the URL using sed
        LOCATION=$(echo $ABNB_URL | sed -n 's/.*query=\([^&]*\).*/\1/p' | sed 's/%20/_/g' | sed 's/%2C//g')
        CHECKIN=$(echo $ABNB_URL | sed -n 's/.*checkin=\([^&]*\).*/\1/p')
        CHECKOUT=$(echo $ABNB_URL | sed -n 's/.*checkout=\([^&]*\).*/\1/p')
        NE_LAT=$(echo $ABNB_URL | sed -n 's/.*ne_lat=\([^&]*\).*/\1/p')
        NE_LNG=$(echo $ABNB_URL | sed -n 's/.*ne_lng=\([^&]*\).*/\1/p')
        SW_LAT=$(echo $ABNB_URL | sed -n 's/.*sw_lat=\([^&]*\).*/\1/p')
        SW_LNG=$(echo $ABNB_URL | sed -n 's/.*sw_lng=\([^&]*\).*/\1/p')
        ZOOM=$(echo $ABNB_URL | sed -n 's/.*zoom=\([^&]*\).*/\1/p')

        # Extract just the city name (first part before any comma or underscore)
        CITY=$(echo $LOCATION | cut -d',' -f1 | cut -d'_' -f1)

        # Suggest a name for the search
        SUGGESTED_NAME="${CITY}_${CHECKIN}"
        echo
        read -p "Suggested name for this search is $SUGGESTED_NAME. Press Enter to accept or type a new name: " NEW_SEARCH_NAME
        NEW_SEARCH_NAME=${NEW_SEARCH_NAME:-$SUGGESTED_NAME}
        
        # Replace spaces with underscores in the new search name
        NEW_SEARCH_NAME=$(echo "$NEW_SEARCH_NAME" | tr ' ' '_')

        NEW_SEARCH_DIR="$BASE_DIR/searches/$NEW_SEARCH_NAME"
        mkdir -p "$NEW_SEARCH_DIR/input_data"
        mkdir -p "$NEW_SEARCH_DIR/output_data"
        
        # Create a config.yaml file with extracted data and default highlight keywords
        cat << EOF > "$NEW_SEARCH_DIR/config.yaml"
# Base directory and search information
base_dir: $BASE_DIR
search_subdir: $NEW_SEARCH_NAME
input_subdir: $NEW_SEARCH_NAME/input_data
output_subdir: $NEW_SEARCH_NAME/output_data

# Search parameters
check_in: '$CHECKIN'
check_out: '$CHECKOUT'
default_occupants: 1

# Price settings
currency: EUR
default_min_price: 0
default_max_price: 5000

# Rating settings
default_min_user_rating: 0
default_max_user_rating: 6

# Map coordinates
ne_lat: $NE_LAT
ne_long: $NE_LNG
sw_lat: $SW_LAT
sw_long: $SW_LNG
zoom_value: $ZOOM

# Additional features
highlight_keywords: private, shared, sharing, attached, separate, en suite, bathroom, underground, metro, train, tube, bus, stop, wifi, wi-fi, hot water, washer, laundry, washing, machine
selected_categories: []
map_overlay_file_1: 
map_overlay_file_2: 

# AI Review Summary Configuration
ai_review_summary:
  questions:
    - 'Summarize the following AbnB reviews into concise bullet points focusing on these areas: 1. Transportation 2. Bathroom and hot water 3. Sleeping arrangements 4. Cleanliness 5. Unexpected Points'
  role_prompt: "You are a review summarizer specializing in extracting concise, focused summaries from AbnB reviews. Your task is to summarize guest reviews by categorizing feedback into specific areas, providing 1 or 2 bullet points for each category. Each bullet point should be succinct and convey only essential information."
  model_name: "gpt-4o-mini"
  max_tokens: 500
  temperature: 0.1

# AI Rating Configuration
ai_rating:
  questions:
    - 'Provide a numerical rating between 1 and 5 based on the text you are given.'
  role_prompt: "You are an expert rating analyst. Your task is to provide a numerical rating between 1 and 5 based on the text you are given. Please provide a rating based on the following criteria: 1. Transportation 2. Bathroom and hot water 3. Sleeping arrangements 4. Cleanliness 5. Unexpected Points. A lack of specific mentions should lower the rating."
  model_name: "gpt-4o-mini"
  max_tokens: 500
  temperature: 0.1
  function_schema:
    name: rate_string
    description: "Evaluate a given string and return a rating between 1 and 5."
    parameters:
      type: object
      properties:
        AI_rating:
          type: number
          minimum: 1.0
          maximum: 5.0
          description: "Overall rating."
      required:
        - AI_rating
EOF
        
        echo
        echo "config.yaml created with data from AbnB URL."
        SEARCH_SUBDIR="$NEW_SEARCH_NAME"
        echo "New search directory created: $NEW_SEARCH_DIR"
        
        # Prompt user to edit the config file
        # Explain the requirements for the map overlay CSV files
        echo 
        echo "The config file has entries for the paths of up to two map overlays (e.g., train stations)."
        echo "Map overlay CSV files should contain the following columns: 'Station', 'Latitude', and 'Longitude'."
        echo "If you want to use map overlays, you will need to create these CSV files from the relevant data."
        echo
        read -p "Do you want to review and edit the config file now? (y/n) " EDIT_CONFIG
        if [[ $EDIT_CONFIG =~ ^[Yy]$ ]]; then
            open -e "$NEW_SEARCH_DIR/config.yaml"
            echo
            echo "TextEdit has been opened with the config file. Please save and close it when you're done."
            read -p "Press Enter when you've finished editing and saved the file."
        else
            echo
            echo "Please remember to review the config file before running the search if needed."
        fi
    fi
    break
done

# Set the selected search subdirectory as an environment variable
export SEARCH_SUBDIR
echo
echo "Using search subdir: $SEARCH_SUBDIR"

# Debug: Print the full path of the config file
echo "Config file path: $BASE_DIR/searches/$SEARCH_SUBDIR/config.yaml"

# Check if the config file exists
if [ ! -f "$BASE_DIR/searches/$SEARCH_SUBDIR/config.yaml" ]; then
    echo "Error: Config file not found at $BASE_DIR/searches/$SEARCH_SUBDIR/config.yaml"
    exit 1
fi

# Continue with running abnb_monitor.py

# After selecting the search subdir, run abnb_monitor.py
echo
echo "Running abnb_monitor.py with selected search subdir: $SEARCH_SUBDIR"
python3 "$BASE_DIR/abnb_monitor.py"

if [ $? -ne 0 ]; then
    echo
    echo "Error: abnb_monitor.py failed to complete successfully."
    exit 1
fi

echo
echo "abnb_monitor.py completed successfully."

# After abnb_monitor.py completes, run review_app.py for further review
echo
echo "Running review_app.py to process the downloaded data..."
streamlit run "$BASE_DIR/review_app.py"

if [ $? -ne 0 ]; then
    echo
    echo "Error: review_app.py failed to complete successfully."
    exit 1
fi

echo
echo "review_app.py completed successfully."