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
    get: Callable[..., Any] | None = None
    post: Callable[..., Any] | None = None
    patch: Callable[..., Any] | None = None
    connect_timeout: int = 10
    sync_start_timeout: int = 30
    sync_poll_interval: int = 5
    time_value: Callable[[], float] | None = None
    sleep: Callable[[float], None] | None = None
    shutdown_requested: Callable[[], bool] = lambda: False
    emit: Callable[[str], None] = print

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
            timeout=self.connect_timeout,
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
            timeout=self.connect_timeout,
        )
        data = payload.get("data", []) if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ServiceRequestError("Bazarr", "fetch subtitles", "unexpected response schema")
        if not data:
            return "", []
        if not isinstance(data[0], dict) or not isinstance(data[0].get("subtitles", []), list):
            raise ServiceRequestError("Bazarr", "fetch subtitles", "unexpected item schema")
        return str(data[0].get("path") or ""), data[0].get("subtitles", [])

    def trigger_sync(self, episodes: bool, movies: bool) -> None:
        if self.post is None:
            raise RuntimeError("Bazarr POST transport is not configured")
        tasks = []
        if episodes:
            tasks.append("series_full_scan_subtitles")
        if movies:
            tasks.append("movies_full_scan_subtitles")
        for task_id in tasks:
            try:
                response = self.post(
                    self._url("system/tasks"),
                    headers=self.headers,
                    params={"taskid": task_id},
                    timeout=self.connect_timeout,
                )
                if response.status_code == 204:
                    self.emit(f"[INFO] Triggered Bazarr task: {task_id}")
                else:
                    self.emit(
                        f"[WARNING] Bazarr task {task_id} returned "
                        f"{response.status_code}"
                    )
            except Exception as exc:
                self.emit(f"[ERROR] Failed to trigger Bazarr task {task_id}: {exc}")

    def episode_series_id(self, episode_id: int) -> int | None:
        payload = self.request_json(
            "get",
            self._url("episodes"),
            service="Bazarr",
            operation=f"resolve series for episode {int(episode_id)}",
            headers=self.headers,
            params={"episodeid[]": int(episode_id)},
            timeout=self.connect_timeout,
        )
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not rows or not isinstance(rows[0], dict):
            return None
        for key in ("sonarrSeriesId", "seriesId", "series_id"):
            try:
                value = int(rows[0].get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def trigger_item_scan(
        self,
        item_type: str,
        item_id: int,
        *,
        series_id: int | None = None,
    ) -> bool:
        if self.patch is None:
            raise RuntimeError("Bazarr PATCH transport is not configured")
        if item_type == "movies":
            endpoint, parameter, identifier = "movies", "radarrid", int(item_id)
        elif item_type == "episodes":
            resolved = series_id or self.episode_series_id(int(item_id))
            if resolved is None:
                return False
            endpoint, parameter, identifier = "series", "seriesid", int(resolved)
        else:
            return False
        response = self.patch(
            self._url(endpoint),
            headers=self.headers,
            params={parameter: identifier, "action": "scan-disk"},
            timeout=self.connect_timeout,
        )
        accepted = int(getattr(response, "status_code", 0)) == 204
        self.emit(
            f"[{'INFO' if accepted else 'WARNING'}] Bazarr item scan "
            f"{'accepted' if accepted else 'rejected'} for {endpoint}:{identifier}"
        )
        return accepted

    @staticmethod
    def _job_matches(job: dict, episodes: bool, movies: bool) -> bool:
        name = (job.get("job_name") or "").lower()
        status = (job.get("status") or "").lower()
        if status != "running":
            return False
        return bool(
            episodes and "episode" in name and "subtitle" in name
            or movies and "movie" in name and "subtitle" in name
            or episodes and "series" in name and "subtitle" in name
        )

    def wait_for_sync(self, episodes: bool, movies: bool, timeout: int) -> bool:
        if not episodes and not movies:
            return True
        if self.get is None:
            raise RuntimeError("Bazarr GET transport is not configured")
        now = self.time_value or __import__("time").time
        sleep = self.sleep or __import__("time").sleep
        self.emit(
            f"[INFO] Waiting for Bazarr subtitle scan to complete "
            f"(timeout {timeout}s)..."
        )
        deadline = now() + timeout
        start_deadline = min(deadline, now() + self.sync_start_timeout)
        logged_jobs: set[int] = set()
        observed_running = False
        while not self.shutdown_requested():
            try:
                response = self.get(
                    self._url("system/jobs"),
                    headers=self.headers,
                    timeout=self.connect_timeout,
                )
                response.raise_for_status()
                jobs = response.json().get("data", [])
            except Exception as exc:
                self.emit(f"[WARNING] Could not poll Bazarr jobs: {exc}")
                jobs = []
            active = [
                job for job in jobs
                if self._job_matches(job, episodes, movies)
            ]
            if not active:
                if observed_running:
                    self.emit("[OK] Bazarr subtitle scan completed")
                    return True
                if now() >= start_deadline:
                    self.emit(
                        "[WARNING] Bazarr subtitle scan did not appear within "
                        f"{self.sync_start_timeout}s"
                    )
                    return False
            else:
                observed_running = True
            for job in active:
                job_id = job.get("job_id")
                if job_id not in logged_jobs:
                    logged_jobs.add(job_id)
                    self.emit(
                        f"[INFO] Bazarr scan running: "
                        f"{job.get('job_name', 'unknown')}"
                    )
                if job.get("is_progress"):
                    self.emit(
                        f"[SYNC] {job.get('job_name')}: "
                        f"{job.get('progress_value', 0)}/"
                        f"{job.get('progress_max', 0)} — "
                        f"{job.get('progress_message', '')}"
                    )
            if now() >= deadline:
                self.emit(
                    f"[WARNING] Bazarr sync timed out after {timeout}s — "
                    "continuing anyway"
                )
                return False
            for _ in range(max(1, self.sync_poll_interval)):
                if self.shutdown_requested():
                    return False
                sleep(1)
        return False
