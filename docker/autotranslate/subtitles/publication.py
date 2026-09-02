"""Journaled publication of validated candidates; retries never call a provider."""
from __future__ import annotations

import os
import json
import uuid
import shutil
import tempfile
from pathlib import Path

from .foundation import file_sha256, normalize_managed_file


class PublicationDeferred(OSError):
    pass


def publish_journaled(record: dict, store, *, completed_cycle: int, lock) -> bool:
    target = Path(record['target_path'])
    candidate = Path(record['candidate_path'])
    stage = None
    with lock:
        if record.get('state') == 'manual_review' or record.get('eligible_cycle', 0) > completed_cycle:
            return False
        try:
            source = record.get('source_path')
            if source and record.get('source_hash') and file_sha256(source) != record['source_hash']:
                store.finish_publication(record['id'], 'superseded')
                return False
            if file_sha256(candidate) != record['candidate_hash']:
                raise OSError('retained recovery candidate hash mismatch')
            # A previous rename can have succeeded before the database commit failed.
            if target.is_file() and file_sha256(target) == record['candidate_hash']:
                store.finish_publication(record['id'], 'published')
                return True
            current = file_sha256(target) if target.exists() else None
            if current != record.get('expected_target_hash'):
                store.finish_publication(record['id'], 'superseded')
                return False
            previous_stage = record.get('stage_path')
            if previous_stage:
                old = Path(previous_stage)
                if old.parent == target.parent and old.name.startswith('.autotranslate-publish-'):
                    old.unlink(missing_ok=True)
            stage = target.parent / f'.autotranslate-publish-{uuid.uuid4().hex}.srt'
            store.stage_publication(record['id'], stage)
            with stage.open('xb') as handle:
                with candidate.open('rb') as reader:
                    shutil.copyfileobj(reader, handle)
                handle.flush()
                os.fsync(handle.fileno())
            normalize_managed_file(stage)
            if file_sha256(stage) != record['candidate_hash']:
                raise OSError('publication staging hash mismatch')
            if (source and record.get('source_hash') and file_sha256(source) != record['source_hash']) or (file_sha256(target) if target.exists() else None) != current:
                store.finish_publication(record['id'], 'superseded')
                return False
            os.replace(stage, target)
            stage = None
            if os.name == 'posix':
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            store.finish_publication(record['id'], 'published')
            return True
        except OSError as exc:
            code = f'publication failed: {type(exc).__name__} (errno {exc.errno})'
            store.fail_publication(record['id'], completed_cycle, code)
            raise PublicationDeferred(code) from exc
        finally:
            if stage is not None:
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    pass


def retain_publication(store, candidate, target, *, source_path, source_hash, expected_target_hash, payload, expected_candidate_hash=None) -> dict:
    existing = store.publication_for_target(target)
    if existing:
        return existing
    expected_candidate_hash = expected_candidate_hash or file_sha256(candidate)
    directory = store.path.parent / 'recovery'
    retained = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix='publication-', suffix='.srt', dir=directory, delete=False) as handle:
            retained = Path(handle.name)
            with Path(candidate).open('rb') as reader:
                shutil.copyfileobj(reader, handle)
            handle.flush()
            os.fsync(handle.fileno())
        digest = file_sha256(retained)
        if digest != expected_candidate_hash or file_sha256(candidate) != expected_candidate_hash:
            raise ValueError('candidate changed during publication preparation')
        receipt = dict(target=str(target), candidate=str(retained), candidate_hash=digest,
            source_path=str(source_path) if source_path else None, source_hash=source_hash, expected_target_hash=expected_target_hash, payload=payload)
        with retained.with_suffix('.json').open('x', encoding='utf-8') as handle:
            json.dump(receipt, handle)
            handle.flush()
            os.fsync(handle.fileno())
        store.record_publication(**receipt)
    except OSError:
        # If copying into the state directory fails, journal the already-written
        # input itself. The coordinator must retain that path until finalization.
        if retained is not None and retained.exists():
            retained.unlink(missing_ok=True)
        if file_sha256(candidate) != expected_candidate_hash:
            raise ValueError('candidate changed during publication preparation')
        store.record_publication(target=target, candidate=candidate, candidate_hash=expected_candidate_hash,
            source_path=source_path, source_hash=source_hash, expected_target_hash=expected_target_hash, payload=payload)
        return store.publication_for_target(target)
    except Exception:
        # A durable receipt allows restart to recover work even if SQLite was unavailable.
        if retained is not None and not retained.with_suffix('.json').exists():
            retained.unlink(missing_ok=True)
        raise
    return store.publication_for_target(target)


def reconcile_publication_receipts(store):
    directory = store.path.parent / 'recovery'
    for receipt in directory.glob('publication-*.json'):
        try:
            data = json.loads(receipt.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or 'candidate' not in data:
                continue
        except (OSError, ValueError):
            continue
        candidate = Path(data['candidate'])
        if candidate != receipt.with_suffix('.srt') or not candidate.is_file():
            continue
        if store.publication_receipt_known(candidate):
            continue
        if file_sha256(candidate) == data['candidate_hash']:
            store.record_publication(**data)


def retain_partial_text(destination, raw):
    """Persist reconstructed progress before a coordinator releases its job."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
