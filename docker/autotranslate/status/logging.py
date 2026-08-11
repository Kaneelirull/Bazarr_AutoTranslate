from __future__ import annotations

import logging
import threading
import time
from pathlib import Path


class DailyLogSink:
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
                self.current_path = (
                    self.log_dir / f"bazarr-autotranslate-{current_date}.log"
                )
                self._file = self.current_path.open(
                    "a", encoding="utf-8", buffering=1
                )
                self._date = current_date
            self._file.write(value)

    def flush(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


class TeeStream:
    def __init__(self, primary, sink: DailyLogSink):
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


class DailyLogHandler(logging.Handler):
    def __init__(self, sink: DailyLogSink):
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.write(self.format(record) + "\n")


class UtcLogFormatter(logging.Formatter):
    converter = time.gmtime


class QueuedLogStream:
    """Turn fragmented print writes into one queued record per thread and line."""

    def __init__(self, logger: logging.Logger, level: int, primary):
        self.logger = logger
        self.level = level
        self.primary = primary
        self._local = threading.local()

    def write(self, value: str) -> int:
        if not value:
            return 0
        pending = getattr(self._local, "pending", "") + value
        lines = pending.split("\n")
        self._local.pending = lines.pop()
        for line in lines:
            if line:
                self.logger.log(self.level, line)
        return len(value)

    def flush(self) -> None:
        pending = getattr(self._local, "pending", "")
        if pending:
            self.logger.log(self.level, pending)
            self._local.pending = ""

    def fileno(self):
        return self.primary.fileno()

    def isatty(self) -> bool:
        return self.primary.isatty()

    @property
    def encoding(self):
        return self.primary.encoding
