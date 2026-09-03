"""Read selected TrueNAS retry evidence; write only into this local folder."""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REMOTE = Path(r"\\BIGBOI-TRUENAS\config\bazarr-autotranslate-config\state")
IDS = [28461, 28467, 12142, 12156, 12157]

def save(name, value):
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    db = sqlite3.connect("file:////BIGBOI-TRUENAS/config/bazarr-autotranslate-config/state/bazarr-autotranslate.sqlite3?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    evidence = {}
    schema = {}
    for table in tables:
        cols = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
        schema[table] = cols
        if "item_id" in cols and "target_language" in cols:
            evidence[table] = [dict(r) for r in db.execute(f'SELECT * FROM "{table}" WHERE item_id IN (28461,28467,12142,12156,12157) AND target_language=?', ("sv",))]
    plans = [r['id'] for r in evidence['retry_plans']]
    artifacts = [r['id'] for r in evidence['subtitle_artifacts']]
    for table, key, values in [('recovery_stage_events', 'retry_plan_id', plans), ('retry_admission_events', 'retry_plan_id', plans), ('validation_results', 'artifact_id', artifacts)]:
        evidence[table] = [dict(r) for r in db.execute(f'SELECT * FROM "{table}" WHERE {key} IN ({",".join("?" for _ in values)})', values)]
    evidence['state_metadata'] = [dict(r) for r in db.execute('SELECT * FROM state_metadata')]
    db.rollback()
    db.close()
    save("schema.json", schema)
    save("records.json", evidence)
    status = json.loads((REMOTE / "status.json").read_text(encoding="utf-8"))
    save("status-selected.json", {"generatedAt": status.get("generatedAt"), "service": status.get("service"), "completedCycle": status.get("completedCycle"), "retryPlans": [p for p in status.get("retryPlans", []) if p.get("itemId") in IDS and p.get("targetLanguage") == "sv"]})
    print(json.dumps({k: len(v) for k, v in evidence.items()}))
    def remote_path(path):
        if path.startswith('/media/'):
            return Path(r'\\BIGBOI-TRUENAS\media') / path[len('/media/'):]
        if path.startswith('/config/'):
            return REMOTE / path[len('/config/'):]
        raise ValueError(path)
    manifest = {'capturedAt': datetime.now(timezone.utc).isoformat(), 'selection': 'Five entries chosen with PowerShell Get-Random -Count 5 from the ten screenshot rows; all target sv.', 'files': []}
    def capture(path, dest, expected=None):
        source = remote_path(path)
        entry = {'remotePath': path, 'localPath': dest.as_posix(), 'expectedHash': expected}
        try:
            data = source.read_bytes()
            entry.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
            if expected is not None:
                entry['hashMatches'] = entry['sha256'] == expected
            (ROOT / dest).parent.mkdir(parents=True, exist_ok=True)
            (ROOT / dest).write_bytes(data)
        except OSError as exc:
            entry['error'] = str(exc)
        manifest['files'].append(entry)
    for plan in evidence['retry_plans']:
        folder = Path(re.sub(r'[^A-Za-z0-9_-]+', '-', plan['media_title']).strip('-'))
        capture(plan['source_path'], folder / 'source.en.srt', plan['source_hash'])
        capture(plan['target_path'], folder / 'current.sv.srt')
        for attempt in evidence['quarantine_attempts']:
            if attempt['item_id'] != plan['item_id']:
                continue
            name = f"attempt-{attempt['attempt_number']:02d}-id-{attempt['id']}"
            capture(attempt['artifact_path'], folder / (name + '.sv.srt'), attempt['target_hash'])
            capture(attempt['report_path'], folder / (name + '.validation.json'))
    save('manifest.json', manifest)
    plan_pattern = '|'.join(str(p) for p in plans)
    attempt_pattern = '|'.join(str(a['id']) for a in evidence['quarantine_attempts'])
    pattern = re.compile(r'S05E04|S05E10|S08E08|S09E10|S09E11|(?:plan|Plan) (?:' + plan_pattern + r')\b|donor attempt (?:' + attempt_pattern + r')\b')
    logs = []
    for log in sorted((REMOTE / 'logs').glob('*.log')):
        if log.name < 'bazarr-autotranslate-2026-08-24.log':
            continue
        lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
        keep = set()
        for index, line in enumerate(lines):
            if pattern.search(line):
                keep.update(range(max(0, index - 1), min(len(lines), index + 3)))
        logs.extend(f'{log.name}:{i+1}: {lines[i]}' for i in sorted(keep))
    (ROOT / 'logs-selected.txt').write_text('\n'.join(logs) + '\n', encoding='utf-8')
    print('Copied files:', sum('sha256' in f for f in manifest['files']), 'Missing:', sum('error' in f for f in manifest['files']), 'Hash mismatches:', sum(f.get('hashMatches') is False for f in manifest['files']))
