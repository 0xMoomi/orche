rev: 1
unit: <unit id>
to: <session>   from: <sender session>
worktree: <absolute path of the target's worktree>   branch: <branch>
project root: <main checkout path>   (brief + REPORT.md live under project root/.orche/units/<unit>/)
Work only inside `worktree`. SCOPE paths are relative to it.
<!--
Dispatch brief skeleton (one unit). Main sends it with `orche dispatch` (stdin or --file,
`---` between blocks); ORCHE stores it as <project root>/.orche/units/<unit>/BRIEF.md with the `rev:` line and the
header above on top (ORCHE writes the header; Main never does). STANDING.md and the persona
line follow, then `## Brief <unit>` and the fields.
Lines are `KEY<spaces>VALUE`; continue a value on the lines below it (indent them by two or more spaces).
Write each key once. A line starting with `#` is a comment, except inside an indented value, where it is content.
All fields below are required except ROLLBACK (mandatory when the unit deploys).
A correction is `AMEND <unit> rev N` + `SUPERSEDES rev N-1: <what>`; a halt is `STOP <unit>`.
-->
TO        <session: div-<project>-<division> | feat-<project>-<feature>>
GOAL      <one-sentence outcome a stranger could execute without chat history>
SCOPE     <writable paths> / <forbidden paths> / <worktree + branch>
CONTEXT   <Pointers (files, PRs, an upstream report in full) + the plan: the shape of the change, the order when it matters, decisions already fixed, constraints>
ACCEPT    <checkable criterion, one per line>
  <another criterion>
VERIFY    <exact command or committed verify-skill path> + <known pitfalls>
TIMEBOX   <approximate cap; on overrun report the partial result and stop>
FORBID    retarget PR, merge, rebase, force-push, out-of-scope edits + <unit-specific bans>
REPORT    status, branch, head SHA, PR, verdict, commands actually run, deviations, follow-ups, memory-save
TIER      <mechanical | standard | hard>
ROLLBACK  <how to revert; required when the unit deploys>
