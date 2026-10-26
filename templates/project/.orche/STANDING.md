# STANDING ORDERS — {{PROJECT}}

<!--
R-STANDING-1: an instruction written twice becomes a numbered line here.
Every dispatch brief starts with this file, so a standing order reaches every unit without
being repeated. Append with `orche hr correct {{PROJECT}} --text "..."`; never renumber or
edit old lines (strike one out with a new line that supersedes it by number).
`orche hr audit` flags lines added more than once in a week as candidates for structural
enforcement (a hook, a gate, a test) instead of another order.
-->

1. No direct push to `main`.
2. Work on a feature branch (`feat/*`) → published PR → head review → the merge queue lands it.
3. One push per unit, after local verification.
4. Docs-only commits include `[skip ci]`.
5. Tags, releases and deploys require user approval.
6. Externals auto-accept list: (empty) — every external request needs confirmation.
