"""Docker-compatible bootstrap for the packaged AutoTranslate runtime."""

from __future__ import annotations

import sys

from autotranslate.app import main


if __name__ == "__main__":
    raise SystemExit(main())

# Preserve historical monkey-patching and documented imports by making this
# module name an alias of the packaged implementation.
from autotranslate import runtime as _runtime
sys.modules[__name__] = _runtime
