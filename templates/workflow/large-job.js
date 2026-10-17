export const meta = {
  name: 'orche-large-job',
  description: 'One-off large job: fan out over a work list, verify each result, collect a summary',
  whenToUse: 'A single large sweep or migration that one context cannot hold; not the daily division flow',
  phases: [
    { title: 'Fan-out', detail: 'one agent per work item' },
    { title: 'Verify', detail: 'independent checks per result' },
    { title: 'Collect', detail: 'one summary for Main' },
  ],
}

// ORCHE large-job skeleton for the Claude Code Workflow tool.
//
// Use it for ONE-OFF large jobs only: a repo-wide audit, a mechanical migration across many files,
// a research sweep. The daily division flow (Main -> dispatch block -> division/feature unit ->
// inbox pointer) does not use workflows; keep using briefs for that.
//
// Shape: fan-out -> verify -> collect.
//   - `args` is the work list, e.g. ["src/a.ts", "src/b.ts"] (pass it as a JSON array).
//   - pipeline() runs every item through both stages with no barrier between them.
//   - The verify stage runs independent checkers in fresh contexts (writer != judge).
//   - The returned object is the job's REPORT; paste it into the unit's REPORT.md.
// Replace the TODO prompts; keep briefs short (<= 300 words) and VERIFY commands concrete.

const RESULT = {
  type: 'object',
  properties: {
    item: { type: 'string' },
    changed: { type: 'array', items: { type: 'string' } },
    commands: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
  required: ['item', 'changed', 'commands'],
}

const VERDICT = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    evidence: { type: 'string' },
  },
  required: ['ok', 'evidence'],
}

const LENSES = ['correctness', 'scope']

const items = Array.isArray(args) ? args : []
log(`large job over ${items.length} items`)

const results = await pipeline(
  items,
  item => agent(
    `TODO: describe the change for ${item}. Stay inside ${item}; run the VERIFY command; report files changed and commands run.`,
    { label: `do ${item}`, phase: 'Fan-out', schema: RESULT },
  ),
  result => parallel(LENSES.map(lens => () => agent(
    `Check this result through the ${lens} lens. You did not write it. Result: ${JSON.stringify(result)}. Default to ok=false when unsure.`,
    { label: `${lens} ${result.item}`, phase: 'Verify', schema: VERDICT },
  ))).then(verdicts => ({ ...result, verdicts: verdicts.filter(Boolean) })),
)

phase('Collect')
const done = results.filter(Boolean)
const verified = done.filter(r => r.verdicts.length === LENSES.length && r.verdicts.every(v => v.ok))
const failed = done.filter(r => !verified.includes(r)).map(r => r.item)
const dropped = items.length - done.length
if (dropped) log(`${dropped} items produced no result`)

return {
  total: items.length,
  verified: verified.map(r => r.item),
  failed,
  dropped,
  details: done,
}
