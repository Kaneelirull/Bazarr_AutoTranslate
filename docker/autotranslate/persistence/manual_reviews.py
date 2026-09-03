from __future__ import annotations

import json
import math
import time
import uuid

from .common import StateStoreError


class ManualReviewsRepositoryMixin:
    """Atomic persistence operations for operator-owned retry recovery."""

    @staticmethod
    def _manual_action_dict(row) -> dict:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError):
            details = {}
        return {
            "id": int(row["id"]),
            "retryPlanId": int(row["retry_plan_id"]),
            "action": row["action"],
            "outcome": row["outcome"],
            "reasonCode": row["reason_code"],
            "details": details if isinstance(details, dict) else {},
            "createdAt": float(row["created_at"]),
        }

    @staticmethod
    def _is_manual_hold(row) -> bool:
        return bool(
            row is not None
            and row["state"] == "regeneration_waiting"
            and row["last_deferral_class"] == "manual_review"
        )

    def manual_review_plan(self, plan_id: int) -> dict | None:
        row = self._fetchone("SELECT * FROM retry_plans WHERE id=?", (int(plan_id),))
        return self._retry_plan_dict(row)

    def manual_review_plans(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT plan.* FROM retry_plans AS plan
                WHERE plan.last_deferral_class='manual_review'
                   OR plan.state='manual_dismissed'
                   OR EXISTS (
                       SELECT 1 FROM manual_review_actions AS action
                       WHERE action.retry_plan_id=plan.id
                   )
                ORDER BY plan.updated_at DESC, plan.id DESC
                """
            ).fetchall()
        return [self._retry_plan_dict(row) for row in rows]

    @staticmethod
    def _manual_status_sql(alias: str = "plan") -> str:
        return f"""CASE
            WHEN {alias}.state='manual_dismissed' THEN 'dismissed'
            WHEN {alias}.state IN (
                'accepted_after_manual_recheck', 'accepted_after_retry',
                'accepted_after_donor_recovery', 'superseded'
            ) THEN 'resolved'
            WHEN {alias}.state='regeneration_waiting'
             AND {alias}.last_deferral_class='manual_review'
                THEN 'needs_attention'
            ELSE 'manually_queued' END"""

    def manual_review_page(self, query: dict) -> dict:
        status_sql = self._manual_status_sql()
        base = """FROM retry_plans AS plan
            WHERE (plan.last_deferral_class='manual_review'
               OR plan.state='manual_dismissed'
               OR EXISTS (SELECT 1 FROM manual_review_actions action
                          WHERE action.retry_plan_id=plan.id))"""
        clauses: list[str] = []
        params: list[object] = []
        search = str(query.get("q") or "").strip().casefold()
        if search:
            clauses.append(
                "LOWER(COALESCE(plan.series_title,'') || ' ' || "
                "COALESCE(plan.media_title,'') || ' ' || "
                "COALESCE(plan.target_language,'') || ' ' || "
                "COALESCE(plan.last_reason,'')) LIKE ?"
            )
            params.append(f"%{search}%")
        status = str(query.get("status") or "").strip()
        if status:
            clauses.append(f"({status_sql})=?")
            params.append(status)
        item_type = str(query.get("itemType") or "").strip()
        if item_type:
            clauses.append("plan.item_type=?")
            params.append(item_type)
        language = str(query.get("language") or "").strip().lower()
        if language:
            clauses.append("LOWER(plan.target_language)=?")
            params.append(language)
        filtered = base + (" AND " + " AND ".join(clauses) if clauses else "")
        sort_columns = {
            "updatedAt": "plan.updated_at", "media": "LOWER(COALESCE(plan.series_title, plan.media_title, ''))",
            "language": "LOWER(plan.target_language)", "attempts": "plan.attempt_count",
            "status": status_sql,
        }
        sort = str(query.get("sort") or "updatedAt")
        direction = str(query.get("direction") or "desc")
        if sort not in sort_columns or direction not in {"asc", "desc"}:
            raise ValueError("invalid manual review ordering")
        requested_page = max(1, int(query.get("page") or 1))
        page_size = max(1, min(100, int(query.get("pageSize") or 20)))
        with self._lock:
            total_row = self._connection.execute(
                f"SELECT COUNT(*) AS count {filtered}", params
            ).fetchone()
            total = int(total_row["count"] if total_row else 0)
            last_page = max(1, math.ceil(total / page_size))
            page = min(requested_page, last_page)
            rows = self._connection.execute(
                f"SELECT plan.* {filtered} ORDER BY {sort_columns[sort]} "
                f"{direction.upper()}, plan.id {direction.upper()} LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
            count_rows = self._connection.execute(
                f"SELECT {status_sql} AS status, COUNT(*) AS count {base} GROUP BY status"
            ).fetchall()
        counts = {
            "needs_attention": 0, "manually_queued": 0,
            "resolved": 0, "dismissed": 0,
        }
        counts.update({row["status"]: int(row["count"]) for row in count_rows})
        return {
            "plans": [self._retry_plan_dict(row) for row in rows],
            "counts": counts,
            "total": total,
            "page": page,
            "pageSize": page_size,
        }

    def manual_review_actions(self, plan_id: int, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM manual_review_actions
                WHERE retry_plan_id=? ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (int(plan_id), max(1, min(100, int(limit)))),
            ).fetchall()
        return [self._manual_action_dict(row) for row in rows]

    def manual_review_action_count(self, plan_id: int) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) AS count FROM manual_review_actions WHERE retry_plan_id=?",
            (int(plan_id),),
        )
        return int(row["count"] if row else 0)

    def manual_review_actions_for_plans(
        self, plan_ids: list[int], limit: int = 100
    ) -> tuple[dict[int, list[dict]], dict[int, int]]:
        ids = [int(plan_id) for plan_id in plan_ids]
        if not ids:
            return {}, {}
        placeholders = ",".join("?" for _ in ids)
        action_limit = max(1, min(100, int(limit)))
        with self._lock:
            rows = self._connection.execute(
                f"""SELECT * FROM (
                    SELECT action.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY retry_plan_id
                               ORDER BY created_at DESC, id DESC
                           ) AS row_number
                    FROM manual_review_actions action
                    WHERE retry_plan_id IN ({placeholders})
                ) WHERE row_number<=? ORDER BY retry_plan_id, created_at DESC, id DESC""",
                (*ids, action_limit),
            ).fetchall()
            count_rows = self._connection.execute(
                f"""SELECT retry_plan_id, COUNT(*) AS count
                    FROM manual_review_actions
                    WHERE retry_plan_id IN ({placeholders}) GROUP BY retry_plan_id""",
                ids,
            ).fetchall()
        actions: dict[int, list[dict]] = {plan_id: [] for plan_id in ids}
        for row in rows:
            actions[int(row["retry_plan_id"])].append(self._manual_action_dict(row))
        counts = {int(row["retry_plan_id"]): int(row["count"]) for row in count_rows}
        return actions, counts

    def manual_review_scan_states(self, plan_ids: list[int]) -> dict[int, dict]:
        ids = [int(plan_id) for plan_id in plan_ids]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._connection.execute(
                f"""SELECT retry_plan_id, state, attempt_count, next_attempt_at,
                           last_error_code, updated_at
                    FROM manual_review_scan_outbox
                    WHERE retry_plan_id IN ({placeholders})""",
                ids,
            ).fetchall()
        return {
            int(row["retry_plan_id"]): {
                "state": row["state"],
                "attemptCount": int(row["attempt_count"] or 0),
                "nextAttemptAt": float(row["next_attempt_at"] or 0),
                "lastErrorCode": row["last_error_code"],
                "updatedAt": float(row["updated_at"] or 0),
            }
            for row in rows
        }

    @staticmethod
    def _insert_manual_action(
        db, plan_id: int, action: str, outcome: str,
        reason_code: str | None, details: dict | None, timestamp: float,
    ) -> int:
        cursor = db.execute(
            """
            INSERT INTO manual_review_actions(
                retry_plan_id, action, outcome, reason_code,
                details_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                int(plan_id), str(action)[:40], str(outcome)[:40],
                str(reason_code)[:80] if reason_code else None,
                json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
                timestamp,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _manual_row_for_update(db, plan_id: int, expected_updated_at: float):
        row = db.execute("SELECT * FROM retry_plans WHERE id=?", (int(plan_id),)).fetchone()
        if row is None:
            raise LookupError("manual review not found")
        expected = float(expected_updated_at)
        if not math.isfinite(expected) or abs(float(row["updated_at"]) - expected) > 0.000001:
            raise RuntimeError("manual review changed")
        if not ManualReviewsRepositoryMixin._is_manual_hold(row):
            raise RuntimeError("manual review is no longer awaiting action")
        return row

    def queue_manual_retry(
        self, plan_id: int, expected_updated_at: float, completed_cycle: int
    ) -> dict:
        timestamp = time.time()
        with self._transaction() as db:
            original = self._manual_row_for_update(db, plan_id, expected_updated_at)
            db.execute(
                """
                UPDATE retry_plans SET state='regeneration_waiting',
                    eligible_completed_cycle=?, last_deferral_class=NULL,
                    final_outcome=NULL, last_reason='manually authorized retry',
                    claim_owner=NULL, claimed_at=NULL,
                    submission_attempt_id=NULL, updated_at=?
                WHERE id=?
                """,
                (max(0, int(completed_cycle)), timestamp, int(plan_id)),
            )
            db.execute("UPDATE subtitle_publications SET state='pending',failure_count=0,eligible_cycle=?,updated_at=? WHERE target_path=? AND state='manual_review'", (completed_cycle, timestamp, original['target_path']))
            db.execute("DELETE FROM recovery_review_holds WHERE retry_plan_id=?", (plan_id,))
            self._insert_manual_action(
                db, plan_id, "queue_retry", "queued", "operator_authorized",
                {"eligibleCompletedCycle": max(0, int(completed_cycle))}, timestamp,
            )
            row = db.execute("SELECT * FROM retry_plans WHERE id=?", (int(plan_id),)).fetchone()
        return self._retry_plan_dict(row)

    def dismiss_manual_review(
        self, plan_id: int, expected_updated_at: float
    ) -> dict:
        timestamp = time.time()
        with self._transaction() as db:
            original = self._manual_row_for_update(db, plan_id, expected_updated_at)
            db.execute(
                """
                UPDATE retry_plans SET state='manual_dismissed',
                    final_outcome='manual_dismissed',
                    last_deferral_class=NULL,
                    last_reason='dismissed by operator', claim_owner=NULL,
                    claimed_at=NULL, submission_attempt_id=NULL, updated_at=?
                WHERE id=?
                """,
                (timestamp, int(plan_id)),
            )
            db.execute("UPDATE subtitle_publications SET state='superseded',updated_at=? WHERE target_path=? AND state='manual_review'", (timestamp, original['target_path']))
            self._insert_manual_action(
                db, plan_id, "dismiss", "dismissed", "operator_dismissed",
                {}, timestamp,
            )
            row = db.execute("SELECT * FROM retry_plans WHERE id=?", (int(plan_id),)).fetchone()
        return self._retry_plan_dict(row)

    def record_manual_recheck(
        self,
        plan_id: int,
        expected_updated_at: float,
        *,
        valid: bool,
        reason_code: str,
        details: dict | None = None,
    ) -> dict:
        timestamp = time.time()
        with self._transaction() as db:
            self._manual_row_for_update(db, plan_id, expected_updated_at)
            if valid:
                raise ValueError("successful rechecks require resolve_manual_recheck")
            db.execute(
                """
                UPDATE retry_plans SET last_reason=?, updated_at=? WHERE id=?
                """,
                (str(reason_code)[:500], timestamp, int(plan_id)),
            )
            outcome = "invalid"
            self._insert_manual_action(
                db, plan_id, "recheck", outcome, reason_code, details, timestamp,
            )
            row = db.execute("SELECT * FROM retry_plans WHERE id=?", (int(plan_id),)).fetchone()
        return self._retry_plan_dict(row)

    def resolve_manual_recheck(
        self,
        plan_id: int,
        expected_updated_at: float,
        *,
        reason_code: str,
        details: dict,
        validation_record: dict,
        quarantine_identity: str | None,
        quarantine_hash: str | None,
    ) -> dict:
        """Atomically persist validation, resolve the exact hold, and queue a scan."""
        timestamp = time.time()
        scan_lease_owner = f"manual-scan:{uuid.uuid4().hex}"
        with self._transaction() as db:
            self._manual_row_for_update(db, plan_id, expected_updated_at)
            self.record(**validation_record)
            if quarantine_identity and quarantine_hash:
                resolved = self.resolve_quarantine_events(
                    quarantine_identity, target_hash=quarantine_hash
                )
                if not resolved:
                    raise StateStoreError("reviewed quarantine hold changed")
            cursor = db.execute(
                """
                UPDATE retry_plans SET state='accepted_after_manual_recheck',
                    final_outcome='accepted_after_manual_recheck',
                    last_deferral_class=NULL,
                    last_reason='restored file passed manual recheck',
                    claim_owner=NULL, claimed_at=NULL,
                    submission_attempt_id=NULL, updated_at=?
                WHERE id=? AND state='regeneration_waiting'
                  AND last_deferral_class='manual_review'
                """,
                (timestamp, int(plan_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("manual review changed")
            self._insert_manual_action(
                db, plan_id, "recheck", "resolved", reason_code, details, timestamp,
            )
            self._insert_manual_action(
                db, plan_id, "bazarr_scan", "pending", "awaiting_dispatch", {}, timestamp,
            )
            db.execute(
                """
                INSERT INTO manual_review_scan_outbox(
                    retry_plan_id, state, attempt_count, next_attempt_at,
                    lease_owner, lease_expires_at, last_error_code,
                    created_at, updated_at
                ) VALUES(?, 'claimed', 0, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(retry_plan_id) DO UPDATE SET
                    state='claimed', attempt_count=0, next_attempt_at=excluded.next_attempt_at,
                    lease_owner=excluded.lease_owner,
                    lease_expires_at=excluded.lease_expires_at,
                    last_error_code=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    int(plan_id), timestamp, scan_lease_owner, timestamp + 600,
                    timestamp, timestamp,
                ),
            )
            row = db.execute(
                "SELECT * FROM retry_plans WHERE id=?", (int(plan_id),)
            ).fetchone()
        result = self._retry_plan_dict(row)
        result["scanLeaseOwner"] = scan_lease_owner
        return result

    def record_manual_scan_outcome(
        self, plan_id: int, *, lease_owner: str, outcome: str,
        reason_code: str | None = None, now: float | None = None,
    ) -> int:
        if outcome not in {"dispatched", "failed"}:
            raise ValueError("unsupported scan outcome")
        timestamp = time.time() if now is None else float(now)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM manual_review_scan_outbox WHERE retry_plan_id=?",
                (int(plan_id),),
            ).fetchone()
            if row is None or row["state"] != "claimed" or row["lease_owner"] != str(lease_owner):
                raise RuntimeError("manual scan lease changed")
            attempts = int(row["attempt_count"] or 0) + 1
            if outcome == "dispatched":
                db.execute(
                    """UPDATE manual_review_scan_outbox
                       SET state='dispatched', attempt_count=?, lease_owner=NULL,
                           lease_expires_at=NULL, last_error_code=NULL, updated_at=?
                       WHERE retry_plan_id=?""",
                    (attempts, timestamp, int(plan_id)),
                )
            else:
                delay = min(21600, 300 * (2 ** min(6, max(0, attempts - 1))))
                db.execute(
                    """UPDATE manual_review_scan_outbox
                       SET state='pending', attempt_count=?, next_attempt_at=?,
                           lease_owner=NULL, lease_expires_at=NULL,
                           last_error_code=?, updated_at=? WHERE retry_plan_id=?""",
                    (
                        attempts, timestamp + delay,
                        str(reason_code or "scan_dispatch_failed")[:80],
                        timestamp, int(plan_id),
                    ),
                )
            return self._insert_manual_action(
                db, plan_id, "bazarr_scan", outcome, reason_code, {}, timestamp,
            )

    def claim_manual_scans(
        self, limit: int = 10, *, now: float | None = None,
        lease_seconds: int = 600, owner: str | None = None,
    ) -> list[dict]:
        timestamp = time.time() if now is None else float(now)
        lease_owner = owner or f"manual-scan:{uuid.uuid4().hex}"
        with self._transaction() as db:
            db.execute(
                """UPDATE manual_review_scan_outbox
                   SET state='pending', lease_owner=NULL, lease_expires_at=NULL,
                       next_attempt_at=MIN(next_attempt_at, ?), updated_at=?
                   WHERE state='claimed' AND lease_expires_at<=?""",
                (timestamp, timestamp, timestamp),
            )
            rows = db.execute(
                """
                SELECT plan.* FROM manual_review_scan_outbox outbox
                JOIN retry_plans plan ON plan.id=outbox.retry_plan_id
                WHERE plan.state='accepted_after_manual_recheck'
                  AND outbox.state='pending' AND outbox.next_attempt_at<=?
                ORDER BY outbox.attempt_count, outbox.next_attempt_at,
                         outbox.retry_plan_id LIMIT ?
                """,
                (timestamp, max(1, min(10, int(limit)))),
            ).fetchall()
            plan_ids = [int(row["id"]) for row in rows]
            for plan_id in plan_ids:
                db.execute(
                    """UPDATE manual_review_scan_outbox SET state='claimed',
                       lease_owner=?, lease_expires_at=?, updated_at=?
                       WHERE retry_plan_id=? AND state='pending'""",
                    (
                        lease_owner, timestamp + max(1, int(lease_seconds)),
                        timestamp, plan_id,
                    ),
                )
        result = []
        for row in rows:
            plan = self._retry_plan_dict(row)
            plan["scanLeaseOwner"] = lease_owner
            result.append(plan)
        return result
