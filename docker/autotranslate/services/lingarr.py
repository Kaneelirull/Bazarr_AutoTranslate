from __future__ import annotations

import random
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .bazarr import ServiceRequestError


ACCEPTED_CUE_KEYS = ("translatedSubtitle", "translatedLine", "translation", "text")


@dataclass(frozen=True)
class LingarrSourceLanguage:
    name: str
    code: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class LingarrActiveTranslation:
    media_id: int | None
    media_type: str
    status: str

    @property
    def media_key(self) -> tuple[int, str] | None:
        if self.media_id is None:
            return None
        return self.media_id, self.media_type.lower()


@dataclass
class LingarrClient:
    base_url: str
    headers: dict[str, str]
    request_json: Callable[..., Any]
    get: Callable[..., Any]
    post: Callable[..., Any]
    connect_timeout: int
    shutdown_requested: Callable[[], bool] = lambda: False
    emit: Callable[[str], None] = print

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url.rstrip('/')}/api/{endpoint}"

    def languages(self) -> list[LingarrSourceLanguage]:
        try:
            payload = self.request_json(
                "get", self._url("Translate/languages"),
                service="Lingarr", operation="fetch languages",
                headers=self.headers, timeout=self.connect_timeout,
            )
        except ServiceRequestError as exc:
            self.emit(f"[WARNING] Could not fetch Lingarr languages: {exc}")
            return []
        if not isinstance(payload, list):
            self.emit("[WARNING] Lingarr languages response has an unexpected schema")
            return []
        result = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                self.emit(f"[WARNING] Ignoring malformed Lingarr language entry at index {index}")
                continue
            name, code, targets = entry.get("name"), entry.get("code"), entry.get("targets", [])
            if (
                not isinstance(name, str) or not name.strip()
                or not isinstance(code, str) or not code.strip()
                or not isinstance(targets, list)
                or not all(isinstance(value, str) and value.strip() for value in targets)
            ):
                self.emit(f"[WARNING] Ignoring malformed Lingarr language entry at index {index}")
                continue
            result.append(LingarrSourceLanguage(
                name.strip(), code.strip(), tuple(value.strip() for value in targets)
            ))
        return result

    def active_translations(self) -> list[LingarrActiveTranslation]:
        payload = self.request_json(
            "get", self._url("TranslationRequest/active"),
            service="Lingarr", operation="fetch active translations",
            headers=self.headers, timeout=self.connect_timeout,
        )
        if not isinstance(payload, list):
            raise ServiceRequestError(
                "Lingarr", "fetch active translations", "unexpected response schema"
            )
        result = []
        for entry in payload:
            if not isinstance(entry, dict):
                raise ServiceRequestError(
                    "Lingarr", "fetch active translations", "malformed active entry"
                )
            media_id, media_type, status = (
                entry.get("mediaId"), entry.get("mediaType"), entry.get("status")
            )
            if (
                (media_id is not None and not isinstance(media_id, int))
                or not isinstance(media_type, str) or not media_type
                or not isinstance(status, str) or not status
            ):
                raise ServiceRequestError(
                    "Lingarr", "fetch active translations", "malformed active entry"
                )
            result.append(LingarrActiveTranslation(media_id, media_type, status))
        return result

    def media_cache(self) -> tuple[dict[int, int], dict[int, int]]:
        movies: dict[int, int] = {}
        episodes: dict[int, int] = {}
        for endpoint, page_size in (("Media/movies", 100), ("Media/shows", 50)):
            page = 1
            while not self.shutdown_requested():
                try:
                    response = self.get(
                        self._url(endpoint), headers=self.headers,
                        params={"pageNumber": page, "pageSize": page_size},
                        timeout=self.connect_timeout,
                    )
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError("response must be an object")
                    items = data.get("items", [])
                    if not isinstance(items, list):
                        raise ValueError("items must be a list")
                except Exception as exc:
                    self.emit(f"[ERROR] Lingarr {endpoint} page {page}: {exc}")
                    break
                for item in items:
                    if not isinstance(item, dict):
                        self.emit(
                            f"[WARNING] Ignoring malformed Lingarr {endpoint} item"
                        )
                        continue
                    if endpoint.endswith("movies"):
                        external, internal = item.get("radarrId"), item.get("id")
                        if external is not None and internal is not None:
                            movies[int(external)] = int(internal)
                    else:
                        seasons = item.get("seasons", []) or []
                        if not isinstance(seasons, list):
                            continue
                        for season in seasons:
                            if not isinstance(season, dict):
                                continue
                            episodes_in_season = season.get("episodes", []) or []
                            if not isinstance(episodes_in_season, list):
                                continue
                            for episode in episodes_in_season:
                                if not isinstance(episode, dict):
                                    continue
                                external, internal = episode.get("sonarrId"), episode.get("id")
                                if external is not None and internal is not None:
                                    episodes[int(external)] = int(internal)
                total = data.get("totalCount", 0)
                actual_size = data.get("pageSize", page_size) or page_size
                if page * actual_size >= total or not items:
                    break
                page += 1
        return episodes, movies

    def submit_file(self, body: dict) -> int | None:
        try:
            response = self.post(
                self._url("Translate/file"), headers=self.headers, json=body,
                timeout=self.connect_timeout,
            )
            response.raise_for_status()
            job_id = response.json().get("jobId")
            return int(job_id) if job_id is not None else None
        except Exception as exc:
            self.emit(f"[ERROR] Lingarr file submission failed: {exc}")
            return None

    def get_job(self, job_id: int) -> dict | None:
        try:
            response = self.get(
                self._url(f"TranslationRequest/{job_id}"),
                headers=self.headers, timeout=self.connect_timeout,
            )
            if response.status_code != 200:
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def cancel_job(self, job_id: int) -> bool:
        detail = self.get_job(job_id)
        if not detail:
            return False
        try:
            response = self.post(
                self._url("TranslationRequest/cancel"), headers=self.headers,
                json=detail, timeout=self.connect_timeout,
            )
            return response.status_code in (200, 202, 204)
        except Exception as exc:
            self.emit(f"[WARNING] Could not cancel Lingarr job {job_id}: {exc}")
            return False


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
    on_event: Callable[[dict[str, Any]], None] | None = None
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
        cancellation_requested: Callable[[], bool] | None = None,
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
            started = time.monotonic()
            status: int | None = None
            try:
                request = lambda: self.post(
                    f"{self.base_url.rstrip('/')}/api/Translate/line",
                    headers=self.headers, json=body, timeout=self.timeout,
                )
                if cancellation_requested is None:
                    response = request()
                else:
                    result: queue.Queue = queue.Queue(maxsize=1)

                    def run_request() -> None:
                        try:
                            result.put((request(), None))
                        except Exception as exc:
                            result.put((None, exc))

                    threading.Thread(
                        target=run_request,
                        name="lingarr-line-request",
                        daemon=True,
                    ).start()
                    while True:
                        if cancellation_requested():
                            raise ProviderResponseError(
                                "provider request cancelled",
                                retryable=True,
                            )
                        try:
                            response, request_error = result.get(timeout=0.1)
                            break
                        except queue.Empty:
                            continue
                    if request_error is not None:
                        raise request_error
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
                translated = parse_cue_response(payload)
                if self.on_event is not None:
                    self.on_event({
                        "classification": "success",
                        "retryable": False,
                        "status": response.status_code,
                        "payload": payload,
                        "duration": time.monotonic() - started,
                    })
                return translated
            except ProviderResponseError as exc:
                last_error = exc
                retryable = exc.retryable
                classification = (
                    "cancelled"
                    if "cancelled" in str(exc).casefold()
                    else "malformed_response"
                )
            except Exception as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status is None or status == 429 or status >= 500
                classification = (
                    "transport" if status is None
                    else "http_retryable" if retryable else "http_permanent"
                )
            if self.on_event is not None:
                self.on_event({
                    "classification": classification,
                    "retryable": retryable,
                    "status": status,
                    "duration": time.monotonic() - started,
                })
            if not retryable or attempt >= self.max_attempts:
                break
            self.sleep((0.5 * (2 ** (attempt - 1))) + self.random_value())
        raise ProviderResponseError(
            str(last_error or "provider request failed"),
            retryable=bool(getattr(last_error, "retryable", retryable)),
        )
