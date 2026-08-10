from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    """Raised when environment configuration is invalid."""


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} environment variable is required")
    return value


def _url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    return normalized if normalized.startswith(("http://", "https://")) else f"http://{normalized}"


def _integer(values: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(values.get(name, str(default))))
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Config:
    bazarr_url: str
    bazarr_api_key: str
    lingarr_url: str
    lingarr_api_key: str
    languages: tuple[str, ...]
    parallel_translates: int
    check_interval: int
    connect_timeout: int
    poll_interval: int
    poll_timeout: int
    repair_shutdown_grace_seconds: int
    state_dir: Path
    quarantine_dir: Path
    log_dir: Path

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if values is None else values
        languages = tuple(
            language.strip().lower()
            for language in env.get("LANGUAGES", "en,et,sv").split(",")
            if language.strip()
        )
        if not languages:
            raise ConfigError("LANGUAGES must contain at least one language code")
        state_dir = Path(env.get("STATE_DIR", "/config").strip() or "/config")
        return cls(
            bazarr_url=_url(_required(env, "BAZARR_URL")),
            bazarr_api_key=_required(env, "BAZARR_API_KEY"),
            lingarr_url=_url(_required(env, "LINGARR_URL")),
            lingarr_api_key=env.get("LINGARR_API_KEY", "").strip(),
            languages=languages,
            parallel_translates=_integer(env, "PARALLEL_TRANSLATES", 1, 1),
            check_interval=_integer(env, "CHECK_INTERVAL", 1200, 10),
            connect_timeout=_integer(env, "CONNECT_TIMEOUT", 10, 5),
            poll_interval=_integer(env, "POLL_INTERVAL", 20, 5),
            poll_timeout=_integer(env, "POLL_TIMEOUT", 900, 30),
            repair_shutdown_grace_seconds=_integer(
                env, "REPAIR_SHUTDOWN_GRACE_SECONDS", 30, 1
            ),
            state_dir=state_dir,
            quarantine_dir=Path(
                env.get("CLEANUP_QUARANTINE_DIR", f"{state_dir}/quarantine")
            ),
            log_dir=Path(env.get("LOG_DIR", "/var/log/bazarr-autotranslate")),
        )
