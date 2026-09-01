from __future__ import annotations

from .common import ACTIVE_RETRY_STATES, SCHEMA_VERSION, StateStoreError
from .database import DatabaseState
from .migrations import MigrationsRepositoryMixin
from .submissions import SubmissionsRepositoryMixin
from .artifacts import ArtifactsRepositoryMixin
from .retries import RetriesRepositoryMixin
from .circuits import CircuitsRepositoryMixin
from .repairs import RepairsRepositoryMixin
from .operations import OperationsRepositoryMixin
from .manual_reviews import ManualReviewsRepositoryMixin
from .maintenance_cache import MaintenanceCacheRepositoryMixin


class StateStore(
    MigrationsRepositoryMixin,
    SubmissionsRepositoryMixin,
    ArtifactsRepositoryMixin,
    RetriesRepositoryMixin,
    CircuitsRepositoryMixin,
    RepairsRepositoryMixin,
    OperationsRepositoryMixin,
    ManualReviewsRepositoryMixin,
    MaintenanceCacheRepositoryMixin,
    DatabaseState,
):
    """Package-owned aggregate composed from focused SQLite repositories."""


__all__ = ["ACTIVE_RETRY_STATES", "SCHEMA_VERSION", "StateStore", "StateStoreError"]
