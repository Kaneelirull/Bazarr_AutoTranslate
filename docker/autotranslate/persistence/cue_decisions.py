from __future__ import annotations

import json
import math
import time
from ..subtitles.names import name_scope, normalize_name_phrase


class CueDecisionsRepositoryMixin:
    """Durable operator decisions for one retained subtitle candidate."""

    @staticmethod
    def _action_replay(db, plan_id: int, action: str, request_id: str | None, fingerprint: str | None):
        if not request_id:
            return None
        existing = db.execute("SELECT * FROM manual_review_action_requests WHERE request_id=?", (request_id,)).fetchone()
        if existing is None:
            return None
        if int(existing["retry_plan_id"]) != int(plan_id) or existing["action"] != action or existing["fingerprint"] != fingerprint:
            raise RuntimeError("idempotency key was reused for different evidence")
        return db.execute("SELECT * FROM retry_plans WHERE id=?", (plan_id,)).fetchone()

    @staticmethod
    def _record_action_request(db, plan_id: int, action: str, request_id: str | None,
                               fingerprint: str | None, created_at: float) -> None:
        if request_id:
            db.execute("INSERT INTO manual_review_action_requests(request_id,retry_plan_id,action,fingerprint,created_at) VALUES(?,?,?,?,?)",
                       (request_id, plan_id, action, fingerprint, created_at))

    def cue_action_replay(self, plan_id: int, action: str, request_id: str | None, fingerprint: str | None):
        if not request_id:
            return None
        with self._lock:
            existing = self._connection.execute(
                "SELECT * FROM manual_review_action_requests WHERE request_id=?", (request_id,)).fetchone()
            if existing is None:
                return None
            if int(existing["retry_plan_id"]) != int(plan_id) or existing["action"] != action or existing["fingerprint"] != fingerprint:
                raise RuntimeError("idempotency key was reused for different evidence")
            row = self._connection.execute("SELECT * FROM retry_plans WHERE id=?", (plan_id,)).fetchone()
        return self._retry_plan_dict(row) if row is not None else None

    def cue_decision_snapshot(self, identity: dict) -> dict:
        plan_id = identity.get("id")
        if plan_id is None:
            item_type = identity.get("itemType") or identity.get("item_type")
            item_id = identity.get("itemId") or identity.get("item_id")
            language = identity.get("targetLanguage") or identity.get("target_language")
            with self._lock:
                plan = self._connection.execute(
                    "SELECT id FROM retry_plans WHERE item_type=? AND item_id=? AND target_language=? ORDER BY id DESC LIMIT 1",
                    (item_type, item_id, language),
                ).fetchone()
            plan_id = plan[0] if plan else None
        if plan_id is None:
            return {"revision": 0, "decisions": [], "approved": []}
        with self._lock:
            revision = self._connection.execute(
                "SELECT revision FROM manual_review_decision_scopes WHERE retry_plan_id=?", (int(plan_id),)
            ).fetchone()
            rows = self._connection.execute(
                "SELECT * FROM manual_review_cue_decisions WHERE retry_plan_id=? ORDER BY cue_number", (int(plan_id),)
            ).fetchall()
        decisions = []
        for row in rows:
            value = dict(row)
            value["findings"] = json.loads(value.pop("findings_json"))
            value["rememberPhrase"] = bool(value.pop("remember_phrase"))
            decisions.append(value)
        approved = [
            {"cueNumber": row["cue_number"], "timestamp": row["timestamp"],
             "sourceCueHash": row["source_cue_hash"], "targetCueHash": row["target_cue_hash"],
             "findings": row["findings"]}
            for row in decisions if row["decision"] == "approve"
        ]
        return {"revision": int(revision[0]) if revision else 0, "decisions": decisions, "approved": approved}

    def save_cue_decision(self, plan_id: int, expected_updated_at: float, expected_revision: int, *,
                          cue: dict, decision: str, remember_phrase: bool = False,
                          request_id: str | None = None, request_fingerprint: str | None = None) -> dict:
        if decision not in {"approve", "retry"}:
            raise ValueError("invalid cue decision")
        if remember_phrase and (decision != "approve" or not set(cue["rules"]) & {"copied_source", "ambiguous_copied_source"}):
            raise ValueError("only approved copied text can be remembered")
        now = time.time()
        with self._transaction() as db:
            replay = self._action_replay(db, plan_id, f"{decision}_cue", request_id, request_fingerprint)
            if replay is not None:
                return self._retry_plan_dict(replay)
            row = self._manual_row_for_update(db, plan_id, expected_updated_at)
            current = db.execute("SELECT revision FROM manual_review_decision_scopes WHERE retry_plan_id=?", (plan_id,)).fetchone()
            if (int(current[0]) if current else 0) != int(expected_revision):
                raise RuntimeError("review decisions changed")
            db.execute("""INSERT INTO manual_review_cue_decisions(
                retry_plan_id,cue_number,timestamp,source_hash,candidate_hash,source_cue_hash,target_cue_hash,
                findings_json,decision,remember_phrase,source_text,target_text,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(retry_plan_id,cue_number) DO UPDATE SET timestamp=excluded.timestamp,
                source_hash=excluded.source_hash,candidate_hash=excluded.candidate_hash,
                source_cue_hash=excluded.source_cue_hash,target_cue_hash=excluded.target_cue_hash,
                findings_json=excluded.findings_json,decision=excluded.decision,
                remember_phrase=excluded.remember_phrase,source_text=excluded.source_text,
                target_text=excluded.target_text,updated_at=excluded.updated_at""",
                (plan_id, cue["cueNumber"], cue["timestamp"], row["source_hash"], row["failed_output_hash"],
                 cue["sourceCueHash"], cue["targetCueHash"], json.dumps(cue["rules"], sort_keys=True), decision,
                 int(bool(remember_phrase)), cue["sourceText"], cue["targetText"], now, now))
            db.execute("INSERT INTO manual_review_decision_scopes(retry_plan_id,revision) VALUES(?,1) ON CONFLICT(retry_plan_id) DO UPDATE SET revision=revision+1", (plan_id,))
            db.execute("UPDATE retry_plans SET updated_at=? WHERE id=?", (now, plan_id))
            self._insert_manual_action(db, plan_id, f"{decision}_cue", "pending", "operator_decision",
                                       {"cueNumber": cue["cueNumber"]}, now)
            self._record_action_request(db, plan_id, f"{decision}_cue", request_id, request_fingerprint, now)
            updated = db.execute("SELECT * FROM retry_plans WHERE id=?", (plan_id,)).fetchone()
        return self._retry_plan_dict(updated)

    def clear_cue_decision(self, plan_id: int, expected_updated_at: float, expected_revision: int, cue_number: int,
                           request_id: str | None = None, request_fingerprint: str | None = None) -> dict:
        now = time.time()
        with self._transaction() as db:
            replay = self._action_replay(db, plan_id, "clear_cue_decision", request_id, request_fingerprint)
            if replay is not None:
                return self._retry_plan_dict(replay)
            self._manual_row_for_update(db, plan_id, expected_updated_at)
            current = db.execute("SELECT revision FROM manual_review_decision_scopes WHERE retry_plan_id=?", (plan_id,)).fetchone()
            if (int(current[0]) if current else 0) != int(expected_revision):
                raise RuntimeError("review decisions changed")
            if db.execute("DELETE FROM manual_review_cue_decisions WHERE retry_plan_id=? AND cue_number=?", (plan_id, cue_number)).rowcount != 1:
                raise RuntimeError("cue decision changed")
            db.execute("UPDATE manual_review_decision_scopes SET revision=revision+1 WHERE retry_plan_id=?", (plan_id,))
            db.execute("UPDATE retry_plans SET updated_at=? WHERE id=?", (now, plan_id))
            self._insert_manual_action(db, plan_id, "clear_cue_decision", "resolved", "operator_decision_cleared", {"cueNumber": cue_number}, now)
            self._record_action_request(db, plan_id, "clear_cue_decision", request_id, request_fingerprint, now)
            updated = db.execute("SELECT * FROM retry_plans WHERE id=?", (plan_id,)).fetchone()
        return self._retry_plan_dict(updated)

    def finish_cue_review(self, plan_id: int, expected_updated_at: float, expected_revision: int,
                          expected_cues: list[int], completed_cycle: int,
                          request_id: str | None = None, request_fingerprint: str | None = None) -> dict:
        now = time.time()
        with self._transaction() as db:
            replay = self._action_replay(db, plan_id, "finish_review", request_id, request_fingerprint)
            if replay is not None:
                return self._retry_plan_dict(replay)
            row = self._manual_row_for_update(db, plan_id, expected_updated_at)
            current = db.execute("SELECT revision FROM manual_review_decision_scopes WHERE retry_plan_id=?", (plan_id,)).fetchone()
            if (int(current[0]) if current else 0) != int(expected_revision):
                raise RuntimeError("review decisions changed")
            rows = db.execute("SELECT * FROM manual_review_cue_decisions WHERE retry_plan_id=?", (plan_id,)).fetchall()
            if any(value["source_hash"] != row["source_hash"] or value["candidate_hash"] != row["failed_output_hash"] for value in rows):
                raise RuntimeError("cue evidence changed")
            if {int(value["cue_number"]) for value in rows} != {int(value) for value in expected_cues}:
                raise RuntimeError("every cue needs a decision")
            remembered = [value for value in rows if value["decision"] == "approve" and value["remember_phrase"]]
            if remembered:
                plan = self._retry_plan_dict(row)
                scope = name_scope(plan)
                source_language = plan.get("sourceLanguage") or ""
                target_language = plan.get("targetLanguage") or ""
                revision_row = db.execute("SELECT revision FROM name_approval_scopes WHERE scope=? AND source_language=? AND target_language=?", (scope, source_language, target_language)).fetchone()
                approval_revision = int(revision_row[0]) if revision_row else 0
                for value in remembered:
                    db.execute("""INSERT INTO name_approvals(scope,source_language,target_language,source_normalized,target_normalized,
                        source_text,target_text,plan_id,cue_number,source_hash,candidate_hash,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,source_language,target_language,source_normalized,target_normalized)
                        DO UPDATE SET revoked_at=NULL,source_text=excluded.source_text,target_text=excluded.target_text,
                        plan_id=excluded.plan_id,cue_number=excluded.cue_number,source_hash=excluded.source_hash,
                        candidate_hash=excluded.candidate_hash,created_at=excluded.created_at""",
                        (scope, source_language, target_language, normalize_name_phrase(value["source_text"]),
                         normalize_name_phrase(value["target_text"]), value["source_text"], value["target_text"], plan_id,
                         value["cue_number"], value["source_hash"], value["candidate_hash"], now))
                    approval_id = db.execute("SELECT id FROM name_approvals WHERE scope=? AND source_language=? AND target_language=? AND source_normalized=? AND target_normalized=?",
                        (scope, source_language, target_language, normalize_name_phrase(value["source_text"]), normalize_name_phrase(value["target_text"]))).fetchone()[0]
                    db.execute("""INSERT INTO name_approval_events(approval_id,action,scope,source_language,target_language,
                        source_text,target_text,plan_id,cue_number,source_hash,candidate_hash,revision,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (approval_id, "approve_phrase", scope, source_language,
                        target_language, value["source_text"], value["target_text"], plan_id, value["cue_number"],
                        value["source_hash"], value["candidate_hash"], approval_revision + 1, now))
                db.execute("INSERT INTO name_approval_scopes(scope,source_language,target_language,revision) VALUES(?,?,?,1) ON CONFLICT(scope,source_language,target_language) DO UPDATE SET revision=revision+1", (scope, source_language, target_language))
                db.execute("DELETE FROM maintenance_validation_cache")
            db.execute("""UPDATE retry_plans SET state='regeneration_waiting',eligible_completed_cycle=?,
                last_deferral_class=NULL,final_outcome=NULL,last_reason='manual cue review finished; recovery queued',
                claim_owner=NULL,claimed_at=NULL,submission_attempt_id=NULL,updated_at=? WHERE id=?""",
                (max(0, int(completed_cycle)), now, plan_id))
            db.execute("DELETE FROM recovery_review_holds WHERE retry_plan_id=?", (plan_id,))
            db.execute("UPDATE subtitle_publications SET state='pending',failure_count=0,eligible_cycle=?,updated_at=? WHERE target_path=? AND state='manual_review'",
                       (max(0, int(completed_cycle)), now, row["target_path"]))
            self._insert_manual_action(db, plan_id, "finish_review", "queued", "operator_review_complete",
                                       {"cueCount": len(expected_cues)}, now)
            self._record_action_request(db, plan_id, "finish_review", request_id, request_fingerprint, now)
            updated = db.execute("SELECT * FROM retry_plans WHERE id=?", (plan_id,)).fetchone()
        return self._retry_plan_dict(updated)

    def reopen_manual_review(self, plan_id: int, expected_updated_at: float,
                             request_id: str | None = None, request_fingerprint: str | None = None) -> dict:
        now = time.time()
        with self._transaction() as db:
            replay = self._action_replay(db, plan_id, "reopen", request_id, request_fingerprint)
            if replay is not None:
                return self._retry_plan_dict(replay)
            row = db.execute("SELECT * FROM retry_plans WHERE id=?", (plan_id,)).fetchone()
            if row is None or not math.isfinite(float(expected_updated_at)) or abs(float(row["updated_at"])-float(expected_updated_at)) > .000001:
                raise RuntimeError("manual review changed")
            if row["state"] != "manual_dismissed":
                raise RuntimeError("review is not ignored")
            db.execute("UPDATE retry_plans SET state='regeneration_waiting',last_deferral_class='manual_review',final_outcome=NULL,last_reason='reopened by operator',updated_at=? WHERE id=?", (now, plan_id))
            self._insert_manual_action(db, plan_id, "reopen", "pending", "operator_reopened", {}, now)
            self._record_action_request(db, plan_id, "reopen", request_id, request_fingerprint, now)
            updated = db.execute("SELECT * FROM retry_plans WHERE id=?", (plan_id,)).fetchone()
        return self._retry_plan_dict(updated)
