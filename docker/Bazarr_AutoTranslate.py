import requests
import time
import os
import sys
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# Force unbuffered output
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

# Disable ANSI colors if not in a TTY
USE_COLORS = sys.stdout.isatty()

# ANSI Escape Color Codes
GREEN = "\033[92m" if USE_COLORS else ""
YELLOW = "\033[93m" if USE_COLORS else ""
RED = "\033[91m" if USE_COLORS else ""
BOLD_RED = "\033[1;91m" if USE_COLORS else ""
RESET = "\033[0m" if USE_COLORS else ""

# Configuration from environment variables
MAX_PARALLEL_TRANSLATIONS = int(os.getenv("MAX_PARALLEL_TRANSLATIONS", "2"))
TRANSLATE_DELAY = float(os.getenv("TRANSLATE_DELAY", "0.3"))
BAZARR_HOSTNAME = os.getenv("BAZARR_HOSTNAME", "localhost:6767")
BAZARR_APIKEY = os.getenv("BAZARR_APIKEY", "")
CONNECT_TIMEOUT = int(os.getenv("CONNECT_TIMEOUT", "10"))
FIRST_LANG = os.getenv("FIRST_LANG", "et")
SECOND_LANG = os.getenv("SECOND_LANG", "sv")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # 5 minutes default
TRANSLATION_VERIFY_TIMEOUT = int(os.getenv("TRANSLATION_VERIFY_TIMEOUT", "300"))  # max seconds to wait for async translation
TRANSLATION_POLL_INTERVAL = int(os.getenv("TRANSLATION_POLL_INTERVAL", "10"))   # how often to poll

if not BAZARR_APIKEY:
    print(f"{BOLD_RED}[ERROR] BAZARR_APIKEY environment variable is required{RESET}")
    sys.exit(1)

HEADERS = {"Accept": "application/json", "X-API-KEY": BAZARR_APIKEY}

# Global flag for graceful shutdown
shutdown_requested = False

def signal_handler(signum, frame):
    """Handle termination signals gracefully."""
    global shutdown_requested
    shutdown_requested = True
    print(f"{RED}[WARNING] Received signal {signum}. Initiating graceful shutdown...{RESET}")
    sys.stdout.flush()

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

def get_api_url(endpoint):
    """Construct the full API URL for a given endpoint."""
    # Check if hostname already has http:// or https://
    if BAZARR_HOSTNAME.startswith(('http://', 'https://')):
        base_url = BAZARR_HOSTNAME
    else:
        base_url = f"http://{BAZARR_HOSTNAME}"
    return f"{base_url}/api/{endpoint}"

def fetch_items(item_type, wanted_endpoint):
    """Fetch the list of wanted episodes or movies."""
    thread_name = threading.current_thread().name
    url = get_api_url(wanted_endpoint)
    print(f"{YELLOW}[DEBUG] [{thread_name}] Fetching {item_type} from URL: {url}{RESET}")

    try:
        response = requests.get(url, headers=HEADERS, timeout=CONNECT_TIMEOUT)
        print(f"{YELLOW}[DEBUG] [{thread_name}] Response status code: {response.status_code}{RESET}")
        sys.stdout.flush()
        if response.status_code == 200:
            items = response.json().get("data", [])
            print(f"{GREEN}[INFO] [{thread_name}] Fetched {len(items)} {item_type}(s).{RESET}")
            return items
        else:
            print(f"{RED}[WARNING] [{thread_name}] Failed to fetch {item_type}: {response.status_code}{RESET}")
            return []
    except requests.Timeout:
        print(f"{BOLD_RED}[ERROR] [{thread_name}] Timeout while fetching {item_type}{RESET}")
        sys.stdout.flush()
        return []
    except requests.RequestException as e:
        print(f"{BOLD_RED}[ERROR] [{thread_name}] Exception occurred while fetching {item_type}: {e}{RESET}")
        sys.stdout.flush()
        return []

