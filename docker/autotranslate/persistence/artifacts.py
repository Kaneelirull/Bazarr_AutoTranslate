from __future__ import annotations

import json
import os
import shutil
import sqlite3
import statistics
import threading
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .common import ACTIVE_RETRY_STATES, SCHEMA_VERSION, StateStoreError, _path_key, _utc_iso

class ArtifactsRepositoryMixin:
    @staticmethod
    def _quarantine_attempt_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "itemType": row["item_type"],
            "itemId": int(row["item_id"]),
            "targetLanguage": row["target_language"],
            "sourceHash": row["source_hash"],
            "targetHash": row["target_hash"],
            "attemptNumber": int(row["attempt_number"]),
            "validatorFingerprint": row["validator_fingerprint"],
            "configFingerprint": row["config_fingerprint"],
            "artifactPath": row["artifact_path"],
            "reportPath": row["report_path"],
            "failureRules": json.loads(row["failure_rules_json"]),
            "repairProvenance": json.loads(row["repair_provenance_json"]),
            "donorProvenance": json.loads(row["donor_provenance_json"]),
            "cueSignatures": json.loads(row["cue_signatures_json"]),
            "createdAt": float(row["created_at"]),
        }

    def record_quarantine_attempt(
        self,
        *,
        item_type: str,
        item_id: int,
        target_language: str,
        source_hash: str,
        target_hash: str,
        attempt_number: int,
        artifact_path: str | Path | None,
        report_path: str | Path | None,
        failure_rules: Iterable[str],
        cue_signatures: list[dict],
        repair_provenance: list[dict] | None = None,
        donor_provenance: list[dict] | None = None,
        created_at: float | None = None,
    ) -> dict:
        """Append one immutable failed-output record for later donor recovery."""
        timestamp = time.time() if created_at is None else float(created_at)
        values = (
            str(item_type), int(item_id), str(target_language).lower(),
            str(source_hash), str(target_hash), max(1, int(attempt_number)),
            self.validator_version, self.config_fingerprint,
            _path_key(artifact_path), _path_key(report_path),
            json.dumps(sorted({str(rule) for rule in failure_rules if rule})),
            json.dumps(repair_provenance or [], sort_keys=True),
            json.dumps(donor_provenance or [], sort_keys=True),
            json.dumps(cue_signatures, sort_keys=True), timestamp,
        )
        with self._transaction() as db:
            row = self._record_quarantine_attempt_in(db, values)
        return self._quarantine_attempt_dict(row)

    @staticmethod
    def _record_quarantine_attempt_in(
        db: sqlite3.Connection, values: tuple
    ) -> sqlite3.Row:
        db.execute(
            """
            INSERT OR IGNORE INTO quarantine_attempts(
                item_type, item_id, target_language, source_hash, target_hash,
                attempt_number, validator_fingerprint, config_fingerprint,
                artifact_path, report_path, failure_rules_json,
                repair_provenance_json, donor_provenance_json,
                cue_signatures_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return db.execute(
            """
            SELECT * FROM quarantine_attempts
            WHERE item_type=? AND item_id=? AND target_language=?
              AND source_hash=? AND target_hash=? AND attempt_number=?
            """,
            values[:6],
        ).fetchone()

    def finalize_quarantine_operation(
        self,
        artifact_id: int,
        *,
        attempt: dict | None = None,
        partial_candidate_id: int | None = None,
    ) -> dict | None:
        """Atomically finalize donor metadata and the artifact disposition."""
        attempt_row = None
        with self._transaction() as db:
            if attempt is not None:
                values = self._quarantine_attempt_values(attempt)
                attempt_row = self._record_quarantine_attempt_in(db, values)
                if partial_candidate_id is not None:
                    partial_cursor = db.execute(
                        """
                        UPDATE partial_candidates
                        SET artifact_path=?, quarantine_attempt_id=?
                        WHERE id=?
                        """,
                        (
                            values[8], int(attempt_row["id"]),
                            int(partial_candidate_id),
                        ),
                    )
                    if partial_cursor.rowcount != 1:
                        raise StateStoreError(
                            f"partial candidate {partial_candidate_id} no longer exists"
                        )
            cursor = db.execute(
                """
                UPDATE subtitle_artifacts
                SET disposition='quarantined', pending_destination=NULL,
                    pending_metadata_json=NULL, updated_at=?
                WHERE id=? AND disposition='quarantine_pending'
                """,
                (_utc_iso(), int(artifact_id)),
            )
            if cursor.rowcount != 1:
                raise StateStoreError(
                    f"artifact {artifact_id} is not pending quarantine"
                )
        return self._quarantine_attempt_dict(attempt_row)

    def record_pending_quarantine_hold(
        self,
        artifact_id: int,
        *,
        identity: str,
        target_path: str | Path,
        target_hash: str,
        target_language: str,
        rules: Iterable[str],
        origin: str | None,
        now: datetime | None = None,
    ) -> tuple[dict, bool, dict]:
        """Record one hold occurrence per pending artifact, idempotently."""
        timestamp = now or datetime.now(timezone.utc)
        with self._transaction() as db:
            row = db.execute(
                """
                SELECT disposition, pending_metadata_json
                FROM subtitle_artifacts WHERE id=?
                """,
                (int(artifact_id),),
            ).fetchone()
            if row is None or row["disposition"] != "quarantine_pending":
                raise StateStoreError(
                    f"artifact {artifact_id} is not pending quarantine"
                )
            try:
                metadata = json.loads(row["pending_metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            event, repeated, metadata = self._record_pending_quarantine_hold_in(
                db,
                int(artifact_id),
                metadata,
                identity=str(identity),
                target_path=target_path,
                target_hash=str(target_hash),
                target_language=str(target_language),
                rules=rules,
                origin=origin,
                timestamp=timestamp,
            )
        return event, repeated, metadata

    def _record_pending_quarantine_hold_in(
        self,
        db: sqlite3.Connection,
        artifact_id: int,
        metadata: dict,
        *,
        identity: str,
        target_path: str | Path,
        target_hash: str,
        target_language: str,
        rules: Iterable[str],
        origin: str | None,
        timestamp: datetime,
    ) -> tuple[dict, bool, dict]:
        if metadata.get("holdRecorded") and isinstance(
            metadata.get("quarantineEvent"), dict
        ):
            return (
                dict(metadata["quarantineEvent"]),
                bool(metadata.get("holdRepeated")),
                metadata,
            )
        previous = db.execute(
            "SELECT * FROM quarantine_holds WHERE identity=? AND target_hash=?",
            (identity, target_hash),
        ).fetchone()
        repeated = previous is not None
        first_seen = previous["first_seen"] if previous else timestamp.isoformat()
        occurrences = int(previous["occurrences"]) + 1 if previous else 1
        rule_values = sorted({str(rule) for rule in rules if rule})
        db.execute(
            """
            INSERT INTO quarantine_holds(
                identity, target_hash, target_path, target_language,
                rules_json, origin, first_seen, last_seen, hold_until,
                occurrences
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity, target_hash) DO UPDATE SET
                target_path=excluded.target_path,
                target_language=excluded.target_language,
                rules_json=excluded.rules_json,
                origin=excluded.origin,
                last_seen=excluded.last_seen,
                hold_until=excluded.hold_until,
                resolved_at=NULL,
                occurrences=excluded.occurrences
            """,
            (
                identity, target_hash, _path_key(target_path), target_language,
                json.dumps(rule_values), origin or "unknown", first_seen,
                timestamp.isoformat(), timestamp.isoformat(), occurrences,
            ),
        )
        event = {
            "identity": identity,
            "targetPath": str(target_path),
            "targetHash": target_hash,
            "targetLanguage": target_language,
            "rules": rule_values,
            "origin": origin or "unknown",
            "firstSeen": first_seen,
            "lastSeen": timestamp.isoformat(),
            "occurrences": occurrences,
            "resolvedAt": None,
        }
        metadata = dict(metadata)
        metadata.update({
            "holdRecorded": True,
            "holdRepeated": repeated,
            "quarantineEvent": event,
        })
        db.execute(
            """
            UPDATE subtitle_artifacts
            SET pending_metadata_json=?, updated_at=?
            WHERE id=? AND disposition='quarantine_pending'
            """,
            (json.dumps(metadata, ensure_ascii=False), _utc_iso(), artifact_id),
        )
        return event, repeated, metadata

    def _quarantine_attempt_values(self, attempt: dict) -> tuple:
        return (
            str(attempt["item_type"]), int(attempt["item_id"]),
            str(attempt["target_language"]).lower(),
            str(attempt["source_hash"]), str(attempt["target_hash"]),
            max(1, int(attempt["attempt_number"])),
            self.validator_version, self.config_fingerprint,
            _path_key(attempt.get("artifact_path")),
            _path_key(attempt.get("report_path")),
            json.dumps(sorted({
                str(rule) for rule in attempt.get("failure_rules", []) if rule
            })),
            json.dumps(attempt.get("repair_provenance") or [], sort_keys=True),
            json.dumps(attempt.get("donor_provenance") or [], sort_keys=True),
            json.dumps(attempt.get("cue_signatures") or [], sort_keys=True),
            float(attempt.get("created_at") or time.time()),
        )

    def quarantine_attempts(
        self, item_type: str, item_id: int, target_language: str
    ) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM quarantine_attempts
                WHERE item_type=? AND item_id=? AND target_language=?
                ORDER BY attempt_number DESC, created_at DESC, id DESC
                """,
                (str(item_type), int(item_id), str(target_language).lower()),
            ).fetchall()
        return [self._quarantine_attempt_dict(row) for row in rows]

    def protected_artifact_paths(self) -> set[Path]:
        """Paths retention must preserve for nonterminal durable work."""
        queries = (
            """
            SELECT source_path AS path FROM repair_jobs
             WHERE state IN ('queued', 'active', 'persisted_for_restart')
            UNION SELECT target_path FROM repair_jobs
             WHERE state IN ('queued', 'active', 'persisted_for_restart')
            """,
            """
            SELECT source_path AS path FROM retry_plans
             WHERE state IN ('repair_retry_queued', 'regeneration_waiting',
                             'regeneration_queued', 'retry_in_progress')
            UNION SELECT target_path FROM retry_plans
             WHERE state IN ('repair_retry_queued', 'regeneration_waiting',
                             'regeneration_queued', 'retry_in_progress')
            UNION SELECT artifact_path FROM retry_plans
             WHERE state IN ('repair_retry_queued', 'regeneration_waiting',
                             'regeneration_queued', 'retry_in_progress')
            UNION SELECT report_path FROM retry_plans
             WHERE state IN ('repair_retry_queued', 'regeneration_waiting',
                             'regeneration_queued', 'retry_in_progress')
            """,
            """
            SELECT target_path AS path FROM subtitle_artifacts
             WHERE disposition LIKE '%_pending'
            UNION SELECT source_path FROM subtitle_artifacts
             WHERE disposition LIKE '%_pending'
            UNION SELECT pending_destination FROM subtitle_artifacts
             WHERE disposition LIKE '%_pending'
            """,
        )
        protected: set[Path] = set()
        with self._lock:
            for query in queries:
                for row in self._connection.execute(query).fetchall():
                    if row["path"]:
                        protected.add(Path(row["path"]))
            rows = self._connection.execute(
                """
                SELECT payload_json FROM repair_jobs
                WHERE state IN ('queued', 'active', 'persisted_for_restart')
                """
            ).fetchall()
            attempt_ids: set[int] = set()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                    attempt_ids.update(int(value) for value in payload.get("quarantineAttemptIds", []))
                except (TypeError, ValueError):
                    continue
            if attempt_ids:
                placeholders = ",".join("?" for _value in attempt_ids)
                for row in self._connection.execute(
                    f"SELECT artifact_path, report_path FROM quarantine_attempts WHERE id IN ({placeholders})",
                    tuple(sorted(attempt_ids)),
                ).fetchall():
                    for key in ("artifact_path", "report_path"):
                        if row[key]:
                            protected.add(Path(row[key]))
        return protected

    def record_artifact_version(
        self,
        target_path: str | Path,
        *,
        target_hash: str,
        source_path: str | Path | None,
        source_hash: str | None,
        source_language: str | None,
        target_language: str | None,
        origin: str,
        operation: str,
        parent_artifact_id: int | None = None,
        attempt_id: int | None = None,
        target_identity: str | None = None,
        target_variant: str | None = None,
        disposition: str = "active",
        pending_destination: str | None = None,
        pending_metadata: dict | None = None,
        item_type: str | None = None,
        item_id: int | None = None,
    ) -> int:
        now = _utc_iso()
        with self._transaction() as db:
            if parent_artifact_id is not None:
                parent = db.execute(
                    """
                    SELECT attempt_id, item_type, item_id
                    FROM subtitle_artifacts
                    WHERE id = ?
                    """,
                    (int(parent_artifact_id),),
                ).fetchone()
                if parent is not None:
                    attempt_id = (
                        attempt_id
                        if attempt_id is not None
                        else parent["attempt_id"]
                    )
                    item_type = item_type or parent["item_type"]
                    item_id = (
                        item_id if item_id is not None else parent["item_id"]
                    )
            cursor = db.execute(
                """
                INSERT INTO subtitle_artifacts(
                    attempt_id, parent_artifact_id, item_type, item_id,
                    target_identity,
                    target_path, target_language, target_variant, target_hash,
                    source_path, source_language, source_hash, origin,
                    operation, disposition, pending_destination,
                    pending_metadata_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    parent_artifact_id,
                    item_type,
                    int(item_id) if item_id is not None else None,
                    target_identity,
                    _path_key(target_path),
                    target_language,
                    target_variant,
                    target_hash,
                    _path_key(source_path),
                    source_language,
                    source_hash,
                    origin,
                    operation,
                    disposition,
                    _path_key(pending_destination),
                    (
                        json.dumps(pending_metadata, ensure_ascii=False)
                        if pending_metadata is not None else None
                    ),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def latest_artifact(
        self, target_path: str | Path, target_hash: str | None = None
    ) -> dict | None:
        path = _path_key(target_path)
        query = "SELECT * FROM subtitle_artifacts WHERE target_path = ?"
        params: list[object] = [path]
        if target_hash is not None:
            query += " AND target_hash = ?"
            params.append(target_hash)
        query += " ORDER BY id DESC LIMIT 1"
        row = self._fetchone(query, params)
        return dict(row) if row else None

    def set_artifact_disposition(
        self,
        artifact_id: int,
        disposition: str,
        *,
        pending_destination: str | Path | None = None,
        pending_metadata: dict | None = None,
    ) -> None:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE subtitle_artifacts
                SET disposition = ?, pending_destination = ?,
                    pending_metadata_json = CASE
                        WHEN ? LIKE '%_pending'
                        THEN COALESCE(?, pending_metadata_json)
                        ELSE NULL
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    disposition,
                    _path_key(pending_destination),
                    disposition,
                    (
                        json.dumps(pending_metadata, ensure_ascii=False)
                        if pending_metadata is not None else None
                    ),
                    _utc_iso(),
                    int(artifact_id),
                ),
            )
            if cursor.rowcount != 1:
                raise StateStoreError(f"artifact {artifact_id} no longer exists")

    def reconcile_pending_operations(self) -> dict[str, int]:
        stats = {"completed": 0, "abandoned": 0}
        with self._transaction() as db:
            rows = db.execute(
                """
                SELECT id, target_path, target_hash, target_identity,
                       target_language, origin, disposition,
                       pending_destination, pending_metadata_json
                FROM subtitle_artifacts
                WHERE disposition IN (
                    'replacement_pending', 'quarantine_pending',
                    'deletion_pending'
                )
                """
            ).fetchall()
            for row in rows:
                target_path = Path(row["target_path"])
                destination = (
                    Path(row["pending_destination"])
                    if row["pending_destination"] else None
                )
                try:
                    metadata = json.loads(row["pending_metadata_json"] or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                if row["disposition"] == "replacement_pending":
                    matches = (
                        target_path.exists()
                        and self._hash_file(target_path) == row["target_hash"]
                    )
                    disposition = "active" if matches else "abandoned"
                elif row["disposition"] == "quarantine_pending":
                    candidate_path = (
                        Path(metadata["candidatePath"])
                        if metadata.get("candidatePath") else None
                    )
                    input_destination = (
                        Path(metadata["inputDestination"])
                        if metadata.get("inputDestination") else None
                    )
                    if candidate_path is not None and destination is not None:
                        try:
                            if (
                                target_path.exists()
                                and input_destination is not None
                                and not input_destination.exists()
                            ):
                                input_destination.parent.mkdir(parents=True, exist_ok=True)
                                shutil.move(str(target_path), str(input_destination))
                            if candidate_path.exists() and not destination.exists():
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                shutil.move(str(candidate_path), str(destination))
                        except OSError:
                            stats["abandoned"] += 1
                            continue
                    elif destination is not None:
                        try:
                            if target_path.exists() and not destination.exists():
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                shutil.move(str(target_path), str(destination))
                        except OSError:
                            stats["abandoned"] += 1
                            continue
                    moved = bool(
                        destination is not None
                        and destination.exists()
                        and not target_path.exists()
                    )
                    destination_matches = bool(
                        moved
                        and self._hash_file(destination) == row["target_hash"]
                    )
                    if destination_matches:
                        db.execute("SAVEPOINT quarantine_resume")
                        try:
                            identity = (
                                metadata.get("holdIdentity")
                                or row["target_identity"]
                            )
                            language = row["target_language"]
                            if identity and language and row["target_hash"]:
                                event, repeated, metadata = (
                                    self._record_pending_quarantine_hold_in(
                                        db,
                                        int(row["id"]),
                                        metadata,
                                        identity=str(identity),
                                        target_path=row["target_path"],
                                        target_hash=str(row["target_hash"]),
                                        target_language=str(language),
                                        rules=metadata.get("rules", []),
                                        origin=row["origin"],
                                        timestamp=datetime.now(timezone.utc),
                                    )
                                )
                                audit = metadata.get("audit")
                                if isinstance(audit, dict):
                                    audit = dict(audit)
                                    audit["quarantineEvent"] = event
                                    audit["repeatOffender"] = repeated
                                    metadata["audit"] = audit
                            self._resume_quarantine_metadata_in(
                                db, metadata, destination
                            )
                            db.execute("RELEASE SAVEPOINT quarantine_resume")
                            disposition = "quarantined"
                        except (OSError, KeyError, TypeError, ValueError, sqlite3.Error):
                            db.execute("ROLLBACK TO SAVEPOINT quarantine_resume")
                            db.execute("RELEASE SAVEPOINT quarantine_resume")
                            stats["abandoned"] += 1
                            continue
                    else:
                        disposition = "quarantine_pending"
                else:
                    disposition = "deleted" if not target_path.exists() else "active"
                if disposition == "deleted":
                    rules = sorted({
                        str(rule) for rule in metadata.get("rules", []) if rule
                    })
                    identity = (
                        metadata.get("holdIdentity")
                        or row["target_identity"]
                    )
                    language = row["target_language"]
                    if identity and language and row["target_hash"]:
                        now = datetime.now(timezone.utc)
                        hold_until = now.isoformat()
                        db.execute(
                            """
                            INSERT INTO quarantine_holds(
                                identity, target_hash, target_path,
                                target_language, rules_json, origin,
                                first_seen, last_seen, hold_until, occurrences
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                            ON CONFLICT(identity, target_hash) DO UPDATE SET
                                last_seen = excluded.last_seen,
                                hold_until = excluded.hold_until,
                                resolved_at = NULL,
                                occurrences = quarantine_holds.occurrences + 1,
                                rules_json = excluded.rules_json
                            """,
                            (
                                identity,
                                row["target_hash"],
                                row["target_path"],
                                language,
                                json.dumps(rules),
                                row["origin"],
                                now.isoformat(),
                                now.isoformat(),
                                hold_until,
                            ),
                        )
                if disposition == "quarantine_pending":
                    stats["abandoned"] += 1
                    continue
                db.execute(
                    """
                    UPDATE subtitle_artifacts
                    SET disposition = ?, pending_destination = NULL,
                        pending_metadata_json = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (disposition, _utc_iso(), int(row["id"])),
                )
                stats[
                    "completed"
                    if disposition in ("active", "quarantined", "deleted")
                    else "abandoned"
                ] += 1
        return stats

    def _resume_quarantine_metadata_in(
        self,
        db: sqlite3.Connection,
        metadata: dict,
        destination: Path,
    ) -> None:
        attempt = metadata.get("quarantineAttempt")
        report_path = (
            Path(attempt["report_path"])
            if isinstance(attempt, dict) and attempt.get("report_path")
            else Path(f"{destination}.validation.json")
        )
        audit = metadata.get("audit")
        if isinstance(audit, dict):
            report_path.parent.mkdir(parents=True, exist_ok=True)
            temp_name = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="",
                    prefix=f".{report_path.name}.", suffix=".tmp",
                    dir=report_path.parent, delete=False,
                ) as report_file:
                    json.dump(audit, report_file, ensure_ascii=False, indent=2)
                    temp_name = report_file.name
                os.replace(temp_name, report_path)
                temp_name = None
            finally:
                if temp_name is not None:
                    try:
                        Path(temp_name).unlink()
                    except OSError:
                        pass
            written_audit = json.loads(report_path.read_text(encoding="utf-8"))
            expected_hash = (
                attempt.get("target_hash")
                if isinstance(attempt, dict)
                else audit.get("targetHash")
            )
            if expected_hash and written_audit.get("targetHash") != expected_hash:
                raise ValueError("quarantine report target hash mismatch")
        if not isinstance(attempt, dict):
            return
        attempt = dict(attempt)
        attempt["artifact_path"] = str(destination)
        attempt["report_path"] = str(report_path)
        attempt_row = self._record_quarantine_attempt_in(
            db, self._quarantine_attempt_values(attempt)
        )
        partial_id = metadata.get("partialCandidateId")
        if partial_id is None:
            return
        cursor = db.execute(
            """
            UPDATE partial_candidates
            SET artifact_path=?, quarantine_attempt_id=?
            WHERE id=?
            """,
            (
                _path_key(destination), int(attempt_row["id"]), int(partial_id),
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"partial candidate {partial_id} no longer exists")

    def matching_record(
        self,
        target_path: str | Path,
        target_hash: str,
        *,
        target_identity: str | None = None,
        target_language: str | None = None,
        target_variant: str | None = None,
    ) -> dict | None:
        path = _path_key(target_path)
        query = """
                SELECT a.*, v.validator_version, v.result, v.details_json,
                       v.validated_at, v.validation_mode
                FROM subtitle_artifacts a
                JOIN validation_results v ON v.artifact_id = a.id
                WHERE a.target_path = ? AND a.target_hash = ?
        """
        parameters: list[object] = [path, target_hash]
        if target_identity is not None:
            query += " AND a.target_identity = ?"
            parameters.append(target_identity)
        if target_language is not None:
            query += " AND a.target_language = ?"
            parameters.append(target_language)
        if target_variant is not None:
            query += " AND a.target_variant = ?"
            parameters.append(target_variant)
        query += " ORDER BY v.id DESC LIMIT 1"
        row = self._fetchone(query, parameters)
        if not row:
            return None
        return {
            "artifactId": int(row["id"]),
            "validatorVersion": str(row["validator_version"]),
            "sourceHash": row["source_hash"],
            "sourcePath": row["source_path"],
            "sourceLanguage": row["source_language"],
            "targetHash": row["target_hash"],
            "result": row["result"],
            "validatedAt": row["validated_at"],
            "origin": row["origin"],
            "validationMode": row["validation_mode"],
            "details": json.loads(row["details_json"]),
        }

    def matching_origin(
        self, target_path: str | Path, target_hash: str
    ) -> str | None:
        entry = self.matching_record(target_path, target_hash)
        return str(entry["origin"]) if entry and entry.get("origin") else None

    def is_unchanged_valid(
        self, target_path: str | Path, source_hash: str | None, target_hash: str
    ) -> bool:
        entry = self.matching_record(target_path, target_hash)
        return bool(
            entry
            and entry.get("validatorVersion") == self.validator_version
            and entry.get("result") in ("valid", "valid_with_warnings")
            and (
                (
                    entry.get("origin") == "lingarr"
                    and entry.get("sourceHash") == source_hash
                )
                or entry.get("origin") != "lingarr"
            )
        )

    def current_valid_details(
        self, target_path: str | Path, target_hash: str
    ) -> dict | None:
        entry = self.matching_record(target_path, target_hash)
        if (
            not entry
            or entry.get("validatorVersion") != self.validator_version
            or entry.get("result") not in ("valid", "valid_with_warnings")
        ):
            return None
        details = entry.get("details")
        return dict(details) if isinstance(details, dict) else {}

    def record_quarantine_event(
        self,
        identity: str,
        *,
        target_path: str | Path,
        target_hash: str,
        target_language: str,
        rules: Iterable[str],
        origin: str | None,
        now: datetime | None = None,
    ) -> tuple[dict, bool]:
        timestamp = now or datetime.now(timezone.utc)
        with self._transaction() as db:
            previous = db.execute(
                """
                SELECT * FROM quarantine_holds
                WHERE identity = ? AND target_hash = ?
                """,
                (str(identity), target_hash),
            ).fetchone()
            repeated = previous is not None
            first_seen = (
                previous["first_seen"] if previous else timestamp.isoformat()
            )
            occurrences = int(previous["occurrences"]) + 1 if previous else 1
            # Kept populated for compatibility with the legacy physical schema.
            # It is audit metadata only and is never consulted for eligibility.
            hold_until = timestamp.isoformat()
            rule_values = sorted({str(rule) for rule in rules if rule})
            db.execute(
                """
                INSERT INTO quarantine_holds(
                    identity, target_hash, target_path, target_language,
                    rules_json, origin, first_seen, last_seen, hold_until,
                    occurrences
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity, target_hash) DO UPDATE SET
                    target_path = excluded.target_path,
                    target_language = excluded.target_language,
                    rules_json = excluded.rules_json,
                    origin = excluded.origin,
                    last_seen = excluded.last_seen,
                    hold_until = excluded.hold_until,
                    resolved_at = NULL,
                    occurrences = excluded.occurrences
                """,
                (
                    str(identity),
                    target_hash,
                    _path_key(target_path),
                    target_language,
                    json.dumps(rule_values),
                    origin or "unknown",
                    first_seen,
                    timestamp.isoformat(),
                    hold_until,
                    occurrences,
                ),
            )
        return (
            {
                "identity": str(identity),
                "targetPath": str(target_path),
                "targetHash": target_hash,
                "targetLanguage": target_language,
                "rules": rule_values,
                "origin": origin or "unknown",
                "firstSeen": first_seen,
                "lastSeen": timestamp.isoformat(),
                "occurrences": occurrences,
                "resolvedAt": None,
            },
            repeated,
        )

    def quarantine_event(
        self,
        identity: str,
        *,
        target_hash: str | None = None,
    ) -> dict | None:
        query = "SELECT * FROM quarantine_holds WHERE identity = ?"
        params: list[object] = [str(identity)]
        if target_hash is not None:
            query += " AND target_hash = ?"
            params.append(target_hash)
        query += " ORDER BY last_seen DESC LIMIT 1"
        row = self._fetchone(query, params)
        if not row:
            return None
        return {
            "identity": row["identity"],
            "targetPath": row["target_path"],
            "targetHash": row["target_hash"],
            "targetLanguage": row["target_language"],
            "rules": json.loads(row["rules_json"]),
            "origin": row["origin"],
            "firstSeen": row["first_seen"],
            "lastSeen": row["last_seen"],
            "occurrences": int(row["occurrences"]),
            "resolvedAt": row["resolved_at"],
        }

    def resolve_quarantine_events(
        self,
        identity: str,
        *,
        target_hash: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        resolved_at = (now or datetime.now(timezone.utc)).isoformat()
        with self._transaction() as db:
            hash_clause = " AND target_hash = ?" if target_hash is not None else ""
            params = (
                (resolved_at, str(identity), str(target_hash))
                if target_hash is not None else (resolved_at, str(identity))
            )
            cursor = db.execute(
                f"""
                UPDATE quarantine_holds
                SET resolved_at = ?
                WHERE identity = ? AND resolved_at IS NULL
                {hash_clause}
                """,
                params,
            )
            return cursor.rowcount > 0

    def prune_older_than(
        self, retention_days: int, now: datetime | None = None
    ) -> int:
        timestamp = now or datetime.now(timezone.utc)
        cutoff = datetime.fromtimestamp(
            timestamp.timestamp() - retention_days * 86400, timezone.utc
        ).isoformat()
        cutoff_timestamp = timestamp.timestamp() - retention_days * 86400
        with self._transaction() as db:
            recovery_events = db.execute(
                "DELETE FROM recovery_stage_events WHERE created_at < ?",
                (cutoff_timestamp,),
            ).rowcount
            donor_events = db.execute(
                "DELETE FROM donor_events WHERE created_at < ?",
                (cutoff_timestamp,),
            ).rowcount
            provider_events = db.execute(
                "DELETE FROM provider_events WHERE created_at < ?",
                (cutoff_timestamp,),
            ).rowcount
            admission_events = db.execute(
                "DELETE FROM retry_admission_events WHERE created_at < ?",
                (cutoff_timestamp,),
            ).rowcount
            maintenance_runs = db.execute(
                """
                DELETE FROM maintenance_runs
                WHERE completed_at IS NOT NULL AND completed_at < ?
                """,
                (cutoff_timestamp,),
            ).rowcount
            repair_jobs = db.execute(
                """
                DELETE FROM repair_jobs
                WHERE updated_at < ? AND state IN ('completed', 'failed')
                """,
                (cutoff_timestamp,),
            ).rowcount
            failure_fingerprints = db.execute(
                """
                DELETE FROM failure_fingerprints AS fingerprint
                WHERE last_seen_at < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM retry_plans AS plan
                    WHERE plan.item_type=fingerprint.item_type
                      AND plan.item_id=fingerprint.item_id
                      AND plan.target_language=fingerprint.target_language
                      AND plan.source_hash=fingerprint.source_file_hash
                      AND plan.state IN (
                        'repair_retry_queued', 'regeneration_waiting',
                        'regeneration_queued', 'retry_in_progress'
                      )
                  )
                """,
                (cutoff_timestamp,),
            ).rowcount
            cue_recoveries = db.execute(
                """
                DELETE FROM cue_recoveries AS recovery
                WHERE recovery.created_at < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM partial_candidates AS candidate
                    JOIN retry_plans AS plan ON plan.id=candidate.retry_plan_id
                    WHERE candidate.id=recovery.partial_candidate_id
                      AND plan.state IN (
                        'repair_retry_queued', 'regeneration_waiting',
                        'regeneration_queued', 'retry_in_progress'
                      )
                  )
                """,
                (cutoff_timestamp,),
            ).rowcount
            partial_candidates = db.execute(
                """
                DELETE FROM partial_candidates AS candidate
                WHERE candidate.created_at < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM cue_recoveries
                    WHERE partial_candidate_id=candidate.id
                  )
                  AND (
                    candidate.retry_plan_id IS NULL OR NOT EXISTS (
                      SELECT 1 FROM retry_plans AS plan
                      WHERE plan.id=candidate.retry_plan_id
                        AND plan.state IN (
                          'repair_retry_queued', 'regeneration_waiting',
                          'regeneration_queued', 'retry_in_progress'
                        )
                    )
                  )
                """,
                (cutoff_timestamp,),
            ).rowcount
            validations = db.execute(
                "DELETE FROM validation_results WHERE validated_at < ?", (cutoff,)
            ).rowcount
            holds = db.execute(
                "DELETE FROM quarantine_holds WHERE last_seen < ?",
                (cutoff,),
            ).rowcount
            attempts = db.execute(
                """
                DELETE FROM translation_attempts
                WHERE cooldown_until < ?
                  AND status IN ('cleared', 'failed', 'legacy')
                  AND id NOT IN (
                    SELECT attempt_id FROM subtitle_artifacts
                    WHERE attempt_id IS NOT NULL
                  )
                """,
                (timestamp.timestamp(),),
            ).rowcount
            timings = db.execute(
                "DELETE FROM timing_samples WHERE created_at < ?",
                (cutoff_timestamp,),
            ).rowcount
        return int(
            recovery_events + donor_events + provider_events
            + admission_events + maintenance_runs + repair_jobs
            + failure_fingerprints + cue_recoveries
            + partial_candidates + validations + holds + attempts + timings
        )
