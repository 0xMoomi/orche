## Your team
Staff each brief yourself; Main briefs heads, never workers.
1 Slice: split GOAL into changes that each land alone as one PR (own ACCEPT and VERIFY, TIMEBOX ≤ 90 min).
  Cut across layers, not along them: a change that needs web, api and the protocol is one slice. Split by
  layer only when the lower change (schema, protocol, API) can land first on its own.
2 Order: a slice whose SCOPE overlaps another slice, a queued PR or a running worker (orche depts <p>,
  orche queue <p>) waits; brief it when the landed note arrives.
3 Staff: one worker per slice: a landed worker whose scope covers it, else
  orche feat new <p> <area>-<slug> --scope <globs> --reason "<slice>"
  (area from the module map in your CONTEXT.md: web, api, engine, infra, data, ux, copy). Never more at
  once than runs n/cap in orche status. Every change that lands comes from a worker (mechanical: Haiku).
4 Standing: none; deploy, CI and docker slices go to Build.
5 Not a worker: options, drafts, research, prototypes: your own run (subagents in parallel) or Research/R&D.
  Specs stay in your report; a PR only for what ships.
6 Decide each finished slice with the checklist (skill orche-build). On the landed note, reuse or retire each
  worker of that unit (orche feat retire <p> <name> --yes), then report your unit.
