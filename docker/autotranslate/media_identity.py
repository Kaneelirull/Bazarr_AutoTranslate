from __future__ import annotations

import hashlib
import re
from pathlib import Path


SEASON_FOLDER_RE = re.compile(r"(?i)^season\s*\d{1,3}(?:\s+s\d{1,3}e\d{1,3})?$")
EPISODE_RE = re.compile(r"(?i)\bS(\d{1,3})E(\d{1,3})\b")
AIRDATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
LANG_SUFFIX_RE = re.compile(
    r"(?i)(?:[._-](?:eng|en|est|et|swe|sv))(?:[._-](?:hi|sdh|forced))?$"
)
RELEASE_RE = re.compile(
    r"(?i)\b(?:WEB(?:DL)?|BluRay|REMUX|HDTV|2160p|1080p|720p|480p|x26[45]|h26[45])\b.*$"
)


def _basename(value: object) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()


def clean_public_title(value: object) -> str | None:
    title = _basename(value)
    if (
        not title
        or SEASON_FOLDER_RE.fullmatch(title)
        or "/" in str(value or "")
        or "\\" in str(value or "")
    ):
        return None
    if title.casefold() in {"unknown", "episodes", "episode", "season"}:
        return None
    title = re.sub(r"\s+", " ", title).strip(" ._-")
    title = re.sub(r"(?i)\s*\[(?:tvdb|tmdb|imdb)id-[^\]]+]\s*$", "", title)
    return title or None


def series_title_from_path(path: str | Path | None) -> str | None:
    if not path:
        return None
    normalized = str(path).replace("\\", "/")
    parts = [part.strip() for part in normalized.split("/") if part.strip()]
    if len(parts) > 1:
        parent = parts[-2]
        if SEASON_FOLDER_RE.fullmatch(parent) and len(parts) > 2:
            return clean_public_title(parts[-3])

    stem = _basename(path)
    stem = re.sub(r"(?i)\.srt(?:season\s*\d+)?$", "", stem)
    stem = LANG_SUFFIX_RE.sub("", stem)
    match = EPISODE_RE.search(stem) or AIRDATE_RE.search(stem)
    if match:
        stem = stem[:match.start()].rstrip(" ._-")
    stem = re.sub(r"\[[^\]]*]", " ", stem)
    stem = RELEASE_RE.sub("", stem)
    return clean_public_title(stem)


def resolve_media_identity(
    item: dict,
    item_type: str,
    item_id: int,
    path: str | Path | None = None,
) -> dict:
    if item_type != "episodes":
        title = clean_public_title(item.get("title")) or f"Movie {item_id}"
        return {"key": f"movies:{title.casefold()}", "title": title}

    raw_title = item.get("seriesTitle") or item.get("series_title")
    title = clean_public_title(raw_title) or series_title_from_path(path)
    series_id = item.get("sonarrSeriesId") or item.get("sonarr_series_id")
    if series_id is not None:
        return {
            "key": f"sonarr:{series_id}",
            "title": title or f"Series {series_id}",
        }
    if title:
        digest = hashlib.sha256(title.casefold().encode("utf-8")).hexdigest()[:16]
        return {"key": f"episodes:title:{digest}", "title": title}
    return {"key": f"episode:{int(item_id)}", "title": f"Episode {int(item_id)}"}


def retry_media_identity(plan: dict) -> dict:
    item_type = str(plan.get("itemType") or "media")
    item_id = plan.get("itemId")
    raw_series = clean_public_title(plan.get("seriesTitle"))
    media = _basename(plan.get("sourcePath") or plan.get("mediaTitle"))
    stem = re.sub(r"(?i)\.srt(?:season\s*\d+)?$", "", media)
    stem = LANG_SUFFIX_RE.sub("", stem)
    stem = re.sub(r"\[[^\]]*]", " ", stem)
    stem = RELEASE_RE.sub("", stem).strip(" ._-")

    episode = EPISODE_RE.search(stem)
    airdate = AIRDATE_RE.search(stem)
    recovered_series = series_title_from_path(
        plan.get("sourcePath") or plan.get("mediaTitle")
    )
    title = raw_series or recovered_series
    detail = ""
    if episode:
        title = title or clean_public_title(stem[:episode.start()])
        trailing = stem[episode.end():].strip(" ._-")
        trailing = re.sub(r"\s+-[A-Z0-9]{2,12}$", "", trailing).strip()
        episode_code = (
            f"S{int(episode.group(1)):02d}E{int(episode.group(2)):02d}"
        )
        episode_title = trailing or None
    elif airdate:
        title = title or clean_public_title(stem[:airdate.start()])
        trailing = stem[airdate.end():].strip(" ._-")
        episode_code = airdate.group(0)
        episode_title = trailing or None
    else:
        title = title or clean_public_title(stem)
        episode_code = None
        episode_title = None
    return {
        "displayTitle": title or f"{item_type.rstrip('s').title()} {item_id or '-'}",
        "episodeCode": episode_code if episode or airdate else None,
        "episodeTitle": episode_title if episode or airdate else None,
    }
