# Verification and final review

Date: 2026-09-03.
Branch: `codex/repair-recovery-and-name-review`.
Base commit: `f4021fca6e18d6e8186e1c1e3e53639c233be635`.

## Test gates

The test-runner workflow was used for focused regressions, production scheduler replay, the complete Python suite, frontend checks and browser verification. Linux runs used the existing local test image `bazarr-autotranslate:reconcile-maintenance-rc`, overridden to execute the current repository mounted read-only at `/work`, with `--network none`. No production container or share was modified.

| Gate | Result |
| --- | --- |
| Complete Python suite in Linux | 360 tests passed, no skips (57.148 seconds) |
| Complete Python suite as non-root Linux UID/GID 1001 | 360 tests passed, no skips (66.045 seconds), networking disabled |
| TypeScript typecheck | Passed |
| Frontend production build | Passed |
| Vitest | 23 tests passed in 4 files |
| Playwright | 20 checks passed across desktop/mobile and dark/light themes |
| Whitespace/error check | Application, tests and docs passed; captured SRT fixtures retain their original whitespace |

The Linux publication test asserts different `st_dev` values for its retained candidate and `/dev/shm` destination. Tests inject rename, permission, disk-full, journal-registration and finalization errors; verify candidate retention, source/target compare-and-swap behavior, corruption rejection, bounded publication retries, and recovery after reopening SQLite. Scheduler tests assert zero AI requests and unchanged translation retry counters, including manual resumption after the third filesystem failure.

The captured-example replay runs the real quarantine scheduler against a temporary SQLite database with AI and socket connections forbidden. Historical donor fingerprints are intentionally different from current policy. Results:

| Example | Offline outcome |
| --- | --- |
| Key & Peele S05E04 | Published through recovery without AI |
| Key & Peele S05E10 | Published using validated donors without AI |
| Shameless S08E08 | Published using validated donors without AI |
| Shameless S09E11 | Held for cue review, then published after exact approval |
| Shameless S09E10 | Held for cue review, then published after exact approval |

Additional coverage includes stale-source, wrong-language, corrupted and mistimed donors; Swedish recovery with `CLEANUP_LANGUAGES=et`; unchanged external-library cleanup behavior; copied-prose rejection; spelling evidence; exact Unicode/whitespace normalization; item/series and both language boundaries; approval transaction rollback; source/candidate/revision conflicts; permanent approval/revocation audit history; cache invalidation; restart-stable holds; and saving accepted cue progress during cancellation. Existing migration tests plus a v17-to-v18 upgrade test check preserved plans, schema version and foreign-key integrity.

UI tests cover explicit source/translation text, preserved line breaks, escaped markup, adjacent cues, unavailable candidates, disabled actions, stale evidence, keyboard approval, announced results and responsive layout. New comparison snapshots were inspected visually. Windows and Linux name-review baselines cover all four theme/viewport combinations. The mobile Manual Review baselines on both platforms were updated for the revised page description/footer; unrelated baselines continued to pass.

CI follow-up found that the initial browser run updated only Windows snapshots.
The six affected Linux baselines were generated with Playwright 1.55.1 on Ubuntu
Noble, visually inspected, then verified without snapshot updates: all 20 checks
passed (12.2 seconds). Snapshot paths include the operating system; updating
Windows baselines does not update Linux CI expectations. Generate changed
baselines on each supported platform with the locked Playwright version and
always rerun without `--update-snapshots`.

The next CI step exposed tests assuming root privileges to assign temporary
files to TrueNAS UID/GID 568. Recovery test fixtures now use the effective test
process UID/GID, preserving real `chown`, `chmod`, staging and rename operations.
The separate-filesystem test verifies resulting ownership and mode. Production
ownership remains 568:568, and ownership-contract and permission-failure tests
remain active. The correction changes test setup only; final review found no
production change or weakened filesystem-failure assertions.

Local detailed logs are in `.test-logs/` (ignored generated artifacts), notably `completion-gate.log`, `name-api-final.log`, `publication-reviewed.log`, and `shutdown-recovery.log`.

Before publishing the branch, the captured-example suite passed again using only
`replay-records.json` and the SRT fixtures. The replay metadata contains just the
required identifiers, titles and timestamps; raw server logs, settings, database
records and capture scripts remain local.

## Final code review

The code-reviewer workflow traced `docker/entrypoint.sh` → `Bazarr_AutoTranslate.py` → `autotranslate.app` → `production.load_runtime`, then the production validation, repair, scheduler, publication, persistence and Manual Review call paths. Review included changed files and callers, new modules, schema migration, retention references, shutdown, artifact-lock ordering, immutable worker approval data, HTTP safeguards and frontend DTOs.

The following findings were corrected and their affected tests rerun:

- **P1 — Approval cache identity:** `maintenance/runtime.py:210` and `subtitles/workflow.py:1545`. Cache dependencies and prepared-result checks now include scope as well as revision; validation records reject stale approval evidence.
- **P1 — Shutdown progress loss:** `subtitles/repair.py:66` and `subtitles/workflow.py:794`. Interrupted repair returns accepted cue progress, and coordinators flush retained candidates and saved cues before releasing the job. Completed candidates blocked by publication admission are retained as well.
- **P1 — Publication candidate identity:** `subtitles/publication.py:80` and `subtitles/workflow.py:652`. The candidate hash is bound to validation, checked through retention/staging, and checked even when reconciling an already matching target. Initial state-copy failure preserves the existing candidate through the journal.
- **P2 — Publication hold churn:** `persistence/publications.py:64`. The hold key is persisted before an unchanged hold can be skipped; publication holds are excluded from legacy reactivation.
- **P2 — Donor identity:** `subtitles/repair.py:185`. Reuse requires current source hash, target language, artifact hash and actual donor timestamp alignment, in addition to fresh validation.

Final re-review verdict: **no unresolved blocking findings**. No push or deployment was performed. These checks use local test files and Linux overlay/tmpfs mounts; they do not claim a live TrueNAS deployment or live provider test. Provider calls were intentionally disabled for captured evidence replay.

Before deployment, back up SQLite. Rollback requires the saved database and previous image together. Operational details and API contracts are in `retry-recovery-and-name-review.md`.