def download_subtitles(item_type, item_id, params_field, language="en", series_id=None):
    """Download subtitles for a given item."""
    url = get_api_url(f"{item_type}/subtitles")
    
    if item_type == "episodes" and series_id:
        query_params = f"seriesid={series_id}&{params_field}={item_id}&language={language}&forced=False&hi=False"
    else:
        query_params = f"{params_field}={item_id}&language={language}&forced=False&hi=False"
    
    constructed_url = f"{url}?{query_params}"

    print(f"{YELLOW}[DEBUG] Attempting to download {language} subtitles from URL: {constructed_url}{RESET}")
    sys.stdout.flush()
    try:
        response = requests.patch(constructed_url, headers=HEADERS, timeout=CONNECT_TIMEOUT)
        print(f"{YELLOW}[DEBUG] Response status code: {response.status_code}{RESET}")
        if response.status_code == 204:
            print(f"{GREEN}[INFO] Subtitles in language '{language}' successfully downloaded for {item_type} (ID: {item_id}).{RESET}")
            return verify_translation_complete(item_type, item_id, params_field, language)
        else:
            print(f"{RED}[WARNING] Failed to download subtitles in language '{language}'. Response Code: {response.status_code}{RESET}")
            return False
    except requests.Timeout:
        print(f"{BOLD_RED}[ERROR] Timeout while downloading subtitles in language '{language}'{RESET}")
        sys.stdout.flush()
        return False
    except requests.RequestException as e:
        print(f"{BOLD_RED}[ERROR] Exception occurred while trying to download subtitles in language '{language}': {e}{RESET}")
        sys.stdout.flush()
        return False

def fetch_subtitle_path(item_type, item_id, params_field, language="en"):
    """Retrieve the path of subtitles for a given item."""
    thread_name = threading.current_thread().name

    url = get_api_url(item_type)
    constructed_url = f"{url}?{params_field}%5B%5D={item_id}"
    print(f"{YELLOW}[DEBUG] [{thread_name}] Fetching subtitle path for {item_type} (ID: {item_id}, Language: {language}) from URL: {constructed_url}{RESET}")

    try:
        response = requests.get(constructed_url, headers=HEADERS, timeout=CONNECT_TIMEOUT)
        print(f"{YELLOW}[DEBUG] [{thread_name}] Response status code for {item_type} (ID: {item_id}): {response.status_code}{RESET}")
        sys.stdout.flush()
        
        if response.status_code == 200:
            history = response.json()
            for item in history.get("data", []):
                if "subtitles" in item:
                    for subtitle in item["subtitles"]:
                        if subtitle.get("code2") == language:
                            raw_path = subtitle["path"]
                            print(f"{GREEN}[INFO] [{thread_name}] Found subtitle path for {item_type} (ID: {item_id}, Language: {language}): {raw_path}{RESET}")
                            return raw_path
        else:
            print(f"{RED}[WARNING] [{thread_name}] Failed to fetch subtitle path for {item_type} (ID: {item_id}). Response Code: {response.status_code}{RESET}")
    except requests.Timeout:
        print(f"{BOLD_RED}[ERROR] [{thread_name}] Timeout while fetching subtitle path for {item_type} (ID: {item_id}){RESET}")
        sys.stdout.flush()
    except requests.RequestException as e:
        print(f"{BOLD_RED}[ERROR] [{thread_name}] Exception occurred while fetching subtitle path for {item_type} (ID: {item_id}): {e}{RESET}")
        sys.stdout.flush()
    
    return None

def verify_translation_complete(item_type, item_id, params_field, target_lang, max_wait=None, poll_interval=None):
    """Poll until the target language subtitle appears, confirming async translation finished."""
    max_wait = max_wait if max_wait is not None else TRANSLATION_VERIFY_TIMEOUT
    poll_interval = poll_interval if poll_interval is not None else TRANSLATION_POLL_INTERVAL
    thread_name = threading.current_thread().name
    elapsed = 0
    while elapsed < max_wait:
        subtitles = fetch_subtitle_data(item_type, item_id, params_field)
        available = [s.get("code2") for s in subtitles if isinstance(subtitles, list)]
        if target_lang in available:
            print(f"{GREEN}[INFO] [{thread_name}] Verified: '{target_lang}' subtitle present for {item_type} (ID: {item_id}) after {elapsed}s.{RESET}")
            sys.stdout.flush()
            if elapsed == 0:
                # Translation completed near-instantly; wait one poll interval before
                # the next submission so we don't hammer the translation service.
                print(f"{YELLOW}[DEBUG] [{thread_name}] Fast translation detected, waiting {poll_interval}s before next job.{RESET}")
                sys.stdout.flush()
                time.sleep(poll_interval)
            return True
        print(f"{YELLOW}[DEBUG] [{thread_name}] '{target_lang}' not yet available for {item_type} (ID: {item_id}), retrying in {poll_interval}s... ({elapsed}s/{max_wait}s){RESET}")
        sys.stdout.flush()
        time.sleep(poll_interval)
        elapsed += poll_interval
        sys.stdout.flush()
    print(f"{RED}[WARNING] [{thread_name}] Timed out waiting for '{target_lang}' translation for {item_type} (ID: {item_id}).{RESET}")
    sys.stdout.flush()
    return False

