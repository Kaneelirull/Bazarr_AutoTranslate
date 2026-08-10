"""Compatibility alias for the packaged status tracker and HTTP server."""

from __future__ import annotations

import sys

from autotranslate.status import dashboard as _dashboard


sys.modules[__name__] = _dashboard
