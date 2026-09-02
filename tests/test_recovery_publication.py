"""Recovery publication contracts, including real separate Linux mounts when available."""
import errno
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'docker'))
from autotranslate.persistence.state_store import StateStore
from autotranslate.persistence.common import StateStoreError
from autotranslate.subtitles.foundation import file_sha256
from autotranslate.subtitles.publication import (
    retain_publication, publish_journaled, reconcile_publication_receipts, PublicationDeferred,
)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = StateStore(self.root / 'state.sqlite3', validator_version='v', config_fingerprint='c')
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.source = self.root / 'source.srt'
        self.source.write_bytes(b'source')
        self.input = self.root / 'candidate.srt'
        self.input.write_bytes(b'complete recovered subtitle')
        self.target = self.root / 'target.srt'

    def retain(self, expected=None):
        return retain_publication(self.store, self.input, self.target, source_path=self.source,
            source_hash=file_sha256(self.source), expected_target_hash=expected,
            payload={'targetLanguage': 'sv', 'sourceLanguage': 'en'})

    def publish(self, row, cycle=0):
        return publish_journaled(row, self.store, completed_cycle=cycle, lock=threading.RLock())

    def test_permission_disk_full_rename_failures_retain_and_bound_retries(self):
        row = self.retain()
        for cycle, code, due in [(0, errno.EACCES, 1), (1, errno.ENOSPC, 3), (3, errno.EXDEV, 5)]:
            with patch('autotranslate.subtitles.publication.os.replace', side_effect=OSError(code, 'failure')):
                with self.assertRaises(PublicationDeferred):
                    self.publish(row, cycle)
            row = self.store.publication_for_target(self.target)
            self.assertEqual(row['eligible_cycle'], due)
            self.assertTrue(Path(row['candidate_path']).is_file())
            self.assertFalse(self.target.exists())
        self.assertEqual(row['state'], 'manual_review')
        self.assertEqual(row['failure_count'], 3)
        self.assertEqual(self.store.retry_plans(), [])

    def test_target_absence_and_source_are_both_compare_and_swap_guards(self):
        row = self.retain()
        self.target.write_bytes(b'concurrent external subtitle')
        self.assertFalse(self.publish(row))
        self.assertEqual(self.target.read_bytes(), b'concurrent external subtitle')
        self.assertTrue(Path(row['candidate_path']).exists())
        self.target.unlink()
        row = self.retain()
        self.source.write_bytes(b'new source')
        self.target.write_bytes(self.input.read_bytes())
        self.assertFalse(self.publish(row), 'even matching target cannot mask a changed source')

    def test_interrupted_database_finalization_reconciles_without_retranslation(self):
        row = self.retain()
        with patch.object(self.store, 'finish_publication', side_effect=StateStoreError('disk unavailable')):
            with self.assertRaises(StateStoreError):
                self.publish(row)
        self.assertEqual(self.target.read_bytes(), self.input.read_bytes())
        self.assertTrue(Path(row['candidate_path']).exists())
        self.store.close()
        self.store = StateStore(self.root / 'state.sqlite3', validator_version='v', config_fingerprint='c')
        self.addCleanup(self.store.close)
        with patch('autotranslate.subtitles.publication.os.replace', side_effect=AssertionError('must reconcile')):
            self.assertTrue(self.publish(self.store.publication_for_target(self.target)))
        self.assertEqual(self.store.publication_for_target(self.target)['state'], 'published')

    def test_receipt_recovers_candidate_when_initial_journal_commit_fails(self):
        with patch.object(self.store, 'record_publication', side_effect=StateStoreError('sqlite unavailable')):
            with self.assertRaises(StateStoreError):
                self.retain()
        self.assertEqual(len(list((self.root / 'recovery').glob('publication-*.srt'))), 1)
        reconcile_publication_receipts(self.store)
        row = self.store.publication_for_target(self.target)
        self.assertIsNotNone(row)
        self.assertTrue(self.publish(row))
        reconcile_publication_receipts(self.store)
        self.assertEqual(len(self.store.pending_publications()), 1)

    def test_state_copy_failure_journals_existing_candidate_for_resume(self):
        with patch('autotranslate.subtitles.publication.shutil.copyfileobj', side_effect=OSError(errno.ENOSPC, 'full')):
            row = self.retain()
        self.assertEqual(Path(row['candidate_path']), self.input)
        self.assertTrue(self.input.exists())
        self.assertTrue(self.publish(row))

    def test_staging_persistence_failure_retains_original_and_candidate(self):
        self.target.write_bytes(b'original')
        row = self.retain(file_sha256(self.target))
        with patch.object(self.store, 'stage_publication', side_effect=StateStoreError('injected')):
            with self.assertRaises(StateStoreError):
                self.publish(row)
        self.assertEqual(self.target.read_bytes(), b'original')
        self.assertTrue(Path(row['candidate_path']).exists())
        self.assertTrue(self.publish(row))

    def test_candidate_changed_after_validation_cannot_enter_publication(self):
        validated_hash = file_sha256(self.input)
        self.input.write_bytes(b'changed since validation')
        with self.assertRaises(ValueError):
            retain_publication(self.store,self.input,self.target,source_path=self.source,
                source_hash=file_sha256(self.source),expected_target_hash=None,payload={},
                expected_candidate_hash=validated_hash)
        self.assertIsNone(self.store.publication_for_target(self.target))
        self.assertFalse(self.target.exists())

    def test_corrupt_retained_candidate_cannot_finalize_matching_target(self):
        row = self.retain()
        self.target.write_bytes(self.input.read_bytes())
        Path(row['candidate_path']).write_bytes(b'corrupted retained artifact')
        with self.assertRaises(PublicationDeferred):
            self.publish(row)
        self.assertEqual(self.store.publication_for_target(self.target)['state'], 'pending')

    @unittest.skipUnless(os.name == 'posix' and Path('/dev/shm').is_dir(), 'requires Linux /dev/shm separate mount')
    def test_real_separate_filesystems_stage_beside_destination(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm') as target_dir:
            self.target = Path(target_dir) / 'target.srt'
            self.assertNotEqual(self.input.stat().st_dev, self.target.parent.stat().st_dev)
            row = self.retain()
            self.assertTrue(self.publish(row))
            self.assertEqual(file_sha256(self.target), row['candidate_hash'])
            self.assertEqual(list(self.target.parent.glob('.autotranslate-publish-*')), [])


if __name__ == '__main__':
    unittest.main()
