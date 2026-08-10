from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence


ACCEPTED_CUE_KEYS = ("translatedSubtitle", "translatedLine", "translation", "text")


class ProviderResponseError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, shape: dict[str, str] | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.shape = shape or {}


def response_shape(payload: Any) -> dict[str, str]:
    if isinstance(payload, dict):
        return {str(key)[:80]: type(value).__name__ for key, value in payload.items()}
    return {"$": type(payload).__name__}


def parse_cue_response(payload: Any) -> str:
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if isinstance(payload, dict):
        for key in ACCEPTED_CUE_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise ProviderResponseError(
        "unexpected response shape", retryable=True, shape=response_shape(payload)
    )


@dataclass
class LingarrProvider:
    base_url: str
    headers: dict[str, str]
    post: Callable[..., Any]
    timeout: int = 120
    max_attempts: int = 3
    sleep: Callable[[float], None] = time.sleep
    random_value: Callable[[], float] = random.random
    name: str = "lingarr"

    def translate_cue(
        self,
        source_text: str,
        source_language: str,
        target_language: str,
        context_before: Sequence[str] = (),
        context_after: Sequence[str] = (),
        *,
        strict: bool = False,
    ) -> str:
        body = {
            "subtitleLine": source_text,
            "sourceLanguage": source_language,
            "targetLanguage": target_language,
            "contextLinesBefore": [] if strict else list(context_before),
            "contextLinesAfter": [] if strict else list(context_after),
        }
        if strict:
            body["instructions"] = (
                "Return only the translated subtitle cue in the requested target "
                "language and script. Do not add commentary or surrounding dialogue."
            )
        last_error: Exception | None = None
        for attempt in range(1, max(1, self.max_attempts) + 1):
            try:
                response = self.post(
                    f"{self.base_url.rstrip('/')}/api/Translate/line",
                    headers=self.headers,
                    json=body,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
                return parse_cue_response(payload)
            except ProviderResponseError as exc:
                last_error = exc
                retryable = exc.retryable
            except Exception as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status is None or status == 429 or status >= 500
            if not retryable or attempt >= self.max_attempts:
                break
            self.sleep((0.5 * (2 ** (attempt - 1))) + self.random_value())
        raise ProviderResponseError(str(last_error or "provider request failed"), retryable=False)
