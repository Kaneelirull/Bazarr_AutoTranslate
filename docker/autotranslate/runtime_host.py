from __future__ import annotations

import threading

from . import runtime_context as state

_load_lock = threading.Lock()
_loaded = False


def load_runtime():
    global _loaded
    with _load_lock:
        if not _loaded:
            from . import runtime_status, runtime_services, runtime_subtitles
            from . import runtime_items, runtime_maintenance, runtime_cycle, runtime_startup
            from .runtime_initialization import initialize_runtime_state
            initialize_runtime_state()
            _loaded = True
    return state


class ProductionRuntimeHost:
    def __init__(self, config):
        self.config = config
        self.state = load_runtime()

    def run(self) -> int:
        return self.state.main(self.config)

    def close(self) -> None:
        self.state.close_runtime_resources()