def translate_subtitle(item_type, item_id, subs_path, target_lang, params_field, series_id=None):
    """Translate subtitles to the specified target language."""
    thread_name = threading.current_thread().name

    print(f"{YELLOW}[DEBUG] [{thread_name}] Translating subtitles for {item_type} (ID: {item_id}) to language: {target_lang}{RESET}")

    if not target_lang:
        print(f"{YELLOW}[DEBUG] [{thread_name}] Target language not specified. Skipping translation for {item_type} (ID: {item_id}).{RESET}")
        return False

    singular_item_type = "movie" if item_type == "movies" else "episode" if item_type == "episodes" else item_type

    url = get_api_url("subtitles")
    params = {
        "action": "translate",
        "language": target_lang,
        "path": subs_path,
        "type": singular_item_type,
        "id": item_id,
        "forced": "False",
        "hi": "False",
    }

    print(f"{YELLOW}[DEBUG] [{thread_name}] Translating {item_type} (ID: {item_id}) to '{target_lang}' using path: {subs_path}{RESET}")

    try:
        start_time = time.time()
        response = requests.patch(url, params=params, headers=HEADERS, timeout=CONNECT_TIMEOUT)
        elapsed_time = time.time() - start_time
        print(f"{GREEN}[INFO] [{thread_name}] Time taken for API request: {elapsed_time:.2f} seconds for {item_type} (ID: {item_id}){RESET}")

        if response.status_code == 204:
            print(f"{GREEN}[INFO] [{thread_name}] Subtitles translated successfully to {target_lang} for {item_type} (ID: {item_id}).{RESET}")
            return verify_translation_complete(item_type, item_id, params_field, target_lang)
        elif response.status_code == 500:
            print(f"{RED}[WARNING] [{thread_name}] Received status code 500 for {item_type} (ID: {item_id}). Retrying...{RESET}")

            if download_subtitles(item_type, item_id, params_field, language="en", series_id=series_id):
                new_subs_path = fetch_subtitle_path(item_type, item_id, params_field)
                if new_subs_path:
                    retry_params = {
                        "action": "translate",
                        "language": target_lang,
                        "path": new_subs_path,
                        "type": singular_item_type,
                        "id": item_id,
                        "forced": "False",
                        "hi": "False",
                    }
                    retry_response = requests.patch(url, params=retry_params, headers=HEADERS, timeout=CONNECT_TIMEOUT)

                    if retry_response.status_code == 204:
                        print(f"{GREEN}[INFO] [{thread_name}] Subtitles successfully translated to {target_lang} after retry for {item_type} (ID: {item_id}).{RESET}")
                        return verify_translation_complete(item_type, item_id, params_field, target_lang)
                    else:
                        print(f"{RED}[WARNING] [{thread_name}] Retry translation failed for {item_type} (ID: {item_id}). Response Code: {retry_response.status_code}{RESET}")

        print(f"{RED}[WARNING] [{thread_name}] Failed to translate subtitles for {item_type} (ID: {item_id}). Response Code: {response.status_code}{RESET}")
        return False
    except requests.Timeout:
        print(f"{BOLD_RED}[ERROR] [{thread_name}] Timeout occurred while translating subtitles for {item_type} (ID: {item_id}).{RESET}")
        return False
    except requests.RequestException as e:
        print(f"{BOLD_RED}[ERROR] [{thread_name}] Exception occurred while translating subtitles for {item_type} (ID: {item_id}): {e}{RESET}")
        return False
    finally:
        time.sleep(TRANSLATE_DELAY)

