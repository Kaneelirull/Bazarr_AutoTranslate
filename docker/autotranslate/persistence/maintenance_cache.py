from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable

from .common import _path_key


class MaintenanceCacheRepositoryMixin:
    """SQLite-owned cache for incremental maintenance validation."""

    def maintenance_cache_entries(
        self, target_paths: Iterable[str | Path]
    ) -> dict[str, dict]:
        keys = list(dict.fromkeys(_path_key(path) for path in target_paths))
        if not keys:
            return {}
        entries: dict[str, dict] = {}
        with self._lock:
            for offset in range(0, len(keys), 500):
                batch = keys[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = self._connection.execute(
                    f"SELECT * FROM maintenance_validation_cache "
                    f"WHERE target_path IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    entries[str(row["target_path"])] = {
                        "targetPath": str(row["target_path"]),
                        "targetSize": int(row["target_size"]),
                        "targetModifiedNs": int(row["target_modified_ns"]),
                        "dependencyFingerprint": json.loads(
                            row["dependency_fingerprint_json"]
                        ),
                        "validatorVersion": str(row["validator_version"]),
                        "configFingerprint": str(row["config_fingerprint"]),
                        "validationResult": str(row["validation_result"]),
                        "actionResult": str(row["action_result"]),
                        "targetHash": row["target_hash"],
                        "details": json.loads(row["details_json"]),
                        "updatedAt": float(row["updated_at"]),
                    }
        return entries

    def upsert_maintenance_cache_entries(self, entries: Iterable[dict]) -> int:
        values = []
        now = time.time()
        for entry in entries:
            values.append((
                _path_key(entry["targetPath"]),
                int(entry["targetSize"]),
                int(entry["targetModifiedNs"]),
                json.dumps(entry.get("dependencyFingerprint") or {}, sort_keys=True),
                str(entry["validatorVersion"]),
                str(entry["configFingerprint"]),
                str(entry["validationResult"]),
                str(entry["actionResult"]),
                entry.get("targetHash"),
                json.dumps(entry.get("details") or {}, sort_keys=True),
                float(entry.get("updatedAt", now)),
            ))
        if not values:
            return 0
        with self._transaction() as db:
            db.executemany(
                """
                INSERT INTO maintenance_validation_cache(
                    target_path, target_size, target_modified_ns,
                    dependency_fingerprint_json, validator_version,
                    config_fingerprint, validation_result, action_result,
                    target_hash, details_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_path) DO UPDATE SET
                    target_size=excluded.target_size,
                    target_modified_ns=excluded.target_modified_ns,
                    dependency_fingerprint_json=excluded.dependency_fingerprint_json,
                    validator_version=excluded.validator_version,
                    config_fingerprint=excluded.config_fingerprint,
                    validation_result=excluded.validation_result,
                    action_result=excluded.action_result,
                    target_hash=excluded.target_hash,
                    details_json=excluded.details_json,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        return len(values)

    def delete_maintenance_cache_entries(
        self, target_paths: Iterable[str | Path]
    ) -> int:
        keys = list(dict.fromkeys(_path_key(path) for path in target_paths))
        if not keys:
            return 0
        removed = 0
        with self._transaction() as db:
            for offset in range(0, len(keys), 500):
                batch = keys[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                cursor = db.execute(
                    f"DELETE FROM maintenance_validation_cache "
                    f"WHERE target_path IN ({placeholders})",
                    batch,
                )
                removed += max(0, int(cursor.rowcount))
        return removed
