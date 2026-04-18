import os
import re as _re
import sys
import signal
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlencode

import requests

# Unbuffered output
sys.stdout = os.fdopen(sys.stdout.fileno(), "w", buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), "w", buffering=1)

# ANSI colors (disabled outside TTY)
_tty = sys.stdout.isatty()
GREEN    = "\033[92m" if _tty else ""
YELLOW   = "\033[93m" if _tty else ""
RED      = "\033[91m" if _tty else ""
CYAN     = "\033[96m" if _tty else ""
BOLD     = "\033[1m"  if _tty else ""
RESET    = "\033[0m"  if _tty else ""

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _require(var: str) -> str:
    val = os.getenv(var, "").strip()
    if not val:
        print(f"{RED}[ERROR] {var} environment variable is required{RESET}")
        sys.exit(1)
    return val

def _normalize_url(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    return raw

_raw_languages = os.getenv("LANGUAGES", "en,et,sv")
LANGUAGES           = [l.strip() for l in _raw_languages.split(",") if l.strip()]
BAZARR_URL          = _normalize_url(_require("BAZARR_URL"))
BAZARR_API_KEY      = _require("BAZARR_API_KEY")
LINGARR_URL_RAW     = os.getenv("LINGARR_URL", "").strip()
LINGARR_URL         = _normalize_url(LINGARR_URL_RAW) if LINGARR_URL_RAW else None
LINGARR_API_KEY     = os.getenv("LINGARR_API_KEY", "").strip()
PARALLEL_TRANSLATES = max(1, int(os.getenv("PARALLEL_TRANSLATES", "1")))
CHECK_INTERVAL      = max(10, int(os.getenv("CHECK_INTERVAL", "1200")))
CONNECT_TIMEOUT     = max(5, int(os.getenv("CONNECT_TIMEOUT", "10")))
POLL_INTERVAL       = max(5, int(os.getenv("POLL_INTERVAL", "20")))
POLL_TIMEOUT        = max(30, int(os.getenv("POLL_TIMEOUT", "600")))
RESUBMIT_COOLDOWN   = max(60, int(os.getenv("RESUBMIT_COOLDOWN", "3600")))
DISK_IMPORT_WAIT    = 120  # seconds to wait for Bazarr to import after file appears on disk

if not LANGUAGES:
    print(f"{RED}[ERROR] LANGUAGES must contain at least one language code{RESET}")
    sys.exit(1)

BAZARR_HEADERS: dict = {"Accept": "application/json", "X-API-KEY": BAZARR_API_KEY}
LINGARR_HEADERS: dict = {"Accept": "application/json"}
if LINGARR_API_KEY:
    LINGARR_HEADERS["X-Api-Key"] = LINGARR_API_KEY

# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

shutdown_requested = False

def _handle_signal(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    print(f"\n{YELLOW}[WARNING] Signal {signum} received — finishing current jobs then stopping.{RESET}")
    sys.stdout.flush()

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Resubmit cooldown cache
# Prevents re-submitting a translation that was already sent recently,
# even if Bazarr still shows the item as wanted (e.g. still importing).
# ---------------------------------------------------------------------------

_submitted_cache: dict[tuple, float] = {}  # (item_id, target_lang) -> submitted_at
_cache_lock = threading.Lock()

def _check_cooldown(item_id: int, target_lang: str) -> int | None:
    """Returns seconds since last submission if within cooldown, else None."""
    key = (item_id, target_lang)
    with _cache_lock:
        submitted_at = _submitted_cache.get(key)
    if submitted_at is None:
        return None
    age = int(time.time() - submitted_at)
    return age if age < RESUBMIT_COOLDOWN else None

def _record_submission(item_id: int, target_lang: str) -> None:
    with _cache_lock:
        _submitted_cache[(item_id, target_lang)] = time.time()

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def bazarr_url(endpoint: str) -> str:
    return f"{BAZARR_URL}/api/{endpoint}"

def lingarr_url(endpoint: str) -> str | None:
    if not LINGARR_URL:
        return None
    return f"{LINGARR_URL}/api/{endpoint}"

# ---------------------------------------------------------------------------
# Bazarr API
# ---------------------------------------------------------------------------

def fetch_wanted(item_type: str) -> list:
    url = bazarr_url(f"{item_type}/wanted")
    try:
        r = requests.get(url, headers=BAZARR_HEADERS,
                         params={"start": 0, "length": -1},
                         timeout=CONNECT_TIMEOUT)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        print(f"{RED}[ERROR] fetch_wanted({item_type}): {e}{RESET}")
        return []


def fetch_subtitles(item_type: str, item_id: int, id_field: str) -> list:
    if item_type == "episodes":
        url = bazarr_url("episodes")
        params = {"episodeid[]": item_id}
    else:
        url = bazarr_url("movies")
        params = {"radarrid[]": item_id}
    try:
        r = requests.get(url, headers=BAZARR_HEADERS, params=params, timeout=CONNECT_TIMEOUT)
        r.raise_for_status()
        data = r.json().get("data", [])
        if data:
            return data[0].get("subtitles", [])
    except Exception as e:
        print(f"{RED}[ERROR] fetch_subtitles({item_type}, {item_id}): {e}{RESET}")
    return []


def submit_translate(item_type: str, item_id: int, source_path: str, target_lang: str) -> bool:
    api_type = "episode" if item_type == "episodes" else "movie"
    encoded_path = quote(source_path, safe="")
    simple = urlencode({
        "action": "translate",
        "language": target_lang,
        "type": api_type,
        "id": item_id,
        "forced": "false",
        "hi": "false",
    })
    url = f"{bazarr_url('subtitles')}?{simple}&path={encoded_path}"
    try:
        r = requests.patch(url, headers=BAZARR_HEADERS, timeout=CONNECT_TIMEOUT)
        return r.status_code == 204
    except Exception as e:
        print(f"{RED}[ERROR] submit_translate({item_type}, {item_id}, {target_lang}): {e}{RESET}")
        return False

# ---------------------------------------------------------------------------
# Lingarr API (optional tracking)
# ---------------------------------------------------------------------------

def lingarr_active_count() -> int | None:
    url = lingarr_url("TranslationRequest/active")
    if not url:
        return None
    try:
        r = requests.get(url, headers=LINGARR_HEADERS, timeout=CONNECT_TIMEOUT)
        if r.status_code == 200:
            return int(r.json())
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# Subtitle helpers
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = _re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")

def _count_dialogue_lines(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        count = 0
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.isdigit() or _TIMESTAMP_RE.match(stripped):
                continue
            count += 1
        return count
    except OSError:
        return None


def _estimate_timeout(source_path: str) -> int:
    n = _count_dialogue_lines(source_path)
    if n is None:
        return POLL_TIMEOUT
    estimated = int(n * 1.1 * 1.2)
    timeout = max(POLL_TIMEOUT, estimated)
    print(f"[INFO] Source has {n} dialogue lines — estimated ~{int(n * 1.1)}s, timeout set to {timeout}s")
    return timeout


def _derive_target_path(source_path: str, source_lang: str, target_lang: str) -> str | None:
    """
    Derive the expected translated subtitle path by replacing the source
    language code with the target language code in the filename.
    e.g. /media/Show/ep.en.srt -> /media/Show/ep.et.srt
    """
    basename = os.path.basename(source_path)
    marker = f".{source_lang}."
    idx = basename.rfind(marker)
    if idx == -1:
        return None
    new_basename = basename[:idx] + f".{target_lang}." + basename[idx + len(marker):]
    return os.path.join(os.path.dirname(source_path), new_basename)

# ---------------------------------------------------------------------------
# Poll-and-verify
# ---------------------------------------------------------------------------

def wait_for_subtitle(item_type: str, item_id: int, id_field: str,
                      target_lang: str, deadline: float,
                      target_path: str | None = None) -> bool:
    """
    Polls every POLL_INTERVAL seconds for `target_lang` to appear in Bazarr.
    Also checks if the file exists on disk. If it does, waits up to
    DISK_IMPORT_WAIT (2 min) for Bazarr to import it, then moves on.
    """
    start = time.time()
    disk_found_at: float | None = None

    while not shutdown_requested:
        # Check Bazarr first
        subs = fetch_subtitles(item_type, item_id, id_field)
        available = {s["code2"] for s in subs if s.get("path")}
        if target_lang in available:
            elapsed = int(time.time() - start)
            print(f"{GREEN}[OK] [{item_type}:{item_id}] '{target_lang}' confirmed by Bazarr after {elapsed}s{RESET}")
            return True

        # Check disk
        if target_path:
            if os.path.exists(target_path):
                if disk_found_at is None:
                    disk_found_at = time.time()
                    print(f"[DISK] [{item_type}:{item_id}] '{target_lang}' found on disk — "
                          f"waiting up to {DISK_IMPORT_WAIT}s for Bazarr to import...")
                elif time.time() - disk_found_at >= DISK_IMPORT_WAIT:
                    elapsed = int(time.time() - start)
                    print(f"{GREEN}[OK] [{item_type}:{item_id}] '{target_lang}' on disk, "
                          f"Bazarr still importing after {elapsed}s — moving on{RESET}")
                    return True

        now = time.time()
        if now >= deadline:
            print(f"{YELLOW}[TIMEOUT] [{item_type}:{item_id}] '{target_lang}' "
                  f"not found after {int(now - start)}s{RESET}")
            return False

        remaining = int(deadline - now)
        elapsed = int(now - start)
        disk_note = " (file on disk)" if disk_found_at else ""
        print(f"[POLL] [{item_type}:{item_id}] Waiting for '{target_lang}'{disk_note}... "
              f"{elapsed}s elapsed, {remaining}s remaining")
        for _ in range(POLL_INTERVAL):
            if shutdown_requested:
                return False
            time.sleep(1)
    return False

# ---------------------------------------------------------------------------
# Per-item processor
# ---------------------------------------------------------------------------

def _item_title(item: dict, item_type: str) -> str:
    if item_type == "episodes":
        series = item.get("seriesTitle", item.get("series_title", "Unknown"))
        ep = item.get("title", item.get("episode_title", ""))
        return f"{series} - {ep}" if ep else series
    return item.get("title", "Unknown")


def process_item(item: dict, item_type: str, id_field: str,
                 stats: dict, stats_lock: threading.Lock) -> None:
    if shutdown_requested:
        return

    item_id = item.get(id_field)
    if item_id is None:
        return
    title = _item_title(item, item_type)

    missing_raw = {s.get("code2") for s in item.get("missing_subtitles", []) if s.get("code2")}
    missing = {l for l in LANGUAGES if l in missing_raw}

    if not missing:
        return

    subs = fetch_subtitles(item_type, item_id, id_field)
    available_map = {s["code2"]: s.get("path", "") for s in subs if s.get("code2") and s.get("path")}

    target_langs = [l for l in LANGUAGES if l in missing and l not in available_map]
    source_langs = [l for l in LANGUAGES if l in available_map]

    if not source_langs:
        print(f"[SKIP] {title}: no source subtitle available from {LANGUAGES}")
        return
    if not target_langs:
        return

    source_lang = source_langs[0]
    source_path = available_map[source_lang]
    item_timeout = _estimate_timeout(source_path)
    print(f"[INFO] {title}: source={source_lang}, targets={target_langs}")

    for target_lang in target_langs:
        if shutdown_requested:
            break

        # Skip if submitted recently (Bazarr may still be importing)
        age = _check_cooldown(item_id, target_lang)
        if age is not None:
            cooldown_remaining = RESUBMIT_COOLDOWN - age
            print(f"[SKIP] {title} '{target_lang}': submitted {age}s ago, "
                  f"cooldown {cooldown_remaining}s remaining — Bazarr may still be importing")
            continue

        target_path = _derive_target_path(source_path, source_lang, target_lang)

        print(f"[TRANSLATE] {title}: {source_lang} -> {target_lang}")
        ok = submit_translate(item_type, item_id, source_path, target_lang)
        if ok:
            _record_submission(item_id, target_lang)
            with stats_lock:
                stats["submitted"] += 1
            deadline = time.time() + item_timeout
            found = wait_for_subtitle(item_type, item_id, id_field, target_lang,
                                      deadline, target_path)
            with stats_lock:
                if found:
                    stats["completed"] += 1
                    stats["translations"].append(f"{title}: {source_lang} -> {target_lang}")
                else:
                    stats["timed_out"] += 1
        else:
            print(f"{RED}[FAIL] submit_translate failed: {title} {source_lang}->{target_lang}{RESET}")
            with stats_lock:
                stats["failed"] += 1

# ---------------------------------------------------------------------------
# Cycle orchestrator
# ---------------------------------------------------------------------------

def run_cycle(cycle_num: int) -> None:
    print(f"\n{BOLD}{CYAN}===== Cycle #{cycle_num} ====={RESET}")

    active_before = lingarr_active_count()
    if active_before is not None:
        print(f"[INFO] Lingarr active queue at cycle start: {active_before}")

    stats: dict = {"submitted": 0, "completed": 0, "timed_out": 0, "failed": 0, "translations": []}
    stats_lock = threading.Lock()

    work: list[tuple] = []
    for ep in fetch_wanted("episodes"):
        work.append((ep, "episodes", "sonarrEpisodeId"))
    for mv in fetch_wanted("movies"):
        work.append((mv, "movies", "radarrId"))

    if not work:
        print("[INFO] No wanted items found.")
    else:
        print(f"[INFO] Processing {len(work)} item(s) with {PARALLEL_TRANSLATES} worker(s)...")
        with ThreadPoolExecutor(max_workers=PARALLEL_TRANSLATES) as executor:
            futures = {
                executor.submit(process_item, item, itype, ifield, stats, stats_lock): (item, itype)
                for item, itype, ifield in work
            }
            for future in as_completed(futures):
                if shutdown_requested:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    future.result()
                except Exception as e:
                    print(f"{RED}[ERROR] Worker exception: {e}{RESET}")

    print(f"\n{BOLD}===== Cycle #{cycle_num} Summary ====={RESET}")
    print(f"  Submitted  : {stats['submitted']}")
    print(f"  Completed  : {stats['completed']}")
    print(f"  Timed out  : {stats['timed_out']}")
    print(f"  Failed     : {stats['failed']}")
    if stats["translations"]:
        print("  Completed translations:")
        for t in stats["translations"]:
            print(f"    {GREEN}- {t}{RESET}")
    active_after = lingarr_active_count()
    if active_after is not None:
        print(f"  Lingarr active queue now: {active_after}")
    sys.stdout.flush()

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"\n{BOLD}Bazarr AutoTranslate starting{RESET}")
    print(f"  Bazarr URL        : {BAZARR_URL}")
    print(f"  Lingarr URL       : {LINGARR_URL or '(not configured)'}")
    print(f"  Languages         : {', '.join(LANGUAGES)}")
    print(f"  Parallel workers  : {PARALLEL_TRANSLATES}")
    print(f"  Check interval    : {CHECK_INTERVAL}s")
    print(f"  Poll interval     : {POLL_INTERVAL}s  (max {POLL_TIMEOUT}s per translation)")
    print(f"  Resubmit cooldown : {RESUBMIT_COOLDOWN}s")
    print(f"  Disk import wait  : {DISK_IMPORT_WAIT}s")
    sys.stdout.flush()

    cycle = 1
    while not shutdown_requested:
        run_cycle(cycle)
        cycle += 1
        if shutdown_requested:
            break
        print(f"[INFO] Next cycle in {CHECK_INTERVAL}s...")
        for _ in range(CHECK_INTERVAL):
            if shutdown_requested:
                break
            time.sleep(1)

    print("[INFO] Bazarr AutoTranslate stopped cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