def fetch_subtitle_data(item_type, item_id, params_field):
    """Fetch available subtitles for a specific episode or movie."""
    thread_name = threading.current_thread().name
    url = get_api_url(f"{item_type}?length=-1&{params_field}%5B%5D={item_id}")
    try:
        response = requests.get(url, headers=HEADERS, timeout=CONNECT_TIMEOUT)
        sys.stdout.flush()
        if response.status_code == 200:
            subtitle_response = response.json()
            for item in subtitle_response.get("data", []):
                if "subtitles" in item:
                    return item["subtitles"]
            return []
        else:
            print(f"{RED}[WARNING] [{thread_name}] Failed to fetch subtitles: {response.status_code}{RESET}")
            return []
    except requests.Timeout:
        print(f"{BOLD_RED}[ERROR] [{thread_name}] Timeout while fetching subtitles{RESET}")
        sys.stdout.flush()
        return []
    except requests.RequestException as e:
        print(f"{BOLD_RED}[ERROR] [{thread_name}] Exception while fetching subtitles: {e}{RESET}")
        sys.stdout.flush()
        return []

def process_item(item, item_type, id_field, params_field):
    """Process a single item: Determine missing subtitles, download, and translate."""
    thread_name = threading.current_thread().name

    if shutdown_requested:
        print(f"{RED}[WARNING] [{thread_name}] Shutdown requested. Stopping processing.{RESET}")
        sys.stdout.flush()
        return

    item_id = item[id_field]
    item_name = item.get("seriesTitle", item.get("title", "Unknown Item"))
    series_id = item.get("sonarrSeriesId") if item_type == "episodes" else None

    print(f"{GREEN}[INFO] [{thread_name}] Processing: {item_name} (ID: {item_id}){RESET}")

    missing_subtitles = item.get("missing_subtitles", [])
    missing_languages = [lang["code2"] for lang in missing_subtitles]

    subtitles_data = fetch_subtitle_data(item_type, item_id, params_field)
    available_languages = [lang.get("code2", "Unknown") for lang in subtitles_data] if isinstance(subtitles_data, list) else []

    print(f"{YELLOW}[DEBUG] [{thread_name}] Missing languages: {missing_languages}, Available languages: {available_languages}{RESET}")

    subs_path = None
    
    if "en" in missing_languages and "en" not in available_languages:
        if not download_subtitles(item_type, item_id, params_field, language="en", series_id=series_id) or not (subs_path := fetch_subtitle_path(item_type, item_id, params_field, language="en")):
            print(f"{RED}[WARNING] [{thread_name}] English subtitles unavailable. Trying another language.{RESET}")
            preferred_languages = [SECOND_LANG, FIRST_LANG]
            for lang in preferred_languages:
                if lang in available_languages:
                    subs_path = fetch_subtitle_path(item_type, item_id, params_field, language=lang)
                    print(f"{GREEN}[INFO] [{thread_name}] Using {lang} subtitles for translation.{RESET}")
                    
                    if "en" in missing_languages:
                        if translate_subtitle(item_type, item_id, subs_path, "en", params_field, series_id=series_id):
                            print(f"{GREEN}[INFO] [{thread_name}] Translated {item_name} subtitles to en.{RESET}")
                            en_path = fetch_subtitle_path(item_type, item_id, params_field, language="en")
                            if en_path:
                                subs_path = en_path
                                print(f"{GREEN}[INFO] [{thread_name}] Switched source to newly translated EN subtitle.{RESET}")
                    break
            else:
                print(f"{RED}[WARNING] [{thread_name}] No usable subtitles found for translation. Skipping...{RESET}")
                return
    else:
        subs_path = fetch_subtitle_path(item_type, item_id, params_field)

    if not subs_path:
        print(f"{RED}[WARNING] [{thread_name}] Failed to retrieve subtitle path for {item_name}. Skipping...{RESET}")
        return
    
    if FIRST_LANG in missing_languages and FIRST_LANG not in available_languages:
        if translate_subtitle(item_type, item_id, subs_path, FIRST_LANG, params_field, series_id=series_id):
            print(f"{GREEN}[INFO] [{thread_name}] Translated {item_name} subtitles to {FIRST_LANG}.{RESET}")

    if SECOND_LANG and SECOND_LANG in missing_languages and SECOND_LANG not in available_languages:
        if translate_subtitle(item_type, item_id, subs_path, SECOND_LANG, params_field, series_id=series_id):
            print(f"{GREEN}[INFO] [{thread_name}] Translated {item_name} subtitles to {SECOND_LANG}.{RESET}")

