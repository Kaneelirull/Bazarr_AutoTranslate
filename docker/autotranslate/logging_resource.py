from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
from pathlib import Path

from .status.logging import (
    DailyLogHandler, DailyLogSink, QueuedLogStream, UtcLogFormatter,
)


class ApplicationLogging:
    """Owns process stream redirection and the asynchronous log listener."""

    def __init__(self, log_dir: Path, *, debug: bool = False) -> None:
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        self._sink = DailyLogSink(log_dir)
        self._queue: queue.Queue = queue.Queue()
        self._logger = logging.getLogger("bazarr_autotranslate")
        self._logger.setLevel(logging.DEBUG if debug else logging.INFO)
        self._logger.propagate = False
        self._handler = logging.handlers.QueueHandler(self._queue)
        self._logger.addHandler(self._handler)
        self._console = logging.StreamHandler(self._stdout)
        self._console.setFormatter(logging.Formatter("%(message)s"))
        daily = DailyLogHandler(self._sink)
        daily.setFormatter(UtcLogFormatter(
            "%(asctime)s.%(msecs)03dZ %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
        ))
        self._listener = logging.handlers.QueueListener(
            self._queue, self._console, daily, respect_handler_level=True
        )
        self._listener.start()
        sys.stdout = QueuedLogStream(self._logger, logging.INFO, self._stdout)
        sys.stderr = QueuedLogStream(self._logger, logging.ERROR, self._stderr)
        self._closed = False

    @property
    def current_path(self) -> Path | None:
        """Return the active daily log without exposing sink ownership."""
        return self._sink.current_path

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        sys.stdout = self._stdout
        sys.stderr = self._stderr
        self._listener.stop()
        self._logger.removeHandler(self._handler)
        self._handler.close()
        self._sink.close()
