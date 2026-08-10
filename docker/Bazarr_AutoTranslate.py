"""Docker-compatible bootstrap for the packaged AutoTranslate runtime."""

from __future__ import annotations

import sys

from autotranslate import runtime as _runtime


if __name__ == "__main__":
    raise SystemExit(_runtime.main())

# Preserve historical monkey-patching and documented imports by making this
# module name an alias of the packaged implementation.
sys.modules[__name__] = _runtime
