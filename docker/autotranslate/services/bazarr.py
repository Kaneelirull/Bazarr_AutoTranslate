from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class ServiceRequestError(RuntimeError):
    def __init__(self, service: str, operation: str, message: str):
        super().__init__(f"{service} {operation}: {message}")
        self.service = service
        self.operation = operation


@dataclass
class BazarrClient:
    base_url: str
    api_key: str
    request_json: Callable[..., Any]

    @property
    def headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "X-API-KEY": self.api_key}

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url.rstrip('/')}/api/{endpoint}"

    def fetch_wanted(self, item_type: str) -> list[dict]:
        payload = self.request_json(
            "get",
            self._url(f"{item_type}/wanted"),
            service="Bazarr",
            operation=f"fetch {item_type} wanted queue",
            headers=self.headers,
            params={"start": 0, "length": -1},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data", []), list):
            raise ServiceRequestError("Bazarr", "fetch wanted queue", "unexpected response schema")
        return payload.get("data", [])

    def fetch_subtitles(self, item_type: str, item_id: int) -> tuple[str, list[dict]]:
        endpoint = "episodes" if item_type == "episodes" else "movies"
        parameter = "episodeid[]" if item_type == "episodes" else "radarrid[]"
        payload = self.request_json(
            "get",
            self._url(endpoint),
            service="Bazarr",
            operation=f"fetch {item_type} subtitles for {item_id}",
            headers=self.headers,
            params={parameter: item_id},
        )
        data = payload.get("data", []) if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ServiceRequestError("Bazarr", "fetch subtitles", "unexpected response schema")
        if not data:
            return "", []
        if not isinstance(data[0], dict) or not isinstance(data[0].get("subtitles", []), list):
            raise ServiceRequestError("Bazarr", "fetch subtitles", "unexpected item schema")
        return str(data[0].get("path") or ""), data[0].get("subtitles", [])
