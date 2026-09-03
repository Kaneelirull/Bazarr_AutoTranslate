# Retry recovery and scoped name review

Implementation branch: `codex/repair-recovery-and-name-review`.
Base: `f4021fca6e18d6e8186e1c1e3e53639c233be635` (freshly fetched `origin/main`).

## Recovery

Generated subtitles with verified source provenance receive structural and cue recovery in every target language. `CLEANUP_LANGUAGES` still controls existing-library cleanup, and `CLEANUP_REPAIR_ENABLED` controls AI requests. Recovery normalizes safely, applies exact approvals, reuses saved cues and validated quarantine donors, then requests only unresolved repairable cues. Historical validator/configuration values remain audit evidence; donor contents must pass current checks.

The shared request adapter uses isolated requests, cancellation and durable rejected-output fingerprints. Two equivalent rejected responses exhaust that strategy, including responses recorded before restart. The configured total attempt ceiling still applies. Ambiguous blocking names and exhausted cues retain their best candidate for Manual Review.

Recovery outcome keys include policy `recovery-v2`, source hash, donor identities/hashes, validator/configuration, approval revision and explicit manual actions. This gives active jobs a fresh recovery opportunity after this upgrade without resetting translation counters. Dismissed and resolved jobs remain terminal. Review holds reopen only for relevant changed evidence/policy or explicit actions.

## Publication and restart

Completed candidates and a recovery receipt are flushed into `state/recovery`. SQLite journals their destination, expected source hash, expected target hash (NULL explicitly means absent), staging path, phase and retry schedule. Publication stages beside the destination, flushes and verifies its hash, rechecks source and target under the artifact lock, and performs a same-filesystem atomic replacement. Linux also flushes the destination directory.

Publication failures retain the candidate. Retries occur after one, then two additional completed cycles. Three failed attempts enter Manual Review with the filesystem error. Translation retry counters and AI requests are unaffected. **Queue manual retry** permits another publication attempt. A receipt can restore the journal if SQLite was unavailable during initial registration; a target matching the candidate reconciles a rename that completed before database finalization. Source/target conflicts supersede publication without overwriting the conflicting file.

Recovery candidates and active artifact references are retained while work is pending. Review candidates are independent of the canonical subtitle, so comparisons work when that subtitle is absent. Publication and approval commits serialize validation against revocation.

## Name decisions

Open **Recovery details → Cues needing review** for original/translated text, timestamp, rejection rules and adjacent cues. Subtitle markup is displayed as text. **Accept as name and remember** stores the exact source/target phrase pair and queues scheduler recovery; the complete file must still pass every other check.

Scope is the reliable canonical Sonarr series plus source/target languages. Movies and episodes without reliable series identity use item scope. Normalization is Unicode NFKC, case folding and whitespace collapsing; punctuation and token boundaries remain significant. Matching uses no substrings, regex, wildcards or cross-series learning. **Forget approval** increments the scope revision and invalidates future cached validation without changing already published subtitles.

URLs, models, acronyms and a name agreeing with its letter-by-letter spelling retain objective invariant handling. Ordinary copied sentences and near-copy prose remain blocking. Uncertain copied name phrases that meet the existing copied-cue threshold require review; shorter observations retain the existing minimum-length policy. No sampled dialogue is hard-coded.

## API and persistence

- `GET /api/manual-reviews/{id}/cues?page=1&pageSize=20` returns explicit dialogue details, adjacent cues, findings, candidate/source hashes, review timestamp, scoped approval revision and remembered decisions. Page size is capped at 100.
- `POST /api/manual-reviews/{id}/actions` accepts `approve_name` with `expectedUpdatedAt`, `approvalRevision`, `sourceHash`, `candidateHash`, `cueNumber`, `targetCueHash`; `revoke_name` takes `expectedUpdatedAt`, `approvalRevision`, `approvalId`.
- Clients submit identifiers and evidence tokens, never paths or replacement text. Existing action-enable, same-origin and action-header checks apply. Stale evidence returns HTTP 409. Approval returns HTTP 202 queued.
- Schema v18 adds scoped approvals/revisions, permanent approval/revocation events, publication metadata and stable recovery holds. Approval, audit, revision and recovery request commit together. Approval history is excluded from ordinary log/quarantine retention.
- CPU workers receive immutable approval pairs and a revision; they do not write SQLite or call AI. Both validation metadata and recovery deduplication consider approval state.

## Release and rollback

Deployment is separate from this change. **Back up SQLite before deployment.** Rollback requires restoring that backup together with the previous image; do not run an older image against the upgraded database.

Verification commands and final results are recorded in `retry-recovery-verification.md`. Captured investigation inputs remain in `examples/RetryInvestigation-2026-09-02`; tests never contact or modify the production shares.
