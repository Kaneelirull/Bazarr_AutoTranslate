from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


@dataclass(frozen=True)
class InventoryVideo:
    path: Path
    sidecars: tuple[Path, ...]


@dataclass
class MaintenanceInventory:
    videos: tuple[InventoryVideo, ...]
    sidecars: tuple[Path, ...]
    errors: tuple[str, ...] = ()
    directories_scanned: int = 0

    def __post_init__(self) -> None:
        self._videos_by_key = {path_key(entry.path): entry for entry in self.videos}
        self._video_by_sidecar = {
            path_key(sidecar): entry.path
            for entry in self.videos
            for sidecar in entry.sidecars
        }

    def sidecars_for(self, video: str | Path) -> tuple[Path, ...]:
        entry = self._videos_by_key.get(path_key(video))
        return entry.sidecars if entry is not None else ()

    def video_for_sidecar(self, sidecar: str | Path) -> Path | None:
        return self._video_by_sidecar.get(path_key(sidecar))


def _matching_video(sidecar: Path, videos: Iterable[Path]) -> Path | None:
    sidecar_stem = sidecar.stem.casefold()
    candidates = [
        video for video in videos
        if sidecar_stem == video.stem.casefold()
        or sidecar_stem.startswith(f"{video.stem.casefold()}.")
    ]
    return max(candidates, key=lambda video: len(video.stem), default=None)


def _inventory_from_directories(
    directories: Iterable[tuple[Path, tuple[str, ...]]],
    video_extensions: Iterable[str],
) -> MaintenanceInventory:
    directory_entries = list(directories)
    extensions = {str(value).casefold() for value in video_extensions}
    sidecars: dict[str, Path] = {}
    video_sidecars: dict[str, tuple[Path, list[Path]]] = {}
    for directory, names in directory_entries:
        paths = [directory / name for name in names]
        videos = sorted(
            (path for path in paths if path.suffix.casefold() in extensions),
            key=lambda path: path.name.casefold(),
        )
        subtitles = sorted(
            (path for path in paths if path.suffix.casefold() == ".srt"),
            key=lambda path: path.name.casefold(),
        )
        for video in videos:
            video_sidecars.setdefault(path_key(video), (video, []))
        for subtitle in subtitles:
            sidecars.setdefault(path_key(subtitle), subtitle)
            video = _matching_video(subtitle, videos)
            if video is not None:
                video_sidecars[path_key(video)][1].append(subtitle)

    entries = tuple(
        InventoryVideo(path=video, sidecars=tuple(paths))
        for video, paths in sorted(
            video_sidecars.values(), key=lambda item: str(item[0]).casefold()
        )
    )
    return MaintenanceInventory(
        videos=entries,
        sidecars=tuple(sorted(sidecars.values(), key=lambda path: str(path).casefold())),
        directories_scanned=len(directory_entries),
    )


def build_inventory(
    roots: Iterable[str | Path], video_extensions: Iterable[str],
) -> MaintenanceInventory:
    seen_directories: set[str] = set()
    directories: list[tuple[Path, tuple[str, ...]]] = []
    errors: list[str] = []
    ordered_roots = sorted(
        (Path(root) for root in roots),
        key=lambda path: (len(path.parts), str(path).casefold()),
    )

    def on_error(error: OSError) -> None:
        errors.append(str(error))

    covered_roots: list[str] = []
    for root in ordered_roots:
        if not root.exists():
            continue
        normalized_root = path_key(root)
        def is_covered(covered: str) -> bool:
            try:
                return os.path.commonpath((normalized_root, covered)) == covered
            except ValueError:
                return False

        if any(is_covered(covered) for covered in covered_roots):
            continue
        covered_roots.append(normalized_root)
        for current, child_names, file_names in os.walk(root, onerror=on_error):
            directory = Path(current)
            key = path_key(directory)
            if key in seen_directories:
                child_names[:] = []
                continue
            seen_directories.add(key)
            child_names.sort(key=str.casefold)
            directories.append((directory, tuple(sorted(file_names, key=str.casefold))))

    inventory = _inventory_from_directories(directories, video_extensions)
    inventory.errors = tuple(errors)
    return inventory


def build_scoped_inventory(
    videos: Iterable[str | Path], video_extensions: Iterable[str],
) -> MaintenanceInventory:
    requested = {path_key(video): Path(video) for video in videos}
    directories: list[tuple[Path, tuple[str, ...]]] = []
    errors: list[str] = []
    for directory in sorted(
        {video.parent for video in requested.values()}, key=lambda path: str(path).casefold()
    ):
        try:
            names = tuple(sorted((entry.name for entry in directory.iterdir() if entry.is_file()), key=str.casefold))
        except OSError as exc:
            errors.append(str(exc))
            names = ()
        directories.append((directory, names))
    inventory = _inventory_from_directories(directories, video_extensions)
    inventory.videos = tuple(
        entry for entry in inventory.videos if path_key(entry.path) in requested
    )
    inventory.sidecars = tuple(
        sidecar for entry in inventory.videos for sidecar in entry.sidecars
    )
    inventory.__post_init__()
    inventory.errors = tuple(errors)
    return inventory


__all__ = [
    "InventoryVideo", "MaintenanceInventory", "build_inventory",
    "build_scoped_inventory", "path_key",
]
