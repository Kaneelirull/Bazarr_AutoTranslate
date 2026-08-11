from __future__ import annotations

import html
from typing import Any, Protocol
from urllib.parse import parse_qs


class ManualReviewProtocol(Protocol):
    actions_enabled: bool

    def list_reviews(self, query: dict[str, Any] | None = None) -> dict: ...
    def perform_action(self, plan_id: int, action: str, expected_updated_at: float) -> tuple[int, dict]: ...


def render_review_page(display_timezone: str = "UTC") -> str:
    timezone = html.escape(str(display_timezone or "UTC"), quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Bazarr AutoTranslate Manual review</title>
<link rel="stylesheet" href="/assets/dashboard.css">
<script src="/assets/review.js" defer></script>
</head>
<body>
<main id="manual-review" class="dashboard-shell review-shell" data-time-zone="{timezone}" aria-busy="true">
  <h1>Manual review</h1>
  <p class="loading">Loading manual reviews&hellip;</p>
</main>
<noscript><p class="noscript">JavaScript is required for manual review actions.</p></noscript>
</body>
</html>"""


def parse_review_query(raw_query: str) -> dict[str, Any]:
    parsed = parse_qs(raw_query, keep_blank_values=True)

    def one(name: str, default: str = "") -> str:
        values = parsed.get(name, [default])
        if len(values) != 1:
            raise ValueError(f"duplicate {name}")
        return values[0]

    page = int(one("page", "1"))
    page_size = int(one("pageSize", "20"))
    if page < 1 or page_size < 1 or page_size > 100:
        raise ValueError("invalid pagination")
    status = one("status")
    item_type = one("itemType")
    language = one("language")
    sort = one("sort", "updatedAt")
    direction = one("direction", "desc")
    if status not in {"", "needs_attention", "manually_queued", "resolved", "dismissed"}:
        raise ValueError("invalid status")
    if item_type not in {"", "episodes", "movies"}:
        raise ValueError("invalid item type")
    if sort not in {"updatedAt", "media", "language", "attempts", "status"}:
        raise ValueError("invalid sort")
    if direction not in {"asc", "desc"}:
        raise ValueError("invalid direction")
    query = one("q")
    if len(query) > 100 or len(language) > 20:
        raise ValueError("query too long")
    return {
        "page": page,
        "pageSize": page_size,
        "q": query,
        "status": status,
        "itemType": item_type,
        "language": language,
        "sort": sort,
        "direction": direction,
    }
