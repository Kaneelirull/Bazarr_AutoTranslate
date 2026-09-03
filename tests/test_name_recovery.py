import json
import errno
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'docker'))
os.environ.setdefault('BAZARR_URL', 'http://bazarr:6767')
os.environ.setdefault('BAZARR_API_KEY', 'test')
os.environ.setdefault('LINGARR_URL', 'http://lingarr:8080')
from autotranslate.config import Config
from autotranslate.production import load_runtime
from autotranslate.persistence.state_store import StateStore
from autotranslate.manual_review.runtime import build_manual_review_service
from autotranslate.manual_review import ManualReviewConflict
from autotranslate.subtitles.foundation import build_detector, file_sha256, VALIDATOR_VERSION
from autotranslate.subtitles import foundation
from autotranslate.subtitles.names import normalize_name_phrase, approved_name, name_scope
from autotranslate.scheduling import runtime as scheduling

app = load_runtime(Config.from_env(), None)


class NameRecoveryTests(unittest.TestCase):
    def setUp(self):
        if os.name == 'posix':
            # Recovery uses real filesystem permissions under an unprivileged runner.
            ownership = patch.multiple(foundation, MANAGED_FILE_UID=os.geteuid(), MANAGED_FILE_GID=os.getegid())
            ownership.start()
            self.addCleanup(ownership.stop)

    def test_shutdown_retains_completed_cues_before_worker_returns(self):
        from autotranslate.subtitles.foundation import validate_subtitle_pair, target_language_for_code
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root/'episode.en.srt', root/'episode.et.srt'
            source.write_text('1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n2\n00:00:03,000 --> 00:00:04,000\nSome other dialogue.\n', encoding='utf-8')
            target.write_text(source.read_text(encoding='utf-8').replace('Hello there.','[SOURCE] first leak [/SOURCE]').replace('Some other dialogue.','[SOURCE] second leak [/SOURCE]'), encoding='utf-8')
            original_hash = file_sha256(target)
            store = StateStore(root/'state.sqlite3', validator_version=VALIDATOR_VERSION, config_fingerprint='c')
            calls = []
            def translate(*args, **kwargs):
                calls.append(args)
                return 'Tere, sõber.'
            try:
                report = validate_subtitle_pair(source,target,build_detector(),target_language_for_code('et'),target_lang='et')
                with patch.object(app, '_get_validation_state', return_value=store), patch.object(app,'lingarr_translate_line',side_effect=translate), patch.multiple(app, CLEANUP_REPAIR_ENABLED=True, shutdown_requested=False):
                    result = app._perform_repair(str(source),str(target),'en','et',5,'Show','episodes',report,original_hash,
                        expected_source_hash=file_sha256(source), cancellation_requested=lambda: len(calls)>=2)
                self.assertEqual(result.action,'repair-deferred')
                self.assertEqual(file_sha256(target),original_hash)
                recoveries = store.cue_recoveries('episodes',5,'et',source_file_hash=file_sha256(source))
                self.assertEqual(len(recoveries),1)
                self.assertEqual(len(list((root/'recovery').glob('partial-*.srt'))),1)
            finally:
                store.close()

    def test_scheduler_publication_retries_do_not_submit_ai_or_consume_translation_retries(self):
        from autotranslate.subtitles.publication import retain_publication
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target, candidate = (root / name for name in ('episode.en.srt', 'episode.sv.srt', 'candidate.srt'))
            source.write_text('1\n00:00:01,000 --> 00:00:02,000\nPlease close the window.\n', encoding='utf-8')
            candidate.write_text('1\n00:00:01,000 --> 00:00:02,000\nStäng fönstret.\n', encoding='utf-8')
            store = StateStore(root/'state.sqlite3', validator_version=VALIDATOR_VERSION, config_fingerprint='current')
            try:
                plan, _ = store.schedule_retry_plan(item_type='episodes', item_id=1, target_language='sv', source_language='en',
                    source_hash=file_sha256(source), source_path=source, target_path=target, series_key='sonarr:1',
                    failure_class='whole_file', rules=['publication_pending'], state='regeneration_waiting', eligible_completed_cycle=0)
                row = retain_publication(store, candidate, target, source_path=source, source_hash=file_sha256(source),
                    expected_target_hash=None, payload={'targetLanguage':'sv','sourceLanguage':'en','origin':'lingarr','operation':'donor_recovery','identity':plan})
                with patch.object(app, '_get_validation_state', return_value=store), \
                     patch.object(app, 'lingarr_translate_line', side_effect=AssertionError('AI forbidden')), \
                     patch.multiple(app, shutdown_requested=False, _completed_cycle=0), \
                     patch('autotranslate.subtitles.publication.os.replace', side_effect=OSError(errno.EACCES, 'denied')) as rename:
                    scheduling._resume_publications({})
                    scheduling._resume_publications({})
                    self.assertEqual(rename.call_count, 1)
                    app._completed_cycle = 1
                    scheduling._resume_publications({})
                    app._completed_cycle = 2
                    scheduling._resume_publications({})
                    self.assertEqual(rename.call_count, 2)
                    app._completed_cycle = 3
                    scheduling._resume_publications({})
                    scheduling._resume_publications({})
                    held = store.retry_plan(plan['id'])
                    self.assertEqual(held['lastDeferralClass'], 'manual_review')
                    self.assertEqual(store.reactivate_changed_manual_reviews('current'), 0)
                    scheduling._resume_publications({})
                    self.assertEqual(store.retry_plan(plan['id'])['updatedAt'], held['updatedAt'])
                    self.assertEqual(held['attemptCount'], 0)
                    self.assertEqual(store.claim_due_retry_plans(99, limit=10, per_series_limit=10), [])
                    self.assertTrue(Path(row['candidate_path']).exists())
                    store.queue_manual_retry(plan['id'], held['updatedAt'], 3)
                with patch.object(app, '_get_validation_state', return_value=store), patch.multiple(app, shutdown_requested=False, _completed_cycle=3):
                    scheduling._resume_publications({})
                self.assertTrue(target.exists())
                self.assertIsNone(store.publication_for_target(target))
                self.assertEqual(store.retry_plan(plan['id'])['attemptCount'], 0)
            finally:
                store.close()

    def test_exact_normalization_and_scope_have_no_generalization(self):
        pair = (normalize_name_phrase('  Éva\nMARTIN! '), normalize_name_phrase('éva Martin!'))
        self.assertTrue(approved_name('E\u0301va Martin!', 'ÉVA MARTIN!', [pair]))
        self.assertFalse(approved_name('Éva Martin', 'Éva Martin', [pair]))
        self.assertFalse(approved_name('Hello Éva Martin!', 'Éva Martin!', [pair]))
        self.assertEqual(name_scope({'itemType': 'episodes', 'itemId': 1, 'canonicalSeriesKey': 'sonarr:12'}), 'sonarr:12')
        self.assertEqual(name_scope({'itemType': 'movies', 'itemId': 1, 'canonicalSeriesKey': 'sonarr:12'}), 'movies:1')
        self.assertEqual(name_scope({'itemType': 'episodes', 'itemId': 1, 'seriesKey': 'guessed show'}), 'episodes:1')

    def test_all_five_captured_examples_through_scheduler_without_network_or_ai(self):
        evidence = ROOT / 'examples' / 'RetryInvestigation-2026-09-02'
        records = json.loads((evidence / 'replay-records.json').read_text(encoding='utf-8'))
        detector = build_detector()
        successes = []
        reviews = []
        for captured in records['retry_plans']:
            with self.subTest(item=captured['item_id']), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = StateStore(root / 'state.sqlite3', validator_version=VALIDATOR_VERSION, config_fingerprint='current')
                try:
                    folder = evidence / re.sub(r'[^A-Za-z0-9_-]+', '-', captured['media_title']).strip('-')
                    source = root / 'episode.en.srt'
                    source.write_bytes((folder / 'source.en.srt').read_bytes())
                    target = root / 'episode.sv.srt'
                    donors = []
                    for entry in records['quarantine_attempts']:
                        if entry['item_id'] != captured['item_id']:
                            continue
                        artifact = folder / f"attempt-{entry['attempt_number']:02d}-id-{entry['id']}.sv.srt"
                        donors.append({'id': entry['id'], 'attemptNumber': entry['attempt_number'],
                            'sourceHash': file_sha256(source), 'targetHash': file_sha256(artifact),
                            'sourceLanguage': 'en', 'targetLanguage': 'sv', 'artifactPath': str(artifact),
                            'createdAt': entry['created_at'], 'validatorVersion': 'old-v4', 'configFingerprint': 'old-config'})
                        store.record_quarantine_attempt(item_type='episodes', item_id=captured['item_id'], target_language='sv',
                            source_hash=file_sha256(source), target_hash=file_sha256(artifact), attempt_number=entry['attempt_number'],
                            artifact_path=artifact, report_path=None, failure_rules=['copied_source'], cue_signatures=[], created_at=entry['created_at'])
                    store._connection.execute("UPDATE quarantine_attempts SET validator_fingerprint='old-v4',config_fingerprint='old-config'")
                    plan, _ = store.schedule_retry_plan(item_type='episodes', item_id=captured['item_id'],
                        target_language='sv', source_language='en', source_hash=file_sha256(source),
                        source_path=source, target_path=target, series_key='sonarr:101', media_title=captured['media_title'],
                        failure_class='whole_file', rules=['copied_source'], state='regeneration_waiting', eligible_completed_cycle=0)
                    with patch.object(app, '_get_validation_state', return_value=store), \
                         patch.object(app, '_get_cleanup_detector', return_value=detector), \
                         patch.object(app, 'lingarr_translate_line', side_effect=AssertionError('AI forbidden')), \
                         patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
                         patch.multiple(app, CLEANUP_LANGUAGES=['et'], CLEANUP_ROOTS=[root], CLEANUP_REPAIR_ENABLED=False,
                                        CLEANUP_QUARANTINE_DIR=root/'quarantine', STATUS_MANUAL_ACTIONS_ENABLED=True,
                                        shutdown_requested=False, _completed_cycle=10):
                        scheduling._run_quarantine_recoveries({})
                        current = store.retry_plan(plan['id'])
                        if target.exists():
                            successes.append(captured['item_id'])
                            self.assertEqual(current['state'], 'accepted_after_donor_recovery')
                        else:
                            reviews.append(captured['item_id'])
                            self.assertEqual(current['lastDeferralClass'], 'manual_review')
                            before = current['updatedAt']
                            scheduling._run_quarantine_recoveries({})
                            self.assertEqual(store.retry_plan(plan['id'])['updatedAt'], before)
                            service = build_manual_review_service(store)
                            details = service.review_cues(plan['id'])
                            self.assertTrue(details['items'])
                            # One exact decision at a time, followed by full scheduled validation.
                            while not target.exists():
                                details = service.review_cues(plan['id'])
                                cue = next(c for c in details['items'] if c['canApproveName'])
                                payload = {k: details[k] for k in ('expectedUpdatedAt','approvalRevision','sourceHash','candidateHash')}
                                payload.update(action='approve_name', cueNumber=cue['cueNumber'], targetCueHash=cue['targetCueHash'])
                                status, result = service.perform_name_action(plan['id'], payload)
                                self.assertEqual((status, result['outcome']), (202, 'queued'))
                                with self.assertRaises(ManualReviewConflict):
                                    service.perform_name_action(plan['id'], payload)
                                scheduling._run_quarantine_recoveries({})
                            self.assertEqual(store.retry_plan(plan['id'])['state'], 'accepted_after_donor_recovery')
                            snapshot = store.name_approval_snapshot(plan)
                            self.assertTrue(snapshot['pairs'])
                            self.assertEqual(store.name_approval_snapshot({**plan,'canonicalSeriesKey':'sonarr:999'})['pairs'], [])
                            self.assertEqual(store.name_approval_snapshot({**plan,'targetLanguage':'et'})['pairs'], [])
                            revocation = {'action':'revoke_name', 'expectedUpdatedAt':store.retry_plan(plan['id'])['updatedAt'],
                                'approvalRevision':snapshot['revision'], 'approvalId':snapshot['approvals'][0]['id']}
                            service.perform_name_action(plan['id'], revocation)
                            self.assertEqual(store.name_approval_snapshot(plan)['revision'], snapshot['revision']+1)
                            published_hash = file_sha256(target)
                            store.prune_older_than(0)
                            self.assertEqual(file_sha256(target), published_hash)
                            self.assertGreaterEqual(store._fetchone('SELECT COUNT(*) FROM name_approval_events')[0], 2)
                finally:
                    store.close()
        self.assertEqual(set(successes), {28461, 28467, 12142})
        self.assertEqual(set(reviews), {12156, 12157})


if __name__ == '__main__':
    unittest.main()
