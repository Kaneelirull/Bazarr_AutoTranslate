"""Exact, operator-approved name matching. No persistence or runtime access."""
from __future__ import annotations

import unicodedata


def normalize_name_phrase(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def approved_name(source: str, target: str, pairs=()) -> bool:
    key = (normalize_name_phrase(source), normalize_name_phrase(target))
    return any(tuple(pair) == key for pair in pairs)


def name_scope(identity: dict) -> str:
    series = identity.get("canonicalSeriesKey") or identity.get("canonical_series_key") or identity.get("seriesKey")
    if identity.get("approvalScope"):
        return str(identity["approvalScope"])
    kind = identity.get("itemType") or identity.get("item_type")
    item = identity.get("itemId", identity.get("item_id"))
    if kind == "episodes" and series and str(series).startswith("sonarr:"):
        return str(series)
    return f"{kind}:{item}"
