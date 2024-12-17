# Subtitle Translation Automation for Bazarr

This repository contains a Python script that automates the process of fetching, downloading, and translating subtitles for movies and TV episodes via the [Bazarr API](https://github.com/morpheus65535/bazarr). This tool supports parallel processing to handle multiple subtitle translations at once and includes robust error handling and retry mechanisms.

---

## Prerequisites

1. **Bazarr**: Ensure you have [Bazarr](https://www.bazarr.media/) installed and running on your server.
2. **API Key**: Obtain your Bazarr API key from the Bazarr settings.
3. **Python 3.8+**: This script is written in Python 3 and requires a modern version of Python.
4. **Dependencies**: Install required Python libraries using:

   ```bash
   pip install requests
   ```
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

## Configuration

Update the following variables at the top of the script to match your setup:

- `BAZARR_HOSTNAME`: The hostname or IP address where Bazarr is running (e.g., `localhost:1337` or `192.168.1.100:1337`).
- `BAZARR_APIKEY`: Your Bazarr API key for authentication.
- `FIRST_LANG`: Your primary target language for subtitle translation (e.g., `et` for Estonian).
- `SECOND_LANG`: (Optional) Your secondary target language for subtitle translation (e.g., `sv` for Swedish).
- `MAX_PARALLEL_TRANSLATIONS`: The maximum number of subtitle translations to process in parallel.

---

## Key Features

### 1. **Fetch Items**

The script fetches wanted movies or episodes where subtitles are missing using the Bazarr API.

- *Endpoint for Movies*: `movies/wanted`
- *Endpoint for Episodes*: `episodes/wanted`

### 2. **Download Subtitles**

The script downloads English subtitles (`en`) as a prerequisite for translation. The downloaded subtitle paths are then retrieved and URL-encoded for further processing.

### 3. **Subtitle Translation**

Subtitles are translated to the preferred target languages:
- Primary language (`FIRST_LANG`)
- Secondary language (`SECOND_LANG`) if configured

If an HTTP 500 status code is encountered during translation, the script implements a retry mechanism:
- Re-downloads English subtitles.
- Re-attempts translation using the updated subtitles.

### 4. **Parallel Processing**

Using Python's `ThreadPoolExecutor`, the script processes multiple movies or episodes simultaneously, improving efficiency for large libraries.

---

## Usage

1. Run the script:

   ```bash
   python translate_subtitles.py
   ```

2. The script will:
   - Fetch wanted movies and episodes from Bazarr.
   - Download missing English subtitles.
   - Translate the subtitles to your preferred languages.
   - Process multiple items in parallel based on the `MAX_PARALLEL_TRANSLATIONS` setting.

---

## Functions

### `main()`

Entry point for the program. It coordinates subtitle translation for both movies and TV episodes.

### `translate_movie_subs()`

Processes all wanted movies from Bazarr:
- Fetches movie IDs.
- Downloads missing English subtitles.
- Translates them to the target languages.

### `translate_episode_subs()`

Similar to `translate_movie_subs`, but processes TV episodes instead.

### `process_item(item, item_type, id_field, params_field)`

Processes a single movie or episode:
1. Downloads missing English subtitles.
2. Fetches the English subtitle path.
3. Translates the subtitles to the preferred target languages (`FIRST_LANG` and `SECOND_LANG`).

### `process_items(item_type, wanted_endpoint, id_field, params_field)`

Processes all items (movies or episodes) of a specific type in parallel using `ThreadPoolExecutor`.

### `fetch_items(item_type, wanted_endpoint)`

Fetches a list of wanted movies or episodes from Bazarr using a specific endpoint.

### `download_subtitles(item_type, item_id, params_field, language)`

Downloads subtitles for a given movie or episode in the specified language (`en`, `FIRST_LANG`, or `SECOND_LANG`).

### `fetch_subtitle_path(item_type, item_id, params_field)`

Retrieves the path to downloaded English subtitles and URL-encodes it for translation processing.

### `translate_subtitle(item_type, item_id, subs_path, target_lang, params_field, series_id=None)`

Translates subtitles to the target language. Implements a retry mechanism in case of failure.

---

## Debugging

The script includes debug messages for every major step of the process:
- `[INFO]`: General information about the current operation.
- `[DEBUG]`: Detailed information for debugging purposes, such as URL construction or response details.
- `[WARNING]`: Non-critical issues, such as subtitle download failure.
- `[ERROR]`: Critical issues that prevent further processing for a specific item.

---

## Limitations

1. **API Rate Limits**: The script does not account for API rate limits. Avoid settings that result in excessive API requests in a short time.
2. **Retry Logic**: Translation retries are only triggered for HTTP 500 errors. Other errors do not trigger retries.
3. **Incomplete Language Support**: If Bazarr does not support the specified target language, translation will fail.

---

## Extending the Script

### Add Support for More Languages
To add a third or additional language, modify `process_item()` and include the new language in the steps where subtitles are checked, downloaded, and translated.

### Customize Processing Logic
You can modify `process_items()` to handle additional item types or integrate with other media management tools.

---

## License

This project is open source

---

For questions or feature requests, please open an issue on this repository. 🎉
