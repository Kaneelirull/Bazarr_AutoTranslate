from __future__ import annotations

import html
import json
import os
import re
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable


TERMINAL_STATES = {"accepted", "failed", "timed_out", "deferred", "quarantined"}
ACTIVE_STATES = {"translating", "validating", "repairing"}
HISTORY_WINDOWS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "12h": 12 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
}
OUTCOME_KEYS = ("accepted", "repaired", "failed", "timed_out", "deferred", "quarantined")
MAINTENANCE_KEYS = (
    "formatted",
    "repaired",
    "quarantined",
    "deleted",
    "undersized",
    "pruned",
    "source_less_warnings",
    "repeat_quarantines",
    "quarantine_holds",
    "variant_outputs",
    "failures",
)
STATIC_DIR = Path(__file__).with_name("static")
STATIC_ASSETS = {
    "/assets/dashboard.css": (
        "text/css; charset=utf-8",
        STATIC_DIR / "dashboard.css",
    ),
    "/assets/dashboard.js": (
        "text/javascript; charset=utf-8",
        STATIC_DIR / "dashboard.js",
    ),
    "/assets/plus-jakarta-sans.ttf": (
        "font/ttf",
        STATIC_DIR / "plus-jakarta-sans.ttf",
    ),
}


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _first_int(item: dict, *keys: str) -> int | None:
    for key in keys:
        value = item.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def episode_identity(item: dict, item_type: str) -> tuple[str | None, str | None]:
    """Return a public episode code and title from Bazarr metadata."""
    if item_type != "episodes":
        return None, None
    season = _first_int(item, "season", "seasonNumber", "season_number")
    episode = _first_int(item, "episode", "episodeNumber", "episode_number")
    code = (
        f"S{season:02d}E{episode:02d}"
        if season is not None and episode is not None
        else None
    )
    series_title = item.get("seriesTitle") or item.get("series_title")
    episode_title = (
        item.get("episodeTitle")
        or item.get("episode_title")
        or item.get("title")
    )
    if not episode_title or str(episode_title).strip() == str(series_title or "").strip():
        episode_title = None
    return code, str(episode_title).strip() if episode_title else None


def episode_identity_from_path(path: str | Path | None) -> str | None:
    """Extract an SxxEyy identity without exposing the source path."""
    if not path:
        return None
    match = re.search(r"(?i)(?:^|[^a-z0-9])s(\d{1,3})e(\d{1,3})(?:[^a-z0-9]|$)", Path(path).name)
    if not match:
        return None
    return f"S{int(match.group(1)):02d}E{int(match.group(2)):02d}"


def build_cycle_jobs(
    work: list[tuple[dict, str, str]],
    languages: list[str],
    cycle_id: str,
    title_getter: Callable[[dict, str], str],
) -> list[dict]:
    """Create a fixed, ordered queue of one job per wanted target language."""
    jobs: list[dict] = []
    seen: set[str] = set()
    for item, item_type, id_field in work:
        item_id = item.get(id_field)
        if item_id is None:
            continue
        episode_code, episode_title = episode_identity(item, item_type)
        missing = {
            str(entry.get("code2")).strip().lower()
            for entry in item.get("missing_subtitles", [])
            if isinstance(entry, dict) and entry.get("code2")
        }
        for language in languages:
            if language not in missing:
                continue
            key = f"{cycle_id}:{item_type}:{item_id}:{language}"
            if key in seen:
                continue
            seen.add(key)
            jobs.append({
                "key": key,
                "itemType": item_type,
                "itemId": item_id,
                "title": title_getter(item, item_type),
                "episodeCode": episode_code,
                "episodeTitle": episode_title,
                "targetLanguage": language,
                "state": "queued",
                "queuedAt": None,
                "startedAt": None,
                "finishedAt": None,
                "durationSeconds": None,
                "repaired": False,
                "reason": None,
            })
    return jobs


