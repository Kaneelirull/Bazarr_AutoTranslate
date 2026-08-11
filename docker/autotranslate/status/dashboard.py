from __future__ import annotations

from .tracker import (
    StatusTracker,
    build_cycle_jobs,
    episode_identity,
    episode_identity_from_path,
    retry_media_identity,
)
from .server import (
    _DashboardServer,
    read_logs,
    render_dashboard,
    render_logs_page,
    start_status_server as _start_status_server,
)


def start_status_server(tracker, bind, port, log_dir=None):
    """Compatibility adapter retaining the historical patch point."""
    return _start_status_server(
        tracker, bind, port, log_dir, server_class=_DashboardServer
    )

__all__ = [
    "StatusTracker", "build_cycle_jobs", "episode_identity",
    "episode_identity_from_path", "retry_media_identity", "read_logs", "render_dashboard",
    "render_logs_page", "start_status_server",
]
