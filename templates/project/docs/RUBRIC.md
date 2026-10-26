# RUBRIC: {{PROJECT}}

Criteria the separate-context grader checks against the diff, ACCEPT and VERIFY output.
Each line must be checkable as pass/fail from those inputs alone; no taste calls.
Grader: at most 3 iterations per unit, then `verifier-failed` and an urgent pointer to Main.

- [ ] Every ACCEPT item in the brief is met and points at the line, test or output that shows it.
- [ ] The VERIFY command ran on the final head SHA and its output is saved as ledger evidence.
- [ ] No file outside the unit's SCOPE globs changed.
- [ ] New or changed behavior has a test that fails without the change.
- [ ] No secrets, `.env*` contents, or absolute home-directory paths were added.
