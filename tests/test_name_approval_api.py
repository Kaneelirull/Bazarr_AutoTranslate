import hashlib
import json
import os
import sys
import tempfile
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'docker'))
os.environ.setdefault('BAZARR_URL', 'http://bazarr:6767')
os.environ.setdefault('BAZARR_API_KEY', 'test')
os.environ.setdefault('LINGARR_URL', 'http://lingarr:8080')
from autotranslate.config import Config
from autotranslate.production import load_runtime
from autotranslate.persistence.state_store import StateStore
from autotranslate.persistence.common import StateStoreError
from autotranslate.manual_review.runtime import build_manual_review_service
from autotranslate.manual_review import ManualReviewConflict, ManualReviewUnavailable
from autotranslate.status.server import start_status_server
from autotranslate.status.tracker import StatusTracker
from autotranslate.subtitles.foundation import (
    SubtitleCue, build_detector, cue_text_hash, file_sha256, parse_srt_cues,
    target_language_for_code, validate_cue_pair, validate_subtitle_pair,
)
from autotranslate.subtitles.names import normalize_name_phrase

app = load_runtime(Config.from_env(), None)


class NameApprovalApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = StateStore(self.root/'state.sqlite3', validator_version='v', config_fingerprint='c')
        self.addCleanup(self.store.close)
        self.source = self.root/'episode.en.srt'
        self.candidate = self.root/'candidate.srt'
        source_text = (
            '1\n00:00:01,000 --> 00:00:02,000\nAlexandra morgenstern.\n\n'
            '2\n00:00:03,000 --> 00:00:04,000\nThis is an ordinary\ncopied sentence.\n\n'
            '3\n00:00:05,000 --> 00:00:06,000\nWe should leave before the rain starts.\n\n'
            '4\n00:00:07,000 --> 00:00:08,000\nI will meet you outside the station.\n\n'
            '5\n00:00:09,000 --> 00:00:10,000\nThe children are already waiting at home.\n\n'
            '6\n00:00:11,000 --> 00:00:12,000\nPlease remember to close the window.\n'
        )
        self.source.write_text(source_text, encoding='utf-8')
        self.candidate.write_text(source_text
            .replace('morgenstern', 'Morgenstern')
            .replace('This is an ordinary\ncopied sentence.', 'This is an ordinary copied sentence.')
            .replace('We should leave before the rain starts.', 'Vi borde gå innan regnet börjar.')
            .replace('I will meet you outside the station.', 'Jag möter dig utanför stationen.')
            .replace('The children are already waiting at home.', 'Barnen väntar redan hemma.')
            .replace('Please remember to close the window.', 'Kom ihåg att stänga fönstret.'), encoding='utf-8')
        self.plan, _ = self.store.schedule_retry_plan(item_type='episodes', item_id=1, source_language='en', target_language='sv',
            source_path=self.source, source_hash=file_sha256(self.source), target_path=self.root/'episode.sv.srt',
            series_key='sonarr:44', failure_class='whole_file', rules=['copied_source'], state='regeneration_waiting',
            artifact_path=self.candidate, failed_output_hash=file_sha256(self.candidate), eligible_completed_cycle=0)
        self.store.hold_retry_for_review(self.plan['id'], 'unresolved_cues')
        for context in [patch.object(app, '_get_validation_state', return_value=self.store),
                        patch.multiple(app, CLEANUP_ROOTS=[self.root], CLEANUP_QUARANTINE_DIR=self.root/'q', STATUS_MANUAL_ACTIONS_ENABLED=True)]:
            context.start()
            self.addCleanup(context.stop)
        self.service = build_manual_review_service(self.store)

    def payload(self):
        detail = self.service.review_cues(self.plan['id'])
        cue = detail['items'][0]
        return {**{k:detail[k] for k in ('expectedUpdatedAt','approvalRevision','sourceHash','candidateHash')},
                'action':'approve_name','cueNumber':cue['cueNumber'],'targetCueHash':cue['targetCueHash']}

    def test_scope_audit_recovery_commit_rolls_back_together(self):
        payload = self.payload()
        self.store._connection.execute("CREATE TRIGGER reject_name_event BEFORE INSERT ON name_approval_events BEGIN SELECT RAISE(ABORT,'injected audit failure'); END")
        with self.assertRaises(ManualReviewUnavailable):
            self.service.perform_name_action(self.plan['id'], payload)
        self.assertEqual(self.store.name_approval_snapshot(self.plan)['revision'], 0)
        self.assertEqual(self.store.name_approval_snapshot(self.plan)['pairs'], [])
        self.assertEqual(self.store.retry_plan(self.plan['id'])['lastDeferralClass'], 'manual_review')

    def test_prose_stays_blocking_and_source_or_candidate_changes_conflict(self):
        detail = self.service.review_cues(self.plan['id'], 2, 1)
        self.assertEqual(detail['pagination']['total'], 2)
        self.assertFalse(detail['items'][0]['canApproveName'])
        payload = self.payload()
        payload.update(cueNumber=2, targetCueHash=detail['items'][0]['targetCueHash'])
        with self.assertRaises(ManualReviewConflict):
            self.service.perform_name_action(self.plan['id'], payload)
        payload = self.payload()
        self.candidate.write_text('changed', encoding='utf-8')
        with self.assertRaises(ManualReviewConflict):
            self.service.perform_name_action(self.plan['id'], payload)
        self.candidate.write_bytes(self.source.read_bytes())
        self.source.write_text('changed', encoding='utf-8')
        with self.assertRaises(ManualReviewConflict):
            self.service.perform_name_action(self.plan['id'], payload)

    def test_revision_invalidation_and_restart_stable_hold(self):
        payload = self.payload()
        self.service.perform_name_action(self.plan['id'], payload)
        snapshot = self.store.name_approval_snapshot(self.plan)
        self.assertEqual(snapshot['revision'], 1)
        self.assertEqual(self.store.name_approval_snapshot({**self.plan, 'sourceLanguage':'de'})['pairs'], [])
        details = {**self.plan, 'approvalScope':snapshot['scope'], 'approvalRevision':1}
        self.assertTrue(self.store.approval_cache_matches(details))
        self.assertFalse(self.store.approval_cache_matches(details, {**self.plan, 'canonicalSeriesKey':'sonarr:999'}))
        event = self.store._fetchone('SELECT * FROM name_approval_events ORDER BY id DESC LIMIT 1')
        self.assertEqual(event['source_hash'], file_sha256(self.source))
        self.assertEqual(event['candidate_hash'], file_sha256(self.candidate))
        self.store.hold_retry_for_review(self.plan['id'], 'other_cues_remain')
        self.store.close()
        self.store = StateStore(self.root/'state.sqlite3', validator_version='v', config_fingerprint='c')
        self.addCleanup(self.store.close)
        held = self.store.retry_plan(self.plan['id'])
        self.assertEqual(self.store.reactivate_changed_manual_reviews('c'), 0)
        self.assertEqual(self.store.retry_plan(self.plan['id'])['updatedAt'], held['updatedAt'])
        self.store.change_name_approval(self.plan['id'], held['updatedAt'], 1, approval_id=snapshot['approvals'][0]['id'])
        self.assertFalse(self.store.approval_cache_matches(details))
        self.assertEqual(self.store.reactivate_changed_manual_reviews('c'), 1)

    def test_validator_version_change_reactivates_existing_review_hold(self):
        self.assertEqual(self.store.retry_plan(self.plan['id'])['lastDeferralClass'], 'manual_review')
        self.store.validator_version = 'source-aware-v6-scoped-name-review'
        self.store.hold_retry_for_review(self.plan['id'], 'unresolved_cues')
        self.store.validator_version = 'source-aware-v7-canonical-cue-approval'
        self.assertEqual(self.store.reactivate_changed_manual_reviews('c'), 1)
        reactivated = self.store.retry_plan(self.plan['id'])
        self.assertIsNone(reactivated['lastDeferralClass'])

    def test_migration_from_v17_preserves_plans_and_adds_v18_atomically(self):
        self.store._connection.execute('DELETE FROM schema_migrations WHERE version=18')
        for table in ('name_approval_events','name_approvals','name_approval_scopes','subtitle_publications','recovery_review_holds'):
            self.store._connection.execute(f'DROP TABLE {table}')
        self.store._connection.execute('PRAGMA user_version=17')
        self.store.close()
        self.store = StateStore(self.root/'state.sqlite3', validator_version='v', config_fingerprint='c')
        self.addCleanup(self.store.close)
        self.assertEqual(self.store._fetchone('PRAGMA user_version')[0], 19)
        self.assertEqual(self.store.retry_plan(self.plan['id'])['lastDeferralClass'], 'manual_review')
        self.assertEqual(self.store._connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.store.name_approval_snapshot(self.plan)['revision'], 0)

    def test_approval_only_removes_copied_finding_other_invariants_still_block(self):
        source = SubtitleCue(1,'00:00:01,000 --> 00:00:02,000',['Alexandra Morgenstern.'])
        target = SubtitleCue(1,source.timestamp,['Alexandra Morgenstern.'])
        pair = (normalize_name_phrase(source.text), normalize_name_phrase(target.text))
        self.assertTrue(validate_cue_pair(source,target,cue_index=0,target_lang='sv'))
        self.assertEqual(validate_cue_pair(source,target,cue_index=0,target_lang='sv',approved_name_pairs=[pair]), [])
        issues = validate_cue_pair(source,target,cue_index=0,target_lang='sv',max_cue_chars=5,approved_name_pairs=[pair])
        self.assertTrue(any(issue.rule != 'copied_source' for issue in issues))
        spelled = SubtitleCue(1,source.timestamp,['alexandra morgenstern. M-O-R-G-E-N-S-T-E-R-N.'])
        self.assertEqual(validate_cue_pair(spelled,spelled,cue_index=0,target_lang='sv'), [])

    def test_http_detail_and_approval_require_safe_action_contract(self):
        tracker = StatusTracker(self.root/'status.json',self.root/'history.jsonl')
        server, thread = start_status_server(tracker, '127.0.0.1', 0, manual_review_service=self.service)
        try:
            base = f'http://127.0.0.1:{server.server_address[1]}'
            with urllib.request.urlopen(base+'/api/manual-reviews/1/cues?pageSize=1') as response:
                detail = json.loads(response.read())
            self.assertEqual(len(detail['items']), 1)
            payload = self.payload()
            request = urllib.request.Request(base+'/api/manual-reviews/1/actions', data=json.dumps(payload).encode(), method='POST', headers={'Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(request)
            self.assertEqual(rejected.exception.code,403)
            rejected.exception.close()
            request.add_header('X-Bazarr-Autotranslate-Action','manual-review')
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status,202)
            with self.assertRaises(urllib.error.HTTPError) as stale:
                urllib.request.urlopen(request)
            self.assertEqual(stale.exception.code,409)
            stale.exception.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_general_cue_decisions_accept_copied_prose_and_finish_atomically(self):
        detail = self.service.review_cues(self.plan['id'], 1, 100)
        self.assertEqual(detail['decisionRevision'], 0)
        self.assertTrue(all(cue['canApproveCue'] for cue in detail['items']))
        source_cues = parse_srt_cues(self.source.read_text(encoding='utf-8'))[0]
        multiline = next(cue for cue in detail['items'] if cue['cueNumber'] == 2)
        self.assertEqual(multiline['sourceCueHash'], cue_text_hash(source_cues[1]))
        self.assertNotEqual(multiline['sourceCueHash'],
            hashlib.sha256(multiline['sourceText'].encode('utf-8')).hexdigest())
        for cue in detail['items']:
            payload = {**{key: detail[key] for key in ('expectedUpdatedAt','decisionRevision','sourceHash','candidateHash')},
                       'action': 'approve_cue', 'cueNumber': cue['cueNumber'],
                       'targetCueHash': cue['targetCueHash'], 'rememberPhrase': False,
                       'requestId': f"approve-cue-request-{cue['cueNumber']}"}
            status, result = self.service.perform_cue_action(self.plan['id'], payload)
            self.assertEqual((status, result['outcome']), (200, 'saved'))
            detail = self.service.review_cues(self.plan['id'], 1, 100)
        self.assertEqual(detail['decisionCounts'], {'approved': 2, 'retry': 0, 'undecided': 0})
        finish = {**{key: detail[key] for key in ('expectedUpdatedAt','decisionRevision','sourceHash','candidateHash')},
                  'action': 'finish_review', 'requestId': 'finish-review-request-001'}
        status, result = self.service.perform_cue_action(self.plan['id'], finish)
        self.assertEqual((status, result['outcome']), (202, 'queued'))
        replay_status, replay = self.service.perform_cue_action(self.plan['id'], finish)
        self.assertEqual((replay_status, replay['outcome']), (202, 'queued'))
        self.assertEqual(len([action for action in self.store.manual_review_actions(self.plan['id'])
                              if action['action'] == 'finish_review']), 1)
        queued = self.store.retry_plan(self.plan['id'])
        self.assertEqual(queued['state'], 'regeneration_waiting')
        self.assertIsNone(queued['lastDeferralClass'])
        decisions = self.store.cue_decision_snapshot(self.store.retry_plan(self.plan['id']))
        report = validate_subtitle_pair(self.source, self.candidate, build_detector(),
            target_language_for_code('sv'), target_lang='sv', min_chars=1,
            approved_cue_findings=decisions['approved'])
        self.assertNotIn('copied_source', {issue.rule for issue in report.issues})
        self.assertEqual(
            {observation.cue_number for observation in report.observations
             if observation.classification == 'operator_approved'},
            {1, 2},
        )
        self.assertTrue(report.valid, report.summary())
        self.assertTrue(self.store.resolve_retry_plan(
            self.plan['id'], file_sha256(self.source), outcome='accepted_after_retry'))
        resolved = self.store.retry_plan(self.plan['id'])
        self.assertEqual(resolved['state'], 'accepted_after_retry')
        self.assertIsNone(resolved['lastDeferralClass'])
