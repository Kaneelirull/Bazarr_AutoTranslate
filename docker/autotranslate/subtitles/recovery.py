from __future__ import annotations

import hashlib
import re
import unicodedata


def normalized_output_fingerprint(value: str) -> str:
    """Hash equivalent provider failures without retaining subtitle dialogue."""

    normalized = unicodedata.normalize("NFKC", str(value)).replace("\r\n", "\n")
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def strategy_for_attempt(attempt_index: int, has_context: bool) -> str:
    if attempt_index == 0 and has_context:
        return "contextual"
    if attempt_index <= 1:
        return "context_free"
    return "strict_isolated"
