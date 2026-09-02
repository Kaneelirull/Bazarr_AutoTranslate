"""Explicit cue-text review surface, separate from the status/list DTOs."""
from __future__ import annotations

from contextlib import nullcontext


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
        available = bool(source and candidate and source.is_file() and candidate.is_file())
        source_hash, candidate_hash, cues = plan.get('sourceHash'), plan.get('failedOutputHash'), []
        if available:
            with self.artifact_access.hold(source, candidate) if self.artifact_access else nullcontext():
                source_hash = self.hash_file(source)
                candidate_hash = self.hash_file(candidate)
                if source_hash != plan.get('sourceHash') or candidate_hash != plan.get('failedOutputHash'):
                    raise ManualReviewConflict('review files changed')
                cues = self.inspect_cues(plan, source, candidate, snapshot['pairs'])
                if self.hash_file(source) != source_hash or self.hash_file(candidate) != candidate_hash:
                    raise ManualReviewConflict('review files changed')
        page_size = max(1, min(100, int(page_size)))
        page = max(1, int(page))
        return {'planId': plan_id, 'expectedUpdatedAt': plan['updatedAt'],
                'sourceHash': source_hash, 'candidateHash': candidate_hash,
                'approvalRevision': snapshot['revision'], 'scope': snapshot['scope'],
                'sourceLanguage': snapshot['sourceLanguage'], 'targetLanguage': snapshot['targetLanguage'],
                'items': cues[(page - 1) * page_size:page * page_size],
                'pagination': {'page': page, 'pageSize': page_size, 'total': len(cues)},
                'approvals': [{'id': a['id'], 'sourceText': a['source_text'], 'targetText': a['target_text']}
                              for a in snapshot['approvals']],
                'candidateAvailable': available,
                'unavailableReason': None if available else 'Source or retained candidate is unavailable.',
                'actionsEnabled': available and self.actions_enabled and self._status(plan) == 'needs_attention'}

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