class StatusTracker:
    def __init__(
        self,
        snapshot_path: Path | str,
        history_path: Path | str,
        *,
        retention_days: int = 30,
        recent_limit: int = 20,
        clock: Callable[[], float] = time.time,
    ):
        self.snapshot_path = Path(snapshot_path)
        self.history_path = Path(history_path)
        self.retention_days = max(7, retention_days)
        self.recent_limit = max(1, recent_limit)
        self.clock = clock
        self._lock = threading.RLock()
        self._started_at = self.clock()
        self._service = {
            "phase": "startup",
            "startedAt": _utc_iso(self._started_at),
            "nextCycleAt": None,
        }
        self._cycle: dict | None = None
        self._history: list[dict] = []
        self._maintenance = {"lastScan": None}
        self._load()
        self._recover_interrupted()
        self._write_snapshot_locked()

    def _load(self) -> None:
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            cycle = payload.get("currentCycle")
            if isinstance(cycle, dict) and isinstance(cycle.get("jobs"), list):
                self._cycle = cycle
            maintenance = payload.get("maintenance")
            if isinstance(maintenance, dict):
                self._maintenance["lastScan"] = maintenance.get("lastScan")
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass

        try:
            with self.history_path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(event, dict) and _parse_iso(event.get("timestamp")) is not None:
                        self._history.append(event)
        except (FileNotFoundError, OSError):
            pass
        self._drop_expired_locked()

    def _recover_interrupted(self) -> None:
        if self._cycle is None or self._cycle.get("completedAt"):
            return
        now = self.clock()
        for job in self._cycle.get("jobs", []):
            if job.get("state") not in TERMINAL_STATES:
                self._finish_job_locked(
                    job, "deferred", now, reason="interrupted by service restart"
                )
        self._cycle["completedAt"] = _utc_iso(now)

    def set_phase(self, phase: str, *, next_cycle_at: float | None = None) -> None:
        with self._lock:
            self._service["phase"] = phase
            self._service["nextCycleAt"] = (
                _utc_iso(next_cycle_at) if next_cycle_at is not None else None
            )
            self._write_snapshot_locked()

    def start_cycle(self, cycle_id: str, cycle_number: int, jobs: list[dict]) -> None:
        now = self.clock()
        with self._lock:
            for job in jobs:
                job["queuedAt"] = _utc_iso(now)
            self._cycle = {
                "id": cycle_id,
                "number": cycle_number,
                "startedAt": _utc_iso(now),
                "completedAt": None,
                "initial": len(jobs),
                "jobs": jobs,
            }
            self._service["phase"] = "translating"
            self._service["nextCycleAt"] = None
            self._write_snapshot_locked()

    def transition(
        self,
        job_key: str,
        state: str,
        *,
        repaired: bool = False,
        reason: str | None = None,
    ) -> bool:
        if state not in TERMINAL_STATES | ACTIVE_STATES | {"queued"}:
            raise ValueError(f"unsupported status state: {state}")
        with self._lock:
            job = self._find_job_locked(job_key)
            if job is None or job.get("state") in TERMINAL_STATES:
                return False
            now = self.clock()
            if state in TERMINAL_STATES:
                self._finish_job_locked(job, state, now, repaired=repaired, reason=reason)
            else:
                job["state"] = state
                if state in ACTIVE_STATES and not job.get("startedAt"):
                    job["startedAt"] = _utc_iso(now)
                if repaired:
                    job["repaired"] = True
                if reason:
                    job["reason"] = reason
            self._write_snapshot_locked()
            return True

    def transition_for(
        self,
        item_type: str | None,
        item_id: int | None,
        target_language: str,
        state: str,
        **kwargs,
    ) -> bool:
        with self._lock:
            if self._cycle is None:
                return False
            match = next((
                job for job in self._cycle.get("jobs", [])
                if job.get("itemType") == item_type
                and job.get("itemId") == item_id
                and job.get("targetLanguage") == target_language
            ), None)
            if match is None:
                return False
            return self.transition(match["key"], state, **kwargs)

    def set_episode_identity(
        self,
        item_type: str | None,
        item_id: int | None,
        episode_code: str | None,
        episode_title: str | None = None,
    ) -> bool:
        """Enrich every queued language job for an episode and persist it."""
        if item_type != "episodes" or item_id is None:
            return False
        clean_code = str(episode_code).strip() if episode_code else None
        clean_title = str(episode_title).strip() if episode_title else None
        if not clean_code and not clean_title:
            return False
        with self._lock:
            if self._cycle is None:
                return False
            changed = False
            for job in self._cycle.get("jobs", []):
                if (
                    job.get("itemType") != item_type
                    or job.get("itemId") != item_id
                ):
                    continue
                if clean_code and not job.get("episodeCode"):
                    job["episodeCode"] = clean_code
                    changed = True
                if clean_title and not job.get("episodeTitle"):
                    job["episodeTitle"] = clean_title
                    changed = True
            if changed:
                self._write_snapshot_locked()
            return changed

    def finish_cycle(self) -> None:
        with self._lock:
            if self._cycle is None:
                return
            now = self.clock()
            for job in self._cycle.get("jobs", []):
                if job.get("state") not in TERMINAL_STATES:
                    self._finish_job_locked(
                        job, "deferred", now, reason="cycle ended before completion"
                    )
            self._cycle["completedAt"] = _utc_iso(now)
            self._write_snapshot_locked()

    def record_maintenance(self, metrics: dict) -> None:
        clean = {
            key: max(0, int(metrics.get(key, 0)))
            for key in MAINTENANCE_KEYS
        }
        now = self.clock()
        event = {
            "kind": "maintenance",
            "timestamp": _utc_iso(now),
            "metrics": clean,
        }
        with self._lock:
            self._maintenance["lastScan"] = event
            self._append_history_locked(event)
            self._write_snapshot_locked()

    def compact_history(self) -> int:
        with self._lock:
            before = len(self._history)
            self._drop_expired_locked()
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            temp: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    prefix=f".{self.history_path.name}.",
                    suffix=".tmp",
                    dir=self.history_path.parent,
                    delete=False,
                ) as handle:
                    temp = Path(handle.name)
                    for event in self._history:
                        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.history_path)
                temp = None
            finally:
                if temp is not None:
                    try:
                        temp.unlink()
                    except OSError:
                        pass
            self._write_snapshot_locked()
            return before - len(self._history)

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def _find_job_locked(self, job_key: str) -> dict | None:
        if self._cycle is None:
            return None
        return next(
            (job for job in self._cycle.get("jobs", []) if job.get("key") == job_key),
            None,
        )

    def _finish_job_locked(
        self,
        job: dict,
        state: str,
        now: float,
        *,
        repaired: bool = False,
        reason: str | None = None,
    ) -> None:
        job["state"] = state
        job["finishedAt"] = _utc_iso(now)
        job["repaired"] = bool(job.get("repaired") or repaired)
        if reason:
            job["reason"] = reason
        started = _parse_iso(job.get("startedAt")) or _parse_iso(job.get("queuedAt")) or now
        job["durationSeconds"] = max(0, round(now - started, 3))
        event = {
            "kind": "job",
            "timestamp": job["finishedAt"],
            "cycleId": self._cycle.get("id") if self._cycle else None,
            "title": job.get("title"),
            "episodeCode": job.get("episodeCode"),
            "episodeTitle": job.get("episodeTitle"),
            "itemType": job.get("itemType"),
            "itemId": job.get("itemId"),
            "targetLanguage": job.get("targetLanguage"),
            "outcome": state,
            "repaired": job["repaired"],
            "durationSeconds": job["durationSeconds"],
            "reason": job.get("reason"),
        }
        self._append_history_locked(event)

    def _append_history_locked(self, event: dict) -> None:
        self._history.append(event)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _drop_expired_locked(self) -> None:
        cutoff = self.clock() - self.retention_days * 86400
        self._history = [
            event for event in self._history
            if (_parse_iso(event.get("timestamp")) or 0) >= cutoff
        ]

    def _window_counts_locked(self, kind: str, seconds: int) -> dict:
        cutoff = self.clock() - seconds
        if kind == "job":
            counts = Counter()
            for event in self._history:
                if event.get("kind") != "job":
                    continue
                if (_parse_iso(event.get("timestamp")) or 0) < cutoff:
                    continue
                outcome = event.get("outcome")
                if outcome in OUTCOME_KEYS:
                    counts[outcome] += 1
                if outcome == "accepted" and event.get("repaired"):
                    counts["repaired"] += 1
            return {key: counts[key] for key in OUTCOME_KEYS}
        totals = Counter()
        for event in self._history:
            if event.get("kind") != "maintenance":
                continue
            if (_parse_iso(event.get("timestamp")) or 0) < cutoff:
                continue
            totals.update(event.get("metrics", {}))
        return {key: totals[key] for key in MAINTENANCE_KEYS}

    def _cycle_public_locked(self) -> dict | None:
        if self._cycle is None:
            return None
        jobs = self._cycle.get("jobs", [])
        states = Counter(job.get("state") for job in jobs)
        done = sum(states[state] for state in TERMINAL_STATES)
        started = _parse_iso(self._cycle.get("startedAt")) or self.clock()
        ended = _parse_iso(self._cycle.get("completedAt")) or self.clock()
        return {
            **self._cycle,
            "queued": states["queued"],
            "translating": states["translating"],
            "validating": states["validating"],
            "repairing": states["repairing"],
            "done": done,
            "accepted": states["accepted"],
            "failed": states["failed"],
            "timedOut": states["timed_out"],
            "deferred": states["deferred"],
            "quarantined": states["quarantined"],
            "remaining": max(0, self._cycle.get("initial", len(jobs)) - done),
            "elapsedSeconds": max(0, round(ended - started, 3)),
        }

    def _job_public(self, job: dict) -> dict:
        public = {
            key: job.get(key)
            for key in (
                "title", "episodeCode", "episodeTitle", "itemType", "itemId",
                "targetLanguage", "state",
                "queuedAt", "startedAt", "finishedAt", "durationSeconds",
                "repaired", "reason",
            )
        }
        if job.get("state") in ACTIVE_STATES:
            started = _parse_iso(job.get("startedAt"))
            if started is not None:
                public["durationSeconds"] = max(0, round(self.clock() - started, 3))
        elif job.get("state") == "queued":
            public["durationSeconds"] = None
        return public

    def _snapshot_locked(self) -> dict:
        now = self.clock()
        cycle = self._cycle_public_locked()
        jobs = self._cycle.get("jobs", []) if self._cycle else []
        active = [
            self._job_public(job) for job in jobs if job.get("state") in ACTIVE_STATES
        ]
        up_next = [
            self._job_public(job) for job in jobs if job.get("state") == "queued"
        ][:10]
        recent = [
            event for event in reversed(self._history) if event.get("kind") == "job"
        ][:self.recent_limit]
        return {
            "generatedAt": _utc_iso(now),
            "service": {
                **self._service,
                "uptimeSeconds": max(0, round(now - self._started_at, 3)),
            },
            "currentCycle": cycle,
            "activeJobs": active,
            "upNext": up_next,
            "recentOutcomes": recent,
            "history": {
                label: self._window_counts_locked("job", seconds)
                for label, seconds in HISTORY_WINDOWS.items()
            },
            "maintenance": {
                "lastScan": self._maintenance.get("lastScan"),
                "history": {
                    label: self._window_counts_locked("maintenance", seconds)
                    for label, seconds in HISTORY_WINDOWS.items()
                },
            },
        }

    def _write_snapshot_locked(self) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{self.snapshot_path.name}.",
                suffix=".tmp",
                dir=self.snapshot_path.parent,
                delete=False,
            ) as handle:
                temp = Path(handle.name)
                json.dump(
                    self._snapshot_locked(),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.snapshot_path)
            temp = None
        finally:
            if temp is not None:
                try:
                    temp.unlink()
                except OSError:
                    pass


