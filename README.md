# Subtitle Translation Script for Bazarr

This script automates the process of fetching, downloading, and translating subtitles for movies and episodes managed by [Bazarr](https://bazarr.media/). The script ensures that subtitles are available for multiple languages based on your specified preferences.

The script first tries to download subtitles in a ENG and then it translates to your desired language. Languages can be modified in `FIRST_LANG` and `SECOND_LANG`(if only want one, leave empty).

### Required
- Bazarr (obviously)

## Best Bazarr settigns to optimize this script:
- Configure **Embedded Subtitles** Provider


![imagen](https://github.com/anast20sm/Bazarr_AutoTranslate/assets/33606434/d5e5b443-b0ae-4adb-b32b-07a6f5338a1d)


- Disable **Use Embedded Subtitles**


![imagen](https://github.com/anast20sm/Bazarr_AutoTranslate/assets/33606434/e2712537-1e83-4590-9cc4-1f2e47ad0cbc)


Embedded subtitles cannot be used/modified by Bazarr so with these two settings it will extract the embedded subtitles in case there are (by default I only programmed extract English subs).

- Enable **Upgrade Previously Downloaded Subtitles** and **Upgrade Manually Downloaded or Translated Subtitles**


 ![imagen](https://github.com/anast20sm/Bazarr_AutoTranslate/assets/33606434/42736f20-fb55-43de-b45e-a07cceea73d2)


 
 ![imagen](https://github.com/anast20sm/Bazarr_AutoTranslate/assets/33606434/5c1eb5c1-e52f-42c4-a871-eb4cfbb90582)


This is recommended to always have the best possible subtitles, and if possible one made by a person who understands what is happening in the show/movie and writes with context.
I repeat again, as translated subtitles will never be as good as subtitles made by someone, this setting will ensure translated is only the last option.

Note: Code assumses that you have 3 languages set in profile, english being one of them:

![{F7585D44-4699-46F1-94FC-81FC559FF036}](https://github.com/user-attachments/assets/e346d80a-e295-4cf2-aaa7-e26c46bd08d3)

---

## Table of Contents

- [Overview](#overview)
- [Installation and Usage](#installation-and-usage)
  - [Dependencies](#dependencies)
  - [Configuration](#configuration)
- [Functions](#functions)
  - [Main Workflow](#main-workflow)
  - [Helper Functions](#helper-functions)
- [Color-coded Logs](#color-coded-logs)
- [License](#license)

---

## Overview

The script:
1. Fetches movies and episodes marked as "wanted" in Bazarr.
2. Downloads missing English subtitles (required for translation).
3. Attempts to translate the subtitles to one or two target languages, as per configuration.
4. Provides debug and info logs for each step of processing.

---

## Installation and Usage

### Dependencies

Make sure the following Python libraries are installed:
- `requests`: Handles HTTP requests to the Bazarr API.
- `urllib.parse`: URL-encodes paths for proper HTTP communication.

Install dependencies via pip:
```bash
pip install requests
```

### Configuration

Edit the following configuration variables in the script before running it:

```python
# Bazarr Configuration
BAZARR_HOSTNAME = "your-bazarr-hostname.com:port"
BAZARR_APIKEY = "your-bazarr-api-key"

# Language preferences
FIRST_LANG = "your-first-language-code"  # Example: 'et' for Estonian
SECOND_LANG = "your-second-language-code"  # Optional. Example: 'sv' for Swedish
```

- `BAZARR_HOSTNAME`: The hostname and port where Bazarr is running.
- `BAZARR_APIKEY`: The API key for accessing the Bazarr API.
- `FIRST_LANG`: The primary language for which subtitles should be translated.
- `SECOND_LANG`: (Optional) A secondary language for subtitle translation.

### Running the Script

Run the script using Python:
```bash
python subtitle_translation.py
```

---

## Functions

### Main Workflow

1. **`main()`**:
   - Entry point of the script.
   - Calls `translate_movie_subs()` and `translate_episode_subs()` to process movies and episodes.

2. **`translate_movie_subs()`**:
   - Fetches all movies marked as "wanted" in Bazarr.
   - Downloads/translates subtitles for these movies.

3. **`translate_episode_subs()`**:
   - Fetches all episodes marked as "wanted" in Bazarr.
   - Downloads/translates subtitles for these episodes.

4. **`process_items(item_type, wanted_endpoint, id_field, params_field)`**:
   - Processes all movies or episodes from the specified endpoint.
   - Calls `process_item()` to handle individual items.

5. **`process_item(item, item_type, id_field, params_field)`**:
   - Handles individual movie or episode processing:
     - Checks for missing languages.
     - Downloads English subtitles if missing.
     - Translates subtitles to the desired languages.

---

### Helper Functions

1. **`get_api_url(endpoint)`**:
   - Constructs a full API URL for a specific endpoint.

2. **`fetch_items(item_type, wanted_endpoint)`**:
   - Fetches a list of items (movies or episodes) from Bazarr's "wanted" endpoint.

3. **`download_subtitles(item_type, item_id, params_field, language, series_id)`**:
   - Downloads missing subtitles for a specific language.

4. **`fetch_subtitle_path(item_type, item_id, params_field)`**:
   - Fetches and URL-encodes the path of existing English subtitles.

5. **`translate_subtitle(item_type, item_id, subs_path, target_lang)`**:
   - Translates subtitles into the specified target language.

---

## API Configuration

This script interacts with the Bazarr API. Refer to the [Bazarr API documentation](https://bazarr.media/) for additional details about endpoints and parameters.

---

## License

This script is provided "as-is" without any warranties. Use it at your own risk.

---

For any issues or feature requests, feel free to create a GitHub issue. Happy subtitle downloading and translating! 🚀
