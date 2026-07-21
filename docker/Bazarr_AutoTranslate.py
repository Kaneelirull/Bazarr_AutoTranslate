import os
import re as _re
import sys
import json
import signal
import time
import threading
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests

# Unbuffered output
sys.stdout = os.fdopen(sys.stdout.fileno(), "w", buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), "w", buffering=1)


class _DailyLogSink:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self._lock = threading.Lock()
        self._date = ""
        self._file = None
        self.current_path: Path | None = None

    def write(self, value: str) -> None:
        if not value:
            return
        with self._lock:
            current_date = time.strftime("%Y-%m-%d")
            if self._file is None or current_date != self._date:
                if self._file is not None:
                    self._file.close()
                self.log_dir.mkdir(parents=True, exist_ok=True)
                self.current_path = self.log_dir / f"bazarr-autotranslate-{current_date}.log"
                self._file = self.current_path.open("a", encoding="utf-8", buffering=1)
                self._date = current_date
            self._file.write(value)

    def flush(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()


class _TeeStream:
    def __init__(self, primary, sink: _DailyLogSink):
        self.primary = primary
        self.sink = sink

    def write(self, value: str) -> int:
        written = self.primary.write(value)
        self.sink.write(value)
        return written

    def flush(self) -> None:
        self.primary.flush()
        self.sink.flush()

    def fileno(self):
        return self.primary.fileno()

    def isatty(self) -> bool:
        return self.primary.isatty()

    @property
    def encoding(self):
        return self.primary.encoding

# ANSI colors (disabled outside TTY)
_tty = sys.stdout.isatty()
GREEN = "\033[92m" if _tty else ""
YELLOW = "\033[93m" if _tty else ""
RED = "\033[91m" if _tty else ""
CYAN = "\033[96m" if _tty else ""
BOLD = "\033[1m" if _tty else ""
RESET = "\033[0m" if _tty else ""

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
LANGUAGES = [l.strip() for l in _raw_languages.split(",") if l.strip()]
BAZARR_URL = _normalize_url(_require("BAZARR_URL"))
BAZARR_API_KEY = _require("BAZARR_API_KEY")
LINGARR_URL = _normalize_url(_require("LINGARR_URL"))
LINGARR_API_KEY = os.getenv("LINGARR_API_KEY", "").strip()
PARALLEL_TRANSLATES = max(1, int(os.getenv("PARALLEL_TRANSLATES", "1")))
CHECK_INTERVAL = max(10, int(os.getenv("CHECK_INTERVAL", "1200")))
CONNECT_TIMEOUT = max(5, int(os.getenv("CONNECT_TIMEOUT", "10")))
POLL_INTERVAL = max(5, int(os.getenv("POLL_INTERVAL", "20")))
POLL_TIMEOUT = max(30, int(os.getenv("POLL_TIMEOUT", "900")))
RESUBMIT_COOLDOWN = max(60, int(os.getenv("RESUBMIT_COOLDOWN", "3600")))
SYNC_TIMEOUT = max(30, int(os.getenv("SYNC_TIMEOUT", "600")))
SYNC_POLL_INTERVAL = max(5, int(os.getenv("SYNC_POLL_INTERVAL", "15")))
SYNC_START_TIMEOUT = max(5, int(os.getenv("SYNC_START_TIMEOUT", "30")))
CLEANUP_MIN_CONFIDENCE = float(os.getenv("CLEANUP_MIN_CONFIDENCE", "0.70"))
CLEANUP_MIN_CHARS = int(os.getenv("CLEANUP_MIN_CHARS", "200"))
CLEANUP_MAX_UNIQUE_RATIO = float(os.getenv("CLEANUP_MAX_UNIQUE_RATIO", "0.15"))
CLEANUP_MAX_CYRILLIC_RATIO = float(os.getenv("CLEANUP_MAX_CYRILLIC_RATIO", "0.05"))
CLEANUP_MAX_CJK_RATIO = float(os.getenv("CLEANUP_MAX_CJK_RATIO", "0.05"))
CLEANUP_MAX_LATIN_RATIO = float(os.getenv("CLEANUP_MAX_LATIN_RATIO", "0.80"))
CLEANUP_MIN_LETTERS_FOR_SCRIPT = int(os.getenv("CLEANUP_MIN_LETTERS_FOR_SCRIPT", "20"))
CLEANUP_MAX_CUE_LINES = max(1, int(os.getenv("CLEANUP_MAX_CUE_LINES", "4")))
CLEANUP_MAX_CUE_CHARS = max(50, int(os.getenv("CLEANUP_MAX_CUE_CHARS", "500")))
CLEANUP_MAX_EXPANSION_RATIO = max(1.0, float(os.getenv("CLEANUP_MAX_EXPANSION_RATIO", "4.0")))
CLEANUP_MAX_EXPANSION_CHARS = max(50, int(os.getenv("CLEANUP_MAX_EXPANSION_CHARS", "300")))
CLEANUP_MAX_SOURCE_SIMILARITY = min(1.0, max(0.5, float(os.getenv("CLEANUP_MAX_SOURCE_SIMILARITY", "0.92"))))
CLEANUP_REPAIR_ENABLED = os.getenv("CLEANUP_REPAIR_ENABLED", "true").lower() in ("1", "true", "yes")
CLEANUP_MAX_REPAIR_ATTEMPTS = max(1, int(os.getenv("CLEANUP_MAX_REPAIR_ATTEMPTS", "2")))
CLEANUP_REPAIR_CONTEXT_LINES = max(0, int(os.getenv("CLEANUP_REPAIR_CONTEXT_LINES", "5")))
CLEANUP_FORMAT_REPAIR_ENABLED = os.getenv("CLEANUP_FORMAT_REPAIR_ENABLED", "true").lower() in ("1", "true", "yes")
CLEANUP_REPAIR_WORKERS = max(1, int(os.getenv("CLEANUP_REPAIR_WORKERS", "1")))
CLEANUP_REPAIR_QUEUE_MAX = max(1, int(os.getenv("CLEANUP_REPAIR_QUEUE_MAX", "100")))
CLEANUP_SCAN_EXISTING = os.getenv("CLEANUP_SCAN_EXISTING", "true").lower() in ("1", "true", "yes")
CLEANUP_SCAN_INTERVAL = max(300, int(os.getenv("CLEANUP_SCAN_INTERVAL", "21600")))
CLEANUP_SCAN_DRY_RUN = os.getenv("CLEANUP_SCAN_DRY_RUN", "false").lower() in ("1", "true", "yes")
CLEANUP_ROOT_RAW = os.getenv("CLEANUP_ROOT", "/media").strip() or "/media"
CLEANUP_ROOTS = [Path(value.strip()) for value in CLEANUP_ROOT_RAW.split(os.pathsep) if value.strip()]
CLEANUP_ACTION = os.getenv("CLEANUP_ACTION", "quarantine").strip().lower()
_raw_cleanup_langs = os.getenv("CLEANUP_LANGUAGES", "et")
CLEANUP_LANGUAGES = {l.strip() for l in _raw_cleanup_langs.split(",") if l.strip()}
STATE_DIR = os.getenv("STATE_DIR", "/config").strip() or "/config"
SUBMIT_CACHE_FILE = os.path.join(STATE_DIR, "submitted_cache.json")
CLEANUP_QUARANTINE_DIR = Path(os.getenv("CLEANUP_QUARANTINE_DIR", f"{STATE_DIR}/quarantine"))
VALIDATION_STATE_FILE = Path(STATE_DIR) / "validation_state.json"
LOG_DIR = Path(os.getenv("LOG_DIR", "/var/log/bazarr-autotranslate"))
RETENTION_DAYS = max(1, int(os.getenv("RETENTION_DAYS", "30")))
RETENTION_CHECK_INTERVAL = max(300, int(os.getenv("RETENTION_CHECK_INTERVAL", "3600")))
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

if not LANGUAGES:
    print(f"{RED}[ERROR] LANGUAGES must contain at least one language code{RESET}")
    sys.exit(1)
if CLEANUP_ACTION not in ("quarantine", "delete", "report"):
    print(f"{RED}[ERROR] CLEANUP_ACTION must be quarantine, delete, or report{RESET}")
    sys.exit(1)

_app_log_sink = _DailyLogSink(LOG_DIR)
sys.stdout = _TeeStream(sys.stdout, _app_log_sink)
sys.stderr = _TeeStream(sys.stderr, _app_log_sink)

BAZARR_HEADERS: dict = {"Accept": "application/json", "X-API-KEY": BAZARR_API_KEY}
LINGARR_HEADERS: dict = {"Accept": "application/json", "Content-Type": "application/json"}
if LINGARR_API_KEY:
    LINGARR_HEADERS["X-Api-Key"] = LINGARR_API_KEY

_cleanup_detector = None
_cleanup_detector_lock = threading.Lock()
_validation_state = None
_validation_state_lock = threading.Lock()
_cleanup_scan_lock = threading.Lock()
_repair_executor = None
_repair_executor_lock = threading.Lock()
_repair_capacity = threading.BoundedSemaphore(CLEANUP_REPAIR_WORKERS + CLEANUP_REPAIR_QUEUE_MAX)
_pending_repairs: dict[Future, dict] = {}
_pending_repairs_lock = threading.Lock()
_repair_keys: set[tuple] = set()
_target_repair_locks: dict[str, threading.Lock] = {}
_target_repair_locks_lock = threading.Lock()

_episode_cache: dict[int, int] = {}
_movie_cache: dict[int, int] = {}
_media_cache_lock = threading.Lock()


@dataclass
class RepairJobResult:
    action: str
    report: object
    title: str
    target_lang: str
    item_type: str | None
    item_id: int | None
    attempts: int = 0
    second_attempts: int = 0
    target_path: str = ""


def dbg(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")


def _get_cleanup_detector():
    global _cleanup_detector
    if not CLEANUP_LANGUAGES:
        return None
    with _cleanup_detector_lock:
        if _cleanup_detector is None:
            print("[INFO] Loading language detector for per-file cleanup...")
            from clean_et_subs import build_detector
            _cleanup_detector = build_detector()
        return _cleanup_detector


def _get_validation_state():
    global _validation_state
    with _validation_state_lock:
        if _validation_state is None:
            from clean_et_subs import ValidationStateStore
            _validation_state = ValidationStateStore(VALIDATION_STATE_FILE)
        return _validation_state


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
# ---------------------------------------------------------------------------

_submitted_cache: dict[tuple, float] = {}
_submitted_paths: dict[tuple, str] = {}
_cache_lock = threading.Lock()


def _cache_key_str(item_id: int, target_lang: str) -> str:
    return f"{item_id}:{target_lang}"


def _load_submit_cache() -> None:
    try:
        with open(SUBMIT_CACHE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        dbg(f"_load_submit_cache: no cache file at {SUBMIT_CACHE_FILE}")
        return
    except (OSError, ValueError) as e:
        print(f"{YELLOW}[WARNING] Could not read submit cache ({e}) — starting fresh{RESET}")
        return

    now = time.time()
    loaded = 0
    pruned = 0
    with _cache_lock:
        for key, submitted_at in raw.items():
            try:
                item_id_s, lang = key.rsplit(":", 1)
                if isinstance(submitted_at, dict):
                    ts = float(submitted_at.get("submittedAt"))
                    target_path = submitted_at.get("targetPath")
                else:
                    ts = float(submitted_at)
                    target_path = None
            except (TypeError, ValueError, AttributeError):
                continue
            if now - ts >= RESUBMIT_COOLDOWN:
                pruned += 1
                continue
            cache_key = (int(item_id_s), lang)
            _submitted_cache[cache_key] = ts
            if isinstance(target_path, str) and target_path:
                _submitted_paths[cache_key] = os.path.normcase(os.path.abspath(target_path))
            loaded += 1
    print(f"[INFO] Loaded {loaded} active cooldown entr{'y' if loaded == 1 else 'ies'} "
          f"from cache ({pruned} expired pruned)")


def _save_submit_cache() -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with _cache_lock:
            serializable = {
                _cache_key_str(item_id, lang): {
                    "submittedAt": ts,
                    "targetPath": _submitted_paths.get((item_id, lang)),
                }
                for (item_id, lang), ts in _submitted_cache.items()
            }
        tmp = f"{SUBMIT_CACHE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(serializable, f)
        os.replace(tmp, SUBMIT_CACHE_FILE)
        dbg(f"_save_submit_cache: wrote {len(serializable)} entries")
    except OSError as e:
        print(f"{YELLOW}[WARNING] Could not persist submit cache: {e}{RESET}")


def _check_cooldown(item_id: int, target_lang: str) -> int | None:
    key = (item_id, target_lang)
    with _cache_lock:
        submitted_at = _submitted_cache.get(key)
    if submitted_at is None:
        return None
    age = int(time.time() - submitted_at)
    return age if age < RESUBMIT_COOLDOWN else None


def _record_submission(item_id: int, target_lang: str, target_path: str | None = None) -> None:
    with _cache_lock:
        key = (item_id, target_lang)
        _submitted_cache[key] = time.time()
        if target_path:
            _submitted_paths[key] = os.path.normcase(os.path.abspath(target_path))
    _save_submit_cache()


def _clear_submission(item_id: int, target_lang: str) -> None:
    """Remove cooldown entry so a cleaned (deleted) file can be re-translated next cycle."""
    with _cache_lock:
        key = (item_id, target_lang)
        removed = _submitted_cache.pop(key, None)
        _submitted_paths.pop(key, None)
    if removed is not None:
        _save_submit_cache()
        dbg(f"_clear_submission({item_id}, {target_lang!r}): cleared")


def _clear_submission_for_path(target_path: str | Path, target_lang: str) -> int:
    normalized = os.path.normcase(os.path.abspath(str(target_path)))
    with _cache_lock:
        keys = [
            key
            for key, path in _submitted_paths.items()
            if key[1] == target_lang and path == normalized
        ]
        for key in keys:
            _submitted_cache.pop(key, None)
            _submitted_paths.pop(key, None)
    if keys:
        _save_submit_cache()
        dbg(f"Cleared {len(keys)} cooldown entr{'y' if len(keys) == 1 else 'ies'} for {target_path}")
    return len(keys)

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def bazarr_url(endpoint: str) -> str:
    return f"{BAZARR_URL}/api/{endpoint}"


def lingarr_url(endpoint: str) -> str:
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
            return vp, subs
    except Exception as e:
        print(f"{RED}[ERROR] fetch_subtitles({item_type}, {item_id}): {e}{RESET}")
    return "", []


def trigger_bazarr_sync(had_episodes: bool, had_movies: bool) -> None:
    tasks = []
    if had_episodes:
        tasks.append("series_full_scan_subtitles")
    if had_movies:
        tasks.append("movies_full_scan_subtitles")
    for taskid in tasks:
        try:
            r = requests.post(
                bazarr_url("system/tasks"),
                headers=BAZARR_HEADERS,
                params={"taskid": taskid},
                timeout=CONNECT_TIMEOUT,
            )
            if r.status_code == 204:
                print(f"[INFO] Triggered Bazarr task: {taskid}")
            else:
                print(f"{YELLOW}[WARNING] Bazarr task {taskid} returned {r.status_code}{RESET}")
        except Exception as e:
            print(f"{RED}[ERROR] Failed to trigger Bazarr task {taskid}: {e}{RESET}")


def _job_matches_scan(job: dict, had_episodes: bool, had_movies: bool) -> bool:
    name = (job.get("job_name") or "").lower()
    status = (job.get("status") or "").lower()
    if status != "running":
        return False
    if had_episodes and "episode" in name and "subtitle" in name:
        return True
    if had_movies and "movie" in name and "subtitle" in name:
        return True
    if had_episodes and "series" in name and "subtitle" in name:
        return True
    return False


def wait_for_bazarr_sync(had_episodes: bool, had_movies: bool, timeout: int) -> bool:
    if not had_episodes and not had_movies:
        return True

    print(f"[INFO] Waiting for Bazarr subtitle scan to complete (timeout {timeout}s)...")
    deadline = time.time() + timeout
    start_deadline = min(deadline, time.time() + SYNC_START_TIMEOUT)
    logged_jobs: set[int] = set()
    observed_running = False

    while not shutdown_requested:
        try:
            r = requests.get(bazarr_url("system/jobs"), headers=BAZARR_HEADERS, timeout=CONNECT_TIMEOUT)
            r.raise_for_status()
            jobs = r.json().get("data", [])
        except Exception as e:
            print(f"{YELLOW}[WARNING] Could not poll Bazarr jobs: {e}{RESET}")
            jobs = []

        active = [j for j in jobs if _job_matches_scan(j, had_episodes, had_movies)]
        if not active:
            if observed_running:
                print(f"{GREEN}[OK] Bazarr subtitle scan completed{RESET}")
                return True
            if time.time() >= start_deadline:
                print(
                    f"{YELLOW}[WARNING] Bazarr subtitle scan did not appear within "
                    f"{SYNC_START_TIMEOUT}s{RESET}"
                )
                return False
        else:
            observed_running = True

        for job in active:
            jid = job.get("job_id")
            if jid not in logged_jobs:
                logged_jobs.add(jid)
                print(f"[INFO] Bazarr scan running: {job.get('job_name', 'unknown')}")
            if job.get("is_progress"):
                pv = job.get("progress_value", 0)
                pm = job.get("progress_max", 0)
                msg = job.get("progress_message", "")
                print(f"[SYNC] {job.get('job_name')}: {pv}/{pm} — {msg}")

        if time.time() >= deadline:
            print(f"{YELLOW}[WARNING] Bazarr sync timed out after {timeout}s — continuing anyway{RESET}")
            return False

        for _ in range(SYNC_POLL_INTERVAL):
            if shutdown_requested:
                return False
            time.sleep(1)

    return False

# ---------------------------------------------------------------------------
# Lingarr API
# ---------------------------------------------------------------------------

def lingarr_get_languages() -> list[str]:
    try:
        r = requests.get(lingarr_url("Translate/languages"), headers=LINGARR_HEADERS, timeout=CONNECT_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        if isinstance(payload, list):
            return [str(x) for x in payload]
    except Exception as e:
        print(f"{YELLOW}[WARNING] Could not fetch Lingarr languages: {e}{RESET}")
    return []


def lingarr_build_media_cache() -> None:
    global _episode_cache, _movie_cache
    episode_cache: dict[int, int] = {}
    movie_cache: dict[int, int] = {}

    page = 1
    while not shutdown_requested:
        try:
            r = requests.get(
                lingarr_url("Media/movies"),
                headers=LINGARR_HEADERS,
                params={"pageNumber": page, "pageSize": 100},
                timeout=CONNECT_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"{RED}[ERROR] lingarr_build_media_cache movies page {page}: {e}{RESET}")
            break

        for movie in data.get("items", []):
            rid = movie.get("radarrId")
            mid = movie.get("id")
            if rid is not None and mid is not None:
                movie_cache[int(rid)] = int(mid)

        total = data.get("totalCount", 0)
        page_size = data.get("pageSize", 100) or 100
        if page * page_size >= total or not data.get("items"):
            break
        page += 1

    page = 1
    while not shutdown_requested:
        try:
            r = requests.get(
                lingarr_url("Media/shows"),
                headers=LINGARR_HEADERS,
                params={"pageNumber": page, "pageSize": 50},
                timeout=CONNECT_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"{RED}[ERROR] lingarr_build_media_cache shows page {page}: {e}{RESET}")
            break

        for show in data.get("items", []):
            for season in show.get("seasons", []) or []:
                for ep in season.get("episodes", []) or []:
                    sid = ep.get("sonarrId")
                    eid = ep.get("id")
                    if sid is not None and eid is not None:
                        episode_cache[int(sid)] = int(eid)

        total = data.get("totalCount", 0)
        page_size = data.get("pageSize", 50) or 50
        if page * page_size >= total or not data.get("items"):
            break
        page += 1

    with _media_cache_lock:
        _episode_cache = episode_cache
        _movie_cache = movie_cache

    print(f"[INFO] Lingarr media cache: {len(movie_cache)} movie(s), {len(episode_cache)} episode(s)")


def lingarr_resolve_media_id(item_type: str, item_id: int) -> int | None:
    with _media_cache_lock:
        if item_type == "episodes":
            return _episode_cache.get(item_id)
        return _movie_cache.get(item_id)


def lingarr_active_count() -> int | None:
    try:
        r = requests.get(lingarr_url("TranslationRequest/active"), headers=LINGARR_HEADERS, timeout=CONNECT_TIMEOUT)
        if r.status_code == 200:
            payload = r.json()
            if isinstance(payload, list):
                return len(payload)
            if isinstance(payload, int):
                return payload
            if isinstance(payload, dict) and "count" in payload:
                return int(payload["count"])
    except Exception as e:
        dbg(f"lingarr_active_count: error {e}")
    return None


def lingarr_submit_file(
    media_id: int,
    subtitle_path: str,
    source_lang: str,
    target_lang: str,
    media_type: str,
) -> int | None:
    body = {
        "mediaId": media_id,
        "subtitlePath": subtitle_path,
        "sourceLanguage": source_lang,
        "targetLanguage": target_lang,
        "mediaType": media_type,
        "subtitleFormat": "srt",
    }
    dbg(f"lingarr_submit_file: POST {body}")
    try:
        r = requests.post(
            lingarr_url("Translate/file"),
            headers=LINGARR_HEADERS,
            json=body,
            timeout=CONNECT_TIMEOUT,
        )
        r.raise_for_status()
        job_id = r.json().get("jobId")
        if job_id is not None:
            return int(job_id)
        print(f"{RED}[ERROR] lingarr_submit_file: no jobId in response{RESET}")
    except Exception as e:
        print(f"{RED}[ERROR] lingarr_submit_file: {e}{RESET}")
    return None


def lingarr_translate_line(
    subtitle_line: str,
    source_lang: str,
    target_lang: str,
    context_before: list[str],
    context_after: list[str],
    *,
    repair_label: str = "",
    cue_number: int | None = None,
    attempt: int | None = None,
    outcome_meta: dict | None = None,
) -> str | None:
    body = {
        "subtitleLine": subtitle_line,
        "sourceLanguage": source_lang,
        "targetLanguage": target_lang,
        "contextLinesBefore": context_before,
        "contextLinesAfter": context_after,
    }
    dbg(
        f"lingarr_translate_line: POST source={source_lang} target={target_lang} "
        f"before={len(context_before)} after={len(context_after)} chars={len(subtitle_line)}"
    )
    started = time.monotonic()
    try:
        r = requests.post(
            lingarr_url("Translate/line"),
            headers=LINGARR_HEADERS,
            json=body,
            timeout=max(CONNECT_TIMEOUT, 120),
        )
        elapsed = time.monotonic() - started
        if outcome_meta is not None:
            outcome_meta.update({"httpStatus": r.status_code, "httpDurationSeconds": round(elapsed, 3)})
        identity = f"{repair_label} cue {cue_number}".strip() if cue_number is not None else "line repair"
        attempt_label = f" attempt {attempt}" if attempt is not None else ""
        print(f"[REPAIR] Lingarr HTTP {r.status_code} for {identity}{attempt_label} after {elapsed:.1f}s")
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError:
            payload = r.text

        if isinstance(payload, str) and payload.strip():
            return payload.strip()
        if isinstance(payload, dict):
            for key in ("translatedSubtitle", "translatedLine", "translation", "text"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        print(f"{RED}[ERROR] lingarr_translate_line: unexpected response shape{RESET}")
    except Exception as e:
        elapsed = time.monotonic() - started
        if outcome_meta is not None:
            outcome_meta.update({"httpStatus": getattr(getattr(e, "response", None), "status_code", None), "httpDurationSeconds": round(elapsed, 3)})
        print(f"{RED}[ERROR] lingarr_translate_line failed after {elapsed:.1f}s: {e}{RESET}")
    return None


def lingarr_get_job(job_id: int) -> dict | None:
    try:
        r = requests.get(
            lingarr_url(f"TranslationRequest/{job_id}"),
            headers=LINGARR_HEADERS,
            timeout=CONNECT_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        dbg(f"lingarr_get_job({job_id}): {e}")
    return None


def lingarr_poll_job(job_id: int, deadline: float, label: str) -> str | None:
    last_progress = -1
    while not shutdown_requested:
        job = lingarr_get_job(job_id)
        if job:
            status = job.get("status", "")
            progress = job.get("progress", 0)
            if progress != last_progress:
                last_progress = progress
                dbg(f"{label} job {job_id}: status={status} progress={progress}")
            if status == "Completed":
                return "Completed"
            if status in ("Failed", "Cancelled", "Interrupted"):
                err = job.get("errorMessage", "")
                print(f"{RED}[FAIL] {label} Lingarr job {job_id}: {status}" +
                      (f" — {err}" if err else "") + RESET)
                return status

        if time.time() >= deadline:
            print(f"{YELLOW}[TIMEOUT] {label} Lingarr job {job_id} not completed in time{RESET}")
            return None

        for _ in range(POLL_INTERVAL):
            if shutdown_requested:
                return None
            time.sleep(1)

    return None

# ---------------------------------------------------------------------------
# Subtitle helpers
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = _re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")

_LANG3 = {
    "en": "eng", "et": "est", "sv": "swe", "de": "ger", "fr": "fre",
    "es": "spa", "nl": "dut", "no": "nob", "fi": "fin", "da": "dan",
    "pl": "pol", "pt": "por", "ru": "rus",
}


def _sub_priority(path: str, lang_code2: str) -> int:
    stem = os.path.basename(path).lower().removesuffix(".srt")
    for code in filter(None, [lang_code2, _LANG3.get(lang_code2, "")]):
        idx = stem.rfind(f".{code}")
        if idx == -1:
            continue
        suffix = stem[idx + len(code) + 1:]
        if suffix == "":
            return 0
        if suffix in ("hi", "sdh"):
            return 1
        if suffix.isdigit():
            return 1 + int(suffix)
        return 10
    return 99


def _find_existing_target(video_path: str, target_lang: str) -> str | None:
    base = os.path.splitext(video_path)[0]
    for variant in ("", ".hi", ".2", ".3", ".4"):
        p = f"{base}.{target_lang}{variant}.srt"
        if os.path.exists(p):
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
    basename = os.path.basename(source_path)
    marker = f".{source_lang}."
    idx = basename.rfind(marker)
    if idx == -1:
        return None
    new_basename = basename[:idx] + f".{target_lang}." + basename[idx + len(marker):]
    return os.path.join(os.path.dirname(source_path), new_basename)


def _validation_kwargs() -> dict:
    return {
        "min_chars": CLEANUP_MIN_CHARS,
        "min_confidence": CLEANUP_MIN_CONFIDENCE,
        "max_unique_ratio": CLEANUP_MAX_UNIQUE_RATIO,
        "max_cyrillic_ratio": CLEANUP_MAX_CYRILLIC_RATIO,
        "max_cjk_ratio": CLEANUP_MAX_CJK_RATIO,
        "max_latin_ratio": CLEANUP_MAX_LATIN_RATIO,
        "min_letters_for_script": CLEANUP_MIN_LETTERS_FOR_SCRIPT,
        "max_cue_lines": CLEANUP_MAX_CUE_LINES,
        "max_cue_chars": CLEANUP_MAX_CUE_CHARS,
        "max_expansion_ratio": CLEANUP_MAX_EXPANSION_RATIO,
        "max_expansion_chars": CLEANUP_MAX_EXPANSION_CHARS,
        "max_source_similarity": CLEANUP_MAX_SOURCE_SIMILARITY,
    }


def _file_hash_or_none(path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        from clean_et_subs import file_sha256
        return file_sha256(path)
    except OSError as e:
        dbg(f"Could not hash {path}: {e}")
        return None


def _record_validation_result(
    target_path: str | Path,
    source_hash: str | None,
    target_hash: str | None,
    result: str,
    report,
    **extra,
) -> None:
    try:
        details = {"validation": report.to_dict(), **extra}
        _get_validation_state().record(
            target_path,
            source_hash=source_hash,
            target_hash=target_hash,
            result=result,
            details=details,
        )
    except OSError as e:
        print(f"{YELLOW}[WARNING] Could not persist validation state: {e}{RESET}")


def _apply_cleanup_action(
    target_path: str | Path,
    source_path: str | Path | None,
    target_lang: str,
    report,
    *,
    repair_attempts: int = 0,
    lingarr_outcome: str = "not attempted",
    attempt_history: list[dict] | None = None,
    format_fixes: list[str] | None = None,
    format_recovered_cues: list[int] | None = None,
    dry_run: bool = False,
) -> str:
    from clean_et_subs import quarantine_subtitle, write_validation_report

    target = Path(target_path)
    source_hash = _file_hash_or_none(source_path)
    target_hash = _file_hash_or_none(target)
    audit = {
        "sourcePath": str(source_path) if source_path is not None else None,
        "targetPath": str(target),
        "sourceHash": source_hash,
        "targetHash": target_hash,
        "targetLanguage": target_lang,
        "repairAttempts": repair_attempts,
        "repairAttemptHistory": attempt_history or [],
        "formatFixes": format_fixes or [],
        "formatRecoveredCues": format_recovered_cues or [],
        "lingarrOutcome": lingarr_outcome,
        "validation": report.to_dict(),
        "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if dry_run or CLEANUP_ACTION == "report":
        print(f"[CLEANUP] {'DRYRUN' if dry_run else 'REPORT'}: would remove {target}")
        _record_validation_result(
            target,
            source_hash,
            target_hash,
            "dry-run-invalid" if dry_run else "reported-invalid",
            report,
            repairAttempts=repair_attempts,
            repairAttemptHistory=attempt_history or [],
            formatFixes=format_fixes or [],
            formatRecoveredCues=format_recovered_cues or [],
            lingarrOutcome=lingarr_outcome,
        )
        return "dry-run" if dry_run else "reported"

    if CLEANUP_ACTION == "quarantine":
        try:
            destination = quarantine_subtitle(target, CLEANUP_ROOTS, CLEANUP_QUARANTINE_DIR)
            try:
                write_validation_report(destination, audit)
            except OSError as e:
                print(f"{YELLOW}[WARNING] Quarantined file but could not write report: {e}{RESET}")
            _record_validation_result(
                target,
                source_hash,
                target_hash,
                "quarantined",
                report,
                quarantinePath=str(destination),
                repairAttempts=repair_attempts,
                repairAttemptHistory=attempt_history or [],
                formatFixes=format_fixes or [],
                formatRecoveredCues=format_recovered_cues or [],
                lingarrOutcome=lingarr_outcome,
            )
            print(f"[CLEANUP] Quarantined {target} -> {destination}")
            return "quarantined"
        except OSError as e:
            print(f"{RED}[ERROR] Could not quarantine {target}: {e}{RESET}")
            return "action-failed"

    try:
        target.unlink()
        _record_validation_result(
            target,
            source_hash,
            target_hash,
            "deleted",
            report,
            repairAttempts=repair_attempts,
            repairAttemptHistory=attempt_history or [],
            formatFixes=format_fixes or [],
            formatRecoveredCues=format_recovered_cues or [],
            lingarrOutcome=lingarr_outcome,
        )
        print(f"[CLEANUP] Deleted {target}")
        return "deleted"
    except OSError as e:
        print(f"{RED}[ERROR] Could not delete {target}: {e}{RESET}")
        return "action-failed"


def _target_repair_lock(target_path: str | Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(str(target_path)))
    with _target_repair_locks_lock:
        return _target_repair_locks.setdefault(key, threading.Lock())


def _write_recovery_candidate(
    target_path: str | Path,
    raw: str,
    *,
    same_directory: bool = True,
) -> Path:
    target = Path(target_path)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix=f".{target.name}.recovery.",
        suffix=".srt",
        dir=target.parent if same_directory else None,
        delete=False,
    ) as handle:
        handle.write(raw)
        return Path(handle.name)


def _perform_repair(
    source_path: str,
    target_path: str,
    source_lang: str,
    target_lang: str,
    item_id: int | None,
    title: str,
    item_type: str | None,
    initial_report,
    expected_target_hash: str | None,
    recovery_raw: str | None = None,
    format_fixes: list[str] | None = None,
    format_recovered_cues: list[int] | None = None,
) -> RepairJobResult:
    from clean_et_subs import repair_subtitle_file, target_language_for_code

    label = title or os.path.basename(target_path)
    detector = _get_cleanup_detector()
    target_language = target_language_for_code(target_lang)
    if detector is None or target_language is None:
        return RepairJobResult(
            "repair-deferred", initial_report, label, target_lang, item_type, item_id,
            target_path=str(target_path),
        )

    with _target_repair_lock(target_path):
        if expected_target_hash is not None and _file_hash_or_none(target_path) != expected_target_hash:
            print(f"[REPAIR] Deferred {label} '{target_lang}': target changed while queued")
            return RepairJobResult(
                "repair-deferred", initial_report, label, target_lang, item_type, item_id,
                target_path=str(target_path),
            )

        working_path = Path(target_path)
        recovery_temp: Path | None = None
        if recovery_raw is not None:
            recovery_temp = _write_recovery_candidate(target_path, recovery_raw)
            working_path = recovery_temp

        attempt_state: dict = {}

        def attempt_logger(event: dict) -> None:
            attempt_state.clear()
            attempt_state.update(event)
            cue = event.get("cueNumber")
            attempt = event.get("attempt")
            maximum = event.get("maxAttempts")
            duration = event.get("durationSeconds", 0)
            http_status = event.get("httpStatus")
            http_label = f" HTTP {http_status}" if http_status is not None else ""
            worker = threading.current_thread().name
            if event["event"] == "sending":
                context = (
                    "without context"
                    if event.get("withoutContext")
                    else f"with context before={event.get('contextBefore', 0)} after={event.get('contextAfter', 0)}"
                )
                print(f"[REPAIR] {worker} sending {label} '{target_lang}' cue {cue} attempt {attempt}/{maximum} {context}")
            elif event["event"] == "accepted":
                print(f"[REPAIR] Cue {cue} attempt {attempt} accepted{http_label} after {duration:.1f}s")
            elif event["event"] == "rejected":
                rules = ",".join(event.get("validationRules", [])) or "validation"
                print(f"[REPAIR] Cue {cue} attempt {attempt} rejected{http_label} after {duration:.1f}s: {rules}")
            else:
                print(f"[REPAIR] Cue {cue} attempt {attempt} failed{http_label} after {duration:.1f}s: {event.get('outcome')}")

        def translator(line: str, before: list[str], after: list[str]):
            outcome_meta: dict = {}
            translated = lingarr_translate_line(
                line,
                source_lang,
                target_lang,
                before,
                after,
                repair_label=label,
                cue_number=attempt_state.get("cueNumber"),
                attempt=attempt_state.get("attempt"),
                outcome_meta=outcome_meta,
            )
            return translated, outcome_meta

        cue_list = ", ".join(str(i + 1) for i in initial_report.repairable_cue_indexes)
        print(f"[REPAIR] Retrying {label} '{target_lang}' cue position(s): {cue_list}")
        try:
            repair = repair_subtitle_file(
                Path(source_path),
                working_path,
                detector,
                target_language,
                translator,
                target_lang=target_lang,
                max_attempts=CLEANUP_MAX_REPAIR_ATTEMPTS,
                context_lines=CLEANUP_REPAIR_CONTEXT_LINES,
                attempt_logger=attempt_logger,
                **_validation_kwargs(),
            )
            second_attempts = sum(
                entry.get("attempt", 0) > 1 and entry.get("withoutContext")
                for entry in repair.attempt_history
            )
            if repair.success:
                if recovery_temp is not None:
                    os.replace(recovery_temp, target_path)
                    recovery_temp = None
                repaired = ", ".join(str(number) for number in repair.repaired_cues)
                print(f"{GREEN}[REPAIR] Repaired and validated {label} '{target_lang}' cue(s): {repaired}{RESET}")
                _record_validation_result(
                    target_path,
                    _file_hash_or_none(source_path),
                    _file_hash_or_none(target_path),
                    "valid",
                    repair.report,
                    repairedCues=repair.repaired_cues,
                    repairAttempts=repair.attempts,
                    repairAttemptHistory=repair.attempt_history,
                    formatFixes=format_fixes or [],
                    formatRecoveredCues=format_recovered_cues or [],
                    lingarrOutcome="repaired",
                )
                return RepairJobResult(
                    "repaired", repair.report, label, target_lang, item_type, item_id,
                    repair.attempts, second_attempts, str(target_path),
                )

            print(f"{YELLOW}[REPAIR] Could not repair {label} '{target_lang}': {repair.reason}{RESET}")
            action = _apply_cleanup_action(
                target_path,
                source_path,
                target_lang,
                repair.report,
                repair_attempts=repair.attempts,
                lingarr_outcome=repair.reason,
                attempt_history=repair.attempt_history,
                format_fixes=format_fixes,
                format_recovered_cues=format_recovered_cues,
            )
            if action in ("quarantined", "deleted") and item_id is not None:
                _clear_submission(item_id, target_lang)
                print(f"[CLEANUP] Cleared cooldown for retry: {label} '{target_lang}'")
            return RepairJobResult(
                action, repair.report, label, target_lang, item_type, item_id,
                repair.attempts, second_attempts, str(target_path),
            )
        finally:
            if recovery_temp is not None:
                try:
                    recovery_temp.unlink()
                except OSError:
                    pass


def _get_repair_executor() -> ThreadPoolExecutor:
    global _repair_executor
    with _repair_executor_lock:
        if _repair_executor is None:
            _repair_executor = ThreadPoolExecutor(
                max_workers=CLEANUP_REPAIR_WORKERS,
                thread_name_prefix="repair-worker",
            )
        return _repair_executor


def _queue_repair(repair_key: tuple, job_kwargs: dict, report, label: str, target_lang: str) -> str:
    with _pending_repairs_lock:
        if repair_key in _repair_keys:
            print(f"[REPAIR] Duplicate repair suppressed for {label} '{target_lang}'")
            return "repair-duplicate"
        if not _repair_capacity.acquire(blocking=False):
            print(f"[REPAIR] Queue full; deferred {label} '{target_lang}' to the next scan")
            return "repair-deferred"
        _repair_keys.add(repair_key)
        try:
            future = _get_repair_executor().submit(_perform_repair, **job_kwargs)
        except Exception:
            _repair_keys.discard(repair_key)
            _repair_capacity.release()
            raise
        _pending_repairs[future] = {"key": repair_key, "report": report}
    for index in report.repairable_cue_indexes:
        print(f"[REPAIR] Queued {label} '{target_lang}' cue position {index + 1}")
    return "repair-queued"


def _drain_pending_repairs(stats: dict) -> list[RepairJobResult]:
    with _pending_repairs_lock:
        futures = list(_pending_repairs)
    results: list[RepairJobResult] = []
    for future in as_completed(futures):
        with _pending_repairs_lock:
            metadata = _pending_repairs.pop(future, {})
            _repair_keys.discard(metadata.get("key"))
        _repair_capacity.release()
        try:
            result = future.result()
        except Exception as exc:
            print(f"{RED}[ERROR] Repair worker failed: {exc}{RESET}")
            stats["cleanup_repair_failures"] = stats.get("cleanup_repair_failures", 0) + 1
            continue
        results.append(result)
        stats["cleanup_repair_attempts"] = stats.get("cleanup_repair_attempts", 0) + result.attempts
        stats["cleanup_second_attempts"] = stats.get("cleanup_second_attempts", 0) + result.second_attempts
        _record_cleanup_stats(stats, result.action, result.report)
        if result.action == "repaired":
            stats["completed"] += 1
            stats["translations"].append(f"{result.title}: repaired {result.target_lang}")
            if result.item_type:
                _mark_activity(stats, result.item_type)
            else:
                stats["episode_activity"] = True
                stats["movie_activity"] = True
        elif result.action in ("quarantined", "deleted"):
            stats["failed"] += 1
            stats["cleaned"] = stats.get("cleaned", 0) + 1
            if result.item_type:
                _mark_activity(stats, result.item_type)
            else:
                stats["episode_activity"] = True
                stats["movie_activity"] = True
        elif result.action == "repair-deferred":
            stats["cleanup_repair_deferred"] = stats.get("cleanup_repair_deferred", 0) + 1
    return results


def _shutdown_repair_executor() -> None:
    global _repair_executor
    with _repair_executor_lock:
        executor = _repair_executor
        _repair_executor = None
    if executor is not None:
        print("[REPAIR] Waiting for active repair worker(s) to stop")
        executor.shutdown(wait=True, cancel_futures=False)


def _validate_translated_file(
    source_path: str,
    target_path: str,
    source_lang: str,
    target_lang: str,
    item_id: int | None,
    title: str = "",
    dry_run: bool = False,
    *,
    defer_repair: bool = False,
    item_type: str | None = None,
) -> tuple[str, object]:
    if target_lang not in CLEANUP_LANGUAGES:
        return "valid", None

    from clean_et_subs import recover_subtitle_pair, target_language_for_code, validate_subtitle_pair

    target_language = target_language_for_code(target_lang)
    detector = _get_cleanup_detector()
    if target_language is None or detector is None:
        return "valid", None

    source_hash = _file_hash_or_none(source_path)
    expected_target_hash = _file_hash_or_none(target_path)
    report = validate_subtitle_pair(
        Path(source_path), Path(target_path), detector, target_language,
        target_lang=target_lang, **_validation_kwargs(),
    )
    if report.valid:
        if CLEANUP_FORMAT_REPAIR_ENABLED:
            recovery = recover_subtitle_pair(source_path, target_path)
            if recovery.safe and recovery.changed and recovery.raw is not None:
                candidate = _write_recovery_candidate(target_path, recovery.raw, same_directory=False)
                try:
                    normalized_report = validate_subtitle_pair(
                        Path(source_path), candidate, detector, target_language,
                        target_lang=target_lang, **_validation_kwargs(),
                    )
                finally:
                    try:
                        candidate.unlink()
                    except OSError:
                        pass
                if not normalized_report.valid:
                    print(
                        f"{YELLOW}[FORMAT] Normalized candidate rejected for "
                        f"{os.path.basename(target_path)}: {normalized_report.summary()}{RESET}"
                    )
                    recovery = None
                if recovery is None:
                    print(f"[CLEANUP] OK {os.path.basename(target_path)} (original retained)")
                    _record_validation_result(target_path, source_hash, expected_target_hash, "valid", report)
                    return "valid", report
                if dry_run:
                    print(f"[FORMAT] DRYRUN: would normalize {target_path}")
                    return "dry-run", report
                with _target_repair_lock(target_path):
                    temp = _write_recovery_candidate(target_path, recovery.raw)
                    os.replace(temp, target_path)
                print(
                    f"{GREEN}[FORMAT] Normalized {os.path.basename(target_path)} without AI: "
                    f"{', '.join(recovery.fixes) or 'canonicalized'}{RESET}"
                )
                _record_validation_result(
                    target_path,
                    source_hash,
                    _file_hash_or_none(target_path),
                    "valid",
                    normalized_report,
                    formatFixes=recovery.fixes,
                    formatRecoveredCues=recovery.recovered_cues,
                )
                return "formatted", normalized_report
        print(f"[CLEANUP] OK {os.path.basename(target_path)} (source-aware validation passed)")
        _record_validation_result(target_path, source_hash, expected_target_hash, "valid", report)
        return "valid", report

    label = title or os.path.basename(target_path)
    print(f"{YELLOW}[CLEANUP] Invalid translation {label} '{target_lang}': {report.summary()}{RESET}")
    recovery_raw = None
    format_fixes: list[str] = []
    format_recovered_cues: list[int] = []
    if CLEANUP_FORMAT_REPAIR_ENABLED:
        recovery = recover_subtitle_pair(source_path, target_path)
        if recovery.safe and recovery.changed and recovery.raw is not None:
            candidate = _write_recovery_candidate(target_path, recovery.raw, same_directory=False)
            try:
                recovered_report = validate_subtitle_pair(
                    Path(source_path), candidate, detector, target_language,
                    target_lang=target_lang, **_validation_kwargs(),
                )
            finally:
                try:
                    candidate.unlink()
                except OSError:
                    pass
            format_fixes = recovery.fixes
            format_recovered_cues = recovery.recovered_cues
            print(
                f"[FORMAT] Source-anchored recovery prepared for {label} '{target_lang}': "
                f"{', '.join(format_fixes) or 'canonicalized'}"
            )
            if recovered_report.valid:
                if dry_run:
                    print(f"[FORMAT] DRYRUN: would atomically repair {target_path}")
                    return "dry-run", report
                with _target_repair_lock(target_path):
                    temp = _write_recovery_candidate(target_path, recovery.raw)
                    os.replace(temp, target_path)
                print(f"{GREEN}[FORMAT] Repaired and validated {label} '{target_lang}' without AI{RESET}")
                _record_validation_result(
                    target_path, source_hash, _file_hash_or_none(target_path), "valid", recovered_report,
                    formatFixes=format_fixes, formatRecoveredCues=format_recovered_cues,
                )
                return "formatted", recovered_report
            report = recovered_report
            recovery_raw = recovery.raw
        elif not recovery.safe:
            dbg(f"Format recovery unsafe for {label}: {recovery.reason}")

    if CLEANUP_REPAIR_ENABLED and report.repairable_cue_indexes and not dry_run:
        job_kwargs = {
            "source_path": source_path,
            "target_path": target_path,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "item_id": item_id,
            "title": label,
            "item_type": item_type,
            "initial_report": report,
            "expected_target_hash": expected_target_hash,
            "recovery_raw": recovery_raw,
            "format_fixes": format_fixes,
            "format_recovered_cues": format_recovered_cues,
        }
        if defer_repair:
            repair_key = (
                os.path.normcase(os.path.abspath(target_path)), source_hash, expected_target_hash,
                target_lang, tuple(report.repairable_cue_indexes),
            )
            return _queue_repair(repair_key, job_kwargs, report, label, target_lang), report
        result = _perform_repair(**job_kwargs)
        return result.action, result.report

    action = _apply_cleanup_action(
        target_path,
        source_path,
        target_lang,
        report,
        format_fixes=format_fixes,
        format_recovered_cues=format_recovered_cues,
        dry_run=dry_run,
    )
    if action in ("quarantined", "deleted") and item_id is not None:
        _clear_submission(item_id, target_lang)
        print(f"[CLEANUP] Cleared cooldown for retry: {label} '{target_lang}'")
    return action, report

# ---------------------------------------------------------------------------
# Per-item processor
# ---------------------------------------------------------------------------

def _item_title(item: dict, item_type: str) -> str:
    if item_type == "episodes":
        return item.get("seriesTitle", item.get("series_title", "Unknown"))
    return item.get("title", "Unknown")


def _mark_activity(stats: dict, item_type: str) -> None:
    if item_type == "episodes":
        stats["episode_activity"] = True
    else:
        stats["movie_activity"] = True


def _bazarr_has_repaired_path(result: RepairJobResult) -> bool:
    if result.item_id is None or result.item_type not in ("episodes", "movies"):
        return True
    id_field = "sonarrEpisodeId" if result.item_type == "episodes" else "radarrId"
    _, subtitles = fetch_subtitles(result.item_type, result.item_id, id_field)
    expected = os.path.normcase(os.path.normpath(result.target_path))
    return any(
        os.path.normcase(os.path.normpath(str(subtitle.get("path", "")))) == expected
        for subtitle in subtitles
    )


def _record_cleanup_stats(stats: dict, action: str, report) -> None:
    if report is None:
        return
    stats["cleanup_checked"] = stats.get("cleanup_checked", 0) + 1
    excessive = sum(issue.rule == "excessive_lines" for issue in report.issues)
    stats["cleanup_excessive_lines"] = stats.get("cleanup_excessive_lines", 0) + excessive
    stats["cleanup_other_issues"] = stats.get("cleanup_other_issues", 0) + len(report.issues) - excessive
    if action == "formatted":
        stats["cleanup_formatted"] = stats.get("cleanup_formatted", 0) + 1
    elif action == "repaired":
        stats["cleanup_repaired"] = stats.get("cleanup_repaired", 0) + 1
    elif action in ("quarantined", "deleted", "reported", "dry-run"):
        stats[f"cleanup_{action}"] = stats.get(f"cleanup_{action}", 0) + 1
    elif action == "action-failed":
        stats["cleanup_action_failed"] = stats.get("cleanup_action_failed", 0) + 1


def process_item(item: dict, item_type: str, id_field: str,
                 stats: dict, stats_lock: threading.Lock) -> None:
    if shutdown_requested:
        return

    item_id = item.get(id_field)
    if item_id is None:
        return
    title = _item_title(item, item_type)
    lingarr_media_type = "Episode" if item_type == "episodes" else "Movie"

    missing_raw = {s.get("code2") for s in item.get("missing_subtitles", []) if s.get("code2")}
    missing = {l for l in LANGUAGES if l in missing_raw}

    if not missing:
        return

    video_path, subs = fetch_subtitles(item_type, item_id, id_field)
    available_map: dict[str, str] = {}
    for s in subs:
        code, path = s.get("code2"), s.get("path", "")
        if not code or not path:
            continue
        if code not in available_map or _sub_priority(path, code) < _sub_priority(available_map[code], code):
            available_map[code] = path

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
        _se = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", os.path.basename(source_path))
        if _se:
            title = f"{title} S{int(_se.group(1)):02d}E{int(_se.group(2)):02d}"
    item_timeout = _estimate_timeout(source_path)
    print(f"[INFO] {title}: source={source_lang}, targets={target_langs}")

    media_id = lingarr_resolve_media_id(item_type, item_id)
    if media_id is None:
        print(f"{YELLOW}[SKIP] {title}: not found in Lingarr media cache (id={item_id}){RESET}")
        return

    for target_lang in target_langs:
        if shutdown_requested:
            break

        age = _check_cooldown(item_id, target_lang)
        if age is not None:
            cooldown_remaining = RESUBMIT_COOLDOWN - age
            print(f"[SKIP] {title} '{target_lang}': submitted {age}s ago, "
                  f"cooldown {cooldown_remaining}s remaining")
            continue

        if video_path:
            target_path = os.path.splitext(video_path)[0] + f".{target_lang}.srt"
        else:
            target_path = _derive_target_path(source_path, source_lang, target_lang)
        if not target_path:
            print(f"{YELLOW}[SKIP] {title} '{target_lang}': could not derive target path{RESET}")
            continue

        existing = _find_existing_target(video_path, target_lang) if video_path else (
            target_path if os.path.exists(target_path) else None
        )
        if existing:
            print(f"[DISK] {title} '{target_lang}': {os.path.basename(existing)} already on disk")
            validation_action, validation_report = _validate_translated_file(
                source_path, existing, source_lang, target_lang, item_id, title=title,
                defer_repair=True, item_type=item_type,
            )
            if validation_action in ("valid", "formatted", "repaired"):
                with stats_lock:
                    stats["completed"] += 1
                    stats["translations"].append(f"{title}: {source_lang} -> {target_lang} (on disk)")
                    _record_cleanup_stats(stats, validation_action, validation_report)
                    _mark_activity(stats, item_type)
            elif validation_action.startswith("repair-"):
                with stats_lock:
                    stats["cleanup_repair_queued"] = stats.get("cleanup_repair_queued", 0) + (
                        validation_action == "repair-queued"
                    )
                    stats["cleanup_repair_deferred"] = stats.get("cleanup_repair_deferred", 0) + (
                        validation_action == "repair-deferred"
                    )
            else:
                with stats_lock:
                    stats["failed"] += 1
                    stats.setdefault("cleaned", 0)
                    stats["cleaned"] += 1
                    _record_cleanup_stats(stats, validation_action, validation_report)
                    if validation_action in ("quarantined", "deleted"):
                        _mark_activity(stats, item_type)
            continue

        while not shutdown_requested:
            active = lingarr_active_count()
            if active is None or active < PARALLEL_TRANSLATES:
                break
            print(f"[INFO] Lingarr queue full ({active}/{PARALLEL_TRANSLATES}) — waiting {POLL_INTERVAL}s...")
            for _ in range(POLL_INTERVAL):
                if shutdown_requested:
                    return
                time.sleep(1)

        if os.path.exists(target_path):
            print(f"[DISK] {title} '{target_lang}': appeared during queue wait")
            validation_action, validation_report = _validate_translated_file(
                source_path, target_path, source_lang, target_lang, item_id, title=title,
                defer_repair=True, item_type=item_type,
            )
            if validation_action in ("valid", "formatted", "repaired"):
                with stats_lock:
                    stats["completed"] += 1
                    stats["translations"].append(f"{title}: {source_lang} -> {target_lang} (on disk)")
                    _record_cleanup_stats(stats, validation_action, validation_report)
                    _mark_activity(stats, item_type)
            elif validation_action.startswith("repair-"):
                with stats_lock:
                    stats["cleanup_repair_queued"] = stats.get("cleanup_repair_queued", 0) + (
                        validation_action == "repair-queued"
                    )
                    stats["cleanup_repair_deferred"] = stats.get("cleanup_repair_deferred", 0) + (
                        validation_action == "repair-deferred"
                    )
            else:
                with stats_lock:
                    stats["failed"] += 1
                    _record_cleanup_stats(stats, validation_action, validation_report)
                    if validation_action in ("quarantined", "deleted"):
                        _mark_activity(stats, item_type)
            continue

        src_lines = _count_dialogue_lines(source_path)
        if src_lines is None:
            print(f"{YELLOW}[SKIP] {title} '{target_lang}': source not readable — deferring{RESET}")
            with stats_lock:
                stats.setdefault("deferred", 0)
                stats["deferred"] += 1
            continue
        if src_lines == 0:
            print(f"{YELLOW}[SKIP] {title} '{target_lang}': source has no dialogue lines{RESET}")
            with stats_lock:
                stats.setdefault("deferred", 0)
                stats["deferred"] += 1
            continue

        print(f"[TRANSLATE] {title}: {source_lang} -> {target_lang} ({src_lines} lines)")
        job_id = lingarr_submit_file(media_id, source_path, source_lang, target_lang, lingarr_media_type)
        if job_id is None:
            with stats_lock:
                stats["failed"] += 1
            continue

        _record_submission(item_id, target_lang, target_path)
        with stats_lock:
            stats["submitted"] += 1
            _mark_activity(stats, item_type)

        deadline = time.time() + item_timeout
        status = lingarr_poll_job(job_id, deadline, title)
        if status != "Completed":
            with stats_lock:
                if status is None:
                    stats["timed_out"] += 1
                else:
                    stats["failed"] += 1
            continue

        if not os.path.exists(target_path):
            print(f"{YELLOW}[WARNING] {title} '{target_lang}': Lingarr completed but file missing at {target_path}{RESET}")
            with stats_lock:
                stats["timed_out"] += 1
            continue

        validation_action, validation_report = _validate_translated_file(
            source_path, target_path, source_lang, target_lang, item_id, title=title,
            defer_repair=True, item_type=item_type,
        )
        if validation_action in ("valid", "formatted", "repaired"):
            print(f"{GREEN}[OK] {title} '{target_lang}' translated to {os.path.basename(target_path)}{RESET}")
            with stats_lock:
                stats["completed"] += 1
                stats["translations"].append(f"{title}: {source_lang} -> {target_lang}")
                _record_cleanup_stats(stats, validation_action, validation_report)
                _mark_activity(stats, item_type)
        elif validation_action.startswith("repair-"):
            with stats_lock:
                stats["cleanup_repair_queued"] = stats.get("cleanup_repair_queued", 0) + (
                    validation_action == "repair-queued"
                )
                stats["cleanup_repair_deferred"] = stats.get("cleanup_repair_deferred", 0) + (
                    validation_action == "repair-deferred"
                )
        else:
            with stats_lock:
                stats["failed"] += 1
                stats.setdefault("cleaned", 0)
                stats["cleaned"] += 1
                _record_cleanup_stats(stats, validation_action, validation_report)

# ---------------------------------------------------------------------------
# Existing-library cleanup
# ---------------------------------------------------------------------------

def run_existing_cleanup_scan() -> dict:
    stats = {
        "files_checked": 0,
        "skipped_unchanged": 0,
        "excessive_line_cues": 0,
        "other_invalid_cues": 0,
        "formatted_files": 0,
        "repaired_files": 0,
        "repair_failures": 0,
        "repair_queued": 0,
        "repair_deferred": 0,
        "quarantined_files": 0,
        "deleted_files": 0,
        "reported_files": 0,
        "dry_run_files": 0,
        "without_source": 0,
        "action_failures": 0,
    }
    if not CLEANUP_SCAN_EXISTING or not CLEANUP_LANGUAGES:
        return stats

    from clean_et_subs import (
        discover_target_subtitles,
        file_sha256,
        find_preferred_source,
        target_language_for_code,
        validate_subtitle_without_source,
    )

    with _cleanup_scan_lock:
        detector = _get_cleanup_detector()
        if detector is None:
            return stats
        state = _get_validation_state()
        candidates = discover_target_subtitles(CLEANUP_ROOTS, CLEANUP_LANGUAGES)
        print(
            f"[SCAN] Existing subtitle cleanup found {len(candidates)} target file(s) "
            f"under {', '.join(str(root) for root in CLEANUP_ROOTS)}"
        )

        changed = False
        for candidate in candidates:
            if shutdown_requested:
                break
            target_language = target_language_for_code(candidate.target_lang)
            if target_language is None:
                print(f"{YELLOW}[SCAN] Unsupported target language for {candidate.path}{RESET}")
                continue

            source_path, source_lang = find_preferred_source(candidate)
            try:
                target_hash = file_sha256(candidate.path)
                source_hash = file_sha256(source_path) if source_path is not None else None
            except OSError as e:
                print(f"{YELLOW}[SCAN] Could not hash {candidate.path}: {e}{RESET}")
                continue

            if state.is_unchanged_valid(candidate.path, source_hash, target_hash):
                stats["skipped_unchanged"] += 1
                continue

            stats["files_checked"] += 1
            if source_path is not None and source_lang is not None:
                action, report = _validate_translated_file(
                    str(source_path),
                    str(candidate.path),
                    source_lang,
                    candidate.target_lang,
                    None,
                    title=candidate.path.name,
                    dry_run=CLEANUP_SCAN_DRY_RUN,
                    defer_repair=not CLEANUP_SCAN_DRY_RUN,
                )
            else:
                stats["without_source"] += 1
                report = validate_subtitle_without_source(
                    candidate.path,
                    detector,
                    target_language,
                    target_lang=candidate.target_lang,
                    **_validation_kwargs(),
                )
                if report.valid:
                    print(f"[SCAN] OK {candidate.path.name} (target-only validation passed)")
                    _record_validation_result(
                        candidate.path, None, target_hash, "valid", report, sourceAvailable=False
                    )
                    action = "valid"
                else:
                    print(
                        f"{YELLOW}[SCAN] Invalid target without source {candidate.path.name}: "
                        f"{report.summary()}{RESET}"
                    )
                    action = _apply_cleanup_action(
                        candidate.path,
                        None,
                        candidate.target_lang,
                        report,
                        lingarr_outcome="not attempted: no source subtitle",
                        dry_run=CLEANUP_SCAN_DRY_RUN,
                    )

            if report is not None:
                excessive = sum(issue.rule == "excessive_lines" for issue in report.issues)
                stats["excessive_line_cues"] += excessive
                stats["other_invalid_cues"] += len(report.issues) - excessive
                if (
                    action not in ("valid", "formatted", "repaired", "repair-queued", "repair-duplicate", "repair-deferred")
                    and source_path is not None
                    and CLEANUP_REPAIR_ENABLED
                    and report.repairable_cue_indexes
                    and not CLEANUP_SCAN_DRY_RUN
                ):
                    stats["repair_failures"] += 1

            if action == "formatted":
                stats["formatted_files"] += 1
                changed = True
            elif action == "repaired":
                stats["repaired_files"] += 1
                changed = True
            elif action == "repair-queued":
                stats["repair_queued"] += 1
            elif action == "repair-deferred":
                stats["repair_deferred"] += 1
            elif action == "quarantined":
                stats["quarantined_files"] += 1
                _clear_submission_for_path(candidate.path, candidate.target_lang)
                changed = True
            elif action == "deleted":
                stats["deleted_files"] += 1
                _clear_submission_for_path(candidate.path, candidate.target_lang)
                changed = True
            elif action == "reported":
                stats["reported_files"] += 1
            elif action == "dry-run":
                stats["dry_run_files"] += 1
            elif action == "action-failed":
                stats["action_failures"] += 1

        print("[SCAN] Existing subtitle cleanup summary:")
        print(f"  Checked             : {stats['files_checked']}")
        print(f"  Skipped unchanged   : {stats['skipped_unchanged']}")
        print(f"  Excessive-line cues : {stats['excessive_line_cues']}")
        print(f"  Other invalid cues  : {stats['other_invalid_cues']}")
        print(f"  Format-only repairs : {stats['formatted_files']}")
        print(f"  Repaired files      : {stats['repaired_files']}")
        print(f"  AI repairs queued   : {stats['repair_queued']}")
        print(f"  AI repairs deferred : {stats['repair_deferred']}")
        print(f"  Repair failures     : {stats['repair_failures']}")
        print(f"  Quarantined files   : {stats['quarantined_files']}")
        if CLEANUP_SCAN_DRY_RUN:
            print(f"  Dry-run files       : {stats['dry_run_files']}")

        if changed and not shutdown_requested:
            trigger_bazarr_sync(True, True)
            wait_for_bazarr_sync(True, True, SYNC_TIMEOUT)
        return stats


def _run_existing_cleanup_scan_safely() -> dict | None:
    try:
        return run_existing_cleanup_scan()
    except Exception as e:
        print(f"{RED}[ERROR] Existing subtitle cleanup scan failed: {e}{RESET}")
        if DEBUG:
            import traceback
            traceback.print_exc()
        return None


def run_retention_housekeeping() -> dict:
    from clean_et_subs import purge_old_files

    current_log = [_app_log_sink.current_path] if _app_log_sink.current_path is not None else []
    quarantine_removed = purge_old_files(CLEANUP_QUARANTINE_DIR, RETENTION_DAYS)
    logs_removed = purge_old_files(LOG_DIR, RETENTION_DAYS, exclude=current_log)
    try:
        state_removed = _get_validation_state().prune_older_than(RETENTION_DAYS)
    except OSError as e:
        print(f"{YELLOW}[WARNING] Could not prune validation state: {e}{RESET}")
        state_removed = 0
    result = {
        "quarantine_files": len(quarantine_removed),
        "log_files": len(logs_removed),
        "state_entries": state_removed,
    }
    print(
        f"[RETENTION] Removed {result['quarantine_files']} quarantine file(s), "
        f"{result['log_files']} log file(s), and {result['state_entries']} validation state record(s) "
        f"older than {RETENTION_DAYS} days"
    )
    return result


# ---------------------------------------------------------------------------
# Cycle orchestrator
# ---------------------------------------------------------------------------

def _drain_lingarr_queue() -> None:
    drain_deadline = time.time() + 2 * CHECK_INTERVAL
    while not shutdown_requested:
        active = lingarr_active_count()
        if active is None or active == 0:
            break
        if time.time() >= drain_deadline:
            print(f"{YELLOW}[WARNING] Lingarr still has {active} active job(s) after "
                  f"{2 * CHECK_INTERVAL}s — continuing anyway{RESET}")
            break
        print(f"[INFO] Lingarr has {active} active job(s) — waiting before next cycle...")
        for _ in range(POLL_INTERVAL):
            if shutdown_requested:
                return
            time.sleep(1)


def run_cycle(cycle_num: int) -> None:
    print(f"\n{BOLD}{CYAN}===== Cycle #{cycle_num} ====={RESET}")

    lingarr_build_media_cache()

    active_before = lingarr_active_count()
    if active_before is not None:
        print(f"[INFO] Lingarr active queue at cycle start: {active_before}")

    stats: dict = {
        "submitted": 0,
        "completed": 0,
        "timed_out": 0,
        "failed": 0,
        "translations": [],
        "episode_activity": False,
        "movie_activity": False,
    }
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

    repair_results: list[RepairJobResult] = []
    pending_count = len(_pending_repairs)
    if pending_count:
        print(f"[REPAIR] Waiting for {pending_count} queued repair job(s) before Bazarr sync")
        repair_results = _drain_pending_repairs(stats)

    print(f"\n{BOLD}===== Cycle #{cycle_num} Summary ====={RESET}")
    print(f"  Submitted  : {stats['submitted']}")
    print(f"  Completed  : {stats['completed']}")
    print(f"  Timed out  : {stats['timed_out']}")
    print(f"  Failed     : {stats['failed']}")
    if stats.get("cleaned"):
        print(f"  Cleaned    : {stats['cleaned']}")
    if stats.get("cleanup_checked"):
        print(f"  Cleanup checked       : {stats['cleanup_checked']}")
        print(f"  Excessive-line cues   : {stats.get('cleanup_excessive_lines', 0)}")
        print(f"  Other cleanup issues  : {stats.get('cleanup_other_issues', 0)}")
        print(f"  Format-only repairs   : {stats.get('cleanup_formatted', 0)}")
        print(f"  AI repairs queued     : {stats.get('cleanup_repair_queued', 0)}")
        print(f"  AI repair attempts    : {stats.get('cleanup_repair_attempts', 0)}")
        print(f"  No-context attempts   : {stats.get('cleanup_second_attempts', 0)}")
        print(f"  AI repairs deferred   : {stats.get('cleanup_repair_deferred', 0)}")
        print(f"  Repaired translations : {stats.get('cleanup_repaired', 0)}")
        print(f"  Quarantined files     : {stats.get('cleanup_quarantined', 0)}")
    if stats["translations"]:
        print("  Completed translations:")
        for t in stats["translations"]:
            print(f"    {GREEN}- {t}{RESET}")
    active_after = lingarr_active_count()
    if active_after is not None:
        print(f"  Lingarr active queue now: {active_after}")
    sys.stdout.flush()

    had_activity = (
        stats["submitted"] > 0
        or stats["completed"] > 0
        or stats["episode_activity"]
        or stats["movie_activity"]
    )
    if had_activity and not shutdown_requested:
        had_episodes = stats["episode_activity"]
        had_movies = stats["movie_activity"]
        trigger_bazarr_sync(had_episodes, had_movies)
        wait_for_bazarr_sync(had_episodes, had_movies, SYNC_TIMEOUT)
        repaired_with_ids = [
            result for result in repair_results
            if result.action == "repaired" and result.item_id is not None
        ]
        missing = [result for result in repaired_with_ids if not _bazarr_has_repaired_path(result)]
        if missing and not shutdown_requested:
            retry_episodes = any(result.item_type == "episodes" for result in missing)
            retry_movies = any(result.item_type == "movies" for result in missing)
            print(f"{YELLOW}[WARNING] Bazarr did not register {len(missing)} repaired path(s); retrying scan once{RESET}")
            trigger_bazarr_sync(retry_episodes, retry_movies)
            wait_for_bazarr_sync(retry_episodes, retry_movies, SYNC_TIMEOUT)
            still_missing = [result for result in missing if not _bazarr_has_repaired_path(result)]
            stats["cleanup_bazarr_registration_failures"] = len(still_missing)
            for result in still_missing:
                print(f"{YELLOW}[WARNING] Bazarr still does not list repaired subtitle for {result.title} '{result.target_lang}'{RESET}")

    _drain_lingarr_queue()

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"\n{BOLD}Bazarr AutoTranslate starting{RESET}")
    print(f"  Bazarr URL        : {BAZARR_URL}")
    print(f"  Lingarr URL       : {LINGARR_URL}")
    print(f"  Languages         : {', '.join(LANGUAGES)}")
    print(f"  Cleanup languages : {', '.join(sorted(CLEANUP_LANGUAGES)) or '(none)'}")
    print(f"  Existing scan     : {'ON' if CLEANUP_SCAN_EXISTING else 'off'} every {CLEANUP_SCAN_INTERVAL}s")
    print(f"  Cleanup roots     : {', '.join(str(root) for root in CLEANUP_ROOTS)}")
    print(f"  Cleanup action    : {CLEANUP_ACTION}{' (scan dry-run)' if CLEANUP_SCAN_DRY_RUN else ''}")
    print(f"  Max cue lines     : {CLEANUP_MAX_CUE_LINES}")
    print(f"  Format recovery   : {'ON' if CLEANUP_FORMAT_REPAIR_ENABLED else 'off'}")
    print(f"  Repair workers    : {CLEANUP_REPAIR_WORKERS} (+{CLEANUP_REPAIR_WORKERS} beyond file workers)")
    print(f"  Repair queue max  : {CLEANUP_REPAIR_QUEUE_MAX}")
    print(f"  Retention         : {RETENTION_DAYS} days (checked every {RETENTION_CHECK_INTERVAL}s)")
    print(f"  Parallel workers  : {PARALLEL_TRANSLATES}")
    print(f"  Check interval    : {CHECK_INTERVAL}s (after Bazarr sync)")
    print(f"  Poll interval     : {POLL_INTERVAL}s  (floor {POLL_TIMEOUT}s per translation)")
    print(f"  Sync timeout      : {SYNC_TIMEOUT}s")
    print(f"  Sync start timeout: {SYNC_START_TIMEOUT}s")
    print(f"  Resubmit cooldown : {RESUBMIT_COOLDOWN}s")
    print(f"  Debug mode        : {'ON' if DEBUG else 'off'}")
    sys.stdout.flush()

    langs = lingarr_get_languages()
    if langs:
        print(f"[INFO] Lingarr supports languages: {', '.join(langs)}")

    _load_submit_cache()
    run_retention_housekeeping()
    last_retention_check = time.monotonic()

    print("[INFO] Waiting 30s for services to start...")
    sys.stdout.flush()
    for _ in range(30):
        if shutdown_requested:
            break
        time.sleep(1)

    if not shutdown_requested:
        print("[INFO] Running initial Bazarr subtitle synchronization...")
        trigger_bazarr_sync(True, True)
        wait_for_bazarr_sync(True, True, SYNC_TIMEOUT)

    last_cleanup_scan = 0.0
    if not shutdown_requested and CLEANUP_SCAN_EXISTING:
        _run_existing_cleanup_scan_safely()
        last_cleanup_scan = time.monotonic()

    cycle = 1
    while not shutdown_requested:
        if time.monotonic() - last_retention_check >= RETENTION_CHECK_INTERVAL:
            run_retention_housekeeping()
            last_retention_check = time.monotonic()
        if (
            CLEANUP_SCAN_EXISTING
            and last_cleanup_scan > 0
            and time.monotonic() - last_cleanup_scan >= CLEANUP_SCAN_INTERVAL
        ):
            _run_existing_cleanup_scan_safely()
            last_cleanup_scan = time.monotonic()
        run_cycle(cycle)
        cycle += 1
        if shutdown_requested:
            break
        print(f"[INFO] Next cycle in {CHECK_INTERVAL}s...")
        for _ in range(CHECK_INTERVAL):
            if shutdown_requested:
                break
            time.sleep(1)

    _shutdown_repair_executor()
    print("[INFO] Bazarr AutoTranslate stopped cleanly.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"{RED}[FATAL] {e}{RESET}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