def render_dashboard(snapshot: dict) -> str:
    """Render the CSP-safe shell; the same-origin script owns live updates."""
    bootstrap_snapshot = {**snapshot}
    cycle = snapshot.get("currentCycle")
    if isinstance(cycle, dict):
        bootstrap_snapshot["currentCycle"] = {
            key: value for key, value in cycle.items() if key != "jobs"
        }
    bootstrap = html.escape(
        json.dumps(bootstrap_snapshot, ensure_ascii=False, separators=(",", ":")),
        quote=True,
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Bazarr AutoTranslate Status</title>
<link rel="stylesheet" href="/assets/dashboard.css">
<script src="/assets/dashboard.js" defer></script>
</head>
<body>
<main id="dashboard" data-snapshot="{bootstrap}" aria-busy="true">
  <h1>Translation status</h1>
  <p class="loading">Loading translation status…</p>
</main>
<noscript>
  <p class="noscript">JavaScript is required for live status updates.</p>
</noscript>
</body>
</html>"""


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True


def start_status_server(
    tracker: StatusTracker,
    bind: str,
    port: int,
) -> tuple[_DashboardServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                body = render_dashboard(tracker.snapshot()).encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
            elif self.path == "/api/status":
                body = json.dumps(
                    tracker.snapshot(), ensure_ascii=False, indent=2
                ).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            elif self.path == "/healthz":
                snapshot = tracker.snapshot()
                body = json.dumps({
                    "status": "ok",
                    "phase": snapshot["service"]["phase"],
                    "generatedAt": snapshot["generatedAt"],
                }).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            elif self.path in STATIC_ASSETS:
                content_type, asset_path = STATIC_ASSETS[self.path]
                try:
                    body = asset_path.read_bytes()
                except OSError:
                    self._send(
                        404,
                        "application/json; charset=utf-8",
                        b'{"error":"asset unavailable"}',
                    )
                    return
                self._send(200, content_type, body)
            else:
                self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')

        def do_HEAD(self) -> None:
            if self.path in ("/", "/api/status", "/healthz") or self.path in STATIC_ASSETS:
                self._send(200, "text/plain; charset=utf-8", b"", include_body=False)
            else:
                self._send(404, "text/plain; charset=utf-8", b"", include_body=False)

        def _send(
            self,
            status: int,
            content_type: str,
            body: bytes,
            *,
            include_body: bool = True,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'self'; script-src 'self'; "
                "font-src 'self'; connect-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def log_message(self, _format: str, *_args) -> None:
            return

    server = _DashboardServer((bind, port), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="status-dashboard",
        daemon=True,
    )
    thread.start()
    return server, thread
