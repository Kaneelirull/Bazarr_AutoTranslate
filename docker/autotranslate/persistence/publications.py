from __future__ import annotations

import json
import time
from .common import _path_key


class PublicationsRepositoryMixin:
    def publication_receipt_known(self, candidate):
        with self._lock:
            return self._connection.execute('SELECT 1 FROM subtitle_publications WHERE candidate_path=?', (_path_key(candidate),)).fetchone() is not None

    def stage_publication(self, publication_id, path):
        with self._transaction() as db:
            db.execute('UPDATE subtitle_publications SET stage_path=?,updated_at=? WHERE id=?', (_path_key(path), time.time(), publication_id))

    def pending_publications(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM subtitle_publications WHERE state IN ('pending','published','manual_review') ORDER BY id").fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def publication_for_target(self, target) -> dict | None:
        return next((p for p in self.pending_publications() if p['target_path'] == _path_key(target)), None)

    def record_publication(self, *, target, candidate, candidate_hash, source_path,
                           source_hash, expected_target_hash, payload) -> int:
        now = time.time()
        with self._transaction() as db:
            cursor = db.execute("""INSERT INTO subtitle_publications(target_path,candidate_path,candidate_hash,
                source_path,source_hash,expected_target_hash,payload_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (_path_key(target), _path_key(candidate), candidate_hash,
                _path_key(source_path), source_hash, expected_target_hash, json.dumps(payload), now, now))
            return int(cursor.lastrowid)

    def finish_publication(self, publication_id: int, state: str) -> None:
        if state not in {'published', 'completed', 'superseded'}:
            raise ValueError('invalid publication disposition')
        with self._transaction() as db:
            db.execute("UPDATE subtitle_publications SET state=?, updated_at=? WHERE id=?", (state, time.time(), publication_id))

    def fail_publication(self, publication_id: int, completed_cycle: int, error: str) -> dict:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM subtitle_publications WHERE id=?", (publication_id,)).fetchone()
            count = row['failure_count'] + 1
            state = 'manual_review' if count >= 3 else 'pending'
            db.execute("""UPDATE subtitle_publications SET state=?,failure_count=?,eligible_cycle=?,last_error=?,updated_at=? WHERE id=?""",
                       (state, count, completed_cycle + min(count, 2), error, time.time(), publication_id))
            db.execute("""UPDATE retry_plans SET last_deferral_class=?, last_reason=?, updated_at=?
                WHERE target_path=? AND state IN ('repair_retry_queued','regeneration_waiting')""",
                ('manual_review' if count >= 3 else 'publication_pending', error, time.time(), row['target_path']))
        return {'state': state, 'failure_count': count}

    def set_retry_candidate(self, plan_id, artifact_path, target_hash):
        with self._transaction() as db:
            db.execute("UPDATE retry_plans SET artifact_path=?, failed_output_hash=?, updated_at=? WHERE id=?",
                       (_path_key(artifact_path), target_hash, time.time(), plan_id))

    def recovery_policy_key(self, plan):
        snapshot = self.name_approval_snapshot(plan)
        attempts = self.quarantine_attempts(plan['itemType'], plan['itemId'], plan['targetLanguage'])
        return json.dumps(['recovery-v2', self.validator_version, self.config_fingerprint,
                           plan['sourceHash'], snapshot['scope'], snapshot['revision'], sorted((a['id'], a['targetHash']) for a in attempts)])

    def hold_retry_for_review(self, plan_id, reason):
        plan = self.retry_plan(plan_id)
        if plan is None:
            return
        key = self.recovery_policy_key(plan)
        with self._lock:
            hold = self._connection.execute('SELECT policy_key FROM recovery_review_holds WHERE retry_plan_id=?', (plan_id,)).fetchone()
        if hold and hold[0] == key and plan.get('lastDeferralClass') == 'manual_review' and plan.get('lastReason') == reason:
            return
        with self._transaction() as db:
            db.execute("""UPDATE retry_plans SET state='regeneration_waiting', last_deferral_class='manual_review',
                last_reason=?, updated_at=? WHERE id=? AND state IN ('regeneration_waiting','repair_retry_queued','retry_in_progress')""",
                (reason, time.time(), plan_id))
            db.execute("INSERT OR REPLACE INTO recovery_review_holds(retry_plan_id,policy_key) VALUES(?,?)", (plan_id, key))
