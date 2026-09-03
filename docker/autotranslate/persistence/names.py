from __future__ import annotations

import json
import time
from contextlib import contextmanager

from ..subtitles.names import name_scope, normalize_name_phrase


class NamesRepositoryMixin:
    @contextmanager
    def approval_guard(self):
        # Serializes publication validation with approval/revocation commits.
        with self._lock:
            yield

    def approval_identity_for_target(self, target_path):
        from .common import _path_key
        with self._lock:
            row = self._connection.execute("SELECT * FROM retry_plans WHERE target_path=? ORDER BY id DESC LIMIT 1", (_path_key(target_path),)).fetchone()
            if row:
                return self._retry_plan_dict(row)
            row = self._connection.execute("SELECT item_type,item_id,source_language,target_language FROM subtitle_artifacts WHERE target_path=? ORDER BY id DESC LIMIT 1", (_path_key(target_path),)).fetchone()
            identity = dict(row) if row else {}
            # Reliable series identity belongs to retry plans; do not infer it from filenames.
            return identity

    def approval_cache_matches(self, details, identity=None):
        snapshot = self.name_approval_snapshot(identity or details)
        return details.get('approvalRevision', 0) == snapshot['revision'] and (not details.get('approvalScope') or details['approvalScope'] == snapshot['scope'])

    def name_approval_snapshot(self, identity: dict) -> dict:
        scope = name_scope(identity)
        source = identity.get("sourceLanguage") or identity.get("source_language") or ""
        target = identity.get("targetLanguage") or identity.get("target_language") or ""
        with self._lock:
            revision = self._connection.execute(
                "SELECT revision FROM name_approval_scopes WHERE scope=? AND source_language=? AND target_language=?",
                (scope, source, target),
            ).fetchone()
            rows = self._connection.execute(
                "SELECT * FROM name_approvals WHERE scope=? AND source_language=? AND target_language=? AND revoked_at IS NULL ORDER BY id",
                (scope, source, target),
            ).fetchall()
        return {"scope": scope, "sourceLanguage": source, "targetLanguage": target,
                "revision": int(revision[0]) if revision else 0,
                "approvals": [dict(row) for row in rows],
                "pairs": [[row["source_normalized"], row["target_normalized"]] for row in rows]}

    def change_name_approval(self, plan_id: int, expected_updated_at: float, expected_revision: int,
                             *, source_text: str | None = None, target_text: str | None = None,
                             approval_id: int | None = None, cue_number: int | None = None) -> dict:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM retry_plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise LookupError("review not found")
            plan = self._retry_plan_dict(row)
            if abs(plan["updatedAt"] - expected_updated_at) > 0.000001:
                raise RuntimeError("review changed")
            snapshot = self.name_approval_snapshot(plan)
            if snapshot["revision"] != expected_revision:
                raise RuntimeError("approvals changed")
            scope = (snapshot["scope"], snapshot["sourceLanguage"], snapshot["targetLanguage"])
            now = time.time()
            if approval_id is None:
                if row["state"] != "regeneration_waiting" or row["last_deferral_class"] != "manual_review":
                    raise RuntimeError("review is not awaiting action")
                if not source_text or not target_text:
                    raise ValueError("name text required")
                db.execute("""INSERT INTO name_approvals(scope, source_language, target_language,
                    source_normalized, target_normalized, source_text, target_text, plan_id, cue_number, source_hash, candidate_hash, created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(scope, source_language, target_language, source_normalized, target_normalized)
                    DO UPDATE SET revoked_at=NULL, source_text=excluded.source_text, target_text=excluded.target_text,
                        plan_id=excluded.plan_id, cue_number=excluded.cue_number, source_hash=excluded.source_hash, candidate_hash=excluded.candidate_hash, created_at=excluded.created_at""",
                    (*scope, normalize_name_phrase(source_text), normalize_name_phrase(target_text),
                     source_text, target_text, plan_id, cue_number, row["source_hash"], row["failed_output_hash"], now))
                approval_id_value = db.execute("SELECT id FROM name_approvals WHERE scope=? AND source_language=? AND target_language=? AND source_normalized=? AND target_normalized=?", (*scope, normalize_name_phrase(source_text), normalize_name_phrase(target_text))).fetchone()[0]
                action = "approve_name"
                # The active plan is the durable recovery request; approval revision changes its ensemble key.
                db.execute("""UPDATE retry_plans SET last_deferral_class=NULL,
                    last_reason='name approved; recovery queued', updated_at=?, eligible_completed_cycle=0,
                    failure_class='whole_file' WHERE id=?""", (now, plan_id))
            else:
                cursor = db.execute("""UPDATE name_approvals SET revoked_at=? WHERE id=? AND scope=?
                    AND source_language=? AND target_language=? AND revoked_at IS NULL""", (now, approval_id, *scope))
                if cursor.rowcount != 1:
                    raise RuntimeError("approval changed")
                approval_id_value = approval_id
                action = "revoke_name"
                db.execute("UPDATE retry_plans SET updated_at=? WHERE id=?", (now, plan_id))
            db.execute("""INSERT INTO name_approval_scopes(scope,source_language,target_language,revision)
                VALUES(?,?,?,1) ON CONFLICT(scope,source_language,target_language)
                DO UPDATE SET revision=revision+1""", scope)
            db.execute("""INSERT INTO manual_review_actions(retry_plan_id,action,outcome,reason_code,details_json,created_at)
                VALUES(?,?,?,?,?,?)""", (plan_id, action, "queued" if approval_id is None else "resolved",
                action, json.dumps({"approvalId": approval_id, "cueNumber": cue_number, "scope": scope[0]}), now))
            approval = db.execute("SELECT * FROM name_approvals WHERE id=?", (approval_id_value,)).fetchone()
            db.execute("""INSERT INTO name_approval_events(approval_id,action,scope,source_language,target_language,
                source_text,target_text,plan_id,cue_number,source_hash,candidate_hash,revision,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (approval_id_value, action, *scope, approval['source_text'], approval['target_text'], plan_id, cue_number, approval['source_hash'], approval['candidate_hash'], expected_revision + 1, now))
            # Versioned lookups protect future reads. Clear metadata caches to avoid legacy seeds bypassing revisions.
            db.execute("DELETE FROM maintenance_validation_cache")
        return self.manual_review_plan(plan_id)
