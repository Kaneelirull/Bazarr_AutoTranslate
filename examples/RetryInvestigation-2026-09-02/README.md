# Retry investigation — 2026-09-02

Investigation only. No application code, server settings, retry state, or media files were changed. Server access was read-only; diagnostic recovery ran on disposable local copies without AI calls.

## Result

The retries are failing for several different reasons. The strongest finding is a publication bug: four of the five selected episodes reached the point of publishing a recovered subtitle, then failed moving it from `/tmp` to `/media` with `[Errno 18] Invalid cross-device link`. This happened 13 times in the captured repair-job history. The recovery result is deleted and its input set is marked attempted, leaving full regeneration queued.

There are also false-positive copied-source findings on names/product names, historical donors excluded by validator/configuration fingerprints, and a Swedish cleanup-scope mismatch that bypasses immediate cue repair.

## Sample and evidence

Five entries were selected using PowerShell `Get-Random -Count 5` from the screenshot's ten rows. All five happened to be Swedish. Snapshot captured at approximately 2026-09-02 19:39 UTC; the queue had progressed since the screenshot.

| Episode | Displayed retries at capture | Archived attempts | Server recovery outcome | Offline reuse of all captured attempts |
| --- | ---: | ---: | --- | --- |
| Key & Peele S05E04 | 4 | 5 | 4 publication failures | First attempt passes after existing format normalization; no AI required |
| Key & Peele S05E10 | 4 | 5 | 3 publication failures | Replacing cue 304 from an older donor produces a valid file; no AI required |
| Shameless S08E08 | 3 | 4 | 3 publication failures | Combining attempts 69 and 83 produces a valid file; no AI required |
| Shameless S09E11 | 3 | 4 | 3 publication failures | Donors resolve formatting damage; cue 623 remains classified as copied source |
| Shameless S09E10 | 3 | 4 | 3 partial/no-progress recovery runs | Donors resolve other damage; cue 723 remains classified as copied source |

Copied 5 English sources, 22 quarantined Swedish subtitles, and 22 validation receipts. All 27 subtitle hashes match the database. All five expected current Swedish targets were absent. `manifest.json` records paths, hashes, and missing targets. `records.json` contains only selected episode records and linked events/results, plus non-secret state metadata. SQLite records were exported in a read-only transaction; files/logs/status were read afterward, so this is not an atomic snapshot of every server file.

`logs-selected.txt` preserves original log filenames and line numbers, with adjacent context. `startup-settings.txt` contains relevant startup settings. `analysis.json` contains per-attempt validation and donor-only results. `capture.py` documents collection; `analyze.py` reproduces offline analysis.

## 1. Recovered files fail at publication

Representative server evidence, all from `bazarr-autotranslate-2026-09-02.log`:

- Line 4058, plan 126 / S05E04: `Invalid cross-device link`, `/tmp/...recovery.h92j1r1o.srt` to `/media/...FLUX.sv.hi.srt`.
- Line 1751, plan 129 / S05E10: same exception.
- Line 2016, plan 134 / S08E08: same exception.
- Line 3114, plan 135 / S09E11: same exception.

The code path in local `main` (`f4021fc`) explains the server errors:

1. `docker/autotranslate/scheduling/runtime.py:117` and `:150` create recovery candidates with `same_directory=False`.
2. `docker/autotranslate/subtitles/workflow.py:601` consequently uses the default temporary directory, observed as `/tmp` in production.
3. After successful validation/repair, `scheduling/runtime.py:171` publishes the candidate through `_replace_managed_file_if_current`.
4. `subtitles/workflow.py:639` calls `os.replace(candidate_path, target)`. The observed mounts cannot support this rename across filesystems.
5. The exception path deletes the candidate (`:642`); the ensemble job becomes `failed` / `ensemble_exception` (`scheduling/runtime.py:198`).
6. `scheduling/runtime.py:228` skips an already-attempted donor set. `persistence/repairs.py:98` treats any matching job record as attempted, including failed jobs. A new whole-file attempt changes the set and triggers another recovery, which can hit the same publication failure.

Reaching this publication call means the recovery code considered the candidate valid. Therefore the four examples are not simply failing to assemble enough good cues: production has already reached an acceptable recovered result and then lost it at publication. This is established by server logs and the call path; no server write was performed to reproduce it.

## 2. Names and product names are rejected as copied English

Offline normalization and donor-only recovery leave these exact blockers:

- **Shameless S09E10, cue 723:** English source `Guacamole, mountain dew,`; all four attempts contain `Guacamole, Mountain Dew.` or the same words with an ellipsis. Validation returns `copied_source` at 100% normalized similarity. The live partial records also identify cue 723 as unresolved.
- **Shameless S09E11, cue 623:** English source `Alexandra galvez. G-a-I-v-e-z.`; all four attempts contain `Alexandra Galvez. G-A-I-V-E-Z.`. Validation again returns `copied_source`. The saved partial record identifies cue 623 as unresolved. Production later progressed to publication on this episode, so this donor-only result does not imply that every live AI repair failed.

These are strong false-positive cases: preserving a name, its spelling, and a food/product-name list is reasonable. Identical text across retries is not itself proof of untranslated prose.

`subtitles/foundation.py:946` enables copied-source repair once both normalized strings have at least 20 characters and similarity is at least 0.92. `subtitles/copied_source.py:119` determines whether the **source** looks like an entity using capitalization-sensitive token checks. Lowercase `galvez`, `mountain`, and `dew` prevent that exemption. The fallback at `:194` classifies the copy as prose. Repeating generation or selecting a different donor cannot resolve a rule that rejects the same reasonable wording in every attempt.

## 3. Older attempts are excluded before fresh validation

The 25 captured donor-rejection events all say `incompatible_validator`. This label covers either validator-version or configuration-fingerprint mismatch (`scheduling/runtime.py:94`). Captured attempts span validators `source-aware-v4-completeness-provenance` and `source-aware-v5-managed-language-validation`, and configuration fingerprints `7955705ccaf02365` and `cb4d69f935b487ec`.

Consequently the dashboard's 4–5 archived attempts are not necessarily 4–5 usable donors. For example, S05E04's first attempt (49) passes current local default validation after existing normalization but is rejected by the live fingerprint gate. S08E08's attempts 69/83 combine successfully offline, but both have been rejected under the newer configuration. This is a conservative compatibility policy, not evidence that their subtitle content is unusable. The exact setting responsible for the fingerprint change was not retained in these records.

## 4. Swedish takes a different immediate-validation path

The latest startup log explicitly lists `Languages: en, et, sv` and `Cleanup languages: et`. At `subtitles/workflow.py:1403`, Swedish therefore takes target-only validation. On an invalid Lingarr output it goes directly to quarantine and schedules regeneration at `:1456`, before the later source-aware format/cue-repair path.

All 22 original validation receipts record `repairAttempts: 0`. Their initial failures are malformed SRT blocks, excessive lines, and provider commentary/instructions mixed into dialogue. Thus the displayed 3–4 retries are whole-file retries, not evidence that each failing cue received 3–5 repairs on every attempt. Separate ensemble recovery does later call cue repair, as the production publication failures and partial records demonstrate.

## Verification limits

Offline analysis used local `main` at `f4021fc`, the repository `.venv`, the current default source-aware validation thresholds, and all captured donors, including historically incompatible fingerprints. This deliberately tests whether usable material exists; it does not claim exact equivalence to every historical deployment setting. Three reconstructed results passed the current validator without AI; two had the copied-source blockers above. Passing automated validation is not a full human review of Swedish translation quality. No reconstructed subtitle was retained or published.

No fixes, commits, or deployments were made.
