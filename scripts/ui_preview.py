"""Run a local status-dashboard preview with representative in-memory data."""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from autotranslate.status.server import start_status_server  # noqa: E402
from autotranslate.status.tracker import StatusTracker  # noqa: E402


def main() -> None:
    state = REPO_ROOT / ".test-logs" / "ui-preview"
    state.mkdir(parents=True, exist_ok=True)
    tracker = StatusTracker(state / "status.json", state / "history.jsonl")
    jobs = [{
        "key": "preview:episodes:1:et",
        "title": "The Last of Us",
        "episodeCode": "S02E04",
        "episodeTitle": "Day One",
        "itemType": "episodes",
        "itemId": 1,
        "targetLanguage": "et",
        "operation": "translation",
    }]
    tracker.start_cycle("preview", 42, jobs)
    tracker.transition(jobs[0]["key"], "translating")
    server, _thread = start_status_server(
        tracker, "127.0.0.1", 8876, display_timezone="Europe/Tallinn"
    )
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
