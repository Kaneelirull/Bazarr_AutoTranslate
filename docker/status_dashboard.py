from __future__ import annotations

import html
import json
import os
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
    "failures",
)


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


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
            temp = self.history_path.with_suffix(self.history_path.suffix + ".tmp")
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                for event in self._history:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            os.replace(temp, self.history_path)
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
                "title", "itemType", "itemId", "targetLanguage", "state",
                "queuedAt", "startedAt", "finishedAt", "durationSeconds",
                "repaired", "reason",
            )
        }
        if job.get("state") not in TERMINAL_STATES:
            started = _parse_iso(job.get("startedAt")) or _parse_iso(job.get("queuedAt"))
            if started is not None:
                public["durationSeconds"] = max(0, round(self.clock() - started, 3))
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
        temp = self.snapshot_path.with_suffix(self.snapshot_path.suffix + ".tmp")
        temp.write_text(
            json.dumps(self._snapshot_locked(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, self.snapshot_path)


def render_dashboard(snapshot: dict) -> str:
    cycle = snapshot.get("currentCycle") or {}
    service = snapshot.get("service") or {}
    history = snapshot.get("history") or {}
    maintenance = snapshot.get("maintenance") or {}
    active = snapshot.get("activeJobs") or []
    upcoming = snapshot.get("upNext") or []
    recent = snapshot.get("recentOutcomes") or []

    def esc(value) -> str:
        return html.escape(str(value if value is not None else "—"), quote=True)

    def metric(label: str, value, tone: str = "") -> str:
        return (
            f'<div class="metric {tone}"><span>{esc(label)}</span>'
            f'<strong>{esc(value)}</strong></div>'
        )

    def jobs_table(rows: list[dict], empty: str) -> str:
        if not rows:
            return f'<p class="empty">{esc(empty)}</p>'
        body = "".join(
            "<tr>"
            f"<td>{esc(row.get('title'))}</td>"
            f"<td>{esc(row.get('itemType'))}</td>"
            f"<td>{esc(row.get('targetLanguage'))}</td>"
            f"<td><span class=\"badge {esc(row.get('state'))}\">{esc(row.get('state'))}</span></td>"
            f"<td>{esc(row.get('durationSeconds'))}</td>"
            f"<td>{esc(row.get('finishedAt') or row.get('startedAt') or row.get('queuedAt'))}</td>"
            "</tr>"
            for row in rows
        )
        return (
            "<div class=\"table-wrap\"><table><thead><tr><th>Title</th><th>Type</th>"
            "<th>Language</th><th>Status</th><th>Seconds</th><th>Time</th></tr></thead>"
            f"<tbody>{body}</tbody></table></div>"
        )

    recent_rows = [
        {
            "title": row.get("title"),
            "itemType": row.get("itemType"),
            "targetLanguage": row.get("targetLanguage"),
            "state": "repaired" if row.get("repaired") and row.get("outcome") == "accepted"
            else row.get("outcome"),
            "durationSeconds": row.get("durationSeconds"),
            "finishedAt": row.get("timestamp"),
        }
        for row in recent
    ]
    history_cards = "".join(
        f'<div class="window"><h3>{esc(label)}</h3>'
        + "".join(
            f"<span><b>{esc(values.get(key, 0))}</b> {esc(key.replace('_', ' '))}</span>"
            for key in OUTCOME_KEYS
        )
        + "</div>"
        for label, values in history.items()
    )
    last_maintenance = (maintenance.get("lastScan") or {}).get("metrics") or {}
    cycle_number = cycle.get("number", "—")
    initial = cycle.get("initial", 0)
    done = cycle.get("done", 0)
    progress = round((done / initial) * 100) if initial else 0

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Bazarr AutoTranslate Status</title>
<style>
:root{{--bg:#0b1017;--panel:#121a24;--panel2:#172231;--line:#263446;--text:#edf4ff;--muted:#91a2b8;--cyan:#55d6d0;--green:#75df9a;--amber:#ffc86b;--red:#ff7f8b}}
*{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(circle at top right,#162739 0,#0b1017 42%);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{width:min(1180px,calc(100% - 32px));margin:32px auto 64px}} header{{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:24px}}
h1{{margin:0;font-size:clamp(24px,4vw,38px);letter-spacing:-.04em}} h2{{font-size:18px;margin:0 0 16px}} h3{{margin:0 0 10px;color:var(--cyan)}}
.sub{{color:var(--muted);margin-top:6px}} .refresh{{color:#071116;background:var(--cyan);font-weight:800;text-decoration:none;padding:10px 16px;border-radius:10px}}
.phase{{display:inline-flex;gap:8px;align-items:center;text-transform:uppercase;letter-spacing:.1em;font-size:12px;color:var(--cyan)}} .dot{{width:8px;height:8px;border-radius:50%;background:var(--cyan);box-shadow:0 0 14px var(--cyan)}}
.panel{{background:linear-gradient(145deg,var(--panel),#0f1721);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:18px;box-shadow:0 12px 35px #0004}}
.metrics{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}} .metric{{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px}}
.metric span{{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.07em}} .metric strong{{display:block;font-size:28px;margin-top:4px}} .metric.good strong{{color:var(--green)}} .metric.warn strong{{color:var(--amber)}} .metric.bad strong{{color:var(--red)}}
.progress{{height:9px;background:#080c11;border-radius:99px;overflow:hidden;margin:16px 0 6px}} .progress i{{display:block;height:100%;width:{progress}%;background:linear-gradient(90deg,var(--cyan),var(--green))}}
.split{{display:grid;grid-template-columns:1fr 1fr;gap:18px}} .windows{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}} .window{{background:var(--panel2);border:1px solid var(--line);padding:14px;border-radius:12px}} .window span{{display:block;color:var(--muted);font-size:12px}} .window b{{color:var(--text);font-size:15px}}
table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;padding:11px 10px;border-bottom:1px solid var(--line)}} th{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}} .table-wrap{{overflow:auto}} .empty{{color:var(--muted);margin:0}}
.badge{{display:inline-block;padding:3px 8px;border-radius:99px;background:#263446;color:var(--text);font-size:12px}} .badge.accepted,.badge.repaired{{background:#143a29;color:var(--green)}} .badge.failed,.badge.timed_out,.badge.quarantined{{background:#45232a;color:var(--red)}} .badge.translating,.badge.validating,.badge.repairing{{background:#173d49;color:var(--cyan)}} .badge.deferred{{background:#46391e;color:var(--amber)}}
.maintenance{{display:flex;flex-wrap:wrap;gap:14px;color:var(--muted)}} .maintenance b{{color:var(--text)}} footer{{color:var(--muted);font-size:12px;margin-top:20px}}
@media(max-width:900px){{.metrics{{grid-template-columns:repeat(3,1fr)}}.windows{{grid-template-columns:repeat(2,1fr)}}.split{{grid-template-columns:1fr}}}}
@media(max-width:560px){{main{{width:min(100% - 20px,1180px);margin-top:18px}}header{{flex-direction:column}}.metrics{{grid-template-columns:repeat(2,1fr)}}.windows{{grid-template-columns:1fr}}}}
</style>
</head>
<body><main>
<header><div><div class="phase"><span class="dot"></span>{esc(service.get("phase"))}</div>
<h1>Translation status</h1><div class="sub">Cycle #{esc(cycle_number)} · generated {esc(snapshot.get("generatedAt"))}</div></div>
<a class="refresh" href="/">Refresh</a></header>
<section class="panel"><h2>Current queue</h2><div class="metrics">
{metric("Initial", initial)}
{metric("Done", done)}
{metric("Remaining", cycle.get("remaining", 0), "warn")}
{metric("Queued", cycle.get("queued", 0))}
{metric("Translating", cycle.get("translating", 0))}
{metric("Validating", cycle.get("validating", 0))}
{metric("Repairing", cycle.get("repairing", 0))}
{metric("Accepted", cycle.get("accepted", 0), "good")}
{metric("Failed", cycle.get("failed", 0), "bad")}
{metric("Timed out", cycle.get("timedOut", 0), "bad")}
{metric("Deferred", cycle.get("deferred", 0), "warn")}
{metric("Quarantined", cycle.get("quarantined", 0), "bad")}
{metric("Elapsed sec", cycle.get("elapsedSeconds", 0))}
{metric("Next cycle", service.get("nextCycleAt") or "—")}
</div><div class="progress"><i></i></div><div class="sub">{progress}% terminal</div></section>
<section class="split"><div class="panel"><h2>Active now</h2>{jobs_table(active, "No active translations or repairs.")}</div>
<div class="panel"><h2>Up next</h2>{jobs_table(upcoming, "No queued jobs.")}</div></section>
<section class="panel"><h2>Rolling outcomes</h2><div class="windows">{history_cards}</div></section>
<section class="panel"><h2>Recent outcomes</h2>{jobs_table(recent_rows, "No completed jobs recorded yet.")}</section>
<section class="panel"><h2>Latest maintenance scan</h2><div class="maintenance">
{''.join(f"<span><b>{esc(last_maintenance.get(key, 0))}</b> {esc(key)}</span>" for key in MAINTENANCE_KEYS)}
</div></section>
<footer>Manual refresh only · trusted LAN endpoint · no subtitle text or filesystem paths exposed</footer>
</main></body></html>"""


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
            else:
                self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')

        def do_HEAD(self) -> None:
            if self.path in ("/", "/api/status", "/healthz"):
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
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
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
