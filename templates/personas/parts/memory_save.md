## Memory-save before done
Before claiming done, write STATUS.md and DECISIONS.tsv (and HANDOFF.md if work continues) in
`.orche/div/{{NAME}}/`, and save new lessons to your agent memory. Append decisions with `orche decide`.
Never claim done, and never `/clear`, until this ritual is complete; the Stop hook blocks a
completion claim whose STATUS.md was not updated in this session.
