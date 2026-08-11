from __future__ import annotations

import threading

from .logging_resource import ApplicationLogging

_load_lock = threading.Lock()
_loaded = False


def _bind(state, feature_module) -> None:
    for name, value in feature_module.EXPORTS.items():
        setattr(state, name, value)


def load_runtime(config, logging_resource):
    global _loaded
    with _load_lock:
        if not _loaded:
            from . import composition
            composition.configure(config, logging_resource)
            state = composition.runtime
            from .status import runtime as status_runtime
            _bind(state, status_runtime)
            from .services import runtime as services_runtime
            _bind(state, services_runtime)
            from .subtitles import workflow as subtitle_workflow
            _bind(state, subtitle_workflow)
            from .manual_review import runtime as manual_review_runtime
            _bind(state, manual_review_runtime)
            from . import items_workflow
            _bind(state, items_workflow)
            from .maintenance import runtime as maintenance_runtime
            _bind(state, maintenance_runtime)
            from .scheduling import runtime as scheduling_runtime
            _bind(state, scheduling_runtime)
            from . import startup
            _bind(state, startup)
            from .initialization import initialize_runtime_state
            initialize_runtime_state()
            _loaded = True
    from .composition import runtime
    return runtime


class ProductionRuntimeHost:
    def __init__(self, config):
        self.config = config
        self.logging = ApplicationLogging(config.log_dir, debug=config.debug)
        try:
            self.state = load_runtime(config, self.logging)
        except Exception:
            self.logging.close()
            raise

    def run(self) -> int:
        return self.state.main(self.config)

    def close(self) -> None:
        try:
            self.state.close_runtime_resources()
        finally:
            self.logging.close()
