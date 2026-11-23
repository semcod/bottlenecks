# Ticket 006: Analyze Planfile delivery evidence for Monag and Reflex

- **ID**: ticket-006
- **Owner**: unresolved:human
- **Status**: IN_PROGRESS
- **Workflow state**: EDIT
- **Created**: 2026-11-23

## Goal and scope

Analyze pending tickets in native Planfile and Monag JSON and emit bounded evidence for Reflex. Session execution authorized by the workspace owner under PLF-25979.

## Acceptance criteria

- [x] AC-01: Verify repeated titles and blocked states against actual tool exports and pass the resulting observations through Reflex.

## Operator verification

Run these read-only projections against local ticket, queue and workspace exports.
The Monag resume export covers current local evidence. Its rows do not contain a
creation time. Use the Planfile ticket list export for a time-bounded cohort.

```bash
planfile ticket list --sprint all --format json > tickets.json
bottlenecks dsl 'delivery.planfile source=tickets.json repository=owner/repo since=2026-11-17T00:00:00+01:00' --format json
planfile executable-queue --sprint all --format json > queue.json
bottlenecks dsl 'delivery.planfile source=queue.json' --format json
monag --root "$HOME/github" --json resume > resume.json
bottlenecks dsl 'delivery.planfile source=resume.json' --format json | jq -c '.data.items[].metrics.reflex' > delivery.jsonl
reflex analyze --source delivery.jsonl --min-repeat 2
monag --root "$HOME/github" --json advise --reflex-source delivery.jsonl
```

A matching title identifies records to compare. A ticket state supplies no
independent proof of a merge. Confirm acceptance and the exact upstream
revision before reconciling delivery. The Reflex projection retains distinct
ticket counts and scopes failure fingerprints to one repository.

## Tracking boundary

This directory contains the minimal reviewed intent. Optional participant prose
and raw command logs are not required delivery output.

<!-- ticket-radar:start -->
## Ticket Radar

Initial planned-scope estimate; advisory only, not execution or merge approval.

| Measure | Initial assessment |
| --- | --- |
| Impact score | 52/100 |
| Complexity | M |
| Estimated effort | 54 min |
| Planning budget | 30 min |
| Split recommended | yes |

Diagnostics: RADAR-ACCEPTANCE-MISSING, RADAR-BUDGET-EXCEEDED.
<!-- ticket-radar:end -->
