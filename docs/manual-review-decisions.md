# Manual review decisions

Manual Review presents one retained subtitle candidate at a time. Select an episode, then decide each reported cue:

- **Approve cue** accepts only the listed text-quality findings for that exact cue and candidate evidence.
- **Try again** asks recovery to repair that cue while preserving approved and unaffected cues.
- **Remember this exact phrase** is optional for copied-text findings and is scoped to the same series or item and language pair.
- **Finish review** becomes available when every cue has a decision. It queues normal scheduler recovery and full-file validation.
- **Ignore** retains the review and candidate without publishing or scheduling work. **Reopen review** refreshes it for later decisions.

Structural, timing, alignment, completeness, source-identity, and publication checks cannot be approved. If the candidate cannot be aligned safely, the UI shows a file-level recovery requirement instead of cue actions.

Schema v19 stores cue decisions, a per-review revision, and a durable idempotency ledger. Action requests carry an idempotency key plus the expected source, candidate, cue, and revision evidence. Repeating the same request is safe; stale evidence or reusing a key for different evidence receives HTTP 409. Validation and recovery consume immutable approval snapshots. Exact cue hashes prevent an approval from carrying to changed dialogue.

Before deployment, back up the SQLite state database. Rolling back to an earlier image requires restoring the matching pre-v19 database backup.
