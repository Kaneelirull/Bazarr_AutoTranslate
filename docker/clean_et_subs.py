"""Compatibility import and standalone CLI for subtitle maintenance."""

from __future__ import annotations

import sys

from autotranslate.subtitles import core as _core


if __name__ == "__main__":
    raise SystemExit(_core.main())

sys.modules[__name__] = _core
