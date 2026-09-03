"""Explicit cue-text review surface, separate from the status/list DTOs."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json


class CueReviewMixin:
    def review_cues(self, plan_id: int, page: int = 1, page_size: int = 20) -> dict:
        from .service import ManualReviewNotFound, ManualReviewConflict, ManualReviewUnavailable
        plan = self.repository.manual_review_plan(plan_id)
        if plan is None:
            raise ManualReviewNotFound('review not found')
        if self.inspect_cues is None:
            raise ManualReviewUnavailable('cue inspection unavailable')
        source, _ = self._managed_path(plan.get('sourcePath'))
        candidate, _ = self._managed_path(plan.get('artifactPath'))
        snapshot = self.repository.name_approval_snapshot(plan)
        decision_snapshot = self.repository.cue_decision_snapshot(plan)
        available = bool(source and candidate and source.is_file() and candidate.is_file())
        source_hash, candidate_hash, cues, file_findings = plan.get('sourceHash'), plan.get('failedOutputHash'), [], []
        if available:
            with self.artifact_access.hold(source, candidate) if self.artifact_access else nullcontext():
                source_hash = self.hash_file(source)
                candidate_hash = self.hash_file(candidate)
                if source_hash != plan.get('sourceHash') or candidate_hash != plan.get('failedOutputHash'):
                    raise ManualReviewConflict('review files changed')
                try:
                    cues = self.inspect_cues(plan, source, candidate, snapshot['pairs'])
                except ManualReviewConflict as exc:
                    file_findings = [{'code': 'structure_recovery_required', 'reason': str(exc),
                                      'action': 'queue_retry'}]
                if self.hash_file(source) != source_hash or self.hash_file(candidate) != candidate_hash:
                    raise ManualReviewConflict('review files changed')
        page_size = max(1, min(100, int(page_size)))
        page = max(1, int(page))
        saved = {int(value['cue_number']): value for value in decision_snapshot['decisions']
                 if value['source_hash'] == source_hash and value['candidate_hash'] == candidate_hash}
        for cue in cues:
            decision = saved.get(int(cue['cueNumber']))
            cue['decision'] = decision['decision'] if decision else None
            cue['rememberPhrase'] = bool(decision and decision['rememberPhrase'])
        return {'planId': plan_id, 'expectedUpdatedAt': plan['updatedAt'],
                'sourceHash': source_hash, 'candidateHash': candidate_hash,
                'approvalRevision': snapshot['revision'], 'scope': snapshot['scope'],
                'decisionRevision': decision_snapshot['revision'],
                'decisionCounts': {
                    'approved': sum(c.get('decision') == 'approve' for c in cues),
                    'retry': sum(c.get('decision') == 'retry' for c in cues),
                    'undecided': sum(not c.get('decision') for c in cues),
                },
                'fileFindings': file_findings,
                'sourceLanguage': snapshot['sourceLanguage'], 'targetLanguage': snapshot['targetLanguage'],
                'items': cues[(page - 1) * page_size:page * page_size],
                'pagination': {'page': page, 'pageSize': page_size, 'total': len(cues)},
                'approvals': [{'id': a['id'], 'sourceText': a['source_text'], 'targetText': a['target_text']}
                              for a in snapshot['approvals']],
                'candidateAvailable': available,
                'unavailableReason': None if available else 'Source or retained candidate is unavailable.',
                'actionsEnabled': available and self.actions_enabled and self._status(plan) == 'needs_attention'}

    def perform_cue_action(self, plan_id: int, payload: dict) -> tuple[int, dict]:
        from .service import ManualReviewConflict, ManualReviewNotFound
        self._require_enabled()
        plan = self.repository.manual_review_plan(plan_id)
        if plan is None:
            raise ManualReviewNotFound('review not found')
        action = payload['action']
        if action not in {'approve_cue', 'retry_cue', 'clear_cue_decision', 'finish_review', 'reopen'}:
            raise ValueError('unsupported cue action')
        request_id = payload.get('requestId')
        request_fingerprint = hashlib.sha256(json.dumps(
            {key: value for key, value in payload.items() if key != 'requestId'},
            sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        try:
            replay = self.repository.cue_action_replay(plan_id, action, request_id, request_fingerprint)
        except RuntimeError as exc:
            raise self._translate_repository_error(exc) from exc
        if replay is not None:
            outcome = {'approve_cue': 'saved', 'retry_cue': 'saved', 'clear_cue_decision': 'cleared',
                       'finish_review': 'queued', 'reopen': 'reopened'}[action]
            return (202 if action == 'finish_review' else 200), {'outcome': outcome, 'item': self._public_item(replay)}
        if action == 'reopen':
            try:
                current = self.repository.reopen_manual_review(plan_id, float(payload['expectedUpdatedAt']),
                    request_id=request_id, request_fingerprint=request_fingerprint)
            except (LookupError, RuntimeError) as exc:
                raise self._translate_repository_error(exc) from exc
            self._notify_change(current)
            return 200, {'outcome': 'reopened', 'item': self._public_item(current)}
        detail = self.review_cues(plan_id, 1, 100)
        if action == 'finish_review' and detail.get('fileFindings'):
            raise ManualReviewConflict('file-level recovery is required')
        all_cues = list(detail['items'])
        for page in range(2, (detail['pagination']['total'] + 99) // 100 + 1):
            all_cues.extend(self.review_cues(plan_id, page, 100)['items'])
        for key in ('sourceHash', 'candidateHash', 'decisionRevision', 'expectedUpdatedAt'):
            if payload.get(key) != detail.get(key):
                raise ManualReviewConflict('review changed')
        source, _ = self._managed_path(plan.get('sourcePath'))
        candidate, _ = self._managed_path(plan.get('artifactPath'))
        if source is None or candidate is None:
            raise ManualReviewConflict('review files are unavailable')
        if self.hash_file(source) != detail['sourceHash'] or self.hash_file(candidate) != detail['candidateHash']:
            raise ManualReviewConflict('review files changed')
        try:
            if action == 'finish_review':
                current = self.repository.finish_cue_review(
                    plan_id, detail['expectedUpdatedAt'], detail['decisionRevision'],
                    [cue['cueNumber'] for cue in all_cues], self.completed_cycle(),
                    request_id=request_id, request_fingerprint=request_fingerprint)
                outcome, status = 'queued', 202
            else:
                cue = next((value for value in all_cues if value['cueNumber'] == int(payload['cueNumber'])), None)
                if cue is None or cue['targetCueHash'] != payload['targetCueHash']:
                    raise ManualReviewConflict('cue evidence changed')
                if action == 'clear_cue_decision':
                    current = self.repository.clear_cue_decision(plan_id, detail['expectedUpdatedAt'],
                        detail['decisionRevision'], cue['cueNumber'], request_id=request_id,
                        request_fingerprint=request_fingerprint)
                    outcome, status = 'cleared', 200
                else:
                    if action == 'approve_cue' and not cue.get('canApproveCue'):
                        raise ManualReviewConflict('cue has mandatory findings')
                    current = self.repository.save_cue_decision(plan_id, detail['expectedUpdatedAt'],
                        detail['decisionRevision'], cue=cue,
                        decision='approve' if action == 'approve_cue' else 'retry',
                        remember_phrase=bool(payload.get('rememberPhrase', False)), request_id=request_id,
                        request_fingerprint=request_fingerprint)
                    outcome, status = 'saved', 200
        except (LookupError, RuntimeError) as exc:
            raise self._translate_repository_error(exc) from exc
        self._notify_change(current)
        return status, {'outcome': outcome, 'item': self._public_item(current)}

    def perform_name_action(self, plan_id: int, payload: dict) -> tuple[int, dict]:
        from .service import ManualReviewConflict, ManualReviewNotFound
        self._require_enabled()
        action = payload['action']
        plan = self.repository.manual_review_plan(plan_id)
        if plan is None:
            raise ManualReviewNotFound('review not found')
        expected = float(payload['expectedUpdatedAt'])
        revision = int(payload['approvalRevision'])
        if action == 'revoke_name':
            try:
                current = self.repository.change_name_approval(plan_id, expected, revision, approval_id=int(payload['approvalId']))
            except (LookupError, RuntimeError) as exc:
                raise self._translate_repository_error(exc) from exc
        else:
            source, _ = self._managed_path(plan.get('sourcePath'))
            candidate, _ = self._managed_path(plan.get('artifactPath'))
            with self.artifact_access.hold(source, candidate) if self.artifact_access else nullcontext():
                # Resolve trusted text on the server; clients never supply the text to learn.
                detail = self.review_cues(plan_id, 1, 100)
                if not detail['candidateAvailable']:
                    raise ManualReviewConflict('review candidate unavailable')
                for key in ('sourceHash', 'candidateHash', 'approvalRevision', 'expectedUpdatedAt'):
                    if payload[key] != detail[key]:
                        raise ManualReviewConflict('review changed')
                all_cues = detail['items']
                for page in range(2, (detail['pagination']['total'] + 99) // 100 + 1):
                    all_cues.extend(self.review_cues(plan_id, page, 100)['items'])
                cue = next((c for c in all_cues if c['cueNumber'] == int(payload['cueNumber'])), None)
                if cue is None or not cue['canApproveName'] or cue['targetCueHash'] != payload['targetCueHash']:
                    raise ManualReviewConflict('cue changed or is not a copied-name finding')
                if self.hash_file(source) != detail['sourceHash'] or self.hash_file(candidate) != detail['candidateHash']:
                    raise ManualReviewConflict('review files changed')
                try:
                    current = self.repository.change_name_approval(plan_id, expected, revision,
                        source_text=cue['sourceText'], target_text=cue['targetText'], cue_number=cue['cueNumber'])
                except (LookupError, RuntimeError) as exc:
                    raise self._translate_repository_error(exc) from exc
        self._notify_change(current)
        return 202 if action == 'approve_name' else 200, {
            'outcome': 'queued' if action == 'approve_name' else 'revoked', 'item': self._public_item(current)}
