# Ticket 007: Report duration coverage of real URTLog observations (Heyasa PLF-29710)

- **ID**: ticket-007
- **Owner**: agent:codex
- **Status**: IN_PROGRESS
- **Workflow state**: EDIT
- **Created**: 2026-11-23

## Goal and scope

Report how many parsed URTLog records can support duration measurements. Separate point observations, malformed ranges, non-LOG kinds and operations outside the requested prefix. Preserve existing timing and lineage results.

## Acceptance criteria

- [x] AC-01: The real CLI distinguishes an empty set from a populated point-only sample and reports usable ranges separately from missing or invalid ones.

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
