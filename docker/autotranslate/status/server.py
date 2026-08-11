from __future__ import annotations

import html
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .tracker import StatusTracker
from .manual_review_http import parse_review_query, render_review_page
from ..manual_review import (
    ManualReviewConflict,
    ManualReviewDisabled,
    ManualReviewNotFound,
    ManualReviewUnavailable,
)

STATIC_DIR = Path(__file__).parents[2] / "static"
STATIC_ASSETS = {
    "/assets/dashboard.css": ("text/css; charset=utf-8", STATIC_DIR / "dashboard.css"),
    "/assets/dashboard.js": ("text/javascript; charset=utf-8", STATIC_DIR / "dashboard.js"),
    "/assets/logs.js": ("text/javascript; charset=utf-8", STATIC_DIR / "logs.js"),
    "/assets/review.js": ("text/javascript; charset=utf-8", STATIC_DIR / "review.js"),
    "/assets/plus-jakarta-sans.ttf": ("font/ttf", STATIC_DIR / "plus-jakarta-sans.ttf"),
}

def render_dashboard(snapshot: dict, display_timezone: str = "UTC") -> str:
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
    display_timezone = html.escape(display_timezone.strip() or "UTC", quote=True)
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
<main id="dashboard" data-snapshot="{bootstrap}" data-time-zone="{display_timezone}" aria-busy="true">
  <h1>Translation status</h1>
  <p class="loading">Loading translation status…</p>
</main>
<noscript>
  <p class="noscript">JavaScript is required for live status updates.</p>
</noscript>
</body>
</html>"""


def render_logs_page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Bazarr AutoTranslate Logs</title>
<link rel="stylesheet" href="/assets/dashboard.css">
<script src="/assets/logs.js" defer></script>
</head>
<body>
<main class="dashboard-shell log-shell">
  <header class="topbar"><div><div class="eyebrow">Diagnostics</div>
  <h1>Service logs</h1><p class="header-meta">Sanitized, read-only operational output · New records use UTC timestamps</p></div>
  <div class="header-actions">
    <a class="btn btn-secondary" href="/">Status</a>
    <a class="btn btn-secondary" href="/review">Manual review</a>
    <a class="btn btn-secondary" href="/logs" aria-current="page">Logs</a>
    <button class="btn btn-secondary" id="theme-toggle" type="button" aria-label="Switch color theme">Theme</button>
    <button class="btn btn-primary" id="refresh-button" type="button">Refresh now</button>
  </div></header>
  <section class="panel">
    <form id="log-filters" class="log-filters">
      <label>Level <select name="level"><option value="">All</option><option>ERROR</option>
      <option>WARNING</option><option>FAIL</option><option>TIMEOUT</option></select></label>
      <label>Show or job <input name="job" maxlength="100" placeholder="Top Gear or job ID"></label>
      <label>Search text <input name="q" maxlength="100" placeholder="Message contains…"></label>
      <button class="btn btn-primary" type="submit">Filter</button>
    </form>
    <p id="log-status" class="section-note" role="status">Loading logs...</p>
    <pre id="log-output" class="log-output" tabindex="0"></pre>
    <button id="load-more" class="btn btn-secondary" type="button">Load older</button>
  </section>
</main>
</body>
</html>"""


_SECRET_RE = re.compile(r"(?i)(api[-_ ]?key|authorization)(\s*[:=]\s*)\S+")
_UNIX_MEDIA_PATH_RE = re.compile(r"(?<!\w)/(?:media|config)/[^\s]+")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\[^\r\n]+")


def _sanitize_log_line(line: str) -> str:
    line = _SECRET_RE.sub(r"\1\2<redacted>", line)
    line = _UNIX_MEDIA_PATH_RE.sub("<managed-path>", line)
    line = _WINDOWS_PATH_RE.sub("<managed-path>", line)
    return line[:4000]


def read_logs(log_dir: Path, query: dict[str, list[str]]) -> dict:
    limit = min(500, max(1, int(query.get("limit", ["200"])[0])))
    cursor = max(0, int(query.get("cursor", ["0"])[0]))
    level = query.get("level", [""])[0].strip().upper()
    job = query.get("job", [""])[0].strip().casefold()
    text = query.get("q", [""])[0].strip().casefold()
    lines: list[str] = []
    files = sorted(log_dir.glob("bazarr-autotranslate-*.log"), reverse=True)[:30]
    for path in files:
        try:
            file_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(file_lines):
            clean = _sanitize_log_line(line)
            folded = clean.casefold()
            if level and f"[{level}]" not in clean.upper():
                continue
            if job and job not in folded:
                continue
            if text and text not in folded:
                continue
            lines.append(clean)
            if len(lines) >= cursor + limit + 1:
                break
        if len(lines) >= cursor + limit + 1:
            break
    page = lines[cursor:cursor + limit]
    return {
        "lines": page,
        "nextCursor": cursor + limit if len(lines) > cursor + limit else None,
        "sanitized": True,
    }


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True


