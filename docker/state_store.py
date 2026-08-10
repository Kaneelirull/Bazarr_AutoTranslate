"""Compatibility alias for the packaged SQLite state store."""

from __future__ import annotations

import sys

from autotranslate.persistence import state_store as _state_store


sys.modules[__name__] = _state_store
