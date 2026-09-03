"""Offline diagnostics against copied evidence; no network or AI calls."""
import json
import re
import sys
import tempfile
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / 'docker'))
from autotranslate.subtitles.foundation import build_detector, recover_subtitle_pair, validate_subtitle_pair, target_language_for_code, file_sha256, source_cue_signatures, parse_srt_cues, validate_cue_pair
from autotranslate.subtitles.repair import repair_subtitle_file

records = json.loads((ROOT / 'records.json').read_text(encoding='utf-8'))
detector = build_detector()
output = []
for plan in records['retry_plans']:
    folder = ROOT / re.sub(r'[^A-Za-z0-9_-]+', '-', plan['media_title']).strip('-')
    source = folder / 'source.en.srt'
    entry = {'title': plan['media_title'], 'attempts': []}
    with tempfile.TemporaryDirectory() as temp:
        candidates = []
        for attempt in [a for a in records['quarantine_attempts'] if a['item_id'] == plan['item_id']]:
            artifact = folder / f"attempt-{attempt['attempt_number']:02d}-id-{attempt['id']}.sv.srt"
            receipt = json.loads(artifact.with_name(artifact.name.replace('.sv.srt', '.validation.json')).read_text(encoding='utf-8'))
            recovery = recover_subtitle_pair(source, artifact)
            result = {'id': attempt['id'], 'attemptNumber': attempt['attempt_number'], 'originalIssues': receipt['validation']['issues'], 'originalRepairAttempts': receipt['repairAttempts'], 'formatSafe': recovery.safe, 'formatReason': recovery.reason, 'formatRecoveredCues': recovery.recovered_cues, 'formatFixes': recovery.fixes}
            if recovery.safe and recovery.raw:
                candidate_path = Path(temp) / f"{attempt['id']}.srt"
                candidate_path.write_text(recovery.raw, encoding='utf-8')
                report = validate_subtitle_pair(source, candidate_path, detector, target_language_for_code('sv'), target_lang='sv')
                result['normalizedIssues'] = report.to_dict()['issues']
                if report.valid or report.repairable_cue_indexes:
                    candidates.append({**attempt, 'artifactPath': str(candidate_path), 'targetHash': file_sha256(candidate_path), 'cueSignatures': source_cue_signatures(source), 'attemptNumber': attempt['attempt_number'], 'createdAt': attempt['created_at'], 'report': report})
            entry['attempts'].append(result)
        if candidates:
            baseline = min(candidates, key=lambda a: (not a['report'].valid, len(a['report'].repairable_cue_indexes), len(a['report'].issues), -a['createdAt']))
            entry['allAttemptsBaseline'] = baseline['id']
            if baseline['report'].valid:
                entry['donorOnlyValid'] = True
            else:
                repaired = repair_subtitle_file(source, baseline['artifactPath'], detector, target_language_for_code('sv'), lambda *a: (_ for _ in ()).throw(AssertionError('AI forbidden')), target_lang='sv', donor_attempts=[c for c in candidates if c is not baseline], provider_enabled=False)
                entry.update(donorOnlyValid=repaired.success, donorOnlyRepaired=repaired.repaired_cues, donorOnlyUnresolved=repaired.unresolved_cues, donorOnlyIssues=repaired.report.to_dict()['issues'], donorHistory=repaired.donor_history)
                source_cues, _ = parse_srt_cues(source.read_text(encoding='utf-8-sig'))
                entry['unresolvedText'] = []
                for number in repaired.unresolved_cues:
                    src = next(c for c in source_cues if c.number == number)
                    texts = []
                    for candidate in candidates:
                        cues, _ = parse_srt_cues(Path(candidate['artifactPath']).read_text(encoding='utf-8-sig'))
                        cue = next(c for c in cues if c.number == number)
                        texts.append({'attempt': candidate['id'], 'target': cue.text, 'issues': [i.rule for i in validate_cue_pair(src, cue, cue_index=number-1, target_lang='sv')]})
                    entry['unresolvedText'].append({'number': number, 'source': src.text, 'targets': texts})
    output.append(entry)
    print(entry['title'], 'donorOnlyValid=', entry.get('donorOnlyValid'), 'unresolved=', entry.get('donorOnlyUnresolved'), flush=True)
(ROOT / 'analysis.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