def start_status_server(
    tracker: StatusTracker,
    bind: str,
    port: int,
    log_dir: Path | str | None = None,
    *,
    server_class=None,
    manual_review_service=None,
    display_timezone: str = "UTC",
) -> tuple[_DashboardServer, threading.Thread]:
    server_type = server_class or _DashboardServer
    managed_log_dir = Path(log_dir) if log_dir is not None else None
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                body = render_dashboard(
                    tracker.snapshot(), display_timezone
                ).encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
            elif parsed.path == "/logs":
                self._send(
                    200,
                    "text/html; charset=utf-8",
                    render_logs_page().encode("utf-8"),
                )
            elif parsed.path == "/review":
                self._send(
                    200, "text/html; charset=utf-8",
                    render_review_page(display_timezone).encode("utf-8"),
                )
            elif parsed.path == "/api/manual-reviews":
                if manual_review_service is None:
                    self._json_error(503, "service_unavailable", "Manual review service is unavailable.")
                    return
                try:
                    payload = manual_review_service.list_reviews(
                        parse_review_query(parsed.query)
                    )
                except (TypeError, ValueError):
                    self._json_error(400, "invalid_query", "Manual review query is invalid.")
                    return
                except (OSError, RuntimeError):
                    self._json_error(503, "service_unavailable", "Manual reviews are temporarily unavailable.")
                    return
                self._send_json(200, payload)
            elif parsed.path == "/api/logs":
                if managed_log_dir is None:
                    self._send(404, "application/json; charset=utf-8", b'{"error":"logs unavailable"}')
                    return
                try:
                    payload = read_logs(managed_log_dir, parse_qs(parsed.query))
                except (TypeError, ValueError):
                    self._send(400, "application/json; charset=utf-8", b'{"error":"invalid query"}')
                    return
                self._send(
                    200,
                    "application/json; charset=utf-8",
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                )
            elif parsed.path == "/api/status":
                body = json.dumps(
                    tracker.snapshot(), ensure_ascii=False, indent=2
                ).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            elif parsed.path == "/healthz":
                snapshot = tracker.snapshot()
                body = json.dumps({
                    "status": "ok",
                    "phase": snapshot["service"]["phase"],
                    "generatedAt": snapshot["generatedAt"],
                }).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            elif parsed.path in STATIC_ASSETS:
                content_type, asset_path = STATIC_ASSETS[parsed.path]
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

        def do_POST(self) -> None:
            parsed = urlsplit(self.path)
            match = re.fullmatch(r"/api/manual-reviews/(\d+)/actions", parsed.path)
            if match is None:
                self._json_error(404, "not_found", "Route not found.")
                return
            if manual_review_service is None:
                self._json_error(503, "service_unavailable", "Manual review service is unavailable.")
                return
            if self.headers.get("X-Bazarr-Autotranslate-Action") != "manual-review":
                self._json_error(403, "action_header_required", "Manual review action header is required.")
                return
            fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
            if fetch_site not in {"", "none", "same-origin"}:
                self._json_error(403, "cross_origin_rejected", "Cross-origin actions are not allowed.")
                return
            content_type = self.headers.get_content_type()
            if content_type != "application/json":
                self._json_error(415, "json_required", "Content-Type must be application/json.")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length < 0 or length > 4096:
                self._json_error(400, "invalid_body_size", "Request body must be 4 KiB or smaller.")
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, ValueError):
                self._json_error(400, "invalid_json", "Request body must be valid JSON.")
                return
            if not isinstance(payload, dict) or set(payload) != {"action", "expectedUpdatedAt"}:
                self._json_error(400, "invalid_body", "Action and expectedUpdatedAt are required.")
                return
            try:
                action = str(payload["action"])
                expected = float(payload["expectedUpdatedAt"])
                if not math.isfinite(expected):
                    raise ValueError("expectedUpdatedAt must be finite")
                if action not in {"recheck", "queue_retry", "dismiss"}:
                    raise ValueError("unsupported action")
                status, response = manual_review_service.perform_action(
                    int(match.group(1)), action, expected
                )
            except (TypeError, ValueError):
                self._json_error(400, "invalid_action", "Manual review action is invalid.")
                return
            except ManualReviewDisabled:
                self._json_error(403, "actions_disabled", "Manual review actions are disabled.")
                return
            except ManualReviewNotFound:
                self._json_error(404, "not_found", "Manual review was not found.")
                return
            except ManualReviewConflict:
                self._json_error(409, "stale_review", "Manual review changed; refresh and try again.")
                return
            except ManualReviewUnavailable:
                self._json_error(503, "service_unavailable", "Manual review action could not be completed.")
                return
            except (OSError, RuntimeError):
                self._json_error(503, "service_unavailable", "Manual review action could not be completed.")
                return
            self._send_json(status, response)

        def do_HEAD(self) -> None:
            path = urlsplit(self.path).path
            if path in ("/", "/review", "/logs", "/api/status", "/api/logs", "/api/manual-reviews", "/healthz") or path in STATIC_ASSETS:
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
            try:
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
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                self.close_connection = True

        def _send_json(self, status: int, payload: dict) -> None:
            self._send(
                status, "application/json; charset=utf-8",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )

        def _json_error(self, status: int, code: str, message: str) -> None:
            self._send_json(status, {"error": {"code": code, "message": message}})

        def log_message(self, _format: str, *_args) -> None:
            return

    server = server_type((bind, port), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="status-dashboard",
        daemon=True,
    )
    thread.start()
    return server, thread
