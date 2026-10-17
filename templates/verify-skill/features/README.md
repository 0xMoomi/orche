# Feature map

One file per feature: `features/<feature>.md`. Keep each file short and executable by a fresh
session with no other context. Every file has exactly these sections:

```markdown
# <feature>

## Entry

URL, route, CLI command or API endpoint where the feature starts, plus required state
(signed-in user, seed record).

## Steps

1. Numbered, concrete actions (click/type/request), one per line.

## Expected

- Observable results that decide pass/fail (text on screen, status code, row in DB).

## Evidence

- Files this feature's run must leave under `evidence/<date>-<feature>/`.
```
