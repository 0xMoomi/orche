The queue merged your branch onto origin/{{BASE}} and the gate passed, but your head reviewed the change as
user-visible (`surface: user`) and the check script ended with `verdict: {{LEVEL}}` (full log {{LOG}}). A
user-visible change lands only when the check drives it on its real surface (the screen, the flow, the API response
or the CLI output a user sees) and ends with `verdict: live-ui-verified`. Make `scripts/verify/ci.sh` do that for
this change, run it, record the ledger row for the new HEAD and end STATUS: done. When that is outside your SCOPE, or
the project cannot drive its real surface here, end STATUS: blocked and name what is missing.
