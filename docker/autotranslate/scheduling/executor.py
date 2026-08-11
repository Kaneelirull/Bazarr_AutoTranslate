from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, wait


class DaemonExecutor:
    """Minimal Future executor whose workers cannot block process shutdown."""

    def __init__(self, max_workers: int, thread_name_prefix: str):
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._stopped = False
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"{thread_name_prefix}_{index}",
                daemon=True,
            )
            for index in range(max(1, int(max_workers)))
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, function, /, *args, **kwargs) -> Future:
        future = Future()
        with self._lock:
            if self._stopped:
                raise RuntimeError("repair executor is shut down")
            self._queue.put((future, function, args, kwargs))
        return future

    def _worker(self) -> None:
        while True:
            work = self._queue.get()
            try:
                if work is None:
                    return
                future, function, args, kwargs = work
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(function(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

    def shutdown(
        self,
        wait: bool = True,
        *,
        cancel_futures: bool = False,
        timeout: float | None = None,
    ) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            if cancel_futures:
                while True:
                    try:
                        work = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if work is not None:
                        work[0].cancel()
                    self._queue.task_done()
            for _thread in self._threads:
                self._queue.put(None)
        if wait:
            deadline = (
                None if timeout is None
                else time.monotonic() + max(0.0, float(timeout))
            )
            for thread in self._threads:
                remaining = (
                    None if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                thread.join(remaining)


def completed_futures(
    futures: Iterable[Future],
    *,
    stop_requested: Callable[[], bool],
    poll_seconds: float = 0.25,
) -> Iterator[Future]:
    """Yield completed futures without trapping the caller in an unbounded wait."""
    pending = set(futures)
    while pending:
        done, pending = wait(pending, timeout=max(0.01, poll_seconds))
        yield from done
        if stop_requested():
            return


# Historical name retained while callers migrate to the generic executor.
DaemonRepairExecutor = DaemonExecutor