def process_items(item_type, wanted_endpoint, id_field, params_field):
    """Process all wanted items of a specified type with parallel processing."""
    if shutdown_requested:
        print(f"{RED}[WARNING] Shutdown requested. Stopping execution.{RESET}")
        sys.stdout.flush()
        return

    items = fetch_items(item_type, wanted_endpoint)
    if not items:
        print(f"{GREEN}[INFO] No {item_type} items found to process.{RESET}")
        return

    print(f"{GREEN}[INFO] Beginning parallel processing for {len(items)} {item_type}(s).{RESET}")
    
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_TRANSLATIONS) as executor:
        future_to_item = {
            executor.submit(process_item, item, item_type, id_field, params_field): item for item in items
        }

        for future in as_completed(future_to_item):
            if shutdown_requested:
                print(f"{RED}[WARNING] Shutdown requested. Cancelling remaining tasks.{RESET}")
                sys.stdout.flush()
                executor.shutdown(wait=False, cancel_futures=True)
                return

            item = future_to_item[future]
            item_name = item.get("seriesTitle", item.get("title", "Unknown Item"))
            item_id = item.get(id_field, "Unknown ID")
            try:
                future.result()
                print(f"{GREEN}[INFO] Successfully completed processing for item: {item_name} (ID: {item_id}){RESET}")
                sys.stdout.flush()
            except Exception as e:
                print(f"{BOLD_RED}[ERROR] Failed to process item: {item_name} (ID: {item_id}). Exception: {e}{RESET}")
                sys.stdout.flush()

def translate_episode_subs():
    """Translate subtitles for all wanted episodes."""
    process_items(
        item_type="episodes",
        wanted_endpoint="episodes/wanted?start=0&length=-1",
        id_field="sonarrEpisodeId",
        params_field="episodeid",
    )

def translate_movie_subs():
    """Translate subtitles for all wanted movies."""
    process_items(
        item_type="movies",
        wanted_endpoint="movies/wanted?start=0&length=-1",
        id_field="radarrId",
        params_field="radarrid",
    )

def run_translation_cycle():
    """Run a single translation cycle for movies and episodes."""
    print(f"{GREEN}[INFO] Starting translation cycle...{RESET}")
    sys.stdout.flush()

    try:
        if not shutdown_requested:
            translate_movie_subs()
        
        if not shutdown_requested:
            translate_episode_subs()

        print(f"{GREEN}[INFO] Translation cycle completed.{RESET}")
        sys.stdout.flush()
        return True
    except Exception as e:
        print(f"{BOLD_RED}[ERROR] Unexpected error during translation cycle: {e}{RESET}")
        sys.stdout.flush()
        return False

def main():
    """Main loop - continuously check for missing subtitles and process them."""
    print(f"{GREEN}[INFO] Bazarr AutoTranslate started in continuous mode{RESET}")
    print(f"{GREEN}[INFO] Configuration:{RESET}")
    print(f"{GREEN}  - Bazarr Host: {BAZARR_HOSTNAME}{RESET}")
    print(f"{GREEN}  - Primary Language: {FIRST_LANG}{RESET}")
    print(f"{GREEN}  - Secondary Language: {SECOND_LANG}{RESET}")
    print(f"{GREEN}  - Check Interval: {CHECK_INTERVAL} seconds{RESET}")
    print(f"{GREEN}  - Max Parallel: {MAX_PARALLEL_TRANSLATIONS}{RESET}")
    sys.stdout.flush()

    cycle_count = 0
    
    while not shutdown_requested:
        cycle_count += 1
        print(f"{GREEN}[INFO] ===== Cycle #{cycle_count} ====={RESET}")
        
        run_translation_cycle()
        
        if shutdown_requested:
            break
        
        print(f"{GREEN}[INFO] Waiting {CHECK_INTERVAL} seconds before next check...{RESET}")
        sys.stdout.flush()
        
        # Sleep in small intervals to allow quick shutdown
        for _ in range(CHECK_INTERVAL):
            if shutdown_requested:
                break
            time.sleep(1)
    
    print(f"{GREEN}[INFO] Bazarr AutoTranslate shutting down gracefully.{RESET}")
    sys.stdout.flush()
    return 0

if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print(f"{RED}[WARNING] Script interrupted by user.{RESET}")
        sys.stdout.flush()
        exit_code = 130
    except Exception as e:
        print(f"{BOLD_RED}[ERROR] Unexpected error: {e}{RESET}")
        sys.stdout.flush()
        exit_code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.exit(exit_code)
