# Ticket 012: Expose input and blocked execution states in the Reflex queue report (PLF-25983)

- **ID**: ticket-012
- **Owner**: agent:codex
- **Status**: IN_PROGRESS
- **Workflow state**: EDIT
- **Created**: 2026-11-24

## Goal and scope

The user asked to detect the actual anomalies through Reflex. A canonical Planfile read-back of 67 repositories showed three execution input waits and one explicit execution block missing after PR 12.

## Acceptance criteria

- Cover all four weekly observations, keeping blocked execution distinct from a blocked label.
- Preserve deduplication and ignore normal queued, running and completed execution states.

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
| Estimated effort | 51 min |
| Planning budget | 30 min |
| Split recommended | yes |

Diagnostics: RADAR-ACCEPTANCE-MISSING, RADAR-BUDGET-EXCEEDED.
<!-- ticket-radar:end -->
