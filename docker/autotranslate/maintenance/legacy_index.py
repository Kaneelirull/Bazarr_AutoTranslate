from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unreadable_identity(path: Path) -> str:
    normalized = str(path.resolve(strict=False)).replace("\\", "/").casefold()
    return "unreadable:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class LegacyQuarantineIndexer:
    state: Any
    root: Path
    inspect_artifact: Callable[[Path, dict, dict], dict]
    shutdown_requested: Callable[[], bool]
    progress: Callable[[dict], None] | None = None

    def run(self) -> dict[str, int]:
        stats = {"discovered": 0, "indexed": 0, "unresolved": 0, "skipped": 0}
        if not self.root.exists():
            return stats
        artifacts = sorted(
            path for path in self.root.rglob("*.srt")
            if ".input" not in path.stem
        )
        stats["discovered"] = len(artifacts)
        for position, artifact in enumerate(artifacts, start=1):
            if self.shutdown_requested():
                break
            try:
                artifact_hash = _sha256(artifact)
            except OSError:
                artifact_hash = _unreadable_identity(artifact)
                if self.state.legacy_quarantine_entry(
                    artifact, artifact_hash
                ) is not None:
                    stats["skipped"] += 1
                    continue
                self.state.record_legacy_quarantine_entry(
                    artifact_path=artifact,
                    artifact_hash=artifact_hash,
                    state="unresolved",
                    reason_code="artifact_unavailable",
                )
                stats["unresolved"] += 1
                continue
            if self.state.legacy_quarantine_entry(
                artifact, artifact_hash
            ) is not None:
                stats["skipped"] += 1
                continue
            report_path = Path(f"{artifact}.validation.json")
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                self.state.record_legacy_quarantine_entry(
                    artifact_path=artifact, artifact_hash=artifact_hash,
                    state="unresolved", reason_code="artifact_report_unavailable",
                )
                stats["unresolved"] += 1
                continue
            source_path = report.get("sourcePath")
            target_path = report.get("targetPath")
            source_hash = report.get("sourceHash")
            target_language = report.get("targetLanguage")
            if not all(isinstance(value, str) and value for value in (
                source_path, target_path, source_hash, target_language
            )):
                reason = "media_identity_unresolved"
            elif report.get("targetHash") != artifact_hash:
                reason = "hash_mismatch"
            else:
                try:
                    current_source_hash = _sha256(Path(source_path))
                except OSError:
                    current_source_hash = None
                reason = None if current_source_hash == source_hash else "source_hash_mismatch"
            identity = None
            if reason is None:
                identity = self.state.resolve_legacy_media(
                    source_path=source_path, target_path=target_path,
                    target_language=target_language, source_hash=source_hash,
                )
                if identity is None:
                    reason = "media_identity_unresolved"
            if reason is not None:
                self.state.record_legacy_quarantine_entry(
                    artifact_path=artifact, artifact_hash=artifact_hash,
                    state="unresolved", reason_code=reason,
                )
                stats["unresolved"] += 1
                continue
            try:
                result = self.inspect_artifact(artifact, report, identity)
            except Exception:
                result = {"accepted": False, "reasonCode": "current_validation_failed"}
            if result.get("accepted"):
                self.state.record_legacy_quarantine_entry(
                    artifact_path=artifact, artifact_hash=artifact_hash,
                    state="indexed",
                    quarantine_attempt_id=result.get("quarantineAttemptId"),
                    partial_candidate_id=result.get("partialCandidateId"),
                )
                stats["indexed"] += 1
            else:
                self.state.record_legacy_quarantine_entry(
                    artifact_path=artifact, artifact_hash=artifact_hash,
                    state="unresolved",
                    reason_code=result.get("reasonCode") or "current_validation_failed",
                )
                stats["unresolved"] += 1
            if self.progress is not None:
                self.progress({**stats, "position": position})
        return stats
