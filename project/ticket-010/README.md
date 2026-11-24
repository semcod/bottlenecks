# Ticket 010: Report first URTLog check failures and verified recovery (PLF-25980)

- **ID**: ticket-010
- **Owner**: agent:codex
- **Status**: IN_PROGRESS
- **Workflow state**: EDIT
- **Created**: 2026-11-24

## Goal and scope

PLF-25980 under workspace PLF-25979. Diagnose explicit Reflex check episodes in the URTLog analysis of the supplied window, including the instant records emitted for the OneDev DNS incident.

## Acceptance criteria

- [x] AC-01: A first opening produces one finding, duplicate input preserves it, and matching recovery clears it. Verify operation filtering, unrelated recovery and duration coverage.

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
