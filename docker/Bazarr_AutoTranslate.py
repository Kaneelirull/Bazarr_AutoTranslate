"""Docker-compatible bootstrap for the packaged AutoTranslate runtime."""

from __future__ import annotations

from autotranslate.app import main


if __name__ == "__main__":
    raise SystemExit(main())
