"""Compatibility import and standalone CLI for subtitle maintenance."""

from __future__ import annotations

from autotranslate.subtitles.library import main


if __name__ == "__main__":
    raise SystemExit(main())
