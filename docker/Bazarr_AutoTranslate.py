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
POLL_TIMEOUT        = max(30, int(os.getenv("POLL_TIMEOUT", "900")))
RESUBMIT_COOLDOWN   = max(60, int(os.getenv("RESUBMIT_COOLDOWN", "3600")))
DISK_IMPORT_WAIT    = 20   # seconds to wait for Bazarr to import after file appears on disk
DEBUG               = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

if not LANGUAGES:
    print(f"{RED}[ERROR] LANGUAGES must contain at least one language code{RESET}")
    sys.exit(1)

def dbg(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")

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
        dbg(f"_check_cooldown({item_id}, {target_lang!r}): clear")
        return None
    age = int(time.time() - submitted_at)
    in_cooldown = age < RESUBMIT_COOLDOWN
    dbg(f"_check_cooldown({item_id}, {target_lang!r}): age={age}s {'IN COOLDOWN' if in_cooldown else 'expired'}")
    return age if in_cooldown else None

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
    dbg(f"fetch_wanted({item_type}): GET {url}")
    try:
        r = requests.get(url, headers=BAZARR_HEADERS,
                         params={"start": 0, "length": -1},
                         timeout=CONNECT_TIMEOUT)
        r.raise_for_status()
        result = r.json().get("data", [])
        dbg(f"fetch_wanted({item_type}): {len(result)} item(s)")
        return result
    except Exception as e:
        print(f"{RED}[ERROR] fetch_wanted({item_type}): {e}{RESET}")
        return []


def fetch_subtitles(item_type: str, item_id: int, id_field: str) -> tuple[str, list]:
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
            vp = data[0].get("path", "")
            subs = data[0].get("subtitles", [])
            dbg(f"fetch_subtitles({item_type}, {item_id}): video_path={vp!r}")
            for s in subs:
                dbg(f"  sub code2={s.get('code2')!r} path={s.get('path', '')!r}")
            return vp, subs
    except Exception as e:
        print(f"{RED}[ERROR] fetch_subtitles({item_type}, {item_id}): {e}{RESET}")
    return "", []


def fetch_sub_status(item_type: str, item_id: int, id_field: str) -> tuple[set, set]:
    """Returns (available_code2s_with_path, missing_code2s) for one episode/movie."""
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
            item = data[0]
            available = {s["code2"] for s in item.get("subtitles", [])
                         if s.get("code2") and s.get("path")}
            missing   = {s["code2"] for s in item.get("missing_subtitles", [])
                         if s.get("code2")}
            dbg(f"fetch_sub_status({item_type}, {item_id}): available={available} missing={missing}")
            return available, missing
    except Exception as e:
        print(f"{RED}[ERROR] fetch_sub_status({item_type}, {item_id}): {e}{RESET}")
    return set(), set()


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
    dbg(f"submit_translate: PATCH {url}")
    try:
        r = requests.patch(url, headers=BAZARR_HEADERS, timeout=CONNECT_TIMEOUT)
        dbg(f"submit_translate: response {r.status_code}")
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
            count = int(r.json())
            dbg(f"lingarr_active_count: {count}")
            return count
    except Exception:
        pass
    dbg("lingarr_active_count: unavailable")
    return None

# ---------------------------------------------------------------------------
# Subtitle helpers
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = _re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")

_LANG3 = {
    'en': 'eng', 'et': 'est', 'sv': 'swe', 'de': 'ger', 'fr': 'fre',
    'es': 'spa', 'nl': 'dut', 'no': 'nob', 'fi': 'fin', 'da': 'dan',
    'pl': 'pol', 'pt': 'por', 'ru': 'rus',
}


def _sub_priority(path: str, lang_code2: str) -> int:
    """Lower = more preferred source. plain=0, hi/sdh=1, .2=2, .3=3, .4=4."""
    stem = os.path.basename(path).lower().removesuffix('.srt')
    for code in filter(None, [lang_code2, _LANG3.get(lang_code2, '')]):
        idx = stem.rfind(f'.{code}')
        if idx == -1:
            continue
        suffix = stem[idx + len(code) + 1:]
        if suffix == '':
            priority = 0
        elif suffix in ('hi', 'sdh'):
            priority = 1
        elif suffix.isdigit():
            priority = 1 + int(suffix)
        else:
            priority = 10
        dbg(f"_sub_priority({os.path.basename(path)!r}, {lang_code2!r}) -> {priority} (suffix={suffix!r})")
        return priority
    dbg(f"_sub_priority({os.path.basename(path)!r}, {lang_code2!r}) -> 99 (lang not found)")
    return 99


def _find_existing_target(video_path: str, target_lang: str) -> str | None:
    """Return the first existing target variant path, or None."""
    base = os.path.splitext(video_path)[0]
    for variant in ('', '.hi', '.2', '.3', '.4'):
        p = f"{base}.{target_lang}{variant}.srt"
        exists = os.path.exists(p)
        dbg(f"_find_existing_target: {p!r} -> {'EXISTS' if exists else 'missing'}")
        if exists:
            return p
    return None

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
    base = n * 1.8
    estimated = int(base * 1.3)
    hard_cap = max(POLL_TIMEOUT, CHECK_INTERVAL - 60)
    timeout = min(max(POLL_TIMEOUT, estimated), hard_cap)
    print(f"[INFO] Source has {n} dialogue lines — base ~{int(base)}s, "
          f"timeout set to {timeout}s (floor {POLL_TIMEOUT}s, cap {hard_cap}s)")
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
        dbg(f"_derive_target_path: marker {marker!r} not found in {basename!r} -> None")
        return None
    new_basename = basename[:idx] + f".{target_lang}." + basename[idx + len(marker):]
    result = os.path.join(os.path.dirname(source_path), new_basename)
    dbg(f"_derive_target_path: {basename!r} -> {new_basename!r}")
    return result

# ---------------------------------------------------------------------------
# Background recheck after moving on with file on disk
# ---------------------------------------------------------------------------

RECHECK_INTERVAL = 120   # seconds between rechecks
RECHECK_TIMEOUT  = 600   # total seconds to keep rechecking

def _background_recheck(item_type: str, item_id: int, id_field: str,
                         target_lang: str, label: str) -> None:
    """
    Spawned as a daemon thread when we move on with a file on disk but Bazarr
    hasn't confirmed yet. Polls every RECHECK_INTERVAL seconds for up to
    RECHECK_TIMEOUT seconds and logs when Bazarr finally imports the subtitle.
    """
    start = time.time()
    attempt = 0
    while not shutdown_requested:
        elapsed = time.time() - start
        if elapsed >= RECHECK_TIMEOUT:
            print(f"{YELLOW}[RECHECK] {label} '{target_lang}' still not confirmed by Bazarr "
                  f"after {RECHECK_TIMEOUT}s of rechecking — giving up{RESET}")
            return
        for _ in range(RECHECK_INTERVAL):
            if shutdown_requested:
                return
            time.sleep(1)
        attempt += 1
        available, missing = fetch_sub_status(item_type, item_id, id_field)
        dbg(f"[RECHECK] {label} '{target_lang}' attempt #{attempt}: available={available} missing={missing}")
        if target_lang in available or target_lang not in missing:
            elapsed = int(time.time() - start)
            print(f"{GREEN}[RECHECK] {label} '{target_lang}' confirmed by Bazarr "
                  f"{elapsed}s after moving on (attempt #{attempt}){RESET}")
            return


def _spawn_recheck(item_type: str, item_id: int, id_field: str,
                   target_lang: str, label: str) -> None:
    t = threading.Thread(
        target=_background_recheck,
        args=(item_type, item_id, id_field, target_lang, label),
        daemon=True,
    )
    t.start()

# ---------------------------------------------------------------------------
# Poll-and-verify
# ---------------------------------------------------------------------------

def wait_for_subtitle(item_type: str, item_id: int, id_field: str,
                      target_lang: str, deadline: float,
                      target_path: str | None = None, title: str = "") -> bool:
    """
    Polls for `target_lang` to appear in Bazarr.
    If the file lands on disk first, waits DISK_IMPORT_WAIT seconds then keeps
    cycling every DISK_POLL_INTERVAL seconds until Bazarr confirms the import.
    """
    label = title or f"[{item_type}:{item_id}]"
    start = time.time()
    disk_found_at: float | None = None

    while not shutdown_requested:
        # Check Bazarr: subtitle has a path, OR no longer in missing_subtitles
        available, missing = fetch_sub_status(item_type, item_id, id_field)
        dbg(f"{label} '{target_lang}' poll: available={available} missing={missing}")
        if target_lang in available or target_lang not in missing:
            elapsed = int(time.time() - start)
            print(f"{GREEN}[OK] {label} '{target_lang}' confirmed by Bazarr after {elapsed}s{RESET}")
            return True

        # Check disk
        if target_path and os.path.exists(target_path):
            if disk_found_at is None:
                disk_found_at = time.time()
                print(f"[DISK] {label} '{target_lang}' found on disk — "
                      f"waiting {DISK_IMPORT_WAIT}s for Bazarr to import...")
            elif time.time() - disk_found_at >= DISK_IMPORT_WAIT:
                elapsed = int(time.time() - start)
                print(f"{GREEN}[OK] {label} '{target_lang}' on disk after {DISK_IMPORT_WAIT}s — "
                      f"Bazarr will import, moving on ({elapsed}s){RESET}")
                _spawn_recheck(item_type, item_id, id_field, target_lang, label)
                return True

        now = time.time()
        if now >= deadline:
            if target_path and os.path.exists(target_path):
                elapsed = int(now - start)
                print(f"{GREEN}[OK] {label} '{target_lang}' on disk at hard cap "
                      f"({elapsed}s) — Bazarr will import eventually, moving on{RESET}")
                _spawn_recheck(item_type, item_id, id_field, target_lang, label)
                return True
            active = lingarr_active_count()
            print(f"{YELLOW}[TIMEOUT] {label} '{target_lang}' "
                  f"not found after {int(now - start)}s{RESET}")
            if active is not None:
                print(f"{YELLOW}[TIMEOUT] Lingarr still has {active} active job(s){RESET}")
            return False

        remaining = int(deadline - now)
        elapsed = int(now - start)
        disk_note = " (file on disk)" if disk_found_at else ""
        print(f"[POLL] {label} Waiting for '{target_lang}'{disk_note}... "
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
        return item.get("seriesTitle", item.get("series_title", "Unknown"))
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

    video_path, subs = fetch_subtitles(item_type, item_id, id_field)
    dbg(f"{title}: item_id={item_id} video_path={video_path!r}")
    available_map: dict[str, str] = {}
    for s in subs:
        code, path = s.get("code2"), s.get("path", "")
        if not code or not path:
            continue
        if code not in available_map:
            available_map[code] = path
            dbg(f"  available_map: picked {code!r} -> {os.path.basename(path)!r} (priority {_sub_priority(path, code)})")
        elif _sub_priority(path, code) < _sub_priority(available_map[code], code):
            dbg(f"  available_map: {code!r} replaced {os.path.basename(available_map[code])!r} with {os.path.basename(path)!r} (better priority)")
            available_map[code] = path
        else:
            dbg(f"  available_map: {code!r} kept {os.path.basename(available_map[code])!r} over {os.path.basename(path)!r}")

    target_langs = [l for l in LANGUAGES if l in missing and l not in available_map]
    source_langs = [l for l in LANGUAGES if l in available_map]

    if not source_langs:
        print(f"[SKIP] {title}: no source subtitle available from {LANGUAGES}")
        return
    if not target_langs:
        return

    source_lang = source_langs[0]
    source_path = available_map[source_lang]
    if item_type == "episodes":
        _se = _re.search(r'[Ss](\d{1,2})[Ee](\d{1,2})', os.path.basename(source_path))
        if _se:
            title = f"{title} S{int(_se.group(1)):02d}E{int(_se.group(2)):02d}"
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

        if video_path:
            target_path = os.path.splitext(video_path)[0] + f".{target_lang}.srt"
        else:
            target_path = _derive_target_path(source_path, source_lang, target_lang)
        dbg(f"{title} '{target_lang}': target_path={target_path!r}")

        existing = _find_existing_target(video_path, target_lang) if video_path else (
            target_path if (target_path and os.path.exists(target_path)) else None
        )
        if existing:
            print(f"[DISK] {title} '{target_lang}': {os.path.basename(existing)} already on disk, "
                  f"waiting for Bazarr to import")
            deadline = time.time() + item_timeout
            found = wait_for_subtitle(item_type, item_id, id_field, target_lang,
                                      deadline, existing, title=title)
            with stats_lock:
                if found:
                    stats["completed"] += 1
                    stats["translations"].append(f"{title}: {source_lang} -> {target_lang}")
                else:
                    stats["timed_out"] += 1
            continue

        _, recheck = fetch_subtitles(item_type, item_id, id_field)
        recheck_available = {s["code2"] for s in recheck if s.get("code2") and s.get("path")}
        if target_lang in recheck_available:
            print(f"[INFO] {title} '{target_lang}': already available in Bazarr, skipping submission")
            with stats_lock:
                stats["completed"] += 1
                stats["translations"].append(f"{title}: {source_lang} -> {target_lang}")
            continue

        if LINGARR_URL and not shutdown_requested:
            while not shutdown_requested:
                active = lingarr_active_count()
                if active is None or active < PARALLEL_TRANSLATES:
                    break
                print(f"[INFO] Lingarr queue full ({active}/{PARALLEL_TRANSLATES}) — waiting {POLL_INTERVAL}s before submitting {title}...")
                for _ in range(POLL_INTERVAL):
                    if shutdown_requested:
                        break
                    time.sleep(1)

        existing = _find_existing_target(video_path, target_lang) if video_path else (
            target_path if (target_path and os.path.exists(target_path)) else None
        )
        if existing:
            print(f"[DISK] {title} '{target_lang}': {os.path.basename(existing)} appeared during queue wait — "
                  f"skipping submission, Bazarr is importing")
            continue

        print(f"[TRANSLATE] {title}: {source_lang} -> {target_lang}")
        ok = submit_translate(item_type, item_id, source_path, target_lang)
        if ok:
            _record_submission(item_id, target_lang)
            with stats_lock:
                stats["submitted"] += 1
            deadline = time.time() + item_timeout
            found = wait_for_subtitle(item_type, item_id, id_field, target_lang,
                                      deadline, target_path, title=title)
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
    print(f"  Poll interval     : {POLL_INTERVAL}s  (floor {POLL_TIMEOUT}s, cap {max(POLL_TIMEOUT, CHECK_INTERVAL - 60)}s per translation)")
    print(f"  Resubmit cooldown : {RESUBMIT_COOLDOWN}s")
    print(f"  Disk import wait  : {DISK_IMPORT_WAIT}s then move on")
    print(f"  Debug mode        : {'ON' if DEBUG else 'off'}")
    sys.stdout.flush()

    print("[INFO] Waiting 30s for services to start...")
    sys.stdout.flush()
    for _ in range(30):
        if shutdown_requested:
            break
        time.sleep(1)

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

        if LINGARR_URL and not shutdown_requested:
            drain_deadline = time.time() + 2 * CHECK_INTERVAL
            while not shutdown_requested:
                active = lingarr_active_count()
                if active is None or active == 0:
                    break
                if time.time() >= drain_deadline:
                    print(f"{YELLOW}[WARNING] Lingarr still has {active} active job(s) after "
                          f"{2 * CHECK_INTERVAL}s — starting next cycle anyway{RESET}")
                    break
                print(f"[INFO] Lingarr has {active} active job(s) — waiting before next cycle...")
                for _ in range(POLL_INTERVAL):
                    if shutdown_requested:
                        break
                    time.sleep(1)

    print("[INFO] Bazarr AutoTranslate stopped cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
