## Report format
REPORT.md, ≤200 words: the first line is `STATUS: done | blocked | failed | stop`
(the Stop hook checks `STATUS: done` and `STATUS: release-ready` claims against the ledger), then
the result in one plain sentence a stranger understands (also your `--summary`'s first line), branch,
head SHA, PR, verdict, commands actually run with results, deviations,
`Decisions needed → default applied` (each with your recommendation), follow-ups,
and `memory-save: STATUS.md, DECISIONS.tsv[, HANDOFF.md] updated`. Label anything unverified.
