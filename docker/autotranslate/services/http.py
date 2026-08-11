from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import requests

from .bazarr import ServiceRequestError


@dataclass
class JsonRequester:
    """Bounded JSON HTTP retry adapter shared by service clients."""

    transport: Any = requests
    sleep: Callable[[float], None] | None = None
    emit: Callable[[str], None] = print
    max_attempts: int = 3

    def request(
        self,
        method: str,
        url: str,
        *,
        service: str,
        operation: str,
        **kwargs,
    ):
        request = getattr(self.transport, method.lower())
        last_error: Exception | None = None
        attempts = max(1, int(self.max_attempts))
        for attempt in range(1, attempts + 1):
            try:
                response = request(url, **kwargs)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status is None or status == 429 or status >= 500
                if not retryable or attempt == attempts:
                    break
            except ValueError as exc:
                raise ServiceRequestError(
                    service, operation, f"invalid JSON response: {exc}"
                ) from exc
            delay = attempt
            self.emit(
                f"[WARNING] {service} {operation} failed "
                f"(attempt {attempt}/{attempts}); retrying in {delay}s"
            )
            if self.sleep is not None:
                self.sleep(delay)
        raise ServiceRequestError(service, operation, str(last_error)) from last_error
